#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE UDP 捕获实测：走 SOCKS5 UDP ASSOCIATE 发一次 DNS 查询，再查 WPE 列表。"""
import json
import socket
import struct
import sys
import time
import urllib.request

HOST = "127.0.0.1"
SOCKS_PORT = 8889
USER = b"TestPass1!"
PWD = b"Ssh-ChangeMe-2026"
API = "http://127.0.0.1:8893"
AUTH = "Basic YWRtaW46Y2NweC04ODkzLUtkOTNtWnE="  # admin:Adapter-Token-ChangeMe


def api(path, data=None):
    req = urllib.request.Request(API + path, headers={"Authorization": AUTH})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def socks5_udp_associate():
    t = socket.create_connection((HOST, SOCKS_PORT), timeout=10)
    t.sendall(b"\x05\x01\x02")
    assert t.recv(2) == b"\x05\x02", "no-auth-method"
    t.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
    assert t.recv(2) == b"\x01\x00", "auth-failed"
    t.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
    rep = t.recv(10)
    assert rep[0] == 5 and rep[1] == 0, "assoc-failed %r" % rep
    relay_ip = ".".join(str(b) for b in rep[4:8])
    relay_port = struct.unpack(">H", rep[8:10])[0]
    return t, relay_ip, relay_port


def dns_query(qname=b"example.com"):
    q = b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for part in qname.split(b"."):
        q += bytes([len(part)]) + part
    return q + b"\x00\x00\x01\x00\x01"


def main():
    print("== 复位 ==")
    api("/wpe/hold", {"on": False})
    api("/wpe/clear")

    print("== 建立 SOCKS5 UDP ASSOCIATE ==")
    t, rip, rport = socks5_udp_associate()
    print("SOCKS5 回的 relay=%s:%d" % (rip, rport))
    # 从服务器本机自测时，发到公网 IP 会被 NAT 发夹改掉源地址（实测变成
    # 203.0.113.10:xxxxx），与 TCP 控制连接的对端 IP（127.0.0.1）对不上，
    # 认领失败 -> 无主包丢弃。所以本机自测直接打 127.0.0.1，源地址才是 127.0.0.1。
    # 真实客户端（手机）从外网进，源 IP 与控制连接一致，走 SOCKS5 返回的地址即可。
    rip, rport = "127.0.0.1", 8888
    print("本机自测改用 relay=%s:%d" % (rip, rport))

    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(8)
    payload = dns_query()
    frame = b"\x00\x00\x00\x01" + socket.inet_aton("223.5.5.5") + struct.pack(">H", 53) + payload
    u.sendto(frame, (rip, rport))
    print("已发 UDP 上行 %d 字节（载荷 %d）" % (len(frame), len(payload)))
    try:
        data, addr = u.recvfrom(65535)
        print("收到 UDP 下行 %d 字节 来自 %s" % (len(data), addr))
    except socket.timeout:
        print("UDP 下行超时（8s）")
    u.close()
    time.sleep(1.0)

    print("== WPE 列表（proto=udp）==")
    d = api("/wpe/list?limit=20&proto=udp")
    for p in d["packets"]:
        print("#%-4d %-4s %-4s %-16s %-18s len=%-5d %s"
              % (p["id"], p["dir"], p["proto"], p["target"], p["client"],
                 p["len"], p["label"]))
        print("      ascii: %s" % p["ascii"][:70])
    print("udp 包数=%d" % len(d["packets"]))

    print("== 全部列表 ==")
    d2 = api("/wpe/list?limit=20")
    print("total=%d" % len(d2["packets"]))
    print("== stat ==")
    print(json.dumps(api("/wpe/stat")["stat"], ensure_ascii=False))
    t.close()


if __name__ == "__main__":
    sys.exit(main())
