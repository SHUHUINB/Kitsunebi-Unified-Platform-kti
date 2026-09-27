# -*- coding: utf-8 -*-
"""MCP（WPE 工具化入口）线上端到端验证。在服务器本机跑。

原则：
  * **只做可安全回滚的写操作**。改写规则先存后设再还原；断点只做「关闭」方向。
  * **不碰 wpe_clear**（会清掉用户正在看的封包缓冲），也不开真断点
    （空规则断点会拦下所有流量）。这两项如实标注为未实机验证。
  * 写路径用**真的收得到字节**来证明：起一个本地 TCP 监听口，
    wpe_send / wpe_replay 往里发，收到才算通。
"""
import http.client
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8890'
URL = BASE + '/mcp'
ECHO_PORT = 19100

PASS, FAIL = [], []


def ck(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print('%-52s %s%s' % (name, 'PASS' if cond else 'FAIL',
                          ('  | ' + str(extra)) if extra else ''))


_seq = [0]


def _nid():
    _seq[0] += 1
    return _seq[0]


def rpc(obj, timeout=40):
    req = urllib.request.Request(URL, data=json.dumps(obj).encode('utf-8'),
                                 method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json, text/event-stream')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8')
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8')
        try:
            raw = json.loads(raw)
        except Exception:                          # noqa: BLE001
            pass
        return e.code, raw, dict(e.headers)


def tool(name, args=None):
    st, resp, _ = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'tools/call',
                       'params': {'name': name, 'arguments': args or {}}})
    if st != 200:
        return {'ok': False, 'http': st, 'err': str(resp)[:400]}
    res = (resp or {}).get('result') or {}
    txt = ((res.get('content') or [{}])[0] or {}).get('text') or '{}'
    try:
        obj = json.loads(txt)
    except Exception:                              # noqa: BLE001
        obj = {'raw': txt}
    if not isinstance(obj, dict):
        obj = {'value': obj}
    obj['_isError'] = res.get('isError')
    return obj


class Echo(threading.Thread):
    """本地 TCP 监听口：收到的字节都存下来，用来证明写路径真的发出去了。"""

    def __init__(self, port):
        threading.Thread.__init__(self, daemon=True)
        self.port = port
        self.got = []
        self._stop = False
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', port))
        self.srv.listen(8)
        self.srv.settimeout(0.4)

    def run(self):
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            c.settimeout(2.0)
            try:
                self.got.append(c.recv(65535))
            except Exception:                      # noqa: BLE001
                pass
            try:
                c.close()
            except Exception:                      # noqa: BLE001
                pass

    def stop(self):
        self._stop = True
        try:
            self.srv.close()
        except Exception:                          # noqa: BLE001
            pass


print('== 0. 先生成一点流量 ==')
# 缓冲是环形的，服务重启后是空的；不先灌一点，后面所有「依赖真实封包」的
# 断言都会因为没数据而失败 —— 那是环境问题，不是功能问题，别混在一起。
try:
    _op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'http': 'http://TestPass1!:Ssh-ChangeMe-2026@127.0.0.1:8888'}))
    _ok = 0
    for _i in range(12):
        with _op.open('http://example.com/?mcp=%d' % _i, timeout=15) as _r:
            _r.read(200)
        _ok += 1
    print('    通过代理发出 %d 条请求' % _ok)
except Exception as _exc:                          # noqa: BLE001
    print('    生成流量失败: %s' % _exc)
time.sleep(1.5)

print('== 1. 协议握手 ==')
st, resp, hdrs = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'initialize',
                      'params': {'protocolVersion': '2025-06-18',
                                 'capabilities': {},
                                 'clientInfo': {'name': 'selftest',
                                                'version': '1'}}})
res = (resp or {}).get('result') or {}
ck('initialize HTTP 200', st == 200, st)
ck('回 protocolVersion', res.get('protocolVersion') == '2025-06-18',
   res.get('protocolVersion'))
ck('回 serverInfo.name=ccpx-wpe',
   (res.get('serverInfo') or {}).get('name') == 'ccpx-wpe', res.get('serverInfo'))
ck('声明 tools 能力', 'tools' in (res.get('capabilities') or {}))
ck('带 instructions', bool(res.get('instructions')))
ck('带 Mcp-Session-Id 响应头',
   any(k.lower() == 'mcp-session-id' for k in (hdrs or {})), list(hdrs or {}))

st, resp, _ = rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
ck('initialized 通知回 202 空体', st == 202, st)

st, resp, _ = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'ping'})
ck('ping 正常', st == 200 and (resp or {}).get('result') == {}, resp)

