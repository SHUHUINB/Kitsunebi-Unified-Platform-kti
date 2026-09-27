# -*- coding: utf-8 -*-
"""UDP 无主包自愈 —— 线上端到端验证（在服务器本机跑）。

复现用户报的「两个包中间有包漏掉了」：

  1. 建 SOCKS5 UDP 关联，正常发 1 个包 -> 收到 echo 回包（证明基线正常）
  2. 关掉 TCP 控制连接（客户端并不知道自己断了）
  3. 等过 udp_control_grace（默认 3s），让关联真正收摊、路由条目被摘掉
  4. 继续用**同一个 UDP 源端口**发包
       旧行为：无主包 -> 静默丢弃，WPE 里连影子都没有  <- 这就是漏包
       新行为：自愈建直连关联 -> 代转 -> 回包能回来 -> 记进 WPE

判据全部取自服务端真实状态（/wpe/stat 的 udp 账本），不看日志自述。
"""
import base64
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

HOST = "127.0.0.1"
SOCKS_PORT = 8889        # SOCKS5 控制连接（UDP ASSOCIATE 走这里）
UDP_RELAY_PORT = 8888    # UDP 共享中继端口（数据包发这里）
ADMIN_PORT = 8893
ADMIN_USER = "admin"
ADMIN_PWD = "Adapter-Token-ChangeMe"
PROXY_USER = "TestPass1!"
PROXY_PWD = "Ssh-ChangeMe-2026"
ECHO_PORT = 19099

PASS = [0, 0]


def ck(name, cond, extra=""):
    PASS[0] += 1
    if cond:
        PASS[1] += 1
    print(("PASS " if cond else "FAIL ") + name
          + (("  | " + str(extra)) if extra != "" else ""))
    return bool(cond)


def api(path):
    req = urllib.request.Request("http://%s:%d%s" % (HOST, ADMIN_PORT, path))
    tok = base64.b64encode(("%s:%s" % (ADMIN_USER, ADMIN_PWD)).encode()).decode()
    req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------- UDP echo（模拟目标服务器） ----------------
echo_stop = threading.Event()


def echo_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", ECHO_PORT))
    s.settimeout(0.3)
    while not echo_stop.is_set():
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        s.sendto(b"ECHO:" + data, addr)
    s.close()


# ---------------- SOCKS5 ----------------
def socks5_udp_associate():
    s = socket.create_connection((HOST, SOCKS_PORT), timeout=10)
    s.sendall(b"\x05\x02\x00\x02")
    r = s.recv(2)
    assert r == b"\x05\x02", "方法协商失败: %r" % r
    u, p = PROXY_USER.encode(), PROXY_PWD.encode()
    s.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
    r = s.recv(2)
    assert r == b"\x01\x00", "鉴权失败: %r" % r
    s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
    r = s.recv(10)
    assert len(r) == 10 and r[0] == 5 and r[1] == 0, "UDP ASSOCIATE 失败: %r" % r
    return s, struct.unpack(">H", r[8:10])[0]


def wrap(dst_host, dst_port, payload):
    return (b"\x00\x00\x00\x01" + socket.inet_aton(dst_host)
            + struct.pack(">H", dst_port) + payload)


def recv_one(sock, timeout=2.0):
    sock.settimeout(timeout)
    try:
        data, _ = sock.recvfrom(65535)
        return data
    except socket.timeout:
        return None


def unwrap_socks5_udp(buf):
    """剥掉 SOCKS5 UDP 头，返回载荷。"""
    if len(buf) < 10 or buf[0] != 0 or buf[1] != 0:
        return None
    atyp = buf[3]
    off = 4
    if atyp == 0x01:
        off += 4
    elif atyp == 0x03:
        off += 1 + buf[4]
    elif atyp == 0x04:
        off += 16
    else:
        return None
    off += 2
    return buf[off:]


