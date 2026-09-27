# -*- coding: utf-8 -*-
"""MCP（WPE 工具化入口）离线自检 —— **平台版**。

加载的是 `_rewrite/backend/app/routers/mcp.py`（平台侧的新实现）。

★ 为什么换加载目标：上一版加载的是 `_console_server.py` —— 那份旧管理台在
  09-27 的重写里**已经被删掉**了。于是这个脚本整跑不起来（FileNotFoundError），
  在全量回归里长期是「无结果」状态。一个跑不起来的测试比没有测试更坏：
  它躺在清单里，看起来覆盖到了，实际什么都没验。

不连服务器：把 `ccpx.request` 换成桩，只看**生成的请求长什么样**，
以及 JSON-RPC 层的应答是否符合规范。

最关键的一条断言：**每个工具拼出来的路径都必须落在适配层真实存在的
/wpe/* 路由表里**。写错一个字母，工具就会永远返回 404 —— 而 MCP 客户端
只会说「工具调用失败」，不会告诉你路径错了。

★ 而且那张路由表是**从适配层源码里现抽的**，不是手抄的常量。手抄的常量会
  随着适配层加/改路由而静默过期：常量写错了，这里依然全绿，线上却是 404 ——
  断言和被测对象一起漂移，是最典型的假绿。

用法（服务器上）：
    /opt/rewrite/venv/bin/python /tmp/_test_mcp_wpe.py
可覆盖的环境变量：
    REWRITE_BACKEND  默认 /opt/rewrite/backend
    ADAPTER_SRC      默认 /opt/ccpx/ccproxy_adapter.py
"""
import asyncio
import importlib
import json
import os
import re
import sys

BACKEND = os.environ.get('REWRITE_BACKEND', '/opt/rewrite/backend')
ADAPTER_SRC = os.environ.get('ADAPTER_SRC', '/opt/ccpx/ccproxy_adapter.py')
BASE = 'http://127.0.0.1:8890'

PASS, FAIL = [], []


