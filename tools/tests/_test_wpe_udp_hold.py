#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE UDP 断点 + 30s 超时兜底 实测（服务器本机）。

为什么单独测 UDP：UDP 走的是共享单端口中继 + 分发循环，断点卡包的副作用
和 TCP 完全不同（会推迟其它关联的包）。TCP 通了不代表 UDP 通。

★ 隔离铁律（上一版就是栽在这里）：
断点规则**必须收窄到测试专用目标**（本脚本自建的 127.0.0.1:18099）。
如果规则只写 {"proto":"udp"}，线上真实设备的游戏 UDP 流量会被一起拦下 ——
测试自己的包被淹没在几千条噪声里，放行循环放的全是设备包，
最后 7 项断言集体失败，看着像产品坏了，其实只是测试没隔离。
真实流量是别人的连接，测试不该碰。

端到端判据：客户端收到的字节必须等于记录里写的「实际转发内容」，否则就是骗人。
"""
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

SOCKS_PORT = 8889
USER = b"TestPass1!"
PWD = b"Ssh-ChangeMe-2026"
RELAY = ("127.0.0.1", 8888)          # 本机自测：避开 NAT 发夹（见 _test_wpe_udp.py 注释）
ECHO = ("127.0.0.1", 18099)
MARK = "18099"                        # 隔离标记：只认目标里带这个端口的包
API = "http://127.0.0.1:8893"
AUTH = "Basic YWRtaW46Y2NweC04ODkzLUtkOTNtWnE="

# ★ 收窄后的断点规则：只拦测试自己的 UDP 包，不碰线上真实流量。
HOLD_RULE = {"proto": "udp", "target": MARK}

fails = []


def api(path, data=None):
    req = urllib.request.Request(API + path, headers={"Authorization": AUTH})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def check(label, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


def mine(p):
    """只认测试自己的包（目标含 18099）。"""
    return MARK in (p.get("target") or "")


def held_now(limit=50):
    """当前卡住的、且属于测试的包。"""
    return [p for p in api("/wpe/list?only_hold=1&limit=%d&target=%s" % (limit, MARK))["packets"]
            if mine(p)]


def my_pkts(limit=200, action=None):
    """测试自己的记录（可按处置过滤）。"""
    out = [p for p in api("/wpe/list?limit=%d&target=%s" % (limit, MARK))["packets"] if mine(p)]
    if action:
        out = [p for p in out if p.get("wpe_action") == action]
    return out


class Echo(threading.Thread):
    """UDP echo 源站：收到什么就回 'ECHO:' + 原文。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(ECHO)
        self.sock.settimeout(1.0)
        self.stop = False
        self.seen = []

    def run(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.seen.append(data)
            try:
                self.sock.sendto(b"ECHO:" + data, addr)
            except OSError:
                pass


def socks5_assoc():
    t = socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=10)
    t.sendall(b"\x05\x01\x02")
    assert t.recv(2) == b"\x05\x02"
    t.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
    assert t.recv(2) == b"\x01\x00"
    t.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
    rep = t.recv(10)
    assert rep[0] == 5 and rep[1] == 0, rep
    return t


def udp_frame(payload):
    return b"\x00\x00\x00\x01" + socket.inet_aton(ECHO[0]) + struct.pack(">H", ECHO[1]) + payload


def udp_roundtrip(payload, timeout=8):
    """经 SOCKS5 UDP 关联发一次，返回收到的载荷（去掉 SOCKS5 头）。"""
    t = socks5_assoc()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(timeout)
    try:
        u.sendto(udp_frame(payload), RELAY)
        try:
            data, _ = u.recvfrom(65535)
        except socket.timeout:
            return None
        # 剥 SOCKS5 UDP 响应头
        if len(data) > 10 and data[0] == 0 and data[1] == 0:
            atyp = data[3]
            off = 4 + (4 if atyp == 1 else 16 if atyp == 4 else 0) + 2
            if atyp == 3:
                off = 4 + 1 + data[4] + 2
            return data[off:]
        return data
    finally:
        u.close()
        t.close()


def drain_hold(action="forward", pick=None, data_hex=None, rounds=10, label=""):
    """把测试自己的断点队列清空。

    ★ 规则是双向的：放行了 c2s 请求后，s2c 回包会紧接着被同一条规则再卡一次。
    所以必须循环放行直到队列真空，否则客户端永远等不到回包。
    只处理 target 含 18099 的包 —— 别人的连接不碰。
    """
    done = []
    for _ in range(rounds):
        held = held_now()
        if not held:
            break
        for h in held:
            act, body = action, None
            if pick and data_hex and pick(h):
                act, body = "modify", data_hex
            r = api("/wpe/release", {"id": h["id"], "action": act,
                                     **({"data_hex": body} if body else {})})
            done.append((h["id"], h["dir"], act, r.get("ok")))
        time.sleep(0.3)
    if done:
        print("    %s放行: %s" % (label, done))
    return done


