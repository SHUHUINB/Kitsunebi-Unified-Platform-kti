#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一花 MySQL(ccpy) → 新平台 schema 迁移。

默认 **dry-run**，只打印将要写什么，不碰任何东西。加 --apply 才真的写。

字段映射（一花 → 新平台）：
  server_list.ip / cport / serveruser / password   → KamiServer.host / port / username / password
  application.appname / appcode / serverip         → KamiApp.name / code / server_id（按 host 匹配）
  kami.kami / times / comment / app                → Kami.code / duration_hours / comment / app_id
  kami.ext.connection                              → Kami.max_conn
  kami.ext.bandwidthup/down                        → Kami.bandwidth（CCProxy 用 "up/down"，这里存单个 -1）
  kami.state=1 时的 use_date / username / end_date  → Kami.used_at / used_by / expire_at

★ `times` 是 varchar 存的 **PHP 相对时间串**（"+30 day"），必须转成整数小时。
  一花把它直接喂 strtotime；新平台存 duration_hours。
  解析不出来就按 24h 兜底，并在报告里点名 —— 不猜、不静默。

★ 幂等：卡密/应用/出口按唯一键查重，已存在就跳过（重复跑不会翻倍）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import httpx

API = os.environ.get('MIG_API', 'http://127.0.0.1:8902')
USER = os.environ.get('MIG_USER', 'admin')
PWD = os.environ.get('MIG_PWD', 'Admin-ChangeMe-2026')
CONTAINER = os.environ.get('MIG_CONTAINER', 'yihua-db')
DBUSER = os.environ.get('MIG_DBUSER', 'root')
DBPASS = os.environ.get('MIG_DBPASS', 'MySQL-Root-ChangeMe')
DBNAME = os.environ.get('MIG_DB', 'ccpy')

_UNIT_HOURS = {
    'second': 1 / 3600.0, 'minute': 1 / 60.0, 'hour': 1.0,
    'day': 24.0, 'week': 168.0, 'month': 720.0, 'year': 8760.0,
}
_TIMES_RE = re.compile(r'([+-]?\d+)\s*(second|minute|hour|day|week|month|year)s?',
                       re.I)


def parse_times(raw: str) -> tuple[int, bool]:
    """PHP 相对时间串 → 整数小时。返回 (hours, ok)。"""
    s = (raw or '').strip()
    m = _TIMES_RE.search(s)
    if not m:
        return 24, False
    n = int(m.group(1))
    unit = m.group(2).lower()
    hours = int(round(abs(n) * _UNIT_HOURS[unit]))
    return (hours or 24), True