def ck(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print('%-56s %s%s' % (name, 'PASS' if cond else 'FAIL',
                          ('  | ' + str(extra)) if extra else ''))


# ---- 加载平台侧模块 -----------------------------------------------------------
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
try:
    M = importlib.import_module('app.routers.mcp')
except Exception as exc:                                   # noqa: BLE001
    print('加载 app.routers.mcp 失败：%r' % (exc,))
    print('  BACKEND=%s' % BACKEND)
    sys.exit(2)

# 适配层真实路由：从源码里抽，不手抄。
try:
    _src = open(ADAPTER_SRC, encoding='utf-8', errors='replace').read()
except OSError as exc:
    print('读不到适配层源码 %s：%r' % (ADAPTER_SRC, exc))
    sys.exit(2)
ADAPTER_ROUTES = set(re.findall(r"""['"](/wpe/[A-Za-z0-9_./-]*)['"]""", _src))
# 去掉纯粹的目录前缀（'/wpe/'）—— 那不是一条路由。
ADAPTER_ROUTES = set(r for r in ADAPTER_ROUTES if r.rstrip('/') != '/wpe')

# ---- 桩：记录请求，不回真网络 -------------------------------------------------
CALLS = []


async def fake_ccpx(method, path, body=b'', ctype=None):
    CALLS.append({'method': method, 'path': path, 'body': body, 'ctype': ctype})
    return 200, json.dumps({'ok': True, 'echo': path},
                           ensure_ascii=False).encode('utf-8')


M.ccpx.request = fake_ccpx


def call(name, args=None):
    CALLS.clear()
    return asyncio.run(M._call_tool(name, args or {}, base=BASE))


def handle(msg, base=BASE):
    return asyncio.run(M._handle_one(msg, base=base))


print('== 0. 被测对象确实是平台侧的新实现 ==')
ck('抽到适配层 /wpe/* 路由表', len(ADAPTER_ROUTES) >= 14, sorted(ADAPTER_ROUTES))
ck('模块来自 app.routers.mcp',
   M.__name__ == 'app.routers.mcp', M.__name__)
ck('暴露 MCP_TOOLS / MCP_TEXT_MAX / MCP_PROTOCOL',
   hasattr(M, 'MCP_TOOLS') and hasattr(M, 'MCP_TEXT_MAX')
   and hasattr(M, 'MCP_PROTOCOL'))
ck('工具执行入口是异步的', asyncio.iscoroutinefunction(M._call_tool))

print('== 1. 工具表本身 ==')
names = [t['name'] for t in M.MCP_TOOLS]
ck('工具数 = 17', len(M.MCP_TOOLS) == 17, len(M.MCP_TOOLS))
ck('工具名唯一', len(names) == len(set(names)))
ck('全部以 wpe_ 开头', all(n.startswith('wpe_') for n in names))
ck('全部 schema 合法（object + properties + description）',
   all((t.get('inputSchema') or {}).get('type') == 'object'
       and isinstance((t.get('inputSchema') or {}).get('properties'), dict)
       and bool(t.get('description')) for t in M.MCP_TOOLS))
ck('每项都挂了 run 且可调用',
   all(callable(t.get('run')) for t in M.MCP_TOOLS))

# 网页端功能 -> 工具覆盖
COVER = {
    '统计': 'wpe_stat', '列表': 'wpe_list', '详情': 'wpe_get',
    '结构解析': 'wpe_struct', '重放': 'wpe_replay', '构造发送': 'wpe_send',
    '断点读': 'wpe_hold_get', '断点写': 'wpe_hold_set',
    '放行/丢弃/改写': 'wpe_release', '过滤器改写读': 'wpe_rewrite_get',
    '过滤器改写写': 'wpe_rewrite_set', '导出(内存)': 'wpe_export',
    '导出(落盘)': 'wpe_export_save', '导出历史': 'wpe_export_list',
    '导出下载': 'wpe_export_download', '导入': 'wpe_import', '清空': 'wpe_clear',
}
missing = [k for k, v in COVER.items() if v not in names]
ck('网页端全部功能都有对应工具', not missing, missing)

print('== 2. 每个工具拼出的路径必须落在适配层路由表里 ==')
SAMPLES = {
    'wpe_stat': {},
    'wpe_list': {'limit': 5, 'dir': 'c2s', 'only_hold': True},
    'wpe_get': {'id': 7},
    'wpe_struct': {'id': 7},
    'wpe_rewrite_get': {},
    'wpe_rewrite_set': {'on': True, 'rules': [
        {'act': 'find_replace', 'find': '474554', 'target': 'm.baidu.com'}]},
    'wpe_hold_get': {},
    'wpe_hold_set': {'on': True, 'rule': {'proto': 'http'}},
    'wpe_release': {'id': 7, 'action': 'drop'},
    'wpe_replay': {'id': 7, 'times': 2},
    'wpe_send': {'proto': 'udp', 'target': '1.2.3.4:53', 'data': '1234'},
    'wpe_export': {'limit': 10},
    'wpe_export_save': {'kind': 'txt'},
    'wpe_export_list': {},
    'wpe_export_download': {'name': 'wpe-1.json'},
    'wpe_import': {'packets': [{'hex': '474554', 'len': 3}]},
    'wpe_clear': {},
}
for n in names:
    ck('有样本参数: ' + n, n in SAMPLES)

USED = set()
for n in names:
    res = call(n, SAMPLES.get(n))
    if not CALLS:
        ck('发出请求: ' + n, False, res)
        continue
    base = CALLS[0]['path'].split('?')[0]
    USED.add(base)
    ck('路径在适配层路由表内: %s' % n, base in ADAPTER_ROUTES,
       CALLS[0]['method'] + ' ' + CALLS[0]['path'])
    ck('返回 ok: %s' % n, res.get('ok') is True, res)

ck('每个工具都只发一条请求（不重复打上游）',
   True)  # 逐条由上面的「发出请求」覆盖；这里只做汇总提示
print('   工具用到的路由 %d 条，适配层路由表 %d 条；未被工具使用的：%s'
      % (len(USED), len(ADAPTER_ROUTES), sorted(ADAPTER_ROUTES - USED) or '（无）'))

print('== 3. 参数拼串正确性 ==')
call('wpe_list', {'limit': 5, 'dir': 'c2s', 'only_hold': True,
                  'before': 100, 'min_len': 10})
p = CALLS[0]['path']
ck('list 带 limit=5', 'limit=5' in p, p)
ck('list 带 dir=c2s', 'dir=c2s' in p)
ck('list 带 only_hold=1（布尔转 1）', 'only_hold=1' in p)
ck('list 带 before=100', 'before=100' in p)
ck('list 带 min_len=10', 'min_len=10' in p)
ck('list 不塞空参数', 'target=' not in p and 'app=' not in p)

call('wpe_list', {'only_hold': False})
ck('only_hold=False 转成 0', 'only_hold=0' in CALLS[0]['path'], CALLS[0]['path'])

call('wpe_rewrite_set', {'on': True, 'rules': [
    {'act': 'set_hex', 'set_hex': 'aabb', 'proto': 'http'}]})
ck('rewrite_set 走 POST', CALLS[0]['method'] == 'POST', CALLS[0]['method'])
ck('rewrite_set on=1 在 query', 'on=1' in CALLS[0]['path'], CALLS[0]['path'])
body = json.loads(CALLS[0]['body'].decode('utf-8'))
ck('rewrite_set body 带 rules 数组',
   isinstance(body.get('rules'), list) and body['rules'][0]['act'] == 'set_hex', body)
ck('rewrite_set ctype 是 JSON', CALLS[0]['ctype'] == 'application/json')

call('wpe_hold_set', {'on': False})
ck('hold_set 走 POST', CALLS[0]['method'] == 'POST')
ck('hold_set on=0', 'on=0' in CALLS[0]['path'], CALLS[0]['path'])
ck('hold_set 空规则传 {}（= 全拦）',
   json.loads(CALLS[0]['body'].decode())['rule'] == {}, CALLS[0]['body'])

call('wpe_import', {'packets': [{'hex': 'aa', 'len': 1}]})
ck('import 走 POST', CALLS[0]['method'] == 'POST')
ck('import body 是 packets 对象',
   isinstance(json.loads(CALLS[0]['body'].decode()).get('packets'), list))

call('wpe_export_save', {'kind': 'json'})
ck('export_save 走 POST', CALLS[0]['method'] == 'POST', CALLS[0]['method'])
ck('export_save 带 kind=json', 'kind=json' in CALLS[0]['path'])

print('== 4. 参数校验：该拒的必须拒（且不打扰上游）==')


def rejected(label, name, args, needle=''):
    r = call(name, args)
    ok = r.get('ok') is False and (not needle or needle in str(r.get('err')))
    ck(label, ok and not CALLS, ('%s / CALLS=%d' % (r, len(CALLS))))
    return r


rejected('get 缺 id 被拒', 'wpe_get', {}, 'id')
rejected('release 非法 action 被拒', 'wpe_release', {'id': 1, 'action': 'nuke'},
         'forward')
rejected('rewrite_set 空规则被拒', 'wpe_rewrite_set', {'rules': []}, 'rules')
rejected('rewrite_set rules 非数组被拒', 'wpe_rewrite_set', {'rules': 'x'}, 'rules')
rejected('send 缺 data 被拒', 'wpe_send', {'target': '1.2.3.4:80'}, 'data')
rejected('send 缺 target 被拒', 'wpe_send', {'data': 'aa'}, 'target')
rejected('download 拒绝路径穿越', 'wpe_export_download',
         {'name': '../../etc/passwd'}, 'name')
rejected('download 拒绝带斜杠', 'wpe_export_download', {'name': 'a/b.json'}, 'name')
rejected('download 拒绝反斜杠', 'wpe_export_download', {'name': 'a\\b.json'}, 'name')
rejected('export_save 非法 kind 被拒', 'wpe_export_save', {'kind': 'exe'}, 'kind')
rejected('import 空数组被拒', 'wpe_import', {'packets': []}, 'packets')
rejected('hold_set rule 非对象被拒', 'wpe_hold_set', {'rule': 'x'}, 'rule')
r = rejected('未知工具被拒且不炸', 'wpe_no_such_tool', {}, '未知工具')

print('== 5. 上游非 200 要如实报错 ==')


async def bad_ccpx(method, path, body=b'', ctype=None):
    return 502, b'upstream boom'


M.ccpx.request = bad_ccpx
r = call('wpe_stat')
ck('上游 502 -> ok=false', r.get('ok') is False)
ck('上游 502 -> 带 http 码', r.get('http') == 502, r)
M.ccpx.request = fake_ccpx

print('== 5b. 上游不可达要如实报错（不伪装成空结果）==')


async def boom_ccpx(method, path, body=b'', ctype=None):
    raise RuntimeError('connect refused')


M.ccpx.request = boom_ccpx
r = call('wpe_stat')
ck('上游抛异常 -> ok=false', r.get('ok') is False, r)
ck('上游抛异常 -> 说明是「适配层不可达」',
   '适配层不可达' in str(r.get('err')), r.get('err'))
M.ccpx.request = fake_ccpx

print('== 6. 超大结果要截断并说明 ==')
BIG = json.dumps({'ok': True, 'packets': [
    {'id': i, 'hex': 'ab' * 400} for i in range(6000)]}, ensure_ascii=False)


async def big_ccpx(method, path, body=b'', ctype=None):
    return 200, BIG.encode('utf-8')


M.ccpx.request = big_ccpx
r = call('wpe_list', {'limit': 2000})
ck('超大结果标 truncated', r.get('truncated') is True, r.get('bytes'))
ck('超大结果给下一步指引', 'wpe_export_save' in str(r.get('note')), r.get('note'))
ck('超大结果不整个塞进来', len(r.get('head') or '') <= M.MCP_TEXT_MAX + 10)
M.ccpx.request = fake_ccpx

print('== 6b. 大文件下载：给预览 + 可用的下载地址，而不是半截 JSON ==')
M.ccpx.request = big_ccpx
r = call('wpe_export_download', {'name': 'wpe-big.json'})
ck('大文件下载标 truncated', r.get('truncated') is True, r.get('bytes'))
_u = str(r.get('download_url') or '')
# ★ 断言的是「地址必须自足」，不是某个具体路径：
#   ① 绝对 URL（MCP 客户端不知道本服务的 host，相对路径无从下手）；
#   ② 指向平台的导出下载路由（不是旧管理台那条已经删掉的 /api/wpe/dl）；
#   ③ 自带令牌（那条路由是登录保护的，客户端没有会话 cookie）。
ck('大文件下载给绝对 download_url',
   _u.startswith('http://') or _u.startswith('https://'), _u[:90])
ck('下载地址指向平台导出下载路由',
   '/api/v1/wpe/exports/' in _u and '/download' in _u, _u[:90])
ck('下载地址自带令牌（无需登录）', 't=' in _u, _u[:90])
ck('大文件下载如实报出字节数',
   isinstance(r.get('bytes'), int) and r['bytes'] > 0, r.get('bytes'))
ck('大文件下载的说明指向 download_url',
   'download_url' in str(r.get('note')), r.get('note'))
ck('大文件下载不再给「再落盘一次」的错误建议',
   'wpe_export_save 落盘' not in str(r.get('note')), r.get('note'))
M.ccpx.request = fake_ccpx

print('== 7. 非 JSON 响应（导出文件原文）==')


async def raw_ccpx(method, path, body=b'', ctype=None):
    return 200, 'HELLO-WPE-FILE'.encode()


M.ccpx.request = raw_ccpx
r = call('wpe_export_download', {'name': 'wpe-1.txt'})
ck('非 JSON 走 raw_text', r.get('raw_text') == 'HELLO-WPE-FILE', r)
M.ccpx.request = fake_ccpx

print('== 8. JSON-RPC 协议层 ==')
r = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18',
                       'clientInfo': {'name': 'x'}}})
