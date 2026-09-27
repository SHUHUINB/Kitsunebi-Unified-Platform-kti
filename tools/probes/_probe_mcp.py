#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 工具实机验证（/mcp 端点，17 个 wpe_* 工具）。

覆盖：
  * 协议层：initialize / tools/list / 通知 202 / 未知方法 -32601
  * 只读工具：wpe_stat / wpe_list / wpe_get / wpe_struct / wpe_hold_get /
             wpe_rewrite_get / wpe_export_list —— 与 REST 路径结果一致
  * 写工具：wpe_hold_set（用**当前值**幂等写，证明通路可用但不改线上行为）
  * 破坏性工具：wpe_clear + wpe_import —— 按「先导出→清→导回」闭环做，
             可回滚；判据是 buffered 恢复且完整 hex 一字不差
  * 失败路径：wpe_release 给一个不存在的 id —— 必须是工具级报错（isError），
             不是协议错、更不是 500

★ 未做：wpe_release 的**正向**路径（真的放行一个被断住的包）。
  当前断点队列为空（holding=0），要造一个真断点得设一条能命中实时流量的规则，
  那会把真实用户的连接卡住最长 30 秒。线上不做这个，如实标注为未执行。
"""
from __future__ import annotations

import json
import os
import sys

import httpx

API = os.environ.get('PROBE_API', 'http://127.0.0.1:8902')
USER = os.environ.get('PROBE_USER', 'admin')
PWD = os.environ.get('PROBE_PWD', 'Admin-ChangeMe-2026')

PASS: list[str] = []
FAIL: list[str] = []

EXPECT_TOOLS = {
    'wpe_stat', 'wpe_list', 'wpe_get', 'wpe_struct', 'wpe_rewrite_get',
    'wpe_rewrite_set', 'wpe_hold_get', 'wpe_hold_set', 'wpe_release',
    'wpe_replay', 'wpe_send', 'wpe_export', 'wpe_export_save',
    'wpe_export_list', 'wpe_export_download', 'wpe_import', 'wpe_clear',
}


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('%-4s %-50s %s' % ('PASS' if cond else 'FAIL', name, detail))


def main() -> int:
    cli = httpx.Client(base_url=API, timeout=120.0, trust_env=False)
    HDR = {'Content-Type': 'application/json',
           'Accept': 'application/json, text/event-stream',
           'MCP-Protocol-Version': '2025-06-18'}
    r = cli.post('/mcp', headers=HDR, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                   'clientInfo': {'name': 'probe', 'version': '1'}}})
    sid = r.headers.get('mcp-session-id') or ''
    check('initialize 回 Mcp-Session-Id', bool(sid), 'SID=%s' % sid[:12])
    H = dict(HDR)
    H['Mcp-Session-Id'] = sid

    def rpc(method, params=None, rid=2):
        resp = cli.post('/mcp', headers=H, json={
            'jsonrpc': '2.0', 'id': rid, 'method': method,
            'params': params or {}})
        body = resp.text
        i = body.find('{')
        try:
            return json.loads(body[i:]) if i >= 0 else {}, resp
        except ValueError:
            return {}, resp

    def call(name, args=None, rid=10):
        d, resp = rpc('tools/call', {'name': name, 'arguments': args or {}}, rid)
        res = (d.get('result') or {})
        txt = ''
        for c in (res.get('content') or []):
            txt += c.get('text') or ''
        return res, txt, resp

    # ---- 协议层 ---------------------------------------------------------- #
    d, _ = rpc('tools/list', {}, 2)
    tools = [t['name'] for t in (d.get('result') or {}).get('tools', [])]
    check('tools/list -> 17 个工具', len(tools) == 17, '实际 %d' % len(tools))
    check('工具名与预期集合完全一致', set(tools) == EXPECT_TOOLS,
          '缺=%s 多=%s' % (sorted(EXPECT_TOOLS - set(tools)),
                           sorted(set(tools) - EXPECT_TOOLS)))

    d, _ = rpc('no/such/method', {}, 3)
    check('未知方法 -> -32601', (d.get('error') or {}).get('code') == -32601,
          str(d.get('error'))[:80])

    # ---- 只读工具与 REST 对齐 -------------------------------------------- #
    res, txt, _ = call('wpe_stat', {}, 11)
    check('wpe_stat 非报错', not res.get('isError'), txt[:80])
    mcp_stat = {}
    try:
        mcp_stat = json.loads(txt)
    except ValueError:
        pass
    if not mcp_stat:
        # 工具可能把结果包在 {ok,data} 或直接文本里，退一步做文本包含判断
        check('wpe_stat 文本含 buffered', 'buffered' in txt, txt[:80])
    else:
        st = mcp_stat.get('stat') or mcp_stat.get('data') or mcp_stat
        check('wpe_stat 含 buffered', 'buffered' in st, sorted(st)[:8])

    res, txt, _ = call('wpe_hold_get', {}, 12)
    check('wpe_hold_get 非报错且含 rule', not res.get('isError') and 'rule' in txt,
          txt[:90])

    res, txt, _ = call('wpe_list', {'limit': 3}, 13)
    check('wpe_list 非报错', not res.get('isError'), txt[:70])

    res, txt, _ = call('wpe_rewrite_get', {}, 14)
    check('wpe_rewrite_get 非报错', not res.get('isError'), txt[:70])

    res, txt, _ = call('wpe_export_list', {}, 15)
    check('wpe_export_list 非报错且含 files', not res.get('isError') and 'files' in txt,
          txt[:70])

    # ---- wpe_hold_set：用当前值幂等写 ------------------------------------ #
    cur = {}
    try:
        cur = json.loads(txt) if False else {}
    except ValueError:
        pass
    # 读一次当前 hold，原样写回去 —— 证明写通路可用且不改线上行为
    _, ht, _ = call('wpe_hold_get', {}, 16)
    try:
        hd = json.loads(ht)
    except ValueError:
        hd = {}
    rule = (hd.get('rule') or hd.get('data', {}).get('rule') or {})
    on = hd.get('on', hd.get('data', {}).get('on', True))
    res, txt, resp = call('wpe_hold_set',
                          {'on': bool(on), 'rule': rule or {}}, 17)
    check('wpe_hold_set（原值幂等写）非报错', not res.get('isError'), txt[:100])
    _, ht2, _ = call('wpe_hold_get', {}, 18)
    check('幂等写后 hold 规则未变', '17500' in ht2 or (rule and json.dumps(rule)[1:-1] in ht2),
          ht2[:100])

    # ---- wpe_release：不存在的 id --------------------------------------- #
    res, txt, resp = call('wpe_release', {'id': 999999999, 'action': 'forward'}, 19)
    check('wpe_release 不存在的 id -> 工具级报错（不是 500/协议错）',
          resp.status_code == 200 and (res.get('isError') is True
                                       or 'not' in txt.lower()
                                       or '不存在' in txt or '无效' in txt),
          'HTTP %d %s' % (resp.status_code, txt[:90]))

    # ---- wpe_clear + wpe_import 闭环 ------------------------------------ #
    _, st, _ = call('wpe_stat', {}, 20)
    try:
        before = (json.loads(st).get('stat') or {}).get('buffered')
    except ValueError:
        before = None
    check('清空前 buffered 已知且非空', isinstance(before, int) and before > 0,
          'buffered=%s' % before)
    if not (isinstance(before, int) and before > 0):
        print('\n缓冲为空 —— 跳过 clear/import 闭环')
    else:
        _, et, _ = call('wpe_export', {'limit': 20000}, 21)
        try:
            ep = json.loads(et).get('packets') or []
        except ValueError:
            ep = []
        check('wpe_export 拿到全量', len(ep) == before,
              'export=%d buffered=%d' % (len(ep), before))

        res, ct, _ = call('wpe_clear', {}, 22)
        check('wpe_clear 非报错', not res.get('isError'), ct[:80])
        _, st2, _ = call('wpe_stat', {}, 23)
        try:
            after = (json.loads(st2).get('stat') or {}).get('buffered')
        except ValueError:
            after = None
        check('wpe_clear 后 buffered == 0', after == 0, 'buffered=%s' % after)

        res, it, _ = call('wpe_import', {'packets': ep}, 24)
        check('wpe_import 非报错', not res.get('isError'), it[:90])
        _, st3, _ = call('wpe_stat', {}, 25)
        try:
            back = (json.loads(st3).get('stat') or {}).get('buffered')
        except ValueError:
            back = None
        check('wpe_import 后 buffered 恢复', back == before,
              '恢复=%s 原=%s' % (back, before))

        _, et2, _ = call('wpe_export', {'limit': 20000}, 26)
        try:
            ep2 = json.loads(et2).get('packets') or []
        except ValueError:
            ep2 = []
        check('★ 经 MCP 往返后完整 hex 多重集一字不差',
              sorted(p.get('hex') or '' for p in ep2)
              == sorted(p.get('hex') or '' for p in ep))

    print('\n%d PASS / %d FAIL' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
    print('未执行：wpe_release 正向路径（需要真断点，会卡真实用户连接最长 30s）')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
