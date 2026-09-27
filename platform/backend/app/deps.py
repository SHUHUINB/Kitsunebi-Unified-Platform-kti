"""依赖注入。

**鉴权只在这里收口。** 旧栈的痛点是同一个 handler 里混着鉴权、CSRF、反代、
静态资源，于是长出四条「必须在 X 之前」的隐性契约。现在路由只声明
`Depends(current_user)`，顺序问题不存在了。

令牌来源两处，优先级 Bearer > Cookie：
  * `Authorization: Bearer <jwt>` —— 脚本 / MCP / 前端 fetch
  * HttpOnly Cookie              —— 浏览器会话，免去「token 挂在 query 上」
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .security import decode_token


def _extract_token(request: Request) -> str:
    auth = request.headers.get('authorization') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.cookies.get(settings.cookie_name) or ''


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """要求登录。未登录一律 401，不做静默降级。"""
    token = _extract_token(request)
    payload = decode_token(token) if token else None
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail='未登录或会话已过期',
                            headers={'WWW-Authenticate': 'Bearer'})

    name = str(payload.get('sub') or '')
    user = db.scalar(select(User).where(User.username == name))
    if user is None or not user.enabled:
        # 令牌有效但账号没了/被停用 —— 必须再查一次库，
        # 否则停用账号在令牌过期前依然能用。
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail='账号不存在或已停用')
    return user


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """免登录模式下允许匿名。生产必须把 allow_anon 关掉。"""
    try:
        return current_user(request, db)
    except HTTPException:
        if settings.allow_anon:
            return None
        raise


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail='需要管理员权限')
    return user
