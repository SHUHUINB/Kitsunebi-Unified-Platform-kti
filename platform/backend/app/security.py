"""密码与令牌。

两件事，别的地方不再各写一份：
  1. 口令哈希 —— 新记录用 bcrypt；同时能校验旧管理台的 PBKDF2 记录，便于平滑迁移。
  2. JWT 签发/校验 —— 取代旧栈「token 挂在 query 上」的做法。

bcrypt 有 72 字节上限，超过会静默截断。这里先做一次 sha256 再交给 bcrypt，
长口令不会因为超出长度而等价。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import bcrypt
import jwt

from .config import settings

BCRYPT_PREFIX = "bcrypt$"
PBKDF2_PREFIX = "pbkdf2$"          # pbkdf2$<salt_hex>$<iterations>$<digest_hex>


def legacy_pbkdf2_string(salt: str, iterations: int, digest_hex: str) -> str:
    """把旧管理台 users.json 的 PBKDF2 记录规整成单串，便于直接入库。"""
    return "%s%s$%d$%s" % (PBKDF2_PREFIX, salt, int(iterations), digest_hex)


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def _prehash(password: str) -> bytes:
    """bcrypt 前的定长化处理，规避 72 字节截断。"""
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    raw = bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=12))
    return BCRYPT_PREFIX + raw.decode("ascii")


def verify_password(password: str, stored: str | dict | None) -> bool:
    """校验口令。

    stored 支持三种形态：
      * "bcrypt$..."                        —— 本服务写入
      * "pbkdf2$<salt>$<iters>$<digest>"    —— 旧管理台记录（迁移期）
      * dict {salt, iterations, hash}       —— 旧 users.json 原始结构
    """
    if not stored:
        return False

    if isinstance(stored, dict):
        return _verify_legacy_pbkdf2(password, stored)

    if not isinstance(stored, str):
        return False

    if stored.startswith(PBKDF2_PREFIX):
        parts = stored.split("$")
        if len(parts) != 4:
            return False
        return _verify_legacy_pbkdf2(
            password, {"salt": parts[1], "iterations": parts[2], "hash": parts[3]})

    if not stored.startswith(BCRYPT_PREFIX):
        return False

    digest = stored[len(BCRYPT_PREFIX):].encode("ascii")
    try:
        return bcrypt.checkpw(_prehash(password), digest)
    except (ValueError, TypeError):
        return False


def _verify_legacy_pbkdf2(password: str, rec: dict) -> bool:
    salt = str(rec.get("salt") or "")
    expected = str(rec.get("hash") or "")
    if not salt or not expected:
        return False
    try:
        iterations = int(rec.get("iterations") or 200000)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), expected)


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def create_token(subject: str, *, extra: dict[str, Any] | None = None,
                 ttl: int | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + int(ttl if ttl is not None else settings.jwt_ttl),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=settings.jwt_alg)


def decode_token(token: str) -> dict[str, Any] | None:
    """解不开就返回 None —— 调用方只需要判「有没有身份」，不需要区分原因。"""
    if not token:
        return None
    try:
        return jwt.decode(token, settings.effective_jwt_secret,
                          algorithms=[settings.jwt_alg])
    except jwt.PyJWTError:
        return None
