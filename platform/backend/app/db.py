"""引擎、会话与初始化。

SQLite 起步是刻意的：迁移期要能随时把库文件拷走比对，等三系统账号模型
稳定、并发量看清之后再换 PostgreSQL —— 换的时候只动这里。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base, Setting, User
from .security import hash_password, legacy_pbkdf2_string

log = logging.getLogger("rewrite.db")

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine():
    url = settings.resolved_db_url
    if url.startswith("sqlite"):
        # 保证目录存在。sqlite 不会替你建父目录，缺了就是一句
        # "unable to open database file"，看不出是路径问题。
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return create_engine(url, future=True, connect_args={"check_same_thread": False})
    return create_engine(url, future=True, pool_pre_ping=True)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def session_scope() -> Session:
    """脚本里用的短会话，调用方负责 commit。"""
    return get_sessionmaker()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """建表 + 首次播种。幂等，可重复调用。"""
    Base.metadata.create_all(get_engine())
    _seed()


def _seed() -> None:
    import os

    db = session_scope()
    try:
        defaults = {
            "site.name": settings.site_name,
            "kami.prefix": os.environ.get("REWRITE_KAMI_PREFIX", "KM"),
            "kami.default_hours": "24",
        }
        for k, v in defaults.items():
            if db.get(Setting, k) is None:
                db.add(Setting(key=k, value=v))

        # ★ 改名迁移：库里已经种下的旧名字要跟着走，否则「改过名」这件事
        #   在已有部署上等于没发生（`_seed` 只在键不存在时才写）。
        #   判据必须是「**还等于旧默认值**」而不是「存在就改」——
        #   后者会在下次改名时把用户自己设的名字冲掉。
        _row = db.get(Setting, "site.name")
        if _row is not None and _row.value == "CCProxy 管理平台":
            _row.value = settings.site_name

        if db.scalar(select(User).limit(1)) is None:
            username = os.environ.get("REWRITE_ADMIN_USER", "admin")
            password = os.environ.get("REWRITE_ADMIN_PASS", "admin")
            db.add(User(
                username=username,
                email=os.environ.get("REWRITE_ADMIN_EMAIL", ""),
                display=username,
                password_hash=hash_password(password),
                is_admin=True,
            ))
            if password == "admin":
                log.warning("已创建默认账号 admin/admin —— 请立刻修改口令")
            else:
                log.info("已创建初始账号 %s", username)

        db.commit()
    finally:
        db.close()


def import_legacy_users(users_json: Path) -> tuple[int, int]:
    """从旧管理台 users.json 导入账号。

    返回 (导入数, 跳过数)。旧记录是 PBKDF2，verify_password 已能校验，
    所以口令原样搬过来，用户不用重设。
    """
    import json

    if not users_json.is_file():
        return (0, 0)

    try:
        raw = json.loads(users_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("users.json 读取失败：%s", exc)
        return (0, 0)

    rows = raw.get("users") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return (0, 0)

    added = skipped = 0
    db = session_scope()
    try:
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("username") or "").strip()
            if not name or db.scalar(select(User).where(User.username == name)):
                skipped += 1
                continue
            # 规整成 pbkdf2$salt$iters$digest 单串 —— verify_password 认得，
            # 首次改密时会自然升级成 bcrypt。
            salt = str(rec.get("salt") or "")
            digest = str(rec.get("hash") or "")
            if not salt or not digest:
                skipped += 1
                continue
            db.add(User(
                username=name,
                email=str(rec.get("email") or ""),
                display=str(rec.get("display") or name),
                password_hash=legacy_pbkdf2_string(
                    salt, int(rec.get("iterations") or 200000), digest),
            ))
            added += 1
        db.commit()
    finally:
        db.close()
    return (added, skipped)
