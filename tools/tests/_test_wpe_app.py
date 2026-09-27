#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 自动解析 + 应用层过滤 端到端实测。

跑法（本地）：python _test_wpe_app.py
它会经管理台 8893 直接打适配层的 /wpe/* 接口（也可改成经 8890 管理台转发）。
"""
import base64
import json
import socket
import struct
import sys
import urllib.request

BASE = "http://127.0.0.1:8893"
AUTH = "admin:Adapter-Token-ChangeMe"


def api(method, path, payload=None):
    url = BASE + path
    data = None
    headers = {"Authorization": "Basic " + base64.b64encode(AUTH.encode()).decode()}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def dns_query(name, qtype=1):
    """手搓一个 DNS 查询包。"""
    txid = 0x1234
    flags = 0x0100
    header = struct.pack(">HHHHHH", txid, flags, 1, 0, 0, 0)
    q = b""
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode("ascii")
    q += b"\x00" + struct.pack(">HH", qtype, 1)
    return header + q


def main():
    fails = []

    def check(label, cond, extra=""):
        print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + str(extra) if extra else ""))
        if not cond:
            fails.append(label)

    print("== 1. 构造发送：UDP DNS 查询 ==")
    # ★ 先清空缓冲、关掉断点：起点干净，断言才不会被上一轮的残留包带偏。
    api("POST", "/wpe/hold", {"on": False})
    api("POST", "/wpe/clear")
    pkt = dns_query("example.com")
    r = api("POST", "/wpe/send", {
        "proto": "udp", "target": "223.5.5.5:53",
        "data_hex": pkt.hex(), "timeout": 5,
    })
    print("   ", json.dumps(r, ensure_ascii=False)[:300])
    check("send udp 成功", r.get("ok") is True)
    check("收到应答", int(r.get("reply") or 0) > 0, "reply=%s" % r.get("reply"))
    check("应答可解析成 DNS 应答",
          "8180" in (r.get("reply_hex") or "")[:8], (r.get("reply_hex") or "")[:16])

    print("== 2. 抓到的 UDP 包被解析成 DNS ==")
    lst = api("GET", "/wpe/list?proto=udp&limit=20")
    pkts = lst.get("packets", [])
    check("有 UDP 包", len(pkts) > 0, "n=%d" % len(pkts))
    apps = [p.get("app") for p in pkts]
    print("    apps:", apps)
    check("存在 app=DNS", "DNS" in apps, apps)
    for p in pkts:
        if p.get("app") == "DNS":
            print("    #%s %s %s | %s" % (p["id"], p["dir"], p["app"], p["summary"]))

    print("== 3. 应用层过滤 app=DNS ==")
    only = api("GET", "/wpe/list?app=DNS&limit=20")
    check("app=DNS 过滤命中", len(only.get("packets", [])) > 0, len(only.get("packets", [])))
    bad = [p for p in only.get("packets", []) if p.get("app") != "DNS"]
    check("app=DNS 过滤无杂质", not bad, bad[:2])

    print("== 4. 应用层过滤 app=TLS / app=HTTP ==")
    tls = api("GET", "/wpe/list?app=TLS&limit=20")
    http = api("GET", "/wpe/list?app=HTTP&limit=20")
    print("    TLS n=%d  HTTP n=%d" % (len(tls.get("packets", [])), len(http.get("packets", []))))
    check("TLS 过滤结果全为 TLS", all(p.get("app") == "TLS" for p in tls.get("packets", [])))
    check("HTTP 过滤结果全为 HTTP", all(p.get("app") == "HTTP" for p in http.get("packets", [])))

    print("== 5. 统计 ==")
    st = api("GET", "/wpe/stat")
    print("   ", json.dumps(st.get("stat", {}), ensure_ascii=False))
    check("captured > 0", int(st["stat"]["captured"]) > 0)

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