st, resp, _ = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'tools/list'})
tools = ((resp or {}).get('result') or {}).get('tools') or []
ck('tools/list 回 17 个工具', len(tools) == 17, len(tools))
ck('工具名齐备',
   set(t['name'] for t in tools) == {
       'wpe_stat', 'wpe_list', 'wpe_get', 'wpe_struct', 'wpe_rewrite_get',
       'wpe_rewrite_set', 'wpe_hold_get', 'wpe_hold_set', 'wpe_release',
       'wpe_replay', 'wpe_send', 'wpe_export', 'wpe_export_save',
       'wpe_export_list', 'wpe_export_download', 'wpe_import', 'wpe_clear'},
   len(tools))

print('== 2. GET / DELETE / 错误路径 ==')
c = http.client.HTTPConnection('127.0.0.1', 8890, timeout=10)
c.request('GET', '/mcp')                       # 不带 SSE Accept
r = c.getresponse()
r.read()
ck('GET 无 SSE Accept 回 405', r.status == 405, r.status)
c.close()

c = http.client.HTTPConnection('127.0.0.1', 8890, timeout=10)
c.request('DELETE', '/mcp')
r = c.getresponse()
r.read()
ck('DELETE 回 204', r.status == 204, r.status)
c.close()

st, resp, _ = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'tools/call',
                   'params': {'name': 'no_such_tool', 'arguments': {}}})
res = (resp or {}).get('result') or {}
ck('未知工具走 isError', res.get('isError') is True, res.get('isError'))

st, resp, _ = rpc({'jsonrpc': '2.0', 'id': _nid(), 'method': 'nope'})
ck('未知方法回 -32601', ((resp or {}).get('error') or {}).get('code') == -32601,
   resp)

req = urllib.request.Request(URL, data=b'{bad json', method='POST')
req.add_header('Content-Type', 'application/json')
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        ck('坏 JSON 回 400', r.status == 400, r.status)
except urllib.error.HTTPError as e:
    ck('坏 JSON 回 400', e.code == 400, e.code)

print('== 3. 读类工具（真实数据）==')
s = tool('wpe_stat')
ck('wpe_stat ok', s.get('ok') is True, s)
st_ = s.get('stat') or {}
ck('stat 带 buffered/capacity', 'buffered' in st_ and 'capacity' in st_,
   {k: st_.get(k) for k in ('buffered', 'capacity', 'captured')})
ck('stat 带 udp 账本', isinstance(st_.get('udp'), dict), st_.get('udp'))

ls = tool('wpe_list', {'limit': 5})
ck('wpe_list ok', ls.get('ok') is True, ls)
pk = ls.get('packets') or []
ck('list 返回封包数组', isinstance(pk, list), len(pk))
ck('list 带翻页元信息',
   all(k in ls for k in ('oldest_id', 'newest_id', 'has_more', 'scanned',
                         'buffered', 'truncated')),
   {k: ls.get(k) for k in ('oldest_id', 'newest_id', 'has_more')})

if pk:
    pid = pk[0]['id']
    g = tool('wpe_get', {'id': pid})
    ck('wpe_get ok', g.get('ok') is True, g.get('ok'))
    p = g.get('packet') or {}
    ck('get 带 hex/ascii/元数据',
       bool(p.get('hex')) and 'ascii' in p and 'dir' in p and 'proto' in p,
       list(p.keys())[:8])
    ck('get 带分层结构解析 struct', isinstance(g.get('struct'), dict),
       (g.get('struct') or {}).get('layers') and
       [l.get('name') for l in (g['struct'].get('layers') or [])])
    ck('struct 标出数据来源 of', g.get('struct', {}).get('of') in ('hex', 'mod_hex'),
       g.get('struct', {}).get('of'))
    s2 = tool('wpe_struct', {'id': pid})
    ck('wpe_struct 与 get 的结构一致',
       json.dumps(s2.get('struct'), sort_keys=True)
       == json.dumps(g.get('struct'), sort_keys=True))
else:
    ck('缓冲里有包（后续 get/struct 断言依赖）', False, '缓冲为空')

print('== 4. 过滤器改写（先存原值，测完还原）==')
rw0 = tool('wpe_rewrite_get')
ck('rewrite_get ok', rw0.get('ok') is True, rw0)
orig_on = rw0.get('on')
orig_rules = rw0.get('rules') or []
print('    原状态: on=%s rules=%d 条' % (orig_on, len(orig_rules)))

never = [{'act': 'find_replace', 'find': '00', 'replace': '00',
          'target': '__mcp_selftest_never_matches__'}]
rw1 = tool('wpe_rewrite_set', {'on': False, 'rules': never})
ck('rewrite_set ok', rw1.get('ok') is True, rw1)
ck('rewrite_set 无被拒规则', not (rw1.get('rejected') or []), rw1.get('rejected'))
ck('rewrite_set 规则已生效（1 条）', len(rw1.get('rules') or []) == 1,
   rw1.get('rules'))
