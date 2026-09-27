"""Charles 配置引擎。

从旧管理台**忠实移植**，一行语义都没简化 —— 这里踩过的坑全部是竞态，
凭直觉重写必然踩回去：

  1. 改配置前必须 `stop` 并**等 Java 进程消失**。Charles 在 JVM 关闭钩子里
     用内存配置覆盖 `~/.charles.config`，该钩子晚于 systemd 收到的 SIGTERM。
     不等 → 我们的写入被静默覆盖，接口返回 ok、备份也生成了，磁盘还是旧值。
  2. 备份必须在 `stop + 等回写` **之后**。stop 之前的磁盘内容不等于即将被覆盖
     的内容，而失败回滚用的正是这份备份。
  3. 写后必须**重读校验**。不一致就再等一次回写、重写一遍；仍不一致则如实报错。
  4. `restart` 只决定「服务原本就没运行」时要不要顺手拉起来。**只要是我们停的
     就必须重启** —— 否则「取消勾选自动重启」会被实现成「停掉不管」，
     代理端口静默断开而接口返回 ok。
  5. 启动失败要先 `reset-failed` 再重试一次。systemd 默认 10 秒内最多启 5 次，
     连点几次「应用」就会撞 StartLimitBurst，单元被标记 failed 后
     `start` 一律回「Start request repeated too quickly」。
  6. 写入串行化，且**只有队尾负责启动**。否则前一个刚 start、后一个立刻 stop，
     那个 start 永远活不到绑定端口（Charles 需要 8–10 秒才绑 8888）。
     恢复判据用队列原状 `_QUEUE_WAS_RUNNING`，不是各写入者自己查到的状态。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from .config import settings

CFG = str(settings.charles_config)
BAK_DIR = str(settings.data_dir / "charles_backups")
SVC = settings.charles_unit
MAX_BAK = 200

XML_HEADER = ("<?xml version='1.0' encoding='UTF-8' ?>\n"
              "<?charles serialisation-version='2.0' ?>\n")

SEG_RE = re.compile(r'^([^\[\]]+)(?:\[(@)?([^\[\]]+)\])?$')

LOCK = threading.RLock()
_TL = threading.local()
_LOCK_QUEUE = 0
_QUEUE_WAS_RUNNING = False


def serialized(fn):
    """写入类接口互斥，顺带维护队列状态。"""
    def wrapper(*a, **kw):
        global _LOCK_QUEUE, _QUEUE_WAS_RUNNING
        depth = getattr(_TL, 'depth', 0)
        _TL.depth = depth + 1
        if depth == 0:
            if _LOCK_QUEUE == 0:
                _QUEUE_WAS_RUNNING = (svc_active() == 'active')
            _LOCK_QUEUE += 1
        try:
            with LOCK:
                return fn(*a, **kw)
        finally:
            _TL.depth = depth
            if depth == 0:
                _LOCK_QUEUE -= 1
                if _LOCK_QUEUE == 0:
                    _QUEUE_WAS_RUNNING = False
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def sh(cmd, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, 'timeout after %ss' % timeout
    except OSError as exc:
        return -1, '%s: %s' % (type(exc).__name__, exc)


def read_cfg() -> str:
    with open(CFG, encoding='utf-8') as fh:
        return fh.read()


def xml_error(text: str) -> str | None:
    try:
        ET.fromstring(text)
        return None
    except ET.ParseError as exc:
        return str(exc)


def ensure_charles_header(text: str) -> tuple[str, bool]:
    """补齐 `<?charles serialisation-version='2.0' ?>` 处理指令。

    ElementTree 只校验 XML 结构合法性 —— 丢了 charles 处理指令一样通过，
    但 Charles 反序列化时依赖它。用户从 GUI 导出或手写片段时常只贴主体，
    所以补齐而不是拒绝。
    """
    if re.search(r'<\?charles\b', text or ''):
        return text, False
    body = re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', text or '')
    return XML_HEADER + body.lstrip(), True


def write_cfg(text: str) -> None:
    err = xml_error(text)
    if err:
        raise ValueError('XML 校验失败，已拒绝写入: %s' % err)
    tmp = CFG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, CFG)          # 原子替换：不会留下半截文件


def backup(tag: str = 'auto') -> str:
    os.makedirs(BAK_DIR, exist_ok=True)
    # 备份名精度只到秒。同一秒内同 tag 备份两次会撞名互相覆盖 ——
    # 手动连点「备份」时后一份会把前一份冲掉，回滚点凭空少一个。
    base = '%s_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), tag)
    name = base + '.config'
    dst = os.path.join(BAK_DIR, name)
    seq = 2
    while os.path.exists(dst):
        name = '%s_%d.config' % (base, seq)
        dst = os.path.join(BAK_DIR, name)
        seq += 1
    shutil.copy2(CFG, dst)

    files = sorted(os.listdir(BAK_DIR))
    while len(files) > MAX_BAK:
        try:
            os.remove(os.path.join(BAK_DIR, files.pop(0)))
        except OSError:
            # 单个删不掉不应中断整个清理（原来用 break 会让备份数一直超限）。
            # pop(0) 已执行，列表在变短，不会死循环。
            continue
    return name


def list_backups() -> list[dict]:
    if not os.path.isdir(BAK_DIR):
        return []
    out = []
    for n in sorted(os.listdir(BAK_DIR), reverse=True):
        p = os.path.join(BAK_DIR, n)
        if not os.path.isfile(p):
            continue
        out.append({
            'name': n,
            'size': os.path.getsize(p),
            'mtime': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return out


# --------------------------------------------------------------------------- #
# 服务控制
# --------------------------------------------------------------------------- #
@serialized
def svc(action: str) -> tuple[int, str]:
    """服务控制也纳入同一把锁。

    否则用户点「重启服务」会和正在进行的写入交叉执行，白白多出几轮
    start/stop（Charles 每次要 8–10 秒才绑端口）。串行化后界面上的服务操作
    排在写入队列后面，状态始终可预期。
    """
    if action not in ('start', 'stop', 'restart'):
        return -1, 'invalid action: %s' % action
    return sh(['sudo', '-n', 'systemctl', action, SVC], timeout=90)


def svc_active() -> str:
    _rc, out = sh(['systemctl', 'is-active', SVC], timeout=15)
    return out.strip() or 'unknown'


def charles_pid() -> str:
    _rc, out = sh(['bash', '-c', "pgrep -f '[c]harles.jar' | head -1"], timeout=15)
    return out.strip()


def wait_stopped(timeout: float = 30.0) -> bool:
    """等 Charles 彻底退出并完成内存配置回写。

    必须等到 Java 进程真的消失 —— 进程没了 = 关闭钩子已执行完。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if svc_active() != 'active' and not charles_pid():
            break
        time.sleep(0.25)
    time.sleep(1.0)                       # 双保险：给文件系统落盘留时间
    prev = None
    for _ in range(24):
        try:
            stt = os.stat(CFG)
            cur = (stt.st_mtime_ns, stt.st_size)
        except OSError:
            cur = None
        if cur is not None and cur == prev:
            return True
        prev = cur
        time.sleep(0.25)
    return False


