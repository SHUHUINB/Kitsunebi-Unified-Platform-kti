#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发 UDP ASSOCIATE 压力测试。

验证共享单端口方案的**多关联正确性** —— 这是共享模式最容易被质疑的地方：
所有关联共用一个收包 socket，会不会串包？

测试设计：并发建立 N 个 UDP 关联，每个从**自己独立的本地 UDP 端口**发包，
每个包里的 DNS 查询带**唯一的 qname**（t0/t1/t2…）。回包里必须包含
和它发出时一致的 qname —— 一旦串包，qname 就对不上。

用法: python _test_udp_concurrent.py [并发数] [host] [port]
"""
import socket
import struct
import sys
import threading
import time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
HOST = sys.argv[2] if len(sys.argv) > 2 else "203.0.113.10"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8889
USER, PWD = b"TestPass1!", b"Ssh-ChangeMe-2026"
DNS = sys.argv[4] if len(sys.argv) > 4 else "114.114.114.114"


def make_query(tag):
    """构造带唯一 qname 的 DNS 查询，便于识别回包归属。"""
    name = ("t%d" % tag).encode()
    q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    q += bytes([len(name)]) + name + b"\x07example\x03com\x00\x00\x01\x00\x01"
    return q


results = {}
lock = threading.Lock()


def worker(i):
    try:
        s = socket.create_connection((HOST, PORT), 15)
        s.settimeout(15)
        s.sendall(b"\x05\x01\x02")
        s.recv(2)
        s.sendall(b"\x01" + bytes([len(USER)]) + USER + bytes([len(PWD)]) + PWD)
        s.recv(2)
        s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        rep = s.recv(10)
        if len(rep) < 10 or rep[1] != 0x00:
            raise RuntimeError("ASSOCIATE 被拒 rep=%s" % rep.hex())
        bnd_ip = socket.inet_ntoa(rep[4:8])
        bnd_port = struct.unpack("!H", rep[8:10])[0]

        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(12)
        q = make_query(i)
        pkt = (b"\x00\x00\x00\x01" + socket.inet_aton(DNS)
               + (53).to_bytes(2, "big") + q)
        u.sendto(pkt, (bnd_ip, bnd_port))
        data, _addr = u.recvfrom(4096)

        mine = ("t%d" % i).encode()
        # 回包必须带自己的 qname；如果带的是别人的，就是串包
        if mine in data:
            verdict = "OK"
        else:
            others = [j for j in range(N) if j != i
                      and ("t%d" % j).encode() in data]
            verdict = "串包→t%d" % others[0] if others else "回包无qname"
        with lock:
            results[i] = (verdict, len(data), bnd_port)
        u.close()
        s.close()
    except Exception as exc:
        with lock:
            results[i] = ("FAIL: %s" % exc, 0, 0)


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
t0 = time.time()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.time() - t0

ok = sum(1 for v in results.values() if v[0] == "OK")
print("并发 %d 个 UDP 关联，耗时 %.1fs" % (N, elapsed))
print("成功 %d / %d" % (ok, N))
print()
for i in sorted(results):
    v, ln, bp = results[i]
    print("  #%-3d %-18s %4d 字节  中继端口 %s" % (i, v, ln, bp))

ports = sorted({v[2] for v in results.values() if v[2]})
print()
print("用到的中继端口:", ports if ports else "(无)")
print(">>> 全部命中同一端口 = 共享模式生效；qname 全对 = 没有串包" if ok == N
      else ">>> 有失败/串包，需要排查")
sys.exit(0 if ok == N else 1)
