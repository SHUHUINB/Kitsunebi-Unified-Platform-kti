"""认证域。

取代旧栈两套并存的会话机制（users.json + 一花密码派生会话），
并去掉「token 挂在 query 上」这一历史包袱。
"""
from __future__ import annotations

import hashlib
import re
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..models import User, utcnow
from ..schemas import LoginIn, PasswordChangeIn
from ..security import create_token, hash_password, verify_password

router = APIRouter()

QQ_EMAIL_RE = re.compile(r'\d{5,12}')


def qq_avatar_url(email: str | None, size: int = 140) -> str:
    """QQ 邮箱头像 = QQ 号头像。

    QQ 邮箱地址本身就是「QQ号@qq.com」，取 @ 前的数字即可。
    腾讯公开接口（无需鉴权）：
        https://q1.qlogo.cn/g?b=qq&nk=<QQ号>&s=<40|100|140|640>
    返回空串表示这个邮箱不是 QQ 邮箱（或号段不像 QQ 号），
    由前端退回首字母头像。
    """
    local = (email or '').split('@')[0].strip()
    if not QQ_EMAIL_RE.fullmatch(local):
        return ''
    return 'https://q1.qlogo.cn/g?b=qq&nk=%s&s=%d' % (local, size)


def fetch_avatar(email: str | None, size: int = 140) -> tuple[bytes | None, str | None]:
    """服务端代取 QQ 头像并落盘缓存，返回 (bytes, content_type)。"""
    url = qq_avatar_url(email, size)
    if not url:
        return None, None
    cache_dir = settings.data_dir / 'avatars'
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:16] + '.img')
    if fp.is_file():
        try:
            return fp.read_bytes(), 'image/jpeg'
        except OSError:
            pass
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ccpx-platform'})
        # 与打本机上游相反：这里是**真的**要出去到 q1.qlogo.cn，
        # 所以不屏蔽环境代理 —— 宿主机有代理时反而才通。
        with urllib.request.urlopen(req, timeout=15) as r:   # noqa: S310
            data = r.read()
            ctype = r.headers.get('Content-Type') or 'image/jpeg'
        if data:
            fp.write_bytes(data)
            return data, ctype
    except Exception:                             # noqa: BLE001
        # 头像取不到不是错误现场 —— 前端退回首字母即可，不能因此 500。
        pass
    return None, None


def _public(user: User) -> dict:
    return {
        'id': user.id,
        'username': user.username,
        'display': user.display or user.username,
        'email': user.email,
        'isAdmin': user.is_admin,
        'lastLogin': user.last_login.isoformat() if user.last_login else None,
    }


@router.post('/login')
async def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    # 账号不存在 / 已停用 / 口令错 —— 一律同一句话。
    # 分开回等于免费送一个账号枚举接口。
    if user is None or not user.enabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail='用户名或口令不正确')

    user.last_login = utcnow()
    db.commit()

    token = create_token(user.username, extra={'uid': user.id})
    response.set_cookie(
        settings.cookie_name, token,
        max_age=settings.jwt_ttl,
        httponly=True,
        samesite='lax',
        # 生产置于 HTTPS 之后时置 1；本机 HTTP 调试置 0，否则浏览器直接不存。
        secure=False,
        path='/',
    )
    return {'ok': True,
            'data': {'token': token, 'user': _public(user),
                     'expiresIn': settings.jwt_ttl}}


@router.post('/logout')
async def logout(response: Response):
    response.delete_cookie(settings.cookie_name, path='/')
    return {'ok': True}


@router.get('/me')
async def me(request: Request, user: User = Depends(current_user)):
    """当前会话。

    ★ 统一走 Envelope `{ok, data:{user}}`：客户端 `data()` 会剥掉一层 data，
      返回 `{user}`，前端 `d.user` 拿到用户。旧实现把 user 平铺在顶层，
      前端只是**碰巧**能用（`data()` 在没有 data 字段时会回退整个对象）——
      这种「靠兜底才没炸」的契约一旦有人给 /me 加上别的顶层字段就会崩。
    """
    pub = _public(user)
    pub['avatar'] = '/api/v1/auth/avatar' if qq_avatar_url(user.email) else ''
    pub['viaCookie'] = not (request.headers.get('authorization') or '').lower().startswith('bearer ')
    pub['noLogin'] = settings.allow_anon
    return {'ok': True, 'data': {'user': pub}}


@router.get('/avatar')
async def avatar(user: User = Depends(current_user)):
    """服务端代取 QQ 头像并落盘缓存。

    为什么不让浏览器直连腾讯：
      1. 管理台是 http，直引 https 图片在部分浏览器会触发混合内容拦截；
      2. 服务端缓存后刷新页面不会每次都打腾讯 CDN；
      3. 前端不必知道 QQ 号，少一处信息外泄。
    """
    data, ctype = await run_in_threadpool(fetch_avatar, user.email or '')
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='该账号没有头像')
    return Response(content=data, media_type=ctype or 'image/jpeg',
                    headers={'Cache-Control': 'private, max-age=86400'})


@router.post('/password')
async def change_password(body: PasswordChangeIn,
                          user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    if not verify_password(body.old, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='原口令不正确')
    # 顺带把旧 PBKDF2 记录升级成 bcrypt —— 改密是唯一的自然升级时机。
    user.password_hash = hash_password(body.new)
    db.commit()
    return {'ok': True}
