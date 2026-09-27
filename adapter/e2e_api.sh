#!/usr/bin/env bash
# 一花卡密系统 -> CCProxy 适配层：真实 HTTP API 全生命周期测试。
# 跑在服务器上（宿主机）。不修改任何源码，只用系统自己的接口。
set -uo pipefail

BASE="http://127.0.0.1:8081"
HOSTHDR="Host: 203.0.113.10:8081"   # 一花 common.php 要求 siteurl == HTTP_HOST
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
APP="4b153d35c751a0f71323b63371cc51d6"
DBP="MySQL-Root-ChangeMe"
APWD="Adapter-Token-ChangeMe"
ADAPTER="http://127.0.0.1:8893"

TAG="$(date +%s)"
U="e2e$((RANDOM))$((RANDOM))"
U="${U:0:12}"
CARD_NEW="E2E-NEW-$TAG"
CARD_RENEW="E2E-RENEW-$TAG"
EXT='{"connection":-1,"bandwidthup":-1,"bandwidthdown":-1}'
MYSID="$(echo -n "$TAG" | md5sum | cut -c1-32)"

PASS=0; FAIL=0
ok() { if [ "$2" = "1" ]; then PASS=$((PASS+1)); printf '  [PASS] %s %s\n' "$1" "${3:-}"; else FAIL=$((FAIL+1)); printf '  [FAIL] %s %s\n' "$1" "${3:-}"; fi; }
db() { docker exec -i yihua-db mysql -uroot -p"$DBP" --default-character-set=utf8mb4 -N ccpy 2>/dev/null; }
accounts() { curl -s -u "admin:$APWD" "$ADAPTER/status" --max-time 8 | grep -o '"accounts": *[0-9]*' | grep -o '[0-9]*'; }

# 造后台会话令牌（用程序自己的 authcode，不改源码）
# 工具固定在 /opt/ccpx/tools/（0700 root，不在 webroot）。
# 绝不能把「免鉴权就吐 token」的 PHP 长期放在 webroot —— 那是后门。
TOKEN=$(sudo -n /opt/ccpx/tools/mktoken.sh admin TestPass1! 2>/dev/null | tr -d '\r\n' | head -c 200)
if [ -z "$TOKEN" ]; then
  echo "!! 无法生成后台会话令牌，检查 /opt/ccpx/tools/mktoken.sh"
  exit 2
fi
docker exec -i yihua-db mysql -uroot -p"$DBP" ccpy 2>/dev/null <<SQL
UPDATE sub_admin SET cookies='$TOKEN' WHERE username='admin';
SQL

# 通用请求头。sec_defend 是必须的：一花 cc_defender() 的判据
# `!substr($cookie,0,32) === substr($iptoken,0,32)` 恒为 false，
# 所以只要带上任意 sec_defend 就直接放行，不需要真去解那段 JS。
# 注意：Cookie 只能有一个头，多个 Cookie 头 PHP 只会读到第一个。
HDRS=(-H "$HOSTHDR" -H "User-Agent: $UA"
      -H "Cookie: sec_defend=1; mysid=$MYSID; sub_admin_token=$TOKEN")

api() { # $1=type  $2..=k=v
  local t="$1"; shift
  local args=()
  for kv in "$@"; do args+=(--data-urlencode "$kv"); done
  curl -s "${HDRS[@]}" "${args[@]}" "$BASE/api/cpproxy.php?type=$t" --max-time 25
}

echo "测试账号: $U"
echo "卡密: $CARD_NEW / $CARD_RENEW"

echo
echo "== 0. 清理可能的残留 =="
db <<SQL
DELETE FROM kami WHERE kami IN ('$CARD_NEW','$CARD_RENEW');
SQL
echo "  已清理"

echo
echo "== 1. 造两张卡（+30 天 / +30 天） =="
db <<SQL
INSERT INTO kami (kami,times,comment,host,sc_user,state,app,ext)
VALUES ('$CARD_NEW','+30 day','e2e','','admin',0,'$APP','$EXT'),
       ('$CARD_RENEW','+30 day','e2e','','admin',0,'$APP','$EXT');
SQL
N=$(db <<<"select count(*) from kami where kami in ('$CARD_NEW','$CARD_RENEW');")
ok "两张卡已入库" "$([ "$N" = "2" ] && echo 1 || echo 0)" "count=$N"

echo
echo "== 2. 服务器列表接口（真实后台 API） =="
SRV=$(curl -s "${HDRS[@]}" \
  "$BASE/sub_admin/ajax.php?act=servertable&page=1&limit=20&ip=&comment=" --max-time 20)