rw2 = tool('wpe_rewrite_get')
ck('rewrite_get 读回同一条规则',
   (rw2.get('rules') or [{}])[0].get('target') == '__mcp_selftest_never_matches__',
   rw2.get('rules'))
rw3 = tool('wpe_rewrite_set', {'on': True, 'rules': []})
ck('空规则表被拒（至少一条）', rw3.get('ok') is False, rw3)

# 还原
if orig_rules:
    rr = tool('wpe_rewrite_set', {'on': bool(orig_on), 'rules': orig_rules})
else:
    rr = tool('wpe_rewrite_set', {'on': bool(orig_on), 'rules': [
        {'act': 'find_replace', 'find': '00', 'replace': '00',
         'target': '__mcp_selftest_never_matches__'}]})
ck('还原改写规则', rr.get('ok') is True, rr.get('ok'))
rwf = tool('wpe_rewrite_get')
ck('还原后规则条数与原值一致',
   len(rwf.get('rules') or []) == max(1, len(orig_rules)),
   '%s vs %s' % (len(rwf.get('rules') or []), len(orig_rules)))

print('== 5. 断点（只做关闭方向，不开真断点）==')
h0 = tool('wpe_hold_get')
ck('hold_get ok', h0.get('ok') is True, h0)
ck('hold_get 带 on/rule/holding',
   all(k in h0 for k in ('on', 'rule', 'holding')), h0)
h1 = tool('wpe_hold_set', {'on': False, 'rule': {'target': '__mcp_selftest__'}})
ck('hold_set ok', h1.get('ok') is True, h1)
ck('hold_set 回显 on=False', h1.get('on') is False, h1.get('on'))
h2 = tool('wpe_hold_get')
ck('hold_get 读回 on=False', h2.get('on') is False, h2.get('on'))
ck('hold_get 读回规则', (h2.get('rule') or {}).get('target') == '__mcp_selftest__',
   h2.get('rule'))
# 断点关闭后队列不该积压
ck('断点关闭时队列为空', not h2.get('holding'), h2.get('holding'))

print('== 6. 导出（内存分页 / 落盘 / 列表 / 下载）==')
ex = tool('wpe_export', {'limit': 2})
ck('export ok', ex.get('ok') is True, ex)
ck('export 是完整 hex（不是列表预览）', ex.get('full_hex') is True,
   ex.get('full_hex'))
ck('export 带续导游标 next_since', 'next_since' in ex, ex.get('next_since'))
ck('export 如实报上限与是否导完',
   'complete' in ex and isinstance(ex.get('limits'), dict),
   {k: ex.get(k) for k in ('complete', 'stopped_by')})

sv = tool('wpe_export_save', {'kind': 'json'})
ck('export_save ok', sv.get('ok') is True, sv)
fname = sv.get('name')
ck('export_save 给出文件名', bool(fname), fname)
ck('export_save 报出导出条数', 'exported' in sv,
   {k: sv.get(k) for k in ('exported', 'buffered', 'size')})

el = tool('wpe_export_list')
ck('export_list ok', el.get('ok') is True, el)
ck('export_list 含刚导出的文件',
   any(f.get('name') == fname for f in (el.get('files') or [])), fname)

