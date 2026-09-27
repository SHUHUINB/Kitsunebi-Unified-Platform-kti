#!/usr/bin/env bash
# 把 8890 从旧 charles-console 让给新后端。
#
# 顺序不能反：
#   ① 先把新后端改到 8890 并**停掉**（此时端口是空的）
#   ② 再停旧 charles-console
#   ③ 起新后端
# 反过来做会出现「两个服务都想绑 8890」，谁先起来谁占，另一个 failed，
# 而 systemd 会一直重试 —— 排查起来像是「服务莫名其妙起不来」。
#
# 回滚：bash rollback.sh
set -euo pipefail

BASE=/opt/rewrite
ENVF=$BASE/rewrite.env
OLD_UNIT=charles-console
NEW_UNIT=rewrite

echo "== 0/5 前置检查 =="
systemctl is-active "$NEW_UNIT" >/dev/null || { echo "新后端没在跑，先 bash deploy.sh"; exit 1; }
PORT=$(grep -E '^REWRITE_PORT=' "$ENVF" | cut -d= -f2)
echo "   新后端当前端口：$PORT"

echo "== 1/5 备份旧单元与配置 =="
STAMP=$(date +%Y%m%d-%H%M%S)
sudo mkdir -p "$BASE/_bak_$STAMP"
sudo cp /etc/systemd/system/$OLD_UNIT.service "$BASE/_bak_$STAMP/" 2>/dev/null || true
sudo cp "$ENVF" "$BASE/_bak_$STAMP/rewrite.env"
echo "   备份在 $BASE/_bak_$STAMP/"

echo "== 2/5 新后端改到 8890 并停止 =="
sudo sed -i 's/^REWRITE_PORT=.*/REWRITE_PORT=8890/' "$ENVF"
sudo systemctl stop "$NEW_UNIT"

echo "== 3/5 停旧 charles-console =="
# 已经用 `deploy.sh --purge-old` 删过的话这里会失败 —— 那不是错误，是已经做过。
if systemctl list-unit-files "$OLD_UNIT.service" >/dev/null 2>&1 \
   && [ -f "/etc/systemd/system/$OLD_UNIT.service" ]; then
  sudo systemctl stop "$OLD_UNIT" || true
  sudo systemctl disable "$OLD_UNIT" >/dev/null 2>&1 || true
  echo "   已停用 $OLD_UNIT（代码目录还在：/opt/charles-console/）"
  echo "   要连代码一起删：bash deploy.sh --purge-old"
else
  echo "   $OLD_UNIT 已不存在（可能已 --purge-old 清理过），跳过"
fi

echo "== 4/5 起新后端 =="
sudo systemctl restart "$NEW_UNIT"
sleep 3
systemctl is-active "$NEW_UNIT"
curl -s --noproxy '*' -o /dev/null -w '   /healthz -> HTTP %{http_code}\n' http://127.0.0.1:8890/healthz

echo "== 5/5 设备侧路径复核（这几条不要求登录，手机要用）=="
for p in /ok /socks /ruleset.conf /ca.crt /ca.pem; do
  printf '   %-14s -> HTTP %s\n' "$p" \
    "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8890$p)"
done

cat <<'EOF'

切换完成。外网验证：
  curl -s -o /dev/null -w '%{http_code}\n' http://203.0.113.10:8890/healthz

回滚：
  bash rollback.sh

下一步（可选，不可逆）：
  删掉旧管理台的代码与单元 —— 会先备份到 /opt/_archive/：
    bash deploy.sh --purge-old

★ 一花 PHP（8081）本次**没动**。按 D2 它要下线，但下线是不可逆的，
  建议先并行跑几天确认新卡密链路无异常，再执行：
    sudo docker stop yihua-app && sudo docker update --restart=no yihua-app
EOF