def main():
    echo = Echo()
    echo.start()
    time.sleep(0.4)
    api("/wpe/hold", {"on": False})
    api("/wpe/clear")

    print("== 0. 基线：不断点，UDP 往返正常 ==")
    got = udp_roundtrip(b"HELLO-A")
    print("    收到 %r" % got)
    check("UDP 基线往返正常", got == b"ECHO:HELLO-A", got)

    print("== 1. UDP 断点 drop（规则已收窄到 target=%s）==" % MARK)
    api("/wpe/hold", {"on": True, "rule": HOLD_RULE})
    box = {}

    def req1():
        box["r"] = udp_roundtrip(b"HELLO-B", timeout=20)

    t1 = threading.Thread(target=req1)
    t1.start()
    time.sleep(2.0)
    held = held_now()
    check("UDP 包被卡住", len(held) >= 1, len(held))
    if held:
        print("    卡住的：%s" % [(h["id"], h["dir"], h["label"], h["target"]) for h in held])
    drain_hold("drop", label="丢弃")
    t1.join(25)
    check("丢弃后客户端收不到回包", box.get("r") is None, box.get("r"))
    check("丢弃计数增加", api("/wpe/stat")["stat"]["dropped"] >= 1)
    # ★ 留痕校验：被丢的包在列表里必须看得出来（只看测试自己的包，不受线上噪声影响）
    dl = my_pkts(limit=200)
    print("    测试自己的记录：")
    for p in dl:
        print("      #%-3d %-4s %-4s %-8s target=%-18s wpe_action=%s note=%r"
              % (p["id"], p["dir"], p["proto"], p.get("label"), p.get("target"),
                 p.get("wpe_action"), p.get("note")))
    dropped = [p for p in dl if p.get("wpe_action") == "drop"]
    check("丢弃在记录里留痕", len(dropped) >= 1, len(dropped))
    if dropped:
        print("    note=%r" % dropped[0].get("note"))
        check("丢弃 note 说明没转发出去", "没有转发" in (dropped[0].get("note") or ""),
              dropped[0].get("note"))

    print("== 2. UDP 断点 modify ==")
    box2 = {}

    def req2():
        box2["r"] = udp_roundtrip(b"HELLO-C", timeout=20)

    t2 = threading.Thread(target=req2)
    t2.start()
    time.sleep(2.0)
    held2 = held_now()
    check("第二次被卡住", len(held2) >= 1, len(held2))
    mod_id = None
    # ★ 按 c2s 精确挑包：s2c 是回包，改它改不到源站。
    c2s = [h for h in held2 if h["dir"] == "c2s"]
    if c2s:
        mod_id = c2s[0]["id"]
        print("    改写目标 #%d（c2s，%s）" % (mod_id, c2s[0]["target"]))
        drain_hold("forward", pick=lambda h: h["dir"] == "c2s" and h["id"] == mod_id,
                   data_hex=b"HELLO-C-MODIFIED".hex(), label="改写")
    else:
        drain_hold("forward", label="放行")
    t2.join(25)
    print("    客户端收到 %r" % box2.get("r"))
    check("客户端收到改写后的内容", box2.get("r") == b"ECHO:HELLO-C-MODIFIED", box2.get("r"))
    check("源站实收也是改写后的内容",
          b"HELLO-C-MODIFIED" in echo.seen, echo.seen[-3:])
    if mod_id:
        after = api("/wpe/get?id=%d" % mod_id)["packet"]
        print("    note=%r mod_len=%s wpe_action=%s"
              % (after.get("note"), after.get("mod_len"), after.get("wpe_action")))
        check("记录 mod_len 等于真实转发长度",
              after.get("mod_len") == len(b"HELLO-C-MODIFIED"),
              "%s vs %d" % (after.get("mod_len"), len(b"HELLO-C-MODIFIED")))
        check("记录 wpe_action=modify", after.get("wpe_action") == "modify")

    print("== 3. 断点 forward 留痕 ==")
    box3 = {}

    def req3():
        box3["r"] = udp_roundtrip(b"HELLO-D", timeout=20)

    t3 = threading.Thread(target=req3)
    t3.start()
    time.sleep(2.0)
    drain_hold("forward", label="放行")
    t3.join(25)
    check("forward 后客户端收到原样回包", box3.get("r") == b"ECHO:HELLO-D", box3.get("r"))
    fwd = my_pkts(limit=200, action="forward")
    check("forward 在记录里留痕", len(fwd) >= 1, len(fwd))
    if fwd:
        print("    note=%r" % fwd[0].get("note"))

    print("== 4. ★ 30s 超时兜底：卡住后不放行，必须自动放行而不是把连接吊死 ==")
    api("/wpe/hold", {"on": True, "rule": HOLD_RULE})
    box4 = {}

    def req4():
        box4["r"] = udp_roundtrip(b"HELLO-TIMEOUT", timeout=60)

    t4 = threading.Thread(target=req4)
    t4.start()
    t0 = time.time()
    time.sleep(3.0)
    held4 = held_now()
    check("超时测试：包确实被卡住了", len(held4) >= 1, len(held4))
    print("    不放行，等自动超时……")
    t4.join(90)
    dt = time.time() - t0
    print("    客户端收到 %r（等待 %.1fs）" % (box4.get("r"), dt))
    check("超时后客户端仍拿到回包（没被吊死）",
          box4.get("r") == b"ECHO:HELLO-TIMEOUT", box4.get("r"))
    # ★ 断点规则是双向的：c2s 超时 30s 放行后，s2c 回包会再被卡 30s，
    # 所以一来一回最长 60s。断言按「单包 30s、双向最多 ~60s」来判。
    check("等待时长符合「每包 30s、双向最多约 60s」", 25 <= dt <= 80, "%.1fs" % dt)
    to = my_pkts(limit=200, action="timeout")
    check("超时在记录里留痕", len(to) >= 1, len(to))
    print("    超时留痕 %d 条：%s" % (len(to), [(p["id"], p["dir"]) for p in to]))
    if to:
        print("    note=%r" % to[0].get("note"))
        check("超时 note 说明是自动放行", "超时" in (to[0].get("note") or ""),
              to[0].get("note"))
    check("测试的断点队列已空（没有残留）", len(held_now()) == 0)

    api("/wpe/hold", {"on": False})
    echo.stop = True
    try:
        echo.sock.close()
    except Exception:
        pass

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