if fname:
    dl = tool('wpe_export_download', {'name': fname})
    ck('export_download ok', dl.get('ok') is True, dl.get('ok'))
    # 三种合法形态：小 .txt 走 raw_text；小 .json 被解析成对象；
    # 大文件走「预览 + 下载地址」。断言不能只认其中一条。
    if dl.get('raw_text') is not None:
        txt = dl['raw_text']
        ck('下载内容是真 JSON（含 packets）', '"packets"' in txt, txt[:80])
        ck('下载内容带完整 hex 标记', '"hex"' in txt, txt[:80])
    elif dl.get('truncated') and dl.get('download_url'):
        _u = str(dl['download_url'])
        # ★ 断言的是「地址必须自足」，不是某个具体路径。
        #   以前这里写死旧管理台的 `/api/wpe/dl?name=` —— 那条路由随旧
        #   console 一起删了，而且它给的是**相对路径**，MCP 客户端拿到手
        #   不知道往哪发，就算拼上 host 也还要登录（客户端没有会话 cookie），
        #   等于「给了地址但门锁着」。现在给的是绝对 URL + 一次性令牌。
        ck('大文件下载给预览 + 下载地址',
           _u.startswith('http://') or _u.startswith('https://'), _u)
        ck('下载地址自带令牌（无需登录）', 't=' in _u, _u[:80])
        ck('大文件下载如实报出字节数',
           isinstance(dl.get('bytes'), int) and dl['bytes'] > 0, dl.get('bytes'))
        ck('大文件下载的说明指向下载地址',
           'download_url' in str(dl.get('note')), dl.get('note'))
        # 给的地址必须真能取到完整文件 —— 否则那句提示就是空话。
        try:
            # 绝对地址直接用；万一哪天又退回相对路径，才拼 BASE。
            _full = _u if _u.startswith('http') else (BASE + _u)
            with urllib.request.urlopen(_full, timeout=300) as _r:
                _blob = _r.read()
            ck('download_url 真能取到完整文件', len(_blob) == dl['bytes'],
               '%d vs %s' % (len(_blob), dl['bytes']))
        except Exception as _exc:                  # noqa: BLE001
            ck('download_url 真能取到完整文件', False, _exc)
        # 反例：换个文件名，令牌必须失效（否则一个地址能下走所有导出）。
        try:
            _bad = _u.replace(urllib.parse.quote(fname, safe=''),
                              urllib.parse.quote('wpe-19700101-000000.json', safe=''))
            urllib.request.urlopen(_bad, timeout=20).close()
            ck('令牌绑死文件名（换名必须被拒）', False, '竟然成功了')
        except urllib.error.HTTPError as _e:
            ck('令牌绑死文件名（换名必须被拒）', _e.code in (400, 401, 403, 404), _e.code)
        except Exception as _exc:                  # noqa: BLE001
            ck('令牌绑死文件名（换名必须被拒）', False, _exc)
    else:
        ck('下载的 .json 解析成导出对象（含 packets 数组）',
           isinstance(dl.get('packets'), list), list(dl.keys())[:10])
        ck('下载对象带 exported/buffered 计数',
           'exported' in dl and 'buffered' in dl,
           {k: dl.get(k) for k in ('exported', 'buffered', 'format')})

dlbad = tool('wpe_export_download', {'name': '../ccproxy_adapter.py'})
ck('download 拒绝路径穿越', dlbad.get('ok') is False, dlbad)

print('== 7. 写路径：wpe_send / wpe_replay 真发字节 ==')
echo = Echo(ECHO_PORT)
echo.start()
time.sleep(0.3)
payload = '4d43502d53454c4654455354'      # "MCP-SELFTEST"
sd = tool('wpe_send', {'proto': 'tcp', 'target': '127.0.0.1:%d' % ECHO_PORT,
                       'data': payload})
ck('wpe_send ok', sd.get('ok') is True, sd)
time.sleep(0.6)
got = [b for b in echo.got if b]
ck('wpe_send 的字节真的到了目标', any(b == b'MCP-SELFTEST' for b in got), got)

# 重放：拿一个真实抓到的 tcp 包，改目标打到本地监听口
tcp_id = None
for p in pk:
    if (p.get('proto') or '') == 'tcp':
        tcp_id = p['id']
        break
if tcp_id is None:
    ls2 = tool('wpe_list', {'limit': 50, 'proto': 'tcp'})
    for p in (ls2.get('packets') or []):
        tcp_id = p['id']
        break
if tcp_id:
    before = len([b for b in echo.got if b])
    rp = tool('wpe_replay', {'id': tcp_id, 'target': '127.0.0.1:%d' % ECHO_PORT,
                             'times': 1})
    ck('wpe_replay ok', rp.get('ok') is True, rp)
    time.sleep(0.6)
    after = len([b for b in echo.got if b])
    ck('wpe_replay 的字节真的到了目标', after > before,
       '%d -> %d' % (before, after))
else:
    ck('找到可重放的 tcp 包', False, '缓冲里没有 tcp 包')

print('== 8. 导入 ==')
im = tool('wpe_import', {'packets': [
    {'hex': '4d43502d494d504f5254', 'len': 10, 'dir': 'c2s', 'proto': 'tcp',
     'target': 'selftest:1', 'app': 'BIN'}]})
ck('wpe_import ok', im.get('ok') is True, im)
ck('import 报出 imported 数', 'imported' in im,
   {k: im.get(k) for k in ('imported', 'skipped', 'buffered')})
ls3 = tool('wpe_list', {'limit': 20, 'label': 'imported'})
ck('导入的包能在列表里查到',
   any('imported' in (p.get('label') or '') for p in (ls3.get('packets') or [])),
   [p.get('label') for p in (ls3.get('packets') or [])][:5])

echo.stop()

print('')
print('==== %d PASS / %d FAIL ====' % (len(PASS), len(FAIL)))
if FAIL:
    print('失败项:')
    for f in FAIL:
        print('  - ' + f)
print('未做实机验证（有意的）：wpe_clear（会清掉用户正在看的封包缓冲）、'
      'wpe_hold_set 开启真断点（空规则会拦下全部流量）、wpe_release（需要先有被断住的包）')
sys.exit(1 if FAIL else 0)
