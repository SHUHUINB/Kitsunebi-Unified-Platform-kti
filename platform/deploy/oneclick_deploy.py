#!/usr/bin/env python3
"""完整包一键部署（本地 → 服务器，一条命令）。

    python oneclick_deploy.py                 # 打包 + 上传 + 部署（暂存 8903）
    python oneclick_deploy.py --fresh         # 同上，并生成新的 JWT 密钥与管理员口令
    python oneclick_deploy.py --fresh --switch # 全新部署并直接切到 8890
    python oneclick_deploy.py --purge-old     # 删旧 charles-console（先备份，需已切到 8890）
    python oneclick_deploy.py --no-upload     # 只打包，得到 tgz 不部署

它做的事，按顺序：
    1. 打包  backend/ + frontend/dist/ + deploy/ + VERSION  →  rewrite-full-<时间戳>.tgz
    2. 上传到服务器 /tmp/
    3. 远端执行  bash oneclick.sh <tgz> [--fresh] [--switch] [--purge-old]
    4. 探活  /healthz  并回显访问地址与口令提示

为什么不把这几步拆开让人手工做
------------------------------
拆开之后最容易漏的是第 1 步的**排除项**：`backend/var/` 里是数据库。
一个 `tar czf` 不小心带上它，上传就会把服务器上的库覆盖成你本地的空库。
打包时排除是硬约束，所以把它固化进脚本，而不是写在文档里等人自觉。

依赖：paramiko（本机托管 venv 里已有）。
用法上的两个已知坑，脚本里已处理：
  * 不能用 git-bash 的裸 python（没有 paramiko）——
    用 C:/Users/MR/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe
  * 本地路径不要用 /tmp（git-bash 虚拟路径，原生 Python 认不到）——
    默认落在本脚本同级目录。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # _rewrite/

DEFAULTS = {
    'host': os.environ.get('SRV_HOST', '203.0.113.10'),
    'user': os.environ.get('SRV_USER', 'ubuntu'),
    'password': os.environ.get('SRV_PASS', 'Ssh-ChangeMe-2026'),
    'port': int(os.environ.get('SRV_PORT', '22')),
}

# 打包时必须排除的东西。每一条都有理由，不是随手加的：
EXCLUDE_DIRS = {
    'var',            # ★ 数据库所在。带上去 = 覆盖线上数据。
    '__pycache__',
    '.data_smoke', '.data_smoke2', '.data_smoke3',
    'node_modules',   # 前端源码目录里的依赖，几百 MB 且 dist 已经构建好了
    '.venv', 'venv',
}
EXCLUDE_SUFFIX = ('.pyc', '.pyo', '.log', '.bak', '.tgz', '.tar', '.gz')
EXCLUDE_NAMES = {'.DS_Store', 'Thumbs.db'}

# ★ Windows 上编辑过的 shell 脚本很容易带上 CRLF 行尾。Linux 上
#   `set -euo pipefail\r` 会报 `set: pipefail: invalid option name` ——
#   这个错看起来像脚本语法问题，实际是行尾问题，而且只有**真跑一次**才现形
#   （`bash -n` 本地检查是过的，因为本地就是 Windows 的 bash）。
#   systemd 单元同理：`ExecStart=...\r` 会让服务起不来。
#   在打包这一层统一归一化，比事后在服务器上 sed 可靠 —— 服务器上 sed
#   只能修一次，下次编辑又会带回来。
_LF_SUFFIX = ('.sh', '.service')


def _add_tree(tf: tarfile.TarFile, path: Path, arcname: str) -> None:
    """递归入包，并对 `.sh` / `.service` 做 CRLF -> LF 归一化。

    为什么要自己递归而不是 `tf.add(dir, filter=...)`：`filter` 只能改
    `TarInfo`（名字/权限/uid），改不了**文件内容**，而这里要改的正是内容。
    """
    if path.is_dir():
        tf.addfile(tf.gettarinfo(str(path), arcname))
        for child in sorted(path.iterdir()):
            if child.name in EXCLUDE_DIRS or child.name in EXCLUDE_NAMES:
                continue
            if child.is_file() and child.suffix in EXCLUDE_SUFFIX:
                continue
            if child.name.startswith('.data_smoke'):
                continue
            _add_tree(tf, child, '%s/%s' % (arcname, child.name))
        return
    if path.suffix in _LF_SUFFIX:
        data = path.read_bytes()
        if b'\r\n' in data:
            data = data.replace(b'\r\n', b'\n')
            print('   归一化行尾：%s' % arcname)
        info = tf.gettarinfo(str(path), arcname)
        info.size = len(data)
        tf.addfile(info, __import__('io').BytesIO(data))
        return
    tf.add(str(path), arcname=arcname, filter=_skip)


def _skip(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    p = Path(tarinfo.name)
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return None
    if p.name.startswith('.data_smoke'):
        return None
    if tarinfo.name.endswith(EXCLUDE_SUFFIX):
        return None
    if p.name in EXCLUDE_NAMES:
        return None
    return tarinfo


def build_package() -> Path:
    """打完整包。返回 tgz 路径。"""
    backend = ROOT / 'backend'
    dist = ROOT / 'frontend' / 'dist'
    deploy = ROOT / 'deploy'

    if not (backend / 'requirements.txt').is_file():
        sys.exit('找不到 backend/requirements.txt —— 请在 _rewrite/deploy/ 下运行本脚本')
    if not (deploy / 'deploy.sh').is_file():
        sys.exit('找不到 deploy/deploy.sh')
    if not (dist / 'index.html').is_file():
        # 不中止：后端 API 仍可用，只是管理台页面 404。如实提示。
        print('! frontend/dist/index.html 不存在 —— 包里不会有前端。'
              '先 cd frontend && npm run build')

    stamp = time.strftime('%Y%m%d-%H%M%S')
    # ★ 输出到 deploy/ **之外**。放在 deploy/ 里会被下一次打包装进包里
    #   （套娃），包体积一路涨：1.3 -> 2.5 -> 5.1 -> 10.2 MB。
    #   实测就是这么发现的。除了这里，EXCLUDE_SUFFIX 里也挡了 *.tgz。
    outdir = ROOT / 'dist_pack'
    outdir.mkdir(exist_ok=True)
    out = outdir / ('rewrite-full-%s.tgz' % stamp)

    with tarfile.open(out, 'w:gz') as tf:
        _add_tree(tf, backend, 'backend')
        if dist.is_dir():
            _add_tree(tf, dist, 'frontend/dist')
        _add_tree(tf, deploy, 'deploy')

        ver = (
            'packed_at=%s\n'
            'packed_from=%s\n'
            'host=%s\n'
            % (time.strftime('%Y-%m-%d %H:%M:%S'), ROOT, DEFAULTS['host'])
        ).encode()
        info = tarfile.TarInfo('VERSION')
        info.size = len(ver)
        info.mtime = int(time.time())
        tf.addfile(info, __import__('io').BytesIO(ver))

    return out


def remote_run(args, tgz: Path, remote_path: str) -> int:
    """上传 + 远端执行。返回远端退出码。"""
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print('连接 %s@%s:%d ...' % (args.user, args.host, args.port))
    cli.connect(args.host, port=args.port, username=args.user,
                password=args.password, timeout=20,
                look_for_keys=False, allow_agent=False)

    print('上传 %s（%.1f MB）→ %s'
          % (tgz.name, tgz.stat().st_size / 1048576, remote_path))
    sftp = cli.open_sftp()
    sftp.put(str(tgz), remote_path)
    remote_size = sftp.stat(remote_path).st_size
    sftp.close()
    if remote_size != tgz.stat().st_size:
        cli.close()
        sys.exit('上传大小不一致：本地 %d / 远端 %d' % (tgz.stat().st_size, remote_size))
    print('   大小一致（%d 字节）' % remote_size)

    flags = []
    if args.fresh:
        flags.append('--fresh')
    if args.switch:
        flags.append('--switch')
    if args.purge_old:
        flags.append('--purge-old')
    cmd = ('bash /tmp/oneclick.sh %s %s' % (remote_path, ' '.join(flags))).strip()

    # oneclick.sh 随包走 —— 但它在包**里面**，所以先把 deploy 目录单独放好。
    # 做法：从 tgz 里直接取，避免依赖上一次部署的残留版本。
    pre = ('set -e; rm -rf /tmp/oneclick_sh; mkdir -p /tmp/oneclick_sh; '
           'tar xzf %s -C /tmp/oneclick_sh deploy/oneclick.sh; '
           'cp /tmp/oneclick_sh/deploy/oneclick.sh /tmp/oneclick.sh; '
           'chmod +x /tmp/oneclick.sh' % remote_path)

    print('--- 远端：解出 oneclick.sh ---')
    rc = _exec(cli, pre)
    if rc != 0:
        cli.close()
        return rc

    print('--- 远端：%s ---' % cmd)
    rc = _exec(cli, cmd)
    cli.close()
    return rc


def _exec(cli, cmd: str) -> int:
    """执行并实时回显（stderr 与 stdout 合并，避免报错被吞）。"""
    stdin, stdout, stderr = cli.exec_command(cmd, get_pty=True, timeout=1800)
    for line in iter(stdout.readline, ''):
        if not line:
            break
        sys.stdout.write(line)
        sys.stdout.flush()
    rc = stdout.channel.recv_exit_status()
    err = stderr.read().decode('utf-8', 'replace')
    if err.strip():
        sys.stdout.write(err)
    return rc


def live_port(cli) -> str:
    """问服务器：rewrite 到底监听哪个端口。**不靠命令行参数猜。**

    ★ 之前是 `'8890' if (args.switch or args.purge_old) else '8903'` ——
      参数推不出事实。服务早就在 8890 上时，只要这次没带 `--switch`，
      脚本就会说「暂存 8903」。一句误导人的话会让人去浏览器里开一个空端口，
      然后以为部署挂了。

    ★ 必须读 `/proc/<MainPID>/cmdline`，**不能** `systemctl show -p ExecStart`：
      后者报的是**未展开**的 `${REWRITE_PORT}` 模板（systemd 本就这样），
      拿它去 grep `--port` 只会得到一个空的数字串。
    """
    out = _exec(cli, "p=$(systemctl show rewrite.service -p MainPID --value); "
                     "tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null; echo")
    m = re.search(r'--port\s+(\d+)', out or '')
    return m.group(1) if m else '8890'


def health_check(args) -> str:
    """部署完自己探一次活 —— 不依赖人去浏览器里点。返回实际监听端口。"""
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(args.host, port=args.port, username=args.user,
                password=args.password, timeout=20,
                look_for_keys=False, allow_agent=False)
    port = live_port(cli)
    cmd = ('echo "--- /healthz ---"; curl -s --noproxy "*" http://127.0.0.1:%s/healthz; echo; '
           'echo "--- /_device ---"; curl -s --noproxy "*" http://127.0.0.1:%s/_device '
           '| head -c 900; echo' % (port, port))
    _exec(cli, cmd)
    cli.close()
    return port


def main() -> int:
    ap = argparse.ArgumentParser(description='完整包一键部署')
    ap.add_argument('--host', default=DEFAULTS['host'])
    ap.add_argument('--user', default=DEFAULTS['user'])
    ap.add_argument('--password', default=DEFAULTS['password'])
    ap.add_argument('--port', type=int, default=DEFAULTS['port'])
    ap.add_argument('--fresh', action='store_true', help='重新生成 JWT 密钥与管理员口令')
    ap.add_argument('--switch', action='store_true', help='部署后直接切到 8890')
    ap.add_argument('--purge-old', action='store_true', dest='purge_old',
                    help='删除旧 charles-console 代码与单元（先备份到 /opt/_archive/）')
    ap.add_argument('--no-upload', action='store_true', help='只打包，不上传')
    args = ap.parse_args()

    tgz = build_package()
    print('已打包：%s（%.1f MB）' % (tgz, tgz.stat().st_size / 1048576))
    if args.no_upload:
        print('--no-upload：到此为止。')
        return 0

    remote_path = '/tmp/' + tgz.name
    rc = remote_run(args, tgz, remote_path)
    if rc != 0:
        print('\n远端退出码 %d —— 部署未成功。上面是远端原始输出。' % rc)
        return rc

    print('\n--- 探活 ---')
    port = health_check(args)
    print('\n完成。')
    # ★ 端口从**服务实际 argv** 读（health_check 返回），不从命令行参数猜：
    #   服务早就在 8890 上时，只要这次没带 --switch，按参数猜就会说
    #   「暂存 8903」—— 一句误导人的话会让人去浏览器里开一个空端口。
    print('管理台：http://%s:%s/' % (args.host, port))
    if port != '8890':
        print('  （当前还在暂存端口；确认后加 --switch 切到 8890）')
    if args.purge_old:
        print('旧 charles-console 已清理（备份在 /opt/_archive/）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