echo "  ${SRV:0:260}"
echo "$SRV" | grep -q '172.20.0.1' && ok "服务器列表指向适配层 172.20.0.1" 1 || ok "服务器列表指向适配层 172.20.0.1" 0
echo "$SRV" | grep -q '8893' && ok "端口 8893 已登记" 1 || ok "端口 8893 已登记" 0

echo
echo "== 3. 发卡（type=insert，真实兑换链路） =="
R=$(api insert "user=$U" "pwd=aa12345" "code=$CARD_NEW")
echo "  ${R:0:200}"
echo "$R" | grep -q '"code":1' && ok "发卡成功" 1 || ok "发卡成功" 0
sleep 1
A=$(accounts)
ok "适配层账号数 = 1" "$([ "$A" = "1" ] && echo 1 || echo 0)" "accounts=$A"
KSTATE=$(db <<<"select state from kami where kami='$CARD_NEW';")
ok "卡密已置为已使用(state=1)" "$([ "$KSTATE" = "1" ] && echo 1 || echo 0)" "state=$KSTATE"
KUSER=$(db <<<"select username from kami where kami='$CARD_NEW';")
ok "卡密记录了账号名" "$([ "$KUSER" = "$U" ] && echo 1 || echo 0)" "username=$KUSER"

echo
echo "== 4. 重复使用同一张卡必须被拒 =="
R=$(api insert "user=${U}x" "pwd=aa12345" "code=$CARD_NEW")
echo "  ${R:0:160}"
echo "$R" | grep -q '卡密已被使用' && ok "重复兑换被拒" 1 || ok "重复兑换被拒" 0

echo
echo "== 5. 查询（type=query） =="
R=$(api query "appcode=$APP" "user=$U")
echo "  ${R:0:320}"
echo "$R" | grep -q '到期时间' && ok "查到账号到期信息" 1 || ok "查到账号到期信息" 0
echo "$R" | grep -q "$U" && ok "响应里带账号名" 1 || printf '  [note] 源码 userquer() 只回到期时间，不回账号名，属正常\n'

echo
echo "== 6. 续费（type=update） =="
BEFORE=$(curl -s -u "admin:$APWD" "$ADAPTER/account" --max-time 8 | grep -A1 'name="disabledate"' | grep -o 'value="[0-9-]*"' | head -1 | cut -d'"' -f2)
R=$(api update "user=$U" "code=$CARD_RENEW")
echo "  ${R:0:200}"
echo "$R" | grep -q '"code":1' && ok "续费成功" 1 || ok "续费成功" 0
sleep 1
AFTER=$(curl -s -u "admin:$APWD" "$ADAPTER/account" --max-time 8 | grep -A1 'name="disabledate"' | grep -o 'value="[0-9-]*"' | head -1 | cut -d'"' -f2)
echo "  续费前 $BEFORE -> 续费后 $AFTER"
D1=$(date -d "${BEFORE:-1970-01-01}" +%s 2>/dev/null || echo 0)
D2=$(date -d "${AFTER:-1970-01-01}"  +%s 2>/dev/null || echo 0)
ok "到期时间被延长" "$([ "$D2" -gt "$D1" ] && echo 1 || echo 0)" "$(( (D2-D1)/86400 )) 天"
KSTATE2=$(db <<<"select state from kami where kami='$CARD_RENEW';")
ok "续费卡已置为已使用" "$([ "$KSTATE2" = "1" ] && echo 1 || echo 0)" "state=$KSTATE2"

echo
echo "== 7. 删除（type=del） =="
R=$(api del "username=$U" "admin_username=admin" "admin_password=$APWD" \
        "admin_port=8893" "proxyaddress=172.20.0.1")
echo "  ${R:0:200}"
echo "$R" | grep -q '删除用户成功' && ok "删除成功" 1 || ok "删除成功" 0
sleep 1
A=$(accounts)
ok "适配层账号数 = 0" "$([ "$A" = "0" ] && echo 1 || echo 0)" "accounts=$A"

echo
echo "== 8. 清会话与测试数据 =="
docker exec -i yihua-db mysql -uroot -p"$DBP" ccpy 2>/dev/null <<SQL
UPDATE sub_admin SET cookies='' WHERE username='admin';
DELETE FROM kami WHERE kami IN ('$CARD_NEW','$CARD_RENEW');
SQL
echo "  已清理"

echo
printf '通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
