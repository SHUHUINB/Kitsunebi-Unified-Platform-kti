#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一平台（8890）WPE 导出通道的线上回归。

为什么单独测这一层：
    浏览器实际走的**不是**适配层的 8893，而是平台的 /api/v1/wpe/*。
    导出在适配层测通了，平台这边仍然可能翻车 —— 事实上就翻过一次：
    下载守卫用 `'json' in content-type` 判「上游报错」，可**导出的 .json 文件
    本身的 Content-Type 就是 application/json**，于是每次下 JSON 存档都被
    自己的守卫当成错误挡掉，返回 502。适配层侧一切正常，用户侧永远下不来。

    这类「中间层把正常结果误判成错误」的 bug，只有走真实链路才抓得到。

★ 2026-09-27 重写：原版打的是**旧管理台**的 `/api/wpe/export/save`、
  `/api/wpe/dl?name=`，用 `?token=<旧 console token>` 鉴权。旧管理台
  （/opt/charles-console）已随重写一起删除，那些路由不存在了，所以这份
  脚本一直报 405/失败 —— 那不是功能缺失，是脚本在测一个已经不存在的东西。
  现在改成平台的真实接口：`/api/v1/wpe/exports`（列表 GET / 落盘 POST）、
  `/api/v1/wpe/exports/{name}/download`（下载 GET），鉴权走**登录会话 cookie**。

用法（在服务器上跑，需要能读 rewrite.env 拿管理员凭据）：
    sudo python3 _test_wpe_console_live.py
"""
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("PLATFORM_BASE", "http://127.0.0.1:8890")
ENV_FILE = os.environ.get("REWRITE_ENV", "/opt/rewrite/rewrite.env")

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


def _creds():
    """管理员凭据：环境变量优先，否则从 rewrite.env 现读。

    故意不写死在脚本里 —— 口令是会被换的，写死之后这个测试就变成
    「每次都要改代码才能跑」，最后没人跑。
    """
    u = os.environ.get("SMOKE_USER")
    p = os.environ.get("SMOKE_PWD")
    if u and p:
        return u, p
    try:
        txt = open(ENV_FILE, encoding="utf-8").read()
    except OSError as exc:
        raise SystemExit("读不到 %s：%s（用 SMOKE_USER/SMOKE_PWD 覆盖）" % (ENV_FILE, exc))
    u = (re.search(r"^REWRITE_ADMIN_USER=(.*)$", txt, re.M) or [None, ""])[1].strip()
    p = (re.search(r"^REWRITE_ADMIN_PASS=(.*)$", txt, re.M) or [None, ""])[1].strip()
    if not u or not p:
        raise SystemExit("rewrite.env 里没有 REWRITE_ADMIN_USER / REWRITE_ADMIN_PASS")
    return u, p


_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def hdr(hd, name):
    """大小写无关地取响应头。

    ★ 别直接 `hd.get('Content-Type')`：`dict(resp.headers)` 保留的是服务端
      实际发出的**原始大小写**，而 uvicorn 发的是全小写。于是「大写的取不到」
      会表现成「这个头根本没设」—— 一个纯粹的读取方式问题被误读成功能缺陷。
      这个坑在这份脚本里踩过一次。
    """
    want = name.lower()
    for k, v in (hd or {}).items():
        if str(k).lower() == want:
            return v
    return ""


def req(method, path, body=None, raw=False):
    """统一走带 cookie 的 opener —— 平台用会话 cookie 鉴权，不是 query token。"""
    url = BASE + path
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(r, timeout=300) as resp:
            blob = resp.read()
            hd = dict(resp.headers)
            if raw:
                return resp.status, hd, blob
            try:
                return resp.status, hd, json.loads(blob.decode("utf-8"))
            except Exception:
                return resp.status, hd, blob
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            return e.code, dict(e.headers), json.loads(blob.decode("utf-8"))
        except Exception:
            return e.code, dict(e.headers), blob


def main():
    print("=" * 62)
    print("统一平台 WPE 导出通道 —— 线上回归")
    print("=" * 62)

    print("\n== 0. 登录拿会话（后面所有请求都靠它）==")
    u, p = _creds()
    st, _, r = req("POST", "/api/v1/auth/login", {"username": u, "password": p})
    ck("登录 HTTP 200", st == 200, "%s %s" % (st, str(r)[:160]))
    ck("拿到会话 cookie", len(_JAR) > 0, "cookie 数 %d" % len(_JAR))
    if st != 200:
        print("\n登录不了，后续无法进行。")
        print("\n==== %d PASS / %d FAIL ====" % (PASS_N, FAIL_N))
        return 1

    print("\n== 1. 落盘导出（走平台，不是直连适配层）==")
    st, _, r = req("POST", "/api/v1/wpe/exports?kind=json", {})
    ck("HTTP 201", st == 201, st)
    ck("ok=true", isinstance(r, dict) and r.get("ok"), str(r)[:200])
    if not (isinstance(r, dict) and r.get("ok")):
        print("\n导出失败，后续无法进行。")
        print("\n==== %d PASS / %d FAIL ====" % (PASS_N, FAIL_N + 1))
        return 1
    name = r["name"]
    size = r["size"]
    print("  %s  %s/%s 条  %.2f MB" % (name, r["exported"], r["buffered"],
                                       size / 1048576.0))
    ck("导出条数 == 缓冲条数", r["exported"] == r["buffered"], r)
    ck("complete=true", r.get("complete") is True, r.get("complete"))

    print("\n== 2. 导出历史列表 ==")
    st, _, rl = req("GET", "/api/v1/wpe/exports")
    ck("列表 HTTP 200", st == 200, st)
    ck("列表里含刚生成的文件",
       isinstance(rl, dict) and any(f["name"] == name for f in rl.get("files") or []),
       str(rl)[:200])

    qname = urllib.parse.quote(name, safe="")

    print("\n== 3. 下载 JSON 存档（这一步原来返回 502）==")
    st, hd, blob = req("GET", "/api/v1/wpe/exports/%s/download" % qname, raw=True)
    ck("下载 HTTP 200（不再是 502）", st == 200, st)
    disp = hdr(hd, "Content-Disposition")
    ctype = hdr(hd, "Content-Type")
    ck("保留 Content-Disposition: attachment", "attachment" in disp.lower(), disp)
    ck("保留上游 Content-Type（json）", "json" in ctype.lower(), ctype)
    ck("字节数与落盘大小一致", len(blob) == size, "%d vs %s" % (len(blob), size))
    ck("下载体是完整 JSON 而非错误信息", blob[:1] == b"{", blob[:80])
    try:
        doc = json.loads(blob.decode("utf-8"))
    except Exception as exc:
        doc = None
        ck("下载体可解析为 JSON", False, str(exc)[:120])
    if doc:
        ck("下载体可解析为 JSON", True)
        pk = doc.get("packets") or []
        ck("文件里条数 == 导出条数", len(pk) == r["exported"],
           "%d vs %s" % (len(pk), r["exported"]))
        bad = [p["id"] for p in pk if len(p["hex"]) != int(p["len"]) * 2]
        ck("每条 len×2 == hex 长度（完整 hex）", not bad, "不符 %d 条" % len(bad))
        odd = [p["id"] for p in pk if len(p["hex"]) % 2]
        ck("没有奇数长度 hex", not odd, len(odd))

    print("\n== 4. 下载 TXT 存档 ==")
    st, _, rt = req("POST", "/api/v1/wpe/exports?kind=txt", {})
    ck("TXT 落盘 ok", isinstance(rt, dict) and rt.get("ok"), str(rt)[:200])
    if isinstance(rt, dict) and rt.get("ok"):
        qn = urllib.parse.quote(rt["name"], safe="")
        st, hd, blobt = req("GET", "/api/v1/wpe/exports/%s/download" % qn, raw=True)
        ctype = hdr(hd, "Content-Type")
        ck("TXT 下载 200", st == 200, st)
        ck("TXT Content-Type 是 text/plain", "text/plain" in ctype.lower(), ctype)
        ck("TXT 字节数与落盘一致", len(blobt) == rt["size"],
           "%d vs %s" % (len(blobt), rt["size"]))
        ck("TXT 里声明完整字节", "hex 为完整字节" in blobt.decode("utf-8", "replace"),
           blobt[:80])

    print("\n== 5. 下载口守卫（不能变成任意文件读取）==")
    for bad_name, why in [
        ("../../../../etc/passwd", "目录穿越"),
        ("/etc/passwd", "绝对路径"),
        ("a/b.json", "含路径分隔符"),
        (".hidden", "以点开头"),
    ]:
        q = urllib.parse.quote(bad_name, safe="")
        st, _, rb = req("GET", "/api/v1/wpe/exports/%s/download" % q)
        # 400 = 被守卫拦下；404 = 路由没匹配上。两种都算拒绝，
        # 关键是**绝不能 200**。
        ck("拒绝%s" % why, st != 200, "%s %s" % (st, str(rb)[:100]))
    st, _, rb = req("GET", "/api/v1/wpe/exports/nope-99999.json/download")
    ck("不存在的文件报错而非 500",
       st in (400, 404, 502) or (isinstance(rb, dict) and rb.get("ok") is False),
       "%s %s" % (st, str(rb)[:100]))

    print("\n== 6. 鉴权：没登录不能导出 / 不能下载 ==")
    anon = urllib.request.build_opener()          # 全新 opener，不带 cookie
    for method, path, why in [
        ("POST", "/api/v1/wpe/exports?kind=json", "匿名落盘导出"),
        ("GET", "/api/v1/wpe/exports/%s/download" % qname, "匿名下载"),
        ("GET", "/api/v1/wpe/exports", "匿名列导出历史"),
    ]:
        r = urllib.request.Request(BASE + path, data=(b"{}" if method == "POST" else None),
                                   method=method)
        r.add_header("Content-Type", "application/json")
        try:
            with anon.open(r, timeout=30) as resp:
                ck("拒绝%s" % why, False, "竟然 %s" % resp.status)
        except urllib.error.HTTPError as e:
            ck("拒绝%s" % why, e.code == 401, e.code)
        except Exception as exc:                   # noqa: BLE001
            ck("拒绝%s" % why, False, exc)

    print("\n==== %d PASS / %d FAIL ====" % (PASS_N, FAIL_N))
    return 0 if FAIL_N == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
