#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测服务器上哪些 UDP 端口从外网可达。

用途：决定 UDP 中继能不能复用「已经为 TCP 开过」的端口号，从而免去改云安全组。

服务器侧先跑 UDP echo，本机侧再打过去。
"""
import socket
import sys
import threading
import time

HOST = "203.0.113.10"
PORTS = [int(x) for x in sys.argv[1:]] or [8888, 8889, 8890, 8893, 40000, 53]


def probe(port):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(4)
    try:
        u.sendto(b"probe-%d" % port, (HOST, port))
        data, addr = u.recvfrom(1024)
        return port, "OPEN  reply=%r from %s" % (data, addr)
    except socket.timeout:
        return port, "BLOCKED/TIMEOUT"
    except Exception as exc:
        return port, "ERR %s" % exc
    finally:
        u.close()


def main():
    results = {}
    threads = []
    for p in PORTS:
        t = threading.Thread(target=lambda p=p: results.__setitem__(p, probe(p)))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    for p in PORTS:
        print("UDP %-6d %s" % (p, results.get(p)))


if __name__ == "__main__":
    main()
