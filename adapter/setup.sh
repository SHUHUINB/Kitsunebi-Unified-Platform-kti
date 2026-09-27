#!/usr/bin/env bash
# 在服务器上安装/更新 CCProxy 适配层。幂等，可重复执行。
# 用法（在服务器上，需 sudo 免密）： sudo bash /tmp/ccpx_setup.sh
set -euo pipefail

SRC_DIR="/opt/ccpx"
SVC="/etc/systemd/system/ccpx.service"
PUB_IP="203.0.113.10"

echo "== 1. 目录与用户 =="
id -u ccpx >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ccpx
install -d -o ccpx -g ccpx -m 0750 "$SRC_DIR"

echo "== 2. 放置文件 =="
install -o ccpx -g ccpx -m 0640 /tmp/ccpx_adapter.py "$SRC_DIR/ccproxy_adapter.py"
if [ ! -f "$SRC_DIR/config.json" ]; then
  install -o ccpx -g ccpx -m 0640 /tmp/ccpx_config.json "$SRC_DIR/config.json"
  echo "   写入新 config.json"
else
  echo "   保留已有 config.json（不覆盖）"
fi
install -o ccpx -g ccpx -m 0644 /tmp/ccpx_README.md "$SRC_DIR/README.md"

echo "== 2b. 运维工具（0700 root，刻意不放在 webroot） =="
# 这两个文件一个是「能建改删账号」的探针，一个是「免鉴权吐后台会话 token」的工具。
# 曾经被留在卡密系统 webroot 上（:8081 未鉴权即可执行），所以固定放这里。
install -d -o root -g root -m 0700 "$SRC_DIR/tools"
for f in _ccpx_probe.php _ccpx_mktoken.php mktoken.sh; do
  if [ -f "/tmp/$f" ]; then
    install -o root -g root -m 0700 "/tmp/$f" "$SRC_DIR/tools/$f"
    echo "   安装 tools/$f"
  fi
done
# 复验：webroot 上不应有这两个文件名
for p in _ccpx_probe.php _ccpx_mktoken.php; do
  if [ -f "/opt/yihua/app/$p" ]; then
    echo "!! 警告：/opt/yihua/app/$p 还在 webroot 上，必须挪走"
  fi
done

echo "== 3. 协议自检（必须全绿才继续） =="
sudo -u ccpx /usr/bin/python3 "$SRC_DIR/ccproxy_adapter.py" --selftest

echo "== 4. systemd =="
install -m 0644 /tmp/ccpx.service "$SVC"
systemctl daemon-reload
systemctl enable ccpx >/dev/null
systemctl restart ccpx
sleep 2
systemctl is-active ccpx

echo "== 5. 监听检查 =="
ss -ltnp | grep -E ':8893|:8892' || { echo "!! 端口未监听"; journalctl -u ccpx -n 40 --no-pager; exit 1; }

echo "== 6. 本机自测 /account（admin 凭据） =="
PWD_=$(/usr/bin/python3 -c "import json;print(json.load(open('$SRC_DIR/config.json'))['admin_password'])")
curl -s -o /dev/null -w "  GET  /account  -> %{http_code}\n" \
  -u "admin:$PWD_" "http://127.0.0.1:8893/account"
curl -s -u "admin:$PWD_" "http://127.0.0.1:8893/status"; echo

echo "== 7. 公网自测 =="
curl -s -o /dev/null -w "  公网 GET /account -> %{http_code}\n" \
  -u "admin:$PWD_" "http://$PUB_IP:8893/account"

echo "== 8. 容器内可达性（yihua-app 要能 fsockopen 到适配层） =="
for TARGET in "$PUB_IP" "172.20.0.1"; do
  docker exec -e T="$TARGET" -e P="$PWD_" yihua-app php -r '
$ip=getenv("T");
$fp=@fsockopen($ip,8893,$e,$s,3);
if(!$fp){ echo "  容器 -> $ip:8893 FAIL ($s)\n"; exit; }
echo "  容器 -> $ip:8893 OK\n";
fputs($fp,"GET /account HTTP/1.0\r\nHost: $ip\r\nAuthorization: Basic ".base64_encode("admin:".getenv("P"))."\r\n\r\n");
$line=""; while(!feof($fp)){ $line.=fread($fp,4096); } fclose($fp);
preg_match_all("/<input .* name=\"username\" .* value=\"(.*?)\"/ui",$line,$m);
echo "  容器用源码正则解析到账号数: ".count($m[1])."\n";
' 2>/dev/null || echo "  容器 -> $TARGET:8893 探测异常"
done

echo
echo "完成。管理密码: $PWD_"
