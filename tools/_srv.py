#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 SSH 助手（paramiko），不依赖 sshpass / ssh -L。

用法:
    python _srv.py run "<shell 命令>"          # 执行并回显 stdout/stderr/exit
    python _srv.py sudo "<shell 命令>"         # sudo -n 执行
    python _srv.py put <本地路径> <远端路径>
    python _srv.py get <远端路径> <本地路径>

环境变量: SRV_HOST / SRV_USER / SRV_PASS / SRV_PORT
"""
import os
import sys

import paramiko

HOST = os.environ.get("SRV_HOST", "203.0.113.10")
USER = os.environ.get("SRV_USER", "ubuntu")
PASS = os.environ.get("SRV_PASS", "Ssh-ChangeMe-2026")
PORT = int(os.environ.get("SRV_PORT", "22"))


def client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=25, banner_timeout=25, auth_timeout=25,
              allow_agent=False, look_for_keys=False)
    return c


def run(cmd, sudo=False):
    c = client()
    try:
        stdin, out, err = c.exec_command(
            "sudo -n bash -s" if sudo else "bash -s", timeout=900)
        stdin.write(cmd + "\n")
        stdin.channel.shutdown_write()
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        sys.stdout.write(o)
        if e.strip():
            sys.stdout.write("\n--- stderr ---\n" + e)
        sys.stdout.write("\n--- exit=%d ---\n" % rc)
        return rc
    finally:
        c.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    mode = sys.argv[1]
    if mode == "run":
        return run(sys.argv[2])
    if mode == "sudo":
        return run(sys.argv[2], sudo=True)
    if mode == "put":
        c = client()
        try:
            sftp = c.open_sftp()
            sftp.put(sys.argv[2], sys.argv[3])
            st = sftp.stat(sys.argv[3])
            print("uploaded %s -> %s (%d bytes)" % (sys.argv[2], sys.argv[3], st.st_size))
            sftp.close()
            return 0
        finally:
            c.close()
    if mode == "get":
        c = client()
        try:
            sftp = c.open_sftp()
            sftp.get(sys.argv[2], sys.argv[3])
            print("downloaded %s -> %s (%d bytes)"
                  % (sys.argv[2], sys.argv[3], os.path.getsize(sys.argv[3])))
            sftp.close()
            return 0
        finally:
            c.close()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
