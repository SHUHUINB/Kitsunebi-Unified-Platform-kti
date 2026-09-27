#!/usr/bin/env bash
# 部署 / 更新新后端（幂等，可重复跑）。
#
# 默认部署到 **8903 暂存端口**，不碰线上 8890。
# 切换 8890 是单独一步 —— 见 switchover.sh，或用 `--switch` 在本脚本末尾顺带做。
# 两件事分开，是因为「部署」出错只影响暂存端口，「切换」出错才是事故。
#
# 用法（在服务器上以 ubuntu 跑，需要免密 sudo）：
#     bash deploy.sh                 # 部署 + 重启 + 冒烟（端口沿用现有 rewrite.env）
#     bash deploy.sh --fresh         # 同上，并**重新生成** JWT 密钥与管理员强口令
#     bash deploy.sh --fresh --switch # 全新部署 + 生成强口令 + 直接切到 8890
#     bash deploy.sh --no-smoke      # 只部署
#     bash deploy.sh --switch        # 部署完顺带执行 switchover.sh
#     bash deploy.sh --purge-old     # 删掉旧 charles-console（**先备份**，需已在 8890 上跑通）
#
# ★ 首次部署（rewrite.env 不存在）会**自动**走 --fresh —— 绝不把
#   CHANGE_ME 模板值带上线。这是之前 `Admin-ChangeMe-2026` 差点上线的教训。
set -euo pipefail

BASE=/opt/rewrite
VENV=$BASE/venv
SRC_DIR=${SRC_DIR:-/tmp/rewrite_src}
SMOKE=${SMOKE:-1}
FRESH=0
SWITCH=0
PURGE_OLD=0

for a in "$@"; do
  case "$a" in
    --no-smoke)  SMOKE=0 ;;
    --fresh)     FRESH=1 ;;
    --switch)    SWITCH=1 ;;
    --purge-old) PURGE_OLD=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

# 生成随机值。去掉 `=` 与 `/` `+` —— 它们出现在 systemd EnvironmentFile 里
# 需要引号转义，去掉之后可以直接裸写，省一类「看着一样其实被截断」的坑。
gen_secret() { openssl rand -base64 48 | tr -d '\n=+/' ; }
gen_pass()   { openssl rand -base64 18 | tr -d '\n=+/' ; }

echo "== 1/7 目录与 venv =="
sudo mkdir -p "$BASE" "$BASE/var"
if [ ! -x "$VENV/bin/python" ]; then
  sudo python3 -m venv "$VENV"
  echo "   venv 已建：$($VENV/bin/python -V)"
else
  echo "   venv 已存在：$($VENV/bin/python -V)"
fi

echo "== 2/7 依赖 =="
# 版本锁在 requirements.txt 里，不在这里写死 —— 两处写版本号迟早对不上。
sudo "$VENV/bin/python" -m pip install --no-input -q --disable-pip-version-check \
     -r "$SRC_DIR/backend/requirements.txt"
"$VENV/bin/python" -c "import fastapi,uvicorn,sqlalchemy,pydantic,jwt,httpx,bcrypt,multipart; print('   deps ok')"
# 二维码是可选件：装了引导页就有扫码，没装也不影响证书下载（代码里已兜底）。
"$VENV/bin/python" -c "import qrcode; print('   qrcode ok（/ca 页面有二维码）')" \
  || echo "   ! qrcode 未装 —— /ca 页面仍可用，只是没有二维码"

echo "== 3/7 同步代码 =="
# rsync --delete 会删掉 var/ 以外的旧文件；var/ 与 venv/ 必须排除，
# 否则一次部署就把数据库和虚拟环境删了。
sudo rsync -a --delete \
     --exclude 'var/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.data_smoke*' \
     "$SRC_DIR/backend/" "$BASE/backend/"
if [ -d "$SRC_DIR/frontend/dist" ]; then
  sudo mkdir -p "$BASE/frontend"
  sudo rsync -a --delete "$SRC_DIR/frontend/dist/" "$BASE/frontend/dist/"
  echo "   前端 dist 已同步"
else
  echo "   ! 没有 frontend/dist —— SPA 会回 404（/_frontend 会如实说没构建）"
fi

echo "== 4/7 环境文件 =="
DB_EXISTED=0
[ -f "$BASE/var/platform.db" ] && DB_EXISTED=1
if [ ! -f "$BASE/rewrite.env" ]; then
  sudo cp "$SRC_DIR/deploy/rewrite.env.example" "$BASE/rewrite.env"
  FRESH=1                      # 首次部署强制生成强口令
  echo "   已生成 $BASE/rewrite.env（首次）"
else
  echo "   已存在，保留不动（改配置请直接编辑它）"
