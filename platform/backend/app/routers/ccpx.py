"""适配层域。

适配层（:8893 管理 API / :8888 HTTP+UDP / :8889 SOCKS5）原样保留，
这里只做适配与「平台侧记录 vs 适配层实际状态」的对账。
"""
from __future__ import annotations

import asyncio
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..models import Account, AuditLog, utcnow
from ..schemas import AccountIn, AccountPatch
from ..upstream import adapter_creds, ccpx

router = APIRouter()

USERNAME_RE = re.compile(r'^[A-Za-z0-9]+$')


async def _tail_log(path: str, lines: int) -> tuple[str, str]:
    """读日志。

    适配层日志在 /opt/ccpx/ 下（0750 ccpx:ccpx），普通用户读不到，
    所以走 `sudo -n tail`。`-n` 是非交互：没配免密就立刻失败，
    而不是挂在那里等输入密码把请求拖死。
    """
    n = max(1, min(int(lines), 2000))

    def _run() -> tuple[str, str]:
        try:
            proc = subprocess.run(
                ['sudo', '-n', 'tail', '-n', str(n), path],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return '', str(exc)
        if proc.returncode != 0:
            return '', (proc.stderr or '').strip()[:200]
        return proc.stdout, ''

    return await asyncio.to_thread(_run)


@router.get('/status')
async def adapter_status(_user=Depends(current_user)):
    return {'ok': True, 'data': await ccpx.status()}


@router.get('/live')
async def adapter_live(_user=Depends(current_user)):
    """实时连接数。

    适配层按账号维度数的是「当前 / 峰值 / 累计」；一花后台那栏「连接数」
    读的是协议里的 connection（上限）。两者不是一回事 —— 想看谁真的连着，
    只能看这里。
    """
    return {'ok': True, 'data': await ccpx.live()}


@router.get('/accounts')
async def list_accounts(_user=Depends(current_user)):
    rows = await ccpx.accounts()
    # admin / credsError 是「凭据现场」——旧栈也带这两个字段。
    # 适配层回 401 时，光看「上游返回 401」没法判断是密码错、还是
    # 本服务读不到 /opt/ccpx/config.json 于是在用兜底凭据。把用户名
    # 和读取失败原因一起带出去，一眼就能定位。口令本身**绝不回传**。
    user, _pwd, cerr = adapter_creds()
    return {'ok': True, 'data': rows, 'count': len(rows),
            'admin': user, 'credsError': cerr}


@router.get('/creds')
async def creds_state(_user=Depends(current_user)):
    """凭据来源现场（只读，不回传口令）。"""
    user, pwd, cerr = adapter_creds()
    return {'ok': True, 'data': {
        'admin': user,
        'hasPassword': bool(pwd),
        'source': ('file' if settings.ccpx_creds_from_file else 'env'),
        'path': settings.ccpx_config_path,
        'error': cerr,
        'ttl': settings.ccpx_creds_ttl,
    }}


@router.post('/accounts', status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountIn,
                         user=Depends(current_user),
                         db: Session = Depends(get_db)):
    if not body.password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='密码不能为空')

    ok, raw = await ccpx.account_add(
        user=body.username, pwd=body.password,
        date=body.disable_date, tm=body.disable_time,
        enable=1 if body.enabled else 0,
        usepassword=1 if body.require_password else 0,
        autodisable=1 if body.auto_disable else 0,
        connection=str(body.max_conn), bandwidth=str(body.bandwidth),
    )
    _audit(db, user.username, 'account.create', body.username, raw)

    rec = db.scalar(select(Account).where(Account.username == body.username))
    if rec is None:
        rec = Account(username=body.username)
        db.add(rec)
    rec.password = body.password
    rec.enabled = body.enabled
    rec.require_password = body.require_password
    rec.auto_disable = body.auto_disable
    rec.disable_date = body.disable_date
    rec.disable_time = body.disable_time
    rec.max_conn = body.max_conn
    rec.bandwidth = body.bandwidth
    rec.synced = ok
    rec.synced_at = utcnow() if ok else None
    db.commit()

    if not ok:
        # 平台侧已记下，但适配层没收 —— 必须让调用方知道，不能报成功。
        raise HTTPException(502, detail='适配层未确认创建：%s' % raw)
    return {'ok': True, 'username': body.username, 'upstream': raw}


@router.patch('/accounts/{username}')
async def patch_account(username: str, body: AccountPatch,
                        user=Depends(current_user),
                        db: Session = Depends(get_db)):
    if not USERNAME_RE.match(username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='用户名不合法')

    rec = db.scalar(select(Account).where(Account.username == username))
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='平台侧无此账号记录')

    # None = 不改；显式传值才覆盖。这条区分很关键 ——
    # 旧栈用真值判断，导致「用户主动取消勾选」和「没传」被混为一谈。
    pwd = body.password if body.password is not None else rec.password
    enabled = body.enabled if body.enabled is not None else rec.enabled
    require_pw = body.require_password if body.require_password is not None else rec.require_password
    auto_dis = body.auto_disable if body.auto_disable is not None else rec.auto_disable
    ddate = body.disable_date if body.disable_date is not None else rec.disable_date
    dtime = body.disable_time if body.disable_time is not None else rec.disable_time
    conn = body.max_conn if body.max_conn is not None else rec.max_conn
    bw = body.bandwidth if body.bandwidth is not None else rec.bandwidth

    ok, raw = await ccpx.account_edit(
        user=username, pwd=pwd, date=ddate, tm=dtime,
        enable=1 if enabled else 0,
        usepassword=1 if require_pw else 0,
        autodisable=1 if auto_dis else 0,
        connection=str(conn), bandwidth=str(bw),
    )
    _audit(db, user.username, 'account.update', username, raw)

    rec.password = pwd
    rec.enabled = enabled
    rec.require_password = require_pw
    rec.auto_disable = auto_dis
    rec.disable_date = ddate
    rec.disable_time = dtime
    rec.max_conn = conn
    rec.bandwidth = bw
    rec.synced = ok
    rec.synced_at = utcnow() if ok else None
    db.commit()

    if not ok:
        raise HTTPException(502, detail='适配层未确认更新：%s' % raw)
    return {'ok': True, 'username': username, 'upstream': raw}


@router.delete('/accounts/{username}')
async def delete_account(username: str,
                         user=Depends(current_user),
                         db: Session = Depends(get_db)):
    if not USERNAME_RE.match(username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='用户名不合法')

    ok, raw = await ccpx.account_delete(username)
    _audit(db, user.username, 'account.delete', username, raw)

    rec = db.scalar(select(Account).where(Account.username == username))
    if rec is not None:
        db.delete(rec)
        db.commit()

    if not ok:
        raise HTTPException(502, detail='适配层未确认删除：%s' % raw)
    return {'ok': True, 'username': username}


@router.get('/logs')
async def adapter_logs(lines: int = Query(200, ge=1, le=2000),
                       _user=Depends(current_user)):
    out, err = await _tail_log(settings.ccpx_log_path, lines)
    if err:
        raise HTTPException(502, detail=err)
    return {'ok': True, 'data': out}


def _audit(db: Session, actor: str, action: str, target: str, detail: str = '') -> None:
    db.add(AuditLog(actor=actor, action=action, target=target, detail=detail[:2000]))
