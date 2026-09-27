#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在服务器本机直连 DNS，测链路本身的丢包率。

目的：把「适配层问题」和「服务器→DNS 链路丢包」分开。

方法：从服务器直发 N 个并发 DNS 查询到指定 DNS，统计应答率。
  如果这里也丢 ~20%  → 链路问题，与适配层无关
  如果这里 100% 应答 → 问题在适配层
"""
import socket
import sys
import threading
import time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
DNS = sys.argv[2] if len(sys.argv) > 2 else "223.5.5.5"

results = {}
lock = threading.Lock()


def query(i):
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(6)
        name = ("t%d" % i).encode()
        q = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             + bytes([len(name)]) + name + b"\x07example\x03com\x00\x00\x01\x00\x01")
        u.sendto(q, (DNS, 53))
        data, _ = u.recvfrom(4096)
        with lock:
            results[i] = ("OK" if name in data else "串包", len(data))
        u.close()
    except Exception as exc:
        with lock:
            results[i] = ("FAIL: %s" % exc, 0)


ts = [threading.Thread(target=query, args=(i,)) for i in range(N)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
el = time.time() - t0

ok = sum(1 for v in results.values() if v[0] == "OK")
print("直连 %s  并发 %d  耗时 %.1fs" % (DNS, N, el))
print("成功 %d / %d  （丢包率 %.0f%%）" % (ok, N, 100.0 * (N - ok) / N))
for i in sorted(results):
    print("  #%-3d %-14s %d 字节" % (i, results[i][0], results[i][1]))
