#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export -> clear -> import 零丢失闭环（对线上适配层，可回滚）。

为什么要按这个顺序：
  `/wpe/clear` 清的是**实时环形缓冲**。缓冲里的包如果没导出，清掉就没了。
  所以顺序必须是「先落盘 → 校验文件非空 → 内存导出取全量 → 清 → 验 0 → 导回」。
  安全网（落盘文件）没就位就一步都不许往下走。

三条判据（都是「不骗人」的直接推论）：
  1. 清空前缓冲必须非空，否则这条测试什么都没验。
  2. 导回后条数必须恢复；`skipped` 必须为 0 ——
     适配层会拒收「len 与 hex 字节数不一致」的包，所以 skipped==0 反过来
     证明了**导出的是完整 hex，不是列表里那 512 字符预览**。
  3. 导回的包必须被标记成 imported + 写明「非实时流量」，
     不能混进列表冒充真抓到的包。
"""
from __future__ import annotations

import os
import sys

import httpx

API = os.environ.get('PROBE_API', 'http://127.0.0.1:8902')
USER = os.environ.get('PROBE_USER', 'admin')
PWD = os.environ.get('PROBE_PWD', 'Admin-ChangeMe-2026')

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('%-4s %-50s %s' % ('PASS' if cond else 'FAIL', name, detail))


def main() -> int:
    cli = httpx.Client(base_url=API, timeout=120.0, trust_env=False)
    tok = (cli.post('/api/v1/auth/login',
                    json={'username': USER, 'password': PWD}).json()
           or {}).get('data', {}).get('token')
    if not tok:
        print('登录失败')
        return 2
    H = {'Authorization': 'Bearer ' + tok}

    def stat() -> dict:
        return (cli.get('/api/v1/wpe/stat', headers=H).json() or {}).get('stat') or {}

    def packets(params=None) -> list:
        return (cli.get('/api/v1/wpe/packets', headers=H,
                        params=params or {'limit': 2000}).json()
                or {}).get('packets') or []

    s0 = stat()
    before = s0.get('buffered')
    check('清空前缓冲非空（否则这条测不到东西）',
          isinstance(before, int) and before > 0, 'buffered=%s' % before)
    if not (isinstance(before, int) and before > 0):
        print('\n缓冲是空的 —— 到此为止，不做 clear')
        return 1
    old_ids = {x['id'] for x in packets()}

    # ---- 1) 落盘导出（安全网） ------------------------------------------ #
    e = cli.post('/api/v1/wpe/exports', headers=H, params={'kind': 'json'})
    check('POST /exports 落盘成功', e.status_code in (200, 201),
          'HTTP %d %s' % (e.status_code, e.text[:110]))
    files = (cli.get('/api/v1/wpe/exports', headers=H).json() or {}).get('files') or []
    newest = max(files, key=lambda f: f.get('mtime') or 0) if files else {}
    check('落盘文件出现在 export/list 且非空',
          (newest.get('size') or 0) > 0,
          'name=%s size=%s' % (newest.get('name'), newest.get('size')))
    if not ((newest.get('size') or 0) > 0):
        print('\n安全网没就位 —— 到此为止，不做 clear')
        return 1

    # ---- 2) 内存导出取全量（导入要用完整 hex） -------------------------- #
    got: list = []
    since = 0
    for _ in range(20):
        r = cli.get('/api/v1/wpe/export', headers=H,
                    params={'since': since, 'limit': 20000}).json()
        got.extend(r.get('packets') or [])
        if not r.get('next_since'):
            break
        since = r['next_since']
    check('内存导出条数与缓冲一致', len(got) == before,
          'export=%d buffered=%d' % (len(got), before))
    nohex = sum(1 for p in got if not p.get('hex'))
    check('导出的包都带 hex', nohex == 0, '缺 hex=%d' % nohex)
    mismatch = [p['id'] for p in got if p.get('len') != len(p.get('hex') or '') // 2]
    check('导出 hex 长度 == len 字节数（不是预览）', not mismatch,
          '不一致=%s' % mismatch[:6])

    # ---- 3) 清空 -------------------------------------------------------- #
    c = cli.delete('/api/v1/wpe/packets', headers=H)
    check('DELETE /packets -> 200', c.status_code == 200,
          'HTTP %d %s' % (c.status_code, c.text[:90]))
    after_clear = stat().get('buffered')
    check('清空后 buffered == 0', after_clear == 0, 'buffered=%s' % after_clear)
    check('清空后列表为空', not packets(), 'len=%d' % len(packets()))

    # ---- 4) 导回 -------------------------------------------------------- #
    imp = cli.post('/api/v1/wpe/import', headers=H, json={'packets': got})
    ij = imp.json() if imp.status_code == 200 else {}
    check('POST /import -> 200', imp.status_code == 200,
          'HTTP %d %s' % (imp.status_code, imp.text[:110]))
    check('导回 imported == 导出条数', ij.get('imported') == len(got),
          'imported=%s 期望=%d' % (ij.get('imported'), len(got)))
    # ★ skipped==0 是「导出的是完整 hex」的反证：预览 hex 会被 len 校验拒收。
    check('导回 skipped == 0（证明导出是完整 hex）', ij.get('skipped') == 0,
          'skipped=%s errors=%s' % (ij.get('skipped'),
                                    str(ij.get('errors'))[:160]))
    after_imp = stat().get('buffered')
    check('导回后 buffered 恢复', after_imp == before,
          '恢复=%s 原=%s' % (after_imp, before))

    # ---- 5) 内容对齐 + 诚实性标记 --------------------------------------- #
    # ★ 不能用列表的 hex 比 —— 列表里那是 512 字符**预览**，拿它跟完整 hex
    #   比必然不等（这一条第一次跑就是这么假红的）。要验字节级还原，
    #   只能再走一次导出，拿完整 hex 比完整 hex。
    got2: list = []
    since = 0
    for _ in range(20):
        r = cli.get('/api/v1/wpe/export', headers=H,
                    params={'since': since, 'limit': 20000}).json()
        got2.extend(r.get('packets') or [])
        if not r.get('next_since'):
            break
        since = r['next_since']
    check('重新导出条数一致', len(got2) == len(got),
          'now=%d before=%d' % (len(got2), len(got)))
    check('★ 完整 hex 多重集一字不差（字节级还原）',
          sorted(p.get('hex') or '' for p in got2)
          == sorted(p.get('hex') or '' for p in got))

    new = packets()
    new_ids = {x['id'] for x in new}
    check('导回条数与清空前一致', len(new) == before,
          'now=%d before=%s' % (len(new), before))
    check('导回的包是新 id（seq 不重置，不与旧 id 撞）',
          not (new_ids & old_ids), '交集=%s' % sorted(new_ids & old_ids)[:6])
    check('导回的包都带 imported 标记',
          all(x.get('imported') for x in new),
          '未标记=%d' % sum(1 for x in new if not x.get('imported')))
    check('导回的包写明「非实时流量」',
          all('非实时' in (x.get('note') or '') for x in new),
          '未写明=%d' % sum(1 for x in new if '非实时' not in (x.get('note') or '')))

    print('\n%d PASS / %d FAIL' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