def bring_back(was_running: bool, restart: bool) -> tuple[bool, str | None, bool]:
    """写入结束后把服务恢复到「可用」状态。返回 (started, err, deferred)。

    见模块 docstring 第 4/5/6 条。判据必须用 `_QUEUE_WAS_RUNNING`，
    不能用本函数的 `was_running` —— 队首把服务停掉后，后面排队的写入查到的
    都是 inactive，会以为「本来就没跑」，于是谁也不负责启动。
    """
    global _QUEUE_WAS_RUNNING
    if not (_QUEUE_WAS_RUNNING or restart):
        return False, None, False
    if _LOCK_QUEUE > 1:                   # 我不是队尾，交给后面的写入者启动
        return False, None, True
    _QUEUE_WAS_RUNNING = False            # 由我恢复，标记清掉

    rc, out = svc('start')
    if rc == 0:
        return True, None, False
    first = out.strip() or 'rc=%d' % rc
    # 任何启动失败都先清计数再试：撞限速是失败，真配置错误也只是多跑一轮。
    sh(['sudo', '-n', 'systemctl', 'reset-failed', SVC], timeout=30)
    time.sleep(1.0)
    rc2, out2 = svc('start')
    if rc2 == 0:
        return True, None, False
    return False, '服务启动失败: %s / 重试后: %s' % (first, out2.strip() or 'rc=%d' % rc2), False


