#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实测适配层 SOCKS5 是否支持 UDP ASSOCIATE (cmd=0x03)。

预期：返回 0x07（Command not supported）—— 说明任何 UDP 流量都进不了代理。
"""
import socket
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "203.0.113.10"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8889
USER = b"TestPass1!"
PWD = b"Ssh-ChangeMe-2026"


def main():
    s = socket.create_connection((HOST, PORT), 15)
    s.settimeout(15)
    print("连接 %s:%d 成功" % (HOST, PORT))

    s.sendall(b"\x05\x01\x02")                      # ver5, 1 method, user/pass
    print("方法协商 reply :", s.recv(2).hex())

    s.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
    print("认证 reply     :", s.recv(2).hex())

    # --- 1) CONNECT (0x01) 到 example.com:80 ---
    host = b"example.com"
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (80).to_bytes(2, "big"))
    rep = s.recv(10)
    print("CONNECT  0x01 reply:", rep.hex(), "   <-- 0x00 = 成功")

    s.close()

    # --- 2) UDP ASSOCIATE (0x03) ---
    s2 = socket.create_connection((HOST, PORT), 15)
    s2.settimeout(15)
    s2.sendall(b"\x05\x01\x02")
    s2.recv(2)
    s2.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
    s2.recv(2)
    s2.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")   # cmd=0x03 UDP ASSOCIATE
    rep2 = s2.recv(10)
    print("UDPASSOC 0x03 reply:", rep2.hex())
    if rep2 and rep2[1] == 0x07:
        print("  >>> 0x07 = Command not supported —— UDP 完全不被支持")
    elif rep2 and rep2[1] == 0x00:
        print("  >>> 0x00 = 支持 UDP 中继")
    else:
        print("  >>> 其他/空回复")
    s2.close()


if __name__ == "__main__":
    main()
