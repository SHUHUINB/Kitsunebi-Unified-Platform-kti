"""请求契约。

只定义**入参**模型：FastAPI 会自动校验并生成 OpenAPI，前端据此拿到类型。
出参统一沿用旧栈的 {ok, data, error} 信封，前端改动面最小。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

HEX_CHARS = set("0123456789abcdefABCDEF")


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
class Envelope(BaseModel):
    ok: bool = True
    error: str | None = None
    data: Any = None


class Page(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(50, ge=1, le=500)


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    """登录入参。

    ★ 故意**不设** `min_length`。空账号 / 空口令必须在 `login()` 里统一回
      「用户名或口令不正确」这一句 401，而不是在校验层变成 422。

      理由有两条，都不是洁癖：
        1. 422 的 `detail` 是 Pydantic 的错误数组，客户端渲染出来是
           `[object Object]` —— 用户看到的是一坨乱码，比「口令不对」还差。
           实测就是这么翻车的：浏览器自动填充没被前端 ref 拿到，
           提交空串 → 422 → 登录页显示乱码 → 用户只能反馈「无法登录」。
        2. 422 和 401 分开回，等于白送一个「这个账号是否存在」的旁路信号。
          统一 401 才是不泄漏的那一种。
    """
    username: str = Field(default='', max_length=64)
    password: str = Field(default='', max_length=256)


class PasswordChangeIn(BaseModel):
    old: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=6, max_length=256)


# --------------------------------------------------------------------------- #
# 代理账号
# --------------------------------------------------------------------------- #
class AccountIn(BaseModel):
    username: str = Field(min_length=5, max_length=64)
    password: str = Field(default="", max_length=190)
    enabled: bool = True
    require_password: bool = True
    auto_disable: bool = True
    disable_date: str = ""
    disable_time: str = ""
    max_conn: int = -1
    bandwidth: int = -1

    @field_validator("username")
    @classmethod
    def _alnum(cls, v: str) -> str:
        # 适配层要求 ^[A-Za-z0-9]+$ 且 >=5 —— 在这里挡住，
        # 免得下游只回一句「用户名不合法」让人猜。
        if not v.isascii() or not v.isalnum():
            raise ValueError("用户名只能由字母/数字组成")
        return v


class AccountPatch(BaseModel):
    """改账号。None 表示「不改这个字段」—— 与「改成空」区分开。"""
    password: str | None = None
    enabled: bool | None = None
    require_password: bool | None = None
    auto_disable: bool | None = None
    disable_date: str | None = None
    disable_time: str | None = None
    max_conn: int | None = None
    bandwidth: int | None = None


# --------------------------------------------------------------------------- #
# WPE 封包编辑器
# --------------------------------------------------------------------------- #
class RuleIn(BaseModel):
    """断点/改写共用条件。至少填一个，否则会命中每一个包。"""
    dir: Literal["up", "down", ""] | None = None
    proto: str | None = None
    target: str | None = None
    hex: str | None = None
    app: str | None = None
    min_len: int | None = Field(None, ge=0)
    max_len: int | None = Field(None, ge=0)

    def has_condition(self) -> bool:
        return any(v not in (None, "", 0) for v in (
            self.dir, self.proto, self.target, self.hex, self.app,
            self.min_len, self.max_len))


class RewriteRuleIn(RuleIn):
    act: Literal["find_replace", "set_hex"] = "find_replace"
    find: str = ""
    replace: str = ""
    label: str = ""

    @field_validator("find", "replace")
    @classmethod
    def _hex_or_text(cls, v: str) -> str:
        return v or ""


class RewriteSetIn(BaseModel):
    on: bool
    rules: list[RewriteRuleIn] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _need_cond(cls, rules: list[RewriteRuleIn]) -> list[RewriteRuleIn]:
        for r in rules:
            if not r.has_condition():
                # 无条件改写会改掉每一个包 —— 适配层也会拒收，这里提前报清楚。
                raise ValueError("每条改写规则至少需要一个匹配条件（dir/proto/target/hex/app/min_len/max_len）")
        return rules


class HoldSetIn(BaseModel):
    on: bool
    # 断点允许空规则（= 拦下全部）；这与改写不同，是有意的。
    rule: RuleIn = Field(default_factory=RuleIn)


class ReleaseIn(BaseModel):
    id: int = Field(ge=1)
    action: Literal["forward", "drop", "modify"] = "forward"
    data: str = ""          # action=modify 时的替换内容


class SendIn(BaseModel):
    proto: Literal["tcp", "udp"] = "tcp"
    target: str = Field(min_length=1)
    data: str = Field(min_length=1)

    @field_validator("data")
    @classmethod
    def _hex(cls, v: str) -> str:
        compact = "".join(v.split())
        if not compact or len(compact) % 2:
            raise ValueError("data 必须是偶数长度的十六进制串")
        bad = set(compact) - HEX_CHARS
        if bad:
            raise ValueError("data 含非十六进制字符：%s" % "".join(sorted(bad)))
        return compact


class ReplayIn(BaseModel):
    id: int = Field(ge=1)
    data: str = ""          # 留空表示按原包重放
    target: str = ""
    proto: str = ""
    times: int = Field(1, ge=1, le=100)


class ImportIn(BaseModel):
    packets: list[dict[str, Any]] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 卡密域（取代一花）
# --------------------------------------------------------------------------- #
class KamiAppIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 对应一花的 application.appcode；留空则客户端按 name 解析。
    code: str = Field("", max_length=32)
    # 应用默认出口（一花的 application.serverip）。
    server_id: int | None = None
    remark: str = ""
    enabled: bool = True


class KamiServerIn(BaseModel):
    host: str = Field(min_length=1, max_length=190)
    port: int = Field(0, ge=0, le=65535)
    username: str = ""
    password: str = ""
    remark: str = ""
    enabled: bool = True
    weight: int = Field(1, ge=0, le=1000)


class KamiUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = ""
    app_id: int | None = None
    enabled: bool = True
    remark: str = ""


class KamiIn(BaseModel):
    code: str = ""
    app_id: int | None = None
    server_id: int | None = None
    owner_id: int | None = None
    comment: str = ""
    duration_hours: int = Field(24, ge=0, le=24 * 3650)
    max_conn: int = Field(1, ge=-1, le=100000)
    bandwidth: int = Field(-1, ge=-1)
    enabled: bool = True


class KamiBatchIn(BaseModel):
    count: int = Field(1, ge=1, le=5000)
    app_id: int | None = None
    server_id: int | None = None
    comment: str = ""
    duration_hours: int = Field(24, ge=0, le=24 * 3650)
    max_conn: int = Field(1, ge=-1, le=100000)
    bandwidth: int = Field(-1, ge=-1)


class KamiDeleteIn(BaseModel):
    """批量删除卡密（一花 `ajax.php?act=delkami` 的 `item` 数组）。

    只按**卡密码**删，不按 id —— 一花就是这么做的，而且卡密码是导出/导入
    清单里唯一稳定的标识；id 只在本地库里有意义。
    """
    codes: list[str] = Field(default_factory=list, max_length=5000)