# --------------------------------------------------------------------------- #
# 节点定位
# --------------------------------------------------------------------------- #
def find_node(root, path: str):
    """path 支持三种写法：

        proxyConfiguration/port
        toolConfiguration/configs/entry[3]/rewrite/debugging      数字索引，从 1 开始
        toolConfiguration/configs/entry[@断点]/rewrite/debugging   @key，按 <string> 文本查

    返回 Element 或 None。非法索引一律返回 None 而不是抛异常 ——
    抛出去会被 apply_changes 的外层 except 兜住，表现成「整批失败并回滚」，
    而不是「跳过这一项」。
    """
    cur = root
    for seg in path.split('/'):
        seg = seg.strip()
        if not seg:
            continue
        m = SEG_RE.match(seg)
        if not m:
            return None
        tag, by_key, sel = m.group(1), m.group(2), m.group(3)
        kids = [c for c in cur if c.tag == tag]
        if sel is not None:
            if by_key:
                hit = None
                for c in kids:
                    s = c.find('string')
                    if s is not None and (s.text or '').strip() == sel:
                        hit = c
                        break
                if hit is None:
                    return None
                cur = hit
            else:
                try:
                    i = int(sel)
                except (TypeError, ValueError):
                    return None
                if i < 1 or i > len(kids):
                    return None
                cur = kids[i - 1]
        else:
            if not kids:
                return None
            cur = kids[0]
    return cur


def verify_written(expect: dict[str, str]) -> list[str]:
    """重读磁盘，返回与期望不一致的路径列表（空 = 全部落盘）。"""
    time.sleep(0.35)
    try:
        cur = ET.fromstring(read_cfg())
    except ET.ParseError:
        return list(expect.keys())
    bad = []
    for p, v in expect.items():
        n = find_node(cur, p)
        if n is None or (n.text or '') != v:
            bad.append(p)
    return bad


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
@serialized
def apply_changes(changes: list[dict], restart: bool = True) -> dict:
    """changes: [{'path': '...', 'value': '...', 'type': 'bool|int|str'}]"""
    if not changes:
        return {'ok': False, 'error': '没有要应用的改动'}

    was_running = (svc_active() == 'active')
    q_was_running = _QUEUE_WAS_RUNNING
    if was_running:
        rc, out = svc('stop')
        if rc != 0:
            # stop 失败时服务状态未知；尽力拉回运行，别留下半死的代理。
            svc('start')
            return {'ok': False, 'error': '停止服务失败: %s' % out.strip()}
        wait_stopped()

    # 备份放在 stop + 等回写之后（见 docstring 第 2 条）
    bname = backup('apply')

    applied, errors, expect = [], [], {}
    try:
        root = ET.fromstring(read_cfg())
        for ch in changes:
            path = ch.get('path', '')
            node = find_node(root, path)
            if node is None:
                errors.append('%s — 节点不存在' % path)
                continue
            val = ch.get('value', '')
            typ = ch.get('type', 'str')
            if typ == 'bool':
                val = 'true' if str(val).lower() in ('1', 'true', 'yes', 'on') else 'false'
            elif typ == 'int':
                try:
                    val = str(int(str(val).strip()))
                except ValueError:
                    errors.append('%s — 不是整数: %r' % (path, ch.get('value')))
                    continue
            node.text = val
            applied.append(path)
            expect[path] = val

        write_cfg(XML_HEADER + ET.tostring(root, encoding='unicode'))

        bad = verify_written(expect)
        if bad:
            wait_stopped()
            root2 = ET.fromstring(read_cfg())
            for p, v in expect.items():
                n = find_node(root2, p)
                if n is not None:
                    n.text = v
            write_cfg(XML_HEADER + ET.tostring(root2, encoding='unicode'))
            bad = verify_written(expect)
            if bad:
                errors.append('写后被外部覆盖且重写未生效: %s' % ', '.join(bad))
    except (OSError, ET.ParseError, ValueError) as exc:
        shutil.copy2(os.path.join(BAK_DIR, bname), CFG)     # 回滚
        bring_back(was_running, True)                        # 排队时交给队尾启动
        return {'ok': False,
                'error': '应用失败并已回滚: %s: %s' % (type(exc).__name__, exc),
                'backup': bname}

    started, serr, deferred = bring_back(was_running, restart)
    if serr:
        errors.append(serr)

    if started:
        note = '配置已写入，服务已重启。'
    elif deferred:
        note = '配置已写入。后面还有排队的写入，服务将在队列处理完后统一启动。'
    elif not q_was_running:
        note = '配置已写入。服务原本未运行，配置将在下次启动时生效。'
    else:
        note = '配置已写入，但服务未能启动，请检查日志。'

    return {'ok': True, 'backup': bname, 'applied': applied, 'errors': errors,
            'restarted': started, 'deferred': deferred,
            'wasRunning': q_was_running, 'note': note}


