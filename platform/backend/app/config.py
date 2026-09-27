"""集中配置。

所有部署细节只在这里出现一次，代码其它地方一律不写死 IP、端口、凭据。
环境变量前缀统一 REWRITE_，方便在同一台机器上和旧栈并存。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


BASE_DIR = Path(__file__).resolve().parent.parent          # backend/


def _resolve_charles_config() -> tuple[Path, str]:
    """Charles 配置路径 + 「我改过它」的说明（没改就是空串）。

    ★ 为什么需要这一步，而不是老老实实读环境变量：

    `REWRITE_CHARLES_CFG` 一旦写进 `rewrite.env`，deploy.sh 的规矩就是
    「已存在，保留不动」—— 于是**坏值会跨版本存活**。实测线上就是这样：

        REWRITE_CHARLES_CFG=/home/ubuntu/.charles/config.xml   ← 文件不存在

    也就是说，只把代码里的默认值改对，**救不了已经在跑的部署**：env 覆盖
    默认值，配置管理继续静默失效，而所有冒烟依然全绿（没有一条断言在问
    「这个文件到底存不存在」）。

    所以这里做一次纠正，并且**绝不静默**：路径不存在、而规范路径
    `~/.charles.config` 存在时，用规范的，并把原因记进 `charles_config_note`，
    由 main.py 在启动日志里以 WARNING 打出来。人一眼就能看到该去修 env。

    注意这不是「猜」：`.charles.config` 是 Charles 自己在 `user.home` 下写的
    单文件，launcher 里写死了 `-Dcharles.config="~/.charles.config"`。规范路径
    是确定的事实，不是启发式。
    """
    raw = _env("REWRITE_CHARLES_CFG", "~/.charles.config")
    p = Path(raw).expanduser()
    if p.exists():
        return p, ""
    canonical = Path("~/.charles.config").expanduser()
    if str(p) != str(canonical) and canonical.exists():
        return canonical, ("REWRITE_CHARLES_CFG=%s 不存在，已自动改用 %s"
                           "（请同步修正 rewrite.env，否则每次启动都会走这条兜底）"
                           % (raw, canonical))
    return p, ""


_CHARLES_CFG, _CHARLES_CFG_NOTE = _resolve_charles_config()


@dataclass(frozen=True)
class Settings:
    # ---- 本服务 ----
    # 机器标识（/healthz 的 app 字段、日志前缀）—— 保持 ASCII，别塞中文：
    # 它是给脚本 grep 的，不是给人念的。
    app_name: str = field(default_factory=lambda: _env(
        "REWRITE_APP_NAME", "kit-soun"))
    # 展示名（页面标题、FastAPI 文档标题、site.name 种子值）。
    # ★ 只在这里定义一次：以前「CCProxy 管理平台」散在前端 3 处 + 后端 2 处，
    #   改名必漏一处，用户会同时看到两个名字。
    site_name: str = field(default_factory=lambda: _env(
        "REWRITE_SITE_NAME", "kit端口统一平台-soun"))
    version: str = "1.0.0"
    host: str = field(default_factory=lambda: _env("REWRITE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("REWRITE_PORT", 8900))
    # 生产环境应置于 Nginx 之后；开发环境放开本机前端端口。
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip() for o in _env(
            "REWRITE_CORS",
            "http://127.0.0.1:5173,http://localhost:5173",
        ).split(",") if o.strip()
    ))

    # ---- 鉴权 ----
    # 未显式设置时生成临时密钥：进程重启即失效，避免「默认密钥上线」。
    jwt_secret: str = field(default_factory=lambda: _env("REWRITE_JWT_SECRET"))
    jwt_alg: str = "HS256"
    jwt_ttl: int = field(default_factory=lambda: _env_int("REWRITE_JWT_TTL", 12 * 3600))
    cookie_name: str = "ccpx_session"
    # 免登录模式：仅用于内网调试，生产必须关闭。
    allow_anon: bool = field(default_factory=lambda: _env_bool("REWRITE_ALLOW_ANON", False))

    # ---- 数据层 ----
    data_dir: Path = field(default_factory=lambda: Path(
        _env("REWRITE_DATA_DIR") or (BASE_DIR / "var")))
    db_url: str = field(default_factory=lambda: _env("REWRITE_DB_URL"))

    # ---- 上游：CCProxy 适配层（既有服务，原样保留）----
    ccpx_admin_base: str = field(default_factory=lambda: _env(
        "REWRITE_CCPX_ADMIN", "http://127.0.0.1:8893"))
    ccpx_admin_user: str = field(default_factory=lambda: _env("REWRITE_CCPX_USER", "admin"))
    ccpx_admin_pass: str = field(default_factory=lambda: _env("REWRITE_CCPX_PASS", ""))
    # 适配层日志。它在 /opt/ccpx/（0750 ccpx:ccpx），普通用户读不到，需要 sudo。
    ccpx_log_path: str = field(default_factory=lambda: _env(
        "REWRITE_CCPX_LOG", "/opt/ccpx/adapter.log"))
    ccpx_unit: str = field(default_factory=lambda: _env("REWRITE_CCPX_UNIT", "ccpx"))
    # 适配层自己的配置文件。里面有 admin_user / admin_password。
    #
    # ★ 为什么要有这个：旧栈每次请求都现读它（`sudo -n cat /opt/ccpx/config.json`），
    #   于是「在适配层改了管理密码」不需要重启管理台。新栈若只认环境变量，
    #   改完密码就得重启一次本服务 —— 而且失败表现是适配层回 401，
    #   前端只会说「上游返回 401」，没人会想到是「管理台还揣着旧密码」。
    #   所以凭据默认从文件现读，环境变量只作兜底（文件读不到时用）。
    ccpx_config_path: str = field(default_factory=lambda: _env(
        "REWRITE_CCPX_CONFIG", "/opt/ccpx/config.json"))
    ccpx_creds_from_file: bool = field(default_factory=lambda: _env_bool(
        "REWRITE_CCPX_CREDS_FROM_FILE", True))
    # 凭据缓存秒数。0 = 每次都读（最忠实，但每次多一个 sudo 进程）。
    # 默认 10 秒：够挡掉 MCP 连打时的 sudo 风暴，又短到改密码几乎立刻生效。
    ccpx_creds_ttl: float = field(default_factory=lambda: float(
        _env("REWRITE_CCPX_CREDS_TTL", "10")))

    # ---- 上游：Charles Web Interface ----
    charles_webif: str = field(default_factory=lambda: _env(
        "REWRITE_CHARLES_WEBIF", "http://127.0.0.1:18888"))
    # Charles 的配置**就是一个单文件**：`~/.charles.config`（XStream 序列化的 XML）。
    #
    # ★ 这里踩过一次真坑，别再改回去。旧版本默认值是 `~/.charles/config.xml`，
    #   而那个路径**在当前部署里根本不存在**（`~/.charles/` 下只有 ca/、certs/、
    #   profiles/、backup/，没有 config.xml）。后果不是报错，是**静默失效**：
    #   `/api/v1/charles/state` 老老实实回 `configSize: 0`，配置页读空，
    #   「增量改动 / 原始 XML」写进一个没人看的文件，Charles 那边毫无反应 ——
    #   而所有冒烟都是绿的，因为没有一条断言在问「这个文件到底存不存在」。
    #
    #   线上实证（2026-09-27）：
    #     `stat ~/.charles.config`            → regular file, 14077 字节
    #     `stat ~/.charles/config.xml`        → No such file or directory
    #     `~/.charles.config` 里有 <registrationConfiguration>（当前授权）
    #   判据来自旧栈 `_console_server.py`：`CFG = os.path.join(HOME, '.charles.config')`
    #   —— 旧栈是对的，重写时被改错了。
    #
    #   ★ 值不是直接读 env，而是走 `_resolve_charles_config()`：它会把「env 里
    #     是坏值、实际用规范路径」这件事记进 `charles_config_note`。原因见那个
    #     函数的注释 —— env 一旦写错就会永久存活，改代码默认值救不了它。
    charles_config: Path = field(default_factory=lambda: _CHARLES_CFG)
    # 空串 = env 与实际情况一致；非空 = 启动日志要打 WARNING（main.py 负责）。
    charles_config_note: str = field(default_factory=lambda: _CHARLES_CFG_NOTE)
    charles_unit: str = field(default_factory=lambda: _env(
        "REWRITE_CHARLES_UNIT", "charles-headless"))
    # Charles 代理端口。Web Interface 挂在代理端口上（不是独立端口），
    # 所以「转发到 Web Interface」和「数监听端口」用的是同一个数。
    charles_http_port: int = field(default_factory=lambda: _env_int(
        "REWRITE_CHARLES_HTTP_PORT", 18888))
    charles_jar: str = field(default_factory=lambda: _env(
        "REWRITE_CHARLES_JAR", "/opt/charles-proxy/lib/charles.jar"))

    # ---- 设备侧（无登录）资源 ----
    # 这些路径由客户端设备直接拉取，不能要求登录 —— 手机上没有管理台账号。
    # Kitsunebi 规则集：放磁盘而不是写死进代码，改规则不用改代码、不用重启。
    #
    # ★ 默认路径是 /opt/rewrite/ruleset.conf，**不是**旧管理台目录下的那个。
    #   旧 charles-console 已被删除，把默认值留在它的目录里会让 /ruleset.conf
    #   在删目录的那一刻静默 404 —— 而这条是手机客户端要拉的东西，断了
    #   表现是「客户端连不上」，跟管理台看不出关系。
    #   deploy.sh 里有一道幂等迁移：新位置不存在且旧位置存在就搬过来。
    ruleset_path: Path = field(default_factory=lambda: Path(_env(
        "REWRITE_RULESET", "/opt/rewrite/ruleset.conf")))
    # Charles 根证书。注意它不在 console 目录下 —— 实测在 ~/.charles/ca/。
    # 旧栈的 ca_info() 找的是 ~/.charles/ca.pem，那个路径在当前部署里不存在，
    # 属于「看起来有兜底、实际永远命中不到」的假兜底，这里按实测路径写。
    charles_ca_dir: Path = field(default_factory=lambda: Path(_env(
        "REWRITE_CHARLES_CA_DIR", "~/.charles/ca")).expanduser())
    charles_ca_cer_name: str = "charles-proxy-ssl-proxying-certificate.cer"
    charles_ca_pem_name: str = "charles-proxy-ssl-proxying-certificate.pem"

    @property
    def charles_ca_cer(self) -> Path:
        return self.charles_ca_dir / self.charles_ca_cer_name

    @property
    def charles_ca_pem(self) -> Path:
        return self.charles_ca_dir / self.charles_ca_pem_name

    # ---- 行为 ----
    upstream_timeout: float = field(default_factory=lambda: float(
        _env("REWRITE_UPSTREAM_TIMEOUT", "30")))
    # 单次上游响应最大读取字节；超出走截断分支而不是把内存吃光。
    max_upstream_bytes: int = field(default_factory=lambda: _env_int(
        "REWRITE_MAX_UPSTREAM", 64 * 1024 * 1024))

    @property
    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return "sqlite:///%s" % (self.data_dir / "platform.db").as_posix()

    @property
    def effective_jwt_secret(self) -> str:
        """未配置密钥时给一个进程级临时值。

        不抛异常是有意的：开发和首次启动不该因为没设环境变量就跑不起来。
        但会打一条醒目告警，避免「以为配了其实没配」。
        """
        if self.jwt_secret:
            return self.jwt_secret
        return _FALLBACK_SECRET or _init_fallback()


_FALLBACK_SECRET = ""


def _init_fallback() -> str:
    global _FALLBACK_SECRET
    import secrets
    _FALLBACK_SECRET = secrets.token_urlsafe(48)
    return _FALLBACK_SECRET


settings = Settings()
