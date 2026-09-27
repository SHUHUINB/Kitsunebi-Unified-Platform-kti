#!/usr/bin/env bash
# 验证「卡密账号能真的当代理用」：HTTP CONNECT / 普通 HTTP / SOCKS5，出口经花瓶。
# 鉴权负例用裸 socket 读状态行 —— curl 遇到 407 会按挑战重试，重试失败只回 000，
# 分不清「代理拒绝」和「网络断了」。
set -uo pipefail

APWD="Adapter-Token-ChangeMe"
A="http://127.0.0.1:8893"
U="pxy$((RANDOM))"
U="${U:0:9}"
P="zz12345"

PASS=0; FAIL=0
ok() { if [ "$2" = "1" ]; then PASS=$((PASS+1)); printf '  [PASS] %s %s\n' "$1" "${3:-}"; else FAIL=$((FAIL+1)); printf '  [FAIL] %s %s\n' "$1" "${3:-}"; fi; }

post() { curl -s -u "admin:$APWD" -X POST --data "$1" "$A/account" --max-time 15; }

# 裸 socket：$1 = 目标端口, $2 = printf 报文；回第一行
raw() {
  local port="$1" payload="$2" out
  out=$(timeout 12 bash -c "
    exec 3<>/dev/tcp/127.0.0.1/$port || exit 9
    printf '%b' \"\$0\" >&3
    timeout 8 head -c 300 <&3 | tr -d '\r' | head -n 1
    exec 3<&- 3>&-
  " "$payload" 2>/dev/null)
  printf '%s' "$out"
}
b64() { printf '%s' "$1" | base64 -w0; }

echo "测试账号: $U / $P"

echo
echo "== 1. 建账号（未过期） =="
R=$(post "add=1&autodisable=1&enable=1&usepassword=1&username=$U&password=$P&connection=-1&bandwidth=-1&disabledate=2027-12-31&disabletime=23:59:59&userid=-1")
echo "  $R"
ok "账号已建" "$([ "$R" = "OK: 已创建" ] && echo 1 || echo 0)" "$R"

echo
echo "== 2. 正向流量（curl 端到端） =="
C=$(curl -s -o /dev/null -w '%{http_code}' -x "http://$U:$P@127.0.0.1:8893" https://www.baidu.com/ --max-time 25)
ok "CONNECT https://www.baidu.com -> 200" "$([ "$C" = "200" ] && echo 1 || echo 0)" "code=$C"

C=$(curl -s -o /dev/null -w '%{http_code}' -x "http://$U:$P@127.0.0.1:8893" http://www.baidu.com/ --max-time 25)
ok "普通 http://www.baidu.com -> 200" "$([ "$C" = "200" ] && echo 1 || echo 0)" "code=$C"

C=$(curl -s -o /dev/null -w '%{http_code}' -x "http://$U:$P@127.0.0.1:8893" http://www.qq.com/ --max-time 25)
ok "普通 http://www.qq.com -> 真实响应(2xx/3xx)" \
   "$([ "$C" = "200" ] || [ "$C" = "301" ] || [ "$C" = "302" ] && echo 1 || echo 0)" "code=$C"

C=$(curl -s -o /dev/null -w '%{http_code}' -x "http://$U:$P@127.0.0.1:8893" http://httpbin.org/ip --max-time 25)
ok "普通 http://httpbin.org/ip -> 200" "$([ "$C" = "200" ] && echo 1 || echo 0)" "code=$C"

C=$(curl -s -o /dev/null -w '%{http_code}' --socks5-hostname "$U:$P@127.0.0.1:8892" https://www.baidu.com/ --max-time 25)
ok "SOCKS5 https://www.baidu.com -> 200" "$([ "$C" = "200" ] && echo 1 || echo 0)" "code=$C"

C=$(curl -s -o /dev/null -w '%{http_code}' --socks5-hostname "$U:$P@127.0.0.1:8892" http://www.baidu.com/ --max-time 25)
ok "SOCKS5 普通 http://www.baidu.com -> 200" "$([ "$C" = "200" ] && echo 1 || echo 0)" "code=$C"

echo
echo "== 3. 鉴权负例（裸 socket，读状态行） =="
L=$(raw 8893 "CONNECT www.baidu.com:443 HTTP/1.1\r\nHost: www.baidu.com:443\r\n\r\n")
ok "CONNECT 无凭据 -> 407" "$(case "$L" in *407*) echo 1;; *) echo 0;; esac)" "$L"

L=$(raw 8893 "CONNECT www.baidu.com:443 HTTP/1.1\r\nHost: www.baidu.com:443\r\nProxy-Authorization: Basic $(b64 "$U:wrongpw")\r\n\r\n")
ok "CONNECT 错密码 -> 407" "$(case "$L" in *407*) echo 1;; *) echo 0;; esac)" "$L"

L=$(raw 8893 "CONNECT www.baidu.com:443 HTTP/1.1\r\nHost: www.baidu.com:443\r\nProxy-Authorization: Basic $(b64 "admin:$APWD")\r\n\r\n")
ok "CONNECT 管理凭据当代理用 -> 407" "$(case "$L" in *407*) echo 1;; *) echo 0;; esac)" "$L"

L=$(raw 8893 "GET http://www.baidu.com/ HTTP/1.1\r\nHost: www.baidu.com\r\n\r\n")
ok "普通请求无凭据 -> 407" "$(case "$L" in *407*) echo 1;; *) echo 0;; esac)" "$L"

L=$(raw 8893 "GET /account HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic $(b64 "$U:$P")\r\n\r\n")
ok "管理面不认用户账号 -> 401" "$(case "$L" in *401*) echo 1;; *) echo 0;; esac)" "$L"

echo
echo "== 4. 到期即断（改成昨天到期） =="
post "edit=1&autodisable=1&usepassword=1&username=$U&password=$P&connection=-1&bandwidth=-1&disabledate=$(date -d yesterday +%F)&disabletime=00:00:00&bandwidthquota=4560&enable=1&userid=$U" >/dev/null
sleep 1
L=$(raw 8893 "CONNECT www.baidu.com:443 HTTP/1.1\r\nHost: www.baidu.com:443\r\nProxy-Authorization: Basic $(b64 "$U:$P")\r\n\r\n")
ok "HTTP 到期后 -> 407" "$(case "$L" in *407*) echo 1;; *) echo 0;; esac)" "$L"

S=$(timeout 12 bash -c '
  exec 3<>/dev/tcp/127.0.0.1/8892 || exit 9
  printf "\x05\x01\x02" >&3
  timeout 6 head -c 2 <&3 | od -An -tx1 | tr -d " \n"
  exec 3<&- 3>&-
' 2>/dev/null)
ok "SOCKS5 支持用户名密码方式(0x05 0x02)" "$([ "$S" = "0502" ] && echo 1 || echo 0)" "$S"

S=$(timeout 12 bash -c '
  exec 3<>/dev/tcp/127.0.0.1/8892 || exit 9
  printf "\x05\x01\x00" >&3
  timeout 6 head -c 2 <&3 | od -An -tx1 | tr -d " \n"
  exec 3<&- 3>&-
' 2>/dev/null)
ok "SOCKS5 拒绝 no-auth(0x05 0xFF)" "$([ "$S" = "05ff" ] && echo 1 || echo 0)" "$S"

echo
echo "== 5. 清理 =="
post "delete=1&userid=$U" >/dev/null
N=$(curl -s -u "admin:$APWD" "$A/status" --max-time 8 | grep -o '"accounts": *[0-9]*' | grep -o '[0-9]*')
ok "账号已删（accounts=0）" "$([ "$N" = "0" ] && echo 1 || echo 0)" "accounts=$N"

echo
echo "== 6. 证据：适配层日志里的出口选择（via） =="
sudo -n tail -n 40 /opt/ccpx/adapter.log 2>/dev/null | grep -E 'via|-> (charles|direct)|CONNECT|SOCKS5|普通' | tail -n 12 | sed 's/^/  /' || echo "  (需要 sudo 才能读 /opt/ccpx/adapter.log)"

echo
printf '通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