fi

# ---- 已知坏值迁移 ----------------------------------------------------------
# ★ 「保留不动」是对的（里面是用户的真实设置），但**已知写错的项必须纠正**，
#   否则坏值会跨版本永久存活。这条是踩出来的：
#
#   REWRITE_CHARLES_CFG 曾经默认写成 `~/.charles/config.xml`，而那个路径
#   在当前部署里**不存在**（Charles 的配置是单文件 `~/.charles.config`）。
#   后果不是报错，是静默失效：`/charles/state` 老实回 `configSize: 0`，
#   配置页读空、「增量改动」写进没人看的文件 —— 而所有冒烟都是绿的。
#
#   只改代码默认值救不了已在跑的部署：env 覆盖默认值，而 env 被「保留不动」。
#   所以这里做一次显式迁移，**改前备份**，并把判据收得很窄 ——
#   只有「值精确等于那个已知坏路径」才动，用户自己填的路径一律不碰。
if sudo grep -qE '^REWRITE_CHARLES_CFG=.*/\.charles/config\.xml$' "$BASE/rewrite.env" 2>/dev/null; then
  sudo cp "$BASE/rewrite.env" "$BASE/rewrite.env.bak_cfgpath_$(date +%Y%m%d-%H%M%S)"
  sudo sed -i 's|^REWRITE_CHARLES_CFG=.*|REWRITE_CHARLES_CFG=/home/ubuntu/.charles.config|' \
       "$BASE/rewrite.env"
  echo "   [迁移] REWRITE_CHARLES_CFG 由 ~/.charles/config.xml 修正为 /home/ubuntu/.charles.config"
  echo "          （原值备份在 $BASE/rewrite.env.bak_cfgpath_*）"
fi

if [ "$FRESH" = "1" ]; then
  JWT=$(gen_secret)
  PASS=$(gen_pass)
  sudo sed -i "s|^REWRITE_JWT_SECRET=.*|REWRITE_JWT_SECRET=$JWT|" "$BASE/rewrite.env"
  sudo sed -i "s|^REWRITE_ADMIN_PASS=.*|REWRITE_ADMIN_PASS=$PASS|"   "$BASE/rewrite.env"
  echo
  echo "   ★★ 管理员口令（只显示这一次，记下来）★★"
  echo "       用户名：$(sudo grep -E '^REWRITE_ADMIN_USER=' "$BASE/rewrite.env" | cut -d= -f2)"
  echo "       密  码：$PASS"
  if [ "$DB_EXISTED" = "1" ]; then
    echo "   ! 注意：数据库已存在（$BASE/var/platform.db）。"
    echo "     REWRITE_ADMIN_PASS 只在**首次建库**时生效 —— 本次改的是 .env，"
    echo "     实际密码没变。要真的改密码：登录管理台 → 改密码；"
    echo "     或删除 var/platform.db 后重启（会清空卡密/账号数据，慎用）。"
  fi
  echo
fi

echo "== 5/7 设备侧资源迁移 =="
# ★ 规则集原本住在旧管理台目录里。旧目录要删，就必须先把这份**活的配置**
#   搬到本服务自己的地盘 —— 否则 /ruleset.conf 会在删目录那一刻静默 404，
#   而这条是手机客户端要拉的，断了的表现是「客户端连不上」，
#   从管理台这边完全看不出跟它有关。
#   幂等：新位置已存在就**不动**（不覆盖你在新位置改过的规则）。
OLD_RS=/opt/charles-console/ruleset.conf
NEW_RS=$BASE/ruleset.conf
if [ ! -f "$NEW_RS" ] && [ -f "$OLD_RS" ]; then
  sudo install -o ubuntu -g ubuntu -m 644 "$OLD_RS" "$NEW_RS"
  echo "   规则集已迁移：$OLD_RS -> $NEW_RS"
elif [ -f "$NEW_RS" ]; then
  echo "   规则集已在 $NEW_RS（保留不动，$(sudo stat -c %s "$NEW_RS") 字节）"
else
  echo "   ! 规则集不存在（$NEW_RS 与 $OLD_RS 都没有）—— /ruleset.conf 会 404"
fi
# .env 里还指着旧目录就改过来（只改这一行，别的不动）。
if sudo grep -qE '^REWRITE_RULESET=/opt/charles-console/' "$BASE/rewrite.env" 2>/dev/null; then
  sudo sed -i "s|^REWRITE_RULESET=.*|REWRITE_RULESET=$NEW_RS|" "$BASE/rewrite.env"
  echo "   rewrite.env 的 REWRITE_RULESET 已指向 $NEW_RS"
fi

