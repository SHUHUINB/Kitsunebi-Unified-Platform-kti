"""卡密域 —— 取代一花 ccproxy_kami（PHP + MySQL，43 个文件 / 约 8042 行）。

按业务实体重新建模，不沿用它的表结构与 SQL 拼接。原系统的三处硬伤在这里
从设计上消失：

  1. **返回码不可信**。一花的 AddUser()/IDelUser() 把 CCProxy 响应读进来就扔，
     无条件回 `code=1`。这里所有写操作返回受影响的行，或直接抛错。
  2. **会话与密码强绑定**。一花会话 id 由 `md5(user.pass.password_hash)` 派生，
     改密码 = 所有会话当场失效。这里会话是独立的 JWT。
  3. **开关只能开不能关**。一花的 UserUpdate() 只在 `enable==0` 时追加
     `enable=1`，没有 else。这里 enabled 是显式布尔字段。
"""
from __future__ import annotations

import json
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import (
    Account, AuditLog, Kami, KamiApp, KamiServer, KamiUser, Setting, utcnow,
)
from ..schemas import (
    KamiAppIn, KamiBatchIn, KamiDeleteIn, KamiIn, KamiServerIn, KamiUserIn,
)
from ..upstream import UpstreamError, ccpx

router = APIRouter()

CODE_ALPHABET = string.ascii_uppercase + string.digits


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _audit(db: Session, actor: str, action: str, target: str, detail: str = '') -> None:
    db.add(AuditLog(actor=actor, action=action, target=target, detail=detail[:2000]))


def _setting(db: Session, key: str, default: str = '') -> str:
    rec = db.get(Setting, key)
    return rec.value if rec else default


def _new_code(db: Session, prefix: str) -> str:
    """生成不重复的卡密。

    先查库再落库之间有窗口，但 code 上有唯一索引兜底 —— 撞了就重试，
    不会写出两条一样的。
    """
    for _ in range(32):
        body = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(12))
        code = '%s-%s-%s' % (prefix, body[:4], body[4:])
        if db.scalar(select(Kami).where(Kami.code == code)) is None:
            return code
    raise HTTPException(500, detail='卡密生成失败：连续 32 次撞号')


def _kami_dict(k: Kami) -> dict:
    now = utcnow()
    if not k.enabled:
        state = 'disabled'
    elif k.activated_at is None:
        state = 'unused'
    elif k.expire_at and k.expire_at < now:
        state = 'expired'
    else:
        state = 'active'
    return {
        'id': k.id,
        'code': k.code,
        'app': k.app.name if k.app else '',
        'appId': k.app_id,
        'server': ('%s:%d' % (k.server.host, k.server.port)) if k.server else '',
        'serverId': k.server_id,
        'owner': k.owner.username if k.owner else '',
        'ownerId': k.owner_id,
        'comment': k.comment,
        'durationHours': k.duration_hours,
        'maxConn': k.max_conn,
        'bandwidth': k.bandwidth,
        'enabled': k.enabled,
        'state': state,
        'usedBy': k.used_by,
        'usedAt': k.used_at.isoformat() if k.used_at else None,
        'activatedAt': k.activated_at.isoformat() if k.activated_at else None,
        'expireAt': k.expire_at.isoformat() if k.expire_at else None,
        'createdAt': k.created_at.isoformat() if k.created_at else None,
    }


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
@router.get('/apps')
async def list_apps(_user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(KamiApp).order_by(KamiApp.id)).all()
    return {'ok': True, 'data': [{
        'id': a.id, 'name': a.name, 'code': a.code or a.name,
        'serverId': a.server_id,
        'server': ('%s:%d' % (a.server.host, a.server.port)) if a.server else '',
        'remark': a.remark, 'enabled': a.enabled,
        'createdAt': a.created_at.isoformat() if a.created_at else None,
    } for a in rows]}