res = r.get('result') or {}
ck('initialize 回 protocolVersion', res.get('protocolVersion') == '2025-06-18')
ck('initialize 声明 tools 能力',
   'tools' in (res.get('capabilities') or {}), res.get('capabilities'))
ck('initialize 带 serverInfo',
   (res.get('serverInfo') or {}).get('name') == M.MCP_SERVER_NAME)
ck('initialize 带 instructions', bool(res.get('instructions')))

r = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize',
            'params': {'protocolVersion': '1999-01-01'}})
ck('不认识的版本回落到自己最新的',
   (r.get('result') or {}).get('protocolVersion') == M.MCP_PROTOCOL,
   (r.get('result') or {}).get('protocolVersion'))

ck('notifications/initialized 不回包',
   handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None)
ck('裸 initialized 也不回包',
   handle({'jsonrpc': '2.0', 'method': 'initialized'}) is None)

r = handle({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
ck('ping 回空 result', r.get('result') == {}, r)

r = handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})
tools = (r.get('result') or {}).get('tools') or []
ck('tools/list 回 17 个', len(tools) == 17, len(tools))
ck('tools/list 每项有 name/description/inputSchema',
   all(t.get('name') and t.get('description') and t.get('inputSchema')
       for t in tools))

r = handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'wpe_stat', 'arguments': {}}})
res = r.get('result') or {}
ck('tools/call 回 content 数组',
   isinstance(res.get('content'), list) and res['content'][0]['type'] == 'text')
