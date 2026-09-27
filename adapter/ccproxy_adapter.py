#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CCProxy 管理协议适配层 —— 让一花卡密系统把账号下发到本机花瓶链路上。

## 为什么需要它

一花卡密系统的「服务器列表」每一行是一台 CCProxy 服务器，它按 CCProxy 的 HTTP
管理协议说话（`includes/function.php`）：

    读账号  queryuserall()  GET  /account   Authorization: Basic base64("admin:"+pwd)
    下发    AddUser()       POST /account   add=1&autodisable=1&...&username=..&password=..
    编辑    UserUpdate()    POST /account   edit=1&...&userid=<旧用户名>
    删除    IDelUser()      POST /account   delete=1&userid=<用户名>

而花瓶（Charles）不实现这套协议，也没有「账号」这个对象（配置里 username/password
零命中，客户端访问控制只有 IP 段白名单）。所以卡密的账号下发链路是断的。

本服务补上这一层：

    一花卡密系统 --CCProxy管理协议--> 本服务 :8893 /account
                                        |
                                        +--> 账号库 (SQLite)，带到期时间
                                        |
    移动客户端 --HTTP代理(需账号) :8893 --> 本服务 --> 外网
    移动客户端 --SOCKS5(需账号)  :8892 --> 本服务 --> 外网
                                               |
                          +--------------------+--------------------+
                          |                                         |
                   普通 HTTP: 绝对URI直接喂花瓶 8888         CONNECT: 花瓶 CONNECT 隧道
                   （花瓶以正向代理身份解析/抓包）          （失败且 fallback_direct 则直连）

这样：卡密系统代码一行不改；账号真实存在、到期真实断开；花瓶继续只干抓包/映射/验证页。

## 兼容性上最容易踩的两个坑（都是从源码里读出来的，不是猜的）

1. **每个 `<input>` 必须独占一行。**
   源码的正则是 `/<input .* name="username" .* value="(.*?)"/ui`，PCRE 的 `.`
   默认不匹配换行。把多个 input 挤在一行，贪婪的 `.*` 会横跨整个行，
   结果 N 个账号只解析出 1 个。

2. **复选框 `checked` 的位置是硬编码的字符下标。**
   源码这样判断是否勾选：
       strripos(str_replace(['<','>','/'], '', $m), "checked") != "46"
   `strripos` 返回位置，再和**字符串** "46" 比较。也就是说，把 `<` `>` `/` 去掉之后，
   "checked" 必须正好从下标 46（enable）或 51（usepassword / autodisable）开始。
   多一个空格、换个属性顺序，判断就全反。
   本文件里的 CANON_* 常量就是那三个唯一合法的字符串，启动时自检，防止格式漂移。

## 启动

    python3 ccproxy_adapter.py --config /opt/ccproxy-adapter/config.json
    python3 ccproxy_adapter.py --selftest        # 只跑协议自检，不开端口
"""
import argparse
import asyncio
import base64
import binascii
import collections
import hmac
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote_plus

# --------------------------------------------------------------------------
# 唯一合法的三个复选框字面量。改动这里等于改变协议，先跑 --selftest。
# --------------------------------------------------------------------------
CANON_CHECKBOX = '<input type="checkbox" name="%s" value="1" checked>'
CANON_CHECKBOX_OFF = '<input type="checkbox" name="%s" value="1">'
# 注意：name="%s" 与 value= 之间必须有两个空格。
# 源码正则形如 '<input .* name="username" .* value="(.*?)"'，
# 其中 ` name="username" ` 已吃掉一个空格，`.*` 可空但后面的 ` value=` 还要再吃一个，
# 因此单空格 -> 0 命中，两空格/三空格 -> 命中。已实测。
CANON_TEXT = '<input type="text" name="%s"  value="%s">'

# 源码里的魔数：剥掉 <> 后 "checked" 的起始下标
CHECKED_POS = {"enable": 46, "usepassword": 51, "autodisable": 51}

# 源码里的九条正则，逐条照抄（注意：没有 DOTALL，`.*` 不跨行）
RE_SPECS = [
    ("username", re.compile(r'<input .* name="username" .* value="(.*?)"', re.I), 1),
    ("password", re.compile(r'<input .* name="password" .* value="(.*?)"', re.I), 1),
    ("enable", re.compile(r'<input .* name="enable" .*', re.I), 0),
    ("usepassword", re.compile(r'<input .* name="usepassword" .*', re.I), 0),
    ("disabledate", re.compile(r'<input .* name="disabledate" .* value="(.*?)"', re.I), 1),
    ("disabletime", re.compile(r'<input .* name="disabletime" .* value="(.*?)"', re.I), 1),
    ("autodisable", re.compile(r'<input .* name="autodisable" .*', re.I), 0),
    ("connection", re.compile(r'<input .* name="connection" .* value="(.*?)"', re.I), 1),
    ("bandwidth", re.compile(r'<input .* name="bandwidth" .* value="(.*?)"', re.I), 1),
]

DEFAULT_CONFIG = {
    "admin_user": "admin",
    "admin_password": "changeme",
    # 主端口：同时承载 CCProxy 管理 API(/account) 和 HTTP 代理。
    # 一花的 server_list.cport 就填这个值。
    "admin_port": 8893,
    # 与 admin_port 相同则不再单独监听（真 CCProxy 就是共用一个口）
    "http_port": 8893,
    "socks_port": 8892,
    "chain_to_charles": True,
    "charles_host": "127.0.0.1",
    "charles_http_port": 8888,
    # 花瓶的 SOCKS5 端口。charles_upstream="socks5" 时走这个口。
    "charles_socks_port": 18889,
    # 上游走花瓶的哪个面：
    #   "socks5" —— 走花瓶 SOCKS5 口。这个口开了
    #               enableSOCKSTransparentHTTPProxying，对**所有端口**的明文
    #               HTTP 都做解析，远程映射/抓包在 :80 和 :8080 上都能命中。
    #   "http"   —— 走花瓶 HTTP 代理口发 CONNECT。花瓶只对「CONNECT 到 80 端口」
    #               的隧道做透明 HTTP 解析，其它端口当纯 TCP 隧道透传，
    #               远程映射不生效（:8080 这类非标端口探针会废掉）。
    "charles_upstream": "socks5",
    # 花瓶拒绝 CONNECT / 连不上时，回落到直连目标。
    # 关掉它意味着「花瓶说不通就整条请求失败」——除非你在做纯抓包环境，否则别关。
    "fallback_direct": True,
    # 这些目标端口不走花瓶，直接连。用于「有证书固定、被 MITM 就握手失败」的
    # 客户端。空列表 = 全部走花瓶。
    "charles_bypass_ports": [],
    # ---- 验证页主机（captive-portal 式处理）----
    # 现代浏览器（Chrome 115+ / UC / 夸克 / QQ）会把地址栏里的 http:// 自动升级成
    # https://。升级之后：
    #   明文 HTTP  → 花瓶能解析 → 远程映射生效 → 验证页 ✅
    #   TLS        → 花瓶要 MITM，就要客户端信任它的根证书，不装就是「无法连接」 ❌
    # 既然不能给客户端装证书，那就别让它走 HTTPS：对这里列出的主机，一旦嗅到
    # 客户端发来的是 TLS ClientHello，立刻断开，逼浏览器回落成明文 HTTP。
    # 明文一回来，远程映射就生效，验证页就出来了。
    "verify_hosts": ["m.baidu.com"],
    "verify_fail_tls": True,
    # ---- SOCKS5 UDP ASSOCIATE（cmd=0x03）----
    # 游戏的实时流量、浏览器的 QUIC(HTTP/3)、部分 DNS 全是 UDP。
    # 不开这一段，这些流量**根本进不来**：适配层收到 cmd=0x03 会回 0x07
    # Command not supported，客户端只能自己直连 —— 于是日志里只剩 TCP 的浏览器流量。
    # 注意：UDP **不走花瓶**（花瓶只做 TCP，没有 UDP 中继），这里是适配层自己转发。
    "udp_enabled": True,
    # 回给客户端的 UDP 中继地址。必须填**客户端能连上的公网 IP**。
    # 云主机是 NAT 的，网卡上是私网 IP（本机是 10.1.0.10），自动探测拿不到公网 IP，
    # 所以这里必须显式写公网地址。留空才回退到自动探测。
    "udp_public_host": "",
    # ★ 共享中继端口。**非 0 时所有关联复用这一个端口**，靠客户端源地址区分归属。
    #
    # 为什么默认走共享而不是端口池：云安全组对 UDP 的放行面极窄 —— 实测只有
    # 8888/8889/8890/7863/8081/8787 通，40000 整段 BLOCKED。一关联一端口时，
    # 服务端把 40000+ 报给客户端，客户端照发，包在云边缘就被丢了，服务端一个
    # 字节都收不到（实测 399 次关联 / 0 个数据包）。共享一个**已放行**端口是
    # 当前唯一能通的做法。
    #
    # 这里填的端口必须同时满足：
    #   1) 云安全组已放行该 UDP 端口
    #   2) 不与其它 UDP 服务冲突（TCP/UDP 端口号独立，和 8888 的 TCP 代理不冲突）
    "udp_relay_port": 8888,
    # 端口池模式（仅在 udp_relay_port=0 时生效）。需要云安全组**整段**放行才可用。
    "udp_relay_port_base": 40000,
    "udp_relay_port_count": 64,
    # UDP 关联空闲回收（秒）。0 = 不按空闲回收，只靠 TCP 控制连接关闭。
    "udp_idle_timeout": 120.0,
    # ★ TCP 控制连接断开后的宽限期（秒）。共享模式下客户端源地址认领只能
    # 按 IP + LIFO，同一出口 IP 并发多个关联时「哪条 TCP 配哪个 UDP 源」
    # 无法精确对应；TCP 一断就关 usock，会误杀另一个关联正在等的在途回包
    # （实测并发 10 丢 3）。留个宽限期兜底，让在途回包先发完。
    "udp_control_grace": 3.0,
    # ★ WPE（网页端封包编辑器）环形缓冲。单包最多记 64KB，超出打截断标记。
    "wpe_enabled": True,
    "wpe_buffer": 4000,
    "bind_host": "0.0.0.0",
    "db_path": "/opt/ccproxy-adapter/accounts.db",
    "log_path": "/opt/ccproxy-adapter/adapter.log",
    "require_proxy_auth": True,
    "connect_timeout": 10.0,
}

log = logging.getLogger("ccpx")


# ==========================================================================
# 账号库
# ==========================================================================
class AccountStore:
    """SQLite 账号库。

    只存事实，不存派生状态：到期是否生效每次现算，避免「改了 disabledate 但
    enabled 字段忘了同步」这种经典不一致。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS accounts (
        username     TEXT PRIMARY KEY,
        password     TEXT NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        autodisable  INTEGER NOT NULL DEFAULT 1,
        expire_at    INTEGER NOT NULL DEFAULT 0,
        connection   TEXT NOT NULL DEFAULT '-1',
        bw_up        TEXT NOT NULL DEFAULT '-1',
        bw_down      TEXT NOT NULL DEFAULT '-1',
        created_at   INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    # ---- 内部 ----
    @staticmethod
    def _now():
        return int(time.time())

    @staticmethod
    def _parse_expire(disabledate, disabletime):
        """把 CCProxy 的日期+时间两段拼成一个 unix 时间戳。"""
        d = (disabledate or "").strip()
        t = (disabletime or "").strip() or "00:00:00"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime("%s %s" % (d, t), fmt).timestamp())
            except ValueError:
                continue
        return 0

    @staticmethod
    def _fmt_expire(ts):
        if not ts:
            return "2037-12-31", "23:59:59"
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")

    def effective_enabled(self, row):
        """是否处于可用状态：开关打开，且（未开启自动禁用 或 未到期）。"""
        if not row["enabled"]:
            return False
        if row["autodisable"] and row["expire_at"] and self._now() > row["expire_at"]:
            return False
        return True

    # ---- 读 ----
    def list_all(self):
        return self.db.execute(
            "SELECT * FROM accounts ORDER BY created_at ASC, username ASC").fetchall()

    def get(self, username):
        return self.db.execute(
            "SELECT * FROM accounts WHERE username=?", (username,)).fetchone()

    def verify(self, username, password):
        """代理鉴权。返回 (ok, reason)。"""
        row = self.get(username)
        if row is None:
            return False, "账号不存在"
        if not hmac.compare_digest(str(row["password"]), str(password or "")):
            return False, "密码错误"
        if not row["enabled"]:
            return False, "账号已被禁用"
        if row["autodisable"] and row["expire_at"] and self._now() > row["expire_at"]:
            return False, "账号已到期"
        return True, "ok"

    # ---- 写 ----
    def add(self, username, password, disabledate, disabletime,
            autodisable=1, connection="-1", bw_up="-1", bw_down="-1", enabled=1):
        if self.get(username) is not None:
            return False, "账号已存在"
        self.db.execute(
            "INSERT INTO accounts (username,password,enabled,autodisable,expire_at,"
            "connection,bw_up,bw_down,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (username, password, enabled, autodisable,
             self._parse_expire(disabledate, disabletime),
             connection, bw_up, bw_down, self._now()))
        self.db.commit()
        return True, "已创建"

    def edit(self, old_username, username=None, password=None, disabledate=None,
             disabletime=None, autodisable=None, connection=None,
             bw_up=None, bw_down=None, enabled=None):
        row = self.get(old_username)
        if row is None:
            return False, "账号不存在"
        new = {
            "username": username or row["username"],
            "password": row["password"] if not password else password,
            "enabled": row["enabled"] if enabled is None else enabled,
            "autodisable": row["autodisable"] if autodisable is None else autodisable,
            "expire_at": row["expire_at"],
            "connection": row["connection"] if connection is None else connection,
            "bw_up": row["bw_up"] if bw_up is None else bw_up,
            "bw_down": row["bw_down"] if bw_down is None else bw_down,
        }
        if disabledate:
            new["expire_at"] = self._parse_expire(disabledate, disabletime)
        self.db.execute(
            "UPDATE accounts SET username=?,password=?,enabled=?,autodisable=?,"
            "expire_at=?,connection=?,bw_up=?,bw_down=? WHERE username=?",
            (new["username"], new["password"], new["enabled"], new["autodisable"],
             new["expire_at"], new["connection"], new["bw_up"], new["bw_down"],
             old_username))
        self.db.commit()
        return True, "已更新"

    def delete(self, username):
        cur = self.db.execute("DELETE FROM accounts WHERE username=?", (username,))
        self.db.commit()
        return (cur.rowcount or 0) > 0


# ==========================================================================
# 页面渲染 / 解析（解析器只为自检存在，线上不用它）
# ==========================================================================
def render_accounts(rows, store):
    """渲染成 CCProxy 那个 HTML 表单。每个 input 一行 —— 见文件头第 1 个坑。"""
    lines = []
    for r in rows:
        d, t = store._fmt_expire(r["expire_at"])
        on = store.effective_enabled(r)
        bw = "%s/%s" % (r["bw_up"], r["bw_down"])
        lines.append(CANON_TEXT % ("username", _esc(r["username"])))
        lines.append(CANON_TEXT % ("password", _esc(r["password"])))
        lines.append((CANON_CHECKBOX if on else CANON_CHECKBOX_OFF) % "enable")
        lines.append(CANON_CHECKBOX % "usepassword")
        lines.append(CANON_TEXT % ("disabledate", d))
        lines.append(CANON_TEXT % ("disabletime", t))
        lines.append((CANON_CHECKBOX if r["autodisable"] else CANON_CHECKBOX_OFF) % "autodisable")
        lines.append(CANON_TEXT % ("connection", _esc(r["connection"])))
        lines.append(CANON_TEXT % ("bandwidth", _esc(bw)))
    body = "\n".join(lines)
    return ("<!DOCTYPE html>\n<html><head><title>CCProxy</title></head><body>\n"
            "<form method=\"post\" action=\"/account\">\n"
            "%s\n</form>\n</body></html>\n" % body)