@router.get('/apps/public')
async def public_apps(db: Session = Depends(get_db)):
    """公开应用列表 —— 用户门户的下拉框用，**不要求登录**。

    对应一花 `api/api.php?act=gethostapp`。用户门户（注册/充值/查询）是给
    最终用户看的，他们不可能有管理台账号，所以这个接口必须开放。

    ★ 只回 `code` + `name`，**绝不回 serverId / 出口地址 / 凭据**。
      一花原版回的是 `select appcode,appname from application`，也是这两列 ——
      这里保持一致，不是因为「照抄」，而是因为门户真的只需要这两列。
      多回一个出口地址，等于把后端拓扑摊给所有能打开门户的人。

    ★ 只列 `enabled` 的应用。停用的应用如果还出现在下拉框里，用户选了它、
      填完卡密、点注册，然后拿到「服务器通信出现问题」—— 这个错会让人以为
      卡密坏了，而不是「这个应用已经下线了」。
    """
    rows = db.scalars(
        select(KamiApp).where(KamiApp.enabled.is_(True)).order_by(KamiApp.id)).all()
    return {'code': 1, 'msg': [{'appcode': a.code or a.name, 'appname': a.name}
                               for a in rows]}


@router.post('/apps', status_code=status.HTTP_201_CREATED)
async def create_app(body: KamiAppIn, user=Depends(current_user),
                     db: Session = Depends(get_db)):
    if db.scalar(select(KamiApp).where(KamiApp.name == body.name)):
        raise HTTPException(409, detail='应用名已存在')
    if body.code and db.scalar(select(KamiApp).where(KamiApp.code == body.code)):
        raise HTTPException(409, detail='appcode 已存在：%s' % body.code)
    app = KamiApp(name=body.name, code=body.code, server_id=body.server_id,
                  remark=body.remark, enabled=body.enabled)
    db.add(app)
    db.flush()
    _audit(db, user.username, 'kami.app.create', body.name)
    db.commit()
    return {'ok': True, 'id': app.id}


@router.put('/apps/{app_id}')
async def update_app(app_id: int, body: KamiAppIn, user=Depends(current_user),
                     db: Session = Depends(get_db)):
    app = db.get(KamiApp, app_id)
    if app is None:
        raise HTTPException(404, detail='应用不存在')
    app.name = body.name
    app.code = body.code
    app.server_id = body.server_id
    app.remark = body.remark
    app.enabled = body.enabled
    _audit(db, user.username, 'kami.app.update', str(app_id))
    db.commit()
    return {'ok': True}


@router.delete('/apps/{app_id}')
async def delete_app(app_id: int, user=Depends(current_user),
                     db: Session = Depends(get_db)):
    app = db.get(KamiApp, app_id)
    if app is None:
        raise HTTPException(404, detail='应用不存在')
    # 挂着卡密的应用不允许直接删 —— 否则卡密会静默失去归属，
    # 排查时只能看到一个指向空的外键。
    used = db.scalar(select(func.count()).select_from(Kami).where(Kami.app_id == app_id))
    if used:
        raise HTTPException(409, detail='该应用下还有 %d 条卡密，先转移或删除' % used)
    db.delete(app)
    _audit(db, user.username, 'kami.app.delete', str(app_id))
    db.commit()
    return {'ok': True}


# --------------------------------------------------------------------------- #
# 服务器
# --------------------------------------------------------------------------- #
@router.get('/servers')
async def list_servers(_user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(KamiServer).order_by(KamiServer.id)).all()
    return {'ok': True, 'data': [{
        'id': s.id, 'host': s.host, 'port': s.port, 'username': s.username,
        'remark': s.remark, 'enabled': s.enabled, 'weight': s.weight,
    } for s in rows]}


@router.post('/servers', status_code=status.HTTP_201_CREATED)
async def create_server(body: KamiServerIn, user=Depends(current_user),
                        db: Session = Depends(get_db)):
    dup = db.scalar(select(KamiServer).where(
        KamiServer.host == body.host, KamiServer.port == body.port))
    if dup:
        raise HTTPException(409, detail='该 host:port 已存在')
    srv = KamiServer(**body.model_dump())
    db.add(srv)
    db.flush()
    _audit(db, user.username, 'kami.server.create', '%s:%d' % (body.host, body.port))
    db.commit()
    return {'ok': True, 'id': srv.id}


