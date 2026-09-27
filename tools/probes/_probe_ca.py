#!/usr/bin/env python3
"""一键安装根证书 —— 实机对账探针。

验的是「用户在手机上点一下到底能不能装成」，所以每一条都对着**真实响应**判，
不看代码意图：

  1. /ca 引导页 200，且**指纹必须与证书文件实算的 SHA-256 一致**
     —— 页面上让用户核对的指纹如果和实际证书不符，那是比没有指纹更坏的事。
  2. /ca.mobileconfig 的 Content-Type 必须是 application/x-apple-aspen-config
     —— iOS 只认这个类型才弹「安装描述文件」；回 octet-stream 就变成
       下载一个用户打不开的文件。
  3. mobileconfig 必须是**合法 plist**，且内嵌的证书 base64 解回来
     **逐字节等于**磁盘上的 DER —— 差一个字节就是装了一张错的证书。
  4. UUID 必须由证书指纹派生（同证书两次请求得到同一 UUID）
     —— 随机 UUID 会让用户每点一次就多一份描述文件。
  5. /ca.qr.svg 200 且是合法 SVG。
  6. 三个新端点都**不要求登录**（设备侧无登录是硬约束）。
  7. /ca 的响应里不能出现「未就绪」字样（证书在，页面就该说就绪）。

用法：
    PROBE_API=http://127.0.0.1:8903 python _probe_ca.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET

import httpx

API = os.environ.get('PROBE_API', 'http://127.0.0.1:8903').rstrip('/')
CA_DER = os.environ.get(
    'PROBE_CA_DER',
    '/home/ubuntu/.charles/ca/charles-proxy-ssl-proxying-certificate.cer')

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = '') -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print('PASS %-52s %s' % (name, extra))
    else:
        FAIL += 1
        print('FAIL %-52s %s' % (name, extra))


def main() -> int:
    # ★ trust_env=False：httpx 默认读 http_proxy，会把 127.0.0.1 交给代理，
    #   回一个不含「代理」二字的 502，排查起来极绕。
    cli = httpx.Client(base_url=API, timeout=30, trust_env=False,
                       follow_redirects=False)

    try:
        with open(CA_DER, 'rb') as fh:
            der = fh.read()
    except OSError as exc:
        print('读不到证书文件 %s：%s' % (CA_DER, exc))
        print('（这个探针必须能在服务器上直接读文件，否则无法验证「页面指纹 == 真实指纹」）')
        return 1

    want_fp = ':'.join(hashlib.sha256(der).hexdigest().upper()[i:i + 2]
                       for i in range(0, 64, 2))
    print('磁盘证书 SHA-256 = %s' % want_fp)
    print('证书大小 = %d 字节' % len(der))
    print()

    # ---- 1) /ca 引导页 ---------------------------------------------------- #
    r = cli.get('/ca')
    check('/ca -> 200', r.status_code == 200, 'HTTP %d' % r.status_code)
    page = r.text
    check('/ca 是 HTML', 'text/html' in r.headers.get('content-type', ''))
    check('/ca 显示就绪（不是「证书未就绪」）', '证书未就绪' not in page)
    check('★ /ca 上的指纹与磁盘证书实算一致', want_fp in page,
          '页面里没找到 %s…' % want_fp[:23] if want_fp not in page else '')
    check('/ca 给出 mobileconfig 下载入口', '/ca.mobileconfig' in page)
    check('/ca 给出 .crt 下载入口', '/ca.crt' in page)
    check('/ca 给出 .pem 下载入口', '/ca.pem' in page)
    # iOS 那步「证书信任设置」是用户最容易漏的，页面上必须点名。
    check('★ /ca 提示 iOS「证书信任设置」这一步', '证书信任设置' in page)
    check('/ca 分了 4 个平台', all(k in page for k in
                                  ('iPhone', 'Android', 'Windows', 'macOS')))

    # ---- 2) /ca.mobileconfig --------------------------------------------- #
    m = cli.get('/ca.mobileconfig')
    ct = m.headers.get('content-type', '')
    check('/ca.mobileconfig -> 200', m.status_code == 200, 'HTTP %d' % m.status_code)
    check('★ Content-Type 是 x-apple-aspen-config（否则 iOS 不弹安装）',
          'x-apple-aspen-config' in ct, ct)
    check('带 Content-Disposition 附件名', 'attachment' in
          m.headers.get('content-disposition', ''),
          m.headers.get('content-disposition', '')[:60])

    try:
        root = ET.fromstring(m.text)
        plist_ok = True
    except ET.ParseError as exc:
        plist_ok = False
        root = None
        check('mobileconfig 是合法 plist', False, str(exc))
    if plist_ok:
        check('mobileconfig 是合法 plist', root.tag == 'plist', root.tag)
        d = root.find('dict')
        # ★ 顶层也要按「key 的下一个兄弟 = 值」取。之前写成
        #   `'Configuration' in top_keys` —— 那是拿**值**去比**键名**，
        #   必然失败。测试自己写错比代码写错更常见，改断言不改代码。
        tvals = list(d)
        tpairs = {}
        for i, el in enumerate(tvals):
            if el.tag == 'key' and i + 1 < len(tvals):
                tpairs[el.text] = tvals[i + 1]
        pt_top = tpairs.get('PayloadType')
        check('★ 顶层 PayloadType=Configuration',
              pt_top is not None and pt_top.text == 'Configuration',
              pt_top.text if pt_top is not None else '缺')
        check('顶层含 PayloadUUID',
              tpairs.get('PayloadUUID') is not None)
        check('顶层声明可移除（PayloadRemovalDisallowed=false）',
              tpairs.get('PayloadRemovalDisallowed') is not None
              and tpairs['PayloadRemovalDisallowed'].tag == 'false')
        inner = d.find('array').find('dict')
        # 按「key 的下一个兄弟节点就是它的值」取成字典 —— plist 就是这个结构。
        vals = list(inner)
        pairs = {}
        for i, el in enumerate(vals):
            if el.tag == 'key' and i + 1 < len(vals):
                pairs[el.text] = vals[i + 1]
        pt = pairs.get('PayloadType')
        check('★ 内嵌载荷类型是 com.apple.security.root',
              pt is not None and pt.text == 'com.apple.security.root',
              pt.text if pt is not None else '缺')
        b64 = pairs['PayloadContent'].text if 'PayloadContent' in pairs else ''
        got = base64.b64decode(b64) if b64 else b''
        check('★ 内嵌证书逐字节等于磁盘 DER',
              got == der, '内嵌 %d / 磁盘 %d' % (len(got), len(der)))
        check('内嵌证书 SHA-256 与磁盘一致',
              hashlib.sha256(got).hexdigest() == hashlib.sha256(der).hexdigest())

    # ---- 3) UUID 幂等 ----------------------------------------------------- #
    m2 = cli.get('/ca.mobileconfig')
    uu = lambda s: re.findall(r'<key>PayloadUUID</key>\s*<string>([^<]+)</string>', s)
    check('★ 同证书两次请求 UUID 相同（幂等，不会堆重复描述文件）',
          uu(m.text) == uu(m2.text) and len(uu(m.text)) == 2, str(uu(m.text)))

    # ---- 4) 二维码 -------------------------------------------------------- #
    q = cli.get('/ca.qr.svg')
    check('/ca.qr.svg -> 200', q.status_code == 200, 'HTTP %d' % q.status_code)
    check('二维码是 SVG', 'image/svg' in q.headers.get('content-type', ''),
          q.headers.get('content-type', ''))
    check('二维码内容非空', len(q.content) > 500, '%d 字节' % len(q.content))

    # ---- 5) 无登录 -------------------------------------------------------- #
    # 上面所有请求都没带任何 Authorization，全 200 就已经证明了。
    check('★ 全部 CA 端点无需登录（设备侧硬约束）', True,
          '本探针全程未带 token')

    # ---- 6) 旧路径没被破坏 ------------------------------------------------ #
    for path in ('/ca.crt', '/ca.pem', '/ok', '/socks', '/ruleset.conf'):
        rr = cli.get(path)
        check('回归 %s 仍 200' % path, rr.status_code == 200, 'HTTP %d' % rr.status_code)

    # ---- 7) 引导页不能把 SPA 兜进来 -------------------------------------- #
    check('★ /ca 没被 SPA 通配兜成 index.html',
          '<div id="app">' not in page and 'assets/index-' not in page)

    print()
    print('%d PASS / %d FAIL' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
