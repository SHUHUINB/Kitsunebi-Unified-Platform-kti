"""上游客户端。

对既有服务只做**适配**，不重写它们的语义：

  * CcpxAdmin   → :8893 适配层管理 API（含 14 条 /wpe/*）
  * CharlesWebif→ :18888 Charles Web Interface

适配层（8888/8889/8893）原样保留：它是真在承载流量的代理核心，
UDP 自愈关联、SOCKS5、CONNECT 隧道都是踩坑踩出来的，重写风险远大于收益。

这里逐字保留了旧管理台对 CCProxy 的表单编码与返回体解析 —— 那套正则和
字段名是与适配层 `handle_post` 绑死的，改了就对不上。
"""
from __future__ import annotations

import base64
import http.client
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from .config import settings

# --------------------------------------------------------------------------- #
# CCProxy 返回体解析（九条正则）
#
# 三个字面量约束（来自一花的协议实现，不满足会静默 0 命中）：
#   ① 每个 <input> 独占一行（正则不开 DOTALL）
#   ② name="X" 与 value= 之间必须 >=2 空格
#   ③ checked 是硬编码字符下标：enable=46 / usepassword=51 / autodisable=51
# --------------------------------------------------------------------------- #
CCPX_RE_SPECS = [
    ('username', re.compile(r'<input .* name="username" .* value="(.*?)"', re.I), 1),
    ('password', re.compile(r'<input .* name="password" .* value="(.*?)"', re.I), 1),
    ('enable', re.compile(r'<input .* name="enable" .*', re.I), 0),
    ('usepassword', re.compile(r'<input .* name="usepassword" .*', re.I), 0),
    ('disabledate', re.compile(r'<input .* name="disabledate" .* value="(.*?)"', re.I), 1),
    ('disabletime', re.compile(r'<input .* name="disabletime" .* value="(.*?)"', re.I), 1),
    ('autodisable', re.compile(r'<input .* name="autodisable" .*', re.I), 0),
    ('connection', re.compile(r'<input .* name="connection" .* value="(.*?)"', re.I), 1),
    ('bandwidth', re.compile(r'<input .* name="bandwidth" .* value="(.*?)"', re.I), 1),
]

CCPX_CHECKED_POS = {'enable': 46, 'usepassword': 51, 'autodisable': 51}


def _u(value) -> str:
    """urlencode 单个值。

    一花那边直接拼字符串，值里带 & / = / # 就会把报文拼散架。
    这里 quote 过再拼，适配层用 parse_qs 解，语义一致且不会散架。
    """
    return quote(str(value if value is not None else ''), safe='')


def add_body(user: str, pwd: str, date: str = '', tm: str = '', enable: int = 1,
             usepassword: int = 1, autodisable: int = 1,
             connection='-1', bandwidth='-1') -> str:
    """AddUser 报文。字段名一字不改 —— 适配层按这套名字解析。"""
    return ('add=1&autodisable=%d&enable=%d&usepassword=%d&enablesocks=1&enablewww=0'
            '&enabletelnet=0&enabledial=0&enableftp=0&enableothers=0&enablemail=0'
            '&username=%s&password=%s&ipaddress=&macaddress=&connection=%s'
            '&bandwidth=%s&disabledate=%s&disabletime=%s&userid=-1'
            % (autodisable, enable, usepassword, _u(user), _u(pwd),
               _u(connection), _u(bandwidth), _u(date), _u(tm)))


def edit_body(user: str, pwd: str = '', date: str = '', tm: str = '', enable: int = 1,
              usepassword: int = 1, autodisable: int = 1,
              connection='-1', bandwidth='-1', old: str | None = None) -> str:
    """UserUpdate 报文。`userid` 是查找键，`username` 是改名后的新名。

    密码留空 = 不改（适配层是 `q.get("password") or None`）。
    但 autodisable / connection / bandwidth 适配层是**无条件写**的，
    所以这几个必须每次显式带上当前值，否则会被抹成 0 / 空串。
    """
    return ('edit=1&autodisable=%d&usepassword=%d&enablesocks=1&enablewww=0'
            '&enabletelnet=0&enabledial=0&enableftp=0&enableothers=0&enablemail=0'
            '&username=%s&password=%s&ipaddress=&macaddress=&connection=%s'
            '&bandwidth=%s&disabledate=%s&disabletime=%s&bandwidthquota=4560'
            '&enable=%d&userid=%s'
            % (autodisable, usepassword, _u(user), _u(pwd), _u(connection),
               _u(bandwidth), _u(date), _u(tm), enable, _u(old or user)))