@router.put('/servers/{server_id}')
async def update_server(server_id: int, body: KamiServerIn,
                        user=Depends(current_user), db: Session = Depends(get_db)):
    srv = db.get(KamiServer, server_id)
    if srv is None:
        raise HTTPException(404, detail='服务器不存在')
    for k, v in body.model_dump().items():
        setattr(srv, k, v)
    _audit(db, user.username, 'kami.server.update', str(server_id))
    db.commit()
    return {'ok': True}


@router.delete('/servers/{server_id}')
async def delete_server(server_id: int, user=Depends(current_user),
                        db: Session = Depends(get_db)):
    srv = db.get(KamiServer, server_id)
    if srv is None:
        raise HTTPException(404, detail='服务器不存在')
    used = db.scalar(select(func.count()).select_from(Kami).where(Kami.server_id == server_id))
    if used:
        raise HTTPException(409, detail='该服务器下还有 %d 条卡密，先转移或删除' % used)
    db.delete(srv)
    _audit(db, user.username, 'kami.server.delete', str(server_id))
    db.commit()
    return {'ok': True}


# --------------------------------------------------------------------------- #
# 卡密用户
# --------------------------------------------------------------------------- #
@router.get('/users')
async def list_kami_users(_user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(KamiUser).order_by(KamiUser.id)).all()
    return {'ok': True, 'data': [{
        'id': u.id, 'username': u.username, 'appId': u.app_id, 'enabled': u.enabled,
        'remark': u.remark,
        'expireAt': u.expire_at.isoformat() if u.expire_at else None,
    } for u in rows]}


@router.post('/users', status_code=status.HTTP_201_CREATED)
async def create_kami_user(body: KamiUserIn, user=Depends(current_user),
                           db: Session = Depends(get_db)):
    from ..security import hash_password
    if db.scalar(select(KamiUser).where(KamiUser.username == body.username)):
        raise HTTPException(409, detail='用户名已存在')
    rec = KamiUser(
        username=body.username,
        password_hash=hash_password(body.password) if body.password else '',
        app_id=body.app_id, enabled=body.enabled, remark=body.remark,
    )
    db.add(rec)
    db.flush()
    _audit(db, user.username, 'kami.user.create', body.username)
    db.commit()
    return {'ok': True, 'id': rec.id}


@router.put('/users/{uid}')
async def update_kami_user(uid: int, body: KamiUserIn,
                           user=Depends(current_user), db: Session = Depends(get_db)):
    from ..security import hash_password
    rec = db.get(KamiUser, uid)
    if rec is None:
        raise HTTPException(404, detail='用户不存在')
    rec.username = body.username
    rec.app_id = body.app_id
    rec.enabled = body.enabled          # 显式布尔 —— 能关也能开
    rec.remark = body.remark
    if body.password:
        rec.password_hash = hash_password(body.password)
    _audit(db, user.username, 'kami.user.update', str(uid))
    db.commit()
    return {'ok': True}


@router.delete('/users/{uid}')
async def delete_kami_user(uid: int, user=Depends(current_user),
                           db: Session = Depends(get_db)):
    rec = db.get(KamiUser, uid)
    if rec is None:
        raise HTTPException(404, detail='用户不存在')
    db.delete(rec)
    _audit(db, user.username, 'kami.user.delete', str(uid))
    db.commit()
    return {'ok': True}


# --------------------------------------------------------------------------- #
# 卡密
# --------------------------------------------------------------------------- #
@router.get('/list')
async def list_kami(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    state: str = Query('', pattern='^(|unused|active|expired|disabled)$'),
    app_id: int | None = None,
    server_id: int | None = None,
    owner_id: int | None = None,
    keyword: str = '',
    _user=Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Kami)
    if app_id is not None:
        stmt = stmt.where(Kami.app_id == app_id)
    if server_id is not None:
        stmt = stmt.where(Kami.server_id == server_id)
    if owner_id is not None:
        stmt = stmt.where(Kami.owner_id == owner_id)
    if keyword:
        like = '%' + keyword + '%'
        stmt = stmt.where(Kami.code.like(like) | Kami.comment.like(like))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Kami.id.desc()).offset((page - 1) * size).limit(size)).all()
    data = [_kami_dict(k) for k in rows]
    if state:
        # state 是计算出来的，SQL 里没有对应列 —— 在内存里过滤。
        # 代价是分页总数会偏大，所以把 total 一起回传，前端不必猜。
        data = [d for d in data if d['state'] == state]
    return {'ok': True, 'data': data, 'total': total, 'page': page, 'size': size}