@serialized
def apply_raw(xml: str, restart: bool = True) -> dict:
    xml, patched = ensure_charles_header(xml)
    err = xml_error(xml)
    if err:
        prefix = '补齐 charles 处理指令后 XML 非法' if patched else 'XML 校验失败'
        return {'ok': False, 'error': '%s: %s' % (prefix, err)}

    was_running = (svc_active() == 'active')
    q_was_running = _QUEUE_WAS_RUNNING
    if was_running:
        rc, out = svc('stop')
        if rc != 0:
            svc('start')
            return {'ok': False, 'error': '停止服务失败: %s' % out.strip()}
        wait_stopped()

    bname = backup('raw')
    try:
        write_cfg(xml)
        # 逐字节比对，防 Charles 关闭钩子的延迟回写覆盖。
        if read_cfg() != xml:
            wait_stopped()
            write_cfg(xml)
        if read_cfg() != xml:
            bring_back(was_running, restart)
            return {'ok': False, 'error': '写后被外部覆盖，磁盘内容与提交内容不一致',
                    'backup': bname}
    except (OSError, ValueError) as exc:
        shutil.copy2(os.path.join(BAK_DIR, bname), CFG)
        bring_back(was_running, True)
        return {'ok': False, 'error': str(exc), 'backup': bname}

    started, serr, deferred = bring_back(was_running, restart)
    return {'ok': True, 'backup': bname, 'restarted': started, 'deferred': deferred,
            'wasRunning': q_was_running, 'warn': serr}


@serialized
def restore(name: str, restart: bool = True) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_\-\.]+', name or ''):
        return {'ok': False, 'error': '非法备份名'}
    src = os.path.join(BAK_DIR, name)
    if not os.path.isfile(src):
        return {'ok': False, 'error': '备份不存在: %s' % name}
    with open(src, encoding='utf-8') as fh:
        want = fh.read()
    err = xml_error(want)
    if err:
        return {'ok': False, 'error': '备份文件本身 XML 非法: %s' % err}

    was_running = (svc_active() == 'active')
    q_was_running = _QUEUE_WAS_RUNNING
    if was_running:
        rc, out = svc('stop')
        if rc != 0:
            svc('start')
            return {'ok': False, 'error': '停止服务失败: %s' % out.strip()}
        wait_stopped()

    backup('pre-restore')
    try:
        write_cfg(want)
        if read_cfg() != want:
            wait_stopped()
            write_cfg(want)
        if read_cfg() != want:
            bring_back(was_running, restart)
            return {'ok': False, 'error': '写后被外部覆盖，磁盘内容与备份不一致'}
    except (OSError, ValueError) as exc:
        bring_back(was_running, True)
        return {'ok': False, 'error': str(exc)}

    started, serr, deferred = bring_back(was_running, restart)
    return {'ok': True, 'restored': name, 'restarted': started,
            'deferred': deferred, 'wasRunning': q_was_running, 'warn': serr}