def del_body(user: str) -> str:
    return 'delete=1&userid=%s' % _u(user)


def parse_accounts(text: str) -> tuple[list[dict] | None, str]:
    """解析 /account 返回体，返回 (rows, err)。"""
    got: dict[str, list[str]] = {}
    for key, rx, group in CCPX_RE_SPECS:
        got[key] = [m.group(group) if group else m.group(0) for m in rx.finditer(text)]

    users = got.get('username') or []
    n = len(users)
    bad = [k for k, _r, _g in CCPX_RE_SPECS if len(got[k]) != n]
    if bad:
        # 字段数对不上 = 返回体格式漂了（多半是那三个字面量约束被改动了）。
        # 这时候宁可报错，也不要拿错位的数据糊弄人。
        return None, '字段数不一致：' + ', '.join(
            '%s=%d' % (k, len(got[k])) for k in bad) + '（账号数 %d）' % n

    def flag(key: str, i: int) -> int:
        s = re.sub(r'[<>/]', '', got[key][i])
        return 1 if s.rfind('checked') == CCPX_CHECKED_POS[key] else 0

    now = datetime.now()
    rows = []
    for i, u in enumerate(users):
        dd = got['disabledate'][i] if i < len(got['disabledate']) else ''
        tt = got['disabletime'][i] if i < len(got['disabletime']) else ''
        try:
            exp = datetime.strptime('%s %s' % (dd, tt), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            exp = None
        rows.append({
            'user': u,
            'pwd': got['password'][i] if i < len(got['password']) else '',
            'state': flag('enable', i),
            'pwdstate': flag('usepassword', i),
            'autodisable': flag('autodisable', i),
            'expire': 1 if (exp and now > exp) else 0,
            'expireAt': exp.strftime('%Y-%m-%d %H:%M:%S') if exp else '',
            'date': dd,
            'time': tt,
            'connection': got['connection'][i] if i < len(got['connection']) else '',
            'bandwidth': got['bandwidth'][i] if i < len(got['bandwidth']) else '',
        })
    return rows, ''


# --------------------------------------------------------------------------- #
# 适配层管理 API
# --------------------------------------------------------------------------- #
class UpstreamError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- #
# 适配层管理凭据（现读适配层自己的 config.json）
# --------------------------------------------------------------------------- #
_CREDS_CACHE: tuple[float, str, str, str] = (0.0, '', '', '')
_CREDS_LOCK = threading.Lock()


def adapter_creds(force: bool = False) -> tuple[str, str, str]:
    """返回适配层的 (admin_user, admin_password, error)。

    为什么不是「配置里写死 / 环境变量注入」
    --------------------------------------
    旧栈的做法是**每次请求现读** `/opt/ccpx/config.json`：

        rc, out = sh(['sudo', '-n', 'cat', path])
        return j.get('admin_user'), j.get('admin_password'), ''

    好处是「在适配层侧改了管理密码，管理台不用重启」。新栈如果只认环境变量，
    改完密码的现场是这样的：适配层回 401 → 前端显示「上游返回 401」→
    没人会想到「管理台还揣着旧密码」，而重启一次本服务就好了 ——
    这种「重启就好」的故障最难查。所以默认从文件现读，环境变量只作兜底。

    缓存 10 秒（`REWRITE_CCPX_CREDS_TTL`）：够挡掉 MCP 连打时的 sudo 风暴，
    又短到改密码几乎立刻生效。上游回 401 时会 force 重读一次（见 CcpxAdmin）。
    """
    global _CREDS_CACHE

    if not settings.ccpx_creds_from_file:
        return settings.ccpx_admin_user, settings.ccpx_admin_pass, ''

    now = time.time()
    with _CREDS_LOCK:
        ts, cu, cp, ce = _CREDS_CACHE
        if not force and cu and (now - ts) < settings.ccpx_creds_ttl:
            return cu, cp, ce

    path = settings.ccpx_config_path
    user, pwd, err = '', '', ''
    try:
        proc = subprocess.run(['sudo', '-n', 'cat', path],
                              capture_output=True, timeout=20)
        if proc.returncode != 0:
            raise OSError((proc.stderr or b'').decode('utf-8', 'replace').strip()
                          or 'exit %d' % proc.returncode)
        cfg = json.loads(proc.stdout.decode('utf-8', 'replace'))
        user = cfg.get('admin_user') or ''
        pwd = cfg.get('admin_password') or ''
    except FileNotFoundError:
        # Windows / 没装 sudo —— 不是错误现场，退回环境变量，不写 error。
        return settings.ccpx_admin_user, settings.ccpx_admin_pass, ''
    except Exception as exc:                      # noqa: BLE001
        err = '读取 %s 失败：%s' % (path, exc)
        # 读失败时用环境变量兜底，并把原因带出去 —— 让 /accounts 的
        # credsError 能说清「为什么用的是兜底凭据」。
        user, pwd = settings.ccpx_admin_user, settings.ccpx_admin_pass
        with _CREDS_LOCK:
            _CREDS_CACHE = (now, user, pwd, err)
        return user, pwd, err

    if not user:
        err = '%s 里没有 admin_user' % path
        user, pwd = settings.ccpx_admin_user, settings.ccpx_admin_pass

    with _CREDS_LOCK:
        _CREDS_CACHE = (now, user, pwd, err)
    return user, pwd, err


def adapter_creds_invalidate() -> None:
    """让下一次 adapter_creds() 强制重读 —— 上游回 401 时调用。"""
    global _CREDS_CACHE
    with _CREDS_LOCK:
        _CREDS_CACHE = (0.0, '', '', '')


def local_client(**kw) -> httpx.AsyncClient:
    """打本机上游用的 httpx 客户端 —— **必须** trust_env=False。

    httpx 默认会读 http_proxy / https_proxy / all_proxy / .netrc / SSL_CERT_FILE。
    本服务的上游全在 127.0.0.1（适配层 8893、Charles Web Interface 18888），
    一旦宿主机环境里存在 `http_proxy`（开发机很常见：公司网络、抓包工具、
    Charles 自己都会设），httpx 会把「连本机 8893」也交给代理，
    然后拿回一个**代理侧**的 502：

        upstream connect failed: 由于目标计算机积极拒绝，无法连接。

    这串错误里没有任何字眼指向「代理」，排查时会一路去怀疑适配层没起来、
    端口没开、防火墙 —— 而真正的原因是这台机器上有个 http_proxy 环境变量。
    实测（本机 shell 有 http_proxy=127.0.0.1:19534）：
    不关 trust_env 时 /api/v1/ccpx/status 返回的就是上面那句代理侧 502。

    旧栈用裸 socket / http.client，**不读环境代理**，所以这是重写引入的行为差异。
    关掉 trust_env 才是与旧栈等价、且在任何宿主机上都可预期的行为。
    """
    kw.setdefault('timeout', settings.upstream_timeout)
    return httpx.AsyncClient(trust_env=False, **kw)


class CcpxAdmin:
    """:8893 适配层管理 API。Basic 认证，仅本机可达。

    凭据**每次请求现读**（见 adapter_creds），不缓存成实例属性 ——
    否则「在适配层改了管理密码」就得重启本服务才生效，而失败表现是
    上游 401，前端只会说「上游返回 401」，排查方向会完全跑偏。
    """

    def __init__(self) -> None:
        self._base = settings.ccpx_admin_base.rstrip('/')

    def _headers(self, ctype: str | None = None) -> dict:
        user, pwd, _err = adapter_creds()
        token = base64.b64encode(('%s:%s' % (user, pwd)).encode()).decode()
        h = {
            'Authorization': 'Basic ' + token,
            'Accept': 'application/json, text/plain, */*',
        }
        if ctype:
            h['Content-Type'] = ctype
        return h

    async def _request(self, method: str, path: str, body: bytes = b'',
                       ctype: str | None = None) -> tuple[int, bytes]:
        try:
            async with local_client() as cli:
                resp = await cli.request(method, self._base + path,
                                         content=body or None,
                                         headers=self._headers(ctype))
                # 401/407 = 凭据不对。可能是适配层侧刚改了密码，
                # 而我们还揣着 10 秒前的缓存值 —— 强制重读一次再打一遍。
                # 只重试一次：第二次还 401 就是真凭据不对，如实回给调用方。
                if resp.status_code in (401, 407):
                    adapter_creds_invalidate()
                    resp = await cli.request(method, self._base + path,
                                             content=body or None,
                                             headers=self._headers(ctype))
                return resp.status_code, resp.content
        except httpx.HTTPError as exc:
            # 连不上 / 超时 / 半路断开 —— 一律当成「上游不可用」的 502。
            # 不裹这一层的话它会以 ConnectError 冒到 FastAPI，变成 500，
            # 前端就只能看到「服务器错误」，分不清是自己错了还是适配层没起来。
            raise UpstreamError(502, '适配层不可达（%s）：%s'
                                     % (type(exc).__name__, exc)) from exc

    async def request(self, method: str, path: str, body: bytes = b'',
                      ctype: str | None = None) -> tuple[int, bytes]:
        """通用转发入口 —— WPE 域直接用它，语义不在平台侧重复实现。"""
        return await self._request(method, path, body, ctype)

    async def raw(self, method: str, path: str, body: bytes = b'',
                  ctype: str | None = None) -> tuple[int, dict, bytes]:
        """带响应头/状态码的转发 —— 下载这类要透传 Content-Disposition 的场景用。"""
        user, pwd, _err = adapter_creds()
        token = base64.b64encode(('%s:%s' % (user, pwd)).encode()).decode()
        h = {'Authorization': 'Basic ' + token, 'Accept': '*/*'}
        if ctype:
            h['Content-Type'] = ctype
        try:
            async with local_client() as cli:
                resp = await cli.request(method, self._base + path,
                                         content=body or None, headers=h)
                return resp.status_code, dict(resp.headers), resp.content
        except httpx.HTTPError as exc:
            raise UpstreamError(502, '适配层不可达（%s）：%s'
                                     % (type(exc).__name__, exc)) from exc

    async def get_json(self, path: str) -> dict:
        code, raw = await self._request('GET', path)
        if code != 200:
            raise UpstreamError(code, raw.decode('utf-8', 'replace')[:200])
        try:
            return json.loads(raw.decode('utf-8', 'replace'))
        except ValueError as exc:
            raise UpstreamError(502, '返回体解析失败：%s' % exc) from exc

    async def status(self) -> dict:
        return await self.get_json('/status')

    async def live(self) -> dict:
        return await self.get_json('/live')

    async def accounts(self) -> list[dict]:
        code, raw = await self._request('GET', '/account')
        if code != 200:
            raise UpstreamError(code, raw.decode('utf-8', 'replace')[:200])
        rows, err = parse_accounts(raw.decode('utf-8', 'replace'))
        if err:
            raise UpstreamError(502, '返回体解析失败：' + err)
        return rows or []

    async def account_add(self, **kw) -> tuple[bool, str]:
        return await self._account_post(add_body(**kw), ('已创建', '账号已存在'))

    async def account_edit(self, **kw) -> tuple[bool, str]:
        return await self._account_post(edit_body(**kw), ('已更新',))

    async def account_delete(self, user: str) -> tuple[bool, str]:
        return await self._account_post(del_body(user), ('已删除',))

    async def _account_post(self, payload: str, marks: tuple[str, ...]) -> tuple[bool, str]:
        code, raw = await self._request(
            'POST', '/account', payload.encode('utf-8'),
            'application/x-www-form-urlencoded')
        text = raw.decode('utf-8', 'replace')
        # 适配层的成功判定靠返回体里的中文标记 —— 它不返回结构化状态码，
        # 所以这里只能按标记判，并把原文带回去供排查。
        return (code == 200 and any(m in text for m in marks)), text.strip()[:400]


# --------------------------------------------------------------------------- #
# Charles Web Interface
# --------------------------------------------------------------------------- #
class CharlesWebif:
    """:18888 Charles Web Interface。

    必须发**绝对 URI 请求行**（`GET http://host/path HTTP/1.1`），否则回 503 ——
    这不是 bug，Web Interface 挂在代理端口上，按代理语义解析请求行。
    httpx 不方便直接构造绝对形式，所以这里用 http.client 的 putrequest，
    它的第二个参数就是请求目标，原样写进去即可。
    """

    def get(self, path: str = '/', timeout: float | None = None) -> tuple[int, dict, bytes]:
        from urllib.parse import urlsplit

        base = urlsplit(settings.charles_webif)
        host, port = base.hostname or '127.0.0.1', base.port or 80
        target = '%s%s' % (settings.charles_webif.rstrip('/'), path or '/')

        conn = http.client.HTTPConnection(host, port,
                                          timeout=timeout or settings.upstream_timeout)
        try:
            conn.putrequest('GET', target, skip_host=True, skip_accept_encoding=True)
            conn.putheader('Host', '%s:%d' % (host, port))
            conn.putheader('Accept', '*/*')
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read(settings.max_upstream_bytes)
            headers = {k: v for k, v in resp.getheaders()}
            return resp.status, headers, body
        finally:
            conn.close()


ccpx = CcpxAdmin()
charles = CharlesWebif()
