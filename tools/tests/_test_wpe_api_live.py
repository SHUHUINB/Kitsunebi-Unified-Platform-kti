#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WPE 网页端 API 线上冒烟测试。

在**服务器本机**跑，直接打 127.0.0.1:8893（适配层管理口，Basic 鉴权）。
纯标准库，不需要额外依赖。

为什么要有这个脚本：单元测试（_test_wpe_struct.py）测的是纯函数，
碰不到「AdminService 里写了 self.rw_on，而 rw_on 其实在 WpeEngine 上」
这类路由层的手误 —— 那种错只会在真打接口时暴露成 AttributeError，
而前端只会显示成一句「加载中…」，很容易被当成缓存问题放过去。

用法：
    python3 /tmp/_test_wpe_api_live.py
退出码 0 = 全绿。
"""

import base64
import json
import sys
import urllib.error
import urllib.request

HOST = "127.0.0.1"
PORT = 8893
USER = "admin"
PASS = "Adapter-Token-ChangeMe"

PASSED = []
FAILED = []


def ck(name, ok, extra=""):
    (PASSED if ok else FAILED).append(name)
    print("%-42s %s%s" % (name, "PASS" if ok else "FAIL",
                          "" if ok else "  <<< " + str(extra)[:300]))


def call(path, method="GET", body=None):
    url = "http://%s:%d%s" % (HOST, PORT, path)
    data = None
    hdrs = {
        "Authorization": "Basic " + base64.b64encode(
            ("%s:%s" % (USER, PASS)).encode()).decode(),
    }
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    try:
        return code, json.loads(raw.decode("utf-8", "replace"))
    except Exception:                                     # noqa: BLE001
        return code, {"_raw": raw[:400].decode("utf-8", "replace")}


print("== 1. /wpe/stat ==")
code, j = call("/wpe/stat")
ck("stat 200 + ok", code == 200 and j.get("ok"), (code, j))
st = j.get("stat") or {}
for k in ("captured", "buffered", "capacity", "hold_on", "holding",
          "modified", "rewritten", "rw_on", "rw_count", "rw_hits"):
    ck("stat 含字段 %s" % k, k in st, sorted(st.keys()))

print("\n== 2. /wpe/rewrite 读 ==")
code, j = call("/wpe/rewrite")
ck("rewrite GET 200 + ok", code == 200 and j.get("ok"), (code, j))
ck("rewrite GET 含 on/rules/hits/rejected",
   all(k in j for k in ("on", "rules", "hits", "rejected")), sorted(j.keys()))

print("\n== 3. /wpe/rewrite 写：合法规则 ==")
code, j = call("/wpe/rewrite", "POST", {
    "on": True,
    "rules": [{"act": "find_replace", "find": "4d41524b455230303031",
               "replace": "52455752495430303031", "proto": "http",
               "note": "live-smoke"}],
})
ck("合法规则被接受", code == 200 and j.get("ok") and len(j.get("rules") or []) == 1,
   (code, j))
ck("proto=http 原样保留", (j.get("rules") or [{}])[0].get("proto") == "http",
   j.get("rules"))

print("\n== 4. /wpe/rewrite 写：各类非法规则必须被拒收并给原因 ==")
code, j = call("/wpe/rewrite", "POST", {
    "on": True,
    "rules": [
        {"act": "find_replace", "find": "41"},                 # 无条件
        {"act": "find_replace", "find": "zz", "dir": "c2s"},   # find 非 hex
        {"act": "set_hex", "set_hex": "", "dir": "c2s"},       # 缺 set_hex
        {"act": "find_replace", "find": "4242", "dir": "c2s"}, # 合法
    ],
})
ck("3 条被拒 / 1 条生效",
   len(j.get("rules") or []) == 1 and len(j.get("rejected") or []) == 3, j)
ck("拒收原因非空", all(x.get("reason") for x in (j.get("rejected") or [])),
   j.get("rejected"))
ck("拒收带原始下标", sorted(x.get("i") for x in (j.get("rejected") or [])) == [0, 1, 2],
   j.get("rejected"))
ck("生效规则 id 重排为 1", [x.get("id") for x in (j.get("rules") or [])] == [1],
   j.get("rules"))

print("\n== 5. /wpe/list 与 /wpe/get（含结构解析） ==")
code, j = call("/wpe/list?limit=50")
ck("list 200 + ok", code == 200 and j.get("ok"), (code, j))
pkts = j.get("packets") or []
ck("list 返回 packets 数组", isinstance(pkts, list), type(pkts))
ck("list 带覆盖范围元信息",
   all(k in j for k in ("scanned", "buffered", "truncated")), sorted(j.keys()))

if pkts:
    pid = pkts[0]["id"]
    code, j2 = call("/wpe/get?id=%d" % pid)
    ck("get 200 + packet", code == 200 and j2.get("ok") and j2.get("packet"), (code, j2))
    st2 = j2.get("struct") or {}
    ck("get 带 struct", bool(st2), sorted(j2.keys()))
    ck("struct 有 app/fields/layers/truncated/of",
       all(k in st2 for k in ("app", "fields", "layers", "truncated", "of")),
       sorted(st2.keys()))
    flds = st2.get("fields") or []
    raw_len = len((j2.get("packet") or {}).get("hex") or "") // 2
    bad = [f for f in flds if f.get("off", 0) + f.get("len", 0) > raw_len]
    ck("struct 字段偏移不越界", not bad, bad[:3])
else:
    print("   （缓冲里没有封包，跳过 get/struct —— 先用代理跑点流量）")

print("\n== 6. /wpe/hold 与 /wpe/export ==")
code, j = call("/wpe/hold", "POST", {"on": False, "rule": {}})
ck("hold 关闭 200 + ok", code == 200 and j.get("ok"), (code, j))
code, j = call("/wpe/hold", "POST", {"on": True, "rule": {"dir": "c2s"}})
ck("hold 开启 200 + ok", code == 200 and j.get("ok"), (code, j))
call("/wpe/hold", "POST", {"on": False, "rule": {}})

code, j = call("/wpe/export?kind=json")
ck("export 200", code == 200, (code, str(j)[:120]))

print("\n== 7. /wpe/list 过滤条件（长度范围 + proto 双语义 + 条件回显） ==")
code, j = call("/wpe/list?limit=5")
ck("list 回显 filter 块", isinstance(j.get("filter"), dict), j.get("filter"))
ck("空条件回显为 0",
   (j.get("filter") or {}).get("min_len") == 0 and
   (j.get("filter") or {}).get("max_len") == 0, j.get("filter"))

# 长度范围：以前没有这个筛法，传了也被忽略（不是报错，是静默忽略 —— 更坏）
code, j = call("/wpe/list?limit=200&min_len=1&max_len=100000")
ck("min/max_len 被接受", code == 200 and j.get("ok"), (code, str(j)[:120]))
ck("min/max_len 回显进 filter",
   (j.get("filter") or {}).get("min_len") == 1 and
   (j.get("filter") or {}).get("max_len") == 100000, j.get("filter"))
lens = [p.get("len", 0) for p in (j.get("packets") or [])]
ck("返回的包长度都在区间内", all(1 <= x <= 100000 for x in lens), lens[:8])

code, j2 = call("/wpe/list?limit=200&max_len=1")
ck("max_len=1 只出极小包",
   all(p.get("len", 0) <= 1 for p in (j2.get("packets") or [])),
   [p.get("len") for p in (j2.get("packets") or [])][:8])

# proto 双语义：内容层名必须也能筛（后端 list 与 _match 行为一致）
code, jh = call("/wpe/list?limit=200&proto=http")
code2, jt = call("/wpe/list?limit=200&proto=tcp")
ck("proto=http 与 proto=tcp 都是 200", code == 200 and code2 == 200, (code, code2))
ck("proto=http 的结果全部 app=HTTP",
   all((p.get("app") or "").upper() == "HTTP" for p in (jh.get("packets") or [])),
   [(p.get("id"), p.get("app")) for p in (jh.get("packets") or [])][:6])
ck("proto=tcp 的结果全部传输层 tcp",
   all((p.get("proto") or "") == "tcp" for p in (jt.get("packets") or [])),
   [(p.get("id"), p.get("proto")) for p in (jt.get("packets") or [])][:6])

# 非法取值不该 500，也不该静默当成无条件
code, jb = call("/wpe/list?limit=5&min_len=abc")
ck("min_len 非数字不炸", code == 200 and jb.get("ok"), (code, str(jb)[:120]))

print("\n== 8. /wpe/hold GET 必须只读（不能把断点关掉） ==")
call("/wpe/hold", "POST", {"on": True, "rule": {"dir": "c2s"}})
code, j = call("/wpe/hold")
ck("hold GET 200", code == 200 and j.get("ok"), (code, str(j)[:120]))
ck("hold GET 报当前状态 on=True", j.get("on") is True, j)
ck("hold GET 报出规则", (j.get("rule") or {}).get("dir") == "c2s", j.get("rule"))
ck("hold GET 带 holding/timeout", "holding" in j and "timeout" in j, sorted(j.keys()))
# 关键：GET 之后状态必须没变
code, j2 = call("/wpe/hold")
ck("GET 之后断点仍然开着（GET 没有副作用）", j2.get("on") is True, j2)
call("/wpe/hold", "POST", {"on": False, "rule": {}})
code, j3 = call("/wpe/hold")
ck("显式 POST 关闭生效", j3.get("on") is False, j3)

print("\n== 9. 收尾：关掉自动改写 ==")
code, j = call("/wpe/rewrite", "POST", {"on": False, "rules": []})
ck("已关闭且规则清空",
   code == 200 and j.get("ok") and j.get("on") is False and not (j.get("rules") or []),
   (code, j))

print("\n==== %d PASS / %d FAIL ====" % (len(PASSED), len(FAILED)))
if FAILED:
    print("FAILED: " + " | ".join(FAILED))
sys.exit(1 if FAILED else 0)