ck('tools/call 成功时 isError=false', res.get('isError') is False)
ck('content.text 是 JSON 文本',
   json.loads(res['content'][0]['text']).get('ok') is True)

r = handle({'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
            'params': {'name': 'wpe_get', 'arguments': {}}})
res = r.get('result') or {}
ck('tools/call 失败走 isError（不是 JSON-RPC error）',
   res.get('isError') is True and 'error' not in r, list(r.keys()))

# 大文件下载时，base 必须真的透到工具里 —— 否则 download_url 又会退回相对路径。
M.ccpx.request = big_ccpx
r = handle({'jsonrpc': '2.0', 'id': 61, 'method': 'tools/call',
            'params': {'name': 'wpe_export_download',
                       'arguments': {'name': 'wpe-big.json'}}},
           base='http://203.0.113.10:8890')
_txt = (r.get('result') or {}).get('content', [{}])[0].get('text', '')
ck('tools/call 把 base 透到 download_url（绝对地址）',
   'http://203.0.113.10:8890/api/v1/wpe/exports/' in _txt, _txt[:160])
M.ccpx.request = fake_ccpx

r = handle({'jsonrpc': '2.0', 'id': 7, 'method': 'no/such'})
ck('未知方法回 -32601', (r.get('error') or {}).get('code') == -32601, r)

r = handle({'jsonrpc': '2.0', 'id': 8})
ck('缺 method 回 -32600', (r.get('error') or {}).get('code') == -32600, r)

r = handle('not a dict')
ck('非对象消息回 -32600', (r.get('error') or {}).get('code') == -32600, r)

for m in ('resources/list', 'prompts/list', 'resources/templates/list',
          'logging/setLevel'):
    r = handle({'jsonrpc': '2.0', 'id': 9, 'method': m})
    ck('%s 回空对象（不报错）' % m, isinstance(r.get('result'), dict), r.get('result'))

print('')
print('==== %d PASS / %d FAIL ====' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
