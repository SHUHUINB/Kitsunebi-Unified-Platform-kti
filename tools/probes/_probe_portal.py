#!/usr/bin/env python3
"""用户门户 + 公开应用列表 + 卡密删除 —— 实机对账探针。

这一轮补的是「完整一花系统」的最后一块：**公开用户门户**。
之前 141 PASS 证明的是**管理端契约**正确，不证明**最终用户走得通** ——
最终用户手里只有卡密，没有管理台账号，所以他们要用的那条链路必须
**全程无鉴权**，而这恰恰是「加鉴权最顺手」的地方，很容易被顺手加死。

每一条都对着真实响应判，不看代码意图：

  1. /portal 200 且**没被 SPA 通配兜成 index.html**
     —— 兜成 index.html 时也是 200，只是用户看到的是管理台外壳，
        这个错在浏览器里一眼看不出（页面能开），必须用内容断言钉住。
  2. /portal **无登录可访问**（全程不带 token）。
  3. /api/v1/kami/apps/public 无登录、`code=1`、`msg` 是列表、
     **每个元素只有 appcode/appname 两个键** —— 多回一个出口地址
     就等于把后端拓扑摊给所有能打开门户的人。
  4. 停用的应用**不出现在**公开列表里（出现 → 用户选了它 → 注册失败
     → 报「服务器通信出现问题」→ 用户以为卡密坏了）。
  5. 三个 client 动作（insert/update/query）从门户链路走通。
     **上游感知**：适配层活着断言 code=1，适配层挂了断言 code=-3 且
     卡密被释放（失败不烧卡）—— 写死「上游必活」会让另一侧必然假红。
  6. 管理端接口**仍然要鉴权**（无 token 必须 401）——
     证明「放开 public 接口」不是把鉴权整体拆掉了。
  7. 新增的卡密删除端点可用，且空参数回一花同款文案。

用法：
    PROBE_API=http://127.0.0.1:8903 \
    PROBE_USER=admin PROBE_PASS=... \
    python _probe_portal.py
"""
from __future__ import annotations

import os
import secrets
import sys

import httpx

API = os.environ.get('PROBE_API', 'http://127.0.0.1:8903').rstrip('/')
UPSTREAM = os.environ.get('PROBE_UPSTREAM', 'http://127.0.0.1:8893').rstrip('/')
USER = os.environ.get('PROBE_USER', 'admin')
PASS = os.environ.get('PROBE_PASS', '')

# 测试数据一律带这个前缀，跑完删干净；即使中途挂了，看名字也知道是谁留的。
PFX = '_probe_p'
# ★ 用户名必须**每次不同**：适配层里的账号是持久化的，`insert` 遇到已存在的
#   账号会回 -1「账号已经存在」。第一版用了固定名，第二次跑必然假红两条 ——
#   而那个红是探针自己造成的，跟被测代码无关。
U1 = 'pp' + secrets.token_hex(3)      # 纯字母数字且 >= 5 位，符合 _CCP_USER_RE
U1B = U1 + 'b'                        # 单入口 /client 那条用另一个名字
PW = 'probe12345'                     # 密码：同时含字母和数字，5-16 位

PASS_N = FAIL = 0


def check(name: str, ok: bool, extra: str = '') -> None:
    global PASS_N, FAIL
    if ok:
        PASS_N += 1
        print('PASS %-56s %s' % (name, extra))
    else:
        FAIL += 1
        print('FAIL %-56s %s' % (name, extra))


def _upstream_alive() -> bool:
    """适配层活不活 —— 决定下面一批断言的期望值。

    ★ 不写死「上游必活」：探针和被测服务常在不同机器/不同状态，
      写死等于让探针在另一种环境下必然假红。
    """
    try:
        r = httpx.get(UPSTREAM + '/status', timeout=6, trust_env=False)
        return r.status_code < 500
    except Exception:
        return False


