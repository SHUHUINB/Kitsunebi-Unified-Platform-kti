#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 导出 / 导入（Save / Load）实测（服务器本机）。

要证明的四件事，每一件都对应一条「不骗人」：
1. 导出的 hex 是**完整**字节，不是列表里那 512 字符预览
   → 判据：exported 里某条的 len(hex) == len*2，且能 bytes.fromhex 还原
2. 导入的包**必须带 imported 标记**，不能被当成实时流量
   → 判据：list?label=imported 里每条 imported=True，note 写明「非实时流量」
3. len 与 hex 字节数对不上的（列表预览被截断过的）**必须拒收**，不能假装导入了
   → 判据：skipped 计数 + errors 里说明原因
4. app/summary **按实际字节重新解析**，不信文件里的声明
   → 判据：文件写 app=HTTP，但字节是 DNS 查询 → 导入后 app 必须是 DNS

最后做一次真往返：导出 → 原样导入 → 逐字节比对。
"""
import json
import sys
import urllib.request

API = "http://127.0.0.1:8893"
AUTH = "Basic YWRtaW46Y2NweC04ODkzLUtkOTNtWnE="

fails = []


def api(path, data=None, timeout=60):
    req = urllib.request.Request(API + path, headers={"Authorization": AUTH})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(label, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


# 一个真实的 DNS 查询 example.com（29 字节）
DNS_Q = bytes.fromhex("123401000001000000000000076578616d706c6503636f6d0000010001")


def main():
    print("== 0. 准备：清空缓冲，发一个真实包 ==")
    api("/wpe/clear")
    r = api("/wpe/send", {"proto": "udp", "target": "223.5.5.5:53",
                          "data_hex": DNS_Q.hex()})
    print("    send: ok=%s sent=%s reply=%s" % (r.get("ok"), r.get("sent"), r.get("reply")))
    check("构造发送成功", bool(r.get("ok")) and int(r.get("sent") or 0) > 0)

    print("== 1. 导出必须是完整 hex，不是预览 ==")
    ex = api("/wpe/export")
    check("导出 ok", bool(ex.get("ok")))
    check("格式标记 wpe-export/1", ex.get("format") == "wpe-export/1", ex.get("format"))
    check("导出声明 full_hex=True", ex.get("full_hex") is True)
    check("导出了至少 1 条", int(ex.get("exported") or 0) >= 1, ex.get("exported"))
    print("    导出 %d/%d 条，hex 共 %d 字节，complete=%s stopped_by=%r"
          % (ex.get("exported"), ex.get("candidates"), ex.get("hex_bytes"),
             ex.get("complete"), ex.get("stopped_by")))
    print("    上限：%s" % ex.get("limits"))

    pkts = ex.get("packets") or []
    # 找我们发出去那条（label=wpe-send）
    mine = [p for p in pkts if p.get("label") == "wpe-send" and p.get("len") == len(DNS_Q)]
    check("导出里有我们发的那个包", len(mine) >= 1, len(mine))
    if mine:
        p = mine[0]
        # ★ 关键判据：hex 长度 == len*2。列表预览是 512 字符，会被这条打死。
        check("导出 hex 是完整字节（len(hex)==len*2）",
              len(p["hex"]) == p["len"] * 2, "%d vs %d" % (len(p["hex"]), p["len"] * 2))
        check("导出 hex 能还原成原始字节",
              bytes.fromhex(p["hex"]) == DNS_Q, p["hex"][:40])
        check("导出条不带 hex_trunc 预览标记", "hex_trunc" not in p)
        check("导出条带 imported=False", p.get("imported") is False)
    # 对照：列表里的同一条应当是预览（如果有截断）
    li = api("/wpe/list?limit=20")
    lp = [x for x in (li.get("packets") or []) if x.get("label") == "wpe-send"]
    if lp:
        print("    列表里的同一条：hex_len=%d hex_trunc=%s hex_full_len=%s"
              % (len(lp[0]["hex"]), lp[0].get("hex_trunc"), lp[0].get("hex_full_len")))
        check("列表与导出用的是不同接口（列表带 hex_trunc 字段）",
              "hex_trunc" in lp[0])

    print("== 2. 导入：合法包进、截断包拒、脏数据拒 ==")
    good = {"dir": "c2s", "proto": "udp", "client": "IMPORT-TEST", "target": "1.1.1.1:53",
            "label": "imported-ok", "len": len(DNS_Q), "hex": DNS_Q.hex(),
            "note": "原始备注"}
    # ★ 文件里**故意写错** app/summary：字节是 DNS 查询，文件声称是 HTTP。
    # 导入后必须按字节重新解析成 DNS —— 文件说什么不算，字节才算。
    liar = {"dir": "c2s", "proto": "udp", "client": "IMPORT-LIAR", "target": "9.9.9.9:53",
            "label": "imported-liar", "len": len(DNS_Q), "hex": DNS_Q.hex(),
            "app": "HTTP", "summary": "GET / I-AM-LYING HTTP/1.1"}
    truncated = {"dir": "c2s", "proto": "tcp", "label": "imported-bad",
                 "len": 9999, "hex": "474554"}          # len 与 hex 对不上
    odd = {"dir": "c2s", "proto": "tcp", "label": "imported-bad2",
           "len": 2, "hex": "474"}                      # hex 长度为奇数
    nohex = {"dir": "c2s", "proto": "tcp", "label": "imported-bad3", "len": 3}
    imp = api("/wpe/import", {"packets": [good, liar, truncated, odd, nohex, "不是对象"]})
    print("    imported=%s skipped=%s buffered=%s"
          % (imp.get("imported"), imp.get("skipped"), imp.get("buffered")))
    for e in imp.get("errors") or []:
        print("      skip #%s: %s" % (e.get("i"), e.get("why")))
    check("导入接口 ok", bool(imp.get("ok")))
    check("导入 2 条（good + liar）", imp.get("imported") == 2, imp.get("imported"))
    check("跳过 4 条（截断/奇hex/无hex/非对象）", imp.get("skipped") == 4, imp.get("skipped"))
    reasons = " | ".join((e.get("why") or "") for e in (imp.get("errors") or []))
    check("拒收理由提到 len 与 hex 不一致", "不一致" in reasons, reasons[:120])
    check("拒收理由提到 hex 长度", "奇数" in reasons, reasons[:120])

    print("== 3. 导入的包必须带「非实时流量」标记 ==")
    ls = api("/wpe/list?limit=50&label=imported")
    rows = ls.get("packets") or []
    print("    label 含 imported 的：%d 条" % len(rows))
    for x in rows:
        print("      #%-3d label=%-14s imported=%s len=%s app=%s"
              % (x["id"], x.get("label"), x.get("imported"), x.get("len"), x.get("app")))
    check("导入的包都能查到", len(rows) >= 2, len(rows))
    check("每条都带 imported=True", all(x.get("imported") is True for x in rows),
          [x.get("imported") for x in rows])
    check("每条 note 都写明非实时流量",
          all("非实时流量" in (x.get("note") or "") for x in rows),
          [x.get("note") for x in rows][:2])
    check("每条 note 都注明来自导出文件",
          all("导出文件" in (x.get("note") or "") for x in rows))
    orig = [x for x in rows if x.get("label") == "imported-ok"]
    if orig:
        check("原备注被保留在 note 里", "原始备注" in (orig[0].get("note") or ""),
              orig[0].get("note"))

    print("== 4. app/summary 按字节重新解析，不信文件声明 ==")
    liar_row = [x for x in rows if x.get("label") == "imported-liar"]
    check("撒谎那条进得来", len(liar_row) == 1, len(liar_row))
    if liar_row:
        x = liar_row[0]
        print("    文件声明 app=HTTP / summary=GET / I-AM-LYING，实际解析：app=%s summary=%r"
              % (x.get("app"), x.get("summary")))
        check("app 按字节解析成 DNS，不是文件里写的 HTTP",
              x.get("app") == "DNS", x.get("app"))
        check("summary 里没有文件那句谎话",
              "I-AM-LYING" not in (x.get("summary") or ""), x.get("summary"))
        check("summary 是真实的 DNS 查询摘要",
              "查询" in (x.get("summary") or "") and "example.com" in (x.get("summary") or ""),
              x.get("summary"))

    print("== 5. 真往返：导出 → 原样导入 → 逐字节比对 ==")
    api("/wpe/clear")
    api("/wpe/send", {"proto": "udp", "target": "223.5.5.5:53", "data_hex": DNS_Q.hex()})
    ex2 = api("/wpe/export")
    back = api("/wpe/import", {"packets": ex2.get("packets") or []})
    print("    导出 %d 条 → 导入 %d 条，跳过 %d 条"
          % (ex2.get("exported"), back.get("imported"), back.get("skipped")))
    check("往返全部导入，0 跳过", back.get("skipped") == 0 and back.get("imported") == ex2.get("exported"),
          "imp=%s skip=%s exp=%s" % (back.get("imported"), back.get("skipped"), ex2.get("exported")))
    src = {p["hex"]: p for p in (ex2.get("packets") or [])}
    dst = api("/wpe/list?limit=50&label=imported")
    mism = []
    for x in (dst.get("packets") or []):
        if x["hex"] not in src:
            mism.append(("不在导出里", x["id"]))
            continue
        s = src[x["hex"]]
        if s["len"] != x["len"] or s["hex"] != x["hex"]:
            mism.append((x["id"], s["len"], x["len"]))
    check("导入回来的字节与导出的逐字节一致", not mism, mism[:3])

    print("== 6. 统计与上限 ==")
    st = api("/wpe/stat").get("stat") or {}
    print("    imported=%s captured=%s buffered=%s" % (st.get("imported"), st.get("captured"), st.get("buffered")))
    check("统计里有 imported 计数", "imported" in st)
    check("imported 计数 > 0", int(st.get("imported") or 0) > 0, st.get("imported"))
    check("导出的上限如实报出", isinstance(ex.get("limits"), dict) and
          ex["limits"].get("max_packets") and ex["limits"].get("max_hex_bytes"),
          ex.get("limits"))

    print()
    if fails:
        print("FAILED %d: %s" % (len(fails), fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
