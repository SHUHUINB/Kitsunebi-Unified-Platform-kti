# -*- coding: utf-8 -*-
"""WPE 结构解析 + 改写规则：纯函数单测（不连服务器）。

核心断言只有一条但最关键：
    **每个字段的 data[off:off+len] 必须落在载荷范围内。**
结构面板就是拿 off/len 去 hex 里高亮，偏移错了等于给用户指错地方 ——
那比不显示更糟。

另外校验 _is_hex 与 _do_rewrite 的字节替换/整包替换行为。
"""
import importlib.util
import struct
import sys

spec = importlib.util.spec_from_file_location('ccpx', 'ccproxy_adapter.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

PASS, FAIL = [], []


def ck(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print('%-46s %s%s' % (name, 'PASS' if cond else 'FAIL',
                          ('  ' + str(extra)) if (extra and not cond) else ''))


def bounds_ok(data, st, label):
    """所有字段/层的 off/len 都必须落在载荷内。"""
    bad = []
    for f in st['fields']:
        if f['off'] < 0 or f['len'] < 0 or f['off'] + f['len'] > len(data):
            bad.append(('field', f['name'], f['off'], f['len']))
    for l in st['layers']:
        if l['off'] < 0 or l['len'] < 0 or l['off'] + l['len'] > len(data):
            bad.append(('layer', l['name'], l['off'], l['len']))
    ck('%s：偏移全部在界内' % label, not bad, bad)


def find(st, name):
    for f in st['fields']:
        if f['name'] == name:
            return f
    return None


# ---------------------------------------------------------------- _is_hex
print('== 1. _is_hex ==')
ck('空串算合法', M._is_hex(''))
ck('"0d0a" 合法', M._is_hex('0d0a'))
ck('"0d 0a" 带空格合法', M._is_hex('0d 0a'))
ck('奇数长度非法', not M._is_hex('0d0'))
ck('"zz" 非法', not M._is_hex('zz'))

# ---------------------------------------------------------------- HTTP 请求
print('\n== 2. _struct HTTP 请求 ==')
req = (b'GET /api/v1/x?b=1 HTTP/1.1\r\n'
       b'Host: example.com\r\n'
       b'User-Agent: probe\r\n'
       b'Content-Length: 4\r\n'
       b'\r\n'
       b'PING')
st = M._struct('tcp', 'c2s', req)
ck('app = HTTP', st['app'] == 'HTTP', st['app'])
f = find(st, '方法')
ck('方法字段正确且字节对得上',
   f and req[f['off']:f['off'] + f['len']] == b'GET', f)
f = find(st, '请求目标')
ck('请求目标字节对得上',
   f and req[f['off']:f['off'] + f['len']] == b'/api/v1/x?b=1', f)
f = find(st, 'Host')
ck('Host 名/值正确',
   f and f['value'] == 'example.com'
   and req[f['off']:f['off'] + f['len']] == b'Host', f)
f = find(st, 'Body')
ck('Body 偏移指向载荷体',
   f and req[f['off']:f['off'] + f['len']] == b'PING', f)
ck('层包含请求行/头部/消息体',
   [l['name'] for l in st['layers']][:1] == ['HTTP 请求行']
   and any('头部' in l['name'] for l in st['layers'])
   and any(l['name'] == '消息体' for l in st['layers']), st['layers'])
bounds_ok(req, st, 'HTTP 请求')

# ---------------------------------------------------------------- HTTP 响应
print('\n== 3. _struct HTTP 响应 ==')
resp = b'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nnope'
st = M._struct('tcp', 's2c', resp)
ck('app = HTTP', st['app'] == 'HTTP', st['app'])
f = find(st, '状态码')
ck('状态码 = 403', f and f['value'] == '403', f)
ck('原因短语 = Forbidden', (find(st, '原因短语') or {}).get('value') == 'Forbidden')
bounds_ok(resp, st, 'HTTP 响应')

# ---------------------------------------------------------------- TLS
print('\n== 4. _struct TLS ClientHello ==')


def build_client_hello(host='test.example.org'):
    name = host.encode()
    sni = b'\x00\x00' + struct.pack('!H', len(name) + 5) \
        + struct.pack('!H', len(name) + 3) + b'\x00' + struct.pack('!H', len(name)) + name
    ext = sni + b'\x00\x2b' + struct.pack('!H', 3) + b'\x02\x03\x04'
    body = (b'\x03\x03' + b'\x11' * 32 + b'\x00'
            + struct.pack('!H', 2) + b'\x13\x01'
            + b'\x01\x00'
            + struct.pack('!H', len(ext)) + ext)
    hs = b'\x01' + len(body).to_bytes(3, 'big') + body
    return b'\x16\x03\x01' + struct.pack('!H', len(hs)) + hs


ch = build_client_hello()
st = M._struct('tcp', 'c2s', ch)
ck('app = TLS', st['app'] == 'TLS', st['app'])
ck('握手类型 = ClientHello',
   (find(st, '握手类型') or {}).get('value') == 'ClientHello')
ck('记录类型 = Handshake', (find(st, '记录类型') or {}).get('value') == 'Handshake')
exts = [f['name'] for f in st['fields'] if f['name'].startswith('扩展')]
ck('列出 2 个扩展（server_name / supported_versions）',
   len(exts) == 2 and '0x0000' in exts[0], exts)
ck('SNI 能从字节里抠出来', M._tls_sni(ch) == 'test.example.org', M._tls_sni(ch))
bounds_ok(ch, st, 'TLS')

# ---------------------------------------------------------------- DNS
print('\n== 5. _struct DNS ==')
q = (b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
     b'\x07example\x03com\x00\x00\x01\x00\x01')
st = M._struct('udp', 'c2s', q)
ck('app = DNS', st['app'] == 'DNS', st['app'])
ck('QDCOUNT = 1', (find(st, 'QDCOUNT') or {}).get('value') == '1')
ck('问题名 = example.com', (find(st, 'Q1 名称') or {}).get('value') == 'example.com',
   find(st, 'Q1 名称'))
ck('问题类型 = A', (find(st, 'Q1 类型') or {}).get('value') == 'A')
bounds_ok(q, st, 'DNS 查询')

ans = (b'\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00'
       b'\x07example\x03com\x00\x00\x01\x00\x01'
       b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22')
st = M._struct('udp', 's2c', ans)
ck('ANCOUNT = 1', (find(st, 'ANCOUNT') or {}).get('value') == '1')
rec = find(st, 'A1 记录')
ck('应答含 A 93.184.216.34 与 TTL=60',
   rec and '93.184.216.34' in rec['value'] and 'TTL=60s' in rec['value'], rec)
bounds_ok(ans, st, 'DNS 应答')

# ---------------------------------------------------------------- SOCKS5 / MQTT / BIN
print('\n== 6. SOCKS5 / MQTT / BIN ==')
s5 = b'\x05\x01\x00\x01\x7f\x00\x00\x01\x1f\x90'
st = M._struct('tcp', 'c2s', s5)
ck('SOCKS5 CMD = CONNECT', (find(st, 'CMD/REP') or {}).get('value') == 'CONNECT')
ck('SOCKS5 目标 127.0.0.1:8080',
   (find(st, 'DST.ADDR') or {}).get('value') == '127.0.0.1'
   and (find(st, 'DST.PORT') or {}).get('value') == '8080')
bounds_ok(s5, st, 'SOCKS5')

mq = b'\x30\x0c\x00\x04test\x00\x01hi'
st = M._struct('tcp', 'c2s', mq)
ck('MQTT 类型 = PUBLISH', (find(st, '报文类型') or {}).get('value') == 'PUBLISH')
ck('MQTT 剩余长度 = 12', (find(st, '剩余长度') or {}).get('value', '').startswith('12'))
bounds_ok(mq, st, 'MQTT')

binr = bytes(range(40))
st = M._struct('tcp', 'c2s', binr)
ck('认不出 -> BIN 且只给一条记录',
   st['app'] == 'BIN' and len(st['fields']) == 1, st['app'])
bounds_ok(binr, st, 'BIN')

ck('空载荷不炸', M._struct('tcp', 'c2s', b'')['fields'] == [])

# ---------------------------------------------------------------- 改写规则
print('\n== 7. 改写规则 ==')
eng = M.WpeEngine(maxlen=50)
r = eng.set_rewrite(on=True, rules=[{'act': 'find_replace', 'find': '50494e47',
                                     'replace': '504f4e47', 'target': 'x'}])
ck('合法规则被接受', r['ok'] and len(r['rules']) == 1, r)

pkt = {'id': 1, 'dir': 'c2s', 'proto': 'tcp', 'target': 'x:1', 'hex': '50494e47',
       'len': 4, 'app': 'BIN', 'label': ''}
nd, act = eng.apply_rewrite(pkt, b'PING')
ck('命中 find/replace 并返回 modify', nd == b'PONG' and act == 'modify', (nd, act))
ck('规则命中计数 +1', eng.rw_hits.get(1) == 1, eng.rw_hits)
ck('pkt 打上 wpe_rule 标记', pkt.get('wpe_rule') == 1, pkt.get('wpe_rule'))

pkt2 = {'id': 2, 'dir': 'c2s', 'proto': 'tcp', 'target': 'y:1', 'hex': '41',
        'len': 1, 'app': 'BIN', 'label': ''}
nd, act = eng.apply_rewrite(pkt2, b'ZZZZ')
ck('目标不匹配 -> 原样转发', nd == b'ZZZZ' and act == 'forward', (nd, act))

# proto 容错：传输层记的是 tcp，规则里写的是内容协议名 http —— 必须也能命中，
# 否则界面上按「HTTP」过滤永远 0 命中，会被当成改写坏了。
eng.set_rewrite(rules=[{'act': 'find_replace', 'find': '50494e47',
                        'replace': '504f4e47', 'proto': 'http'}])
pkta = {'id': 9, 'dir': 'c2s', 'proto': 'tcp', 'target': 'x:1', 'hex': '50494e47',
        'len': 4, 'app': 'HTTP', 'label': ''}
nd, act = eng.apply_rewrite(pkta, b'PING')
ck('proto=http 命中 app=HTTP 的 tcp 包', nd == b'PONG' and act == 'modify', (nd, act))
pktb = dict(pkta, id=10, app='BIN')
nd, act = eng.apply_rewrite(pktb, b'PING')
ck('proto=http 不命中 app=BIN', nd == b'PING' and act == 'forward', (nd, act))

eng.set_rewrite(rules=[{'act': 'set_hex', 'set_hex': 'aa bb cc', 'target': 'x'}])
pkt3 = {'id': 3, 'dir': 'c2s', 'proto': 'tcp', 'target': 'x:1', 'hex': '00',
        'len': 1, 'app': 'BIN', 'label': ''}
nd, act = eng.apply_rewrite(pkt3, b'\x00')
ck('set_hex 整包替换', nd == b'\xaa\xbb\xcc' and act == 'modify', (nd, act))

# 放在最后 —— 这一句会把 rw_rules 整体换成空列表，
# 放前面会把上面所有 apply_rewrite 断言全部打成 forward。
r = eng.set_rewrite(on=True, rules=[{'act': 'find_replace', 'find': '41'}])
ck('无条件的规则被拒收', len(r['rules']) == 0, r)
ck('拒收时给出原因', bool((r.get('rejected') or [{}])[0].get('reason')), r.get('rejected'))

# 拒收后 id 必须连续重排，否则前端「命中」列按 id 取数会串行。
r = eng.set_rewrite(on=True, rules=[
    {'act': 'find_replace', 'find': '41'},                    # 拒：无条件
    {'act': 'find_replace', 'find': '4242', 'dir': 'c2s'},    # 收
    {'act': 'set_hex', 'set_hex': 'zz', 'dir': 'c2s'},        # 拒：hex 非法
    {'act': 'find_replace', 'find': '43', 'dir': 'c2s'},      # 收
    {'act': 'set_hex', 'set_hex': 'aabb', 'dir': 's2c'},      # 收
])
ck('收 3 拒 2', len(r['rules']) == 3 and len(r['rejected']) == 2, r)
ck('id 连续重排 1..3', [x['id'] for x in r['rules']] == [1, 2, 3], [x['id'] for x in r['rules']])
ck('拒收原因带下标', sorted(x['i'] for x in r['rejected']) == [0, 2], r['rejected'])

# 前端下拉列表装不下的取值（比如后端存的 proto=http）必须能原样保留 ——
# 界面上渲染成「不限」再保存，就等于把条件静默删掉，规则会被当无条件拒收。
r = eng.set_rewrite(on=True, rules=[{'act': 'find_replace', 'find': '41',
                                     'proto': 'http'}])
ck('proto=http 这类内容协议名算有效条件', len(r['rules']) == 1, r)

eng.set_rewrite(on=False)
nd, act = eng.apply_rewrite(pkt3, b'\x00')
ck('关闭后不改写', nd == b'\x00' and act == 'forward', (nd, act))

# ---------------------------------------------------------------- 列表过滤
# 列表过滤和断点/改写走的是**两套代码**（list() vs _match()）。同一个 proto 字段
# 在两处行为不一致，是最容易把人带沟里的那种 bug —— 改写里 proto=http 能命中，
# 列表里按 HTTP 筛却是 0 条。所以这里必须单独钉一遍。
lst = M.WpeEngine(maxlen=50)
lst.record('c2s', 'tcp', '10.0.0.1:5', 'a.com:80', b'GET / HTTP/1.1\r\n\r\n')
lst.record('s2c', 'tcp', 'a.com:80', '10.0.0.1:5', b'HTTP/1.1 200 OK\r\n\r\n')
lst.record('c2s', 'udp', '10.0.0.1:5', '8.8.8.8:53',
           b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00' + b'\x03www\x07example\x03com\x00\x00\x01\x00\x01')
lst.record('c2s', 'tcp', '10.0.0.1:5', 'b.com:443', b'\x16\x03\x01\x00\x50' + b'\x00' * 80)

ck('list 无过滤全出', len(lst.list()['packets']) == 4, len(lst.list()['packets']))
ck('list proto=tcp 只出 tcp', [p['id'] for p in lst.list(proto='tcp')['packets']] == [4, 2, 1],
   [p['id'] for p in lst.list(proto='tcp')['packets']])
# 关键：proto=http 必须按内容分类命中，而不是按传输层
ck('list proto=http 命中内容层', [p['id'] for p in lst.list(proto='http')['packets']] == [2, 1],
   [p['id'] for p in lst.list(proto='http')['packets']])
ck('list proto=dns 命中 DNS', [p['id'] for p in lst.list(proto='dns')['packets']] == [3],
   [p['id'] for p in lst.list(proto='dns')['packets']])
# 长度范围：以前根本没有这个筛法
ids = lambda **kw: [p['id'] for p in lst.list(**kw)['packets']]
ck('list min_len=30 只出大包', all(p >= 30 for p in
   [len(bytes.fromhex(x['hex'])) for x in lst.list(min_len=30)['packets']]),
   [x['id'] for x in lst.list(min_len=30)['packets']])
ck('list max_len=20 只出小包', all(p <= 20 for p in
   [len(bytes.fromhex(x['hex'])) for x in lst.list(max_len=20)['packets']]),
   [x['id'] for x in lst.list(max_len=20)['packets']])
ck('list min+max 区间', len(lst.list(min_len=18, max_len=40)['packets']) >= 1,
   [x['id'] for x in lst.list(min_len=18, max_len=40)['packets']])
ck('list 长度条件与 app 叠加', [p['id'] for p in
   lst.list(proto='http', min_len=100)['packets']] == [],
   [p['id'] for p in lst.list(proto='http', min_len=100)['packets']])
ck('list 回显生效条件', (lst.list(min_len=5, max_len=9, proto='tcp')['filter'] or {})
   .get('min_len') == 5, lst.list(min_len=5, max_len=9, proto='tcp')['filter'])
ck('list 空条件回显为 0 而不是 None',
   lst.list()['filter']['min_len'] == 0 and lst.list()['filter']['max_len'] == 0,
   lst.list()['filter'])

# 断点 GET 必须只读：探测一次不能把断点关掉
lst.set_hold(True, rule={'dir': 'c2s'})
ck('set_hold 保留 app/max_len 字段',
   lst.set_hold(True, rule={'app': 'HTTP', 'max_len': 99})['rule'] ==
   {'app': 'HTTP', 'max_len': 99}, lst.hold_rule)
lst.set_hold(False, rule={})

# ---------------------------------------------------------------- 导出完整性
# 用户报的「无法导出完整 hex」根因就在这里：内存回传那条路有字节上限，
# 缓冲抓满时只能导出一小部分（实测 514/4000）。所以要有两件事：
#   ① 分页续导（next_since），让「没导完」变成可续，而不是一句抱怨；
#   ② 落盘导出（export_to_file），不受内存上限约束，一次拿全。
exp = M.WpeEngine(maxlen=50)
for i in range(12):
    exp.record('c2s', 'tcp', '10.0.0.1:5', 'x.com:80', b'GET / HTTP/1.1\r\n\r\n' + b'A' * 200)

r = exp.export_json()
ck('小缓冲导出 complete', r['complete'] is True, (r['exported'], r['candidates']))
ck('complete 时 next_since 为 None', r['next_since'] is None, r['next_since'])
ck('导出条数 == 候选数', r['exported'] == r['candidates'] == 12, (r['exported'], r['candidates']))

# 把字节上限压到很小，强制触发「导不完」——这正是原来线上发生的事
r1 = exp.export_json(max_hex=1000)
ck('字节上限触发 stopped_by=bytes', r1['stopped_by'] == 'bytes', r1['stopped_by'])
ck('触发上限时 complete=False', r1['complete'] is False, r1['complete'])
ck('触发上限时给出 next_since', isinstance(r1['next_since'], int), r1['next_since'])
ck('导出条数少于候选数', r1['exported'] < r1['candidates'],
   (r1['exported'], r1['candidates']))

# 拿 next_since 续导，把剩下的接上 —— 两条合起来必须等于全部
r2 = exp.export_json(since=r1['next_since'], max_hex=1000)
ids1 = [p['id'] for p in r1['packets']]
ids2 = [p['id'] for p in r2['packets']]
ck('续导不重复', not (set(ids1) & set(ids2)), (ids1, ids2))
ck('续导从 next_since 之后开始', ids2 and ids2[0] > r1['next_since'], (r1['next_since'], ids2[:3]))
ck('两页首尾衔接', ids1 + ids2 == sorted(ids1 + ids2), (ids1, ids2))

# ★ 退化情形：字节预算比**单个包**还小。
#   原来这里会一条都不给、且 next_since=None —— 客户端拿不到数据又没有游标，
#   等于分页彻底卡死。修法是「每页至少出一条」的前进性保证。
#   这个用例必须留着：线上真有人把 max_hex 设小了就是这个下场。
r_deg = exp.export_json(max_hex=1)
ck('预算=1字节时仍至少给出 1 条', len(r_deg['packets']) >= 1, len(r_deg['packets']))
ck('预算=1字节时给出游标可续', isinstance(r_deg['next_since'], int), r_deg['next_since'])
ck('预算=1字节时 stopped_by=bytes', r_deg['stopped_by'] == 'bytes', r_deg['stopped_by'])
# 一路续导到底，证明这种退化预算也能把全部取完（不会死循环也不会漏）
_acc, _cur, _guard = [], 0, 0
while True:
    _r = exp.export_json(since=_cur, max_hex=1)
    _acc += [p['id'] for p in _r['packets']]
    if _r['complete'] or _r['next_since'] is None:
        break
    _cur = _r['next_since']
    _guard += 1
    if _guard > 100:
        break
ck('退化预算下也能逐页取完全部', sorted(_acc) == sorted(p['id'] for p in exp.packets),
   (len(_acc), len(exp.packets)))
ck('退化预算续导无重复', len(_acc) == len(set(_acc)), len(_acc))

# 落盘导出：不受内存上限约束，一次拿全
import tempfile as _tf, os as _os, json as _json
exp.EXPORT_DIR = _tf.mkdtemp(prefix='wpe-exp-')
f1 = exp.export_to_file(kind='json')
ck('落盘导出 ok', f1['ok'] is True, f1)
ck('落盘导出条数 == 缓冲全部', f1['exported'] == 12, (f1['exported'], f1['buffered']))
ck('落盘导出 complete=True', f1['complete'] is True, f1['complete'])
ck('落盘文件存在', _os.path.isfile(f1['path']), f1['path'])
ck('落盘大小与声明一致', _os.path.getsize(f1['path']) == f1['size'], f1['size'])
with open(f1['path'], encoding='utf-8') as fh:
    doc = _json.load(fh)
ck('落盘 JSON 可解析', isinstance(doc, dict) and doc.get('exported') == 12, type(doc))
ck('落盘 JSON 每条都带完整 hex',
   all(len(p['hex']) // 2 == p['len'] for p in doc['packets']),
   [(p['id'], p['len'], len(p['hex']) // 2) for p in doc['packets'] if len(p['hex']) // 2 != p['len']][:3])
ck('落盘 JSON 条数对', len(doc['packets']) == 12, len(doc['packets']))

# 落盘的文件必须能**导回去** —— 导得出导不回等于导出是假的
imp = M.WpeEngine(maxlen=50)
ir = imp.import_packets(doc['packets'])
ck('落盘存档可重新导入且 0 拒收',
   ir['imported'] == 12 and not ir.get('errors'), (ir.get('imported'), ir.get('errors')))

f2 = exp.export_to_file(kind='txt')
ck('落盘 TXT ok', f2['ok'] is True, f2)
with open(f2['path'], encoding='utf-8') as fh:
    txt = fh.read()
ck('TXT 含完整 hex（不是 512 预览）',
   len(txt) > 12 * 400, len(txt))
ck('TXT 声明为完整字节', 'hex 为完整字节' in txt, txt[:80])
ck('TXT 里 hex 行与包数一致',
   sum(1 for ln in txt.splitlines() if ln.startswith('  HEX  : ')) == 12,
   sum(1 for ln in txt.splitlines() if ln.startswith('  HEX  : ')))

print('\n==== %d PASS / %d FAIL ====' % (len(PASS), len(FAIL)))
if FAIL:
    print('FAILED: ' + ' | '.join(FAIL))
sys.exit(1 if FAIL else 0)