def _esc(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def parse_like_php(html):
    """照抄源码那九条正则 + 下标对齐逻辑，用来验证渲染结果能被正确解析。"""
    got = {}
    for name, rx, grp in RE_SPECS:
        got[name] = [m.group(grp) for m in rx.finditer(html)]

    users = got["username"]
    n = len(users)
    for name, _, _ in RE_SPECS:
        if len(got[name]) != n:
            raise AssertionError(
                "下标对齐失败：username %d 个，%s %d 个 —— 源码里它们用同一个 $key 取值，"
                "数量不等会取到 null" % (n, name, len(got[name])))

    out = []
    for k in range(n):
        if users[k] == "":
            continue
        # 源码：strripos(剥掉 <>/, "checked") != "<魔数>" ? 0 : 1
        def flag(key):
            s = re.sub(r'[<>/]', '', got[key][k])
            pos = s.rfind("checked")
            return 1 if pos == CHECKED_POS[key] else 0
        dd, tt = got["disabledate"][k], got["disabletime"][k]
        bw = got["bandwidth"][k].split("/")
        expire_at = AccountStore._parse_expire(dd, tt)
        out.append({
            "id": k,
            "user": users[k],
            "pwd": got["password"][k],
            "state": flag("enable"),
            "pwdstate": flag("usepassword"),
            "disabletime": "%s %s" % (dd, tt),
            "expire": 1 if int(time.time()) > expire_at else 0,
            "connection": got["connection"][k],
            "bandwidthup": bw[0],
            "bandwidthdown": bw[1] if len(bw) > 1 else "",
            "autodisable": flag("autodisable"),
        })
    return out


# ==========================================================================
# 自检
# ==========================================================================
def selftest():
    fails = []

    def check(name, cond, extra=""):
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    print("== 1. 复选框 checked 位置（源码用硬编码下标判断，位置错了开关就全反） ==")
    for key, want in CHECKED_POS.items():
        s = re.sub(r'[<>/]', '', CANON_CHECKBOX % key)
        pos = s.rfind("checked")
        check("%s 应为 %d，实际 %d" % (key, want, pos), pos == want)
    for key in CHECKED_POS:
        s = re.sub(r'[<>/]', '', CANON_CHECKBOX_OFF % key)
        check("%s 未勾选时不应出现 checked" % key, s.rfind("checked") == -1)

    print("\n== 2. 单行约束（`.*` 不跨行，挤一行会只解析出 1 个账号） ==")
    _s2 = _MemStore()
    _now2 = int(time.time())
    _s2.seed([dict(username="l%d" % i, password="aa12345", enabled=1, autodisable=1,
                   expire_at=_now2 + 86400, connection="-1", bw_up="-1", bw_down="-1")
              for i in range(3)])
    probe = render_accounts(_s2.list_all(), _s2)
    multi = [ln for ln in probe.splitlines() if ln.count("<input") > 1]
    n_inputs = probe.count("<input")
    check("渲染结果每个 input 独占一行", not multi and n_inputs == 27,
          "" if not multi else "有 %d 行挤了多个 input" % len(multi))

    print("\n== 3. 多账号往返（渲染 -> 源码等价解析） ==")
    store = _MemStore()
    now = int(time.time())
    store.seed([
        dict(username="alpha001", password="aa12345", enabled=1, autodisable=1,
             expire_at=now + 86400 * 30, connection="-1", bw_up="-1", bw_down="-1"),
        dict(username="beta002", password="bb12345", enabled=1, autodisable=1,
             expire_at=now - 86400, connection="5", bw_up="100", bw_down="200"),
        dict(username="gamma003", password="cc12345", enabled=0, autodisable=0,
             expire_at=now + 86400 * 365, connection="-1", bw_up="-1", bw_down="-1"),
    ])
    html = render_accounts(store.list_all(), store)
    got = parse_like_php(html)
    check("账号数 3", len(got) == 3, "实际 %d" % len(got))
    if len(got) == 3:
        check("alpha001 未到期且启用 -> state=1, expire=0",
              got[0]["state"] == 1 and got[0]["expire"] == 0,
              "state=%s expire=%s" % (got[0]["state"], got[0]["expire"]))
        check("beta002 已过期 -> expire=1", got[1]["expire"] == 1)
        check("beta002 带宽 100/200 正确拆分",
              got[1]["bandwidthup"] == "100" and got[1]["bandwidthdown"] == "200")
        check("gamma003 手工禁用 -> state=0", got[2]["state"] == 0)
        check("用户名顺序与写入一致",
              [g["user"] for g in got] == ["alpha001", "beta002", "gamma003"])
        check("密码解析正确", got[0]["pwd"] == "aa12345")

    print("\n== 4. 单行 vs 同行（证明坑 1 是真的） ==")
    one = CANON_TEXT % ("username", "a")
    two = one + one
    check("两个 input 挤在一行时，正则只能抓到 1 个",
          len(RE_SPECS[0][1].findall(two)) == 1,
          "抓到 %d 个" % len(RE_SPECS[0][1].findall(two)))
    two_nl = one + "\n" + one
    check("分成两行时能抓到 2 个",
          len(RE_SPECS[0][1].findall(two_nl)) == 2,
          "抓到 %d 个" % len(RE_SPECS[0][1].findall(two_nl)))

    print("\n== 4b. 空格宽度（单空格必须 0 命中，否则说明我们的字面量又漂了） ==")
    one_space = '<input type="text" name="username" value="alpha001">'
    check("单空格 -> 0 命中（源码要求至少两空格）",
          len(RE_SPECS[0][1].findall(one_space)) == 0,
          "实际 %d" % len(RE_SPECS[0][1].findall(one_space)))
    check("CANON_TEXT 两空格 -> 命中",
          len(RE_SPECS[0][1].findall(CANON_TEXT % ("username", "alpha001"))) == 1)

    print("\n== 5. 账号名/密码约束（源码 CheckStrChinese / CheckStrPwd） ==")
    rx_user = re.compile(r'^[A-Za-z0-9]+$')
    rx_pwd = re.compile(r'^(?![0-9]+$)(?![a-zA-Z]+$)[0-9A-Za-z_]{5,16}$')
    check("alpha001 合法用户名", bool(rx_user.match("alpha001")))
    check("aa12345 合法密码", bool(rx_pwd.match("aa12345")))
    check("纯数字密码被拒", not rx_pwd.match("TestPass1!"))
    check("纯字母密码被拒", not rx_pwd.match("abcdef"))

    print()
    if fails:
        print("自检失败 %d 项：%s" % (len(fails), ", ".join(fails)))
        return 1
    print("自检全部通过。")
    return 0


class _StubStore:
    _fmt_expire = staticmethod(lambda ts: ("2037-12-31", "23:59:59"))
    effective_enabled = staticmethod(lambda r: True)


class _MemStore:
    """自检用：内存里塞几行，接口和 AccountStore 的读侧一致。"""

    def __init__(self):
        self.rows = []

    def seed(self, items):
        for i, it in enumerate(items):
            it.setdefault("created_at", i)
            self.rows.append(it)

    def list_all(self):
        return self.rows

    def _fmt_expire(self, ts):
        return AccountStore._fmt_expire(ts)

    def effective_enabled(self, r):
        if not r["enabled"]:
            return False
        if r["autodisable"] and r["expire_at"] and int(time.time()) > r["expire_at"]:
            return False
        return True


# ==========================================================================
# HTTP 小工具
# ==========================================================================
async def read_http_head(reader, limit=65536):
    """读到 \\r\\n\\r\\n 为止，返回 (请求行, 头字典, 头之后多读到的字节, 原始头字节)。

    第三个返回值必须留着 —— read(4096) 常把 body 一起读进来，
    直接丢掉会让 Content-Length>0 的 POST 变成空 body（一花的 AddUser 就死在这）。

    第四个返回值（原始头字节，含结尾 CRLFCRLF）给 WPE 用：请求行和头在这里
    就被消费掉了，不留下原字节的话，抓包列表里「请求包」永远缺一半。
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            return None, None, b"", b""
        buf += chunk
        if len(buf) > limit:
            raise ValueError("请求头过大")
    head, _, rest = buf.partition(b"\r\n\r\n")
    raw_head = head + b"\r\n\r\n"
    lines = head.decode("latin-1").split("\r\n")
    request_line = lines[0]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
    return request_line, headers, rest, raw_head


def basic_auth(headers, key="authorization"):
    raw = headers.get(key)
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        dec = base64.b64decode(parts[1]).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    if ":" not in dec:
        return None
    u, _, p = dec.partition(":")
    return u, p


def http_response(code, reason, body=b"", ctype="text/html; charset=utf-8", extra=""):
    if isinstance(body, str):
        body = body.encode("utf-8")
    head = ("HTTP/1.0 %d %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n%s\r\n") % (code, reason, ctype, len(body), extra)
    return head.encode("latin-1") + body


def close_quietly(writer):
    try:
        writer.close()
    except Exception:
        pass


# ==========================================================================
# 前端分发：同一个端口既当 CCProxy 管理 API，又当 HTTP 代理
# ==========================================================================
class FrontService:
    """复刻真 CCProxy 的端口模型：管理页面和 HTTP 代理共用 cport。

    一花的 server_list.cport 同时被当成「通讯端口」和「代理端口」用
    （fsockopen 打 /account，用户侧连同一个口当代理），所以这里必须合流：
      - 路径 /account 或 /status  -> 管理 API（admin:password 鉴权）
      - 其它一切                   -> HTTP 代理（账号:密码 鉴权）
    """

    ADMIN_PATHS = ("/account", "/status", "/live")

    # WPE 网页端接口是 /wpe/xxx 前缀，单独走前缀匹配。
    ADMIN_PREFIXES = ("/wpe",)

    def __init__(self, cfg, store, stats=None):
        self.admin = AdminService(cfg, store, stats)
        self.proxy = ProxyService(cfg, store, stats)

    async def __call__(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            request_line, headers, rest, _raw = await read_http_head(reader)
            if request_line is None:
                return
            parts = request_line.split()
            path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
            if path in self.ADMIN_PATHS or path.startswith(self.ADMIN_PREFIXES):
                await self.admin.handle(reader, writer, request_line, headers, rest, peer)
            else:
                await self.proxy.handle_http(reader, writer, request_line, headers,
                                             rest, peer, _raw)
        except Exception as exc:
            log.warning("前端分发异常 %s: %s", peer, exc)
        finally:
            close_quietly(writer)


# ==========================================================================
# 上游（花瓶）
# ==========================================================================
async def connect_upstream_socks(cfg, target_host, target_port):
    """连到花瓶的 SOCKS5 口，让它去连目标。返回 (reader, writer)。

    为什么上游要分两个面：
      花瓶的 **HTTP 代理口**只对「CONNECT 到 80 端口」的隧道做透明 HTTP 解析，
      其它端口（比如 8080）当成纯 TCP 隧道透传 —— 远程映射、抓包全都不生效。
      而花瓶的 **SOCKS5 口**开了 enableSOCKSTransparentHTTPProxying，对所有端口
      的明文 HTTP 都做解析，实测 :80 / :8080 都能命中远程映射。

    所以默认走这条，行为最一致；charles_upstream="http" 可以切回旧行为。
    """
    r, w = await asyncio.wait_for(
        asyncio.open_connection(cfg["charles_host"], cfg["charles_socks_port"]),
        timeout=cfg["connect_timeout"])
    try:
        # 协商：只报「无认证」。花瓶这个口不开用户名/密码认证。
        w.write(b"\x05\x01\x00")
        await w.drain()
        resp = await asyncio.wait_for(r.readexactly(2),
                                      timeout=cfg["connect_timeout"])
        if resp != b"\x05\x00":
            raise ConnectionError("花瓶 SOCKS5 协商失败: %r" % resp)

        # 目标一律用域名交给花瓶远端解析 —— 抓包和映射匹配看到的都是域名。
        try:
            host_b = target_host.encode("idna")
        except (UnicodeError, LookupError):
            host_b = target_host.encode("latin-1", "replace")
        if not host_b or len(host_b) > 255:
            raise ConnectionError("目标域名非法或过长: %r" % target_host)

        w.write(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                + int(target_port).to_bytes(2, "big"))
        await w.drain()

        head = await asyncio.wait_for(r.readexactly(4),
                                      timeout=cfg["connect_timeout"])
        if head[0] != 5 or head[1] != 0:
            raise ConnectionError("花瓶 SOCKS5 拒绝: code=%d" % head[1])
        atyp = head[3]
        if atyp == 1:      # IPv4
            await asyncio.wait_for(r.readexactly(6), timeout=cfg["connect_timeout"])
        elif atyp == 3:    # 域名
            ln = (await asyncio.wait_for(r.readexactly(1),
                                         timeout=cfg["connect_timeout"]))[0]
            await asyncio.wait_for(r.readexactly(ln + 2),
                                   timeout=cfg["connect_timeout"])
        elif atyp == 4:    # IPv6
            await asyncio.wait_for(r.readexactly(18), timeout=cfg["connect_timeout"])
        else:
            raise ConnectionError("花瓶 SOCKS5 返回未知地址类型 %d" % atyp)
    except Exception:
        w.close()
        raise
    return r, w


async def connect_upstream(cfg, target_host, target_port):
    """连到花瓶，并让花瓶去连目标。返回 (reader, writer)，或抛异常。

    走花瓶而不是直连，是为了让抓包、远程映射、验证页这些还都生效。
    具体走花瓶的哪个面由 charles_upstream 决定（见 DEFAULT_CONFIG 注释）。
    """
    if not cfg["chain_to_charles"]:
        return await asyncio.open_connection(target_host, target_port)

    if ((cfg.get("charles_upstream") or "socks5").lower() == "socks5"
            and cfg.get("charles_socks_port")):
        return await connect_upstream_socks(cfg, target_host, target_port)

    # ---- 旧路径：花瓶 HTTP 代理口 + CONNECT ----
    r, w = await asyncio.wait_for(
        asyncio.open_connection(cfg["charles_host"], cfg["charles_http_port"]),
        timeout=cfg["connect_timeout"])
    req = ("CONNECT %s:%d HTTP/1.0\r\nHost: %s:%d\r\n\r\n"
           % (target_host, target_port, target_host, target_port))
    w.write(req.encode("latin-1"))
    await w.drain()

    line = await asyncio.wait_for(r.readline(), timeout=cfg["connect_timeout"])
    if not line:
        w.close()
        raise ConnectionError("花瓶没有应答")
    status = line.decode("latin-1", "replace").strip()
    # 吃掉剩余响应头
    while True:
        ln = await asyncio.wait_for(r.readline(), timeout=cfg["connect_timeout"])
        if ln in (b"\r\n", b"\n", b""):
            break
    if " 200" not in status:
        w.close()
        raise ConnectionError("花瓶拒绝 CONNECT: %s" % status)
    return r, w


async def open_via_charles_plain(cfg):
    """只连花瓶的 HTTP 代理口，**不做 CONNECT**。

    普通 HTTP（非 CONNECT）请求该走这条：花瓶以正向代理身份收下绝对 URI
    请求行，抓包、映射、断点全都生效。

    为什么不能对普通 HTTP 也用 CONNECT：CONNECT 建的是「到目标 80 端口的裸
    隧道」，花瓶在这条隧道上不再解析 HTTP，也就谈不上抓包；而且把绝对 URI
    原样喂给隧道对面的源站，行为不可控（实测拿回 502）。
    """
    return await asyncio.wait_for(
        asyncio.open_connection(cfg["charles_host"], cfg["charles_http_port"]),
        timeout=cfg["connect_timeout"])


async def connect_target(cfg, host, port):
    """拿到一条通往 host:port 的连接，返回 (reader, writer, via)。

    via 是 "charles" 或 "direct"，只用于日志——运维时要能一眼看出这条流量
    到底有没有过花瓶（过了才谈得上抓包）。
    """
    use_charles = cfg["chain_to_charles"] and port not in set(
        cfg.get("charles_bypass_ports") or ())
    if use_charles:
        try:
            r, w = await connect_upstream(cfg, host, port)
            return r, w, "charles"
        except Exception as exc:
            if not cfg.get("fallback_direct", True):
                raise
            log.warning("花瓶不可用 %s:%d (%s) -> 回落直连", host, port, exc)
    r, w = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=cfg["connect_timeout"])
    return r, w, "direct"


def classify_payload(data):
    """按首字节猜客户端把这条连接当什么用。

    这是判「浏览器有没有把非标端口也升级成 HTTPS」的唯一硬证据：
    首字节 0x16 = TLS ClientHello（客户端在做 HTTPS 握手）；
    以 HTTP 方法名开头 = 明文 HTTP。
    """
    if not data:
        return "empty"
    for m in (b"GET ", b"POST", b"HEAD", b"PUT ", b"OPTI", b"DELE", b"CONN",
              b"PATC", b"TRAC"):
        if data.startswith(m):
            return "HTTP"
    if data[0] == 0x16:
        return "TLS"
    if data[0] == 0x05:
        return "SOCKS5"
    return "other(0x%02x)" % data[0]


def extract_sni(data):
    """从 TLS ClientHello 里抠出 SNI。

    ClientHello 在 TLS 1.2/1.3 里都是**明文**，即使整条连接是加密的。
    适配层不做 MITM（decryptSSL=false 全透传），所以这是**唯一**能看到
    「加密流量到底在找哪个域名」的地方。

    为什么必须要它：日志里 443 端口的记录有 98 条目标是**纯 IP**，光看
    `157.148.79.10:443` 这种行，谁也不知道背后是什么服务。把 SNI 解出来，
    才能还原成域名，也才能判断这些连接到底是浏览器、SDK 还是游戏进程。

    结构（RFC8446 §4.1.2 / RFC6066 §3）：
      TLS record:  type(1)=0x16  ver(2)  len(2)
      handshake:   type(1)=0x01(ClientHello)  len(3)
      client_version(2)  random(32)
      session_id:  len(1) + bytes
      cipher_suites: len(2) + bytes
      compression: len(1) + bytes
      extensions:  len(2) + [ type(2) len(2) data ]
        其中 type=0x0000 是 server_name：
          list_len(2)  name_type(1)=0  name_len(2)  name

    解不出来一律返回 None，绝不让解析异常冒到 relay 里。
    """
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        rec_len = int.from_bytes(data[3:5], "big")
        body = data[5:5 + rec_len]
        if len(body) < 4 or body[0] != 0x01:
            return None
        p = 4                      # 跳过 handshake header
        p += 2                     # client_version
        p += 32                    # random
        if p >= len(body):
            return None
        sid_len = body[p]
        p += 1 + sid_len
        if p + 2 > len(body):
            return None
        cs_len = int.from_bytes(body[p:p + 2], "big")
        p += 2 + cs_len
        if p + 1 > len(body):
            return None
        comp_len = body[p]
        p += 1 + comp_len
        if p + 2 > len(body):
            return None
        ext_total = int.from_bytes(body[p:p + 2], "big")
        p += 2
        end = min(p + ext_total, len(body))
        while p + 4 <= end:
            etype = int.from_bytes(body[p:p + 2], "big")
            elen = int.from_bytes(body[p + 2:p + 4], "big")
            p += 4
            if p + elen > end:
                break
            if etype == 0x0000:
                ed = body[p:p + elen]
                if len(ed) >= 5:
                    nlen = int.from_bytes(ed[3:5], "big")
                    name = ed[5:5 + nlen]
                    if not name:
                        return None
                    try:
                        return name.decode("ascii").lower()
                    except UnicodeDecodeError:
                        return None
                return None
            p += elen
        return None
    except Exception:
        return None


def parse_socks5_udp(data):
    """剥掉 SOCKS5 UDP 请求头（RFC1928 §7）。

    结构：RSV(2) FRAG(1) ATYP(1) DST.ADDR DST.PORT DATA

    返回 (host, port, payload)。下面几种情况返回 None：
      - 数据短于最小长度
      - RSV 不是 0x0000
      - FRAG != 0（分片，我们不支持，直接丢）
      - ATYP 不认识
    """
    if len(data) < 4:
        return None
    if data[0] != 0x00 or data[1] != 0x00:
        return None
    if data[2] != 0x00:
        return None
    atyp = data[3]
    i = 4
    if atyp == 0x01:
        if len(data) < i + 6:
            return None
        host = ".".join(str(b) for b in data[i:i + 4])
        i += 4
    elif atyp == 0x03:
        if len(data) < i + 1:
            return None
        ln = data[i]
        i += 1
        if len(data) < i + ln + 2:
            return None
        host = data[i:i + ln].decode("utf-8", "replace")
        i += ln
    elif atyp == 0x04:
        if len(data) < i + 18:
            return None
        host = ":".join("%02x" % b for b in data[i:i + 16])
        i += 16
    else:
        return None
    port = int.from_bytes(data[i:i + 2], "big")
    i += 2
    return host, port, data[i:]


def build_socks5_udp(host, port, payload):
    """把目标回包按 SOCKS5 UDP 格式包好（RSV+FRAG+ATYP+ADDR+PORT+DATA）。

    只接受 IPv4 / IPv6 字面量：回包源地址一定是 IP，不会是域名。
    """
    try:
        packed = socket.inet_aton(host)
        atyp = b"\x01"
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, host)
            atyp = b"\x04"
        except OSError:
            return None
    return (b"\x00\x00\x00" + atyp + packed
            + int(port).to_bytes(2, "big") + payload)


# ==========================================================================
# WPE —— 网页端封包编辑器
# ==========================================================================
def _pkt_ascii(data):
    """bytes -> 可打印 ASCII，不可打印字符用 . 顶替（WPE 的 ASCII 栏）。"""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


# ---------------- 自动解析：把裸字节认成「什么协议、在说什么」 ----------------
# 只做定长、有界的浅解析，绝不为了解析去拼整个流 —— 每个包都要过这里，
# 复杂度必须 O(包长)，且解析失败一律降级成 BIN/UDP，不影响抓包本身。
_HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"OPTIONS ",
                 b"PATCH ", b"TRACE ", b"CONNECT ", b"PRI * HTTP/2.0")
_DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX",
              16: "TXT", 28: "AAAA", 33: "SRV", 41: "OPT", 65: "HTTPS"}
_TLS_HS = {1: "ClientHello", 2: "ServerHello", 11: "Certificate", 12: "ServerKeyExchange",
           13: "CertificateRequest", 14: "ServerHelloDone", 15: "CertificateVerify",
           16: "ClientKeyExchange", 20: "Finished"}
_TLS_VER = {0x0300: "SSL3.0", 0x0301: "TLS1.0", 0x0302: "TLS1.1",
            0x0303: "TLS1.2", 0x0304: "TLS1.3"}
# ClientHello 扩展表。结构面板要逐条列出扩展，认不出的显示原始编号。
_TLS_EXT = {0: "server_name", 5: "status_request", 10: "supported_groups",
            11: "ec_point_formats", 13: "signature_algorithms", 16: "application_layer_protocol_negotiation",
            18: "signed_certificate_timestamp", 21: "padding", 23: "extended_master_secret",
            35: "session_ticket", 43: "supported_versions", 45: "psk_key_exchange_modes",
            51: "key_share", 65281: "renegotiation_info"}


def _tls_sni(buf):
    """从 ClientHello 里抠 SNI。TLS1.2/1.3 的 ClientHello 都是明文。"""
    try:
        if len(buf) < 6 or buf[0] != 0x16:
            return ""
        p = 5 + 4                                  # record 头 + handshake 头
        p += 2 + 32                                # version + random
        sid = buf[p]; p += 1 + sid                 # session id
        cs = int.from_bytes(buf[p:p + 2], "big"); p += 2 + cs
        cm = buf[p]; p += 1 + cm                   # compression
        ext_len = int.from_bytes(buf[p:p + 2], "big"); p += 2
        end = min(len(buf), p + ext_len)
        while p + 4 <= end:
            et = int.from_bytes(buf[p:p + 2], "big")
            el = int.from_bytes(buf[p + 2:p + 4], "big")
            if et == 0 and p + 4 + el <= end:      # server_name
                q = p + 4 + 2 + 1                  # list len + name type
                nl = int.from_bytes(buf[q:q + 2], "big")
                return buf[q + 2:q + 2 + nl].decode("ascii", "replace")
            p += 4 + el
    except Exception:
        pass
    return ""


def _dns_name(buf, p):
    """DNS 名字解压。返回 (名字, 该名字之后的偏移)。指针最多跳 8 次防环。"""
    parts = []
    hops = 0
    nxt = None
    while p < len(buf) and hops < 8:
        ln = buf[p]
        if ln == 0:
            p += 1
            if nxt is None:
                nxt = p
            break
        if ln & 0xC0 == 0xC0:
            if p + 1 >= len(buf):
                break
            if nxt is None:
                nxt = p + 2                    # 指针本身占 2 字节，名字在此结束
            p = ((ln & 0x3F) << 8) | buf[p + 1]
            hops += 1
            continue
        if p + 1 + ln > len(buf):
            break
        parts.append(buf[p + 1:p + 1 + ln].decode("ascii", "replace"))
        p += 1 + ln
    return ".".join(parts), (nxt if nxt is not None else p)


def _dns_analyze(buf, direction):
    """DNS 报文摘要：查询问什么 / 应答答什么（A/AAAA/CNAME 带出结果）。"""
    if len(buf) < 12:
        return None
    txid = int.from_bytes(buf[0:2], "big")
    flags = int.from_bytes(buf[2:4], "big")
    qd = int.from_bytes(buf[4:6], "big")
    an = int.from_bytes(buf[6:8], "big")
    if qd == 0 or qd > 8 or an > 32:
        return None
    name, p = _dns_name(buf, 12)
    if not name or "." not in name or p + 4 > len(buf):
        return None
    qtype = int.from_bytes(buf[p:p + 2], "big")
    tname = _DNS_TYPES.get(qtype, "T%d" % qtype)
    if not (flags & 0x8000):
        return "DNS", "查询 %s? %s (txid 0x%04x)" % (tname, name, txid)
    q = p + 4                                  # 跳过 QTYPE + QCLASS
    out = []
    for _ in range(min(an, 8)):
        if q >= len(buf):
            break
        _an, q = _dns_name(buf, q)
        if q + 10 > len(buf):
            break
        atype = int.from_bytes(buf[q:q + 2], "big")
        rdlen = int.from_bytes(buf[q + 8:q + 10], "big")
        rd = buf[q + 10:q + 10 + rdlen]
        if atype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in rd))
        elif atype == 28 and rdlen == 16:
            out.append(":".join("%02x%02x" % (rd[i], rd[i + 1]) for i in range(0, 16, 2)))
        elif atype == 5 and rd:
            cn, _ = _dns_name(buf, q + 10)
            out.append(cn or "CNAME")
        q += 10 + rdlen
    return "DNS", "应答 %s %s -> %s (txid 0x%04x)" % (
        tname, name, ", ".join(out) if out else "(无 A/AAAA/CNAME)", txid)


def _analyze(proto, direction, data):
    """(应用协议, 一行摘要)。识别不出就回 ("BIN"/"UDP", "")，绝不猜。"""
    if not data:
        return "", ""
    try:
        if proto == "udp":
            # DNS 明文：端口 53 或 结构自证
            r = _dns_analyze(data, direction)
            if r:
                return r
            return "UDP", "%d 字节 UDP 载荷" % len(data)

        b = data
        if b[0] == 0x16 and len(b) > 5 and b[1] == 0x03:
            hs = b[5] if len(b) > 5 else 0
            ver = int.from_bytes(b[9:11], "big") if len(b) > 11 else 0
            name = _TLS_HS.get(hs, "HS%d" % hs)
            if hs == 1:
                sni = _tls_sni(b)
                return "TLS", "ClientHello %s SNI=%s len=%d" % (
                    _TLS_VER.get(ver, "0x%04x" % ver), sni or "(无)", len(b))
            if hs == 2:
                return "TLS", "ServerHello %s len=%d" % (_TLS_VER.get(ver, "0x%04x" % ver), len(b))
            return "TLS", "%s len=%d" % (name, len(b))
        if b[0] in (0x14, 0x15, 0x17) and len(b) > 2 and b[1] == 0x03:
            kind = {0x14: "ChangeCipherSpec", 0x15: "Alert",
                    0x17: "ApplicationData"}.get(b[0], "TLS记录")
            return "TLS", "%s len=%d（已加密）" % (kind, len(b))

        for m in _HTTP_METHODS:
            if b.startswith(m):
                line = b.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                host = ""
                for h in b.split(b"\r\n")[1:9]:
                    if h.lower().startswith(b"host:"):
                        host = h[5:].strip().decode("latin-1", "replace")
                        break
                return "HTTP", (line[:120] + ("  Host=" + host if host else ""))
        if b.startswith(b"HTTP/1."):
            return "HTTP", b.split(b"\r\n", 1)[0].decode("latin-1", "replace")[:120]

        if b[0] == 0x05 and len(b) >= 3:
            return "SOCKS5", "SOCKS5 报文 ver=%d cmd/rep=0x%02x" % (b[0], b[1])
        if b[0] & 0xF0 == 0x10 and len(b) >= 2:
            return "MQTT", "MQTT 控制报文 type=%d" % (b[0] >> 4)
        return "BIN", "%d 字节二进制" % len(b)
    except Exception:
        return "BIN", ""


# --------------------------------------------------------------------------- #
# 结构解析：把裸字节拆成字段，给详情页的「结构」面板
# --------------------------------------------------------------------------- #
#
# 为什么必须单独做这个 —— 原来的详情页只有 hex + 一行摘要。摘要能告诉你
# 「这是个 HTTP 请求」，但看不出请求行在哪几个字节、Host 头值是什么、
# body 从哪个偏移开始、DNS 的 TTL 是多少、TLS 有几个扩展。
# 用户要的「查看所有的结构」指的就是这个：**字段级**，而且每个字段都要带
# off/len，点一下能把 hex 里对应区间高亮 —— 结构和字节必须能对上，
# 不能是两套各说各话的展示。
#
# 铁律：只做定长/有界解析，认不出就退化成一条 BIN 记录，**绝不猜**。
# 字段总数有上限，超了如实报 truncated。

STRUCT_MAX_FIELDS = 400


def _is_hex(s):
    """字符串是不是合法十六进制（允许空格/换行）。空串也算合法（= 空字节）。"""
    s = (s or "").replace(" ", "").replace("\n", "").replace("\t", "")
    if len(s) % 2:
        return False
    try:
        bytes.fromhex(s)
        return True
    except Exception:                                 # noqa: BLE001
        return False


def _f(off, ln, name, value, note=""):
    """一行结构。off/len 是**相对整个载荷**的偏移，前端拿它去 hex 里高亮。"""
    return {"off": int(off), "len": int(ln), "name": name,
            "value": value, "note": note}


def _struct_http(data):
    """HTTP/1.x 请求或响应。返回 (fields, layers) 或 None。"""
    split = data.find(b"\r\n\r\n")
    head = data if split < 0 else data[:split]
    if not head:
        return None
    lines = head.split(b"\r\n")
    first = lines[0]
    fields = []
    layers = []

    is_req = False
    for m in _HTTP_METHODS:
        if first.startswith(m):
            is_req = True
            break
    if is_req:
        parts = first.split(b" ", 2)
        fields.append(_f(0, len(parts[0]), "方法", parts[0].decode("latin-1", "replace")))
        if len(parts) > 1:
            fields.append(_f(len(parts[0]) + 1, len(parts[1]), "请求目标",
                             parts[1].decode("latin-1", "replace")))
        if len(parts) > 2:
            fields.append(_f(len(parts[0]) + len(parts[1]) + 2, len(parts[2]),
                             "版本", parts[2].decode("latin-1", "replace")))
        layers.append({"name": "HTTP 请求行", "off": 0, "len": len(first)})
    elif first.startswith(b"HTTP/"):
        parts = first.split(b" ", 2)
        fields.append(_f(0, len(parts[0]), "版本", parts[0].decode("latin-1", "replace")))
        if len(parts) > 1:
            fields.append(_f(len(parts[0]) + 1, len(parts[1]), "状态码",
                             parts[1].decode("latin-1", "replace")))
        if len(parts) > 2:
            fields.append(_f(len(parts[0]) + len(parts[1]) + 2, len(parts[2]),
                             "原因短语", parts[2].decode("latin-1", "replace")))
        layers.append({"name": "HTTP 状态行", "off": 0, "len": len(first)})
    else:
        return None

    # 头部区。偏移要一行一行累加，不能只记行号 —— 前端要用它去 hex 里定位。
    pos = len(first) + 2
    n = 0
    for ln in lines[1:]:
        if not ln:
            break
        c = ln.find(b":")
        if c > 0:
            fields.append(_f(pos, c, ln[:c].decode("latin-1", "replace"),
                             ln[c + 1:].strip().decode("latin-1", "replace")))
        else:
            fields.append(_f(pos, len(ln), "(无冒号)",
                             ln.decode("latin-1", "replace")))
        pos += len(ln) + 2
        n += 1
        if n >= STRUCT_MAX_FIELDS:
            break
    layers.append({"name": "头部（%d 行）" % n,
                   "off": len(first) + 2,
                   "len": max(0, (split + 4 if split >= 0 else len(data)) - len(first) - 2)})
    if split >= 0:
        blen = len(data) - (split + 4)
        layers.append({"name": "消息体", "off": split + 4, "len": blen})
        if blen:
            fields.append(_f(split + 4, blen, "Body", "%d 字节" % blen,
                             "未做内容解码，按原字节呈现"))
    return fields, layers


def _struct_tls(data):
    """TLS 记录 + 握手（ClientHello 拆到扩展级）。"""
    if len(data) < 5:
        return None
    rtype = {0x14: "ChangeCipherSpec", 0x15: "Alert", 0x16: "Handshake",
             0x17: "ApplicationData", 0x18: "Heartbeat"}.get(data[0], "未知(%d)" % data[0])
    rlen = int.from_bytes(data[3:5], "big")
    fields = [
        _f(0, 1, "记录类型", rtype),
        _f(1, 2, "记录版本", _TLS_VER.get(int.from_bytes(data[1:3], "big"),
                                          "0x%04x" % int.from_bytes(data[1:3], "big"))),
        _f(3, 2, "记录长度", str(rlen)),
    ]
    layers = [{"name": "TLS 记录头", "off": 0, "len": 5}]
    if data[0] != 0x16 or len(data) < 9:
        layers.append({"name": "TLS 载荷", "off": 5, "len": max(0, len(data) - 5)})
        return fields, layers

    hs = data[5]
    hl = int.from_bytes(data[6:9], "big")
    fields.append(_f(5, 1, "握手类型", _TLS_HS.get(hs, "HS%d" % hs)))
    fields.append(_f(6, 3, "握手长度", str(hl)))
    layers.append({"name": "握手头", "off": 5, "len": 4})

    try:
        p = 9
        if hs == 1:                                     # ClientHello
            fields.append(_f(p, 2, "client_version",
                             _TLS_VER.get(int.from_bytes(data[p:p + 2], "big"), "?")))
            p += 2 + 32
            sid = data[p]
            fields.append(_f(p, 1 + sid, "session_id", "%d 字节" % sid))
            p += 1 + sid
            cs = int.from_bytes(data[p:p + 2], "big")
            fields.append(_f(p, 2 + cs, "cipher_suites", "%d 个候选套件" % (cs // 2)))
            p += 2 + cs
            cm = data[p]
            fields.append(_f(p, 1 + cm, "compression", "%d 种" % cm))
            p += 1 + cm
            if p + 2 <= len(data):
                ext_len = int.from_bytes(data[p:p + 2], "big")
                fields.append(_f(p, 2, "extensions_len", str(ext_len)))
                layers.append({"name": "扩展区", "off": p + 2, "len": ext_len})
                p += 2
                end = min(len(data), p + ext_len)
                k = 0
                while p + 4 <= end and k < 40:
                    et = int.from_bytes(data[p:p + 2], "big")
                    el = int.from_bytes(data[p + 2:p + 4], "big")
                    fields.append(_f(p, 4 + el, "扩展 0x%04x" % et,
                                     _TLS_EXT.get(et, "未知"),
                                     "%d 字节" % el))
                    p += 4 + el
                    k += 1
        elif hs == 2:                                   # ServerHello
            fields.append(_f(p, 2, "server_version",
                             _TLS_VER.get(int.from_bytes(data[p:p + 2], "big"), "?")))
            p += 2 + 32
            sid = data[p]
            fields.append(_f(p, 1 + sid, "session_id", "%d 字节" % sid))
            p += 1 + sid
            if p + 2 <= len(data):
                fields.append(_f(p, 2, "cipher_suite", "0x%04x"
                                 % int.from_bytes(data[p:p + 2], "big")))
                p += 2
            if p + 1 <= len(data):
                fields.append(_f(p, 1, "compression",
                                 "0x%02x" % data[p]))
    except Exception:
        pass
    return fields, layers


def _struct_dns(data):
    """DNS 头 + 问题区 + 资源记录，逐条带 TTL 与 rdata。"""
    if len(data) < 12:
        return None
    txid = int.from_bytes(data[0:2], "big")
    flags = int.from_bytes(data[2:4], "big")
    qd = int.from_bytes(data[4:6], "big")
    an = int.from_bytes(data[6:8], "big")
    ns = int.from_bytes(data[8:10], "big")
    ar = int.from_bytes(data[10:12], "big")
    if qd == 0 or qd > 8 or an > 32:
        return None
    fields = [
        _f(0, 2, "事务ID", "0x%04x" % txid),
        _f(2, 2, "标志", "0x%04x" % flags,
           "%s opcode=%d AA=%d TC=%d RD=%d RA=%d rcode=%d" % (
               "响应" if flags & 0x8000 else "查询",
               (flags >> 11) & 0xF, (flags >> 10) & 1, (flags >> 9) & 1,
               (flags >> 8) & 1, (flags >> 7) & 1, flags & 0xF)),
        _f(4, 2, "QDCOUNT", str(qd)),
        _f(6, 2, "ANCOUNT", str(an)),
        _f(8, 2, "NSCOUNT", str(ns)),
        _f(10, 2, "ARCOUNT", str(ar)),
    ]
    layers = [{"name": "DNS 头", "off": 0, "len": 12}]
    p = 12
    for i in range(min(qd, 4)):
        if p >= len(data):
            break
        name, p2 = _dns_name(data, p)
        if p2 + 4 > len(data):
            break
        qtype = int.from_bytes(data[p2:p2 + 2], "big")
        fields.append(_f(p, p2 - p, "Q%d 名称" % (i + 1), name or "?"))
        fields.append(_f(p2, 2, "Q%d 类型" % (i + 1), _DNS_TYPES.get(qtype, "T%d" % qtype)))
        fields.append(_f(p2 + 2, 2, "Q%d 类" % (i + 1),
                         str(int.from_bytes(data[p2 + 2:p2 + 4], "big"))))
        layers.append({"name": "问题区 #%d" % (i + 1), "off": p, "len": p2 + 4 - p})
        p = p2 + 4
    for i in range(min(an, 32)):
        if p >= len(data):
            break
        _an, p2 = _dns_name(data, p)
        if p2 + 10 > len(data):
            break
        atype = int.from_bytes(data[p2:p2 + 2], "big")
        ttl = int.from_bytes(data[p2 + 4:p2 + 8], "big")
        rdlen = int.from_bytes(data[p2 + 8:p2 + 10], "big")
        rd = data[p2 + 10:p2 + 10 + rdlen]
        val = rd.hex()
        if atype == 1 and rdlen == 4:
            val = ".".join(str(b) for b in rd)
        elif atype == 28 and rdlen == 16:
            val = ":".join("%02x%02x" % (rd[k], rd[k + 1]) for k in range(0, 16, 2))
        elif atype in (5, 2, 12):
            cn, _ = _dns_name(data, p2 + 10)
            val = cn or val
        fields.append(_f(p, p2 + 10 + rdlen - p, "A%d 记录" % (i + 1),
                         "%s %s TTL=%ds" % (_DNS_TYPES.get(atype, "T%d" % atype), val, ttl)))
        layers.append({"name": "应答 #%d" % (i + 1), "off": p, "len": p2 + 10 + rdlen - p})
        p = p2 + 10 + rdlen
    return fields, layers


def _struct_socks5(data):
    if len(data) < 4 or data[0] != 0x05:
        return None
    cmd = {1: "CONNECT", 2: "BIND", 3: "UDP ASSOCIATE"}.get(data[1], "0x%02x" % data[1])
    fields = [
        _f(0, 1, "VER", "0x%02x" % data[0]),
        _f(1, 1, "CMD/REP", cmd),
        _f(2, 1, "RSV", "0x%02x" % data[2]),
        _f(3, 1, "ATYP", {1: "IPv4", 3: "域名", 4: "IPv6"}.get(data[3], "0x%02x" % data[3])),
    ]
    layers = [{"name": "SOCKS5 头", "off": 0, "len": 4}]
    p = 4
    try:
        if data[3] == 1 and len(data) >= 10:
            fields.append(_f(4, 4, "DST.ADDR", ".".join(str(b) for b in data[4:8])))
            fields.append(_f(8, 2, "DST.PORT", str(int.from_bytes(data[8:10], "big"))))
            p = 10
        elif data[3] == 3 and len(data) >= 5:
            dl = data[4]
            fields.append(_f(4, 1 + dl, "DST.ADDR", data[5:5 + dl].decode("latin-1", "replace")))
            fields.append(_f(5 + dl, 2, "DST.PORT",
                             str(int.from_bytes(data[5 + dl:7 + dl], "big"))))
            p = 7 + dl
        elif data[3] == 4 and len(data) >= 22:
            fields.append(_f(4, 16, "DST.ADDR", data[4:20].hex()))
            fields.append(_f(20, 2, "DST.PORT", str(int.from_bytes(data[20:22], "big"))))
            p = 22
    except Exception:
        pass
    if p < len(data):
        layers.append({"name": "SOCKS5 载荷", "off": p, "len": len(data) - p})
    return fields, layers


def _struct_mqtt(data):
    if not data:
        return None
    t = data[0] >> 4
    if t == 0 or t > 15:
        return None
    name = {1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 8: "SUBSCRIBE",
            9: "SUBACK", 12: "PINGREQ", 13: "PINGRESP", 14: "DISCONNECT"}.get(t, "TYPE%d" % t)
    # Remaining Length 是 varint，最多 4 字节
    p, mul, rl = 1, 1, 0
    while p < len(data) and p <= 4:
        b = data[p]
        rl += (b & 0x7F) * mul
        mul *= 128
        p += 1
        if not (b & 0x80):
            break
    fields = [
        _f(0, 1, "报文类型", name),
        _f(0, 1, "标志位", "0x%02x" % (data[0] & 0x0F),
           "DUP=%d QoS=%d RETAIN=%d" % ((data[0] >> 3) & 1, (data[0] >> 1) & 3, data[0] & 1)),
        _f(1, p - 1, "剩余长度", "%d（varint %d 字节）" % (rl, p - 1)),
    ]
    layers = [{"name": "MQTT 固定头", "off": 0, "len": p}]
    if p < len(data):
        layers.append({"name": "可变头 + 载荷", "off": p, "len": len(data) - p})
    return fields, layers


def _struct(proto, direction, data):
    """把裸字节拆成字段级结构。返回 {app, fields, layers, truncated}。

    fields 每行带 off/len，前端据此在 hex 里高亮 —— 结构与字节必须能对上。
    认不出就只给一条 BIN 记录。绝不猜。
    """
    if not data:
        return {"app": "", "fields": [], "layers": [], "truncated": False}
    app, fields, layers = "", [], []
    try:
        if proto == "udp":
            r = _dns_analyze(data, direction)
            if r:
                app = "DNS"
                got = _struct_dns(data)
                if got:
                    fields, layers = got
        if not fields:
            if data[0] in (0x16, 0x14, 0x15, 0x17, 0x18) and len(data) > 2 and data[1] == 0x03:
                got = _struct_tls(data)
                if got:
                    app, (fields, layers) = "TLS", got
            if not fields:
                got = _struct_http(data)
                if got:
                    app, (fields, layers) = "HTTP", got
            if not fields:
                got = _struct_dns(data)
                if got:
                    app, (fields, layers) = "DNS", got
            if not fields:
                got = _struct_socks5(data)
                if got:
                    app, (fields, layers) = "SOCKS5", got
            if not fields:
                got = _struct_mqtt(data)
                if got:
                    app, (fields, layers) = "MQTT", got
    except Exception:
        fields, layers = [], []
    if not fields:
        app = app or ("UDP" if proto == "udp" else "BIN")
        fields = [_f(0, len(data), "载荷", "%d 字节" % len(data),
                     "没认出已知结构，按原始字节呈现")]
        layers = [{"name": "原始载荷", "off": 0, "len": len(data)}]
    trunc = len(fields) > STRUCT_MAX_FIELDS
    if trunc:
        fields = fields[:STRUCT_MAX_FIELDS]
    return {"app": app, "fields": fields, "layers": layers, "truncated": trunc}


def _parse_hostport(s):
    """拆 "host:port"。IPv6 走 [::1]:80 形式；没有端口时 port 返回 0。"""
    s = (s or "").strip()
    if not s:
        return "", 0
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        rest = rest.lstrip(":")
        return host, int(rest) if rest.isdigit() else 0
    if s.count(":") == 1:
        host, _, port = s.partition(":")
        return host, int(port) if port.isdigit() else 0
    return s, 0


class WpeEngine:
    """WPE（Winsock Packet Editor）的网页端等价实现。

    ## 为什么不需要注入

    经典 WPE 在 Windows 上靠 **注入目标进程 + hook Winsock 的 send/recv**
    来拦截封包 —— 所以它只能抓本机某个进程的包，还要过反调试、反注入。

    这里不用注入：**适配层本身就是所有流量的必经点**。客户端把代理指向它，
    TCP 走 CONNECT / 绝对 URI，UDP 走 SOCKS5 UDP ASSOCIATE，全部由它转发。
    于是只要在转发路径上挂记录点，就拿到了和 hook 完全等价的能力，
    而且不挑平台、不进目标进程、不触发反作弊的注入检测。

    ## 能力对照（逐项对应 WPE Pro）

      Capture  抓包      relay() / _udp_dispatch_loop() / upstream() 三处记录点
      Parse    自动解析  _analyze() 把裸字节认成 HTTP/TLS/DNS/MQTT/SOCKS5/BIN
                        并生成一行摘要（TLS 抠 SNI、DNS 抠查询名与应答 IP）
      Struct   结构      _struct()  字段级拆分，每行带 off/len，供详情页高亮
                        HTTP 拆请求行/头部/体；TLS 拆记录头/握手/扩展；
                        DNS 拆头/问题区/资源记录（含 TTL）；SOCKS5 / MQTT 同理
      List     封包列表  list()   环形缓冲，按方向/协议/应用层/目标/十六进制过滤
      Detail   十六进制  get()    完整 hex + ASCII + 字段结构
      Edit     编辑      hex 在前端改；重放时提交，或断点放行时改后转发（回填 mod_hex）
      Replay   重放      replay() 原样或改后重发（新建连接，与原始连接无关）
      Send     构造发送  send()   从零构造封包发给任意目标
      Hold     拦截断点  set_hold() 命中规则的包**卡在转发路径上**等人放行
      Release  放行      release() forward / drop / modify 三选一
      Rewrite  过滤器改写 set_rewrite() 命中条件**不用人工放行**，直接按规则改后转发
                        find/replace 字节替换，或 set_hex 整包替换；
                        规则至少要有一个条件，无条件规则会被拒收
      Export   导出      /wpe/export 吐完整 hex（不是列表预览）
      Import   导入      /wpe/import 导入导出文件，一律打 imported 标记
      Stat     统计      stat()   实时计数（含改写次数与逐条规则命中数）

    ## 内存

    环形缓冲默认 4000 条，单包 hex 最多记 MAX_PKT 字节（超出打截断标记）。
    4000 × 64KB 是理论上限，实际常见封包 1KB 以内，占用可以忽略。
    """

    MAX_PKT = 65536          # 单包最多记录的字节数
    HOLD_TIMEOUT = 30.0      # 断点最长卡住时间（秒），防止无人放行把连接吊死
    # 导出上限（只作用于**走 JSON 回传内存**的那条路）。
    #
    # ★ 这里踩过一次坑，写下来别重犯：原来 EXPORT_MAX_HEX = 8MB，
    #   而环形缓冲是 4000 条 —— 抓满一次大流量后，导出会在 8MB 处停下，
    #   4000 条只导出 500 多条（实测 514/4000）。界面上叫「导出完整 hex」，
    #   实际给了 13%，这就是用户报的「无法导出完整 hex」。
    #   根因不是上限太小，是**根本不该把完整导出塞进一个 JSON 响应里**。
    #   所以现在：内存回传这条路保留（有上限、可分页、会明说没导完），
    #   真要完整就用 export_to_file() 落盘 —— 那条路不受这里约束。
    EXPORT_MAX_PKTS = 20000
    EXPORT_MAX_HEX = 64 * 1024 * 1024
    # 落盘导出的目录。必须在适配层用户可写的路径下。
    EXPORT_DIR = "/opt/ccpx/exports"

    def __init__(self, cfg=None, maxlen=4000):
        self.cfg = cfg or {}
        self.packets = collections.deque(maxlen=max(100, int(maxlen)))
        self.seq = 0
        self.lock = threading.Lock()
        self.started = time.time()
        # ---- 断点 ----
        self.hold_on = False
        self.hold_rule = {}
        self.held = {}                     # id -> {"ev","action","data"}
        self.hold_timeout = self.HOLD_TIMEOUT
        # ---- 自动改写（过滤器改写）----
        self.rw_on = False
        self.rw_rules = []
        self.rw_hits = {}
        # 注意：内部计数器不能叫 self.stat —— 会被同名的 stat() 方法遮住，
        # 外部调 e.stat() 会变成 dict 不可调用。叫 counters。
        self.counters = {
            "captured": 0, "held": 0, "replayed": 0, "sent": 0,
            "dropped": 0, "modified": 0, "truncated": 0, "imported": 0,
            "rewritten": 0,
        }

    # ---------------- 捕获 ----------------
    def record(self, direction, proto, client, target, data, label="", note=""):
        """记一个封包。direction: c2s(客户端->目标) / s2c(目标->客户端)。"""
        if not data:
            return None
        raw = data[:self.MAX_PKT]
        trunc = len(data) > self.MAX_PKT
        app, summary = _analyze(proto, direction, raw)
        with self.lock:
            self.seq += 1
            pkt = {
                "id": self.seq,
                "ts": time.time(),
                "t": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "dir": direction,
                "proto": proto,
                "app": app,
                "summary": summary,
                "client": client or "",
                "target": target or "",
                "label": label or "",
                "len": len(data),
                "hex": raw.hex(),
                "ascii": _pkt_ascii(raw),
                "note": note or "",
                "trunc": trunc,
            }
            self.packets.append(pkt)
            self.counters["captured"] += 1
            if trunc:
                self.counters["truncated"] += 1
        return pkt

    # ---------------- 查询 ----------------
    @staticmethod
    def _brief(pkt):
        b = dict(pkt)
        # 列表只带预览，完整 hex 走 detail，避免一次回传几 MB。
        # 但必须**明说这是预览**：不标出来，调用方会以为 512 字符就是全部内容。
        b["hex_full_len"] = len(pkt["hex"])
        b["hex_trunc"] = len(pkt["hex"]) > 512
        b["hex"] = pkt["hex"][:512]
        b["ascii"] = pkt["ascii"][:256]
        if b.get("mod_hex"):
            b["mod_hex_full_len"] = len(b["mod_hex"])
            b["mod_hex"] = b["mod_hex"][:512]
            b["mod_ascii"] = (b.get("mod_ascii") or "")[:256]
        return b

    def annotate_modify(self, pkt_id, new_data):
        """改写生效后回填记录。

        不这么做的话，列表里显示的永远是「拦下来的原始包」，而实际转发出去的
        是改写后的内容 —— 那就是在骗人。回填 mod_hex / mod_len 并改 note，
        让记录和真实转发一致。
        """
        try:
            pid = int(pkt_id)
        except Exception:
            return False
        with self.lock:
            for pkt in self.packets:
                if pkt["id"] == pid:
                    pkt["mod_hex"] = new_data[:self.MAX_PKT].hex()
                    pkt["mod_ascii"] = _pkt_ascii(new_data[:self.MAX_PKT])
                    pkt["mod_len"] = len(new_data)
                    rule = pkt.get("wpe_rule") or 0
                    if rule:
                        # 自动改写（过滤器规则）—— 和断点人工改写要能分清，
                        # 否则回头看记录根本不知道这个包是谁改的。
                        pkt["wpe_action"] = "rewrite"
                        pkt["note"] = ("已被 WPE 自动改写（规则 #%s）：%d -> %d 字节，"
                                       "%s；实际转发的是 mod_hex"
                                       % (rule, pkt["len"], len(new_data),
                                          pkt.get("wpe_rule_how") or ""))
                    else:
                        pkt["wpe_action"] = "modify"
                        pkt["note"] = ("已被 WPE 改写：%d -> %d 字节，"
                                       "实际转发的是 mod_hex" % (pkt["len"], len(new_data)))
                    return True
        return False

    def list(self, since=0, limit=300, direction="", proto="", target="",
             hexmatch="", label="", only_hold=False, app="", min_len=0, max_len=0,
             before=0):
        """按过滤条件取封包列表。

        ★ 诚实性要点：过滤只在**从新到旧扫到够 limit 条为止**的窗口内生效。
        如果扫到 limit 就停了，那更旧的包根本没看 —— 这时候「0 命中」并不等于
        「不存在」。所以除了 packets，还要如实报出 scanned / buffered / truncated，
        让调用方知道这次搜索的覆盖范围，而不是把「没扫到」当成「没有」。

        ★ proto 与 _match() 同语义：既认传输层名（tcp/udp）也认内容层名
        （http/tls/...）。这里以前只比 pkt["proto"]，界面上按「HTTP」筛就永远
        0 命中 —— 同一个字段在两个地方两种行为，是最容易把人带沟里的那种不一致。

        ★ min_len / max_len：WPE Pro 的过滤对话框里有 Size 区间，这里以前没有，
        等于少了一半筛法（想只看大包/小包做不到）。

        ★ before：只看 id **严格小于** before 的包 —— 这是「加载更早」的游标。
        以前只有 since（只看更新的），前端想翻到第 2000 条以前没有任何入口，
        缓冲 4000 条时后 2000 条永远看不到。配 has_more / oldest_id 就能翻页。
        """
        out = []
        hx = (hexmatch or "").replace(" ", "").replace("\n", "").lower()
        ap = (app or "").strip().upper()
        pr = (proto or "").strip().lower()
        try:
            mn = int(min_len or 0)
        except Exception:                       # noqa: BLE001
            mn = 0
        try:
            mx = int(max_len or 0)
        except Exception:                       # noqa: BLE001
            mx = 0
        try:
            bf = int(before or 0)
        except Exception:                       # noqa: BLE001
            bf = 0
        lim = max(1, min(int(limit or 300), 2000))
        with self.lock:
            snap = list(self.packets)
        scanned = 0
        for pkt in reversed(snap):
            scanned += 1
            if since and pkt["id"] <= int(since):
                continue
            if bf and pkt["id"] >= bf:
                continue
            if direction and pkt["dir"] != direction:
                continue
            if pr and pr not in (pkt["proto"], (pkt.get("app") or "").lower()):
                continue
            if ap and (pkt.get("app") or "").upper() != ap:
                continue
            if target and target.lower() not in (pkt["target"] or "").lower():
                continue
            if label and label.lower() not in (pkt["label"] or "").lower():
                continue
            if hx and hx not in pkt["hex"]:
                continue
            if mn and pkt["len"] < mn:
                continue
            if mx and pkt["len"] > mx:
                continue
            if only_hold and pkt["id"] not in self.held:
                continue
            out.append(self._brief(pkt))
            if len(out) >= lim:
                break
        return {
            "packets": out,
            "matched": len(out),
            "scanned": scanned,
            "buffered": len(snap),
            "limit": lim,
            "truncated": scanned < len(snap),
            "capped": len(out) >= lim,
            # 翻页游标：前端拿 oldest_id 当 before 再请求一次 = 「加载更早」。
            # has_more 为真才说明还有更旧的没取。
            "before": bf,
            "oldest_id": (out[-1]["id"] if out else None),
            "newest_id": (out[0]["id"] if out else None),
            "has_more": scanned < len(snap),
            "filter": {"dir": direction, "proto": proto, "app": app,
                       "target": target, "hex": hexmatch, "label": label,
                       "min_len": mn, "max_len": mx, "only_hold": bool(only_hold)},
        }

    def get(self, pkt_id):
        try:
            pid = int(pkt_id)
        except Exception:
            return None
        with self.lock:
            for pkt in self.packets:
                if pkt["id"] == pid:
                    return dict(pkt)
        return None

    # ---------------- 断点 ----------------
    def set_hold(self, on, rule=None):
        self.hold_on = bool(on)
        if rule is not None:
            self.hold_rule = rule or {}
        log.info("WPE 断点 %s 规则=%s", "开启" if self.hold_on else "关闭",
                 self.hold_rule or "(全部)")
        return {"ok": True, "on": self.hold_on, "rule": self.hold_rule}

    def _match(self, pkt, data, rule=None):
        """pkt 是否命中规则。rule 为 None 时用断点规则。

        空规则 = 全部命中（断点的语义：不填条件就是全拦）。
        但**自动改写**不允许空规则 —— 见 apply_rewrite()：空条件会改掉所有包，
        那是灾难不是功能。
        """
        r = self.hold_rule if rule is None else rule
        r = r or {}
        if not r:
            return True                     # 空规则 = 全部拦
        d = (r.get("dir") or "").lower()
        if d and d != pkt["dir"]:
            return False
        # proto 是**传输层**标签（tcp/udp），三处 relay 记的都是 tcp —— 这是
        # 有意的：隧道里装的是明文 HTTP 还是 TLS，传输层并不知道。
        # 但用规则的人写的是「HTTP」「TLS」这种内容协议名，写 proto=http 却
        # 永远 0 命中，会以为改写坏了。所以这里同时拿 app（内容分类）比一次：
        #   proto=tcp   -> 命中所有 tcp
        #   proto=http  -> 命中内容被判成 HTTP 的包
        # 两个字段都能用，谁都不会静默失灵。
        pr = (r.get("proto") or "").lower()
        if pr and pr not in (pkt["proto"], (pkt.get("app") or "").lower()):
            return False
        t = (r.get("target") or "").strip()
        if t and t.lower() not in (pkt["target"] or "").lower():
            return False
        hx = (r.get("hex") or "").replace(" ", "").lower()
        if hx and hx not in pkt["hex"]:
            return False
        ap = (r.get("app") or "").strip().upper()
        if ap and ap != (pkt.get("app") or "").upper():
            return False
        mn = r.get("min_len")
        if mn:
            try:
                if pkt["len"] < int(mn):
                    return False
            except Exception:
                pass
        mx = r.get("max_len")
        if mx:
            try:
                if pkt["len"] > int(mx):
                    return False
            except Exception:
                pass
        return True

    # ---------------- 自动改写（过滤器改写） ---------------- #
    #
    # 经典 WPE Pro 的 Filter 干的就是这件事：命中条件的包**不用人工放行**
    # 直接按规则改掉再转发。原来这里只有「断点 + 人工改」，等于少了半条腿。
    #
    # 规则字段（全部可选，但**至少要有一个条件**，否则拒绝）：
    #   dir/proto/target/hex/app/min_len/max_len  —— 同 _match，用来挑包
    #   find    + replace   —— 在载荷里把 find 的 hex 字节换成 replace
    #   set_hex             —— 整包替换成给定 hex（等价于 WPE 的"改包重发"，但自动）
    #   note                —— 备注，写进封包记录的 note
    #
    # ★ 诚实性：改写必须回填到记录里（annotate_modify），列表里显示的和真正
    #   转发出去的一致；改写次数计入 counters；命中次数按规则记 rw_hits。
    # ★ 长度变化：find/replace 长度不同会让载荷变长/变短。TCP 是有帧的协议，
    #   乱改长度可能破坏分帧 —— 不禁止，但如实报出长度变化，让用的人自己判断。

    RW_ACTIONS = ("find_replace", "set_hex")

    def set_rewrite(self, on=None, rules=None):
        """设置自动改写规则。返回里带 rejected —— 被拒的规则和原因都要说清楚。

        静默丢规则是最坏的做法：界面上看着保存成功了，实际少了一条，
        流量照原样转发，用的人会以为「改写不生效」而查半天。所以这里
        逐条给出下标和原因，界面必须原样展示。
        """
        rejected = []
        if on is not None:
            self.rw_on = bool(on)
        if rules is not None:
            clean = []
            for i, r in enumerate(rules or []):
                if not isinstance(r, dict):
                    rejected.append({"i": i, "reason": "规则不是对象"})
                    continue
                rr = dict(r)
                act = (rr.get("act") or "").strip() or "find_replace"
                if act == "set_hex":
                    hx = (rr.get("set_hex") or "").replace(" ", "").replace("\n", "")
                    if not hx:
                        rejected.append({"i": i, "reason": "整包替换规则没有 set_hex"})
                        continue
                    if not _is_hex(hx):
                        rejected.append({"i": i, "reason": "set_hex 不是合法十六进制"})
                        continue
                else:
                    act = "find_replace"
                    fx = (rr.get("find") or "").replace(" ", "").replace("\n", "")
                    if not fx:
                        rejected.append({"i": i, "reason": "字节替换规则没有 find"})
                        continue
                    if not _is_hex(fx):
                        rejected.append({"i": i, "reason": "find 不是合法十六进制"})
                        continue
                    rx = (rr.get("replace") or "").replace(" ", "").replace("\n", "")
                    if rx and not _is_hex(rx):
                        rejected.append({"i": i, "reason": "replace 不是合法十六进制"})
                        continue
                if not self._rule_has_cond(rr):
                    # 没有条件的改写规则会改掉每一个包 —— 直接拒收，不静默放行
                    rejected.append({
                        "i": i,
                        "reason": "没有任何条件（dir/proto/target/hex/app/"
                                  "min_len/max_len 至少填一个），无条件改写会改掉所有包"})
                    continue
                rr["act"] = act
                rr["id"] = len(clean) + 1
                clean.append(rr)
            self.rw_rules = clean
            self.rw_hits = dict((r["id"], 0) for r in clean)
        if rejected:
            log.warning("WPE 自动改写：%d 条规则被拒收 %s", len(rejected), rejected)
        log.info("WPE 自动改写 %s，%d 条规则",
                 "开启" if self.rw_on else "关闭", len(self.rw_rules))
        return {"ok": True, "on": self.rw_on, "rules": self.rw_rules,
                "hits": self.rw_hits, "rejected": rejected}

    @staticmethod
    def _rule_has_cond(r):
        for k in ("dir", "proto", "target", "hex", "app", "min_len", "max_len"):
            v = r.get(k)
            if v not in (None, "", 0):
                return True
        return False

    def _do_rewrite(self, rule, data):
        """按规则算出新载荷。返回 (新数据, 说明) 或 (None, 原因)。"""
        act = rule.get("act") or "find_replace"
        if act == "set_hex":
            try:
                nd = bytes.fromhex((rule.get("set_hex") or "").replace(" ", "")
                                   .replace("\n", ""))
            except Exception as exc:                  # noqa: BLE001
                return None, "set_hex 不是合法十六进制：%s" % exc
            if not nd:
                return None, "set_hex 是空的"
            return nd, "整包替换 %d -> %d 字节" % (len(data), len(nd))
        find = bytes.fromhex((rule.get("find") or "").replace(" ", "")) \
            if _is_hex(rule.get("find")) else b""
        if not find:
            return None, "find 不是合法十六进制"
        if find not in data:
            return None, "载荷里没有 find 字节"
        rep = bytes.fromhex((rule.get("replace") or "").replace(" ", "")) \
            if _is_hex(rule.get("replace") or "") else b""
        lim = int(rule.get("count") or 0)
        nd = data.replace(find, rep) if not lim else data.replace(find, rep, lim)
        return nd, "字节替换 %s -> %s（%d -> %d 字节）" % (
            find.hex(), rep.hex() or "(空)", len(data), len(nd))

    def apply_rewrite(self, pkt, data):
        """自动改写。没开启 / 没命中就原样返回 (data, "forward")。

        返回动作 "modify"（沿用断点改写那条通路），并在 pkt 上打 wpe_rule 标记 ——
        annotate_modify() 看到标记就会把 note 写成「自动改写（规则 #N）」。
        """
        if not self.rw_on or not self.rw_rules:
            return data, "forward"
        for rule in self.rw_rules:
            if not self._match(pkt, data, rule):
                continue
            nd, how = self._do_rewrite(rule, data)
            if nd is None:
                log.info("WPE 改写规则 #%s 未生效：%s", rule.get("id"), how)
                continue
            if nd == data:
                continue
            pkt["wpe_rule"] = rule.get("id")
            pkt["wpe_rule_how"] = how
            with self.lock:
                self.counters["modified"] += 1
                self.counters["rewritten"] += 1
                self.rw_hits[rule.get("id")] = self.rw_hits.get(rule.get("id"), 0) + 1
            log.info("WPE 自动改写封包 #%d（规则 #%s）：%s",
                     pkt["id"], rule.get("id"), how)
            return nd, "modify"
        return data, "forward"

    def mark_action(self, pkt_id, action, note):
        """把「这个包被 WPE 怎么处理了」写进记录。

        ★ 为什么必须做：断点放行 / 丢弃 / 超时之后，如果记录里不留痕，
        回头看列表的人根本分不清「这个包真的转发出去了」还是「被丢掉了」。
        沉默等于骗人 —— 记录必须能回答「它到底去哪了」。
        """
        try:
            pid = int(pkt_id)
        except Exception:
            return False
        with self.lock:
            for pkt in self.packets:
                if pkt["id"] == pid:
                    pkt["wpe_action"] = action
                    pkt["note"] = note
                    return True
        return False

    async def maybe_hold(self, pkt, data):
        """断点 + 自动改写，一个入口。

        返回 (新数据 or None, 动作)：
          forward  原样转发
          modify   改后转发（断点人工改 / 自动改写规则命中，用 pkt["wpe_rule"] 区分）
          drop     丢弃（调用方不要转发，也不要写出去）
        """
        if not self.hold_on or not self._match(pkt, data):
            # 断点没命中 —— 那就看自动改写规则（过滤器改写）。
            return self.apply_rewrite(pkt, data)
        ev = asyncio.Event()
        slot = {"ev": ev, "action": None, "data": None}
        self.held[pkt["id"]] = slot
        with self.lock:
            self.counters["held"] += 1
        timed_out = False
        try:
            await asyncio.wait_for(ev.wait(), timeout=self.hold_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            log.info("WPE 断点 #%d 超时 %.0fs，自动放行", pkt["id"], self.hold_timeout)
        finally:
            self.held.pop(pkt["id"], None)
        act = slot.get("action") or "forward"
        if act == "drop":
            with self.lock:
                self.counters["dropped"] += 1
            # ★ 留痕：列表里必须看得出来这个包没发出去
            self.mark_action(pkt["id"], "drop",
                             "已被 WPE 丢弃：没有转发给 %s" % (pkt.get("target") or "?"))
            return None, "drop"
        if act == "modify" and slot.get("data"):
            with self.lock:
                self.counters["modified"] += 1
            return slot["data"], "modify"   # note 由 annotate_modify 回填
        if timed_out:
            self.mark_action(pkt["id"], "timeout",
                             "断点超时（%.0fs 无人放行）已自动原样转发"
                             % self.hold_timeout)
        else:
            self.mark_action(pkt["id"], "forward",
                             "断点放行：已原样转发给 %s" % (pkt.get("target") or "?"))
        return data, "forward"

    def release(self, pkt_id, action="forward", data_hex=None):
        try:
            pid = int(pkt_id)
        except Exception:
            return {"ok": False, "err": "封包号非法: %r" % pkt_id}
        slot = self.held.get(pid)
        if not slot:
            return {"ok": False, "err": "封包 #%d 不在断点队列" % pid}
        act = (action or "forward").lower()
        if act == "modify":
            try:
                slot["data"] = binascii.unhexlify(
                    (data_hex or "").replace(" ", "").replace("\n", ""))
            except Exception as exc:
                return {"ok": False, "err": "十六进制解析失败: %s" % exc}
        slot["action"] = act
        slot["ev"].set()
        return {"ok": True, "id": pid, "action": act}

    # ---------------- 重放 / 发送 ----------------
    async def _send_one(self, proto, host, port, payload, timeout=8.0,
                        label="wpe-send"):
        """按协议把一段字节发给目标。TCP 新建连接发完即关（WPE 重放同语义）。

        注意：自己造出来的包、和它收到的回包，**都必须进抓包列表**。
        WPE 发出去的包在列表里看不见的话，「抓包」就是残缺的 —— 那等于骗人。
        （重放/构造发送不走断点，否则 WPE 会把自己卡死。）
        """
        target = "%s:%d" % (host, port)
        reply_bytes = b""
        from_addr = ""
        try:
            if proto == "udp":
                loop = asyncio.get_running_loop()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(timeout)
                await loop.run_in_executor(None, sock.sendto, payload, (host, port))
                try:
                    data, addr = await loop.run_in_executor(
                        None, lambda: sock.recvfrom(65535))
                    sock.close()
                    reply_bytes = data
                    from_addr = "%s:%d" % addr
                    res = {"ok": True, "target": target,
                           "sent": len(payload), "reply": len(data),
                           "reply_hex": data[:512].hex(),
                           "from": from_addr}
                except socket.timeout:
                    sock.close()
                    res = {"ok": True, "target": target,
                           "sent": len(payload), "reply": 0,
                           "note": "已发出，%.0fs 内无回包" % timeout}
                except OSError as exc:
                    sock.close()
                    res = {"ok": True, "target": target,
                           "sent": len(payload), "reply": 0,
                           "note": "已发出，收包异常: %s" % exc}
            else:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port),
                                              timeout=timeout)
                w.write(payload)
                await w.drain()
                try:
                    reply_bytes = await asyncio.wait_for(r.read(65536),
                                                         timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                except (ConnectionError, OSError):
                    pass
                try:
                    w.close()
                except Exception:
                    pass
                from_addr = target
                res = {"ok": True, "target": target,
                       "sent": len(payload), "reply": len(reply_bytes),
                       "reply_hex": reply_bytes[:512].hex()}
        except Exception as exc:
            res = {"ok": False, "target": target,
                   "err": "%s: %s" % (type(exc).__name__, exc)}
        # ---- 记账：自己发的包记 c2s，回包记 s2c，列表里看得见 ----
        try:
            self.record("c2s", proto, "WPE(本机)", target, payload,
                        label=label, note="WPE 主动发出，非客户端流量")
            if reply_bytes:
                self.record("s2c", proto, from_addr or target, "WPE(本机)",
                            reply_bytes, label=label + " 回包",
                            note="WPE 主动发送的应答")
        except Exception:
            pass
        return res

    async def replay(self, pkt_id, data_hex=None, target=None, proto=None, times=1):
        pkt = self.get(pkt_id)
        if not pkt:
            return {"ok": False, "err": "封包 #%s 不存在" % pkt_id}
        try:
            payload = binascii.unhexlify(
                (data_hex if data_hex is not None else pkt["hex"])
                .replace(" ", "").replace("\n", ""))
        except Exception as exc:
            return {"ok": False, "err": "十六进制解析失败: %s" % exc}
        tgt = (target or pkt["target"] or "").strip()
        pr = (proto or pkt["proto"] or "tcp").lower()
        host, port = _parse_hostport(tgt)
        if not host or not port:
            return {"ok": False, "err": "目标地址非法: %r" % tgt}
        n = max(1, min(int(times or 1), 50))
        results = []
        for _ in range(n):
            results.append(await self._send_one(pr, host, port, payload,
                                                label="wpe-replay #%s" % pkt_id))
        ok = sum(1 for r in results if r.get("ok"))
        with self.lock:
            self.counters["replayed"] += ok
        return {"ok": ok > 0, "proto": pr, "target": tgt,
                "bytes": len(payload), "sent": ok, "total": n,
                "results": results}

    async def send(self, proto, target, data_hex, timeout=8.0):
        try:
            payload = binascii.unhexlify(
                (data_hex or "").replace(" ", "").replace("\n", ""))
        except Exception as exc:
            return {"ok": False, "err": "十六进制解析失败: %s" % exc}
        if not payload:
            return {"ok": False, "err": "封包内容为空"}
        host, port = _parse_hostport(target)
        if not host or not port:
            return {"ok": False, "err": "目标地址非法: %r" % target}
        pr = (proto or "tcp").lower()
        r = await self._send_one(pr, host, port, payload, timeout,
                                 label="wpe-send")
        if r.get("ok"):
            with self.lock:
                self.counters["sent"] += 1
        return r

    # ---------------- 维护 ----------------
    def clear(self):
        with self.lock:
            n = len(self.packets)
            self.packets.clear()
        return {"ok": True, "cleared": n}

    def stat(self):
        with self.lock:
            s = dict(self.counters)
        s.update({
            "buffered": len(self.packets),
            "capacity": self.packets.maxlen,
            "seq": self.seq,
            "hold_on": self.hold_on,
            "hold_rule": self.hold_rule,
            "holding": len(self.held),
            "rw_on": self.rw_on,
            "rw_count": len(self.rw_rules),
            "rw_hits": dict(self.rw_hits),
            "uptime": round(time.time() - self.started, 1),
        })
        # UDP 中继的真实账本。用户报「漏包」时，这几个数就是判据：
        #   in_pkts   客户端发到中继端口的 UDP 包总数
        #   orphan    其中「无活跃关联」的（以前直接丢且不记录）
        #   adopted   其中被自愈救回来、真正代转出去的
        #   wild      当前活着的自愈关联数
        # orphan - adopted = 确实没能转出去的（这些包仍然进了封包列表，带
        # udp-orphan 标签，看得见，只是没发出去）。
        if PROXY is not None:
            try:
                u = dict(PROXY._udp_stat)
                u["wild"] = len(PROXY._udp_wild)
                u["routes"] = len(PROXY._udp_route)
                u["pending"] = len(PROXY._udp_pending)
                s["udp"] = u
            except Exception:
                pass
        return s

    # ---------------- 导出 / 导入（WPE 的 Save / Load） ----------------
    def export_json(self, limit=None, since=0, max_hex=None):
        """导出**完整**封包（含完整 hex，不是列表里那 512 字符预览）。

        ★ 为什么必须单独做这个接口：
        列表里的 hex 是截断预览。如果「导出」也走预览，那导出来的文件
        拿去导入就还原不出原始字节 —— 存了个残缺档还管它叫存档，那是骗人。
        所以导出走服务端，直接吐完整 hex。

        ★ 但这条路的产物要**整个放进一个 JSON 响应里**，所以必须有上限。
        上限一到就停，如实报出 stopped_by / complete，并且给出 **next_since** ——
        调用方拿它当下一次请求的 since，就能把剩下的接着导完（分页）。
        不给续导游标的话，「没导完」就只是一句抱怨，用户没有任何办法拿到全部。

        ★ 真要一次拿全，用 export_to_file()：落盘，不受这里约束。
        """
        lim = int(limit or self.EXPORT_MAX_PKTS)
        lim = max(1, min(lim, self.EXPORT_MAX_PKTS))
        hard_hex = int(max_hex or self.EXPORT_MAX_HEX)
        with self.lock:
            snap = list(self.packets)
        cand = [p for p in snap if not since or p["id"] > int(since)]
        out = []
        used = 0
        stopped_by = ""
        for pkt in cand:
            if len(out) >= lim:
                stopped_by = "count"
                break
            hlen = len(pkt["hex"])
            # ★ `out and` 不是多余的：单包 hex 可能比整个字节预算还大。
            #   原来没有这个条件时，max_hex=1000 配上 3000 字节的包，
            #   第一条就超预算 -> out 为空 -> complete=False 但 next_since=None，
            #   客户端**一条也拿不到，而且没有任何游标可以续** —— 死循环卡死。
            #   所以规则是：每页至少出一条，宁可让这一页超一点预算。
            #   前进性（progress guarantee）比「严格不超预算」重要得多。
            if out and used + hlen > hard_hex:
                stopped_by = "bytes"
                break
            out.append({
                "id": pkt["id"], "ts": pkt["ts"], "t": pkt["t"],
                "dir": pkt["dir"], "proto": pkt["proto"],
                "app": pkt.get("app", ""), "summary": pkt.get("summary", ""),
                "client": pkt["client"], "target": pkt["target"],
                "label": pkt["label"], "len": pkt["len"],
                "hex": pkt["hex"], "ascii": pkt["ascii"],
                "note": pkt.get("note", ""), "trunc": pkt.get("trunc", False),
                "mod_hex": pkt.get("mod_hex"), "mod_len": pkt.get("mod_len"),
                "wpe_action": pkt.get("wpe_action"),
                "imported": pkt.get("imported", False),
            })
            used += hlen
        complete = len(out) >= len(cand)
        # 续导游标：没导完就给出「下一条该从哪开始」。
        # 调用方原样回传 next_since，就能把剩下的接着取走 —— 没有它，
        # 「没导完」就只是一句抱怨，用户拿不到剩下的东西。
        next_since = out[-1]["id"] if (out and not complete) else None
        return {
            "ok": True, "format": "wpe-export/1", "packets": out,
            "exported": len(out), "buffered": len(snap),
            "candidates": len(cand), "hex_bytes": used, "full_hex": True,
            "complete": complete,
            "stopped_by": stopped_by,
            "next_since": next_since,
            "limits": {"max_packets": lim, "max_hex_bytes": hard_hex},
        }

    def export_to_file(self, since=0, kind="json"):
        """把**整个缓冲**导出到磁盘文件，不受内存响应上限约束。

        ★ 为什么必须有这条路：环形缓冲 4000 条 × 单包最多 64KB hex，
        最坏能到 500MB —— 塞进一个 JSON 响应里必然要砍（那就是原来的
        「导出不完整」）。落盘没有这个问题：磁盘写多少都行，下载走流式。

        返回里带 size / packets / hex_bytes，前端据此如实报出导了多少。
        kind='json' 是能重新导入的存档；kind='txt' 是给人看的文本（含完整 hex）。
        """
        with self.lock:
            snap = list(self.packets)
        cand = [p for p in snap if not since or p["id"] > int(since)]
        try:
            os.makedirs(self.EXPORT_DIR, exist_ok=True)
        except Exception as exc:                   # noqa: BLE001
            return {"ok": False, "err": "导出目录不可用 %s: %s"
                    % (self.EXPORT_DIR, exc)}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = "wpe-%s.%s" % (stamp, "txt" if kind == "txt" else "json")
        full = os.path.join(self.EXPORT_DIR, name)
        hex_bytes = 0
        try:
            if kind == "txt":
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write("# WPE 导出 %s　共 %d 条（hex 为完整字节，可核对长度）\n"
                             % (time.strftime("%Y-%m-%d %H:%M:%S"), len(cand)))
                    for pkt in cand:
                        hex_bytes += len(pkt["hex"])
                        fh.write("\n#%d  %s  %s  %s  [%s]  %s -> %s  %dB  [%s]\n"
                                 % (pkt["id"], pkt["t"], pkt["dir"], pkt["proto"],
                                    pkt.get("app") or "-", pkt["client"],
                                    pkt["target"], pkt["len"], pkt["label"]))
                        if pkt.get("summary"):
                            fh.write("  解析: %s\n" % pkt["summary"])
                        if pkt.get("note"):
                            fh.write("  备注: %s\n" % pkt["note"])
                        fh.write("  HEX  : %s\n" % pkt["hex"])
                        fh.write("  ASCII: %s\n"
                                 % (pkt.get("ascii") or "").replace("\n", "\\n"))
                        if pkt.get("mod_hex"):
                            fh.write("  改写 : %s   （%sB，实际转发的就是它）\n"
                                     % (pkt["mod_hex"], pkt.get("mod_len")))
            else:
                with open(full, "w", encoding="utf-8") as fh:
                    # 逐条写，不先拼成一个大字符串 —— 拼一次就多占一份内存。
                    fh.write('{"ok": true, "format": "wpe-export/1", '
                             '"complete": true, "exported": %d, "buffered": %d, '
                             '"packets": [\n' % (len(cand), len(snap)))
                    for i, pkt in enumerate(cand):
                        hex_bytes += len(pkt["hex"])
                        rec = {
                            "id": pkt["id"], "ts": pkt["ts"], "t": pkt["t"],
                            "dir": pkt["dir"], "proto": pkt["proto"],
                            "app": pkt.get("app", ""),
                            "summary": pkt.get("summary", ""),
                            "client": pkt["client"], "target": pkt["target"],
                            "label": pkt["label"], "len": pkt["len"],
                            "hex": pkt["hex"], "ascii": pkt["ascii"],
                            "note": pkt.get("note", ""),
                            "trunc": pkt.get("trunc", False),
                            "mod_hex": pkt.get("mod_hex"),
                            "mod_len": pkt.get("mod_len"),
                            "wpe_action": pkt.get("wpe_action"),
                            "imported": pkt.get("imported", False),
                        }
                        fh.write(json.dumps(rec, ensure_ascii=False))
                        fh.write(",\n" if i + 1 < len(cand) else "\n")
                    fh.write("]}\n")
        except OSError as exc:
            return {"ok": False, "err": "写文件失败: %s" % exc}
        size = os.path.getsize(full)
        log.info("WPE 导出落盘 %s：%d 条 / %.1f MB",
                 name, len(cand), size / 1048576.0)
        return {"ok": True, "name": name, "path": full, "kind": kind,
                "size": size, "exported": len(cand), "buffered": len(snap),
                "hex_bytes": hex_bytes, "complete": True,
                "truncated_pkts": sum(1 for p in cand if p.get("trunc"))}

    def import_packets(self, items):
        """导入之前导出的封包（WPE 的 Load）。

        ★ 诚实性铁律（三条，都是「不骗人」的直接推论）：
        ① 导入的包**不是实时流量**，必须打 imported=True 并在 note 里写明来路。
           不标记的话，列表里混进一批来路不明的包，看的人会当成真抓到的 ——
           那是最恶劣的一种骗人。
        ② len 与 hex 字节数对不上就**拒收**。对不上说明这份导出是列表预览
           （被截断过），拿它当原始字节用会让人以为「我抓到了全部」。
        ③ app/summary **按实际字节重新解析**，不信文件里的声明。
           文件可以写任何东西，字节才是事实。
        """
        if not isinstance(items, list):
            return {"ok": False, "err": "packets 必须是数组"}
        ok = 0
        skipped = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                skipped.append({"i": i, "why": "不是对象"})
                continue
            hx = str(it.get("hex") or "").strip().lower()
            if not hx or len(hx) % 2:
                skipped.append({"i": i, "why": "hex 缺失或长度为奇数"})
                continue
            try:
                raw = bytes.fromhex(hx)
            except Exception:
                skipped.append({"i": i, "why": "hex 非法"})
                continue
            if not raw:
                skipped.append({"i": i, "why": "hex 为空"})
                continue
            try:
                blen = int(it.get("len"))
            except Exception:
                blen = None
            if blen is not None and blen != len(raw):
                skipped.append({
                    "i": i,
                    "why": "len=%s 与 hex=%d 字节不一致（多半是列表预览被截断，拒收）"
                           % (blen, len(raw))})
                continue
            direction = str(it.get("dir") or "c2s").lower()
            if direction not in ("c2s", "s2c"):
                direction = "c2s"
            proto = str(it.get("proto") or "tcp").lower()
            trunc = len(raw) > self.MAX_PKT
            raw = raw[:self.MAX_PKT]
            # ③ 重新解析，不信文件里的 app/summary
            app, summary = _analyze(proto, direction, raw)
            orig = str(it.get("note") or "").strip()
            note = "导入（非实时流量，来自导出文件）"
            if orig:
                note += "；原备注：" + orig
            if blen is not None and blen != len(raw):
                note += "；已截断到 %d 字节" % len(raw)
            with self.lock:
                self.seq += 1
                pkt = {
                    "id": self.seq, "ts": time.time(),
                    "t": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                    "dir": direction, "proto": proto, "app": app, "summary": summary,
                    "client": str(it.get("client") or ""),
                    "target": str(it.get("target") or ""),
                    "label": str(it.get("label") or "") or "imported",
                    "len": len(raw), "hex": raw.hex(), "ascii": _pkt_ascii(raw),
                    "note": note, "trunc": trunc, "imported": True,
                }
                self.packets.append(pkt)
                self.counters["imported"] += 1
                if trunc:
                    self.counters["truncated"] += 1
            ok += 1
        return {"ok": True, "imported": ok, "skipped": len(skipped),
                "errors": skipped[:50], "buffered": len(self.packets)}


# 全局单例：serve() 里初始化。relay / UDP 转发路径通过它记录封包。
WPE = None
# 全局单例：serve() 里初始化。只为把 UDP 中继的收发/丢弃计数暴露到 /wpe/stat ——
# 「到底漏没漏包」必须让用户自己看得见，不能只躺在服务端内存里。
PROXY = None

# WPE POST body 上限（导入封包可能带完整 hex，但要有个头）。
# 超限一律 413，不静默截断 —— 截断的导入就是残缺的存档。
#
# ★ 为什么从 32MB 抬到 64MB：导出改成落盘之后，一次导出整个缓冲可以到几十 MB，
#   而**自己导出的存档必须能自己导回去**。上限比导出产物还小的话，
#   就变成「导得出、导不回」——那等于导出功能是假的。
WPE_MAX_BODY = 64 * 1024 * 1024


async def relay(a_reader, a_writer, b_reader, b_writer, label=None,
                kill_tls=False, client=None, target=None, proto="tcp"):
    """双向透传，任一侧断开就收摊。

    label 非空时，嗅探「客户端发来的第一段数据」并按类型打一条日志。
    这条日志是用来判「浏览器有没有把非标端口也升级成 HTTPS」的唯一硬证据。

    kill_tls=True 时，只要嗅到首段是 TLS ClientHello，就立刻把两侧都断开。
    这是给验证页主机用的：浏览器既然已经把 http 升级成了 https，那就让这条
    https 快速失败，逼它回落到明文 http —— 明文一回来，远程映射就生效了。
    不这么做的话，连接会一直吊着（花瓶不接管这个端口，隧道对面没人应答），
    用户只能看到转圈到超时。

    WPE：client/target 非空时，每个封包都会记进 WPE 引擎（可查、可重放）；
    断点开启且命中规则时，包会**卡在这里**等人放行 —— 这就是 WPE 的 Hold。
    """
    sniffed = {"done": False}

    async def pump(src, dst, sniff=False, direction="c2s"):
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                if sniff and not sniffed["done"]:
                    sniffed["done"] = True
                    kind = classify_payload(data)
                    if label:
                        # TLS 首段顺手解 SNI。不做 MITM 时，SNI 是唯一能看出
                        # 「这条加密连接到底找的是哪个域名」的地方 —— 不解的话
                        # 日志里只剩一串 IP:443，跟没记一样。
                        sni = extract_sni(data) if kind == "TLS" else None
                        log.info("嗅探 %s 首段=%s len=%d sni=%s head=%r",
                                 label, kind, len(data), sni or "-", data[:16])
                    if kill_tls and kind == "TLS":
                        log.info("验证主机 %s 收到 TLS -> 立刻断开，逼浏览器回落明文 HTTP",
                                 label)
                        for w in (a_writer, b_writer):
                            try:
                                w.close()
                            except Exception:
                                pass
                        return
                # ---- WPE：捕获 + 断点 ----
                # 代理层在转发路径上，这里就是 WPE 的「send/recv hook」等价点。
                if WPE is not None:
                    # ★ 抓包/断点是**旁挂**功能，它出任何错都不该把转发带走。
                    #   以前这里裸奔：WPE.record 或 maybe_hold 抛一个 ValueError
                    #   （比如畸形包让 _analyze 崩了），异常会冒到 pump 外层 ——
                    #   而外层只捕获 OSError 家族，于是这个 async 任务直接死掉，
                    #   **这条连接后续所有包全部漏抓，且日志里一个字都没有**。
                    #   用户报的「中间开始整段没包」就有这条路径的份。
                    #   现在：抓包失败只记一笔，数据照常转发，绝不连坐。
                    try:
                        pkt = WPE.record(direction, proto, client, target, data,
                                         label=label or "")
                        # 断点关着也要过 maybe_hold —— 它同时负责**自动改写规则**。
                        # 原来这里是 `and WPE.hold_on`，等于把过滤器改写那条腿砍了。
                        if pkt is not None:
                            newdata, act = await WPE.maybe_hold(pkt, data)
                            if act == "drop":
                                log.info("WPE 丢弃封包 #%d（%s %s %s）",
                                         pkt["id"], proto, direction, pkt["label"])
                                continue
                            if act == "modify":
                                log.info("WPE 改写封包 #%d：%d -> %d 字节",
                                         pkt["id"], len(data), len(newdata or b""))
                                WPE.annotate_modify(pkt["id"], newdata or b"")
                            data = newdata
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning("WPE 抓包/断点异常（已忽略，转发继续）: %s", exc)
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            try:
                dst.close()
            except Exception:
                pass

    await asyncio.gather(pump(a_reader, b_writer, sniff=True, direction="c2s"),
                         pump(b_reader, a_writer, direction="s2c"),
                         return_exceptions=True)
    for w in (a_writer, b_writer):
        try:
            w.close()
        except Exception:
            pass


# ==========================================================================
# 管理 API（CCProxy 协议）
# ==========================================================================
class AdminService:
    """CCProxy 管理 API：GET/POST /account。

    真 CCProxy 的管理页面和 HTTP 代理**共用同一个端口**（默认 808），
    所以一花的 server_list.cport 既是「通讯端口」也是「代理端口」。
    这里 handle() 被 FrontService 复用，保证同一个端口同时能当管理 API 和代理。
    """

    def __init__(self, cfg, store, stats=None):
        self.cfg = cfg
        self.store = store
        self.stats = stats

    async def __call__(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            request_line, headers, rest, _raw = await read_http_head(reader)
            if request_line is None:
                return
            await self.handle(reader, writer, request_line, headers, rest, peer)
        except Exception as exc:
            log.exception("管理API异常: %s", exc)
            try:
                writer.write(http_response(500, "Internal Server Error", str(exc)))
                await writer.drain()
            except Exception:
                pass
        finally:
            close_quietly(writer)

    async def handle(self, reader, writer, request_line, headers, rest, peer):
        # 请求行是 "GET /account HTTP/1.0" 三段，不能只按第一个空格切。
        parts = request_line.split()
        method = parts[0] if parts else ""
        raw_path = parts[1] if len(parts) > 1 else ""
        path, _, qs = raw_path.partition("?")
        q = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}

        cred = basic_auth(headers)
        if cred is None or not (
                hmac.compare_digest(cred[0], self.cfg["admin_user"]) and
                hmac.compare_digest(cred[1], self.cfg["admin_password"])):
            log.warning("管理API鉴权失败 %s %s from %s", method, path, peer)
            writer.write(http_response(
                401, "Unauthorized",
                "<h1>401 Unauthorized</h1>",
                extra="WWW-Authenticate: Basic realm=\"CCProxy\"\r\n"))
            await writer.drain()
            return

        if path == "/status":
            body = json.dumps({
                "ok": True,
                "accounts": len(self.store.list_all()),
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            writer.write(http_response(200, "OK", body, "application/json; charset=utf-8"))
            await writer.drain()
            return

        if path == "/live":
            # 实时连接计数。一花那栏「连接数」是**上限**，永远不会动；
            # 想知道谁真的连着、连了几条，只能看这里。
            snap = self.stats.snapshot() if self.stats else {}
            rows = []
            for r in self.store.list_all():
                u = r["username"]
                live = snap.get(u, {"current": 0, "peak": 0, "total": 0})
                rows.append({
                    "username": u,
                    "limit": r["connection"],
                    "current": live["current"],
                    "peak": live["peak"],
                    "total": live["total"],
                    "online": live["current"] > 0,
                })
            body = json.dumps({
                "ok": True,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "online_total": sum(1 for x in rows if x["online"]),
                "accounts": rows,
            }, ensure_ascii=False, indent=2)
            writer.write(http_response(200, "OK", body, "application/json; charset=utf-8"))
            await writer.drain()
            return

        # ---------------- WPE 网页端 API ----------------
        # 抓包/列表/详情/编辑/重放/发送/断点/放行/清空/统计，全部走这里。
        if path.startswith("/wpe"):
            await self.handle_wpe(method, path, q, headers, reader, rest, writer, peer)
            return

        if path != "/account":
            writer.write(http_response(404, "Not Found", "<h1>404</h1>"))
            await writer.drain()
            return

        if method.upper() == "GET":
            body = render_accounts(self.store.list_all(), self.store)
            writer.write(http_response(200, "OK", body))
            await writer.drain()
            return

        if method.upper() == "POST":
            length = int(headers.get("content-length") or 0)
            raw = rest or b""
            while len(raw) < length:
                chunk = await reader.read(length - len(raw))
                if not chunk:
                    break
                raw += chunk
            body = raw[:length] if length else raw
            reply = self.handle_post(body.decode("utf-8", "replace"), peer)
            writer.write(http_response(200, "OK", reply))
            await writer.drain()
            return

        writer.write(http_response(405, "Method Not Allowed"))
        await writer.drain()

    # ---------------- WPE 网页端 API ----------------
    async def handle_wpe(self, method, path, q, headers, reader, rest, writer, peer):
        """WPE 引擎的 HTTP 面。

        GET 用 query string，POST 用 JSON body（也兼容 urlencoded）。
        所有响应统一 JSON，前端直接吃。
        """
        if WPE is None:
            writer.write(http_response(
                503, "Service Unavailable",
                json.dumps({"ok": False, "err": "WPE 引擎未初始化（wpe_enabled=false？）"},
                           ensure_ascii=False),
                "application/json; charset=utf-8"))
            await writer.drain()
            return

        # POST 读 body。JSON 优先，失败退回 urlencoded。
        body = {}
        if method.upper() == "POST":
            length = int(headers.get("content-length") or 0)
            # 导入封包可能带完整 hex，body 会比较大；但也不能无限收。
            # 超限直接 413 并说明上限，不静默截断（截断的导入就是残缺的存档）。
            if length > WPE_MAX_BODY:
                writer.write(http_response(
                    413, "Payload Too Large",
                    json.dumps({"ok": False,
                                "err": "请求体 %d 字节超过上限 %d；请分批导入"
                                       % (length, WPE_MAX_BODY)},
                               ensure_ascii=False),
                    "application/json; charset=utf-8"))
                await writer.drain()
                return
            raw = rest or b""
            while len(raw) < length:
                chunk = await reader.read(length - len(raw))
                if not chunk:
                    break
                raw += chunk
            raw = raw[:length] if length else raw
            text = raw.decode("utf-8", "replace").strip()
            if text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        body = parsed
                except Exception:
                    body = {k: v[0] for k, v in
                            parse_qs(text, keep_blank_values=True).items()}

        args = dict(q)
        for k, v in body.items():
            if v is not None:
                args[k] = v

        def g(key, default=None):
            return args.get(key, default)

        def flag(key, default=False):
            v = args.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")

        try:
            if path == "/wpe/list":
                # list() 现在返回 dict（packets + 覆盖范围元信息），直接摊平进响应。
                res = {"ok": True}
                res.update(WPE.list(
                    since=g("since", 0), limit=g("limit", 300),
                    direction=g("dir", ""), proto=g("proto", ""),
                    target=g("target", ""), hexmatch=g("hex", ""),
                    label=g("label", ""), only_hold=flag("only_hold"),
                    app=g("app", ""),
                    min_len=g("min_len", 0), max_len=g("max_len", 0),
                    before=g("before", 0)))

            elif path == "/wpe/get":
                pkt = WPE.get(g("id"))
                res = {"ok": bool(pkt), "packet": pkt}
                if pkt:
                    # 结构面板的数据源：字段级拆分 + off/len，前端据此在 hex 里高亮。
                    # 这里现算，不存进环形缓冲 —— 存了会把每条记录撑大好几倍。
                    src = pkt.get("mod_hex") or pkt["hex"]
                    try:
                        raw = binascii.unhexlify(src)
                    except Exception:                 # noqa: BLE001
                        raw = b""
                    res["struct"] = _struct(pkt.get("proto") or "", pkt.get("dir") or "", raw)
                    res["struct"]["of"] = "mod_hex" if pkt.get("mod_hex") else "hex"
                else:
                    res["err"] = "封包 #%s 不存在（可能已被环形缓冲挤出）" % g("id")

            elif path == "/wpe/stat":
                res = {"ok": True, "stat": WPE.stat()}

            elif path == "/wpe/rewrite":
                # 自动改写（过滤器改写）规则的读写口。
                # GET 只读；POST 带 on / rules 才改。
                if method.upper() == "POST":
                    rules = body.get("rules") if isinstance(body, dict) else None
                    if isinstance(rules, str):
                        try:
                            rules = json.loads(rules)
                        except Exception:             # noqa: BLE001
                            rules = None
                    if rules is not None and not isinstance(rules, list):
                        res = {"ok": False, "err": "rules 必须是数组"}
                    else:
                        res = WPE.set_rewrite(on=flag("on"), rules=rules)
                else:
                    # 注意：rw_on/rw_rules/rw_hits 都在 **WPE 引擎** 上，
                    # 不在 AdminService 上 —— 写成 self.rw_on 会 AttributeError。
                    res = {"ok": True, "on": WPE.rw_on, "rules": WPE.rw_rules,
                           "hits": WPE.rw_hits, "rejected": [],
                           "note": ("规则至少要带一个条件（dir/proto/target/hex/app/"
                                    "min_len/max_len），否则会被拒收 —— 无条件改写会改掉每一个包")}

            elif path == "/wpe/hold":
                # ★ GET 必须**只读**。以前这里不分方法，一律 set_hold(flag("on"))，
                # 于是任何一次 GET /wpe/hold（探测、刷新、误点）都会被当成
                # on=false 把断点关掉 —— 一个读操作悄悄改了状态，是这类接口里
                # 最阴的一种 bug。现在 GET 只回报当前状态，不改任何东西。
                if method.upper() == "GET":
                    res = {"ok": True, "on": WPE.hold_on, "rule": WPE.hold_rule,
                           "holding": len(WPE.held),
                           "timeout": WPE.hold_timeout}
                else:
                    rule = body.get("rule") if isinstance(body, dict) else None
                    res = WPE.set_hold(flag("on"), rule=rule)
                    res["ok"] = True

            elif path == "/wpe/release":
                res = WPE.release(g("id"), g("action", "forward"),
                                  g("data") or g("data_hex"))

            elif path == "/wpe/replay":
                res = await WPE.replay(
                    g("id"), data_hex=g("data") or g("data_hex"),
                    target=g("target"), proto=g("proto"), times=g("times", 1))

            elif path == "/wpe/send":
                res = await WPE.send(g("proto", "tcp"), g("target", ""),
                                     g("data") or g("data_hex", ""))

            elif path == "/wpe/export":
                # 导出**完整** hex（不是列表预览），上限与「有没有导完」都如实报出。
                # 带 next_since：没导完时拿它当 since 再请求一次就能续上。
                res = WPE.export_json(limit=g("limit"), since=g("since", 0),
                                      max_hex=g("max_hex") or None)

            elif path == "/wpe/export/save":
                # 落盘导出：不受内存响应上限约束，能拿到整个缓冲。
                # 这是「导出完整 hex」的正路；上面那个 JSON 接口是有上限的分页版。
                #
                # ★ 只认 POST。它会**在磁盘上生成文件**，是个有副作用的动作；
                #   同一个文件里刚给 /wpe/hold 修过「GET 不该改状态」，
                #   这里不能自己再犯一遍（浏览器预取、爬虫扫到都会触发）。
                if method.upper() != "POST":
                    res = {"ok": False, "err": "导出落盘必须用 POST（会产生文件，不是读操作）"}
                else:
                    res = WPE.export_to_file(since=g("since", 0),
                                             kind=(g("kind") or "json").lower())

            elif path == "/wpe/export/download":
                # 把落盘文件流式吐出去。
                # ★ 注意：这里**必须直接写 writer 并 return**，不能只把结果塞进 res ——
                #   本函数尾部是统一的 json.dumps(res)，二进制/自定义 header 会被
                #   丢掉，浏览器拿到一个空响应，看起来就像「下载没反应」。
                # ★ name 必须**只允许**纯文件名：不校验的话 ../../etc/passwd
                #   就成了任意文件读取。导出目录里的东西才给下。
                name = g("name") or ""
                if not name or os.path.basename(name) != name:
                    res = {"ok": False, "err": "name 必须是纯文件名，不能带路径"}
                else:
                    full = os.path.join(WPE.EXPORT_DIR, name)
                    if not os.path.isfile(full):
                        res = {"ok": False, "err": "文件不存在：%s" % name}
                    else:
                        with open(full, "rb") as fh:
                            blob = fh.read()
                        ctype = ("text/plain; charset=utf-8"
                                 if name.endswith(".txt")
                                 else "application/json; charset=utf-8")
                        head = (
                            "HTTP/1.1 200 OK\r\n"
                            "Content-Type: %s\r\n"
                            "Content-Disposition: attachment; filename=\"%s\"\r\n"
                            "Content-Length: %d\r\n"
                            "Cache-Control: no-store\r\n"
                            "Connection: close\r\n\r\n"
                        ) % (ctype, name, len(blob))
                        writer.write(head.encode("utf-8") + blob)
                        await writer.drain()
                        log.info("WPE 导出下载 %s：%d 字节", name, len(blob))
                        return

            elif path == "/wpe/export/list":
                # 已落盘的导出文件列表（带大小/时间），方便找回上一次导出。
                try:
                    files = []
                    for n in sorted(os.listdir(WPE.EXPORT_DIR), reverse=True):
                        fp = os.path.join(WPE.EXPORT_DIR, n)
                        if os.path.isfile(fp):
                            files.append({"name": n, "size": os.path.getsize(fp),
                                          "mtime": int(os.path.getmtime(fp))})
                except OSError:
                    files = []
                res = {"ok": True, "dir": WPE.EXPORT_DIR, "files": files[:100],
                       "count": len(files)}

            elif path == "/wpe/import":
                # 导入之前导出的封包。导入的包一律打 imported 标记 + 写清来路。
                pk = body.get("packets") if isinstance(body, dict) else None
                if pk is None:
                    pk = args.get("packets")
                if isinstance(pk, str):
                    try:
                        pk = json.loads(pk)
                    except Exception:
                        pk = None
                if not isinstance(pk, list):
                    res = {"ok": False, "err": "缺 packets 数组（POST JSON: {\"packets\":[…]}"}
                else:
                    res = WPE.import_packets(pk)

            elif path == "/wpe/clear":
                res = WPE.clear()
                res["ok"] = True

            else:
                writer.write(http_response(
                    404, "Not Found",
                    json.dumps({"ok": False, "err": "未知 WPE 接口: %s" % path},
                               ensure_ascii=False),
                    "application/json; charset=utf-8"))
                await writer.drain()
                return
        except Exception as exc:
            log.exception("WPE API 异常 %s: %s", path, exc)
            res = {"ok": False, "err": "%s: %s" % (type(exc).__name__, exc)}

        log.info("WPE API %-6s %-14s from %s -> ok=%s",
                 method, path, peer[0] if peer else "?", res.get("ok"))
        writer.write(http_response(200, "OK",
                                   json.dumps(res, ensure_ascii=False),
                                   "application/json; charset=utf-8"))
        await writer.drain()

    def handle_post(self, body, peer):
        """CCProxy 的 POST 是 urlencoded，用 add= / edit= / delete= 区分动作。"""
        q = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        action = "unknown"
        result = "?"

        if q.get("delete"):
            action = "delete"
            ok = self.store.delete(q.get("userid", ""))
            result = "已删除" if ok else "账号不存在"

        elif q.get("add"):
            action = "add"
            user = q.get("username", "")
            if not re.match(r'^[A-Za-z0-9]+$', user) or len(user) < 5:
                result = "用户名不合法"
            else:
                bw = q.get("bandwidth", "-1")
                up, _, down = bw.partition("/")
                ok, msg = self.store.add(
                    user, q.get("password", ""),
                    q.get("disabledate", ""), q.get("disabletime", ""),
                    autodisable=1 if q.get("autodisable") else 0,
                    connection=q.get("connection", "-1"),
                    bw_up=up or "-1", bw_down=down or "-1",
                    enabled=1 if q.get("enable") else 1)
                result = msg

        elif q.get("edit"):
            action = "edit"
            old = q.get("userid", "")
            bw = q.get("bandwidth", "-1")
            up, _, down = bw.partition("/")
            ok, msg = self.store.edit(
                old,
                username=q.get("username") or None,
                password=q.get("password") or None,
                disabledate=q.get("disabledate") or None,
                disabletime=q.get("disabletime") or None,
                autodisable=1 if q.get("autodisable") else 0,
                connection=q.get("connection"),
                bw_up=up or None, bw_down=down or None,
                enabled=1 if q.get("enable") else None)
            result = msg

        log.info("管理API %-6s from %s -> %s | %s",
                 action, peer[0] if peer else "?", result,
                 {k: v for k, v in q.items() if k in
                  ("username", "userid", "disabledate", "disabletime")})
        return "OK: %s" % result


# ==========================================================================
# 实时连接计数
# ==========================================================================
class LiveStats:
    """账号维度的实时连接计数（进程内内存态）。

    为什么必须有这个：一花后台那一栏「连接数」读的是 CCProxy 协议里的
    `connection` 字段，那是**上限**，不是实时值 —— 客户端连一百次它也不会动。
    真正的实时连接数只有代理进程自己知道，所以在这里数，并通过
    `GET /live` 暴露出去。

    计数按账号维度：
      current = 当前活跃连接数
      peak    = 历史峰值（进程生命周期内）
      total   = 累计建立过的连接数
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cur = {}
        self._peak = {}
        self._total = {}

    def try_acquire(self, user, limit=-1):
        """原子地「检查上限 + 占位」。超限返回 False。"""
        with self._lock:
            n = self._cur.get(user, 0)
            if limit >= 0 and n >= limit:
                return False
            n += 1
            self._cur[user] = n
            self._peak[user] = max(self._peak.get(user, 0), n)
            self._total[user] = self._total.get(user, 0) + 1
            return True

    def release(self, user):
        with self._lock:
            if self._cur.get(user, 0) > 0:
                self._cur[user] -= 1
                return self._cur[user]
            return 0

    def current(self, user):
        with self._lock:
            return self._cur.get(user, 0)

    def snapshot(self):
        with self._lock:
            keys = set(self._cur) | set(self._peak)
            return {
                u: {"current": self._cur.get(u, 0),
                    "peak": self._peak.get(u, 0),
                    "total": self._total.get(u, 0)}
                for u in sorted(keys, key=lambda x: (x is None, x or ""))
            }


# ==========================================================================
# 代理服务（HTTP + SOCKS5），都需要账号鉴权，出口走花瓶
# ==========================================================================
class ProxyService:
    # 无主 UDP 包的自愈窗口（秒）。客户端控制连接断了以后往往还在用同一个
    # 源端口发包（它并不知道控制连接已经没了），这些包以前被静默丢掉。
    # 只要该源 IP 在这个窗口内活跃过，我们就认它、代转、并记进 WPE。
    UDP_ADOPT_TTL = 180.0
    # 自愈关联的空闲回收时间（秒）。比普通关联短 —— 它没有控制连接，
    # 唯一的存活信号就是数据包本身。
    UDP_WILD_IDLE = 60.0

    def __init__(self, cfg, store, stats=None):
        self.cfg = cfg
        self.store = store
        self.stats = stats if stats is not None else LiveStats()
        # 验证页主机集合（小写）。命中即走 captive-portal 式处理，见 relay()。
        self._verify_hosts = {
            str(h).strip().lower()
            for h in (cfg.get("verify_hosts") or []) if str(h).strip()}
        # UDP 中继里已经打过日志的 (host, port)，避免刷屏。上限 500 条。
        self._udp_seen = set()
        # ---- UDP 共享中继状态 ----
        # 云安全组只放行了极少数 UDP 端口（实测 8888/8889/8890/7863/8081/8787 通，
        # 40000 段全 BLOCKED）。一关联一端口的话，包在云边缘就被丢了 —— 实测
        # 客户端建了 399 次关联，服务端一个真实数据包都没收到。所以改成
        # 「所有关联共用一个已放行端口」，靠客户端源地址区分归属。
        self._udp_shared = None          # 共享收包 socket
        self._udp_shared_port = None     # 共享端口号
        self._udp_shared_task = None     # 全局分发循环
        self._udp_route = {}             # (ip, port) -> 关联字典
        self._udp_pending = []           # 已建立但还没被数据包认领的关联
        # 全局收发计数，用于回答「到底漏没漏包」。
        self._udp_stat = {"in_pkts": 0, "in_bytes": 0, "out_pkts": 0,
                          "out_bytes": 0, "orphan": 0, "adopted": 0}
        # 最近活跃过的客户端源 IP -> 最后见到的时间戳。
        # 无主包能不能自愈转发，就看这个 IP 最近有没有真的协商过关联 ——
        # 不设这个门，任何往 8888 打 UDP 的陌生人都能被我们代转，本机就成了
        # 开放 UDP 中继（反射放大）。有门 = 只服务「刚刚确实连过我们」的客户端。
        self._udp_recent = {}
        # 自愈出来的「野关联」：没有 TCP 控制连接，靠 reaper 自己收摊。
        # key 是客户端源地址，value 是 assoc 字典。
        self._udp_wild = {}

    def _limit_of(self, user):
        """一花下发的连接数上限。-1 / 缺失 / 非法 = 不限制。"""
        if not user:
            return -1
        try:
            row = self.store.get(user)
            if not row:
                return -1
            return int(row["connection"])
        except (TypeError, ValueError, KeyError):
            return -1

    def _acquire(self, user):
        """占一个连接位；超限返回 False。"""
        limit = self._limit_of(user)
        if self.stats.try_acquire(user, limit):
            n = self.stats.current(user)
            log.info("连接计数 %s +1 -> 当前 %d%s", user, n,
                     ("/上限 %d" % limit) if limit >= 0 else " (不限)")
            return True
        log.info("连接计数 %s 已达上限 %d，拒绝", user, limit)
        return False

    def _release(self, user):
        n = self.stats.release(user)
        log.info("连接计数 %s -1 -> 当前 %d", user, n)

    def _check(self, username, password):
        ok, reason = self.store.verify(username, password)
        if not ok:
            log.info("代理鉴权拒绝 %s: %s", username, reason)
        return ok

    async def http(self, reader, writer):
        """独立 HTTP 代理端口用（默认与 admin_port 合并不再单独监听）。"""
        peer = writer.get_extra_info("peername")
        try:
            request_line, headers, head_rest, raw_head = await read_http_head(reader)
            if request_line is None:
                return
            await self.handle_http(reader, writer, request_line, headers,
                                   head_rest, peer, raw_head)
        except Exception as exc:
            log.warning("HTTP代理异常 %s: %s", peer, exc)
        finally:
            close_quietly(writer)

    async def handle_http(self, reader, writer, request_line, headers, head_rest,
                          peer, raw_head=b""):
        parts = request_line.split(" ")
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]

        user = None
        if self.cfg["require_proxy_auth"]:
            cred = basic_auth(headers, "proxy-authorization")
            if cred is None or not self._check(*cred):
                writer.write(http_response(
                    407, "Proxy Authentication Required",
                    "<h1>407 需要代理认证</h1>",
                    extra="Proxy-Authenticate: Basic realm=\"CCProxy\"\r\n"))
                await writer.drain()
                return
            user = cred[0]

        # 连接位：鉴权通过才占；超限直接 503，不进上游。
        if not self._acquire(user):
            writer.write(http_response(503, "Service Unavailable",
                                       "<h1>503 连接数已达上限</h1>"))
            await writer.drain()
            return
        try:
            await self._serve_http(reader, writer, method, target, headers,
                                   head_rest, peer, raw_head)
        finally:
            self._release(user)

    async def _serve_http(self, reader, writer, method, target, headers,
                          head_rest, peer, raw_head=b""):
        """HTTP 代理的实际转发体（已鉴权、已占连接位）。"""
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            if not host:
                host, port = target, "443"
            try:
                ur, uw, via = await connect_target(self.cfg, host, int(port))
            except Exception as exc:
                log.warning("CONNECT %s 失败: %s", target, exc)
                writer.write(http_response(502, "Bad Gateway", "上游不可用: %s" % exc))
                await writer.drain()
                return
            log.info("CONNECT %s -> %s", target, via)
            # ---- WPE：CONNECT 请求头在 read_http_head 里已被消费，这里补记 c2s ----
            if WPE is not None and raw_head:
                pkt = WPE.record(
                    "c2s", "tcp",
                    ("%s:%d" % (peer[0], peer[1])) if peer else "",
                    "%s:%d" % (host, int(port)), raw_head,
                    label="CONNECT %s" % target)
                if pkt is not None:
                    newdata, act = await WPE.maybe_hold(pkt, raw_head)
                    if act == "drop":
                        log.info("WPE 丢弃 CONNECT #%d（%s）", pkt["id"], target)
                        close_quietly(uw)
                        writer.write(http_response(403, "Dropped by WPE", ""))
                        await writer.drain()
                        return
                    if act == "modify":
                        # 隧道已建立，改 CONNECT 头不改变任何行为 —— 如实记录，
                        # 不假装改写生效。
                        log.info("WPE 改写 CONNECT #%d 已忽略（隧道已建立，头无实际作用）",
                                 pkt["id"])
            writer.write(b"HTTP/1.0 200 Connection established\r\n\r\n")
            await writer.drain()
            # ★ head_rest：read_http_head 一次性读进来的「头部之后的数据」。
            #   CONNECT 正常没有 body，但客户端完全可能把 CONNECT 头和紧随其后的
            #   TLS ClientHello（或首个应用层数据）放在同一个 TCP 段里发过来。
            #   这部分以前**既没转发给上游、也没记进 WPE** —— 直接人间蒸发。
            #   症状：WPE 里看得到 CONNECT 头，下一跳却是隧道里的第 2 个包，
            #   中间那段数据不见了；严重时握手直接卡死（上游等不到 ClientHello）。
            if head_rest:
                log.info("CONNECT %s 头部携带 %d 字节后续数据，补转发并补记录",
                         target, len(head_rest))
                if WPE is not None:
                    pkt = WPE.record(
                        "c2s", "tcp",
                        ("%s:%d" % (peer[0], peer[1])) if peer else "",
                        "%s:%d" % (host, int(port)), head_rest,
                        label="CONNECT %s (early)" % target)
                    if pkt is not None:
                        newdata, act = await WPE.maybe_hold(pkt, head_rest)
                        if act == "drop":
                            log.info("WPE 丢弃 CONNECT 早段 #%d（%s）", pkt["id"], target)
                            close_quietly(uw)
                            return
                        if act == "modify" and newdata is not None:
                            WPE.annotate_modify(pkt["id"], newdata)
                            head_rest = newdata
                try:
                    uw.write(head_rest)
                    await uw.drain()
                except Exception as exc:
                    log.warning("CONNECT %s 早段转发失败: %s", target, exc)
                    close_quietly(uw)
                    return
            await relay(reader, writer, ur, uw, label="CONNECT %s" % target,
                        kill_tls=(bool(self.cfg.get("verify_fail_tls", True))
                                  and host.lower() in self._verify_hosts),
                        client=("%s:%d" % (peer[0], peer[1])) if peer else "",
                        target="%s:%d" % (host, int(port)))
            return

        # ---- 普通 HTTP：把请求整包交给花瓶的正向代理面 ----
        # 关键：请求行保持**绝对 URI**（`GET http://host/path HTTP/1.0`），
        # 这才是正向代理的语义，花瓶才会去解析、抓包、走它的映射规则。
        rest = head_rest or b""
        length = int(headers.get("content-length") or 0)
        while len(rest) < length:
            chunk = await reader.read(length - len(rest))
            if not chunk:
                break
            rest += chunk
        rest = rest[:length] if length else rest

        split = urlsplit(target)
        host = split.hostname or target
        port = split.port or 80
        origin_form = split.path or "/"
        if split.query:
            origin_form += "?" + split.query

        # 抹掉代理专用头，别泄漏到上游；connection/content-length 由我们重写
        hdrs = "".join(
            "%s: %s\r\n" % (k.title(), v) for k, v in headers.items()
            if k not in ("proxy-authorization", "proxy-connection",
                         "connection", "content-length"))
        hdrs += "Connection: close\r\n"
        if length:
            hdrs += "Content-Length: %d\r\n" % length

        use_charles = (self.cfg["chain_to_charles"]
                       and port not in set(self.cfg.get("charles_bypass_ports") or ()))
        try:
            if use_charles:
                ur, uw = await open_via_charles_plain(self.cfg)
                head = "%s %s HTTP/1.0\r\n%s\r\n" % (method, target, hdrs)
                via = "charles"
            else:
                ur, uw = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.cfg["connect_timeout"])
                head = "%s %s HTTP/1.0\r\n%s\r\n" % (method, origin_form, hdrs)
                via = "direct"
        except Exception as exc:
            log.warning("普通请求 %s 失败: %s", target, exc)
            writer.write(http_response(502, "Bad Gateway", "上游不可用: %s" % exc))
            await writer.drain()
            return
        log.info("%s %s -> %s", method, target, via)
        head_b = head.encode("latin-1") + rest
        # ---- WPE：普通 HTTP 的请求行+头+体在 relay 之前就被这里消费掉了，
        # 不补记的话抓包列表里「请求包」会整段缺失（只有响应，没有请求）。----
        if WPE is not None:
            pkt = WPE.record(
                "c2s", "tcp",
                ("%s:%d" % (peer[0], peer[1])) if peer else "",
                "%s:%d" % (host, port), head_b,
                label="HTTP %s" % target)
            if pkt is not None:
                newdata, act = await WPE.maybe_hold(pkt, head_b)
                if act == "drop":
                    log.info("WPE 丢弃 HTTP 请求 #%d（%s）", pkt["id"], target)
                    close_quietly(uw)
                    writer.write(http_response(403, "Dropped by WPE", ""))
                    await writer.drain()
                    return
                if act == "modify" and newdata is not None:
                    log.info("WPE 改写 HTTP 请求 #%d：%d -> %d 字节",
                             pkt["id"], len(head_b), len(newdata))
                    WPE.annotate_modify(pkt["id"], newdata)
                    head_b = newdata
        uw.write(head_b)
        await uw.drain()
        await relay(reader, writer, ur, uw, label="HTTP %s" % target,
                    client=("%s:%d" % (peer[0], peer[1])) if peer else "",
                    target="%s:%d" % (host, port))

    async def socks5(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            # 握手
            head = await asyncio.wait_for(reader.readexactly(2), timeout=15)
            if head[0] != 0x05:
                return
            nmethods = head[1]
            methods = await asyncio.wait_for(reader.readexactly(nmethods), timeout=15)
            username = None
            if self.cfg["require_proxy_auth"]:
                if 0x02 not in methods:
                    writer.write(b"\x05\xff")
                    await writer.drain()
                    return
                writer.write(b"\x05\x02")
                await writer.drain()
                # RFC1929 用户名/密码子协商
                ver = (await asyncio.wait_for(reader.readexactly(1), timeout=15))[0]
                if ver != 0x01:
                    return
                ulen = (await asyncio.wait_for(reader.readexactly(1), timeout=15))[0]
                user = (await asyncio.wait_for(reader.readexactly(ulen), timeout=15)).decode("utf-8", "replace")
                plen = (await asyncio.wait_for(reader.readexactly(1), timeout=15))[0]
                pwd = (await asyncio.wait_for(reader.readexactly(plen), timeout=15)).decode("utf-8", "replace")
                if not self._check(user, pwd):
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    return
                writer.write(b"\x01\x00")
                await writer.drain()
                username = user
            else:
                writer.write(b"\x05\x00")
                await writer.drain()

            # 请求
            req = await asyncio.wait_for(reader.readexactly(4), timeout=15)
            cmd = req[1] if len(req) > 1 else -1
            if req[0] != 0x05 or cmd not in (0x01, 0x03):
                # 以前这里静默返回 0x07，日志里一个字都没有。排查「为什么只看到
                # 浏览器的流量」时就会误判成「流量根本没过来」。现在把命令打出来。
                _names = {0x01: "CONNECT", 0x02: "BIND", 0x03: "UDP ASSOCIATE"}
                log.warning("SOCKS5 不支持的命令 cmd=0x%02x(%s) 来自 %s",
                            cmd, _names.get(cmd, "未知"), peer)
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            atyp = req[3]
            if atyp == 0x01:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            elif atyp == 0x03:
                ln = (await asyncio.wait_for(reader.readexactly(1), timeout=15))[0]
                host = (await asyncio.wait_for(reader.readexactly(ln), timeout=15)).decode("utf-8", "replace")
            elif atyp == 0x04:
                raw = await reader.readexactly(16)
                host = ":".join("%02x" % b for b in raw)
            else:
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            port = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout=15), "big")

            # 验证页主机：浏览器把 http 升级成 https 之后，让它快速失败，逼回落明文。
            kill_tls = (bool(self.cfg.get("verify_fail_tls", True))
                        and host.lower() in self._verify_hosts)

            if not self._acquire(username):
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            try:
                if cmd == 0x03:
                    # UDP ASSOCIATE。req 里的地址是客户端「打算用来发 UDP 的源地址」，
                    # 实际多半是 0.0.0.0:0，所以这里不拿它当真，只当提示。
                    if not self.cfg.get("udp_enabled", True):
                        log.warning("SOCKS5 UDP ASSOCIATE 被拒：udp_enabled=false（客户端 %s）", peer)
                        writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                        await writer.drain()
                        return
                    await self.socks5_udp_associate(reader, writer, peer)
                    return
                try:
                    ur, uw, via = await connect_target(self.cfg, host, port)
                except Exception as exc:
                    log.warning("SOCKS %s:%d 失败: %s", host, port, exc)
                    writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                    await writer.drain()
                    return
                log.info("SOCKS5 %s:%d -> %s", host, port, via)
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                await relay(reader, writer, ur, uw, label="%s:%d" % (host, port),
                            kill_tls=kill_tls,
                            client=("%s:%d" % (peer[0], peer[1])) if peer else "",
                            target="%s:%d" % (host, port))
            finally:
                self._release(username)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            log.warning("SOCKS5异常 %s: %s", peer, exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # SOCKS5 UDP ASSOCIATE —— 让 UDP 流量也能进代理
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # UDP 共享中继
    # ------------------------------------------------------------------
    async def _ensure_shared_udp(self):
        """拿到共享 UDP socket，首次调用时创建并启动全局分发循环。

        返回 (socket, port)；失败返回 (None, None)。
        """
        if self._udp_shared is not None:
            return self._udp_shared, self._udp_shared_port
        port = int(self.cfg.get("udp_relay_port", 0) or 0)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 共享模式下 SO_REUSEADDR 是安全的：这个端口只有一个 socket 在绑。
        # （一关联一端口的旧模式绝对不能设 —— 内核只把数据报投给其中一个，
        #   其余关联静默失效，踩过。）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            log.error("UDP 共享中继端口 %d 绑定失败: %s", port, exc)
            sock.close()
            return None, None
        sock.setblocking(False)
        self._udp_shared = sock
        self._udp_shared_port = sock.getsockname()[1]
        self._udp_shared_task = asyncio.ensure_future(self._udp_dispatch_loop())
        log.info("UDP 共享中继端口 %d 已就绪（所有关联复用，靠客户端源地址区分归属）",
                 self._udp_shared_port)
        return sock, self._udp_shared_port

    async def _udp_upstream_loop(self, assoc, shared):
        """目标回包 → 包成 SOCKS5 UDP → 从共享 socket 发回客户端。

        这里**不能用 asyncio.wait_for 套 1 秒超时轮询**：实测并发 10 个关联时
        会稳定丢 ~20% 的回包（并发 3 全通、并发 10 丢 2、并发 20 丢 5）。
        轮询式取消会反复 remove/add reader，回包撞上取消窗口就没了。
        改成直接 await，退出靠 usock.close() 触发 OSError 唤醒。

        以前这段是 _udp_associate_shared 里的闭包。抽出来是因为「自愈关联」
        （没有 TCP 控制连接的那种）必须复用同一份逻辑 —— 两份实现迟早走偏。
        """
        loop = asyncio.get_running_loop()
        usock = assoc["usock"]
        while True:
            try:
                data, addr = await loop.sock_recvfrom(usock, 65535)
            except asyncio.CancelledError:
                raise
            except OSError:
                # socket 已被关闭，正常退出路径
                break
            if assoc["stop"].is_set():
                break
            # 这条日志以前是 INFO，每个回包打一条 —— 实测 55 分钟刷了 2 万多行，
            # journal 全被它占满，真正有用的告警反而被埋了。降到 DEBUG，
            # 需要时把日志级别调成 DEBUG 就能拿回同样的诊断能力。
            log.debug("UDP 回包进入 usock:%d 来自 %s:%d (%d 字节) → 客户端 %s",
                      usock.getsockname()[1] if usock.fileno() >= 0 else -1,
                      addr[0], addr[1], len(data),
                      ("%s:%d" % assoc["client_addr"]) if assoc["client_addr"]
                      else "(未认领)")
            if not assoc["client_addr"]:
                # 回包比客户端首个数据包先到 —— 没法知道发给谁。计一笔便于排查。
                assoc["early"] += 1
                if assoc["early"] <= 5:
                    log.warning("UDP 回包早于认领，丢弃 %d 字节 来自 %s:%d（关联 %s:%d）",
                                len(data), addr[0], addr[1],
                                assoc["peer_ip"], assoc["peer_port"])
                continue
            # ---- WPE：UDP 下行捕获（记的是真实载荷，SOCKS5 头在下面才加）----
            if WPE is not None:
                pkt = WPE.record(
                    "s2c", "udp",
                    ("%s:%d" % assoc["client_addr"]) if assoc["client_addr"] else "",
                    "%s:%d" % (addr[0], addr[1]), data, label="udp-down")
                if pkt is not None:
                    newdata, act = await WPE.maybe_hold(pkt, data)
                    if act == "drop":
                        assoc["bad"] += 1
                        log.info("WPE 丢弃 UDP 下行 #%d（%s:%d）",
                                 pkt["id"], addr[0], addr[1])
                        continue
                    if act == "modify" and newdata is not None:
                        WPE.annotate_modify(pkt["id"], newdata)
                        data = newdata
            framed = build_socks5_udp(addr[0], addr[1], data)
            if not framed:
                assoc["bad"] += 1
                continue
            try:
                await loop.sock_sendto(shared, framed, assoc["client_addr"])
            except Exception as exc:
                # 以前这里是 except: pass —— 静默吞异常，回包丢了都不知道。
                assoc["tx_err"] += 1
                if assoc["tx_err"] <= 5:
                    log.warning("UDP 回包发往客户端失败 %s:%d: %s",
                                assoc["client_addr"][0],
                                assoc["client_addr"][1], exc)
                continue
            self._udp_stat["out_pkts"] += 1
            self._udp_stat["out_bytes"] += len(framed)
            assoc["down_pkts"] += 1
            assoc["down_bytes"] += len(data)

    async def _udp_wild_reaper(self, assoc):
        """自愈关联的空闲回收。它没有控制连接，唯一的存活信号就是数据包。"""
        while not assoc["stop"].is_set():
            # 0.5s 一跳而不是 5s：stop 一置位就能立刻退出。用 5s 的话，
            # 关联已经结束了还要多挂 5 秒才释放 socket 和注册表条目，
            # 期间同一个源地址再发包会被判成「已有路由」而走旧的死关联。
            for _ in range(10):
                await asyncio.sleep(0.5)
                if assoc["stop"].is_set():
                    return
            if time.time() - assoc["last"] > self.UDP_WILD_IDLE:
                log.info("UDP 自愈关联 %s 空闲 %.0fs 回收",
                         "%s:%d" % assoc["client_addr"], self.UDP_WILD_IDLE)
                assoc["stop"].set()
                # 必须关 socket 才能把阻塞在 recvfrom 上的 upstream 唤醒，
                # 光设 stop 事件它是不会醒的。
                try:
                    assoc["usock"].close()
                except Exception:
                    pass
                return

    async def _udp_wild_supervise(self, assoc, shared):
        """自愈关联的生命周期：跑上行循环 + 空闲回收，退出时清理注册表。

        ★★ 这里**绝对不能用 gather** —— 踩过，是真的会漏，不是慢一点。★★

        原来的写法是 `await asyncio.gather(上行循环, reaper, return_exceptions=True)`。
        gather 的语义是「两个都结束才往下走」，而上行循环唯一的退出路径是
        `usock.close()` 让 `loop.sock_recvfrom` 抛 OSError。实测（2026-09-27）：

            设 stop + close usock → 1s/3s/6s/12s 后任务状态始终 PENDING，
            `_udp_wild` 与 `_udp_route` 条目**永不删除**。

        原因：`loop.sock_recvfrom` 是在 fd 上注册 epoll reader，而 Linux 在 fd
        被 close 时会**自动把它从 epoll 摘掉** —— 那个事件再也不会触发，await
        就那样挂着。close() 唤不醒它。

        泄漏的不只是内存，是会出故障的：
          · supervise 任务对象永久 PENDING（每来一个孤儿客户端漏一个）
          · 它那个 socket fd 永不关闭（迟早撞 ulimit）
          · `_udp_route` / `_udp_wild` 里留着**死关联**，同一源地址再发包时
            会被「已有路由」判定命中而丢进死关联 —— 用户看到的就是
            「两个包中间少一个」。这曾经被当成偶发丢包查了很久。

        改成 FIRST_COMPLETED：谁先结束就收工，另一个显式 cancel 并等它落地。
        reaper 每 0.5s 检查一次 stop，所以 stop 置位后最多 0.5s 完成回收。
        """
        up = asyncio.ensure_future(self._udp_upstream_loop(assoc, shared))
        reap = asyncio.ensure_future(self._udp_wild_reaper(assoc))
        try:
            await asyncio.wait({up, reap}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            assoc["stop"].set()
            for t in (up, reap):
                if not t.done():
                    t.cancel()
            # 等取消真正落地 —— 不等的话取消会变成「Task exception was never
            # retrieved」噪音，而且 socket 可能在被取消的那一刻还被引用。
            try:
                await asyncio.gather(up, reap, return_exceptions=True)
            except asyncio.CancelledError:
                # 自己被取消（进程收尾）—— 下面的清理照做，别再往外抛。
                pass
            ca = assoc["client_addr"]
            # 只在注册表里还是自己时才删 —— 否则会把接管了这个源地址的
            # 新关联给误删掉（客户端重连后源端口复用是常态）。
            if self._udp_route.get(ca) is assoc:
                self._udp_route.pop(ca, None)
            self._udp_wild.pop(ca, None)
            try:
                assoc["usock"].close()
            except Exception:
                pass
            log.info("UDP 自愈关联结束 %s:%d 上行 %d 包/%d 字节 下行 %d 包/%d 字节",
                     ca[0], ca[1], assoc["up_pkts"], assoc["up_bytes"],
                     assoc["down_pkts"], assoc["down_bytes"])

    def _adopt_orphan(self, addr):
        """给「无主」客户端源地址临时建一个自愈关联。

        为什么需要：客户端控制连接断开后**并不知道自己断了**，往往继续用同一个
        源端口发包；也可能只是换了源端口。这些包本身完全合法（SOCKS5 头里
        带着目标地址），以前被静默丢掉 —— 用户看到的就是「两个包中间少一个」。

        安全门：只接受最近 UDP_ADOPT_TTL 秒内真的协商过关联的源 IP。
        没有这道门，任何往共享端口打 UDP 的人都能让我们代转，本机就成了
        开放 UDP 中继，还能被拿去打反射放大。
        """
        ip = addr[0]
        if time.time() - self._udp_recent.get(ip, 0.0) > self.UDP_ADOPT_TTL:
            return None
        shared = self._udp_shared
        if shared is None:
            return None
        try:
            usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            usock.setblocking(False)
            # 显式绑定：① 立刻拿到真实端口，日志和诊断才有意义；
            # ② 不依赖「recvfrom 时隐式绑定」这个平台差异 —— 普通关联是先
            #    sendto 才有回包，所以碰不到；自愈关联的 upstream 循环可能在
            #    任何出站流量之前就开始 recvfrom，靠隐式绑定不可靠。
            usock.bind(("0.0.0.0", 0))
        except OSError as exc:
            log.warning("UDP 自愈关联建 socket 失败: %s", exc)
            return None
        assoc = {
            "peer_ip": ip, "peer_port": addr[1],
            "client_addr": addr, "usock": usock,
            "stop": asyncio.Event(), "last": time.time(),
            "up_pkts": 0, "up_bytes": 0, "down_pkts": 0, "down_bytes": 0,
            "bad": 0, "early": 0, "tx_err": 0, "wild": True,
        }
        self._udp_route[addr] = assoc
        self._udp_wild[addr] = assoc
        self._udp_stat["adopted"] += 1
        log.info("UDP 无主包自愈：为 %s:%d 临时建直连关联（累计第 %d 个）",
                 addr[0], addr[1], self._udp_stat["adopted"])
        asyncio.ensure_future(self._udp_wild_supervise(assoc, shared))
        return assoc

    def _claim_assoc(self, addr):
        """给一个还没认领的关联绑定客户端 UDP 源地址。

        同一台设备上不同 App 用不同源端口，所以 (ip, port) 能区分关联。
        优先认领**同 IP 中最近建立**的那个（LIFO）—— 并发建多个关联时，
        最新的最可能是当前活跃的那条。
        """
        for a in reversed(self._udp_pending):
            if a["peer_ip"] == addr[0]:
                self._udp_pending.remove(a)
                a["client_addr"] = addr
                a["last"] = time.time()
                self._udp_route[addr] = a
                log.info("SOCKS5 UDP 关联认领客户端源 %s:%d（共享端口 %s）",
                         addr[0], addr[1], self._udp_shared_port)
                return a
        return None

    async def _udp_dispatch_loop(self):
        """全局分发：读共享 socket → 按客户端源地址找关联 → 剥 SOCKS5 头转发。

        这是「所有 UDP 都从这一个端口进」的入口。每个包都会计数，
        无主包（没有活跃关联）单独计一笔，用于回答「到底漏没漏」。
        """
        loop = asyncio.get_running_loop()
        sock = self._udp_shared
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 65535)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("UDP 共享中继收包异常: %s", exc)
                await asyncio.sleep(0.2)
                continue

            self._udp_stat["in_pkts"] += 1
            self._udp_stat["in_bytes"] += len(data)

            assoc = self._udp_route.get(addr)
            if assoc is None:
                assoc = self._claim_assoc(addr)
            if assoc is None:
                # ★ 以前这里直接 continue —— 包凭空消失，WPE 里连影子都没有。
                #   用户报的「两个包中间有包漏掉了」就是这条路径（实测 60+ 条）。
                #   成因：控制连接断开后 _udp_route 立刻 pop，但客户端并不知情，
                #   还在用同一个源端口发包；也可能只是换了源端口。
                #   现在改成两级：能自愈就代转，不能自愈也**必须留痕**。
                assoc = self._adopt_orphan(addr)
            if assoc is None:
                self._udp_stat["orphan"] += 1
                if self._udp_stat["orphan"] <= 20:
                    log.info("UDP 共享中继收到无主包 %d 字节 来自 %s:%d"
                             "（无活跃关联，已如实记录、不转发）",
                             len(data), addr[0], addr[1])
                # 诚实底线：抓不到就是抓不到，但**不能假装它不存在**。
                # 原始数据照样进 WPE，打 udp-orphan 标签 + note 说明未转发，
                # 用户一眼能分清「正常包」和「这条没出去」。
                if WPE is not None:
                    _op = parse_socks5_udp(data)
                    if _op:
                        WPE.record("c2s", "udp", "%s:%d" % addr,
                                   "%s:%d" % (_op[0], _op[1]), _op[2],
                                   label="udp-orphan", note="无活跃关联，未转发")
                    else:
                        WPE.record("c2s", "udp", "%s:%d" % addr, "",
                                   data, label="udp-orphan",
                                   note="非 SOCKS5 格式，解析不出目标，未转发")
                continue
            # 自愈关联没有控制连接，靠数据包本身续命。
            self._udp_recent[addr[0]] = time.time()

            assoc["last"] = time.time()
            parsed = parse_socks5_udp(data)
            if not parsed:
                # 客户端没按 SOCKS5 格式包 —— 解析不出目标，转发不了。
                # 但同样不能让它凭空消失：原样进 WPE 留痕，标 udp-bad。
                assoc["bad"] += 1
                if WPE is not None:
                    WPE.record(
                        "c2s", "udp",
                        ("%s:%d" % assoc["client_addr"]) if assoc["client_addr"] else "",
                        "", data, label="udp-bad",
                        note="非 SOCKS5 格式，解析不出目标，未转发")
                continue
            dst_host, dst_port, payload = parsed
            assoc["up_pkts"] += 1
            assoc["up_bytes"] += len(payload)
            # ---- WPE：UDP 上行捕获（SOCKS5 头已剥，记的是真实载荷）----
            if WPE is not None:
                pkt = WPE.record(
                    "c2s", "udp",
                    ("%s:%d" % assoc["client_addr"]) if assoc["client_addr"] else "",
                    "%s:%d" % (dst_host, dst_port), payload, label="udp-up")
                if pkt is not None:
                    # 注意：这是共享分发循环，卡包会推迟（不是丢弃）其它关联的包，
                    # 靠 hold_timeout 兜底。规则里带上 proto=udp 才建议开。
                    newdata, act = await WPE.maybe_hold(pkt, payload)
                    if act == "drop":
                        assoc["bad"] += 1
                        log.info("WPE 丢弃 UDP 上行 #%d（%s:%d）",
                                 pkt["id"], dst_host, dst_port)
                        continue
                    if act == "modify" and newdata is not None:
                        WPE.annotate_modify(pkt["id"], newdata)
                        payload = newdata
            try:
                infos = await loop.getaddrinfo(dst_host, dst_port,
                                               type=socket.SOCK_DGRAM)
                if not infos:
                    assoc["bad"] += 1
                    log.warning("UDP 目标 %s:%d 解析不到地址，丢弃", dst_host, dst_port)
                    continue
                await loop.sock_sendto(assoc["usock"], payload, infos[0][4])
                key = (dst_host, dst_port)
                if key not in self._udp_seen:
                    if len(self._udp_seen) < 500:
                        self._udp_seen.add(key)
                    log.info("UDP %s:%d -> direct (len=%d)", dst_host, dst_port,
                             len(payload))
            except Exception as exc:
                assoc["bad"] += 1
                log.warning("UDP 转发 %s:%d 失败: %s", dst_host, dst_port, exc)

    async def _udp_associate_shared(self, reader, writer, peer, shared, port):
        """共享模式下的单个 UDP 关联。"""
        loop = asyncio.get_running_loop()
        cfg = self.cfg

        # 每个关联一个「上游」socket：不绑固定端口，OS 随机分配。
        # 目标回包从这个 socket 进来，天然知道属于哪个关联 —— 这是共享收包
        # socket 之外唯一能区分回包归属的办法。
        usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        usock.setblocking(False)

        public_host = self._pick_public_host(peer)
        try:
            bnd_ip = socket.inet_aton(public_host)
        except OSError:
            log.warning("UDP 中继地址 %r 非法，回 0.0.0.0", public_host)
            public_host = "0.0.0.0"
            bnd_ip = socket.inet_aton(public_host)

        writer.write(b"\x05\x00\x00\x01" + bnd_ip + port.to_bytes(2, "big"))
        await writer.drain()

        assoc = {
            "peer_ip": peer[0], "peer_port": peer[1],
            "client_addr": None, "usock": usock,
            "stop": asyncio.Event(), "last": time.time(),
            "up_pkts": 0, "up_bytes": 0, "down_pkts": 0, "down_bytes": 0,
            "bad": 0,
            # 诊断计数：回包早于认领 / 发往客户端失败。以前这两种情况都是静默丢弃。
            "early": 0, "tx_err": 0,
        }
        self._udp_pending.append(assoc)
        # 记下「这个源 IP 刚刚真的协商过关联」。无主包的自愈门就看这一条 ——
        # 只有真协商过的客户端才配被代转，陌生人打过来的包照样只能留痕。
        self._udp_recent[peer[0]] = time.time()
        log.info("SOCKS5 UDP ASSOCIATE 中继 %s:%d 已建立"
                 "（客户端 %s:%d，共享模式，等首个数据包认领）",
                 public_host, port, peer[0], peer[1])

        async def watch_control():
            """TCP 控制连接一断，关联就结束（RFC1928 就是这么规定的）。

            但**不能立刻关 usock**。共享模式下所有关联共用一个收包端口，
            客户端源地址只能靠「首个数据包到达时认领」，而认领只能按
            IP + LIFO —— 同一个出口 IP（实测手机和电脑共用 NAT，
            都是 117.183.29.197）下并发多个关联时，「哪条 TCP 配哪个
            UDP 源」根本无法精确对应。

            实测证据：并发 10 时，3 条关联的 TCP 先断，把它们正在服务的
            usock 一起关掉，另一个关联还在等的 DNS 回包就跟着丢了
            （日志表现为「下行 0 包 / 早到 0 / 发失败 0」，pcap 里
            DNS 应答全有、但服务器只转出 7 个）。给一个宽限期，
            让在途回包先发完再收摊。
            """
            try:
                while True:
                    if not await reader.read(4096):
                        break
            except Exception:
                pass
            grace = float(cfg.get("udp_control_grace", 3.0) or 0.0)
            if grace > 0:
                log.info("UDP 关联控制连接断开 %s:%d，宽限 %.1fs 等在途回包",
                         peer[0], peer[1], grace)
                try:
                    await asyncio.sleep(grace)
                except asyncio.CancelledError:
                    raise
            assoc["stop"].set()

        async def upstream():
            # 逻辑已抽成 _udp_upstream_loop，自愈关联复用同一份，避免两份实现走偏。
            await self._udp_upstream_loop(assoc, shared)

        async def reaper():
            """空闲回收。待认领的关联一直没数据包也会被回收。"""
            idle = float(cfg.get("udp_idle_timeout", 120.0) or 0.0)
            if not idle:
                return
            while not assoc["stop"].is_set():
                await asyncio.sleep(5)
                if time.time() - assoc["last"] > idle:
                    log.info("UDP 关联 %s:%d 空闲超时回收", peer[0], peer[1])
                    assoc["stop"].set()
                    break

        tasks = [asyncio.ensure_future(watch_control()),
                 asyncio.ensure_future(upstream()),
                 asyncio.ensure_future(reaper())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            assoc["stop"].set()
            for t in tasks:
                t.cancel()
            if assoc["client_addr"]:
                self._udp_route.pop(assoc["client_addr"], None)
            if assoc in self._udp_pending:
                self._udp_pending.remove(assoc)
            try:
                usock.close()
            except Exception:
                pass
            log.info("SOCKS5 UDP ASSOCIATE 中继结束（控制 %s:%d → 数据源 %s）"
                     " 上行 %d 包/%d 字节 下行 %d 包/%d 字节 裸包 %d 早到 %d 发失败 %d",
                     peer[0], peer[1],
                     ("%s:%d" % assoc["client_addr"]) if assoc["client_addr"]
                     else "(未认领)",
                     assoc["up_pkts"], assoc["up_bytes"],
                     assoc["down_pkts"], assoc["down_bytes"], assoc["bad"],
                     assoc["early"], assoc["tx_err"])

    def _pick_public_host(self, peer):
        """挑一个「客户端能连回来」的中继地址。

        云主机是 NAT 的：网卡上只有私网 IP（本机 10.1.0.10），公网 IP 在 NAT 上，
        所以自动探测一定拿不到公网地址。必须在配置里写死 udp_public_host。
        留空时才退回自动探测（只在非 NAT 主机上正确）。
        """
        configured = (self.cfg.get("udp_public_host") or "").strip()
        if configured:
            return configured
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((peer[0], 53))
            ip = probe.getsockname()[0]
            probe.close()
            return ip
        except Exception:
            return "0.0.0.0"

    async def socks5_udp_associate(self, reader, writer, peer):
        """RFC1928 §7 的 UDP ASSOCIATE。

        为什么必须适配层自己做：花瓶（Charles）只做 TCP，没有 UDP 中继能力，
        所以这一段**不 chain_to_charles**，直接发往目标。代价是 UDP 不进花瓶，
        抓不到、也不走远程映射；换来的是「游戏 / QUIC 的 UDP 至少能被代理」。

        流程：
          1. 客户端在 TCP 控制连接上发 cmd=0x03
          2. 服务端分配一个 UDP 中继端口，回 BND.ADDR:BND.PORT
          3. 客户端把每个 UDP 包按 RSV(2) FRAG(1) ATYP DST.ADDR DST.PORT DATA 发过来
          4. 服务端剥头转发；目标回包按同样格式包好发回
          5. TCP 控制连接关闭 ⇒ 关联结束，中继端口回收

        端口策略：
          udp_relay_port > 0 → **共享模式**（默认）。所有关联复用同一个端口，
                                靠客户端源地址区分归属。这是当前唯一能通的做法，
                                因为云安全组只放行了极少数 UDP 端口。
          udp_relay_port = 0 → 旧的一关联一端口模式（端口池 40000+），
                                需要云安全组整段放行才可用。
        """
        loop = asyncio.get_running_loop()
        cfg = self.cfg

        if int(cfg.get("udp_relay_port", 0) or 0) > 0:
            shared, sport = await self._ensure_shared_udp()
            if shared is None:
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            await self._udp_associate_shared(reader, writer, peer, shared, sport)
            return

        usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 绝对不要设 SO_REUSEADDR：UDP 上允许多个 socket 绑同一端口，但内核只会把
        # 数据报投给其中一个，其余关联全部静默失效。实测踩过：5 个关联全绑到 40000，
        # 只有 1 个能收到包。靠 bind 失败来逐端口试探，才能保证一关联一端口。

        base = int(cfg.get("udp_relay_port_base", 40000))
        count = max(1, int(cfg.get("udp_relay_port_count", 16)))
        bound = None
        for off in range(count):
            try:
                usock.bind(("0.0.0.0", base + off))
                bound = base + off
                break
            except OSError:
                continue
        if bound is None:
            try:
                usock.bind(("0.0.0.0", 0))
                bound = usock.getsockname()[1]
            except OSError as exc:
                log.warning("UDP 中继绑定失败: %s（客户端 %s）", exc, peer)
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                usock.close()
                return
        usock.setblocking(False)

        public_host = self._pick_public_host(peer)
        try:
            bnd_ip = socket.inet_aton(public_host)
        except OSError:
            log.warning("UDP 中继地址 %r 非法，回 0.0.0.0", public_host)
            public_host = "0.0.0.0"
            bnd_ip = socket.inet_aton(public_host)

        writer.write(b"\x05\x00\x00\x01" + bnd_ip + bound.to_bytes(2, "big"))
        await writer.drain()
        log.info("SOCKS5 UDP ASSOCIATE 中继 %s:%d 已建立（客户端 %s:%d）",
                 public_host, bound, peer[0], peer[1])

        client_addr = None
        idle = float(cfg.get("udp_idle_timeout", 120.0) or 0.0)
        stop = asyncio.Event()

        async def watch_control():
            """TCP 控制连接一断，关联就结束（RFC1928 就是这么规定的）。"""
            try:
                while True:
                    if not await reader.read(4096):
                        break
            except Exception:
                pass
            stop.set()

        async def pump():
            nonlocal client_addr
            last = time.time()
            while not stop.is_set():
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(usock, 65535), timeout=1.0)
                except asyncio.TimeoutError:
                    if idle and client_addr and (time.time() - last) > idle:
                        log.info("UDP 中继 %s:%d 空闲超时回收", public_host, bound)
                        break
                    continue
                except OSError:
                    break
                last = time.time()

                if client_addr is None:
                    # 第一个包认定来自客户端。SOCKS5 客户端在请求里通常报 0.0.0.0:0，
                    # 真正可用的源地址只能从实际数据包里学。
                    client_addr = addr
                    log.info("SOCKS5 UDP 中继 %s:%d 学到客户端 UDP 源 %s:%d",
                             public_host, bound, addr[0], addr[1])

                if addr == client_addr:
                    parsed = parse_socks5_udp(data)
                    if not parsed:
                        continue
                    dst_host, dst_port, payload = parsed
                    try:
                        infos = await loop.getaddrinfo(
                            dst_host, dst_port, type=socket.SOCK_DGRAM)
                        if not infos:
                            continue
                        await loop.sock_sendto(usock, payload, infos[0][4])
                        key = (dst_host, dst_port)
                        if key not in self._udp_seen:
                            if len(self._udp_seen) < 500:
                                self._udp_seen.add(key)
                            log.info("UDP %s:%d -> direct (len=%d)",
                                     dst_host, dst_port, len(payload))
                    except Exception as exc:
                        log.warning("UDP 转发 %s:%d 失败: %s", dst_host, dst_port, exc)
                else:
                    framed = build_socks5_udp(addr[0], addr[1], data)
                    if framed and client_addr:
                        try:
                            await loop.sock_sendto(usock, framed, client_addr)
                        except Exception:
                            pass

        tasks = [asyncio.ensure_future(watch_control()),
                 asyncio.ensure_future(pump())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            try:
                usock.close()
            except Exception:
                pass
            log.info("SOCKS5 UDP ASSOCIATE 中继 %s:%d 结束（客户端 %s:%d）",
                     public_host, bound, peer[0], peer[1])


# ==========================================================================
# 入口
# ==========================================================================
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def setup_logging(cfg):
    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg.get("log_path"):
        os.makedirs(os.path.dirname(cfg["log_path"]), exist_ok=True)
        handlers.append(logging.FileHandler(cfg["log_path"], encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers)


async def serve(cfg):
    global WPE, PROXY
    store = AccountStore(cfg["db_path"])
    stats = LiveStats()
    if cfg.get("wpe_enabled", True):
        WPE = WpeEngine(cfg, maxlen=int(cfg.get("wpe_buffer", 4000) or 4000))
        log.info("WPE 引擎           启用  缓冲 %d 条  "
                 "/wpe/list | /wpe/get | /wpe/replay | /wpe/send | /wpe/hold",
                 WPE.packets.maxlen)
    else:
        WPE = None
        log.info("WPE 引擎           关闭（wpe_enabled=false）")
    admin = AdminService(cfg, store, stats)
    proxy = ProxyService(cfg, store, stats)
    PROXY = proxy
    front = FrontService(cfg, store, stats)
    host = cfg.get("bind_host", "0.0.0.0")

    servers = []
    servers.append(await asyncio.start_server(
        front, host, cfg["admin_port"], reuse_address=True))
    log.info("主端口(管理API + HTTP代理) %s:%d  /account | CONNECT | 普通请求",
             host, cfg["admin_port"])

    if cfg.get("http_port") and cfg["http_port"] != cfg["admin_port"]:
        servers.append(await asyncio.start_server(
            proxy.http, host, cfg["http_port"], reuse_address=True))
        log.info("HTTP代理(独立端口)         %s:%d  (需账号鉴权)", host, cfg["http_port"])

    if cfg.get("socks_port") and cfg["socks_port"] != cfg["admin_port"]:
        servers.append(await asyncio.start_server(
            proxy.socks5, host, cfg["socks_port"], reuse_address=True))
        log.info("SOCKS5代理                 %s:%d  (需账号鉴权)", host, cfg["socks_port"])

    log.info("出口链到花瓶        %s (upstream=%s, http=%d, socks=%d)",
             cfg["charles_host"], cfg.get("charles_upstream") or "socks5",
             cfg["charles_http_port"], cfg.get("charles_socks_port") or 0)
    log.info("验证页主机          %s (fail_tls=%s)",
             ",".join(cfg.get("verify_hosts") or []) or "(无)",
             cfg.get("verify_fail_tls", True))
    if cfg.get("udp_enabled", True):
        pub = (cfg.get("udp_public_host") or "").strip() or "(自动探测)"
        shared_port = int(cfg.get("udp_relay_port", 0) or 0)
        if shared_port > 0:
            log.info("UDP ASSOCIATE      启用  共享模式  中继 %s:%d  "
                     "（所有关联复用这一个端口，靠客户端源地址区分归属；"
                     "该 UDP 端口必须已被云安全组放行）",
                     pub, shared_port)
        else:
            base = int(cfg.get("udp_relay_port_base", 40000))
            cnt = max(1, int(cfg.get("udp_relay_port_count", 16)))
            log.info("UDP ASSOCIATE      启用  端口池模式  中继地址 %s  端口池 %d-%d  "
                     "（需要云安全组整段放行这段 UDP，否则包会在边缘被丢）",
                     pub, base, base + cnt - 1)
    else:
        log.info("UDP ASSOCIATE      关闭（客户端 UDP 会收到 0x07 拒绝）")
    log.info("账号库              %s", cfg["db_path"])

    async with asyncio.TaskGroup() as tg:
        for s in servers:
            tg.create_task(s.serve_forever())


def main():
    ap = argparse.ArgumentParser(description="CCProxy 管理协议适配层")
    ap.add_argument("--config", default="/opt/ccproxy-adapter/config.json")
    ap.add_argument("--selftest", action="store_true", help="只跑协议自检")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    cfg = load_config(args.config)
    setup_logging(cfg)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        log.info("收到中断，退出")


if __name__ == "__main__":
    main()
