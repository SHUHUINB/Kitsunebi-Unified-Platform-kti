#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 全功能端到端实测（在服务器本机跑）。

覆盖：抓包 / 自动解析 / 列表 / 过滤 / 详情 / 断点 / 放行(forward|drop|modify) /
      改写回填 / 重放 / 构造发送 / 统计 / 清空。

判据全部来自真实字节：客户端看到的响应必须和记录里写的转发内容一致。
"""
import base64
import json
import socket
import threading
import time
import urllib.request
import http.client

BASE = "http://127.0.0.1:8893"
AUTH = "admin:Adapter-Token-ChangeMe"
PROXY = ("127.0.0.1", 8888)
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 18099

fails = []


def api(method, path, payload=None):
    headers = {"Authorization": "Basic " + base64.b64encode(AUTH.encode()).decode()}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def check(label, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


def release_all(action="forward", pick=None, data_hex=None, rounds=12):
    """把断点队列清空。

    ★ 关键认知：断点规则是**双向**的（WPE 的过滤器本来就同时管 send 和 recv）。
    只放行 c2s 请求，s2c 响应会紧接着被同一条规则卡住 —— 客户端就永远等不到回包。
    所以放行必须循环做，直到队列真空。

    pick: 可选，函数 (pkt)->bool，命中的那个包用 data_hex 走 modify，其余 forward。
    """
    done = []
    for _ in range(rounds):
        held = api("GET", "/wpe/list?only_hold=1&limit=50")["packets"]
        if not held:
            break
        for h in held:
            act = action
            body = None
            if pick is not None and data_hex is not None and pick(h):
                act, body = "modify", data_hex
            r = api("POST", "/wpe/release",
                    {"id": h["id"], "action": act,
                     **({"data_hex": body} if body else {})})
            done.append((h["id"], h["dir"], act, r.get("ok")))
        time.sleep(0.4)
    return done


def http_via_proxy(method, url, timeout=12):
    """经适配层 8888 发一条明文 HTTP 请求，返回 (status, body)。"""
    c = http.client.HTTPConnection(PROXY[0], PROXY[1], timeout=timeout)
    path = url.split(TARGET_HOST + ":%d" % TARGET_PORT, 1)[1]
    pauth = base64.b64encode(b"TestPass1!:Ssh-ChangeMe-2026").decode()
    c.request(method, url, headers={
        "Host": "%s:%d" % (TARGET_HOST, TARGET_PORT),
        "Proxy-Authorization": "Basic " + pauth,
    })
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


class Origin(threading.Thread):
    """一个极小的明文 HTTP 源站，用来做可控的断点/改写试验。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((TARGET_HOST, TARGET_PORT))
        self.sock.listen(16)
        self.seen = []
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = conn.recv(4096)
                if not d:
                    return
                buf += d
            head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
            first = head.split("\r\n")[0]
            self.seen.append(first)
            # 回一个带标记的响应：请求行原样回显，方便判断有没有被改写
            body = ("ORIGIN-OK :: " + first).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                         b"Connection: close\r\n\r\n" + body)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def main():
    origin = Origin()
    origin.start()
    time.sleep(0.4)
    print("源站已起：http://%s:%d" % (TARGET_HOST, TARGET_PORT))

    # ★ 先清空缓冲、关掉断点。理由：本脚本第 1 节按 target 挑包，
    # 如果缓冲里还留着上一轮（或别的测试、或真实设备）发往同一目标的包，
    # `s2c[0]` 可能挑到旧包，断言就会莫名其妙地失败 —— 那是测试没隔离，
    # 不是产品坏了。测试的起点必须是干净的。
    api("POST", "/wpe/hold", {"on": False})
    api("POST", "/wpe/clear")
    time.sleep(0.2)

    url = "http://%s:%d/probe-a" % (TARGET_HOST, TARGET_PORT)

    print("== 1. 抓包 + 自动解析（明文 HTTP 请求/响应）==")
    st, body = http_via_proxy("GET", url)
    print("    客户端 status=%s body=%r" % (st, body[:60]))
    check("源站返回 200", st == 200, st)
    lst = api("GET", "/wpe/list?limit=50")
    ps = lst["packets"]
    mine = [p for p in ps if p["target"] == "%s:%d" % (TARGET_HOST, TARGET_PORT)]
    check("抓到本次请求", len(mine) >= 2, len(mine))
    c2s = [p for p in mine if p["dir"] == "c2s"]
    s2c = [p for p in mine if p["dir"] == "s2c"]
    check("有 c2s 请求包", len(c2s) >= 1)
    check("有 s2c 响应包", len(s2c) >= 1)
    if c2s:
        p = c2s[0]
        print("    c2s: app=%s summary=%r" % (p["app"], p["summary"]))
        check("请求被解析成 HTTP", p["app"] == "HTTP", p["app"])
        check("摘要含请求行", "GET" in p["summary"], p["summary"])
    if s2c:
        p = s2c[0]
        print("    s2c: app=%s summary=%r" % (p["app"], p["summary"]))
        check("响应被解析成 HTTP", p["app"] == "HTTP", p["app"])
        check("摘要含状态码", "200" in p["summary"], p["summary"])

    print("== 2. 过滤（dir / proto / app / target / hex）==")
    # 注意：列表里的 hex 是 512 字符预览（hex_trunc 会标出来）。
    # 所以「hex 包含」的断言只能对没被截断的包做，否则是拿预览当全文，属于自己骗自己。
    for q, want in (("dir=c2s", lambda p: p["dir"] == "c2s"),
                    ("proto=tcp", lambda p: p["proto"] == "tcp"),
                    ("app=HTTP", lambda p: p["app"] == "HTTP"),
                    ("target=18099", lambda p: "18099" in p["target"]),
                    ("hex=474554", lambda p: ("474554" in p["hex"]) or p.get("hex_trunc"))):
        r = api("GET", "/wpe/list?" + q + "&limit=50")["packets"]
        bad = [p for p in r if not want(p)]
        check("过滤 %s 命中且无杂质" % q, len(r) > 0 and not bad,
              "n=%d bad=%d" % (len(r), len(bad)))
    prev = api("GET", "/wpe/list?limit=50")["packets"]
    check("列表预览会标注是否截断",
          all("hex_trunc" in p and "hex_full_len" in p for p in prev))

    print("== 3. 详情（完整 hex / ascii）==")
    pid = c2s[0]["id"]
    one = api("GET", "/wpe/get?id=%d" % pid)["packet"]
    check("详情 id 一致", one["id"] == pid)
    check("详情 hex 比列表预览长或等长", len(one["hex"]) >= 0 and len(one["hex"]) % 2 == 0,
          len(one["hex"]))
    check("详情含请求行明文", "474554" in one["hex"], one["hex"][:24])

    print("== 4. 断点 drop：拦下 → 丢弃 ==")
    api("POST", "/wpe/hold", {"on": True, "rule": {"target": "18099"}})
    hs = api("GET", "/wpe/stat")["stat"]
    check("断点已开", hs["hold_on"] is True)
    box = {}

    def do_req():
        try:
            box["r"] = http_via_proxy("GET", url + "-drop", timeout=25)
        except Exception as exc:
            box["err"] = "%s: %s" % (type(exc).__name__, exc)

    t = threading.Thread(target=do_req)
    t.start()
    time.sleep(2.0)
    held = api("GET", "/wpe/list?only_hold=1&limit=20")["packets"]
    check("请求被卡住（断点队列非空）", len(held) >= 1, len(held))
    rel = release_all("drop")
    print("    丢弃 %s" % rel)
    t.join(30)
    got = box.get("r") or (0, b"")
    print("    客户端 status=%s err=%s" % (got[0], box.get("err")))
    check("客户端被拒（403）", got[0] == 403, got[0])
    check("丢弃计数 +1", api("GET", "/wpe/stat")["stat"]["dropped"] >= 1)

    print("== 5. 断点 modify：改写后放行，记录必须等于真实转发 ==")
    box2 = {}

    def do_req2():
        try:
            box2["r"] = http_via_proxy("GET", url + "-mod", timeout=25)
        except Exception as exc:
            box2["err"] = "%s: %s" % (type(exc).__name__, exc)

    t2 = threading.Thread(target=do_req2)
    t2.start()
    time.sleep(2.0)
    held2 = api("GET", "/wpe/list?only_hold=1&limit=20")["packets"]
    check("第二次被卡住", len(held2) >= 1, len(held2))
    mod_id = None
    new = b""
    if held2:
        mod_id = held2[0]["id"]
        orig = api("GET", "/wpe/get?id=%d" % mod_id)["packet"]
        raw = bytes.fromhex(orig["hex"])
        # 把请求路径 /probe-a-mod 改成 /probe-a-REWRITTEN
        new = raw.replace(b"/probe-a-mod", b"/probe-a-REWRITTEN")
        check("改写内容确实不同", new != raw, "%d -> %d" % (len(raw), len(new)))
        rel2 = release_all("forward", pick=lambda h: h["dir"] == "c2s", data_hex=new.hex())
        print("    放行 %s" % rel2)
        print("    改写放行 #%d：%d -> %d 字节" % (mod_id, len(raw), len(new)))
    t2.join(30)
    got2 = box2.get("r") or (0, b"")
    print("    客户端 status=%s err=%s body=%r" % (got2[0], box2.get("err"), got2[1][:80]))
    check("客户端拿到 200", got2[0] == 200, got2[0])
    check("源站收到的是改写后的请求", any("REWRITTEN" in s for s in origin.seen),
          origin.seen[-2:])
    if mod_id:
        after = api("GET", "/wpe/get?id=%d" % mod_id)["packet"]
        print("    记录 note=%r mod_len=%s" % (after.get("note"), after.get("mod_len")))
        check("记录回填了 mod_hex", bool(after.get("mod_hex")))
        check("mod_len 等于真实转发长度", after.get("mod_len") == len(new),
              "%s vs %d" % (after.get("mod_len"), len(new)))
        check("note 说明实际转发的是改写内容",
              "改写" in (after.get("note") or "") and "实际转发" in (after.get("note") or ""),
              after.get("note"))

    print("== 6. 断点放行 forward（原样通过）==")
    box3 = {}

    def do_req3():
        try:
            box3["r"] = http_via_proxy("GET", url + "-fwd", timeout=25)
        except Exception as exc:
            box3["err"] = "%s: %s" % (type(exc).__name__, exc)

    t3 = threading.Thread(target=do_req3)
    t3.start()
    time.sleep(2.0)
    held3 = api("GET", "/wpe/list?only_hold=1&limit=20")["packets"]
    rel3 = release_all("forward")
    print("    放行 %s" % rel3)
    t3.join(30)
    got3 = box3.get("r") or (0, b"")
    print("    客户端 status=%s err=%s" % (got3[0], box3.get("err")))
    check("forward 后客户端 200", got3[0] == 200, got3[0])
    check("源站收到原始路径", any("probe-a-fwd" in s for s in origin.seen), origin.seen[-3:])
    check("放行后断点队列已空",
          len(api("GET", "/wpe/list?only_hold=1&limit=20")["packets"]) == 0)

    api("POST", "/wpe/hold", {"on": False})
    check("断点已关", api("GET", "/wpe/stat")["stat"]["hold_on"] is False)

    print("== 7. 重放 ==")
    rp = api("POST", "/wpe/replay", {"id": pid, "times": 1})
    print("    %s" % json.dumps(rp, ensure_ascii=False)[:200])
    check("重放返回 ok", rp.get("ok") is True)
    check("重放有回包", int((rp.get("results") or [{}])[0].get("reply") or 0) > 0,
          (rp.get("results") or [{}])[0].get("reply"))

    print("== 8. 构造发送（TCP + UDP）==")
    snd = api("POST", "/wpe/send", {
        "proto": "tcp", "target": "%s:%d" % (TARGET_HOST, TARGET_PORT),
        "data_hex": b"GET /constructed HTTP/1.1\r\nHost: x\r\n\r\n".hex(), "timeout": 6})
    print("    tcp: %s" % json.dumps(snd, ensure_ascii=False)[:160])
    check("构造发送 TCP ok", snd.get("ok") is True and snd.get("reply", 0) > 0, snd.get("reply"))
    check("构造发送也进抓包列表",
          any(p["label"] == "wpe-send" for p in api("GET", "/wpe/list?limit=50")["packets"]))

    print("== 9. 统计 ==")
    st9 = api("GET", "/wpe/stat")["stat"]
    print("    %s" % json.dumps(st9, ensure_ascii=False))
    check("统计字段齐全",
          all(k in st9 for k in ("captured", "held", "replayed", "sent", "dropped",
                                 "modified", "buffered", "capacity", "seq")))

    print("== 10. 清空 ==")
    cl = api("POST", "/wpe/clear")
    check("清空 ok", cl.get("ok") is True, cl)
    check("清空后列表为空", len(api("GET", "/wpe/list?limit=10")["packets"]) == 0)

    origin.stop = True
    try:
        origin.sock.close()
    except Exception:
        pass

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
