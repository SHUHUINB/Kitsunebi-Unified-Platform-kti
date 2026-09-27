#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 真链路对账：新后端 /api/v1/wpe/*  vs  适配层 /wpe/*  逐字段比对。

判据是「忠实透传」：
  * 不重算 —— 账本数字必须与适配层一模一样
  * 不丢字段 —— 键集合必须一致
  * 不截断 —— 列表是 512 字符预览，/packets/{id} 必须给全量 hex
  * 不吞错 —— 上游说什么报什么

只读，不写任何东西。
"""
from __future__ import annotations

import os
import sys

import httpx

API = os.environ.get('PROBE_API', 'http://127.0.0.1:8902')
ADP = os.environ.get('PROBE_ADP', 'http://127.0.0.1:8893')
USER = os.environ.get('PROBE_USER', 'admin')
PWD = os.environ.get('PROBE_PWD', 'Admin-ChangeMe-2026')
ADP_USER = os.environ.get('PROBE_ADP_USER', 'admin')
ADP_PASS = os.environ.get('PROBE_ADP_PASS', 'Adapter-Token-ChangeMe')

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('%-4s %-50s %s' % ('PASS' if cond else 'FAIL', name, detail))


def main() -> int:
    cli = httpx.Client(base_url=API, timeout=30.0, trust_env=False)
    adp = httpx.Client(base_url=ADP, timeout=30.0, trust_env=False,
                       auth=(ADP_USER, ADP_PASS))

    tok = (cli.post('/api/v1/auth/login',
                    json={'username': USER, 'password': PWD}).json()
           or {}).get('data', {}).get('token')
    if not tok:
        print('登录失败，后面全部无法验证')
        return 2
    H = {'Authorization': 'Bearer ' + tok}

    # ---- stat：账本逐字段 ------------------------------------------------ #
    ra = adp.get('/wpe/stat').json()
    rm = cli.get('/api/v1/wpe/stat', headers=H).json()
    check('stat 外层 ok 一致', ra.get('ok') == rm.get('ok'),
          'adp=%s mine=%s' % (ra.get('ok'), rm.get('ok')))
    a, b = ra.get('stat') or {}, rm.get('stat') or {}
    check('stat 键集合完全一致', set(a) == set(b),
          '缺=%s 多=%s' % (sorted(set(a) - set(b)), sorted(set(b) - set(a))))
    # uptime 是活的秒数，两次请求之间必然不同，单独排除。
    diff = sorted(k for k in a if k != 'uptime' and a.get(k) != b.get(k))
    check('stat 数值逐项一致（uptime 除外）', not diff, '不一致=%s' % diff)
    check('udp 账本逐项一致', a.get('udp') == b.get('udp'),
          'adp=%s mine=%s' % (a.get('udp'), b.get('udp')))

    # ---- list：条数与 id 序列 -------------------------------------------- #
    al = adp.get('/wpe/list', params={'limit': 2000}).json()
    ml = cli.get('/api/v1/wpe/packets', headers=H,
                 params={'limit': 2000}).json()
    ap, mp = al.get('packets') or [], ml.get('packets') or []
    check('list 条数一致', len(ap) == len(mp),
          '适配层=%d 新后端=%d' % (len(ap), len(mp)))
    check('list id 序列完全一致',
          [x.get('id') for x in ap] == [x.get('id') for x in mp])
    check('list 条数与 stat.buffered 自洽',
          len(mp) == b.get('buffered'),
          'list=%d buffered=%s' % (len(mp), b.get('buffered')))
    check('list 首条逐字段一致', (ap or [None])[0] == (mp or [None])[0])

    # ---- get：完整 hex + struct ----------------------------------------- #
    if mp:
        pid = mp[0]['id']
        ga = adp.get('/wpe/get', params={'id': pid}).json()
        gm = cli.get('/api/v1/wpe/packets/%d' % pid, headers=H).json()
        check('get 含 packet', isinstance(gm.get('packet'), dict))
        check('get 含 struct', gm.get('struct') is not None,
              type(gm.get('struct')).__name__)
        check('get packet 与适配层逐字段一致', ga.get('packet') == gm.get('packet'))
        check('get struct 与适配层一致', ga.get('struct') == gm.get('struct'))
        # 列表是「512 字符预览」：hex_full_len 记的是**截断前**的 hex 字符数
        # （不是字节数），hex_trunc 说明有没有被截。这两个字段是调用方判断
        # 「要不要再去 /packets/{id} 取全量」的唯一依据。
        lh = len(mp[0].get('hex') or '')
        check('列表 hex 不超 512（是预览不是全量）', lh <= 512, 'len=%d' % lh)
        check('列表给了 hex_full_len / hex_trunc',
              isinstance(mp[0].get('hex_full_len'), int)
              and isinstance(mp[0].get('hex_trunc'), bool),
              'full_len=%s trunc=%s' % (mp[0].get('hex_full_len'),
                                        mp[0].get('hex_trunc')))
        check('hex_trunc 与 hex_full_len 自洽',
              mp[0].get('hex_trunc') == (mp[0].get('hex_full_len') > 512),
              'full_len=%s trunc=%s' % (mp[0].get('hex_full_len'),
                                        mp[0].get('hex_trunc')))

        # ★「一个包都不放过」的硬判据：挑**最大**的那个包。
        #   它在列表里必然被截，但 /packets/{id} 必须给出全量 hex。
        big = max(mp, key=lambda x: x.get('len') or 0)
        blen = big.get('len') or 0
        gb = cli.get('/api/v1/wpe/packets/%d' % big['id'], headers=H).json()
        bh = (gb.get('packet') or {}).get('hex') or ''
        check('最大包在列表里确实被截（否则这条测不到东西）',
              big.get('hex_trunc') is True,
              'len=%d full_len=%s trunc=%s' % (blen, big.get('hex_full_len'),
                                               big.get('hex_trunc')))
        check('最大包 get 到全量 hex（2×字节数，一个字节不少）',
              len(bh) == 2 * blen, 'hex=%d 期望=%d' % (len(bh), 2 * blen))
        check('最大包 hex_full_len == 2×字节数',
              big.get('hex_full_len') == 2 * blen,
              'full_len=%s len=%d' % (big.get('hex_full_len'), blen))
        gba = adp.get('/wpe/get', params={'id': big['id']}).json()
        check('最大包 packet 与适配层逐字段一致',
              gba.get('packet') == gb.get('packet'))

    # ---- before 游标：加载更早 ------------------------------------------ #
    if len(mp) >= 3:
        # ★ 用**中间**的 id 当游标。拿最老的 id 会得到空集，而
        #   `all(...)` 对空集恒真 —— 那条「通过」其实什么都没验。
        mid = mp[len(mp) // 2]['id']
        r2 = cli.get('/api/v1/wpe/packets', headers=H,
                     params={'before': mid, 'limit': 5}).json()
        ids = [x.get('id') for x in (r2.get('packets') or [])]
        check('before 游标返回非空（否则这条是空跑）', bool(ids),
              'before=%d got=%d 条' % (mid, len(ids)))
        check('before 游标只看严格更早的包',
              all(i < mid for i in ids), 'before=%d got=%s' % (mid, ids))

    # ---- 过滤：dir / only_hold ------------------------------------------ #
    if mp:
        c2s_ids = {x['id'] for x in mp if x.get('dir') == 'c2s'}
        f = cli.get('/api/v1/wpe/packets', headers=H,
                    params={'dir': 'c2s', 'limit': 2000}).json()
        got = {x['id'] for x in (f.get('packets') or [])}
        check('dir=c2s 过滤返回非空（否则这条是空跑）', bool(got),
              'c2s=%d' % len(got))
        check('dir=c2s 与本地筛出的集合完全一致', got == c2s_ids,
              '对称差=%s' % sorted(got ^ c2s_ids)[:5])

        # 断点队列的唯一可信来源就是 only_hold —— 适配层不在封包上打标记。
        ha = adp.get('/wpe/list', params={'only_hold': 1, 'limit': 2000}).json()
        hm = cli.get('/api/v1/wpe/packets', headers=H,
                     params={'only_hold': True, 'limit': 2000}).json()
        ai = [x.get('id') for x in (ha.get('packets') or [])]
        mi = [x.get('id') for x in (hm.get('packets') or [])]
        check('only_hold 与适配层 id 序列一致（断点队列唯一可信来源）',
              ai == mi, 'adp=%s mine=%s' % (ai, mi))

    # ---- rewrite / hold：读态一致 --------------------------------------- #
    for path in ('/rewrite', '/hold'):
        rm2 = cli.get('/api/v1/wpe' + path, headers=H)
        ra2 = adp.get('/wpe' + path)
        same = rm2.status_code == 200 and rm2.json() == ra2.json()
        check('GET %s 与适配层一致' % path, same,
              'HTTP %d' % rm2.status_code if not same else '')

    # ---- exports 列表 ---------------------------------------------------- #
    e = cli.get('/api/v1/wpe/exports', headers=H)
    check('GET /exports -> 200', e.status_code == 200, 'HTTP %d' % e.status_code)

    print('\n%d PASS / %d FAIL' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
