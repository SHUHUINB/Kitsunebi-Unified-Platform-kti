"""MCP 端点 —— 把 WPE 封包编辑器的能力暴露给 agent。

两条铁律（旧管理台里写死的那两条，这里一条都没放松）：

  1. **不粉饰**。工具看到的结果必须和界面看到的结果一致 —— 上游非 200、
     参数缺失、结果被截断，都如实报（`ok=false` / `truncated=true`），
     绝不返回一个「看起来正常」的空结果。
  2. **不重复实现语义**。所有工具都翻译成一条适配层请求，转发出去，
     原样带回。平台侧不猜、不补、不美化。

路径保持 `/mcp`（不挂 `/api/v1` 前缀）—— 已经接进客户端的地址不能变。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..upstream import ccpx
from ..security import create_token

router = APIRouter(tags=['mcp'])

MCP_PROTOCOL = '2025-06-18'
# 客户端报上来的版本只要在这个表里就照它回，否则回我们自己最新的。
# 硬顶一个客户端不认识的值，往往会让它直接放弃连接。
MCP_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
MCP_SERVER_NAME = 'ccpx-wpe'
MCP_SERVER_VERSION = '1.0.0'
MCP_SESSION = secrets.token_hex(16)
MCP_MAX_BODY = 64 * 1024 * 1024          # 导入可能很大，与适配层上限对齐
MCP_TEXT_MAX = 1500000                   # 单次工具结果文本上限，超了如实标 truncated
# 大文件下载令牌的有效期（秒）。够人把地址粘进浏览器、也够 agent 立刻去取；
# 短到即使 URL 进了日志，能用的窗口也有限。
MCP_DOWNLOAD_TTL = 1800
MCP_SSE_MAX = 8                          # 同时在线的 SSE 流上限，防连接被占满
MCP_SSE_TTL = 900                        # 单条 SSE 最长存活 15 分钟，到点自断
MCP_SSE_OPEN = 0

MCP_INSTRUCTIONS = (
    '这是「WPE 封包编辑器」的 MCP 入口。抓包点在 CCProxy 适配层的转发路径上'
    '（TCP CONNECT 隧道 / 明文 HTTP / UDP SOCKS5 中继），**不注入任何目标进程**。\n'
    '典型用法：wpe_stat 看总览 → wpe_list 按条件筛包 → wpe_get 看单个包的完整 hex '
    '与分层结构 → wpe_hold_set 下断点后用 wpe_release 放行/丢弃/改写，或用 '
    'wpe_rewrite_set 挂自动改写规则 → wpe_replay / wpe_send 重放与构造发送 → '
    'wpe_export_save 落盘导出完整 hex。\n'
    '注意：环形缓冲上限 4000 条，wpe_list 单次上限 2000；要取更旧的用 before 游标'
    '（返回里的 oldest_id 就是下一页该传的 before），has_more=false 表示已到最旧。'
)

# 规则对象（断点规则 / 自动改写规则）的公共条件字段。
# 至少填一个条件 —— 无条件规则会命中每一个包，适配层会直接拒收。
MCP_RULE_PROPS = {
    'dir': {'type': 'string', 'enum': ['c2s', 's2c'],
            'description': '方向：c2s=客户端→目标，s2c=目标→客户端'},
    'proto': {'type': 'string',
              'description': '协议。传输层写 tcp/udp；内容层写 http/tls/dns 等'
                             '（适配层会同时比 proto 与 app，两种写法都认）'},
    'target': {'type': 'string', 'description': '目标包含的子串，如 m.baidu.com 或 :53'},
    'hex': {'type': 'string', 'description': '载荷十六进制包含的子串，如 474554'},
    'app': {'type': 'string', 'description': '应用层分类，如 HTTP / TLS / DNS'},
    'min_len': {'type': 'integer', 'description': '载荷最小长度'},
    'max_len': {'type': 'integer', 'description': '载荷最大长度'},
}


def _q(pairs) -> str:
    """把 (键, 值) 拼成 query。None / 空串跳过，布尔转 1/0。"""
    out = []
    for k, v in pairs:
        if v is None or v == '':
            continue
        if isinstance(v, bool):
            v = '1' if v else '0'
        out.append('%s=%s' % (k, quote(str(v), safe='')))
    return '&'.join(out)


def _need(args: dict, key: str, kind=int):
    """取必填参数。缺了或类型不对就抛 ValueError —— 由调用方转成 ok=false。"""
    if key not in args or args[key] in (None, ''):
        raise ValueError('缺必填参数 %s' % key)
    try:
        return kind(args[key])
    except Exception:                              # noqa: BLE001
        raise ValueError('参数 %s 类型不对（应为 %s）：%r'
                         % (key, getattr(kind, '__name__', kind), args[key]))


# --------------------------------------------------------------------------- #
# 各工具 -> 适配层请求的映射
# 每个函数返回 (method, path, body, ctype)，由 _call_tool 统一发出去。
# --------------------------------------------------------------------------- #
def _t_stat(a):
    return 'GET', '/wpe/stat', None, None


def _t_list(a):
    q = _q([
        ('since', a.get('since')), ('before', a.get('before')),
        ('limit', a.get('limit')), ('dir', a.get('dir')),
        ('proto', a.get('proto')), ('target', a.get('target')),
        ('hex', a.get('hex')), ('label', a.get('label')),
        ('app', a.get('app')), ('min_len', a.get('min_len')),
        ('max_len', a.get('max_len')), ('only_hold', a.get('only_hold')),
    ])
    return 'GET', '/wpe/list?' + q, None, None


def _t_get(a):
    return 'GET', '/wpe/get?id=%d' % _need(a, 'id'), None, None


def _t_rewrite_get(a):
    return 'GET', '/wpe/rewrite', None, None


def _t_rewrite_set(a):
    rules = a.get('rules')
    if not isinstance(rules, list) or not rules:
        raise ValueError('rules 必须是非空数组（无条件规则会被适配层拒收，'
                         '至少给 dir/proto/target/hex/app/min_len/max_len 里的一项）')
    body = json.dumps({'rules': rules}, ensure_ascii=False).encode('utf-8')
    return 'POST', '/wpe/rewrite?' + _q([('on', a.get('on', True))]), \
        body, 'application/json'


def _t_hold_get(a):
    return 'GET', '/wpe/hold', None, None


def _t_hold_set(a):
    rule = a.get('rule')
    if rule is not None and not isinstance(rule, dict):
        raise ValueError('rule 必须是对象（空对象 {} = 拦下全部封包）')
    body = json.dumps({'rule': rule or {}}, ensure_ascii=False).encode('utf-8')
    return 'POST', '/wpe/hold?' + _q([('on', a.get('on', True))]), \
        body, 'application/json'


def _t_release(a):
    act = str(a.get('action') or 'forward').strip().lower()
    if act not in ('forward', 'drop', 'modify'):
        raise ValueError('action 只能是 forward / drop / modify')
    q = _q([('id', _need(a, 'id')), ('action', act), ('data', a.get('data'))])
    return 'GET', '/wpe/release?' + q, None, None


def _t_replay(a):
    q = _q([('id', _need(a, 'id')), ('data', a.get('data')),
            ('target', a.get('target')), ('proto', a.get('proto')),
            ('times', a.get('times'))])
    return 'GET', '/wpe/replay?' + q, None, None


def _t_send(a):
    q = _q([('proto', a.get('proto') or 'tcp'),
            ('target', _need(a, 'target', str)),
            ('data', _need(a, 'data', str))])
    return 'GET', '/wpe/send?' + q, None, None


def _t_export(a):
    q = _q([('limit', a.get('limit')), ('since', a.get('since')),
            ('max_hex', a.get('max_hex'))])
    return 'GET', '/wpe/export?' + q, None, None


def _t_export_save(a):
    kind = str(a.get('kind') or 'json').strip().lower()
    if kind not in ('json', 'txt'):
        raise ValueError('kind 只能是 json / txt')
    q = _q([('since', a.get('since')), ('kind', kind)])
    return 'POST', '/wpe/export/save?' + q, b'', None


def _t_export_list(a):
    return 'GET', '/wpe/export/list', None, None


def _t_export_download(a):
    name = _need(a, 'name', str)
    # 只允许纯文件名：放行路径分隔符就等于开了个任意文件读取口子。
    if os.path.basename(name) != name or '/' in name or '\\' in name:
        raise ValueError('name 必须是导出目录里的纯文件名，不能带路径')
    return 'GET', '/wpe/export/download?name=' + quote(name, safe=''), None, None


def _t_import(a):
    pk = a.get('packets')
    if not isinstance(pk, list) or not pk:
        raise ValueError('packets 必须是非空数组（元素形如 {"hex":"...","len":N}）')
    body = json.dumps({'packets': pk}, ensure_ascii=False).encode('utf-8')
    return 'POST', '/wpe/import', body, 'application/json'


def _t_clear(a):
    return 'GET', '/wpe/clear', None, None


MCP_TOOLS = [
    {
        'name': 'wpe_stat',
        'description': '抓包总览：已捕获/缓冲占用/容量/序号、断点状态与队列长度、'
                       '自动改写命中、重放与发送计数，以及 UDP 中继账本'
                       '（in_pkts/out_pkts/orphan/adopted/wild/routes/pending）。'
                       '排查「有没有漏包」先看这里 —— orphan 减去 adopted 就是'
                       '确实没转发出去的包数。',
        'inputSchema': {'type': 'object', 'properties': {},
                        'additionalProperties': False},
        'run': _t_stat,
    },
    {
        'name': 'wpe_list',
        'description': '列出封包（新→旧）。支持方向/协议/目标/HEX/应用层/长度区间/'
                       '断点队列等过滤，以及 since（看更新的）与 before（看更旧的）'
                       '两个游标。返回带 matched/scanned/buffered/limit/truncated '
                       '以及 oldest_id/newest_id/has_more —— 要取更旧的一页，'
                       '把 oldest_id 当 before 再调一次即可。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'since': {'type': 'integer', 'description': '只看 id 大于它的包'},
                'before': {'type': 'integer',
                           'description': '只看 id 严格小于它的包（翻旧页用）'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 2000,
                          'description': '单次条数，默认 300，服务端硬上限 2000'},
                'dir': {'type': 'string', 'enum': ['c2s', 's2c'],
                        'description': '方向'},
                'proto': {'type': 'string',
                          'description': '协议，传输层 tcp/udp 或内容层 http/tls/dns'},
                'target': {'type': 'string', 'description': '目标包含的子串'},
                'hex': {'type': 'string', 'description': '载荷 hex 包含的子串'},
                'label': {'type': 'string', 'description': '标签包含的子串'},
                'app': {'type': 'string', 'description': '应用层分类，如 HTTP/DNS'},
                'min_len': {'type': 'integer', 'description': '载荷最小长度'},
                'max_len': {'type': 'integer', 'description': '载荷最大长度'},
                'only_hold': {'type': 'boolean', 'description': '只看断点队列里的包'},
            },
            'additionalProperties': False,
        },
        'run': _t_list,
    },
    {
        'name': 'wpe_get',
        'description': '取单个封包的完整信息：元数据、完整 hex、ascii、是否被截断、'
                       '人工改写/规则改写的处置标记，以及**分层结构解析**'
                       '（struct：HTTP/TLS/DNS/SOCKS5/MQTT 的字段级 off/len）。'
                       '结构面板就是拿 off/len 去 hex 里高亮，偏移错了等于指错地方。',
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': '封包 id'}},
            'required': ['id'], 'additionalProperties': False,
        },
        'run': _t_get,
    },
    {
        'name': 'wpe_struct',
        'description': '只看某个封包的分层结构解析（等同 wpe_get 的 struct 部分）。'
                       '想知道一个包里每一层、每个字段分别是什么，用这个。',
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': '封包 id'}},
            'required': ['id'], 'additionalProperties': False,
        },
        'run': _t_get,
    },
    {
        'name': 'wpe_rewrite_get',
        'description': '读当前的自动改写（过滤器改写）状态：是否开启、规则列表、'
                       '每条规则的命中次数。',
        'inputSchema': {'type': 'object', 'properties': {},
                        'additionalProperties': False},
        'run': _t_rewrite_get,
    },
    {
        'name': 'wpe_rewrite_set',
        'description': '写自动改写规则（整表覆盖）。每条规则形如 '
                       '{"act":"find_replace","find":"474554","replace":"48454c4c",'
                       '"target":"m.baidu.com"} 或 '
                       '{"act":"set_hex","set_hex":"...","proto":"http"}。'
                       '**至少填一个条件**（dir/proto/target/hex/app/min_len/max_len），'
                       '否则适配层会拒收 —— 无条件改写会改掉每一个包。'
                       '返回里的 rejected 会逐条给出被拒的规则和原因，别忽略它。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'on': {'type': 'boolean', 'description': '是否开启自动改写，默认 true'},
                'rules': {
                    'type': 'array',
                    'description': '规则数组（整表覆盖，不是追加）',
                    'items': {
                        'type': 'object',
                        'properties': dict(MCP_RULE_PROPS, **{
                            'act': {'type': 'string',
                                    'enum': ['find_replace', 'set_hex'],
                                    'description': 'find_replace=字节替换（默认），'
                                                   'set_hex=整包替换'},
                            'find': {'type': 'string', 'description': '要替换的 hex'},
                            'replace': {'type': 'string', 'description': '替换成什么 hex，'
                                                                        '留空=删除'},
                            'set_hex': {'type': 'string',
                                        'description': '整包替换成这个 hex'},
                        }),
                    },
                },
            },
            'required': ['rules'], 'additionalProperties': False,
        },
        'run': _t_rewrite_set,
    },
    {
        'name': 'wpe_hold_get',
        'description': '读断点（Hold）状态：是否开启、当前匹配规则、队列里积压几个包、'
                       '断点超时秒数。',
        'inputSchema': {'type': 'object', 'properties': {},
                        'additionalProperties': False},
        'run': _t_hold_get,
    },
    {
        'name': 'wpe_hold_set',
        'description': '设置断点开关与匹配规则。规则为空对象 {} 表示**拦下全部封包**'
                       '（这是断点的语义：不填条件就是全拦）。命中的包会被挂住，'
                       '直到用 wpe_release 放行/丢弃/改写，或等到超时。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'on': {'type': 'boolean', 'description': '是否开启断点，默认 true'},
                'rule': {'type': 'object',
                         'description': '匹配规则，至少一个条件；空对象 = 全部拦',
                         'properties': MCP_RULE_PROPS},
            },
            'additionalProperties': False,
        },
        'run': _t_hold_set,
    },
    {
        'name': 'wpe_release',
        'description': '处置一个被断住的封包。action=forward 原样放行；drop 丢弃'
                       '（不会到达目标）；modify 用 data 里的新 hex 替换后放行。'
                       '被改过的包会在列表和详情里标成「人工改写」，与规则自动改写'
                       '分开显示 —— 出问题时要能查出是谁改的。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': '被封住的封包 id'},
                'action': {'type': 'string', 'enum': ['forward', 'drop', 'modify'],
                           'description': '默认 forward'},
                'data': {'type': 'string',
                         'description': 'action=modify 时的新载荷 hex'},
            },
            'required': ['id'], 'additionalProperties': False,
        },
        'run': _t_release,
    },
    {
        'name': 'wpe_replay',
        'description': '重放一个已抓到的封包（可选先改载荷、换目标、换协议、重复多次）。'
                       '不带 data 就是原样重放。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': '要重放的封包 id'},
                'data': {'type': 'string', 'description': '替换后的载荷 hex（可选）'},
                'target': {'type': 'string', 'description': '覆盖目标 host:port（可选）'},
                'proto': {'type': 'string', 'enum': ['tcp', 'udp'],
                          'description': '覆盖协议（可选）'},
                'times': {'type': 'integer', 'minimum': 1,
                          'description': '重复次数，默认 1'},
            },
            'required': ['id'], 'additionalProperties': False,
        },
        'run': _t_replay,
    },
    {
        'name': 'wpe_send',
        'description': '从零构造并发送一个封包（不依赖已有抓包）。proto=tcp 时按 '
                       'host:port 建连接发原始字节；proto=udp 时直接发数据报。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'proto': {'type': 'string', 'enum': ['tcp', 'udp'],
                          'description': '默认 tcp'},
                'target': {'type': 'string', 'description': '目标，形如 1.2.3.4:80'},
                'data': {'type': 'string', 'description': '要发送的载荷 hex'},
            },
            'required': ['target', 'data'], 'additionalProperties': False,
        },
        'run': _t_send,
    },
    {
        'name': 'wpe_export',
        'description': '导出完整 hex（不是列表里那个截断预览）。返回里带 '
                       'next_since 续导游标和「有没有导完」的标记；没导完就拿 '
                       'next_since 当 since 再调一次。要整包一次拿全请用 '
                       'wpe_export_save 落盘。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'description': '本次最多导出多少条'},
                'since': {'type': 'integer', 'description': '从哪个 id 之后开始'},
                'max_hex': {'type': 'integer', 'description': 'hex 总字节预算上限'},
            },
            'additionalProperties': False,
        },
        'run': _t_export,
    },
    {
        'name': 'wpe_export_save',
        'description': '把整个缓冲导出成文件落在服务器上（不受响应体积限制，'
                       '这是拿完整数据正路）。kind=json 或 txt。返回文件名，'
                       '之后可以用 wpe_export_download 取内容。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'since': {'type': 'integer', 'description': '从哪个 id 之后开始'},
                'kind': {'type': 'string', 'enum': ['json', 'txt'],
                         'description': '默认 json'},
            },
            'additionalProperties': False,
        },
        'run': _t_export_save,
    },
    {
        'name': 'wpe_export_list',
        'description': '列出服务器上已落盘的导出文件（文件名/大小/时间），'
                       '方便找回上一次导出。',
        'inputSchema': {'type': 'object', 'properties': {},
                        'additionalProperties': False},
        'run': _t_export_list,
    },
    {
        'name': 'wpe_export_download',
        'description': '读取某个已落盘导出文件的内容（文本形式返回，超大时如实标 '
                       'truncated）。name 只能是纯文件名，不能带路径。',
        'inputSchema': {
            'type': 'object',
            'properties': {'name': {'type': 'string',
                                    'description': 'wpe_export_list 里给出的文件名'}},
            'required': ['name'], 'additionalProperties': False,
        },
        'run': _t_export_download,
    },
    {
        'name': 'wpe_import',
        'description': '导入之前导出的封包数组，进环形缓冲。导入的包一律打 '
                       'imported 标记并写清来路（不是实时流量）。返回 imported / '
                       'skipped 与逐条跳过原因 —— 跳过绝不静默。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'packets': {
                    'type': 'array',
                    'description': '封包数组，元素形如 {"hex":"...","len":N}，'
                                   '可带 dir/proto/target/app 等元数据',
                    'items': {'type': 'object'},
                },
            },
            'required': ['packets'], 'additionalProperties': False,
        },
        'run': _t_import,
    },
    {
        'name': 'wpe_clear',
        'description': '清空封包环形缓冲。注意这只清缓冲，不动自动改写规则和断点设置。',
        'inputSchema': {'type': 'object', 'properties': {},
                        'additionalProperties': False},
        'run': _t_clear,
    },
]

MCP_TOOL_MAP = dict((t['name'], t) for t in MCP_TOOLS)


async def _call_tool(name: str, args, base: str = '') -> dict:
    """执行一个工具。返回可直接塞进 content 的结果对象。

    `base` 是本次请求的对外根地址（形如 `http://203.0.113.10:8890`），
    只用来把「下载地址」拼成**绝对 URL**。以前这里给的是相对路径
    `/api/v1/wpe/exports/x/download` —— MCP 客户端拿到手根本不知道该往哪发，
    而且那条路由要登录，客户端没有会话 cookie，照着取就是 401。
    """
    if not isinstance(args, dict):
        args = {}
    spec = MCP_TOOL_MAP.get(name)
    if spec is None:
        return {'ok': False,
                'err': '未知工具：%s（可用工具见 tools/list）' % name}
    try:
        method, path, body, ctype = spec['run'](args)
    except ValueError as exc:
        return {'ok': False, 'err': str(exc)}
    except Exception as exc:                       # noqa: BLE001
        return {'ok': False, 'err': '参数处理失败：%s' % exc}

    try:
        code, data = await ccpx.request(method, path, body or b'', ctype)
    except Exception as exc:                       # noqa: BLE001
        # 适配层没起来 / 连不上 —— 如实说是上游不可达，不给假结果。
        return {'ok': False, 'err': '适配层不可达：%s' % exc}

    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        # 导出产物理论上都是文本；真出现二进制就如实说明，
        # 不能 decode 成乱码当正常结果返回。
        return {'ok': False, 'http': code,
                'err': '上游返回的不是 UTF-8 文本（%d 字节）' % len(data)}
    nbytes = len(data)

    if code != 200:
        return {'ok': False, 'http': code, 'err': text[:2000]}

    try:
        obj = json.loads(text)
    except Exception:                              # noqa: BLE001
        obj = None

    if nbytes > MCP_TEXT_MAX:
        if name == 'wpe_export_download':
            # 导出文件本来就常常超过工具结果上限（落盘导出能到几十 MB）。
            # 整个塞进上下文没有意义 —— 给一段预览 + 可直接下载的地址，
            # 比返回半截 JSON（还劝人「再落盘一次」）有用得多。
            #
            # ★ 地址必须是**可用的**，否则等于没给：
            #   ① 绝对 URL —— MCP 客户端不知道本服务的 host，相对路径无从下手；
            #   ② 带一次性令牌 —— `/wpe/exports/{name}/download` 是登录保护的，
            #      而 /mcp 本身不鉴权，客户端手里没有会话 cookie。
            #      令牌用与登录同一个密钥签，带 scope + 文件名 + TTL，
            #      改一个字节就失效，泄漏窗口有限（默认 30 分钟）。
            dn = str(args.get('name') or '')
            tok = create_token('mcp-download',
                               extra={'scope': 'wpe:download', 'name': dn},
                               ttl=MCP_DOWNLOAD_TTL)
            dl = ('%s/api/v1/wpe/exports/%s/download?t=%s'
                  % (base.rstrip('/'), quote(dn, safe=''), quote(tok, safe='')))
            return {'ok': True, 'http': code, 'truncated': True, 'bytes': nbytes,
                    'name': dn,
                    'download_url': dl,
                    'note': '文件 %d 字节超过单次结果上限 %d，这里只回预览。'
                            '完整文件用 download_url 直接下载（地址自带令牌，'
                            '%d 分钟内有效，无需登录）；'
                            '只要其中一部分就用 wpe_export 带 since/limit 分页取。'
                            % (nbytes, MCP_TEXT_MAX, MCP_DOWNLOAD_TTL),
                    'head': text[:MCP_TEXT_MAX]}
        # 太大就不整个塞进工具结果 —— 否则一次列表就能把 agent 的上下文刷爆。
        # 给一段前缀 + 明确的下一步，而不是悄悄截断装作完整。
        return {'ok': True, 'http': code, 'truncated': True, 'bytes': nbytes,
                'note': '结果 %d 字节超过上限 %d，已截断。要完整数据请用 '
                        'wpe_export_save 落盘，或调小 limit / 用 before 翻页。'
                        % (nbytes, MCP_TEXT_MAX),
                'head': text[:MCP_TEXT_MAX]}
    if obj is None:
        # 非 JSON 响应 —— 目前只有 wpe_export_download 会走到这里（文件原文）。
        return {'ok': True, 'http': code, 'raw_text': text, 'bytes': nbytes}
    return obj


def _ok(mid, result):
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def _err(mid, code, message, data=None):
    e = {'code': code, 'message': message}
    if data is not None:
        e['data'] = data
    return {'jsonrpc': '2.0', 'id': mid, 'error': e}


async def _handle_one(msg, base: str = ''):
    """处理一条 JSON-RPC 消息。返回响应 dict；通知类消息返回 None（要回 202 空体）。

    `base` 透传给 `_call_tool`，用于拼绝对下载地址（见那里的注释）。
    """
    if not isinstance(msg, dict):
        return _err(None, -32600, 'Invalid Request：消息不是对象')
    mid = msg.get('id')
    method = msg.get('method')
    params = msg.get('params')
    if not isinstance(params, dict):
        params = {}
    if not isinstance(method, str) or not method:
        return _err(mid, -32600, 'Invalid Request：缺 method')

    # 通知一律不回包。规范要求回 202 空体，回了 JSON 客户端会当成多余响应。
    if mid is None and (method.startswith('notifications/') or method == 'initialized'):
        return None

    if method == 'initialize':
        want = params.get('protocolVersion')
        pv = want if want in MCP_PROTOCOLS else MCP_PROTOCOL
        return _ok(mid, {
            'protocolVersion': pv,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': MCP_SERVER_NAME, 'version': MCP_SERVER_VERSION},
            'instructions': MCP_INSTRUCTIONS,
        })

    if method == 'ping':
        return _ok(mid, {})

    if method == 'tools/list':
        return _ok(mid, {'tools': [
            {'name': t['name'], 'description': t['description'],
             'inputSchema': t['inputSchema']} for t in MCP_TOOLS]})

    if method == 'tools/call':
        args = params.get('arguments')
        res = await _call_tool(params.get('name') or '',
                               args if isinstance(args, dict) else {},
                               base=base)
        return _ok(mid, {
            'content': [{'type': 'text',
                         'text': json.dumps(res, ensure_ascii=False, indent=2)}],
            # 工具执行失败走 isError，不走 JSON-RPC error ——
            # 后者是协议层错误，客户端会当成连接出了问题。
            'isError': not bool(res.get('ok')),
        })

    # 没实现的能力明确回空列表，而不是 Method not found：
    # 有些客户端会无条件探测这几个，报错会让它认为服务端不完整。
    if method == 'resources/list':
        return _ok(mid, {'resources': []})
    if method == 'resources/templates/list':
        return _ok(mid, {'resourceTemplates': []})
    if method == 'prompts/list':
        return _ok(mid, {'prompts': []})
    if method == 'logging/setLevel':
        return _ok(mid, {})

    return _err(mid, -32601, 'Method not found: %s' % method)


def _reply(payload, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status,
                        headers={'Mcp-Session-Id': MCP_SESSION})


def _accepted() -> Response:
    """通知的应答：202 空体。"""
    return Response(status_code=202, media_type='text/plain; charset=utf-8',
                    headers={'Mcp-Session-Id': MCP_SESSION})


@router.post('/mcp')
async def mcp_post(request: Request):
    raw = await request.body()
    if not raw:
        return _reply({'jsonrpc': '2.0', 'id': None,
                       'error': {'code': -32700, 'message': 'Parse error：请求体为空'}}, 400)
    if len(raw) > MCP_MAX_BODY:
        return _reply({'jsonrpc': '2.0', 'id': None,
                       'error': {'code': -32600,
                                 'message': '请求体 %d 字节超过上限 %d'
                                            % (len(raw), MCP_MAX_BODY)}}, 413)
    try:
        msg = json.loads(raw.decode('utf-8'))
    except Exception as exc:                       # noqa: BLE001
        return _reply({'jsonrpc': '2.0', 'id': None,
                       'error': {'code': -32700, 'message': 'Parse error：%s' % exc}}, 400)

    # 对外根地址：用于把下载地址拼成绝对 URL。取 Host 头而不是写死 ——
    # 平台可能被反代、也可能直连，写死一个等于换个入口就失效。
    base = str(request.base_url).rstrip('/')

    # JSON-RPC 批处理：全部是通知时也要回 202 空体，不能回一个空数组。
    if isinstance(msg, list):
        outs = []
        for m in msg:
            r = await _handle_one(m, base=base)
            if r is not None:
                outs.append(r)
        if not outs:
            return _accepted()
        return _reply(outs)
    out = await _handle_one(msg, base=base)
    if out is None:
        return _accepted()
    return _reply(out)


async def _sse_stream():
    """SSE 长连接：只发心跳，不推业务事件。

    本实现是无状态的（每次 POST 独立处理），SSE 存在的意义只是让
    只支持 SSE 的客户端连得上。到点自断，不指望客户端善后。
    """
    global MCP_SSE_OPEN
    try:
        yield ': connected\n\n'
        waited = 0
        while waited < MCP_SSE_TTL:
            await asyncio.sleep(15)
            waited += 15
            yield ': keepalive %d\n\n' % waited
    except asyncio.CancelledError:
        raise
    finally:
        MCP_SSE_OPEN -= 1


@router.get('/mcp')
async def mcp_get(request: Request):
    global MCP_SSE_OPEN
    accept = request.headers.get('accept') or ''
    if 'text/event-stream' not in accept:
        return Response(
            status_code=405,
            content='MCP 端点用 POST 发 JSON-RPC；要开 SSE 长连接请带 '
                    'Accept: text/event-stream。\n',
            media_type='text/plain; charset=utf-8',
            headers={'Allow': 'POST, GET, DELETE', 'Mcp-Session-Id': MCP_SESSION})
    if MCP_SSE_OPEN >= MCP_SSE_MAX:
        return Response(status_code=503,
                        content='MCP SSE 连接数已达上限 %d\n' % MCP_SSE_MAX,
                        media_type='text/plain; charset=utf-8',
                        headers={'Mcp-Session-Id': MCP_SESSION})
    MCP_SSE_OPEN += 1
    return StreamingResponse(
        _sse_stream(), media_type='text/event-stream; charset=utf-8',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive',
                 'Mcp-Session-Id': MCP_SESSION})


@router.delete('/mcp')
async def mcp_delete():
    """会话终止。本实现是无状态的，回 204 即可。"""
    return Response(status_code=204, media_type='text/plain; charset=utf-8',
                    headers={'Mcp-Session-Id': MCP_SESSION})
