"""后端冒烟测试 —— 起真服务、打真请求。

用法（先另开一个进程跑 uvicorn，或让本脚本自己起）：

    set REWRITE_JWT_SECRET=smoke
    python _smoke.py            # 默认打 http://127.0.0.1:8890

★ 默认端口跟着**当前部署**走：平台自己就住在 8890（迁移期那个 8903 暂存端口
  和旧管理台一起退休了）。要打别的地址用 SMOKE_BASE 覆盖。

只做「有没有跑起来、契约对不对」的验证，不验证上游业务语义
（上游是远端 203.0.113.10:8893，本机多半不可达 —— 那部分会如实标 SKIP/502）。
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get('SMOKE_BASE', 'http://127.0.0.1:8890')
USER = os.environ.get('SMOKE_USER', 'admin')
PWD = os.environ.get('SMOKE_PWD', 'admin')

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('%-4s %-42s %s' % ('PASS' if cond else 'FAIL', name, detail))


def skip(name, detail=''):
    SKIP.append(name)
    print('%-4s %-42s %s' % ('SKIP', name, detail))


def main() -> int:
    # trust_env=False：本机 shell 常带 http_proxy（抓包工具 / 公司网络），
    # 不关的话连 127.0.0.1:8890 都会走代理，测试结果随宿主机环境漂移。
    cli = httpx.Client(base_url=BASE, timeout=20.0, trust_env=False)

    # ---- 元信息 --------------------------------------------------------- #
    r = cli.get('/healthz')
    check('GET /healthz', r.status_code == 200 and r.json().get('ok') is True,
          'HTTP %d' % r.status_code)

    r = cli.get('/openapi.json')
    paths = sorted(r.json().get('paths', {})) if r.status_code == 200 else []
    check('GET /openapi.json', bool(paths), '%d 条路径' % len(paths))
    for want in ('/mcp', '/api/v1/auth/login', '/api/v1/overview',
                 '/api/v1/wpe/packets', '/api/v1/kami/list'):
        check('路由存在 %s' % want, want in paths)

    # ---- 鉴权 ----------------------------------------------------------- #
    r = cli.get('/api/v1/overview')
    check('未登录访问受保护端点 -> 401', r.status_code == 401,
          'HTTP %d' % r.status_code)

    r = cli.post('/api/v1/auth/login', json={'username': USER, 'password': 'definitely-wrong'})
    check('错误口令 -> 401', r.status_code == 401, 'HTTP %d' % r.status_code)

    # ---- 空凭据必须是 401，不能是 422 的错误数组 ------------------------ #
    # ★ 这条钉的是线上真实翻车：浏览器口令管理器自动填充时**可能不派发
    #   `input` 事件**，前端 v-model 的 ref 还是空的 → 提交
    #   `{"username":"","password":""}` → 老 schema 的 `min_length=1` 回 422，
    #   而 422 的 detail 是 Pydantic 的**数组**，前端 `new Error(数组)` 被
    #   String() 变成 `[object Object]` —— 用户只看到一坨乱码，
    #   除了「无法登录」什么也反馈不出来。
    #   现在 login() 统一回一句话 401，客户端才有东西可显示。
    r = cli.post('/api/v1/auth/login', json={'username': '', 'password': ''})
    check('空账号空口令 -> 401（不是 422）', r.status_code == 401,
          'HTTP %d %s' % (r.status_code, r.text[:100]))
    check('空凭据的 detail 是字符串（不是数组）',
          isinstance((r.json() or {}).get('detail'), str),
          repr((r.json() or {}).get('detail'))[:80])

    r = cli.post('/api/v1/auth/login', json={'username': '', 'password': 'x'})
    check('空账号 -> 401（不是 422）', r.status_code == 401, 'HTTP %d' % r.status_code)

    r = cli.post('/api/v1/auth/login', json={'username': USER, 'password': PWD})
    ok = r.status_code == 200 and r.json().get('ok') is True
    check('正确口令 -> 200', ok, 'HTTP %d %s' % (r.status_code, r.text[:120]))
    token = ''
    if ok:
        token = r.json().get('data', {}).get('token') or ''
        check('login 返回 token', bool(token), token[:16] + '...')
    auth = {'Authorization': 'Bearer ' + token} if token else {}

    r = cli.get('/api/v1/auth/me', headers=auth)
    check('GET /auth/me 带 token', r.status_code == 200, 'HTTP %d' % r.status_code)
    j = r.json() if r.status_code == 200 else {}
    u = (j.get('data') or {}).get('user') or {}
    # 必须走 Envelope：客户端 data() 会剥一层 data 再取 .user。
    # 平铺 user 在顶层时前端只是「碰巧」能用（data() 无 data 字段会回退整个对象），
    # 靠兜底才没炸的契约随时会崩，所以这里钉死结构。
    check('GET /auth/me 走 Envelope 且带 user', bool(u.get('username')),
          'user=%s' % list(u)[:6])
    for f in ('avatar', 'viaCookie', 'noLogin'):
        check('GET /auth/me 含 %s' % f, f in u)

    r = cli.get('/api/v1/auth/avatar', headers=auth)
    check('GET /auth/avatar 非 QQ 邮箱 -> 404（不是 500）',
          r.status_code in (200, 404), 'HTTP %d' % r.status_code)

    r = cli.get('/api/v1/ccpx/creds', headers=auth)
    j = r.json() if r.status_code == 200 else {}
    d = j.get('data') or {}
    check('GET /ccpx/creds 回凭据现场', r.status_code == 200
          and 'admin' in d and 'source' in d and 'password' not in d,
          'admin=%r source=%s err=%s' % (d.get('admin'), d.get('source'),
                                         (d.get('error') or '')[:60]))

    # 帮助页：本机没有 charles.jar，所以「名单为空 / 取页 404」都是**正确**行为；
    # 唯一不能接受的是 500 —— 那说明 jar 读失败没被兜住。
    r = cli.get('/api/v1/charles/help', headers=auth)
    names = (r.json().get('data') if r.status_code == 200 else None) or []
    check('GET /charles/help 回名单（不是 500）', r.status_code == 200
          and isinstance(names, list),
          'HTTP %d %d 页' % (r.status_code, len(names)))
    r = cli.get('/api/v1/charles/help', params={'name': 'no-such-page'}, headers=auth)
    check('GET /charles/help?name=不存在 -> 404（不是 500）', r.status_code == 404,
          'HTTP %d' % r.status_code)
    r = cli.get('/api/v1/charles/help', params={'name': '../../etc/passwd'}, headers=auth)
    check('GET /charles/help 名字穿越 -> 404（白名单挡住）', r.status_code == 404,
          'HTTP %d' % r.status_code)

    # ---- MCP ------------------------------------------------------------ #
    def rpc(payload, headers=None):
        return cli.post('/mcp', json=payload, headers=headers or {})

    r = rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                        'clientInfo': {'name': 'smoke', 'version': '0'}}})
    j = r.json() if r.status_code == 200 else {}
    res = j.get('result') or {}
    check('MCP initialize', r.status_code == 200
          and res.get('protocolVersion') == '2025-06-18'
          and res.get('serverInfo', {}).get('name') == 'ccpx-wpe',
          'HTTP %d %s' % (r.status_code, res.get('protocolVersion')))
    check('MCP 回 Mcp-Session-Id 头', bool(r.headers.get('mcp-session-id')))

    # 通知按规范**不带 id**；带了 id 就是普通请求，会走 Method not found。
    r = rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    check('MCP 通知 -> 202 空体', r.status_code == 202 and r.content == b'',
          'HTTP %d %d 字节' % (r.status_code, len(r.content)))

    r = rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
    tools = ((r.json().get('result') or {}).get('tools') or []) if r.status_code == 200 else []
    names = sorted(t['name'] for t in tools)
    check('MCP tools/list -> 17 个工具', len(tools) == 17, '实际 %d' % len(tools))
    want_tools = ['wpe_clear', 'wpe_export', 'wpe_export_download', 'wpe_export_list',
                  'wpe_export_save', 'wpe_get', 'wpe_hold_get', 'wpe_hold_set',
                  'wpe_import', 'wpe_list', 'wpe_release', 'wpe_replay',
                  'wpe_rewrite_get', 'wpe_rewrite_set', 'wpe_send', 'wpe_stat',
                  'wpe_struct']
    check('工具名与旧栈一致', names == want_tools,
          '缺：%s' % [n for n in want_tools if n not in names])

    r = rpc({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'no_such_tool', 'arguments': {}}})
    j = r.json() if r.status_code == 200 else {}
    check('未知工具 -> isError 而非协议错', r.status_code == 200
          and (j.get('result') or {}).get('isError') is True)

    r = rpc({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
             'params': {'name': 'wpe_get', 'arguments': {}}})
    j = r.json() if r.status_code == 200 else {}
    body = ((j.get('result') or {}).get('content') or [{}])[0].get('text', '')
    check('wpe_get 缺 id -> 报错不静默', 'isError' in json.dumps(j)
          and '缺必填参数' in body, body[:80])

    r = rpc({'jsonrpc': '2.0', 'id': 6, 'method': 'resources/list'})
    check('resources/list -> 空列表', r.status_code == 200
          and (r.json().get('result') or {}).get('resources') == [])

    r = rpc({'jsonrpc': '2.0', 'id': 7, 'method': 'no/such'})
    j = r.json() if r.status_code == 200 else {}
    check('未知方法 -> -32601', (j.get('error') or {}).get('code') == -32601)

    r = cli.post('/mcp', content=b'{not json')
    check('坏 JSON -> 400 Parse error', r.status_code == 400,
          'HTTP %d' % r.status_code)

    r = cli.post('/mcp', json=[{'jsonrpc': '2.0', 'method': 'notifications/x'}])
    check('批处理全通知 -> 202', r.status_code == 202, 'HTTP %d' % r.status_code)

    r = cli.get('/mcp')
    check('GET /mcp 无 SSE Accept -> 405', r.status_code == 405,
          'HTTP %d Allow=%s' % (r.status_code, r.headers.get('allow')))

    r = cli.request('DELETE', '/mcp')
    check('DELETE /mcp -> 204', r.status_code == 204, 'HTTP %d' % r.status_code)

    # ---- 前端托管 ------------------------------------------------------- #
    r = cli.get('/_frontend')
    j = r.json() if r.status_code == 200 else {}
    check('GET /_frontend 回 JSON（未被 SPA 通配吃掉）',
          r.status_code == 200 and 'dist' in j,
          'HTTP %d ok=%s' % (r.status_code, j.get('ok')))
    if not j.get('ok'):
        skip('前端静态托管', j.get('hint') or '尚未构建')
    else:
        r = cli.get('/')
        check('GET / 返回 SPA index',
              r.status_code == 200 and '<div id="app">' in r.text,
              'HTTP %d' % r.status_code)
        r = cli.get('/wpe')
        check('SPA 回退 /wpe', r.status_code == 200 and '<div id="app">' in r.text,
              'HTTP %d' % r.status_code)
        r = cli.get('/api/v1/does-not-exist')
        check('API 前缀不走 SPA 回退 -> 404', r.status_code == 404,
              'HTTP %d' % r.status_code)

        # ---- API 兜底的「三态」---------------------------------------- #
        # ★ 这一组钉住的是「兜底不许撒谎」：路径**存在**但方法不对时必须回
        #   405 并列出允许的方法，回 404 说「没有这条路由」会让人去翻路由
        #   注册代码找一个明明存在的端点。曾经就是这样（而且因为
        #   `include_router` 不摊平 `app.routes`，路由表只数到 5 条，
        #   `did_you_mean` 恒空）—— 所以这里逐条钉死，别让它退回去。
        r = cli.get('/api/v1/kami')          # 该路径只有 POST
        check('GET 只收 POST 的路由 -> 405（不是 404）', r.status_code == 405,
              'HTTP %d' % r.status_code)
        check('405 里点名了允许的方法',
              'POST' in (r.headers.get('allow') or '')
              and '只接受 POST' in r.text,
              'allow=%s body=%s' % (r.headers.get('allow'), r.text[:80]))

        r = cli.get('/api/v1/kami/')          # 只差一个尾斜杠
        check('尾斜杠 + 路由存在 -> 307 跳正主', r.status_code == 307,
              'HTTP %d' % r.status_code)

        r = cli.get('/api/v1/kami/clinet/query')   # 拼错 client
        check('拼错路径 -> 404 且给出候选', r.status_code == 404
              and '/api/v1/kami/client/query' in r.text,
              'HTTP %d %s' % (r.status_code, r.text[:120]))

        r = cli.post('/mcp/', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        check('POST /mcp/（尾斜杠）等同 /mcp', r.status_code == 200
              and 'tools' in r.text, 'HTTP %d %s' % (r.status_code, r.text[:80]))
        r = cli.get('/mcp/')
        check('GET /mcp/ 不再回「没有这条路由」',
              'No such API route' not in r.text,
              'HTTP %d %s' % (r.status_code, r.text[:80]))

        # 真实 GET 路由不能被兜底吃掉。
        # ★ 判据不能写「401」或「200」：`httpx.Client` 会累积登录 Cookie，
        #   跑到这里已经是带会话的，401 永远等不到；而写死 200 又会在
        #   上游不可达（502）时假红。唯一稳的判据是「回的不是兜底那句话」。
        r = cli.get('/api/v1/wpe/stat')
        check('真实 GET 路由未被兜底吃（不落兜底 404）',
              not (r.status_code == 404 and 'No such API route' in r.text),
              'HTTP %d' % r.status_code)

    # ---- 设备侧无登录入口 ----------------------------------------------- #
    # 这组路径给客户端设备用（手机 / Kitsunebi / 证书导入），一律不要求登录。
    # 判据里最要紧的一条是「没被 SPA 通配吃掉」—— 被吃掉的话回的是 200 + HTML，
    # 状态码完全正常，只有 content-type 和正文能戳穿。
    r = cli.get('/ok')
    check('GET /ok（免登录）-> 200 text/html', r.status_code == 200
          and r.headers.get('content-type', '').startswith('text/html'),
          'HTTP %d ct=%s' % (r.status_code, r.headers.get('content-type')))
    check('GET /ok 未被 SPA 通配吃掉', '<div id="app">' not in r.text
          and '服务端看到你的地址' in r.text)

    r = cli.get('/socks')
    check('GET /socks（免登录）-> 200 text/html', r.status_code == 200
          and r.headers.get('content-type', '').startswith('text/html'),
          'HTTP %d' % r.status_code)
    r = cli.get('/socks', params={'via': 'map'})
    check('GET /socks?via=map 命中强判据「验证成功」',
          r.status_code == 200 and '验证成功' in r.text, 'HTTP %d' % r.status_code)
    r = cli.get('/socks')
    check('GET /socks 无 via=map 不显示「验证成功」', '验证成功' not in r.text)

    r = cli.get('/ruleset.conf')
    check('GET /ruleset.conf -> 200 text/plain', r.status_code == 200
          and r.headers.get('content-type', '').startswith('text/plain'),
          'HTTP %d ct=%s' % (r.status_code, r.headers.get('content-type')))
    check('GET /ruleset.conf 带 no-store', r.headers.get('cache-control') == 'no-store',
          r.headers.get('cache-control'))
    check('GET /ruleset.conf 正文非空', bool(r.text.strip()), '%d 字节' % len(r.text))
    body_conf = r.text
    r = cli.get('/ruleset')
    check('GET /ruleset 与 /ruleset.conf 同体', r.status_code == 200 and r.text == body_conf)

    for path, want_ct in (('/ca.crt', 'application/x-x509-ca-cert'),
                          ('/ca.cer', 'application/x-x509-ca-cert'),
                          ('/ca.pem', 'application/x-pem-file')):
        r = cli.get(path)
        if r.status_code == 200:
            check('GET %s -> 200 %s' % (path, want_ct),
                  r.headers.get('content-type', '').startswith(want_ct)
                  and 'attachment' in (r.headers.get('content-disposition') or '')
                  and len(r.content) > 0,
                  'HTTP %d %d 字节' % (r.status_code, len(r.content)))
        else:
            # 证书文件不在本机是环境问题（线上有），但**必须是 404 + 明文说明**，
            # 不能是 SPA 的 index.html —— 后者会让客户端「下载」到一个网页。
            check('GET %s 无文件时 -> 404 明文（非 SPA）' % path,
                  r.status_code == 404 and '证书文件不存在' in r.text,
                  'HTTP %d' % r.status_code)

    r = cli.get('/_device')
    j = r.json() if r.status_code == 200 else {}
    check('GET /_device 回清单', r.status_code == 200
          and '/ok' in (j.get('endpoints') or {}), 'HTTP %d' % r.status_code)

    # ---- 上游相关：本机多半连不上 8893 ---------------------------------- #
    r = cli.get('/api/v1/ccpx/status', headers=auth)
    if r.status_code == 200:
        check('GET /ccpx/status（上游可达）', r.json().get('ok') is True)
    else:
        # 上游不可达必须是 502（网关语义），不能是 500 —— 500 等于把
        # 「适配层没起来」说成「平台自己坏了」，排查会被带偏。
        check('GET /ccpx/status 上游不可达 -> 502', r.status_code == 502,
              'HTTP %d %s' % (r.status_code, r.text[:120]))
        # 再钉一条：错误措辞必须来自**直连**，不能是代理侧。
        # 宿主机有 http_proxy 时，httpx 默认会把「连本机 8893」交给代理，
        # 拿回 "upstream connect failed" —— 那串里没有一个字指向代理，
        # 排查会一路怀疑适配层/端口/防火墙。trust_env=False 关掉后
        # 才会得到 httpx 自己的 ConnectError 措辞。
        body = r.text
        check('上游错误非代理侧措辞（trust_env=False 生效）',
              'upstream connect failed' not in body, body[:160])

    r = cli.get('/api/v1/overview', headers=auth)
    j = r.json() if r.status_code == 200 else {}
    check('GET /overview 即使下游挂也回 200', r.status_code == 200
          and 'degraded' in j, 'degraded=%s down=%s'
          % (j.get('degraded'), j.get('down')))

    # ---- 卡密（纯本地库，不依赖上游） ----------------------------------- #
    r = cli.get('/api/v1/kami/apps', headers=auth)
    check('GET /kami/apps -> 200', r.status_code == 200, 'HTTP %d' % r.status_code)
    r = cli.get('/api/v1/kami/stats', headers=auth)
    check('GET /kami/stats -> 200', r.status_code == 200, 'HTTP %d' % r.status_code)
    r = cli.get('/api/v1/kami/list', headers=auth)
    j = r.json() if r.status_code == 200 else {}
    # `total/page/size` 在**顶层**，`data` 才是列表。
    # 之前写成 `'total' in (data or body)` —— data 非空时必然假红。
    check('GET /kami/list -> 分页结构', r.status_code == 200
          and isinstance(j.get('data'), list)
          and all(k in j for k in ('total', 'page', 'size')),
          'HTTP %d keys=%s' % (r.status_code, sorted(j)))

    # ---- 卡密客户端核销（一花 api/cpproxy.php 契约） -------------------- #
    # 这一段专门钉「契约没被新后端的审美改掉」：返回体必须是**裸** {code,msg}，
    # code 的四种取值（1/-1/-2/-3 与两个字符串）必须原样，msg 必须能区分不同
    # 拒绝原因（-1 承载了四种拒绝，合并成一个笼统 400 就是契约破坏）。
    tag = 'SMK%d' % int(time.time())
    # 出口：优先复用已有的 127.0.0.1:8888。
    # ★ `kami_servers` 有 UniqueConstraint(host, port)，重复建会回 409 ——
    #   那是**正确**行为。之前这里直接建、拿到 409 就当 None 用，而且**没有断言**，
    #   于是 srv_id=None 一路静默传下去，最后只在 insert 那步表现成「-3 服务器通信
    #   出现问题」，看着像上游挂了，实际是测试自己没建出出口。断言补上，堵死这条路。
    rows = (cli.get('/api/v1/kami/servers', headers=auth).json() or {}).get('data') or []
    srv_id = next((x['id'] for x in rows
                   if x['host'] == '127.0.0.1' and x['port'] == 8888), None)
    if srv_id is None:
        r = cli.post('/api/v1/kami/servers', headers=auth, json={
            'host': '127.0.0.1', 'port': 8888, 'username': 'u', 'password': 'p',
            'remark': tag})
        srv_id = (r.json() or {}).get('id') if r.status_code == 201 else None
    check('出口可用（复用或新建 127.0.0.1:8888）', srv_id is not None,
          'srv_id=%s' % srv_id)

    r = cli.post('/api/v1/kami/apps', headers=auth, json={
        'name': 'smoke-app-%s' % tag, 'code': 'APP' + tag, 'server_id': srv_id})
    app_id = (r.json() or {}).get('id') if r.status_code == 201 else None
    if app_id is None:
        rows = (cli.get('/api/v1/kami/apps', headers=auth).json() or {}).get('data') or []
        app_id = next((x['id'] for x in rows if x.get('code') == 'APP' + tag), None)
    check('POST /kami/apps 带 code/server_id', app_id is not None, 'app_id=%s' % app_id)

    code = 'CARD-' + tag
    r = cli.post('/api/v1/kami', headers=auth, json={
        'code': code, 'app_id': app_id, 'server_id': srv_id,
        'duration_hours': 720, 'max_conn': 1, 'bandwidth': -1})
    check('POST /kami 建卡密', r.status_code in (201, 409),
          'HTTP %d %s' % (r.status_code, r.text[:90]))

    def _kami_row():
        got = (cli.get('/api/v1/kami/list', headers=auth,
                       params={'keyword': code}).json() or {}).get('data') or []
        return got[0] if got else {}

    # 1) 契约形状：裸 {code,msg}，不能套 {ok,data}
    r = cli.post('/api/v1/kami/client', data={'type': 'bogus'})
    j = r.json() if r.status_code == 200 else {}
    check('POST /kami/client 未知 type -> "无效事务"',
          j.get('code') == '无效事务' and 'ok' not in j,
          'HTTP %d %s' % (r.status_code, r.text[:90]))

    r = cli.post('/api/v1/kami/client')
    j = r.json() if r.status_code == 200 else {}
    check('POST /kami/client 缺 type -> "非法参数"', j.get('code') == '非法参数',
          'HTTP %d %s' % (r.status_code, r.text[:90]))

    r = cli.post('/api/v1/kami/client/insert', data={'user': 'x'})
    j = r.json() if r.status_code == 200 else {}
    check('insert 缺参 -> "非法参数"', j.get('code') == '非法参数', str(j)[:90])

    # 2) -2 卡密不存在
    r = cli.post('/api/v1/kami/client/insert',
                 data={'user': 'smokeuser', 'pwd': 'abc123', 'code': 'NOPE-' + tag})
    j = r.json() if r.status_code == 200 else {}
    check('insert 未知卡密 -> -2 卡密不存在',
          j.get('code') == -2 and j.get('msg') == '卡密不存在', str(j)[:90])

    # 3) -1 用户名不合法（校验在占位之前，卡密不能被动到）
    r = cli.post('/api/v1/kami/client/insert',
                 data={'user': 'bad-name', 'pwd': 'abc123', 'code': code})
    j = r.json() if r.status_code == 200 else {}
    check('insert 用户名含非法字符 -> -1 用户名不合法',
          j.get('code') == -1 and j.get('msg') == '用户名不合法', str(j)[:90])

    # 4) -1 密码不合法（纯数字不满足 CheckStrPwd）
    r = cli.post('/api/v1/kami/client/insert',
                 data={'user': 'smokeuser', 'pwd': 'TestPass1!', 'code': code})
    j = r.json() if r.status_code == 200 else {}
    check('insert 纯数字密码 -> -1 密码不合法',
          j.get('code') == -1 and j.get('msg') == '密码不合法', str(j)[:90])

    # 5) 上游是死是活，先探一次。
    #    ★ 这一段的正/负断言完全取决于上游可达性：本机（上游 502）只能验
    #      「拒绝路径」，服务器（上游通）才能验「真建号」。写死任何一边，
    #      另一边必然假红 —— 第一次上服务器就是这么假红了一条。
    up = cli.get('/api/v1/ccpx/accounts', headers=auth)
    up_ok = up.status_code == 200 and bool((up.json() or {}).get('ok'))
    print('     上游可达 = %s' % up_ok)

    # 6) 格式校验都在占位之前 —— 无论上游死活，卡密都不能被动到。
    #    上面 3)、4) 已经走过两遍，这里确认卡密仍是 unused。
    row = _kami_row()
    check('格式校验失败后卡密仍是 unused（校验在占位之前）',
          row.get('state') == 'unused', 'state=%s' % row.get('state'))
    # 出口没落到卡密上，后面 insert 会以「-3 服务器通信出现问题」收场 ——
    # 那个报错看着像上游挂了，实际是出口没接上。单独钉一条，别让它在别处现形。
    check('卡密带上了出口（serverId 非空）', row.get('serverId') is not None,
          'serverId=%s appId=%s' % (row.get('serverId'), row.get('appId')))

    if up_ok:
        # ---- 上游可达：验「真建号」 ------------------------------------ #
        # 先清掉上一次跑剩的，否则第二条 insert 会回 -1 账号已经存在。
        cli.delete('/api/v1/ccpx/accounts/smokeuser', headers=auth)

        r = cli.post('/api/v1/kami/client/insert',
                     data={'user': 'smokeuser', 'pwd': 'abc123', 'code': code})
        j = r.json() if r.status_code == 200 else {}
        check('insert 上游可达 -> 1 注册用户成功',
              j.get('code') == 1 and j.get('msg') == '注册用户成功', str(j)[:110])

        row = _kami_row()
        # 核销成功后 state 算出来是 active（不是 used）—— SQL 里没有 state 列。
        check('核销成功后卡密变 active 且记下使用账号',
              row.get('state') == 'active' and row.get('usedBy') == 'smokeuser',
              'state=%s usedBy=%s' % (row.get('state'), row.get('usedBy')))

        # 账号真的落到上游了（不是「本服务以为建了」）
        rows = (cli.get('/api/v1/ccpx/accounts', headers=auth).json() or {}).get('data') or []
        got = next((x for x in rows if x.get('user') == 'smokeuser'), None)
        check('账号真出现在 CCProxy 账号表', got is not None, str(got)[:110])

        # query：msg 必须是 HTML 片段 —— 页面直接 innerHTML，回纯文本会丢颜色。
        r = cli.post('/api/v1/kami/client/query',
                     data={'user': 'smokeuser', 'appcode': 'APP' + tag})
        j = r.json() if r.status_code == 200 else {}
        check('query 回 HTML 片段且含到期时间',
              j.get('code') == 1 and '<h5' in (j.get('msg') or '')
              and '到期时间' in (j.get('msg') or ''), str(j)[:110])

        # 同一张卡再走 update -> 已被上面那次核销用掉
        r = cli.post('/api/v1/kami/client/update',
                     data={'user': 'smokeuser', 'code': code})
        j = r.json() if r.status_code == 200 else {}
        check('update 已用卡密 -> -1 卡密已被使用',
              j.get('code') == -1 and j.get('msg') == '卡密已被使用', str(j)[:110])

        # 收尾：删掉这次建出来的账号，不留生产垃圾。
        d = cli.delete('/api/v1/ccpx/accounts/smokeuser', headers=auth)
        check('收尾：删除 smokeuser（不留生产垃圾）',
              d.status_code in (200, 204), 'HTTP %d %s' % (d.status_code, d.text[:80]))
        rows = (cli.get('/api/v1/ccpx/accounts', headers=auth).json() or {}).get('data') or []
        check('收尾后账号表已无 smokeuser',
              not any(x.get('user') == 'smokeuser' for x in rows))
    else:
        # ---- 上游不可达：验「拒绝路径 + 占位回滚」 ---------------------- #
        r = cli.post('/api/v1/kami/client/insert',
                     data={'user': 'smokeuser', 'pwd': 'abc123', 'code': code})
        j = r.json() if r.status_code == 200 else {}
        check('insert 上游不可达 -> -3 服务器通信出现问题',
              j.get('code') == -3 and j.get('msg') == '服务器通信出现问题', str(j)[:110])

        # ★ 关键断言：占位必须已回滚 —— 上游失败不能把用户的卡烧掉
        row = _kami_row()
        check('insert 失败后卡密回到 unused（占位已回滚）',
              row.get('state') == 'unused', 'state=%s' % row.get('state'))

        r = cli.post('/api/v1/kami/client/update',
                     data={'user': 'smokeuser', 'code': code})
        j = r.json() if r.status_code == 200 else {}
        check('update 上游不可达 -> -3 服务器通信出现问题', j.get('code') == -3, str(j)[:110])

        r = cli.post('/api/v1/kami/client/query',
                     data={'user': 'smokeuser', 'appcode': 'NOAPP' + tag})
        j = r.json() if r.status_code == 200 else {}
        check('query 未知 appcode -> -3', j.get('code') == -3, str(j)[:110])

    # 7) /redeem 别名：契约形状必须与 insert 一致（裸 {code,msg}）。
    #    卡密此刻已被用掉，所以这条无论上游死活都该走「拒绝」——
    #    这里只钉形状与 code 取值域，不钉具体语义。
    r = cli.post('/api/v1/kami/redeem',
                 data={'user': 'smokeuser', 'pwd': 'abc123', 'code': code})
    j = r.json() if r.status_code == 200 else {}
    check('/redeem 别名 -> 裸 {code,msg} 且走 insert 语义',
          'ok' not in j and 'msg' in j and j.get('code') in (1, -1, -2, -3),
          str(j)[:110])

    print('\n%d PASS / %d FAIL / %d SKIP' % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    for i in range(30):
        try:
            httpx.get(BASE + '/healthz', timeout=2.0)
            break
        except Exception:                          # noqa: BLE001
            time.sleep(0.5)
    else:
        print('服务没起来：%s' % BASE)
        sys.exit(2)
    sys.exit(main())
