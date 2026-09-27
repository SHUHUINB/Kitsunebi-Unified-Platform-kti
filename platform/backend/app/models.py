"""持久化模型。

取代三处分散的账号表示：
  * 旧管理台  users.json        → User
  * 适配层    accounts.db       → Account（本地镜像 + 同步状态）
  * 一花      MySQL 五张表       → KamiApp / KamiServer / KamiUser / Kami / Setting

原一花数据层由 PHP 直接读写 MySQL，字段语义散在 includes/function.php 的
SQL 拼接里。这里按业务实体重新建模，不再沿用它的表结构。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """进出都是「aware UTC」的 datetime 列。

    ★ 为什么需要它 —— 服务器实机跑出来的 bug：
      SQLite 不存时区。写进去的 aware UTC 读回来是 **naive**，于是
      `k.expire_at < utcnow()` 抛
      `TypeError: can't compare offset-naive and offset-aware datetimes`。
      这个坑在本地**永远触发不了**：上游 502 时 `expire_at` 一直是 NULL，
      短路了 `and`。上线后第一次真核销就会让 `/kami/list`、`/kami/stats` 500。

    在**类型层**修，而不是在调用点各写一次 `.replace(tzinfo=...)` ——
    漏一个就是一个 500：
      * bind  ：aware → 换算到 UTC → 去掉 tzinfo（SQLite 只存墙钟）
      * result：贴回 UTC
    这样 `TZDateTime` 才算真的是它字面上的意思，
    SQLite / PostgreSQL 行为一致，`isoformat()` 也带得出 `+00:00`。

    历史行不受影响：过去 bind 进去的就是 aware UTC 的墙钟值，读出来贴 UTC 正好。
    """
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            # 已经是 naive —— 按 UTC 墙钟原样存，不猜本地时区。
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(TZDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# 平台自身
# --------------------------------------------------------------------------- #
class User(Base):
    """平台登录账号（管理台使用者）。"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(190), default="")
    display: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    last_login: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)


class Setting(Base):
    """键值配置，取代一花的 siteinfo。"""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    """写操作留痕。一花的 log 表只记了「谁改了谁」，这里带上动作与结果。"""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = _ts()
    actor: Mapped[str] = mapped_column(String(64), default="", index=True)
    action: Mapped[str] = mapped_column(String(64), default="")
    target: Mapped[str] = mapped_column(String(190), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# --------------------------------------------------------------------------- #
# 代理账号（对应适配层）
# --------------------------------------------------------------------------- #
class Account(Base):
    """代理账号。

    适配层仍是账号的**执行者**（真正鉴权、限速、限连接），这里是平台侧的
    权威记录与同步状态。两个字段分开存，是为了能看出「平台改了但适配层没同步上」。
    """
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password: Mapped[str] = mapped_column(String(190), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    require_password: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_disable: Mapped[bool] = mapped_column(Boolean, default=True)
    disable_date: Mapped[str] = mapped_column(String(32), default="")
    disable_time: Mapped[str] = mapped_column(String(16), default="")
    max_conn: Mapped[int] = mapped_column(Integer, default=-1)
    bandwidth: Mapped[int] = mapped_column(Integer, default=-1)
    # 同步状态
    synced: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------- #
# 卡密域（取代一花 MySQL）
# --------------------------------------------------------------------------- #
class KamiApp(Base):
    """应用。一花里叫 app，卡密挂在应用下。

    `code` 对应一花的 `application.appcode` —— 客户端查询页会把 appcode
    原样回传（`api/cpproxy.php?type=query` 收 `appcode`），所以必须有一个
    与展示名解耦的短标识。留空时按 `name` 解析，这样后台只填名字也能跑。
    """
    __tablename__ = "kami_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 一花的 application 直接指向一个 serverip；这里保留同样的「应用默认出口」，
    # 单张卡密仍可用 Kami.server_id 覆盖。
    server_id: Mapped[int | None] = mapped_column(ForeignKey("kami_servers.id"), default=None)
    remark: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()

    server: Mapped[KamiServer | None] = relationship(lazy="joined")


class KamiServer(Base):
    """卡密可用的代理出口。"""
    __tablename__ = "kami_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(190))
    port: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(64), default="")
    password: Mapped[str] = mapped_column(String(190), default="")
    remark: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (UniqueConstraint("host", "port", name="uq_server_host_port"),)


class KamiUser(Base):
    """卡密系统的下游用户（代理/子账号）。"""
    __tablename__ = "kami_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    app_id: Mapped[int | None] = mapped_column(ForeignKey("kami_apps.id"), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    expire_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_at: Mapped[datetime] = _ts()


class Kami(Base):
    """卡密。

    `code` 是唯一对外凭证；`duration_hours` 与 `expire_at` 分开：
    前者是激活后的有效时长，后者是激活时刻推算出来的到期点。
    未激活时 expire_at 为 NULL —— 这样「未使用」和「已过期」在数据上就分得开。
    """
    __tablename__ = "kami"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("kami_apps.id"), default=None)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("kami_servers.id"), default=None)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("kami_users.id"), default=None)

    comment: Mapped[str] = mapped_column(String(255), default="")
    duration_hours: Mapped[int] = mapped_column(Integer, default=24)
    max_conn: Mapped[int] = mapped_column(Integer, default=1)
    bandwidth: Mapped[int] = mapped_column(Integer, default=-1)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    expire_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    # 核销现场：一花把这三项直接写在 kami 行上（username / use_date / end_date）。
    # 少了 used_by 就回答不了「这张卡给了谁」，只能反查审计日志，而日志是可清的。
    used_by: Mapped[str] = mapped_column(String(64), default="")
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    created_at: Mapped[datetime] = _ts()

    app: Mapped[KamiApp | None] = relationship(lazy="joined")
    server: Mapped[KamiServer | None] = relationship(lazy="joined")
    owner: Mapped[KamiUser | None] = relationship(lazy="joined")

    __table_args__ = (Index("ix_kami_state", "enabled", "expire_at"),)