def main():
    threading.Thread(target=echo_server, daemon=True).start()
    time.sleep(0.4)

    st0 = api("/wpe/stat")["stat"]
    u0 = st0.get("udp") or {}
    ck("服务端暴露 UDP 账本", "orphan" in u0 and "adopted" in u0, u0)
    orphan0 = u0.get("orphan", 0)
    adopted0 = u0.get("adopted", 0)

    # ---- 1. 基线：正常关联 ----
    ctl, relay_port = socks5_udp_associate()
    ck("UDP ASSOCIATE 拿到共享中继端口", relay_port == UDP_RELAY_PORT, relay_port)
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.bind(("127.0.0.1", 0))
    cli_addr = cli.getsockname()
    print("    客户端 UDP 源端口 = %d" % cli_addr[1])

    cli.sendto(wrap("127.0.0.1", ECHO_PORT, b"NORMAL-1"), (HOST, relay_port))
    got = recv_one(cli)
    ck("基线：正常关联的上行能往返", got is not None, got[:32] if got else None)
    if got:
        ck("基线：回包载荷正确", unwrap_socks5_udp(got) == b"ECHO:NORMAL-1",
           unwrap_socks5_udp(got))

    # ---- 2. 关掉控制连接，等过 grace ----
    ctl.close()
    print("    已关闭 TCP 控制连接，等 4.5s 让关联收摊（grace=3s）...")
    time.sleep(4.5)

    # ---- 3. 继续用同一源端口发包（这就是「客户端不知情」）----
    sent = [b"ADOPT-%d" % i for i in range(1, 4)]
    for p in sent:
        cli.sendto(wrap("127.0.0.1", ECHO_PORT, p), (HOST, relay_port))
        time.sleep(0.12)

    got_list = []
    for _ in range(3):
        g = recv_one(cli, timeout=2.5)
        if g is None:
            break
        got_list.append(unwrap_socks5_udp(g))
    ck("自愈：断连后的包全部收到回包",
       len(got_list) == 3, got_list)
    ck("自愈：回包内容逐条对上",
       got_list == [b"ECHO:" + p for p in sent], got_list)

    # ---- 4. 服务端账本 ----
    time.sleep(0.5)
    st1 = api("/wpe/stat")["stat"]
    u1 = st1.get("udp") or {}
    print("    UDP 账本: %s" % json.dumps(u1, ensure_ascii=False))
    ck("自愈计数增加", u1.get("adopted", 0) > adopted0,
       "%s -> %s" % (adopted0, u1.get("adopted")))
    ck("本轮 3 个包被救回（adopted 增量 >= 1）",
       u1.get("adopted", 0) - adopted0 >= 1,
       u1.get("adopted", 0) - adopted0)
    ck("in_pkts 记到了这 3 个包", u1.get("in_pkts", 0) >= 4,
       u1.get("in_pkts"))
    ck("本轮没有产生新的无主丢弃", u1.get("orphan", 0) == orphan0,
       "%s -> %s" % (orphan0, u1.get("orphan")))

    # ---- 5. 包真的进了 WPE 列表 ----
    lst = api("/wpe/list?limit=200")
    rows = lst.get("packets") or []
    hit = [r for r in rows if "ADOPT-" in (r.get("summary") or "")
           or "ADOPT" in (r.get("ascii") or "")]
    ck("自愈的包出现在封包列表里", len(hit) >= 3, len(hit))
    if hit:
        ck("自愈包带 udp 协议标记",
           all((r.get("proto") == "udp") for r in hit), [r.get("proto") for r in hit])
        print("    命中示例: id=%s summary=%r label=%r"
              % (hit[0].get("id"), hit[0].get("summary"), hit[0].get("label")))

    # ---- 6. 收尾 ----
    cli.close()
    echo_stop.set()
    print("\n==== %d PASS / %d FAIL ====" % (PASS[1], PASS[0] - PASS[1]))
    return 0 if PASS[1] == PASS[0] else 1


if __name__ == "__main__":
    sys.exit(main())