@router.post('', status_code=status.HTTP_201_CREATED)
async def create_kami(body: KamiIn, user=Depends(current_user),
                      db: Session = Depends(get_db)):
    prefix = _setting(db, 'kami.prefix', 'KM')
    code = body.code.strip() or _new_code(db, prefix)
    if db.scalar(select(Kami).where(Kami.code == code)):
        raise HTTPException(409, detail='卡密已存在：%s' % code)

    rec = Kami(**body.model_dump(exclude={'code'}), code=code)
    db.add(rec)
    db.flush()
    _audit(db, user.username, 'kami.create', code)
    db.commit()
    return {'ok': True, 'id': rec.id, 'code': code}


@router.post('/batch', status_code=status.HTTP_201_CREATED)
async def batch_create(body: KamiBatchIn, user=Depends(current_user),
                       db: Session = Depends(get_db)):
    """批量生成。

    单事务提交：要么全成，要么全不成。旧系统是逐条 insert，中途失败会留下
    一批「生成了但没记录」的卡密，对不上账。
    """
    prefix = _setting(db, 'kami.prefix', 'KM')
    codes: list[str] = []
    for _ in range(body.count):
        code = _new_code(db, prefix)
        db.add(Kami(code=code, app_id=body.app_id, server_id=body.server_id,
                    comment=body.comment, duration_hours=body.duration_hours,
                    max_conn=body.max_conn, bandwidth=body.bandwidth))
        codes.append(code)
        db.flush()          # 让唯一索引立刻生效，避免批内撞号
    _audit(db, user.username, 'kami.batch', '%d 条' % len(codes))
    db.commit()
    return {'ok': True, 'count': len(codes), 'codes': codes}


@router.post('/delete')
async def delete_kami_batch(body: KamiDeleteIn, user=Depends(current_user),
                            db: Session = Depends(get_db)):
    """批量删除卡密 —— 一花 `ajax.php?act=delkami` 的等价物。

    一花的实现是逐条 `delete from kami where kami="..."`，然后把「成功/失败
    条数」拼进 msg。这里一次查出来批量删，响应结构保持兼容（`code`/`msg`），
    另外多回 `deleted` / `missing` / `used` 三个数，让前端不用解析中文。

    ★ 删掉一条**已激活**的卡密，不会回收对应用户在适配层里的账号。
      一花也不回收（它删完就完了）。所以这里把 `used` 数量单独报出来：
      前端必须据此二次确认，否则管理员会以为「删了卡密用户就没了」——
      实际那个账号还能连，只是卡密记录没了，下次排查时查不到来源。
      想真回收账号，要去 CCProxy 面板删账号，或者改用「停用卡密」。
    """
    codes = [c.strip() for c in (body.codes or []) if c and c.strip()]
    if not codes:
        return {'code': -1, 'msg': '删除失败参数为空!'}

    rows = db.scalars(select(Kami).where(Kami.code.in_(codes))).all()
    found = {k.code for k in rows}
    missing = [c for c in codes if c not in found]
    used = [k.code for k in rows if k.activated_at is not None]
    for k in rows:
        db.delete(k)
    _audit(db, user.username, 'kami.delete',
           '%d 条（其中已激活 %d 条）' % (len(rows), len(used)),
           json.dumps(sorted(found), ensure_ascii=False))
    db.commit()

    if missing:
        msg = '删除成功：%d 条，未找到：%d 条' % (len(rows), len(missing))
    else:
        msg = '删除成功'
    return {'code': 1, 'msg': msg, 'deleted': len(rows),
            'missing': missing, 'used': used}


@router.delete('/{kid}')
async def delete_kami(kid: int, user=Depends(current_user),
                      db: Session = Depends(get_db)):
    """删单条卡密（按本地 id）。前端列表里逐行删除用这个。

    与 `/delete` 是同一件事的两个入口：列表行里有 id，批量勾选时只有 code。
    """
    k = db.get(Kami, kid)
    if k is None:
        raise HTTPException(404, detail='卡密不存在')
    code = k.code
    used = k.activated_at is not None
    db.delete(k)
    _audit(db, user.username, 'kami.delete', code, '已激活' if used else '未使用')
    db.commit()
    return {'ok': True, 'code': code, 'wasUsed': used}


