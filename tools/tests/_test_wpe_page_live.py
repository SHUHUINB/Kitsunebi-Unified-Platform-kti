#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上验证 WPE 列表的「加载更早」翻页游标（before）。

背景：缓冲 4000 条、单次上限 2000，以前只有 since（看更新的）没有 before
（看更旧的），于是后 2000 条永远刷不出来 —— 用户看到的「列表显示不全」。
这里验证的是翻页真的能覆盖整个缓冲，且不重不漏。

判据：
  1. 两页 id 集合不相交，第二页全部严格小于第一页的 oldest_id；
  2. 一直翻到底，累计条数 == buffered；
  3. 累计序列无重复、严格递减；
  4. 到底后 has_more=False。
"""
import json
import sys
import time
import urllib.request

TOKEN = "Console-Token-ChangeMe"
CONSOLE = "http://127.0.0.1:8890"
PROXY = "http://TestPass1!:Ssh-ChangeMe-2026@127.0.0.1:8888"

ok = 0
fail = 0


def C(name, cond, extra=""):
    global ok, fail
    line = "  [%s] %s" % ("PASS" if cond else "FAIL", name)
    if extra != "":
        line += " — " + str(extra)
    print(line)
    if cond:
        ok += 1
    else:
        fail += 1


def api(path):
    url = CONSOLE + path + ("&" if "?" in path else "?") + "token=" + TOKEN
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def gen(n):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY}))
    got = 0
    for i in range(n):
        try:
            with op.open("http://example.com/?wpepage=%d" % i, timeout=15) as r:
                r.read(200)
            got += 1
        except Exception as exc:               # noqa: BLE001
            print("    生成流量第 %d 条失败: %s" % (i, exc))
    return got


print("== 0. 生成流量（走代理，让它真的进 WPE 缓冲）==")
sent = gen(24)
print("    通过代理发出 %d 条请求" % sent)
time.sleep(1.5)

root = api("/api/wpe/stat")
st = root.get("stat") if isinstance(root.get("stat"), dict) else root
buffered = st.get("buffered")
print("    stat.buffered = %s  capacity = %s" % (buffered, st.get("capacity")))
C("缓冲里有包", (buffered or 0) > 0, buffered)

print("== 1. 第一页（limit=10）==")
p1 = api("/api/wpe/list?limit=10")
ids1 = [x["id"] for x in p1["packets"]]
print("    page1 ids = %s" % ids1)
print("    oldest_id = %s  newest_id = %s  has_more = %s"
      % (p1["oldest_id"], p1["newest_id"], p1["has_more"]))
C("第一页有包", len(ids1) > 0, len(ids1))
C("返回 oldest_id", p1["oldest_id"] is not None)
C("返回 newest_id", p1["newest_id"] is not None)
C("has_more 是布尔", isinstance(p1["has_more"], bool))
C("第一页按 id 降序", all(ids1[i] > ids1[i + 1] for i in range(len(ids1) - 1)))

print("== 2. 用 before 翻第二页 ==")
p2 = api("/api/wpe/list?limit=10&before=%d" % p1["oldest_id"])
ids2 = [x["id"] for x in p2["packets"]]
print("    page2 ids = %s" % ids2)
C("第二页全部严格小于第一页 oldest_id",
  len(ids2) > 0 and all(i < p1["oldest_id"] for i in ids2), ids2)
C("两页 id 无重叠", not (set(ids1) & set(ids2)))

print("== 3. 一直翻到底 ==")
seen = list(ids1)
cur = p1
rounds = 0
while cur["has_more"] and cur["oldest_id"] and rounds < 60:
    rounds += 1
    cur = api("/api/wpe/list?limit=10&before=%d" % cur["oldest_id"])
    got = [x["id"] for x in cur["packets"]]
    if not got:
        break
    seen.extend(got)
print("    翻页 %d 轮，累计 %d 条 / 缓冲 %s 条" % (rounds, len(seen), buffered))
C("累计条数 == buffered", len(seen) == buffered, "%d vs %s" % (len(seen), buffered))
C("无重复 id", len(seen) == len(set(seen)),
  "重复 %d" % (len(seen) - len(set(seen))))
C("累计 id 严格递减",
  all(seen[i] > seen[i + 1] for i in range(len(seen) - 1)))
C("翻到底后 has_more=False", cur["has_more"] is False, cur["has_more"])

print("== 4. before 超出范围不应报错 ==")
pz = api("/api/wpe/list?limit=5&before=1")
C("before=1 正常返回", pz.get("ok") is True, pz.get("matched"))
C("before=1 无更旧包", len(pz["packets"]) == 0, len(pz["packets"]))

print("")
print("ALL PASS" if fail == 0 else "HAS %d FAILURES" % fail)
print("PASS=%d FAIL=%d" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
