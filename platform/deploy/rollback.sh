#!/usr/bin/env bash
# 回滚：把 8890 还给旧 charles-console。
#
# 只动端口与服务启停，不删任何数据 —— 新后端的库在 /opt/rewrite/var，
# 回滚后依然在，下次切换直接复用（卡密不会丢）。
set -euo pipefail

BASE=/opt/rewrite
ENVF=$BASE/rewrite.env
OLD_UNIT=charles-console
NEW_UNIT=rewrite

echo "== 1/4 新后端退回暂存端口并停止 =="
sudo sed -i 's/^REWRITE_PORT=.*/REWRITE_PORT=8903/' "$ENVF"
sudo systemctl stop "$NEW_UNIT"

echo "== 2/4 恢复旧 charles-console =="
sudo systemctl enable "$OLD_UNIT" >/dev/null 2>&1 || true
sudo systemctl start "$OLD_UNIT"
sleep 2
systemctl is-active "$OLD_UNIT"
curl -s --noproxy '*' -o /dev/null -w '   :8890/healthz -> HTTP %{http_code}\n' http://127.0.0.1:8890/healthz || true

echo "== 3/4 新后端按暂存端口重新起来 =="
sudo systemctl start "$NEW_UNIT"
sleep 2
systemctl is-active "$NEW_UNIT"
curl -s --noproxy '*' -o /dev/null -w '   :8903/healthz -> HTTP %{http_code}\n' http://127.0.0.1:8903/healthz || true

echo "== 4/4 完成 =="
echo "已回滚到旧 charles-console（8890）。新后端仍在 8903 暂存。"
echo "数据未动：/opt/rewrite/var 原样保留。"
