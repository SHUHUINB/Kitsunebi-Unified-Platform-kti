#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 导出完整性的**线上实机**验证。

为什么要有这个脚本（而不是只看单测）：
    用户报的是「无法导出完整 hex」。单测在本地用小缓冲跑通了，
    但线上真实条件是 —— 缓冲 4000 条、单包最大 64KB、旧实现有 8MB 字节上限。
    只有把缓冲**真的填满**再导出，才能证明「导出不完整」确实被修掉了。

★ 关键约束：线上缓冲是**和真实流量共享**的。
    这台机器上随时可能有别的客户端在走代理，它们的包会进同一个环形缓冲，
    把刚导入的合成包挤出去。所以断言不能写成「第 i 条一定是我的第 i 条」——
    那种断言在有人用的时候必然误报。真正与流量无关的判据是：
      ① exported == buffered        导出条数必须等于缓冲里的条数（不砍）
      ② 每条的 len×2 == hex 长度    导出的是**完整**字节，不是预览截断
      ③ 合成包只要还在，内容就必须逐字节正确
      ④ 导出的文件必须能原样导回去
    这四条无论有没有人同时在用都成立。

用法（在服务器上跑）：
    python3 _test_wpe_export_live.py
"""
import base64
import json
import sys
import urllib.request
import urllib.error

HOST = "http://127.0.0.1:8893"
USER = "admin"
PASS = "Adapter-Token-ChangeMe"

# 合成包的尺寸：4000 × 3000B = 12MB 原始字节 / 24MB hex。
# 旧实现的 EXPORT_MAX_HEX 是 8MB —— 这个量级必然把它顶穿，
# 所以「导出条数 == 4000」本身就是修复生效的判据。
PKT_BYTES = 3000
PKT_COUNT = 4000
MARK = b"WPEVERIFY"          # 合成包的特征前缀，用来把真实流量区分出来
MAX_PKT = 65536              # 必须与 WpeEngine.MAX_PKT 一致

PASS_N = 0
FAIL_N = 0


def ck(name, cond, detail=""):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print("  %-46s PASS" % name)
    else:
        FAIL_N += 1
        print("  %-46s FAIL  %s" % (name, detail))


def req(method, path, body=None, raw=False):
    url = HOST + path
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)
    tok = base64.b64encode(("%s:%s" % (USER, PASS)).encode()).decode()
    r.add_header("Authorization", "Basic " + tok)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            blob = resp.read()
            if raw:
                return resp.status, dict(resp.headers), blob
            try:
                return resp.status, dict(resp.headers), json.loads(blob.decode("utf-8"))
            except Exception:
                return resp.status, dict(resp.headers), blob
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, dict(e.headers), json.loads(blob.decode("utf-8"))
        except Exception:
            return e.code, dict(e.headers), blob


def make_synthetic(i):
    """第 i 条合成包：前 17 字节是特征+序号，其余填 0。"""
    head = MARK + b"%06d|" % i
    return head + b"\x00" * (PKT_BYTES - len(head))


def main():
    print("=" * 62)
    print("WPE 导出完整性 —— 线上实机验证")
    print("=" * 62)

    # ---------------------------------------------------------- 0. 清干净
    print("\n== 0. 起始状态 ==")
    st, _, r = req("POST", "/wpe/clear", {})
    ck("清空缓冲 ok", isinstance(r, dict) and r.get("ok"), r)
    st, _, r = req("GET", "/wpe/stat")
    ck("清空后 buffered=0", r["stat"]["buffered"] == 0, r["stat"])

    # ---------------------------------------------------------- 1. 导入填满
    print("\n== 1. 导入 %d 条 × %dB，把缓冲填满 ==" % (PKT_COUNT, PKT_BYTES))
    items = []
    for i in range(PKT_COUNT):
        raw = make_synthetic(i)
        items.append({"hex": raw.hex(), "len": len(raw),
                      "dir": "c2s" if i % 2 == 0 else "s2c",
                      "proto": "tcp",
                      "target": "10.0.%d.%d:8080" % (i % 250, i % 200)})
    payload = json.dumps({"packets": items}).encode()
    print("  导入体 %.1f MB" % (len(payload) / 1048576.0))
    st, _, r = req("POST", "/wpe/import", payload)
    ck("导入返回 ok", isinstance(r, dict) and r.get("ok"), str(r)[:200])
    ck("导入 %d 条且 0 拒收" % PKT_COUNT,
       r.get("imported") == PKT_COUNT and not r.get("errors"),
       "imported=%s errors=%s" % (r.get("imported"), str(r.get("errors"))[:200]))
    st, _, r = req("GET", "/wpe/stat")
    buffered_at_export = r["stat"]["buffered"]
    ck("缓冲已满 buffered=4000", buffered_at_export == 4000, buffered_at_export)

    # ---------------------------------------------------------- 2. 落盘导出
    print("\n== 2. 落盘导出（本次修复的正路）==")
    st, _, r = req("POST", "/wpe/export/save?kind=json", {})
    ck("落盘导出 ok", isinstance(r, dict) and r.get("ok"), str(r)[:200])
    name = r.get("name")
    exported = r.get("exported")
    hex_bytes = r.get("hex_bytes") or 0
    print("  文件 %s  条数 %s/%s  hex %.2f MB  文件 %.2f MB"
          % (name, exported, r.get("buffered"),
             hex_bytes / 1048576.0, (r.get("size") or 0) / 1048576.0))
    ck("导出条数 == 缓冲全部（不砍）", exported == r.get("buffered") == 4000,
       "%s / %s" % (exported, r.get("buffered")))
    ck("complete=True", r.get("complete") is True, r.get("complete"))
    ck("hex 总量 > 旧 8MB 上限（这就是原来被砍的地方）",
       hex_bytes > 8 * 1024 * 1024, "%.2f MB" % (hex_bytes / 1048576.0))
    ck("落盘文件非空", (r.get("size") or 0) > 20 * 1024 * 1024,
       "%.2f MB" % ((r.get("size") or 0) / 1048576.0))
    ck("无截断包（全部 ≤ %dB）" % MAX_PKT, r.get("truncated_pkts") == 0,
       r.get("truncated_pkts"))

    # ---------------------------------------------------------- 3. 下载并逐条对账
    print("\n== 3. 下载导出文件并逐条对账 ==")
    st, hdrs, blob = req("GET", "/wpe/export/download?name=" + name, raw=True)
    ck("下载 200", st == 200, st)
    disp = (hdrs.get("Content-Disposition") or hdrs.get("content-disposition") or "")
    ck("带 Content-Disposition: attachment", "attachment" in disp.lower(), disp)
    ck("Content-Type 是 JSON",
       "json" in (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower(),
       hdrs.get("Content-Type"))
    ck("下载字节数 == 落盘大小", len(blob) == r.get("size"),
       "%d vs %s" % (len(blob), r.get("size")))
    doc = json.loads(blob.decode("utf-8"))
    pk = doc.get("packets") or []
    ck("文件里条数 == 4000", len(pk) == PKT_COUNT, len(pk))
    ck("文件声明 complete=true", doc.get("complete") is True, doc.get("complete"))

    # ---- 判据 ②：完整 hex（与有没有人同时在用无关）----
    bad_len = [p["id"] for p in pk if len(p["hex"]) != int(p["len"]) * 2]
    ck("每条 len×2 == hex 长度（无预览截断）", not bad_len, "不符 %d 条" % len(bad_len))
    over = [p["id"] for p in pk if len(p["hex"]) > MAX_PKT * 2]
    ck("每条 hex 都不超过单包上限", not over, "超限 %d 条" % len(over))
    odd = [p["id"] for p in pk if len(p["hex"]) % 2]
    ck("没有奇数长度的 hex", not odd, "奇数 %d 条" % len(odd))

    # ---- 判据 ③：合成包只要还在，内容必须逐字节正确 ----
    synth = {}
    mh = MARK.hex()
    for p in pk:
        h = p["hex"]
        if h.startswith(mh):
            try:
                # 序号是 ASCII 十进制（b"%06d"），必须按 10 进制读 ——
                # 按 16 进制读的话 "000010" 会变成 16，序号一多就全错位。
                idx = int(bytes.fromhex(h[len(mh):len(mh) + 12]).decode(), 10)
            except Exception:
                continue
            synth[p["id"]] = (idx, h)
    print("  合成包仍在缓冲中的：%d / %d（其余被实时流量挤出，属正常）"
          % (len(synth), PKT_COUNT))
    ck("合成包大部分还在（实时流量只挤掉少量）", len(synth) >= 3000, len(synth))
    corrupt = [pid for pid, (idx, h) in synth.items()
               if h != make_synthetic(idx).hex()]
    ck("在缓冲里的合成包逐字节正确", not corrupt, "损坏 %d 条" % len(corrupt))
    idxs = sorted(i for i, _ in synth.values())
    ck("合成包序号连续（没有中间缺块）",
       not idxs or idxs == list(range(idxs[0], idxs[0] + len(idxs))),
       "缺口示例 %s" % ([i for i in range(idxs[0], idxs[-1]) if i not in set(idxs)][:8]
                        if idxs else "n/a"))

    # ---- 判据 ①补充：id 不重复 ----
    ids = [p["id"] for p in pk]
    ck("导出里没有重复 id", len(set(ids)) == len(ids), "%d/%d" % (len(set(ids)), len(ids)))

    # ---------------------------------------------------------- 4. 回导
    print("\n== 4. 导出的文件必须能导回去（导得出导不回 = 导出是假的）==")
    req("POST", "/wpe/clear", {})
    st, _, r3 = req("POST", "/wpe/import", blob)
    ck("回导 ok", isinstance(r3, dict) and r3.get("ok"), str(r3)[:200])
    ck("回导 %d 条且 0 拒收" % PKT_COUNT,
       r3.get("imported") == PKT_COUNT and not r3.get("errors"),
       "imported=%s errors=%s" % (r3.get("imported"), str(r3.get("errors"))[:200]))

    # ---------------------------------------------------------- 5. 分页续导
    print("\n== 5. JSON 回传那条路：上限到了要给续导游标 ==")
    st, _, r4 = req("GET", "/wpe/export?max_hex=100000")
    ck("小上限触发 stopped_by=bytes", r4.get("stopped_by") == "bytes", r4.get("stopped_by"))
    ck("小上限时 complete=False", r4.get("complete") is False, r4.get("complete"))
    ns = r4.get("next_since")
    ck("给出 next_since 游标", isinstance(ns, int), ns)
    ids1 = [p["id"] for p in r4.get("packets") or []]
    ck("第一页有内容且少于候选数", 0 < len(ids1) < r4.get("candidates", 0),
       "%d / %s" % (len(ids1), r4.get("candidates")))
    ck("第一页每条也是完整 hex",
       all(len(p["hex"]) == int(p["len"]) * 2 for p in r4.get("packets") or []), "")
    if isinstance(ns, int):
        st, _, r5 = req("GET", "/wpe/export?max_hex=100000&since=%d" % ns)
        ids2 = [p["id"] for p in r5.get("packets") or []]
        ck("续导从 next_since 之后开始", ids2 and ids2[0] > ns, ids2[:3])
        ck("续导与第一页不重叠",
           not (set(ids1) & set(ids2)), "重叠 %d" % len(set(ids1) & set(ids2)))

    # ★ 退化情形：字节预算比单个包还小。必须仍能前进，否则客户端永远拿不到东西。
    st, _, r6 = req("GET", "/wpe/export?max_hex=1")
    ids6 = [p["id"] for p in r6.get("packets") or []]
    ck("预算=1 字节时仍至少给出 1 条（前进性保证）", len(ids6) >= 1, len(ids6))
    ck("预算=1 字节时给出游标可续", isinstance(r6.get("next_since"), int),
       r6.get("next_since"))

    # ---------------------------------------------------------- 6. 守卫
    print("\n== 6. 守卫 ==")
    st, _, r7 = req("GET", "/wpe/export/save")
    ck("导出落盘 GET 被拒（有副作用，只能 POST）",
       isinstance(r7, dict) and r7.get("ok") is False, str(r7)[:120])
    st, _, r8 = req("GET", "/wpe/export/download?name=../../../opt/ccpx/config.json")
    ck("下载口拒绝目录穿越", st == 400 or (isinstance(r8, dict) and r8.get("ok") is False),
       "%s %s" % (st, str(r8)[:120]))
    st, _, r9 = req("GET", "/wpe/export/download?name=/etc/passwd")
    ck("下载口拒绝绝对路径", st == 400 or (isinstance(r9, dict) and r9.get("ok") is False),
       "%s %s" % (st, str(r9)[:120]))
    st, _, r10 = req("GET", "/wpe/export/download?name=nope-12345.json")
    ck("不存在的文件报错而非 500",
       st in (404, 400) or (isinstance(r10, dict) and r10.get("ok") is False),
       "%s %s" % (st, str(r10)[:120]))
    st, _, r11 = req("GET", "/wpe/export/list")
    ck("导出文件列表含刚生成的文件",
       isinstance(r11, dict) and any(f["name"] == name for f in r11.get("files") or []),
       str(r11)[:200])
    ck("列表带 dir", isinstance(r11, dict) and r11.get("dir") == "/opt/ccpx/exports",
       r11.get("dir") if isinstance(r11, dict) else r11)

    # ---------------------------------------------------------- 7. TXT 导出
    print("\n== 7. TXT 落盘（人读版，hex 也必须是完整字节）==")
    st, _, rt = req("POST", "/wpe/export/save?kind=txt", {})
    ck("TXT 落盘 ok", isinstance(rt, dict) and rt.get("ok"), str(rt)[:200])
    st, hdrs, blobs = req("GET", "/wpe/export/download?name=" + rt["name"], raw=True)
    txt = blobs.decode("utf-8", "replace")
    ck("TXT 下载 200", st == 200, st)
    ck("TXT Content-Type 是 text/plain",
       "text/plain" in (hdrs.get("Content-Type") or "").lower(), hdrs.get("Content-Type"))
    ck("TXT 声明为完整字节", "hex 为完整字节" in txt, txt[:80])
    ck("TXT 里 HEX 行数 == 4000", txt.count("\n  HEX  : ") == PKT_COUNT,
       txt.count("\n  HEX  : "))
    # 不能硬找「第 0 条」——实时流量可能已经把最前面几条挤出去了。
    # 改成：凡带合成特征前缀的 HEX 行，长度都必须正好是 3000 字节（6000 字符）。
    hex_lines = [ln.split("  HEX  : ", 1)[1].strip()
                 for ln in txt.splitlines() if "  HEX  : " in ln]
    mk = MARK.hex()
    synth_lines = [l for l in hex_lines if l.startswith(mk)]
    ck("TXT 里存在大量完整合成 hex 行", len(synth_lines) > 3000, len(synth_lines))
    lens = sorted({len(l) for l in synth_lines})
    ck("TXT 里合成包的 hex 行都是完整的 3000 字节", lens == [PKT_BYTES * 2], lens[:5])

    print("\n==== %d PASS / %d FAIL ====" % (PASS_N, FAIL_N))
    return 0 if FAIL_N == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
