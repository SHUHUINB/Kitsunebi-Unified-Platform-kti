# -*- coding: utf-8 -*-
"""UDP 无主包自愈 —— 本地单测。

覆盖用户报的「两个包中间有包漏掉了」：
  1. 安全门：TTL 内才自愈，陌生人拒绝
  2. 自愈关联的注册/清理
  3. 自愈关联真的能收发包（upstream 复用正确）
  4. orphan 记录分支的代码路径存在（防回归）

不依赖网络，全在 127.0.0.1 上跑。
"""
import asyncio
import io
import socket
import sys
import time

sys.path.insert(0, ".")
import ccproxy_adapter as A  # noqa: E402

PASS = [0, 0]


def ck(name, cond, extra=""):
    PASS[0] += 1
    if cond:
        PASS[1] += 1
    print(("PASS " if cond else "FAIL ") + name
          + (("  | " + str(extra)) if extra != "" else ""))
    return bool(cond)


async def udp_echo(port, stop):
    """最小 UDP echo，用来证明自愈关联的回包路径是通的。"""
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.setblocking(False)
    while not stop.is_set():
        try:
            data, addr = await asyncio.wait_for(loop.sock_recvfrom(s, 4096), 0.3)
        except asyncio.TimeoutError:
            continue
        except OSError:
            break
        await loop.sock_sendto(s, b"ECHO:" + data, addr)
    s.close()


async def main():
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    ECHO_PORT = 19099
    asyncio.ensure_future(udp_echo(ECHO_PORT, stop))

    # 共享收包 socket（自愈关联要靠它把回包发回客户端）
    shared = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    shared.setblocking(False)

    svc = A.ProxyService({}, None)
    svc._udp_shared = shared

    # ---- 1. 安全门 ----
    addr_ok = ("203.0.113.9", 40001)
    svc._udp_recent[addr_ok[0]] = time.time()
    a1 = svc._adopt_orphan(addr_ok)
    ck("TTL 内：自愈成功", a1 is not None)
    ck("TTL 内：注册进 _udp_route", svc._udp_route.get(addr_ok) is a1)
    ck("TTL 内：注册进 _udp_wild", svc._udp_wild.get(addr_ok) is a1)
    ck("TTL 内：adopted 计数 +1", svc._udp_stat["adopted"] == 1,
       svc._udp_stat["adopted"])
    ck("TTL 内：client_addr 已绑定", a1["client_addr"] == addr_ok)
    ck("TTL 内：标记为 wild", a1.get("wild") is True)
    ck("TTL 内：assoc 字段齐备",
       all(k in a1 for k in ("usock", "stop", "last", "up_pkts", "up_bytes",
                             "down_pkts", "down_bytes", "bad", "early", "tx_err")))

    # ---- 2. TTL 外 / 陌生人 ----
    svc._udp_recent["198.51.100.7"] = time.time() - (A.ProxyService.UDP_ADOPT_TTL + 60)
    ck("TTL 外：拒绝自愈", svc._adopt_orphan(("198.51.100.7", 5000)) is None)
    ck("从未协商过的 IP：拒绝自愈",
       svc._adopt_orphan(("198.51.100.8", 5000)) is None)
    ck("拒绝时 adopted 不增长", svc._udp_stat["adopted"] == 1,
       svc._udp_stat["adopted"])

    # ---- 3. 自愈关联真的能收发 ----
    # 客户端侧 socket：充当「断了控制连接但还在发包」的那个客户端
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.setblocking(False)
    cli.bind(("127.0.0.1", 0))
    cli_addr = cli.getsockname()
    # 换一个干净的自愈关联，client_addr 指向本地这个 socket
    svc._udp_recent["127.0.0.1"] = time.time()
    a2 = svc._adopt_orphan(("127.0.0.1", cli_addr[1]))
    ck("本地自愈关联建立", a2 is not None)
    if a2 is not None:
        a2["client_addr"] = cli_addr          # 回包发给我们的测试 socket
        ck("自愈 usock 已显式绑定", a2["usock"].getsockname()[1] > 0,
           a2["usock"].getsockname())
        # 模拟「目标回包进入 usock」：直接往 assoc 的 usock 发一个包。
        # usock bind 在 0.0.0.0，发的时候要落到 127.0.0.1 才收得到。
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.sendto(b"HELLO-FROM-TARGET",
                  ("127.0.0.1", a2["usock"].getsockname()[1]))
        up.close()
        got = None
        for _ in range(40):                   # 最多等 2 秒
            try:
                got, _src = await asyncio.wait_for(loop.sock_recvfrom(cli, 4096), 0.05)
                break
            except asyncio.TimeoutError:
                continue
        ck("自愈关联的回包能到达客户端", got is not None, got[:24] if got else None)
        if got:
            # build_socks5_udp 包出来的应该是 RSV FRAG ATYP ADDR PORT DATA
            ck("回包是合法 SOCKS5 UDP 封装",
               got[:2] == b"\x00\x00" and got[2] == 0x00 and got[3] == 0x01,
               got[:10])
            ck("回包载荷正确", got.endswith(b"HELLO-FROM-TARGET"), got[-24:])
        ck("下行计数已累加", a2["down_pkts"] >= 1, a2["down_pkts"])
        ck("全局 out_pkts 已累加", svc._udp_stat["out_pkts"] >= 1,
           svc._udp_stat["out_pkts"])

    # ---- 4. 清理 ----
    for a in list(svc._udp_wild.values()):
        a["stop"].set()
        try:
            a["usock"].close()
        except Exception:
            pass
    await asyncio.sleep(1.0)
    ck("自愈关联回收后注册表清空", len(svc._udp_wild) == 0, len(svc._udp_wild))
    ck("回收后 _udp_route 已清理",
       all(svc._udp_route.get(k) is not a
           for k, a in [(addr_ok, a1), (("127.0.0.1", cli_addr[1]), a2)] if a),
       list(svc._udp_route.keys()))

    # ---- 5. orphan / bad 记录分支存在（防回归）----
    src = io.open("ccproxy_adapter.py", encoding="utf-8").read()
    ck("orphan 分支会 record（label=udp-orphan）",
       'label="udp-orphan"' in src)
    ck("bad 分支会 record（label=udp-bad）", 'label="udp-bad"' in src)
    ck("自愈入口 _adopt_orphan 存在", "def _adopt_orphan" in src)
    ck("upstream 已抽成 _udp_upstream_loop", "async def _udp_upstream_loop" in src)
    ck("CONNECT head_rest 已补转发", "CONNECT %s 头部携带" in src)
    ck("relay 抓包异常不再连坐", "抓包/断点异常（已忽略，转发继续）" in src)

    stop.set()
    cli.close()
    shared.close()
    print("\n==== %d PASS / %d FAIL ====" % (PASS[1], PASS[0] - PASS[1]))
    return 0 if PASS[1] == PASS[0] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
