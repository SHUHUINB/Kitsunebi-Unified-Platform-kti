"""设备侧入口 —— 给「客户端设备」用，**一律不要求登录**。

为什么单独成域
--------------
管理台那套路由（`/api/*`）都带 token 校验，因为使用者是人，坐在浏览器前。
但这个文件里的路径，使用者是**手机 / 客户端设备**：

    /ok            代理连通性检测页（客户端主动访问）
    /socks         SOCKS 验证页（m.baidu.com 的「远程映射」落点）
    /ruleset.conf  Kitsunebi 规则集（设备「从 URL 更新规则」拉取）
    /ruleset       同上，方便浏览器直接看
    /ca.crt .cer   根证书 DER（Android / Windows）
    /ca.pem        根证书 PEM（iOS / OpenSSL）

手机上不可能填管理台账号。这些路径一旦加上鉴权，整条客户端链路就断了 ——
而断的方式很隐蔽：设备只会显示「连接失败」，没人会想到是服务端在要 token。

两条硬约束
----------
1. **必须注册在 SPA 通配路由之前**。`/{full_path:path}` 会把 `/ok` 也兜走，
   回一份 index.html（200，看起来「正常」），而客户端拿到的是 HTML 不是证书。
2. **判定来源 IP 只能读 socket 对端，不能读任何请求头**。`/ok` 与 `/socks`
   的结论建立在「服务端看到的来源地址」上，这是不可伪造的硬证据；
   一旦改成读 `X-Forwarded-For`，客户端自己就能把自己伪装成「走过了代理」，
   这个页面就从检测工具变成安慰剂。所以本模块**不读 XFF** —— 代价是：
   如果在前面加了反向代理，peer 会恒为 127.0.0.1，本页判定失效。
   上线拓扑是「本服务直接对外提供 8890」，没有中间代理，判据成立。
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
import subprocess
import time
import uuid
from datetime import datetime
from string import Template

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..charles_store import CFG, charles_pid, svc_active
from ..config import settings

router = APIRouter()

LOCAL_IPS = ('127.0.0.1', '::1', '::ffff:127.0.0.1')


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _esc(t) -> str:
    """HTML 转义。来源地址虽来自 socket 不可伪造，回显前一律转义。"""
    return html.escape(str(t), quote=True)


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else '') or ''


def _server_port(request: Request) -> int:
    return request.url.port or settings.port


def charles_listen_ports() -> list[int]:
    """Charles 进程当前实际监听的端口（去重升序）。

    不硬编码 8888：`dynamicHTTPPort=true` 时 Charles 每次重启都换端口。
    """
    from ..charles_store import sh
    pid = charles_pid()
    if not pid:
        return []
    _rc, out = sh(['bash', '-c',
                   "ss -tlnp 2>/dev/null | grep -F 'pid=%s,'" % pid], timeout=15)
    ports: set[int] = set()
    for line in out.strip().split('\n'):
        f = line.split()
        if len(f) > 3 and ':' in f[3]:
            try:
                ports.add(int(f[3].rsplit(':', 1)[1]))
            except ValueError:
                pass
    return sorted(ports)


def proxy_endpoints() -> tuple[int | None, int | None, bool]:
    """推断 HTTP / SOCKS 代理端口，返回 (http_port, socks_port, socks_enabled)。

    开了动态端口后「配置里写的端口」和「实际在听的端口」会不一致，
    必须配置 + 实测两边对着推。只看配置会拿到一个根本没在听的端口号。
    """
    import xml.etree.ElementTree as ET
    http_p: int | None = None
    socks_p: int | None = None
    socks_on = False
    try:
        root = ET.parse(CFG).getroot()
        pc = root.find('proxyConfiguration')
        if pc is None:
            return None, None, False

        def txt(tag: str, dflt: str = '') -> str:
            e = pc.find(tag)
            return (e.text or '').strip() if e is not None else dflt

        def as_int(s: str) -> int:
            try:
                return int(s or 0)
            except ValueError:
                return 0

        socks_on = txt('enableSOCKSProxy', 'false').lower() == 'true'
        dyn_h = txt('dynamicHTTPPort', 'false').lower() == 'true'
        dyn_s = txt('dynamicSOCKSPort', 'false').lower() == 'true'
        cfg_h, cfg_s = as_int(txt('port', '0')), as_int(txt('SOCKSPort', '0'))

        live = set(charles_listen_ports())
        if not dyn_h and cfg_h in live:
            http_p = cfg_h
        if socks_on and not dyn_s and cfg_s in live:
            socks_p = cfg_s

        rest = sorted(p for p in live if p not in (http_p, socks_p))
        if http_p is None and rest:
            http_p = rest.pop(0)
        if socks_on and socks_p is None and rest:
            socks_p = rest.pop(0)
    except Exception:                             # noqa: BLE001
        # 读配置失败不该让检测页 500 —— 页面上会显示「未检测到」，
        # 那本身就是有用的信息（说明 Charles 侧没配好）。
        pass
    return http_p, socks_p, socks_on


def _endpoint_text() -> tuple[str, str, list[int], str]:
    """三个页面共用的一行现场：HTTP 端口 / SOCKS 端口 / 全部监听端口 / 服务状态。"""
    ports = charles_listen_ports()
    http_p, socks_p, socks_on = proxy_endpoints()
    http_txt = str(http_p) if http_p else '（未检测到）'
    if not socks_on:
        socks_txt = '已关闭'
    elif socks_p:
        socks_txt = str(socks_p)
    else:
        socks_txt = '（已启用但未检测到）'
    return http_txt, socks_txt, ports, svc_active()


# --------------------------------------------------------------------------- #
# 代理连通性检测页（/ok）
# --------------------------------------------------------------------------- #
_CONN_TPL = Template('''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>代理连接检测</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;padding:28px 18px;background:#f5f6f8;color:#1c1e21;line-height:1.6}
.box{max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2e5e9;
  border-radius:12px;padding:26px 30px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.badge{display:inline-block;padding:5px 14px;border-radius:999px;font-size:13px;
  font-weight:600;letter-spacing:.02em}
.ok{background:#e6f7ec;color:#0a7d33;border:1px solid #b6e5c6}
.bad{background:#fff4e5;color:#9a5b00;border:1px solid #f2d9ac}
h1{margin:14px 0 6px;font-size:21px}
.desc{margin:0 0 20px;font-size:13.5px;color:#5a6470}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-bottom:18px}
td{padding:9px 0;border-bottom:1px solid #eef0f3;vertical-align:top}
td.k{width:150px;color:#6b7480}
td.v{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.hint{background:#f0f4fb;border:1px solid #d5e0f2;border-radius:8px;
  padding:14px 16px;font-size:13px;color:#33456b}
.hint b{color:#1f3c6b}
code{background:#eceef1;padding:2px 6px;border-radius:4px;font-size:12.5px}
ul{margin:8px 0 0;padding-left:20px}
li{margin:4px 0}
</style></head>
<body><div class="box">
$badge
<h1>$title</h1>
<p class="desc">$desc</p>
<table>
<tr><td class="k">服务端看到你的地址</td><td class="v">$ip</td></tr>
<tr><td class="k">服务器时间</td><td class="v">$now</td></tr>
<tr><td class="k">Charles 进程</td><td class="v">$svc</td></tr>
<tr><td class="k">HTTP 代理端口</td><td class="v">$http_port</td></tr>
<tr><td class="k">SOCKS 代理端口</td><td class="v">$socks_port</td></tr>
<tr><td class="k">全部监听端口</td><td class="v">$ports</td></tr>
<tr><td class="k">本页端口</td><td class="v">$sport</td></tr>
</table>
$hint
</div></body></html>
''')


def conn_page(client_ip: str, host_header: str | None, server_port: int) -> str:
    """代理连通性检测页。

    判定依据（Charles 与本服务同机部署时成立）：
      直连   -> 服务端看到的来源地址是客户端自己的 IP
      经代理 -> Charles 从本机发起连接，来源地址变成 127.0.0.1

    这是服务端可判定的硬证据，不依赖客户端自报，也不依赖任何请求头。
    """
    ip = (client_ip or '').strip()
    via = ip in LOCAL_IPS
    http_txt, socks_txt, ports, svc = _endpoint_text()
    http_p, socks_p, socks_on = proxy_endpoints()
    hostip = (host_header or '').split(':')[0] or '本机'

    if via:
        badge = '<div class="badge ok">连接成功</div>'
        title = '请求已通过代理到达'
        desc = ('这次请求经过了 Charles 转发。服务端看到的来源地址是 127.0.0.1 —— '
                '因为 Charles 与本页跑在同一台服务器上，是由它代你发起连接的。')
        hint = ('<div class="hint"><b>链路已确认。</b>代理配置正确，'
                'Charles 正在正常转发流量，可以开始抓包了。'
                '<br><br><b>注意：</b>本页只判定「有没有经过代理」，'
                '<b>不区分 HTTP 代理还是 SOCKS 代理</b> —— 两种方式都表现为来源 127.0.0.1，'
                '服务端无法分辨。要确认走的哪种，以客户端的代理设置为准。</div>')
    else:
        badge = '<div class="badge bad">直连（未经过代理）</div>'
        title = '这次请求没有走代理'
        desc = ('服务端看到的是你自己的地址，说明请求是直连到达的。'
                '如果你本来想验证代理链路，请检查客户端的代理设置。')
        lines = ['<li>代理地址：<code>%s</code></li>' % _esc(hostip)]
        if http_p:
            lines.append('<li>HTTP / HTTPS 代理端口：<code>%d</code></li>' % http_p)
        if socks_on and socks_p:
            lines.append('<li>SOCKS 代理端口：<code>%d</code>（类型选 SOCKS5）</li>' % socks_p)
        elif socks_on:
            lines.append('<li>SOCKS 代理：已启用，但没取到端口 —— 确认 Charles 在运行</li>')
        hint = ('<div class="hint"><b>怎么配：</b><ul>%s</ul>'
                '配好后重新打开本页。若端口每次重启都不一样，说明 Charles 开了'
                '「动态端口」，可到管理台「代理设置」里关掉以固定端口。</div>'
                % ''.join(lines))

    return _CONN_TPL.substitute(
        badge=badge, title=title, desc=desc, hint=hint,
        ip=_esc(ip), now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        svc=_esc(svc), http_port=_esc(http_txt), socks_port=_esc(socks_txt),
        ports=(_esc(', '.join(str(p) for p in ports)) if ports else '（无）'),
        sport=str(server_port))


# --------------------------------------------------------------------------- #
# SOCKS 代理验证页（/socks）—— m.baidu.com 的「远程映射」落点
# --------------------------------------------------------------------------- #
_SOCKS_TPL = Template('''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOCKS 代理验证</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;padding:28px 18px;background:#f3f7f4;color:#15211a;line-height:1.6}
.box{max-width:640px;margin:0 auto;background:#fff;border:1px solid #dbe7df;
  border-radius:14px;padding:28px 32px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.badge{display:inline-block;padding:6px 16px;border-radius:999px;font-size:13.5px;
  font-weight:700;letter-spacing:.03em}
.ok{background:#e3f7ea;color:#0a7a33;border:1px solid #a9e0bf}
.bad{background:#fff4e5;color:#9a5b00;border:1px solid #f2d9ac}
h1{margin:16px 0 8px;font-size:22px;letter-spacing:.01em}
.desc{margin:0 0 20px;font-size:13.5px;color:#55635b}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-bottom:18px}
td{padding:9px 0;border-bottom:1px solid #eef2ef;vertical-align:top}
td.k{width:168px;color:#6d7a72}
td.v{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.hint{background:#f0f6f9;border:1px solid #d6e4ec;border-radius:9px;
  padding:14px 16px;font-size:13px;color:#2f4756}
.hint b{color:#1c3342}
code{background:#eceff0;padding:2px 6px;border-radius:4px;font-size:12.5px}
ul{margin:8px 0 0;padding-left:20px}
li{margin:4px 0}
</style></head>
<body><div class="box">
$badge
<h1>$title</h1>
<p class="desc">$desc</p>
<table>
<tr><td class="k">服务端看到你的地址</td><td class="v">$ip</td></tr>
<tr><td class="k">服务器时间</td><td class="v">$now</td></tr>
<tr><td class="k">Charles 进程</td><td class="v">$svc</td></tr>
<tr><td class="k">HTTP 代理端口</td><td class="v">$http_port</td></tr>
<tr><td class="k">SOCKS 代理端口</td><td class="v">$socks_port</td></tr>
<tr><td class="k">全部监听端口</td><td class="v">$ports</td></tr>
<tr><td class="k">本页端口</td><td class="v">$sport</td></tr>
<tr><td class="k">远程映射规则</td><td class="v">$map_rule</td></tr>
</table>
$hint
</div></body></html>
''')


def socks_page(client_ip: str, host_header: str | None, server_port: int,
               query: dict | None = None) -> str:
    """SOCKS 代理验证页 —— 给「访问 m.baidu.com」这个动作当落点。

    和 /ok 的分工
    -------------
    /ok 是客户端**主动去访问**的检测页；本页是**被映射过来的**落点：
    Charles 里配了「远程映射」m.baidu.com -> 127.0.0.1:8890/socks?via=map。
    客户端只要配了代理（SOCKS 或 HTTP），请求就会先到 Charles，
    再被改写到本页 —— 于是「能打开这一页」本身就等于「代理通了」。
    没配代理的客户端访问 m.baidu.com 会直接去真的百度，永远看不到本页。

    两级判据（为什么不能只看来源 IP）
    --------------------------------
    1) 强判据：URL 带 `?via=map`。这个参数只存在于「远程映射」的 destLocation 里，
       只有请求真的被 Charles 改写过来才会带上 —— 是链路生效的直接证据。
    2) 弱判据：来源地址是 127.0.0.1（本服务与 Charles 同机，由 Charles 代发）。
       单靠它会把「在服务器上直接 curl 本页」也判成代理通了，所以降为辅助。
    """
    ip = (client_ip or '').strip()
    marker = ((query or {}).get('via') or [''])[0] == 'map'
    local_ip = ip in LOCAL_IPS
    http_txt, socks_txt, ports, svc = _endpoint_text()
    http_p, socks_p, socks_on = proxy_endpoints()
    hostip = (host_header or '').split(':')[0] or '本机'
    map_rule = 'm.baidu.com  ->  127.0.0.1:%s/socks?via=map' % server_port

    if marker:
        badge = '<div class="badge ok">验证成功</div>'
        title = '代理链路验证通过'
        desc = ('你请求的是 m.baidu.com，但这个请求并没有真的发到百度 —— '
                '它被 Charles 按「远程映射」规则改写到了本验证页'
                '（URL 里带上了 <code>?via=map</code> 标记，这是链路生效的直接证据）。'
                '能看到这一页，就说明客户端代理配置生效、流量确实经过了代理。')
        hint = ('<div class="hint"><b>链路已确认。</b>客户端 → 代理 → 目标 的整条路径是通的，'
                'Charles 正在正常转发流量，可以开始抓包了。'
                '<br><br><b>关于 HTTP / SOCKS：</b>本页只判定「请求有没有经过 Charles」，'
                '<b>不区分 HTTP 代理（%s）还是 SOCKS 代理（%s）</b> —— 两者在服务端都表现为'
                '来源 127.0.0.1，无法分辨。'
                '<br>要确认走的是 SOCKS：在客户端<b>只</b>填 SOCKS 代理（不填 HTTP 代理），'
                '类型选 <code>SOCKS5</code>，再重新打开 <code>http://m.baidu.com</code>。'
                '</div>' % (_esc(http_txt), _esc(socks_txt)))
    elif local_ip:
        badge = '<div class="badge bad">本机直连</div>'
        title = '请求来自服务器本机，没经过映射'
        desc = ('这个请求是从服务器本机直接打过来的（没有 <code>?via=map</code> 标记）。'
                '请从客户端设备配好代理后访问 <code>http://m.baidu.com</code>，'
                '这样才会真正走一遍代理链路。')
        hint = ('<div class="hint"><b>注意：</b>在服务器上直接 curl 本页会看到这个结果 —— '
                '它是本机回环，不经过 Charles，所以不算验证通过。'
                '真正的验证要在客户端设备上做。</div>')
    else:
        badge = '<div class="badge bad">未经过代理</div>'
        title = '这个请求没有走代理'
        desc = ('你是直接打开本页的 —— 服务端看到的是你自己的地址。'
                '本页的用途是作为 <code>m.baidu.com</code> 的映射落点，'
                '请先配好代理，再去访问 <code>m.baidu.com</code>。')
        lines = ['<li>代理地址：<code>%s</code></li>' % _esc(hostip)]
        if socks_on and socks_p:
            lines.append('<li>代理类型选 <code>SOCKS5</code>，端口填 <code>%d</code></li>' % socks_p)
        if http_p:
            lines.append('<li>（若用 HTTP 代理）端口填 <code>%d</code></li>' % http_p)
        if not socks_on:
            lines.append('<li>SOCKS 代理当前是关闭的 —— 到管理台「代理设置」里打开</li>')
        hint = ('<div class="hint"><b>怎么测：</b><ul>%s</ul>'
                '配好后在浏览器/客户端里访问 <code>http://m.baidu.com</code>。'
                '没配代理时它会直接去真的百度，<b>只有走代理才会落到本页</b>。'
                '<br><br>用 https 访问（<code>https://m.baidu.com</code>）需要客户端先信任 '
                'Charles 的根证书，否则会报证书错误；<b>用 http 最省事</b>。</div>'
                % ''.join(lines))

    return _SOCKS_TPL.substitute(
        badge=badge, title=title, desc=desc, hint=hint,
        ip=_esc(ip), now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        svc=_esc(svc), http_port=_esc(http_txt), socks_port=_esc(socks_txt),
        ports=(_esc(', '.join(str(p) for p in ports)) if ports else '（无）'),
        sport=str(server_port), map_rule=_esc(map_rule))


# --------------------------------------------------------------------------- #
# Kitsunebi 规则集（/ruleset.conf）
# --------------------------------------------------------------------------- #
def ruleset_text() -> str:
    """读取 Kitsunebi 规则集文件。

    放在磁盘上而不是硬编码进代码：改规则集只需覆盖文件，不用改代码、不用重启服务。
    文件不存在时返回一段可读的说明，而不是抛 500 —— 客户端拉取失败时
    至少能看到原因，不用去翻服务器日志。
    """
    path = str(settings.ruleset_path)
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.read()
    except OSError as exc:
        return ('// Kitsunebi 规则集文件不存在\n'
                '// 期望路径: %s\n'
                '// 原因: %s\n'
                '// 把规则集文件放到该路径即可生效，无需重启服务。\n'
                % (path, exc))


# --------------------------------------------------------------------------- #
# 根证书下载（/ca.crt /ca.pem ...）
# --------------------------------------------------------------------------- #
def ca_blob(fmt: str = 'der') -> tuple[bytes | None, str | None]:
    """读取 Charles 根证书，返回 (bytes, 下载文件名)；读不到返回 (None, None)。

    手机端拿到它之后导入系统信任库，https 映射才会通过证书校验。
    DER（.cer/.crt）给 Android / Windows；PEM 给 iOS / OpenSSL。
    """
    path, name = ((settings.charles_ca_pem, 'charles-proxy-ca.pem') if fmt == 'pem'
                  else (settings.charles_ca_cer, 'charles-proxy-ca.crt'))
    try:
        with open(path, 'rb') as fh:
            return fh.read(), name
    except OSError:
        return None, None


def _sha256_der(blob: bytes) -> str:
    """DER 字节的 SHA-256，格式化成 `AA:BB:...` 大写十六进制。

    ★ 这个值就是「证书指纹」，和系统里「查看证书 → 指纹」显示的是同一个东西。
      必须让用户看得见 —— 装完证书要能核对，否则就是闭眼信任一个来路不明的根证书。
      服务端算出来给用户抄，比让用户自己在手机上找要可靠得多。
    """
    d = hashlib.sha256(blob).hexdigest().upper()
    return ':'.join(d[i:i + 2] for i in range(0, len(d), 2))


def _openssl_meta(der_path: str) -> dict:
    """用 openssl 读出主题 / 颁发者 / 有效期。读不到就返回空 dict。

    ★ 不引 `cryptography` 解析 X.509：为了三行展示多背一个依赖不划算，
      而且 openssl 是 Ubuntu 基础镜像自带的。读不到（没装 openssl / 文件坏）
      时页面照常显示指纹与下载按钮 —— 缺的是补充信息，不是核心功能。
    """
    try:
        out = subprocess.run(
            ['openssl', 'x509', '-inform', 'DER', '-in', der_path, '-noout',
             '-subject', '-issuer', '-dates'],
            capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return {}
        meta: dict[str, str] = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            for key, label in (('subject=', 'subject'), ('issuer=', 'issuer'),
                               ('notBefore=', 'notBefore'), ('notAfter=', 'notAfter')):
                if line.startswith(key):
                    meta[label] = line[len(key):].strip()
        return meta
    except (OSError, subprocess.SubprocessError):
        return {}


def ca_meta() -> dict:
    """证书现场 —— 引导页与 `/_device` 共用。

    `ready=False` 时页面要如实说「证书还没生成」，而不是给一个点了没反应的按钮。
    """
    blob, _name = ca_blob('der')
    if blob is None:
        return {'ready': False, 'error': '证书文件不存在: %s' % settings.charles_ca_cer,
                'der': str(settings.charles_ca_cer), 'pem': str(settings.charles_ca_pem)}
    meta = _openssl_meta(str(settings.charles_ca_cer))
    return {
        'ready': True,
        'sha256': _sha256_der(blob),
        'size': len(blob),
        'der': str(settings.charles_ca_cer),
        'pem': str(settings.charles_ca_pem),
        'subject': meta.get('subject', ''),
        'issuer': meta.get('issuer', ''),
        'notBefore': meta.get('notBefore', ''),
        'notAfter': meta.get('notAfter', ''),
    }


def mobileconfig_bytes(host: str) -> tuple[bytes | None, str]:
    """生成 iOS / macOS 配置描述文件（.mobileconfig）—— 真正的「一键安装」。

    为什么必须有它
    --------------
    iOS 上「下载 .crt 文件」得到的是文件 App 里一个打不开的附件，用户要自己
    进「设置 → 通用 → VPN与设备管理」翻出来装，再进「关于本机 → 证书信任设置」
    手动打开开关。三步里任何一步没走对，表现都是「https 还是报证书错误」，
    而用户以为已经装好了。`.mobileconfig` 是 Apple 官方的配置描述文件格式：
    点一下 → 系统直接弹「安装描述文件」→ 输密码 → 装进受信任根证书。
    这是 iOS 上唯一真正意义上的「一键安装」。

    PayloadUUID 用**证书指纹派生**（uuid5），不是随机数：
    同一张证书反复生成得到同一个 UUID，重复安装是**替换**而不是叠加出多份
    同名描述文件；换证书（CA 重新生成）UUID 自然变化，会装成新的。
    用随机 UUID 的话，用户每点一次就多一条记录，设备管理里越堆越多。

    签名说明：本文件**未签名**。iOS 会标「未验证」但仍可安装（用户点「仍然安装」）。
    要签需要 Apple 开发者证书，本场景没有 —— 如实告知，不假装是签过名的。
    """
    blob, name = ca_blob('der')
    if blob is None:
        return None, 'charles-proxy-ca.mobileconfig'

    fp = hashlib.sha256(blob).hexdigest()
    ns = uuid.UUID('6f1a2c34-5b7d-4e8f-9a01-2b3c4d5e6f70')
    ca_uuid = str(uuid.uuid5(ns, 'ca:' + fp))
    prof_uuid = str(uuid.uuid5(ns, 'profile:' + fp))
    b64 = base64.b64encode(blob).decode('ascii')

    # 描述文件里的字符串一律 XML 转义 —— 主机名来自 Host 头，属于外部输入。
    disp = html.escape('Charles 抓包根证书', quote=True)
    org = html.escape('CCProxy Platform', quote=True)
    host_e = html.escape(host, quote=True)

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '  <key>PayloadContent</key>\n  <array>\n    <dict>\n'
        '      <key>PayloadCertificateFileName</key>\n      <string>%s</string>\n'
        '      <key>PayloadContent</key>\n      <data>%s</data>\n'
        '      <key>PayloadDescription</key>\n'
        '      <string>安装后，本设备将信任由 %s 上的 Charles 签发的 HTTPS 证书。</string>\n'
        '      <key>PayloadDisplayName</key>\n      <string>%s</string>\n'
        '      <key>PayloadIdentifier</key>\n'
        '      <string>com.ccpx.charles.rootca</string>\n'
        '      <key>PayloadType</key>\n      <string>com.apple.security.root</string>\n'
        '      <key>PayloadUUID</key>\n      <string>%s</string>\n'
        '      <key>PayloadVersion</key>\n      <integer>1</integer>\n'
        '    </dict>\n  </array>\n'
        '  <key>PayloadDisplayName</key>\n  <string>%s</string>\n'
        '  <key>PayloadDescription</key>\n'
        '  <string>由 %s 下发。装完后还需在「设置 → 通用 → 关于本机 → '
        '证书信任设置」里为本证书打开信任开关（iOS 10+ 的强制要求）。</string>\n'
        '  <key>PayloadIdentifier</key>\n  <string>com.ccpx.charles.profile</string>\n'
        '  <key>PayloadOrganization</key>\n  <string>%s</string>\n'
        '  <key>PayloadRemovalDisallowed</key>\n  <false/>\n'
        '  <key>PayloadType</key>\n  <string>Configuration</string>\n'
        '  <key>PayloadUUID</key>\n  <string>%s</string>\n'
        '  <key>PayloadVersion</key>\n  <integer>1</integer>\n'
        '</dict>\n</plist>\n'
    ) % (html.escape(name, quote=True), b64, host_e, disp, ca_uuid,
         disp, host_e, org, prof_uuid)
    return xml.encode('utf-8'), name.replace('.crt', '.mobileconfig')


def qr_svg(data: str, scale: int = 5) -> bytes | None:
    """把文本编成二维码 SVG。库不在就返回 None（**不让缺依赖拖垮整页**）。

    ★ `qrcode` 是懒加载的：如果部署时忘了装，引导页仍然要能打开、能下载证书 ——
      二维码只是「少了个方便」，不是核心功能。为它让整页 500 是本末倒置。
    """
    try:
        import qrcode
        import qrcode.image.svg as svg
    except ImportError:
        return None
    img = qrcode.make(data, image_factory=svg.SvgPathImage,
                      box_size=scale, border=2)
    import io
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 根证书安装引导页（/ca）
# --------------------------------------------------------------------------- #
_CA_TPL = Template('''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>根证书安装</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;padding:26px 16px 60px;background:#f4f6f9;color:#16191d;line-height:1.62}
.box{max-width:680px;margin:0 auto}
.card{background:#fff;border:1px solid #e1e5ea;border-radius:14px;padding:24px 26px;
  margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.badge{display:inline-block;padding:5px 14px;border-radius:999px;font-size:13px;font-weight:700}
.ok{background:#e6f7ec;color:#0a7d33;border:1px solid #b6e5c6}
.bad{background:#fff4e5;color:#9a5b00;border:1px solid #f2d9ac}
h1{margin:14px 0 4px;font-size:22px}
h2{margin:0 0 12px;font-size:15.5px;color:#2b3340}
.sub{margin:0;font-size:13.5px;color:#5c6674}
.fp{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;
  background:#f6f8fa;border:1px solid #e4e8ed;border-radius:9px;padding:12px 14px;
  word-break:break-all;color:#243040;margin:10px 0 0}
.dl{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}
a.btn{display:inline-block;padding:11px 18px;border-radius:9px;text-decoration:none;
  font-size:14px;font-weight:600;border:1px solid transparent}
a.p1{background:#1f6feb;color:#fff}
a.p2{background:#fff;color:#1f3c6b;border-color:#c8d6ea}
a.p3{background:#fff;color:#3a4450;border-color:#d5dae1}
.qr{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}
.qr svg{width:150px;height:150px;background:#fff;border:1px solid #e4e8ed;border-radius:9px;padding:6px}
ol,ul{margin:8px 0 0;padding-left:22px}
li{margin:6px 0;font-size:13.5px}
code{background:#eceff2;padding:2px 6px;border-radius:4px;font-size:12.5px}
.step{font-size:13.5px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.tab{padding:7px 14px;border-radius:8px;border:1px solid #d7dde4;background:#fff;
  font-size:13.5px;cursor:pointer;color:#3a4450}
.tab.on{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:600}
.pane{display:none}.pane.on{display:block}
.warn{background:#fff8e6;border:1px solid #f0dfae;border-radius:9px;padding:12px 14px;
  font-size:13px;color:#6b5406;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
td{padding:7px 0;border-bottom:1px solid #eef1f4;vertical-align:top}
td.k{width:112px;color:#6b7480}
td.v{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
</style></head>
<body><div class="box">
$cards
<div class="card">
  <h2>选你的设备</h2>
  <div class="tabs">
    <button class="tab on" data-p="ios">iPhone / iPad</button>
    <button class="tab" data-p="android">Android</button>
    <button class="tab" data-p="win">Windows</button>
    <button class="tab" data-p="mac">macOS</button>
  </div>
  <div class="pane on" id="p-ios">
    <p class="sub"><b>推荐用描述文件，这是 iOS 上唯一真正的「一键」。</b></p>
    <ol>
      <li>用 <b>Safari</b> 打开本页（微信/QQ 内置浏览器装不了，必须 Safari）。</li>
      <li>点上面 <b>「iOS / macOS 一键安装」</b> → 系统弹「此网站正尝试下载一个配置描述文件」→ 允许。</li>
      <li>设置 → 通用 → <b>VPN 与设备管理</b> → 下载的描述文件 → <b>安装</b> → 输锁屏密码。</li>
      <li>设置 → 通用 → 关于本机 → 拉到底 → <b>证书信任设置</b> → 打开本证书的开关。</li>
    </ol>
    <div class="warn"><b>第 4 步不能省。</b>iOS 10 之后即使装好了证书，也必须在这个开关里手动信任，
      否则 https 仍然报证书错误 —— 绝大多数「装完了还是不行」都卡在这一步。</div>
  </div>
  <div class="pane" id="p-android">
    <ol>
      <li>点 <b>「Android / Windows」</b> 下载 <code>.crt</code>。</li>
      <li>设置 → 安全 → 加密与凭据 → <b>从存储设备安装</b> → <b>CA 证书</b>。</li>
      <li>选择刚下载的 <code>charles-proxy-ca.crt</code> → 确认安装。</li>
      <li>部分机型会弹「您的网络可能受到监控」—— 这是安装根证书的正常提示，确认即可。</li>
    </ol>
    <div class="warn">Android 7+ 上<b>普通 App 默认不信任用户安装的证书</b>（只信系统证书）。
      如果目标 App 做了证书固定或只信系统库，需要 root 后把证书放进
      <code>/system/etc/security/cacerts/</code>。这是 Android 的安全设计，不是本页能绕过的。</div>
  </div>
  <div class="pane" id="p-win">
    <ol>
      <li>点 <b>「Android / Windows」</b> 下载 <code>.crt</code>。</li>
      <li>双击该文件 → <b>安装证书</b> → 存储位置选「本地计算机」。</li>
      <li>下一步 → 「将所有的证书都放入下列存储」→ 浏览 → <b>受信任的根证书颁发机构</b>。</li>
      <li>完成 → 弹安全警告时选「是」。</li>
    </ol>
    <div class="warn">Firefox 用自己的证书库，不读 Windows 的：
      设置 → 隐私与安全 → 证书 → 查看证书 → 证书颁发机构 → 导入。</div>
  </div>
  <div class="pane" id="p-mac">
    <ol>
      <li>点 <b>「iOS / macOS 一键安装」</b> 下载 <code>.mobileconfig</code>。</li>
      <li>系统设置 → 通用 → 设备管理 → 双击描述文件 → 安装。</li>
      <li>或手动：下载 <code>.pem</code> → 双击导入「钥匙串访问」→
          在 <b>系统</b> 钥匙串里找到它 → 右键「显示简介」→ 信任 → 「始终信任」。</li>
    </ol>
  </div>
</div>
<div class="card">
  <h2>为什么要装它</h2>
  <p class="sub">Charles 要解密 HTTPS，就得给每个站点<b>现签一张证书</b>，而这些证书由本服务器的
  Charles 根证书签发。设备不信任这张根证书，浏览器就会对每一个 https 站点报「证书无效」——
  不是站点有问题，是签发者不认识。</p>
</div>
</div>
<script>
(function(){
  var tabs=document.querySelectorAll('.tab'),panes=document.querySelectorAll('.pane');
  function pick(name){
    tabs.forEach(function(t){t.classList.toggle('on',t.dataset.p===name)});
    panes.forEach(function(p){p.classList.toggle('on',p.id==='p-'+name)});
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){pick(t.dataset.p)})});
  var ua=navigator.userAgent;
  if(/iPhone|iPad|iPod/i.test(ua))pick('ios');
  else if(/Android/i.test(ua))pick('android');
  else if(/Macintosh/i.test(ua))pick('mac');
  else pick('win');
})();
</script>
</body></html>
''')


def ca_page(host: str, port: int) -> str:
    """根证书安装引导页。

    ★ 页面上**必须**显示 SHA-256 指纹。让用户装一张来路不明的根证书却无法核对，
      等于把「信任」变成盲信 —— 指纹是唯一能让用户自己确认「装的就是这台服务器的
      那张证书」的东西。服务端算好给用户抄，比让用户在手机上翻证书详情可靠。
    """
    meta = ca_meta()
    base = 'http://%s:%d' % (host or '127.0.0.1', port)

    if not meta['ready']:
        cards = ('<div class="card"><div class="badge bad">证书未就绪</div>'
                 '<h1>服务器上还没有根证书</h1>'
                 '<p class="sub">Charles 首次启动时会自动生成根证书，'
                 '路径 <code>%s</code>。当前读不到它，所以这里没有可下载的文件。</p>'
                 '<div class="warn">%s</div></div>'
                 % (_esc(meta['der']), _esc(meta.get('error', ''))))
        return _CA_TPL.substitute(cards=cards)

    rows = [('SHA-256 指纹', meta['sha256'])]
    if meta.get('subject'):
        rows.append(('主题', meta['subject']))
    if meta.get('issuer'):
        rows.append(('颁发者', meta['issuer']))
    if meta.get('notBefore'):
        rows.append(('生效时间', meta['notBefore']))
    if meta.get('notAfter'):
        rows.append(('失效时间', meta['notAfter']))
    rows.append(('文件大小', '%d 字节' % meta['size']))
    table = ''.join('<tr><td class="k">%s</td><td class="v">%s</td></tr>'
                    % (_esc(k), _esc(v)) for k, v in rows)

    qr_html = ''
    svg = qr_svg(base + '/ca')
    if svg:
        qr_html = ('<div class="qr"><div>%s</div><div style="flex:1;min-width:220px">'
                   '<p class="sub">手机扫码直接打开本页，不用手打地址。'
                   '装完证书后可用 <a href="%s/ok">%s/ok</a> 验证代理链路。</p></div></div>'
                   % (svg.decode('utf-8'), base, base))

    cards = ('''<div class="card">
  <div class="badge ok">证书已就绪</div>
  <h1>安装 Charles 根证书</h1>
  <p class="sub">装好之后，https 站点才不会再报证书错误。按下面的步骤选你的设备。</p>
  <div class="dl">
    <a class="btn p1" href="%s/ca.mobileconfig">iOS / macOS 一键安装</a>
    <a class="btn p2" href="%s/ca.crt">Android / Windows（.crt）</a>
    <a class="btn p3" href="%s/ca.pem">.pem（OpenSSL / 手动导入）</a>
  </div>
  <table>%s</table>
  <div class="warn"><b>核对指纹：</b>装完后在你的设备上查看该证书，
    指纹应与上面 <b>SHA-256 指纹</b>完全一致。不一致说明装错了证书，
    请删掉重装 —— 根证书等于把这条链路上所有加密内容交给持有私钥的人。</div>
</div>
<div class="card"><h2>手机扫码</h2>%s</div>''' % (base, base, base, table, qr_html))

    return _CA_TPL.substitute(cards=cards)


# --------------------------------------------------------------------------- #
# 用户门户（/portal）—— 一花 index.php 的等价物，给最终用户用，**无登录**
# --------------------------------------------------------------------------- #
# ★ 这里**故意**不用 `string.Template`：门户的 JS 里有 `$` 函数简写
#   （`var $=function(id){...}`），而 `Template.substitute()` 会把每个
#   `$` 当占位符解析 —— 一个 `$('r-go')` 就足以让整页 500。
#   这个页面不需要任何服务端插值（数据全走 fetch），所以就是普通字符串。
_PORTAL_TPL = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>账号自助服务</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;padding:26px 16px 60px;background:#eef1f6;color:#16191d;line-height:1.6}
.box{max-width:520px;margin:0 auto}
.card{background:#fff;border:1px solid #e1e5ea;border-radius:14px;padding:22px 24px;
  box-shadow:0 1px 4px rgba(20,30,50,.06)}
h1{margin:0 0 4px;font-size:20px}
.sub{margin:0 0 18px;font-size:13px;color:#5c6674}
.tabs{display:flex;gap:6px;margin-bottom:18px;background:#f1f4f8;padding:4px;border-radius:10px}
.tab{flex:1;text-align:center;padding:9px 0;border-radius:8px;font-size:14px;
  cursor:pointer;color:#4a5563;border:0;background:transparent}
.tab.on{background:#fff;color:#1f6feb;font-weight:600;box-shadow:0 1px 3px rgba(20,30,50,.1)}
.pane{display:none}.pane.on{display:block}
label{display:block;font-size:13px;color:#4a5563;margin:14px 0 6px}
input,select{width:100%;padding:11px 12px;border:1px solid #d5dbe3;border-radius:9px;
  font-size:15px;background:#fff;color:#16191d;outline:none}
input:focus,select:focus{border-color:#1f6feb;box-shadow:0 0 0 3px rgba(31,111,235,.12)}
button.go{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;
  background:#1f6feb;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button.go:disabled{background:#9db8e0;cursor:default}
.res{margin-top:16px;padding:13px 15px;border-radius:9px;font-size:14px;display:none}
.res.ok{display:block;background:#e6f7ec;border:1px solid #b6e5c6;color:#0a7d33}
.res.err{display:block;background:#fdecec;border:1px solid #f3c2c2;color:#a12626}
.res.info{display:block;background:#eef4fd;border:1px solid #cfe0f7;color:#1b4a86}
.hint{font-size:12.5px;color:#6b7480;margin-top:10px}
.foot{margin-top:16px;text-align:center;font-size:12.5px;color:#7b8494}
a{color:#1f6feb}
</style></head>
<body><div class="box">
<div class="card">
  <h1>账号自助服务</h1>
  <p class="sub">注册新账号、用卡密充值、查询到期时间。卡密由管理员发放。</p>
  <div class="tabs">
    <button class="tab on" data-p="reg">注册</button>
    <button class="tab" data-p="pay">充值</button>
    <button class="tab" data-p="chk">查询</button>
  </div>

  <div class="pane on" id="p-reg">
    <label>账号</label>
    <input id="r-user" autocomplete="off" placeholder="5 位以上，字母或数字">
    <label>密码</label>
    <input id="r-pwd" type="password" autocomplete="off" placeholder="同时含字母和数字，5-16 位">
    <label>卡密</label>
    <input id="r-code" autocomplete="off" placeholder="粘贴卡密">
    <button class="go" id="r-go">注册</button>
    <div class="res" id="r-res"></div>
    <div class="hint">卡密已经绑定了应用和时长，所以注册时不用选应用。</div>
  </div>

  <div class="pane" id="p-pay">
    <label>账号</label>
    <input id="p-user" autocomplete="off" placeholder="要充值的已有账号">
    <label>卡密</label>
    <input id="p-code" autocomplete="off" placeholder="粘贴卡密">
    <button class="go" id="p-go">充值</button>
    <div class="res" id="p-res"></div>
    <div class="hint">账号必须已经存在。未到期的账号在原到期时间上叠加，已过期的从现在起算。</div>
  </div>

  <div class="pane" id="p-chk">
    <label>账号</label>
    <input id="c-user" autocomplete="off" placeholder="要查询的账号">
    <label>应用</label>
    <select id="c-app"></select>
    <button class="go" id="c-go">查询</button>
    <div class="res" id="c-res"></div>
  </div>

  <div class="foot">代理配置与证书安装见 <a href="/ca">证书安装引导</a> · <a href="/ok">连通性检测</a></div>
</div>
</div>
<script>
var $=function(id){return document.getElementById(id)};

function show(el, cls, html){
  el.className='res '+cls;
  if(html===undefined||html===null){el.textContent='';return}
  el.innerHTML=String(html);           // query 回的是 HTML 片段，必须 innerHTML
}

function loadApps(){
  fetch('/api/v1/kami/apps/public').then(function(r){return r.json()}).then(function(j){
    var list=(j&&j.msg)||[], sel=document.querySelectorAll('select');
    if(!list.length){
      for(var i=0;i<sel.length;i++){sel[i].innerHTML='<option value="">（暂无可用应用）</option>'}
      return;
    }
    var html=list.map(function(a){
      return '<option value="'+String(a.appcode).replace(/"/g,'&quot;')+'">'+
             String(a.appname).replace(/[<>&]/g,function(c){
               return {'<':'&lt;','>':'&gt;','&':'&amp;'}[c]})+'\u2003('+
             String(a.appcode).replace(/[<>&]/g,function(c){
               return {'<':'&lt;','>':'&gt;','&':'&amp;'}[c]})+'</option>';
    }).join('');
    for(var i=0;i<sel.length;i++){sel[i].innerHTML=html}
  }).catch(function(){
    var sel=document.querySelectorAll('select');
    for(var i=0;i<sel.length;i++){sel[i].innerHTML='<option value="">（读取失败）</option>'}
  });
}

function post(url, data, btn, res, done){
  var fd=new URLSearchParams();
  for(var k in data){fd.append(k, data[k])}
  btn.disabled=true;
  show(res,'info','处理中…');
  fetch(url,{method:'POST',body:fd,
    headers:{'Content-Type':'application/x-www-form-urlencoded'}})
    .then(function(r){return r.json()}).then(function(j){
      var code=j&&j.code, msg=(j&&j.msg);
      if(code===1){
        // query 的 msg 是 HTML 片段；insert/update 是纯文本。
        var isHtml = typeof msg==='string' && msg.indexOf('<h5')===0;
        show(res,'ok', isHtml ? msg : (msg||'操作成功'));
      }else{
        show(res,'err', msg||('失败（code='+code+'）'));
      }
      if(done)done();
    }).catch(function(e){
      show(res,'err','请求失败：'+(e&&e.message?e.message:e));
    }).then(function(){btn.disabled=false});
}

var tabs=document.querySelectorAll('.tab'),panes=document.querySelectorAll('.pane');
for(var i=0;i<tabs.length;i++){
  tabs[i].addEventListener('click',function(){
    var n=this.dataset.p;
    for(var j=0;j<tabs.length;j++){tabs[j].classList.toggle('on',tabs[j].dataset.p===n)}
    for(var j=0;j<panes.length;j++){panes[j].classList.toggle('on',panes[j].id==='p-'+n)}
  });
}

$('r-go').addEventListener('click',function(){
  var u=$('r-user').value.trim(), p=$('r-pwd').value, c=$('r-code').value.trim();
  if(u.length<5){show($('r-res'),'err','账号至少 5 位');return}
  if(!p||p.length<5){show($('r-res'),'err','密码至少 5 位');return}
  if(!c){show($('r-res'),'err','请填卡密');return}
  // ★ 注册不传 appcode：一花的 insert 只收 user/pwd/code，应用归属写在卡密上。
  //   之前这里多传了一个 appcode，后端会忽略它 —— 但前端会先去读一个
  //   已经不存在的下拉框元素，直接 TypeError，按钮点不动。
  post('/api/v1/kami/client/insert',{user:u,pwd:p,code:c},
       this,$('r-res'),function(){$('r-user').value='';$('r-pwd').value='';$('r-code').value=''});
});
$('p-go').addEventListener('click',function(){
  var u=$('p-user').value.trim(), c=$('p-code').value.trim();
  if(u.length<5){show($('p-res'),'err','账号至少 5 位');return}
  if(!c){show($('p-res'),'err','请填卡密');return}
  post('/api/v1/kami/client/update',{user:u,code:c},this,$('p-res'),
       function(){$('p-code').value=''});
});
$('c-go').addEventListener('click',function(){
  var u=$('c-user').value.trim(), app=$('c-app').value;
  if(u.length<5){show($('c-res'),'err','账号至少 5 位');return}
  if(!app){show($('c-res'),'err','没有可用应用');return}
  post('/api/v1/kami/client/query',{user:u,appcode:app},this,$('c-res'));
});

loadApps();
</script>
</body></html>
'''


def portal_page() -> str:
    """用户门户页 —— 静态骨架，数据全靠 `/api/v1/kami/apps/public` 与
    `/api/v1/kami/client/*` 拉。

    ★ 为什么不塞进 Vue 管理台：这是给**最终用户**看的页面，他们不该看到
      管理台的外壳、导航和登录入口。一花原版也是独立页面（index.php），
      这里保持同样的边界 —— 门户在 `/portal`，管理台在 `/`。
    """
    return _PORTAL_TPL


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
_HTML = 'text/html; charset=utf-8'


@router.get('/ok', include_in_schema=False)
async def ok_page(request: Request):
    """代理连通性检测页。客户端设备直接访问，不要求登录。"""
    body = await run_in_threadpool(
        conn_page, _client_ip(request), request.headers.get('host'),
        _server_port(request))
    return Response(content=body, media_type=_HTML)


@router.get('/socks', include_in_schema=False)
async def socks(request: Request):
    """SOCKS 验证页 —— m.baidu.com 的「远程映射」落点。"""
    # query_params 是单值视图，而模板按「取第一个」的方式用（原实现基于
    # parse_qs 的 {k: [v]} 结构），所以这里显式转成 {k: [v]}。
    ql = {k: [v] for k, v in request.query_params.multi_items()}
    body = await run_in_threadpool(
        socks_page, _client_ip(request), request.headers.get('host'),
        _server_port(request), ql)
    return Response(content=body, media_type=_HTML)


@router.get('/ruleset.conf', include_in_schema=False)
@router.get('/ruleset', include_in_schema=False)
async def ruleset():
    """Kitsunebi 规则集。两个路径等价：.conf 给客户端填，/ruleset 方便浏览器看。

    ★ 必须带 no-store：客户端「从 URL 更新规则」时如果命中缓存，
      改完规则却拉不到新版本，会变成「明明改了没生效」的悬案。
    """
    text = await run_in_threadpool(ruleset_text)
    return Response(content=text, media_type='text/plain; charset=utf-8',
                    headers={'Cache-Control': 'no-store'})


@router.get('/ca.crt', include_in_schema=False)
@router.get('/ca.cer', include_in_schema=False)
@router.get('/charles-ca.crt', include_in_schema=False)
@router.get('/charles-ca.cer', include_in_schema=False)
async def ca_der():
    """根证书 DER。手机浏览器打开即下载，导入系统信任库。"""
    blob, name = await run_in_threadpool(ca_blob, 'der')
    if blob is None:
        return Response(content='证书文件不存在: %s\n' % settings.charles_ca_cer,
                        media_type='text/plain; charset=utf-8', status_code=404)
    return Response(content=blob, media_type='application/x-x509-ca-cert',
                    headers={'Content-Disposition':
                             'attachment; filename="%s"' % name})


@router.get('/ca.pem', include_in_schema=False)
@router.get('/charles-ca.pem', include_in_schema=False)
async def ca_pem():
    """根证书 PEM。给 iOS / OpenSSL。"""
    blob, name = await run_in_threadpool(ca_blob, 'pem')
    if blob is None:
        return Response(content='证书文件不存在: %s\n' % settings.charles_ca_pem,
                        media_type='text/plain; charset=utf-8', status_code=404)
    return Response(content=blob, media_type='application/x-pem-file',
                    headers={'Content-Disposition':
                             'attachment; filename="%s"' % name})


@router.get('/ca.mobileconfig', include_in_schema=False)
@router.get('/charles-ca.mobileconfig', include_in_schema=False)
async def ca_mobileconfig(request: Request):
    """iOS / macOS 配置描述文件 —— 点一下装根证书。

    ★ Content-Type 必须是 `application/x-apple-aspen-config`：
      iOS 只认这个类型才会弹「安装描述文件」。回 `application/octet-stream`
      或者 `application/xml` 的话，Safari 会把它当普通文件下载到「文件」App，
      用户拿它没办法 —— 表现就是「点了没反应」。
    """
    host = (request.headers.get('host') or '').split(':')[0]
    blob, name = await run_in_threadpool(mobileconfig_bytes, host)
    if blob is None:
        return Response(content='证书文件不存在: %s\n' % settings.charles_ca_cer,
                        media_type='text/plain; charset=utf-8', status_code=404)
    return Response(content=blob,
                    media_type='application/x-apple-aspen-config',
                    headers={'Content-Disposition':
                             'attachment; filename="%s"' % name})


@router.get('/ca.qr.svg', include_in_schema=False)
async def ca_qr(request: Request, what: str = 'ca'):
    """二维码 SVG。`what=ca` 指向安装引导页，`what=ok` 指向连通性检测页。

    库缺失时回 404 + 纯文本说明 —— 引导页已经做了兜底，不会因为这张图挂掉。
    """
    target = ('/ok' if what == 'ok' else '/ca')
    host = (request.headers.get('host') or '').split(':')[0]
    port = _server_port(request)
    svg = await run_in_threadpool(qr_svg, 'http://%s:%d%s' % (host, port, target))
    if svg is None:
        return Response(content='未安装 qrcode 库：pip install qrcode\n',
                        media_type='text/plain; charset=utf-8', status_code=404)
    return Response(content=svg, media_type='image/svg+xml',
                    headers={'Cache-Control': 'no-store'})


@router.get('/ca', include_in_schema=False)
async def ca_guide(request: Request):
    """根证书安装引导页 —— 分平台步骤 + 指纹核对 + 扫码。

    ★ 只有「下载」没有「引导」等于把用户丢在半路：iOS 上装完还要去
      「证书信任设置」开开关，这一步没人告诉他就一定漏。
    """
    host = (request.headers.get('host') or '').split(':')[0]
    body = await run_in_threadpool(ca_page, host, _server_port(request))
    return Response(content=body, media_type=_HTML)


@router.get('/portal', include_in_schema=False)
async def portal():
    """用户自助门户 —— 一花 `index.php` 的等价物。

    ★ 不要求登录，也不能要求：这是给**最终用户**用的（注册 / 充值 / 查询），
      他们手里只有卡密，没有管理台账号。加鉴权 = 这个页面没人用得上。
    ★ 页面本身是静态骨架，数据全靠 `/api/v1/kami/apps/public`（应用列表）
      与 `/api/v1/kami/client/*`（三个动作）拉。所以门户能跑通的前提是那
      两个接口也保持无鉴权 —— 改的时候要一起改，不能只放开一个。
    """
    return Response(content=portal_page(), media_type=_HTML)


@router.get('/_device', include_in_schema=False)
async def device_index():
    """设备侧入口清单 —— 排障时一眼看全，不用翻代码。

    顺便回一下证书与规则集的**现场**：文件在不在、多大、指纹是什么。
    这比「访问一下试试」快，而且能区分「路由没实现」和「文件不存在」——
    这两个问题在客户端看来都是「打不开」，但修法完全不同。
    """
    cer_exists = os.path.isfile(str(settings.charles_ca_cer))
    pem_exists = os.path.isfile(str(settings.charles_ca_pem))
    rs_exists = os.path.isfile(str(settings.ruleset_path))
    meta = ca_meta()
    return {
        'ok': True,
        'auth': 'none（客户端设备用，不要求登录）',
        'note': '来源 IP 只读 socket 对端，不读 X-Forwarded-For；前面加反代会失效',
        'endpoints': {
            '/ok': '代理连通性检测页（客户端主动访问）',
            '/socks': 'SOCKS 验证页（m.baidu.com 远程映射落点）',
            '/ruleset.conf': 'Kitsunebi 规则集',
            '/ruleset': '同上，浏览器可直接看',
            '/ca': '★ 根证书安装引导页（分平台步骤 + 指纹核对 + 扫码）',
            '/ca.mobileconfig': '★ iOS / macOS 一键安装描述文件',
            '/ca.crt': '根证书 DER（同 /ca.cer /charles-ca.crt /charles-ca.cer）',
            '/ca.pem': '根证书 PEM（同 /charles-ca.pem）',
            '/ca.qr.svg': '二维码 SVG（?what=ca 引导页 / ?what=ok 检测页）',
            '/portal': '★ 用户自助门户（注册 / 充值 / 查询，无登录）',
        },
        'files': {
            'ca_cer': {'path': str(settings.charles_ca_cer), 'exists': cer_exists},
            'ca_pem': {'path': str(settings.charles_ca_pem), 'exists': pem_exists},
            'ruleset': {'path': str(settings.ruleset_path), 'exists': rs_exists,
                        'size': (os.path.getsize(str(settings.ruleset_path))
                                 if rs_exists else 0)},
        },
        'ca': meta,
        'serverTime': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
