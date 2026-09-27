#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从外网做 SOCKS5 UDP ASSOCIATE 端到端测试。

覆盖三件事：
  1. 适配层是否接受 cmd=0x03 并回一个中继端点
  2. 云安全组是否放行了那个 UDP 中继端口（本机 → 公网 IP 打 UDP）
  3. 中继是否真的能把 DNS 查询转出去、把应答转回来

用法: python _test_udp_e2e.py [host] [port]
"""
import socket
import struct
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "203.0.113.10"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8889
USER, PWD = b"TestPass1!", b"Ssh-ChangeMe-2026"

DNS_SERVER = "114.114.114.114"
QUERY = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
         b"\x07example\x03com\x00\x00\x01\x00\x01")


def main():
    s = socket.create_connection((HOST, PORT), 15)
    s.settimeout(15)
    s.sendall(b"\x05\x01\x02")
    print("方法协商 :", s.recv(2).hex())
    s.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
    print("认证     :", s.recv(2).hex())

    s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
    rep = s.recv(10)
    print("UDP ASSOCIATE reply:", rep.hex())
    if len(rep) < 10 or rep[1] != 0x00:
        print(">>> 不支持 / 失败，退出")
        return 1
    bnd_ip = socket.inet_ntoa(rep[4:8])
    bnd_port = struct.unpack("!H", rep[8:10])[0]
    print(">>> 中继端点: %s:%d" % (bnd_ip, bnd_port))

    pkt = (b"\x00\x00\x00\x01" + socket.inet_aton(DNS_SERVER)
           + struct.pack("!H", 53) + QUERY)

    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(12)
    u.sendto(pkt, (bnd_ip, bnd_port))
    print("已向 %s:%d 发出 %d 字节的 SOCKS5 UDP 包（内含 DNS 查询）"
          % (bnd_ip, bnd_port, len(pkt)))
    try:
        data, addr = u.recvfrom(4096)
    except socket.timeout:
        print(">>> 超时。可能原因：云安全组没放行 UDP %d（入站）" % bnd_port)
        return 2
    print(">>> 收到回包 %d 字节，来自 %s" % (len(data), addr))
    print("    回包头:", data[:10].hex())
    if len(data) > 12:
        print("    >>> UDP 中继端到端正常 ✅")
        return 0
    print("    >>> 回包异常")
    return 3


if __name__ == "__main__":
    sys.exit(main())