def mysql_tsv(sql: str) -> list[dict]:
    """走 docker exec 读一花库，返回 list[dict]。只读。"""
    cmd = ['docker', 'exec', CONTAINER, 'mysql',
           '-u' + DBUSER, '-p' + DBPASS, '--batch', '--raw', '-e', sql]
    # sudo 是必须的：docker 需要 root。
    out = subprocess.run(['sudo', '-n'] + cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError('读库失败：%s' % (out.stderr or out.stdout)[:400])
    lines = [l for l in out.stdout.splitlines()
             if l and 'Using a password' not in l]
    if not lines:
        return []
    head = lines[0].split('\t')
    return [dict(zip(head, l.split('\t'))) for l in lines[1:]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真的写（默认只预览）')
    args = ap.parse_args()

    cli = httpx.Client(base_url=API, timeout=60.0, trust_env=False)
    tok = (cli.post('/api/v1/auth/login',
                    json={'username': USER, 'password': PWD}).json()
           or {}).get('data', {}).get('token')
    if not tok:
        print('登录失败')
        return 2
    H = {'Authorization': 'Bearer ' + tok}

    servers = mysql_tsv(
        'SELECT id,ip,serveruser,password,state,cport,username FROM %s.server_list' % DBNAME)
    apps = mysql_tsv(
        'SELECT appid,appcode,appname,serverip,username FROM %s.application' % DBNAME)
    cards = mysql_tsv(
        'SELECT id,kami,times,comment,host,state,use_date,username,app,end_date,ext '
        'FROM %s.kami' % DBNAME)

    print('读到：出口 %d / 应用 %d / 卡密 %d' % (len(servers), len(apps), len(cards)))
    print('模式：%s\n' % ('APPLY（真写）' if args.apply else 'DRY-RUN（不写）'))

    # ---- 出口 ------------------------------------------------------------ #
    existing = {('%s:%s' % (s['host'], s['port'])): s['id']
                for s in (cli.get('/api/v1/kami/servers', headers=H).json()
                          or {}).get('data') or []}
    host2srv: dict[str, int] = {}
    for s in servers:
        key = '%s:%s' % (s['ip'], s['cport'])
        if key in existing:
            host2srv[s['ip']] = existing[key]
            print('  出口 %-18s 已存在 id=%s，跳过' % (key, existing[key]))
            continue
        payload = {'host': s['ip'], 'port': int(s['cport'] or 0),
                   'username': s['serveruser'] or '', 'password': s['password'] or '',
                   'remark': '一花迁移 server_list.id=%s' % s['id'],
                   'enabled': str(s.get('state') or '1') == '1'}
        print('  出口 %-18s -> %s' % (key, json.dumps(payload, ensure_ascii=False)))
        if args.apply:
            r = cli.post('/api/v1/kami/servers', headers=H, json=payload)
            if r.status_code == 201:
                host2srv[s['ip']] = (r.json() or {}).get('id')
            else:
                print('    !! 失败 HTTP %d %s' % (r.status_code, r.text[:160]))
                continue
        else:
            host2srv[s['ip']] = -1

    # ---- 应用 ------------------------------------------------------------ #
    app_existing = {a.get('code'): a['id']
                    for a in (cli.get('/api/v1/kami/apps', headers=H).json()
                              or {}).get('data') or []}
    code2app: dict[str, int] = {}
    for a in apps:
        code = a['appcode']
        if code in app_existing:
            code2app[code] = app_existing[code]
            print('  应用 %-34s 已存在 id=%s，跳过' % (code, app_existing[code]))
            continue
        payload = {'name': a['appname'], 'code': code,
                   'server_id': host2srv.get(a['serverip']) or None,
                   'remark': '一花迁移 application.appid=%s' % a['appid']}
        print('  应用 %-34s -> %s' % (code, json.dumps(payload, ensure_ascii=False)))
        if args.apply:
            r = cli.post('/api/v1/kami/apps', headers=H, json=payload)
            if r.status_code == 201:
                code2app[code] = (r.json() or {}).get('id')
            else:
                print('    !! 失败 HTTP %d %s' % (r.status_code, r.text[:160]))
                continue
        else:
            code2app[code] = -1

    # ---- 卡密 ------------------------------------------------------------ #
    cards_existing = {k.get('code') for k in
                      (cli.get('/api/v1/kami/list', headers=H,
                               params={'size': 500}).json() or {}).get('data') or []}
    bad_times: list[str] = []
    n_new = 0
    for k in cards:
        code = k['kami']
        hours, ok = parse_times(k['times'])
        if not ok:
            bad_times.append('%s(times=%r)' % (code, k['times']))
        try:
            ext = json.loads(k.get('ext') or '{}')
        except ValueError:
            ext = {}
        bw = ext.get('bandwidthdown', ext.get('bandwidth', -1))
        payload = {
            'code': code,
            'app_id': code2app.get(k.get('app') or ''),
            'duration_hours': hours,
            'comment': (k.get('comment') or '')[:255],
            'max_conn': int(ext.get('connection', 1) or 1),
            'bandwidth': int(bw if bw is not None else -1),
            'enabled': True,
        }
        if code in cards_existing:
            print('  卡密 %-20s 已存在，跳过（times=%s -> %dh）' % (code, k['times'], hours))
            continue
        n_new += 1
        print('  卡密 %-20s times=%-10s -> %4dh  state=%s  -> %s'
              % (code, k['times'], hours, k.get('state'),
                 json.dumps(payload, ensure_ascii=False)))
        if args.apply:
            r = cli.post('/api/v1/kami', headers=H, json=payload)
            if r.status_code not in (200, 201, 409):
                print('    !! 失败 HTTP %d %s' % (r.status_code, r.text[:160]))

    print('\n--- 汇总 ---')
    print('出口 %d / 应用 %d / 待迁卡密 %d' % (len(servers), len(apps), n_new))
    if bad_times:
        print('★ times 解析不出、已按 24h 兜底：%s' % ', '.join(bad_times))
    if not args.apply:
        print('这是 DRY-RUN —— 什么都没写。加 --apply 才落库。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