@router.get('/stats')
async def kami_stats(_user=Depends(current_user), db: Session = Depends(get_db)):
    now = utcnow()
    total = db.scalar(select(func.count()).select_from(Kami)) or 0
    unused = db.scalar(select(func.count()).select_from(Kami).where(
        Kami.activated_at.is_(None), Kami.enabled.is_(True))) or 0
    expired = db.scalar(select(func.count()).select_from(Kami).where(
        Kami.expire_at.isnot(None), Kami.expire_at < now)) or 0
    disabled = db.scalar(select(func.count()).select_from(Kami).where(
        Kami.enabled.is_(False))) or 0
    return {'ok': True, 'data': {
        'total': total, 'unused': unused, 'expired': expired, 'disabled': disabled,
        'active': max(0, total - unused - expired - disabled),
        'apps': db.scalar(select(func.count()).select_from(KamiApp)) or 0,
        'servers': db.scalar(select(func.count()).select_from(KamiServer)) or 0,
        'users': db.scalar(select(func.count()).select_from(KamiUser)) or 0,
    }}


@router.get('/logs')
async def kami_logs(limit: int = Query(200, ge=1, le=2000),
                    _user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return {'ok': True, 'data': [{
        'ts': r.ts.isoformat() if r.ts else None,
        'actor': r.actor, 'action': r.action, 'target': r.target, 'detail': r.detail,
    } for r in rows]}


# --------------------------------------------------------------------------- #
# 客户端核销（对齐一花 api/cpproxy.php）
# --------------------------------------------------------------------------- #
# 一花的客户端动作有**三个**，不是一个：
#
#   type=insert  用卡密**注册**新代理账号 —— 要求账号尚不存在，密码由用户自填
#   type=update  用卡密给**已存在**账号充值/续费 —— 要求账号已存在，不改密码
#   type=query   查某账号到期时间 —— 只读，返回 HTML 片段
#
# 三者共用一张 kami 表，`state` 0=未使用 / 1=已使用，用完即废（一卡一人一次）。
#
# ★ 契约必须照抄，不能按新后端的审美「优化」：
#   * 返回体是**裸** `{"code":…,"msg":…}`，不套 `{ok,data}` —— 一花面板的
#     JS 直接比 `data.code == 1 / -1 / -2 / -3`，套上 Envelope 会全部落到
#     「未知错误」。
#   * `code` 类型是混的：业务码是**整数** 1/-1/-2/-3，参数错误是**字符串**
#     （"非法参数"/"无效事务"）。老客户端只认那四个整数，字符串原样透传。
#   * 请求体是 **form-urlencoded**（jQuery `$.ajax` 默认），不是 JSON。
#   * `-1` 承载了四种不同拒绝（卡密已使用 / 账号已经存在 / 充值账号不存在 /
#     用户名不合法），靠 `msg` 区分 —— 不能合并成一个笼统的 400。
#   * `query` 的 `msg` 是 **HTML 片段**，页面直接 innerHTML 进去。
#
# ★ 与一花的**有意**差异（都在下面注明）：
#   1. 一花的 `state` 检查与写回之间没有事务，并发核销同一张卡会双开账号。
#      这里用条件 UPDATE 的 rowcount 做原子占位。
#   2. 一花把账号真相放在 CCProxy 的 `/account` 页面里（每次核销都拉全量 HTML
#      再正则解析）。这里走适配层 —— 适配层就是 CCProxy 的代理，同一份真相，
#      但不用在业务代码里解析 HTML。
#
# 一花**没有**的概念（不要发明）：设备绑定、卡密停用。一花只有「用过 / 没用过」。
_CCP_USER_RE = re.compile(r'^[A-Za-z0-9]+$')                      # CheckStrChinese
_CCP_PWD_RE = re.compile(r'^(?![0-9]+$)(?![a-zA-Z]+$)[0-9A-Za-z_]{5,16}$')  # CheckStrPwd


def _reply(code, msg: str = '') -> dict:
    return {'code': code, 'msg': msg}


def _bad_param() -> dict:
    return {'code': '非法参数', 'icon': '5'}


def _s(b: dict, key: str) -> str:
    v = b.get(key)
    return str(v).strip() if v is not None else ''


async def _client_body(request: Request) -> dict:
    """收客户端参数。

    一花面板是 `$.ajax({type:'POST', data:{...}})` → form-urlencoded；
    脚本 / MCP 调用方会发 JSON。两种都收，别为了一个 Content-Type 把老页面
    挡在门外。query string 一并合入 —— 一花用 `$_REQUEST` 取值，
    `?type=insert` 本来就是挂在 URL 上的。
    """
    raw = await request.body()
    ctype = (request.headers.get('content-type') or '').lower()
    out: dict = {}
    if raw:
        text = raw.decode('utf-8', 'replace')
        if 'json' in ctype:
            try:
                got = json.loads(text)
                if isinstance(got, dict):
                    out.update(got)
            except ValueError:
                pass
        else:
            for k, v in parse_qs(text, keep_blank_values=True).items():
                out.setdefault(k, v[0] if v else '')
    for k, v in request.query_params.multi_items():
        out.setdefault(k, v)
    return out


def _hours(k: Kami) -> int:
    """卡密时长（小时）。

    一花存的是 PHP 相对时间串（如 `" +30 day"`）直接喂 `strtotime`；这里存整数
    小时。0 或负值按 24h 兜底 —— 一花算成 `1970-01-01 08:00:00` 时也兜底 +1 day。
    """
    h = int(k.duration_hours or 0)
    return h if h > 0 else 24


def _resolve_app(db: Session, appcode: str) -> KamiApp | None:
    """按一花的 appcode 找应用；后台只填了名字时用名字兜底。"""
    if not appcode:
        return None
    return db.scalar(select(KamiApp).where(
        (KamiApp.code == appcode) | (KamiApp.name == appcode)))


def _resolve_server(db: Session, k: Kami) -> KamiServer | None:
    """出口解析：卡密自己的出口优先，其次应用默认出口。

    一花只有 `application.serverip` 一层；这里的双层是超集，行为向后兼容。
    """
    if k.server is not None:
        return k.server
    if k.app is not None and k.app.server is not None:
        return k.app.server
    return None


def _claim(db: Session, k: Kami, user: str) -> bool:
    """原子占位：只有把 `activated_at` 从 NULL 改成时间的那个请求算抢到。

    条件 UPDATE 的 rowcount 就是互斥判据 —— 读-判断-写三步在并发下会双开，
    加一个 WHERE 条件就没有那个窗口了。立刻 commit，避免把 SQLite 写锁
    一直攥在上游 HTTP 调用的这段时间里。
    """
    now = utcnow()
    n = db.execute(
        update(Kami)
        .where(Kami.id == k.id, Kami.activated_at.is_(None))
        .values(activated_at=now, used_by=user, used_at=now)
    ).rowcount
    db.commit()
    return bool(n)


def _release(db: Session, k: Kami) -> None:
    """占位之后失败 → 把卡密放回去。

    一花只在成功后写 `state=1`，失败不烧卡。这里占位提前了，就必须有对称的
    回滚，否则「上游挂了」会变成「用户的卡没了」。
    """
    db.execute(update(Kami).where(Kami.id == k.id)
               .values(activated_at=None, expire_at=None, used_by='', used_at=None))
    db.commit()


async def _accounts() -> tuple[list[dict], str]:
    """拉适配层的账号表（= CCProxy 的账号真相）。"""
    try:
        return await ccpx.accounts(), ''
    except UpstreamError as exc:
        return [], str(exc)


def _mirror_account(db: Session, user: str, pwd: str, k: Kami, end: datetime) -> None:
    """同步平台侧账号镜像。

    适配层才是执行者（真正鉴权、限速、限连接）；这里记的是「平台认为它长什么样」。
    `synced=True` 表示这次写入是上游确认过的，不是本地一厢情愿。
    """
    rec = db.scalar(select(Account).where(Account.username == user))
    if rec is None:
        rec = Account(username=user)
        db.add(rec)
    if pwd:
        rec.password = pwd
    rec.enabled = True
    rec.require_password = True
    rec.auto_disable = True
    rec.disable_date = end.strftime('%Y-%m-%d')
    rec.disable_time = end.strftime('%H:%M:%S')
    rec.max_conn = k.max_conn
    rec.bandwidth = k.bandwidth
    rec.synced = True
    rec.synced_at = utcnow()


async def _client_insert(b: dict, db: Session) -> dict:
    """type=insert：用卡密注册新账号。"""
    user, pwd, code = _s(b, 'user'), _s(b, 'pwd'), _s(b, 'code')
    if not (user and pwd and code):
        return _bad_param()

    k = db.scalar(select(Kami).where(Kami.code == code))
    if k is None:
        return _reply(-2, '卡密不存在')
    if k.activated_at is not None:
        return _reply(-1, '卡密已被使用')
    if not k.enabled:
        # 一花没有「停用卡密」这个概念，这是本系统的扩展状态。
        # 码位沿用 -1（客户端会显示 msg），措辞如实说停用而不是「已被使用」。
        return _reply(-1, '卡密已停用')

    # 校验顺序照抄一花 insert()：用户名格式 → 长度 → 密码格式。
    if not _CCP_USER_RE.match(user):
        return _reply(-1, '用户名不合法')
    if len(user) < 5:
        return _reply(-1, '用户名长度不合法')
    if not _CCP_PWD_RE.match(pwd):
        return _reply(-1, '密码不合法')

    if _resolve_server(db, k) is None:
        return _reply(-3, '服务器通信出现问题')

    # ① 原子占位（一花缺这一步）。
    if not _claim(db, k, user):
        return _reply(-1, '卡密已被使用')

    # ② 账号是否已存在。一花是拉 /account 全量再逐个比对 —— 同一份真相。
    rows, err = await _accounts()
    if err:
        _release(db, k)
        return _reply(-3, '服务器通信出现问题')
    if any(r.get('user') == user for r in rows):
        _release(db, k)
        return _reply(-1, '账号已经存在')

    # ③ 建号。到期点交给 CCProxy 的 autodisable 自己执行。
    end = utcnow() + timedelta(hours=_hours(k))
    ok, raw = await ccpx.account_add(
        user=user, pwd=pwd,
        date=end.strftime('%Y-%m-%d'), tm=end.strftime('%H:%M:%S'),
        enable=1, usepassword=1, autodisable=1,
        connection=str(k.max_conn), bandwidth=str(k.bandwidth))
    if not ok:
        _release(db, k)
        return _reply(-3, '服务器通信出现问题')

    db.execute(update(Kami).where(Kami.id == k.id).values(expire_at=end))
    _mirror_account(db, user, pwd, k, end)
    _audit(db, user, 'kami.redeem.insert', code, 'user=%s' % user)
    db.commit()
    return _reply(1, '注册用户成功')


async def _client_update(b: dict, db: Session) -> dict:
    """type=update：用卡密给已存在账号充值/续费。"""
    user, code = _s(b, 'user'), _s(b, 'code')
    if not (user and code):
        return _bad_param()

    k = db.scalar(select(Kami).where(Kami.code == code))
    if k is None:
        return _reply(-2, '卡密不存在')
    if k.activated_at is not None:
        return _reply(-1, '卡密已被使用')
    if not k.enabled:
        return _reply(-1, '卡密已停用')

    if _resolve_server(db, k) is None:
        return _reply(-3, '服务器通信出现问题')

    rows, err = await _accounts()
    if err:
        return _reply(-3, '服务器通信出现问题')
    row = next((r for r in rows if r.get('user') == user), None)
    if row is None:
        # 充值路径要求账号**已存在**（与注册路径正好相反）。
        return _reply(-1, '充值账号不存在')
    if not (row.get('date') or '').strip():
        # 一花 updatequer() 拿到空 disabletime → -3 + "账号不存在"。
        # 注意与上面的 -1「充值账号不存在」是**两条不同分支**，措辞不同，照抄。
        return _reply(-3, '账号不存在')

    if not _claim(db, k, user):
        return _reply(-1, '卡密已被使用')

    # 到期叠加规则照抄一花 update()：
    #   expire == 0（未到期）→ 在原到期点上叠加
    #   expire == 1（已到期）→ 从现在起算
    base = utcnow()
    if not int(row.get('expire') or 0):
        try:
            base = datetime.strptime(
                '%s %s' % (row.get('date'), row.get('time')),
                '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except ValueError:
            base = utcnow()
    end = base + timedelta(hours=_hours(k))

    ok, raw = await ccpx.account_edit(
        user=user, pwd='',                       # 一花充值不改密码
        date=end.strftime('%Y-%m-%d'), tm=end.strftime('%H:%M:%S'),
        enable=1, usepassword=int(row.get('pwdstate') or 0) or 1, autodisable=1,
        connection=str(row.get('connection') or k.max_conn),
        bandwidth=str(row.get('bandwidth') or k.bandwidth))
    if not ok:
        _release(db, k)
        return _reply(-3, '服务器通信出现问题')

    db.execute(update(Kami).where(Kami.id == k.id).values(expire_at=end))
    _mirror_account(db, user, row.get('pwd') or '', k, end)
    _audit(db, user, 'kami.redeem.update', code, 'user=%s' % user)
    db.commit()
    return _reply(1, '更新用户成功')


async def _client_query(b: dict, db: Session) -> dict:
    """type=query：查账号到期时间。只读，不改任何状态。"""
    user, appcode = _s(b, 'user'), _s(b, 'appcode')
    if not (user and appcode):
        return _bad_param()

    app = _resolve_app(db, appcode)
    if app is None or app.server is None:
        # 一花这里要经 application.serverip 找到 server_list 才拿得到地址。
        return _reply(-3, '服务器通信出现问题')

    rows, err = await _accounts()
    if err:
        return _reply(-3, '服务器通信出现问题')

    row = next((r for r in rows if r.get('user') == user), None)
    # msg 是 **HTML 片段**：页面直接 innerHTML。返回纯文本会把颜色和
    # 「到期时间：」前缀一起丢掉。
    if row is None or not (row.get('date') or '').strip():
        return _reply(1, '<h5 style="color: red;display: inline;">账号不存在</h5>')
    dt = '%s %s' % (row.get('date'), row.get('time'))
    if int(row.get('expire') or 0):
        return _reply(1, '<h5 style="color: red;display: inline;">到期时间：%s</h5>' % dt)
    return _reply(1, '<h5 style="color: #1E9FFF;display: inline;">到期时间：%s</h5>' % dt)


@router.post('/client')
async def client_dispatch(request: Request, db: Session = Depends(get_db)):
    """一花 `api/cpproxy.php` 的等价单入口：按 `type` 分派。

    保留单入口是为了让现有页面把 `api/cpproxy.php?type=xxx` 直接指过来，
    不用改前端。`del` 不在客户端侧开放 —— 一花那个分支要求 POST 里带
    管理员账号密码，等于把 CCProxy 凭据摊在客户端。
    """
    b = await _client_body(request)
    t = _s(b, 'type').lower()
    if not t:
        return _bad_param()
    if t == 'insert':
        return await _client_insert(b, db)
    if t == 'update':
        return await _client_update(b, db)
    if t == 'query':
        return await _client_query(b, db)
    return _reply('无效事务')


@router.post('/client/insert')
async def client_insert(request: Request, db: Session = Depends(get_db)):
    return await _client_insert(await _client_body(request), db)


@router.post('/client/update')
async def client_update(request: Request, db: Session = Depends(get_db)):
    return await _client_update(await _client_body(request), db)


@router.post('/client/query')
async def client_query(request: Request, db: Session = Depends(get_db)):
    return await _client_query(await _client_body(request), db)


@router.post('/redeem')
async def redeem(request: Request, db: Session = Depends(get_db)):
    """兼容别名 —— 等价于一花的 `type=insert`。

    旧实现是「卡密换一个系统生成的账号密码」，那个契约是**错的**：一花注册时
    账号密码由**用户自己填**，系统只负责建号与写到期时间。现在按一花语义转发，
    返回体也是 `{code, msg}`。
    """
    return await _client_insert(await _client_body(request), db)