echo "== 6/7 属主 + 语法 =="
sudo chown -R ubuntu:ubuntu "$BASE"
sudo chmod 600 "$BASE/rewrite.env"        # 里面有密钥
PYTHONPYCACHEPREFIX=/tmp/pyc_deploy "$VENV/bin/python" -m compileall -q "$BASE/backend" >/dev/null
echo "   compileall ok"

echo "== 7/7 单元 + 启动 =="
sudo cp "$SRC_DIR/deploy/rewrite.service" /etc/systemd/system/rewrite.service
sudo systemctl daemon-reload
sudo systemctl enable rewrite.service >/dev/null 2>&1 || true
sudo systemctl restart rewrite.service
sleep 3
systemctl is-active rewrite.service

PORT=$(grep -E '^REWRITE_PORT=' "$BASE/rewrite.env" | cut -d= -f2)
echo "-- 探活 127.0.0.1:$PORT --"
curl -s --noproxy '*' "http://127.0.0.1:$PORT/healthz" || true
echo

if [ "$SMOKE" = "1" ]; then
  echo "-- 冒烟 --"
  cd "$BASE/backend"
  SMOKE_BASE="http://127.0.0.1:$PORT" \
  SMOKE_USER=$(grep -E '^REWRITE_ADMIN_USER=' "$BASE/rewrite.env" | cut -d= -f2) \
  SMOKE_PWD=$(grep -E '^REWRITE_ADMIN_PASS=' "$BASE/rewrite.env" | cut -d= -f2) \
    "$VENV/bin/python" _smoke.py | tail -6
fi

echo
echo "部署完成。当前跑在端口 $PORT（暂存）。"

if [ "$SWITCH" = "1" ]; then
  echo
  echo "== 附带执行切换（--switch）=="
  bash "$SRC_DIR/deploy/switchover.sh"
else
  echo "确认无误后执行：bash switchover.sh    （或下次加 --switch 一步到位）"
fi

if [ "$PURGE_OLD" = "1" ]; then
  echo
  echo "== 清理旧管理台（charles-console）=="
  # ★ 删之前必须过三关，任何一关不过就中止 —— 删掉之后没有回头路：
  #   ① 新栈已经跑在 8890 上（否则删完 8890 就没人了）
  #   ② /ruleset.conf 能拉到（这份文件只在旧目录里有一份原始拷贝）
  #   ③ 旧单元确实存在（不存在就是已经删过了，别重复备份）
  PORT_NOW=$(grep -E '^REWRITE_PORT=' "$BASE/rewrite.env" | cut -d= -f2)
  if [ "$PORT_NOW" != "8890" ]; then
    echo "   ! 新栈还在 $PORT_NOW 上。先 --switch 再删旧目录（否则 8890 会空掉）。"
    exit 1
  fi
  if ! curl -sf --noproxy '*' -o /dev/null "http://127.0.0.1:8890/ruleset.conf"; then
    echo "   ! 8890 的 /ruleset.conf 拉不到。先修好再删 —— 删了旧目录就没这份文件了。"
    exit 1
  fi

  STAMP=$(date +%Y%m%d-%H%M%S)
  ARCH=/opt/_archive
  sudo mkdir -p "$ARCH"
  # 备份**整个目录**（含 users.json / integrations.json / avatars / backups），
  # 再备份 unit 文件。只删不备 = 不可逆。
  sudo tar czf "$ARCH/charles-console_$STAMP.tar.gz" -C /opt charles-console
  sudo cp /etc/systemd/system/charles-console.service \
          "$ARCH/charles-console.service_$STAMP" 2>/dev/null || true
  echo "   备份：$ARCH/charles-console_$STAMP.tar.gz（$(sudo stat -c %s "$ARCH/charles-console_$STAMP.tar.gz") 字节）"

  sudo systemctl stop charles-console.service 2>/dev/null || true
  sudo systemctl disable charles-console.service >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/charles-console.service
  sudo systemctl daemon-reload
  sudo rm -rf /opt/charles-console
  echo "   已删除 /opt/charles-console/ 与 charles-console.service"

  # 删完立刻复核：新栈还在，规则集还拉得到。
  systemctl is-active rewrite.service
  printf '   复核 /ruleset.conf -> HTTP %s\n' \
    "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8890/ruleset.conf)"
  printf '   复核 /ca.crt       -> HTTP %s\n' \
    "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8890/ca.crt)"
  echo "   回滚：sudo tar xzf $ARCH/charles-console_$STAMP.tar.gz -C /opt \\"
  echo "         && sudo cp $ARCH/charles-console.service_$STAMP /etc/systemd/system/charles-console.service"
  echo "         && sudo systemctl daemon-reload && sudo systemctl start charles-console"
fi