def main() -> int:  # noqa: C901
    # ★ trust_env=False：httpx 默认读 http_proxy，会把 127.0.0.1 交给代理。
    cli = httpx.Client(base_url=API, timeout=30, trust_env=False,
                       follow_redirects=False)
    admin = httpx.Client(base_url=API, timeout=30, trust_env=False,
                         follow_redirects=False)
    created = {'servers': [], 'apps': [], 'codes': []}

    # ---- 0) 管理员登录（只用于建/清测试数据 + 验证管理端仍要鉴权） ------- #
    r = admin.post('/api/v1/auth/login', json={'username': USER, 'password': PASS})
    if r.status_code != 200:
        print('管理员登录失败 HTTP %d：%s' % (r.status_code, r.text[:200]))
        print('（用 PROBE_USER / PROBE_PASS 指定；这一步失败后面建不了测试数据）')
        return 1
    tok = (r.json().get('data') or {}).get('token') or ''
    admin.headers['Authorization'] = 'Bearer ' + tok
    print('管理员已登录，token 长度 %d' % len(tok))
    up = _upstream_alive()
    print('适配层上游 %s：%s' % (UPSTREAM, '可达' if up else '不可达'))
    print()

    try:
        # ================================================================== #
        # A. 门户页
        # ================================================================== #
        p = cli.get('/portal')
        page = p.text
        check('/portal -> 200', p.status_code == 200, 'HTTP %d' % p.status_code)
        check('/portal 是 HTML',
              'text/html' in p.headers.get('content-type', ''),
              p.headers.get('content-type', ''))
        check('门户含三个 tab（注册/充值/查询）',
              all(k in page for k in ('注册', '充值', '查询')))
        # ★ 这两条是同一个错误的两面：SPA 通配把 /portal 兜成 index.html。
        #   那种情况 HTTP 200、页面也能开，但用户看到的是管理台外壳。
        check('★ /portal 没被 SPA 通配兜成 index.html',
              '<div id="app">' not in page and 'assets/index-' not in page)
        # ★ 一花的 insert 只传 user/pwd/code（卡密自带应用归属）。
        #   门户上给注册表单加应用下拉框是**误导** —— 填了也不生效。
        check('★ 注册表单没有应用下拉框（id="r-app" 不存在）',
              'id="r-app"' not in page)
        check('注册表单说明了「不用选应用」',
              '卡密已经绑定了应用和时长' in page)
        check('门户 JS 指向三个 client 端点',
              all(k in page for k in ('/api/v1/kami/client/insert',
                                      '/api/v1/kami/client/update',
                                      '/api/v1/kami/client/query')))
        check('门户 JS 指向公开应用列表',
              '/api/v1/kami/apps/public' in page)
        check('★ /portal 无登录可访问（本探针全程未带 token）', True, '')

        # ================================================================== #
        # B. 公开应用列表
        # ================================================================== #
        pub = cli.get('/api/v1/kami/apps/public')
        check('★ /apps/public 无 token -> 200', pub.status_code == 200,
              'HTTP %d' % pub.status_code)
        pj = pub.json() if pub.status_code == 200 else {}
        check('/apps/public code == 1', pj.get('code') == 1, repr(pj.get('code')))
        check('/apps/public msg 是列表', isinstance(pj.get('msg'), list),
              type(pj.get('msg')).__name__)
        items = pj.get('msg') if isinstance(pj.get('msg'), list) else []
        keysets = {tuple(sorted(it.keys())) for it in items}
        check('★ 每个元素只有 appcode / appname 两个键',
              keysets <= {('appcode', 'appname')}, repr(keysets))
        # ★ 拓扑泄漏检查：拿原始 JSON 文本找关键词，比逐个键名更狠
        #   —— 后端哪天改成嵌一层 {"app": {...}} 也拦得住。
        raw = pub.text.lower()
        leaked = [w for w in ('serverid', 'server_id', 'host', 'port', 'ip',
                              'address', 'addr', 'password', 'passwd')
                  if w in raw]
        check('★ 响应里不含任何出口/凭据字段', not leaked, repr(leaked))

        # 建测试数据（启用 + 停用各一个应用）
        srv = admin.post('/api/v1/kami/servers',
                         json={'host': PFX + '_srv', 'port': 0,
                               'remark': 'portal probe'})
        sid = (srv.json() or {}).get('id') if srv.status_code < 300 else None
        if sid:
            created['servers'].append(sid)
        check('建测试出口成功', bool(sid), 'id=%s' % sid)

        on = admin.post('/api/v1/kami/apps',
                        json={'name': PFX + '_on', 'code': PFX + '_on',
                              'server_id': sid, 'enabled': True})
        on_id = (on.json() or {}).get('id') if on.status_code < 300 else None
        if on_id:
            created['apps'].append(on_id)
        off = admin.post('/api/v1/kami/apps',
                         json={'name': PFX + '_off', 'code': PFX + '_off',
                               'server_id': sid, 'enabled': False})
        off_id = (off.json() or {}).get('id') if off.status_code < 300 else None
        if off_id:
            created['apps'].append(off_id)
        check('建两个测试应用（一启用一停用）', bool(on_id and off_id),
              'on=%s off=%s' % (on_id, off_id))

        pub2 = cli.get('/api/v1/kami/apps/public').json()
        codes2 = [it.get('appcode') for it in (pub2.get('msg') or [])]
        check('★ 启用的应用出现在公开列表', PFX + '_on' in codes2)
        check('★ 停用的应用不出现在公开列表', PFX + '_off' not in codes2)

        # ================================================================== #
        # C. 门户链路（四个卡密：insert / 重复 insert / update / 单入口）
        # ================================================================== #
        kc = []
        for i in range(4):
            kr = admin.post('/api/v1/kami',
                            json={'app_id': on_id, 'server_id': sid,
                                  'duration_hours': 1,
                                  'comment': '%s_%d' % (PFX, i)})
            kc.append((kr.json() or {}).get('code') if kr.status_code < 300 else None)
        created['codes'] = [c for c in kc if c]
        check('建 4 张测试卡密', len(created['codes']) == 4,
              repr(created['codes']))

        ins = cli.post('/api/v1/kami/client/insert',
                       data={'user': U1, 'pwd': PW, 'code': kc[0]})
        ij = ins.json()
        check('★ /client/insert 无 token 可达',
              ins.status_code == 200 and 'code' in ij, repr(ij)[:120])
        if up:
            check('★ 上游活：insert 回 code=1（注册成功）',
                  ij.get('code') == 1, 'code=%s msg=%s' % (ij.get('code'), ij.get('msg')))
        else:
            check('★ 上游死：insert 回 code=-3（服务器通信出现问题）',
                  ij.get('code') == -3, 'code=%s msg=%s' % (ij.get('code'), ij.get('msg')))

        # ★ 「失败不烧卡」——上游挂掉时 `_release()` 必须把占位退回。
        #   这条断言在两种上游状态下都有意义：活的时候卡应已激活，
        #   死的时候卡必须回到未使用，否则一次网络抖动就吃掉用户一张卡。
        row = admin.get('/api/v1/kami/list',
                        params={'keyword': kc[0]}).json()
        krow = (row.get('data') or [{}])[0]
        if up:
            check('★ 上游活：卡密已被激活', bool(krow.get('activatedAt')),
                  'activatedAt=%s' % krow.get('activatedAt'))
        else:
            check('★ 上游死：卡密被释放（失败不烧卡）',
                  not krow.get('activatedAt'), 'activatedAt=%s' % krow.get('activatedAt'))

        again = cli.post('/api/v1/kami/client/insert',
                         data={'user': U1, 'pwd': PW, 'code': kc[0]}).json()
        if up:
            check('★ 重复用同一张卡 -> code=-1（已被使用）',
                  again.get('code') == -1, 'code=%s msg=%s' % (again.get('code'), again.get('msg')))
        else:
            check('上游死：重复用同一张卡仍 code=-3', again.get('code') == -3,
                  'code=%s' % again.get('code'))

        upd = cli.post('/api/v1/kami/client/update',
                       data={'user': U1, 'code': kc[1]}).json()
        check('★ /client/update 无 token 可达且状态正确',
              upd.get('code') in ((1, -1, -3) if up else (-3,)),
              'code=%s msg=%s' % (upd.get('code'), upd.get('msg')))

        qry = cli.post('/api/v1/kami/client/query',
                       data={'user': U1, 'appcode': PFX + '_on'}).json()
        check('★ /client/query 无 token 可达且状态正确',
              qry.get('code') in ((1, -3) if up else (-3,)),
              'code=%s' % qry.get('code'))

        single = cli.post('/api/v1/kami/client',
                          data={'type': 'insert', 'user': U1B,
                                'pwd': PW, 'code': kc[2]}).json()
        check('★ 单入口 /client（带 type）也走通',
              single.get('code') in ((1, -1, -3) if up else (-3,)),
              'code=%s msg=%s' % (single.get('code'), single.get('msg')))

        # ================================================================== #
        # D. 管理端仍然要鉴权（证明没把鉴权整体拆掉）
        # ================================================================== #
        for path in ('/api/v1/kami/apps', '/api/v1/kami/list',
                     '/api/v1/kami/servers'):
            rr = cli.get(path)
            check('★ 管理端 %s 无 token -> 401' % path, rr.status_code == 401,
                  'HTTP %d' % rr.status_code)
        rr = cli.post('/api/v1/kami/delete', json={'codes': ['x']})
        check('★ 管理端 /kami/delete 无 token -> 401', rr.status_code == 401,
              'HTTP %d' % rr.status_code)
        rr = admin.get('/api/v1/kami/apps')
        check('管理端带 token -> 200', rr.status_code == 200,
              'HTTP %d' % rr.status_code)

        # ================================================================== #
        # E. 卡密删除（本轮新增，对应一花 ajax.php?act=delkami）
        # ================================================================== #
        empty = admin.post('/api/v1/kami/delete', json={'codes': []}).json()
        check('★ 空参数回一花同款文案',
              empty.get('code') == -1 and '参数为空' in (empty.get('msg') or ''),
              repr(empty))

        kid = krow.get('id')
        one = admin.delete('/api/v1/kami/%d' % kid) if kid else None
        check('★ 单条删除 DELETE /kami/{id} -> 200',
              bool(one) and one.status_code == 200,
              'HTTP %s' % (one.status_code if one else 'n/a'))
        if one and one.status_code == 200:
            created['codes'] = [c for c in created['codes'] if c != kc[0]]

        rest = [c for c in created['codes']]
        many = admin.post('/api/v1/kami/delete', json={'codes': rest}).json()
        check('★ 批量删除回 code=1',
              many.get('code') == 1,
              'deleted=%s missing=%s used=%s' % (many.get('deleted'),
                                                 many.get('missing'), many.get('used')))
        check('★ 批量删除条数与请求一致',
              many.get('deleted') == len(rest),
              '请求 %d / 删除 %s' % (len(rest), many.get('deleted')))
        if many.get('code') == 1:
            created['codes'] = []
        left = admin.get('/api/v1/kami/list',
                         params={'keyword': PFX}).json().get('data') or []
        check('★ 测试卡密已清空', not left, '残留 %d 条' % len(left))

        # ================================================================== #
        # F. 回归：设备侧老端点没被碰坏
        # ================================================================== #
        for path in ('/ca', '/ok', '/ca.crt', '/socks', '/ruleset.conf'):
            rr = cli.get(path)
            check('回归 %s 仍 200' % path, rr.status_code == 200,
                  'HTTP %d' % rr.status_code)
        dev = cli.get('/_device')
        dj = dev.json() if dev.status_code == 200 else {}
        eps = dj.get('endpoints') or {}
        check('/_device 无 token 200', dev.status_code == 200,
              'HTTP %d' % dev.status_code)
        check('★ /_device 清单里有 /portal', '/portal' in eps)
        check('★ /_device 里 /portal 标注了无登录',
              '无登录' in (eps.get('/portal') or ''),
              repr(eps.get('/portal'))[:60])
        check('/_device 清单里有 /ca.mobileconfig', '/ca.mobileconfig' in eps)

    finally:
        # ================================================================== #
        # G. 清理（适配层账号 → 卡密 → 应用 → 出口；顺序不能反：挂着的删不掉）
        # ================================================================== #
        print()
        # ★ 适配层账号必须删：它是**持久**在 ccpx 里的，不删的话下次跑
        #   `insert` 会撞「账号已经存在」，探针自己把自己搞红。
        for uname in (U1, U1B):
            admin.delete('/api/v1/ccpx/accounts/%s' % uname)
        if created['codes']:
            admin.post('/api/v1/kami/delete', json={'codes': created['codes']})
        for aid in created['apps']:
            admin.delete('/api/v1/kami/apps/%d' % aid)
        for sid in created['servers']:
            admin.delete('/api/v1/kami/servers/%d' % sid)
        left = admin.get('/api/v1/kami/list',
                         params={'keyword': PFX}).json().get('data') or []
        apps_left = [a for a in (admin.get('/api/v1/kami/apps').json().get('data') or [])
                     if (a.get('name') or '').startswith(PFX)]
        srvs_left = [s for s in (admin.get('/api/v1/kami/servers').json().get('data') or [])
                     if (s.get('host') or '').startswith(PFX)]
        accts = (admin.get('/api/v1/ccpx/accounts').json().get('data') or [])
        accts_left = [a for a in accts
                      if (a.get('username') or a.get('user') or '').startswith('pp')]
        print('清理：卡密残留 %d / 应用残留 %d / 出口残留 %d / 账号残留 %d'
              % (len(left), len(apps_left), len(srvs_left), len(accts_left)))
        check('★ 测试数据已全部清理',
              not (left or apps_left or srvs_left or accts_left))

    print()
    print('%d PASS / %d FAIL' % (PASS_N, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
