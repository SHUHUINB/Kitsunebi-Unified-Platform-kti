"""验证适配层能否从 TLS ClientHello 里解出 SNI。

关键场景：CONNECT 的目标是**纯 IP**，但 ClientHello 里的 SNI 是**域名**。
这正是日志里那 98 条 `157.148.79.10:443` 之类的真实形态 —— 光看 IP 不知道
是什么服务，解出 SNI 才能还原。
"""
import base64
import socket
import time


def client_hello(sni):
    name = sni.encode()
    sn_ext = (b"\x00\x00" + (len(name) + 5).to_bytes(2, "big")
              + (len(name) + 3).to_bytes(2, "big") + b"\x00"
              + len(name).to_bytes(2, "big") + name)
    ext_block = len(sn_ext).to_bytes(2, "big") + sn_ext
    body = (b"\x03\x03" + b"\x00" * 32 + b"\x00"
            + b"\x00\x02\x13\x01" + b"\x01\x00" + ext_block)
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


CASES = [
    ("157.148.79.10", 443, "game-anti-cheat.tencent.com"),
    ("119.147.175.11", 443, "ums-telemetry-cn.heytapmobi.com"),
    ("81.68.134.17", 443, "kafe-kampus.ru"),
]

auth = base64.b64encode(b"TestPass1!:Ssh-ChangeMe-2026").decode()

for ip, port, sni in CASES:
    try:
        s = socket.create_connection(("127.0.0.1", 8888), 10)
        s.sendall(
            ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
             "Proxy-Authorization: Basic %s\r\n\r\n"
             % (ip, port, ip, port, auth)).encode())
        resp = s.recv(4096)
        line = resp.split(b"\r\n")[0].decode("latin1")
        s.sendall(client_hello(sni))
        time.sleep(0.4)
        s.close()
        print("CONNECT %s:%d -> %s | SNI 发送=%s" % (ip, port, line, sni))
    except Exception as exc:
        print("CONNECT %s:%d 失败: %s" % (ip, port, exc))

time.sleep(1.0)
print("done")