# --------------------------------------------------------------------------- #
# 只读信息
# --------------------------------------------------------------------------- #
def state() -> dict:
    return {
        'service': svc_active(),
        'pid': charles_pid(),
        'configPath': CFG,
        'configSize': os.path.getsize(CFG) if os.path.exists(CFG) else 0,
        'serverTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def logs(lines: int = 120) -> str:
    try:
        n = max(1, min(int(lines), 5000))
    except (TypeError, ValueError):
        n = 120
    _rc, out = sh(['sudo', '-n', 'journalctl', '-u', SVC, '-n', str(n),
                   '--no-pager', '-o', 'short-iso'], timeout=30)
    return out


def help_names() -> list[str]:
    """charles.jar 里自带的官方帮助页名单。

    名单是**扫出来的**，不是硬编码的 —— 升级 Charles 后帮助页会增减，
    硬编码一份名单等于给自己埋一个「点了 404」的按钮。
    """
    import zipfile
    try:
        with zipfile.ZipFile(settings.charles_jar) as z:
            return sorted(
                n.rsplit('/', 1)[-1][:-5]
                for n in z.namelist()
                if n.startswith('com/xk72/charles/help/') and n.endswith('.html')
            )
    except Exception:                             # noqa: BLE001
        return []


def help_html(name: str) -> str | None:
    """从 charles.jar 提取官方帮助页。name 只允许字母数字。

    ★ name 必须白名单校验：它会被拼进 zip 里的路径。虽然 zipfile 本身
      不做路径穿越，但一旦这里换成按文件系统取（比如解包到磁盘再读），
      `../../etc/passwd` 这种名字立刻就是目录穿越。先卡死字符集，
      比事后补救便宜。
    """
    import zipfile
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', name or ''):
        return None
    try:
        with zipfile.ZipFile(settings.charles_jar) as z:
            return z.read('com/xk72/charles/help/%s.html' % name).decode(
                'utf-8', 'replace')
    except KeyError:
        return None
    except Exception:                             # noqa: BLE001
        # jar 不存在 / 不是 zip / 读不了 —— 都当「没有这一页」，
        # 路由层会回 404 而不是 500。
        return None


def ca_info() -> dict:
    """根证书现场。

    headless 下没有「SSL 代理设置 / 根证书」面板，这里是唯一能在界面上
    看到根证书状态的地方。

    ★ 路径以实测为准：证书在 `~/.charles/ca/charles-proxy-ssl-proxying-certificate.*`。
      旧栈找的是 `~/.charles/ca.pem` —— 那在当前部署里**不存在**，
      于是「有没有证书」永远回 exists:false，是个只会误导人的假兜底。
    """
    import hashlib
    cands = [
        str(settings.charles_ca_pem),
        str(settings.charles_ca_cer),
    ]
    info: dict = {'paths': [], 'found': None, 'sha256': '', 'size': 0,
                  'dir': str(settings.charles_ca_dir)}
    for p in cands:
        if os.path.isfile(p):
            info['paths'].append(p)
            if info['found'] is None:
                with open(p, 'rb') as fh:
                    blob = fh.read()
                info['found'] = p
                info['sha256'] = hashlib.sha256(blob).hexdigest().upper()
                info['size'] = len(blob)
    return info


# --------------------------------------------------------------------------- #
# 授权（Charles 注册）
# --------------------------------------------------------------------------- #
# 授权就写在配置里这一块：
#     <registrationConfiguration>
#       <name>your-license-name</name>
#       <key>0000000000000000</key>
#     </registrationConfiguration>
# 用**文本块替换**而不是 ElementTree 整树重序列化 —— 后者会把整个 14000 字节的
# 配置重新排版，等于顺手改了一堆用户没动过的东西（diff 里全是噪声，
# 出问题也无从判断是谁改的）。只碰目标块，其余逐字节保持原样。
_LIC_RE = re.compile(r'[ \t]*<registrationConfiguration>.*?</registrationConfiguration>',
                     re.S)


def _mask_key(key: str) -> str:
    if not key:
        return ''
    if len(key) <= 10:
        return key[:2] + '*' * max(0, len(key) - 2)
    return '%s…%s' % (key[:6], key[-4:])


def license_info() -> dict:
    """当前 Charles 授权状态。

    ★ 从**配置**里读，不是问进程：headless 下没有注册面板，进程也没暴露
      授权查询接口，`~/.charles.config` 的 `<registrationConfiguration>`
      是唯一事实源。

    ★ 授权码只回掩码：这是给界面看的，完整 key 没必要在 HTTP 响应里来回传
      （日志、浏览器历史、截图都会留痕）。
    """
    out = {'registered': False, 'name': '', 'keyMasked': '',
           'configPath': CFG, 'configExists': os.path.exists(CFG)}
    if not out['configExists']:
        out['error'] = '配置文件不存在：%s' % CFG
        return out
    try:
        root = ET.fromstring(read_cfg())
    except Exception as exc:                       # noqa: BLE001
        out['error'] = '配置解析失败：%s' % exc
        return out
    node = root.find('.//registrationConfiguration')
    if node is None:
        return out
    name = (node.findtext('name') or '').strip()
    key = (node.findtext('key') or '').strip()
    out['registered'] = bool(name and key)
    out['name'] = name
    out['keyMasked'] = _mask_key(key)
    return out


@serialized
def apply_license(name: str, key: str, restart: bool = True) -> dict:
    """把授权写进配置。空值拒绝 —— 免得把现有授权洗掉。

    ★ 走 `apply_raw` 同一条停-改-起通道：Charles 在 JVM 关闭钩子里用内存配置
      覆盖 `~/.charles.config`，不等进程消失就写，等于白写。
    ★ 自己也要 `@serialized`：读-改-写必须整体在锁里，否则两个并发请求
      会各自基于旧内容算出新内容，后写的把先写的盖掉。
      `serialized` 用线程局部 depth 计数，嵌套调用只算一次队尾 —— 所以
      这里套 `apply_raw` 不会把队列状态算乱。
    """
    name = (name or '').strip()
    key = (key or '').strip()
    if not name or not key:
        return {'ok': False, 'error': 'name 与 key 都不能为空（空值会把现有授权洗掉）'}
    if not os.path.exists(CFG):
        return {'ok': False, 'error': '配置文件不存在：%s' % CFG}
    from xml.sax.saxutils import escape
    block = ('<registrationConfiguration>\n'
             '    <name>%s</name>\n'
             '    <key>%s</key>\n'
             '  </registrationConfiguration>' % (escape(name), escape(key)))
    cur = read_cfg()
    if _LIC_RE.search(cur):
        new = _LIC_RE.sub(lambda _m: '  ' + block, cur, count=1)
    else:
        # 没有这一块就插在 <configuration> 开头 —— XStream 不要求顺序。
        m = re.search(r'<configuration[^>]*>', cur)
        if not m:
            return {'ok': False, 'error': '配置里找不到 <configuration> 根节点'}
        new = cur[:m.end()] + '\n  ' + block + cur[m.end():]
    res = apply_raw(new, restart=restart)
    if res.get('ok'):
        res['license'] = license_info()
    return res
