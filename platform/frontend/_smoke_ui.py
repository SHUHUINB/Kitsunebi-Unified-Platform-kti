"""前端 UI 冒烟 —— 真开浏览器、真渲染、真收集控制台错误。

    python _smoke_ui.py            # 默认打 http://127.0.0.1:8900

为什么不是「能 build 就算过」：构建只证明模板能编译，证明不了运行时。
视图里一个 undefined 取值就够让整页白屏，而 build 是绿的。

流程：起 Edge 无头 → CDP 连上 → 抓 console.error / exceptionThrown →
逐个路由导航 → 断言关键文案在 DOM 里。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

BASE = os.environ.get('SMOKE_BASE', 'http://127.0.0.1:8900')
PORT = int(os.environ.get('SMOKE_CDP_PORT', '9333'))
USER = os.environ.get('SMOKE_USER', 'admin')
PWD = os.environ.get('SMOKE_PWD', 'admin')
# 截图落在 _rewrite/screenshots/ —— 交付时可以直接看渲染结果，不用自己开浏览器。
SHOT_DIR = Path(__file__).resolve().parents[1] / 'screenshots'

EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]

ROUTES = [
    ('/login', ['登录'], False),
    ('/overview', ['平台数据', 'CCProxy'], True),
    ('/wpe', ['封包列表', '自动改写'], True),
    ('/ccpx', ['账号列表'], True),
    ('/charles', ['增量改动', '备份', '官方帮助'], True),
    ('/kami', ['生成卡密', '卡密列表'], True),
]


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def cdp_targets() -> list[dict]:
    req = urllib.request.Request('http://127.0.0.1:%d/json/list' % PORT)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


class Cdp:
    def __init__(self, ws_url: str):
        # websocket-client 会去读 http_proxy —— 本机有代理时必须显式绕开，
        # 否则连 127.0.0.1 的 CDP 也会被塞进代理，直接连不上。
        os.environ.pop('http_proxy', None)
        os.environ.pop('https_proxy', None)
        os.environ.pop('HTTP_PROXY', None)
        os.environ.pop('HTTPS_PROXY', None)
        os.environ.pop('all_proxy', None)
        os.environ.pop('ALL_PROXY', None)
        self.ws = websocket.create_connection(ws_url, timeout=20)
        self.n = 0
        # 分两桶：JS 层的异常/console.error 才算失败；网络层的
        # 「Failed to load resource」是环境问题（上游没起），单独列出来，
        # 不能拿它当代码坏了 —— 但也不能假装没看见。
        self.errors: list[str] = []
        self.net: list[str] = []

    def send(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params}))
        return self.n

    def drain(self, seconds: float = 0.6):
        """把这段时间内的事件读出来，顺手收集错误。"""
        end = time.time() + seconds
        self.ws.settimeout(0.25)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:                      # noqa: BLE001
                continue
            m = msg.get('method')
            if m == 'Runtime.exceptionThrown':
                d = msg['params'].get('exceptionDetails', {})
                txt = (d.get('exception') or {}).get('description') or d.get('text')
                self.errors.append('exception: %s' % txt)
            elif m == 'Runtime.consoleAPICalled':
                if msg['params'].get('type') == 'error':
                    args = msg['params'].get('args') or []
                    self.errors.append('console.error: %s'
                                       % ' '.join(str(a.get('value') or a.get('description'))
                                                  for a in args))
            elif m == 'Log.entryAdded':
                e = msg['params'].get('entry') or {}
                if e.get('level') != 'error':
                    continue
                txt = str(e.get('text') or '')
                if 'Failed to load resource' in txt:
                    self.net.append('%s %s' % (txt, e.get('url') or ''))
                else:
                    self.errors.append('log: %s %s' % (txt, e.get('url') or ''))

    def evaluate(self, expr: str, await_promise: bool = False):
        i = self.send('Runtime.evaluate', expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        self.ws.settimeout(20)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == i:
                res = msg.get('result') or {}
                if 'exceptionDetails' in res:
                    return None, res['exceptionDetails'].get('text', 'evaluate 异常')
                return (res.get('result') or {}).get('value'), None

    def screenshot(self, path: str) -> bool:
        import base64
        i = self.send('Page.captureScreenshot', format='png', captureBeyondViewport=False)
        self.ws.settimeout(30)
        while True:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:                      # noqa: BLE001
                return False
            if msg.get('id') == i:
                data = (msg.get('result') or {}).get('data')
                if not data:
                    return False
                with open(path, 'wb') as fh:
                    fh.write(base64.b64decode(data))
                return True

    def close(self):
        try:
            self.ws.close()
        except Exception:                          # noqa: BLE001
            pass


def form_login_check(cdp) -> list[str]:
    """走**真表单**登录，返回失败原因列表。

    ★ 这是本脚本之前完全没覆盖的路径：老版本是「用 JS 直接 fetch 换 token
      写进 localStorage」，所以「表单到底能不能登录」一次都没被验证过 ——
      线上那个「无法登录」的 bug 就是从这条缝里溜过去的。

    ★ 填值**故意不派发 `input` 事件**：浏览器口令管理器自动填充就是这个形态。
      老代码只信 v-model 的 ref → ref 是空的 → 提交
      `{"username":"","password":""}` → 后端 422 → 前端把 Pydantic 的错误数组
      渲染成 `[object Object]`。所以这里必须「不派发事件也能登进去」。
    """
    fails: list[str] = []
    clr = "try{localStorage.removeItem('ccpx.token')}catch(e){}"

    # --- 1. 空表单：本地就该拦下并给出人话（不该打网络换一个 422 数组） --- #
    cdp.evaluate(clr)
    cdp.send('Page.navigate', url=BASE + '/login')
    cdp.drain(3.0)
    cdp.evaluate("document.querySelector('form').requestSubmit()")
    cdp.drain(1.5)
    txt, _ = cdp.evaluate("(document.querySelector('.err-box')||{}).innerText||''")
    if not txt or '请输入' not in (txt or ''):
        fails.append('空表单提交没有本地提示（.err-box=%r）' % txt)

    # --- 2. 自动填充形态（设值但不派发 input）→ 必须登进去 ---------------- #
    js = """
    (() => {
      const u = document.querySelector('#u'), p = document.querySelector('#p');
      if (!u || !p) return 'NO_FORM';
      u.value = %s; p.value = %s;            /* ← 故意不 dispatchEvent */
      document.querySelector('form').requestSubmit();
      return 'SUBMITTED';
    })()
    """ % (json.dumps(USER), json.dumps(PWD))
    val, err = cdp.evaluate(js)
    if val != 'SUBMITTED':
        fails.append('自动填充形态提交失败：%s %s' % (val, err or ''))
        return fails
    cdp.drain(3.5)
    href, _ = cdp.evaluate('location.href')
    tok, _ = cdp.evaluate("localStorage.getItem('ccpx.token')||''")
    if not tok:
        fails.append('自动填充形态登录后没有令牌（href=%s）' % href)
    elif '/login' in (href or ''):
        fails.append('登录成功但没离开登录页（href=%s）' % href)

    # --- 3. 口令错：提示必须是人能读的话，不能是 [object Object] ---------- #
    cdp.evaluate(clr)
    cdp.send('Page.navigate', url=BASE + '/login')
    cdp.drain(3.0)
    js2 = """
    (() => {
      const u = document.querySelector('#u'), p = document.querySelector('#p');
      u.value = %s; p.value = 'definitely-not-the-password';
      document.querySelector('form').requestSubmit();
      return 'SUBMITTED';
    })()
    """ % json.dumps(USER)
    cdp.evaluate(js2)
    cdp.drain(3.5)
    txt, _ = cdp.evaluate("(document.querySelector('.err-box')||{}).innerText||''")
    txt = txt or ''
    if not txt or 'object' in txt.lower():
        fails.append('口令错时的提示不可读：%r' % txt)

    cdp.evaluate(clr)
    return fails


def main() -> int:
    edge = find_edge()
    if not edge:
        print('找不到 Edge，跳过 UI 冒烟')
        return 0

    profile = tempfile.mkdtemp(prefix='edgeui_')
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    shot_dir = SHOT_DIR
    proc = subprocess.Popen(
        [edge, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
         '--remote-allow-origins=*',
         '--remote-debugging-port=%d' % PORT, '--user-data-dir=' + profile, 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    fails, passes, nets = [], [], []
    try:
        target = None
        for _ in range(60):
            try:
                for t in cdp_targets():
                    if t.get('type') == 'page' and t.get('webSocketDebuggerUrl'):
                        target = t
                        break
                if target:
                    break
            except Exception:                      # noqa: BLE001
                pass
            time.sleep(0.25)
        if not target:
            print('CDP 没起来（page target 拿不到）')
            return 2

        cdp = Cdp(target['webSocketDebuggerUrl'])
        cdp.send('Runtime.enable')
        cdp.send('Page.enable')
        cdp.send('Log.enable')
        cdp.send('Emulation.setDeviceMetricsOverride', width=1440, height=900,
                 deviceScaleFactor=1, mobile=False)
        cdp.drain(0.5)

        # ---- 真表单登录（含浏览器自动填充形态）------------------------- #
        # 必须排在最前面：后面那套用 localStorage 换 token 的做法掩盖了这条路径。
        form_fails = form_login_check(cdp)
        fails.extend(form_fails)
        if not form_fails:
            passes.append('表单登录')

        # 先到登录页，再用真实接口换 token 写进 localStorage —— 后面才是「已登录」的路由。
        cdp.send('Page.navigate', url=BASE + '/login')
        cdp.drain(2.0)

        login_js = """
        (async () => {
          const r = await fetch('/api/v1/auth/login', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: %s, password: %s})
          });
          const j = await r.json();
          if (!j.ok) return 'login failed: ' + JSON.stringify(j);
          localStorage.setItem('ccpx.token', j.data.token);
          return 'ok';
        })()
        """ % (json.dumps(USER), json.dumps(PWD))
        val, err = cdp.evaluate(login_js, await_promise=True)
        if val != 'ok':
            print('登录失败，无法继续 UI 冒烟：%s %s' % (val, err or ''))
            return 2

        for path, markers, need_auth in ROUTES:
            cdp.errors.clear()
            cdp.net.clear()
            cdp.send('Page.navigate', url=BASE + path)
            cdp.drain(3.2)
            text, eerr = cdp.evaluate(
                'document.body ? document.body.innerText : ""')
            if eerr:
                fails.append('%s 取值失败：%s' % (path, eerr))
                continue
            text = text or ''
            missing = [m for m in markers if m not in text]
            if missing:
                fails.append('%s 缺文案 %s（页面文本 %d 字符）｜%s'
                             % (path, missing, len(text),
                                text[:220].replace('\n', ' / ')))
            else:
                passes.append(path)
            if cdp.errors:
                fails.append('%s 控制台错误：%s' % (path, cdp.errors[:3]))
            if cdp.net:
                nets.extend('%s → %s' % (path, x) for x in cdp.net[:4])
            cdp.screenshot(str(shot_dir / (path.strip('/').replace('/', '_') + '.png')))

        cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                          # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print('UI 通过：%s' % (', '.join(passes) or '（无）'))
    if nets:
        print('上游不可达（环境问题，非前端代码）：')
        for x in nets[:8]:
            print('  ~ ' + x)
    if fails:
        print('UI 失败：')
        for f in fails:
            print('  - ' + f)
        return 1
    print('%d 个路由渲染正常 + 表单登录通过，无 JS 异常/console.error' % len(ROUTES))
    return 0


if __name__ == '__main__':
    sys.exit(main())
