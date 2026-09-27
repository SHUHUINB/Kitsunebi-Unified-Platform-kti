#!/usr/bin/env bash
# kit端口统一平台-soun —— 全链路一键部署
#
# 从一台干净的 Ubuntu 起，把**四套东西**装齐并逐个验证：
#
#   1. 花瓶  Charles Proxy（headless）+ 根证书 + 注册授权
#   2. 适配层 CCProxy（ccpx：HTTP 8888 / SOCKS5 8889 / 管理 8893 / UDP 中继）
#   3. 卡密  一花 ccproxy 卡密系统（docker：yihua-app + yihua-db，127.0.0.1:8081）
#   4. 平台  rewrite 统一后端 + 前端（8890，含 /mcp）
#
# ── 设计原则 ────────────────────────────────────────────────────────────
# ★ 幂等，而且是**先探测再动手**：每一步都问「它现在是什么状态」，
#   已经在跑就只做验证，绝不覆盖线上已有的配置（尤其是 rewrite.env
#   和 ~/.charles.config —— 那里面是用户的真实设置）。
# ★ 单点失败不拖垮全局：某套系统装不上，记一笔继续装下一套，
#   最后统一出状态表。一次跑完能看到全部问题，不用改一个跑一次。
# ★ 不撒谎：没做的写「未执行」，做失败的写真实错误，不打印成功文案。
#
# ── 用法 ────────────────────────────────────────────────────────────────
#   bash fullchain.sh --plan                  # 只打印将做什么，不动手
#   bash fullchain.sh                         # 装齐 + 验证
#   bash fullchain.sh --charles-tgz /tmp/charles-proxy-4.5.6.tar.gz
#   bash fullchain.sh --license-name X --license-key Y
#   bash fullchain.sh --public-host 1.2.3.4   # 对外 IP（UDP 中继 / 门户 / 证书用）
#   bash fullchain.sh --skip charles,yihua    # 跳过某几套（只重装平台时用）
#   bash fullchain.sh --only rewrite          # 只跑某几套
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
VENDOR=$HERE/vendor
SRC_DIR=${SRC_DIR:-/tmp/rewrite_src}

# ---- 参数 ----------------------------------------------------------------
PLAN=0
CHARLES_TGZ=""
LIC_NAME=""
LIC_KEY=""
PUBLIC_HOST=""
SKIP=""
ONLY=""
DBPASS=${YIHUA_DBPASS:-MySQL-Root-ChangeMe}
DBNAME=${YIHUA_DBNAME:-ccpy}

while [ $# -gt 0 ]; do
  case "$1" in
    --plan)          PLAN=1 ;;
    --charles-tgz)   CHARLES_TGZ=${2:-}; shift ;;
    --license-name)  LIC_NAME=${2:-}; shift ;;
    --license-key)   LIC_KEY=${2:-}; shift ;;
    --public-host)   PUBLIC_HOST=${2:-}; shift ;;
    --skip)          SKIP=${2:-}; shift ;;
    --only)          ONLY=${2:-}; shift ;;
    -h|--help)       sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

# ---- 输出 ----------------------------------------------------------------
RESULT=()          # "系统|状态|说明"
FAILS=0
n=0

step()  { n=$((n+1)); echo; echo "── $n. $* ─────────────────────────────────"; }
say()   { echo "   $*"; }
ok()    { echo "   [OK]   $*"; }
bad()   { echo "   [FAIL] $*"; FAILS=$((FAILS+1)); }
warn()  { echo "   [WARN] $*"; }
note()  { echo "   [--]   $*"; }
plan()  { echo "   [PLAN] $*"; }
record(){ RESULT+=("$1|$2|$3"); }

run() {   # 动手前先看 PLAN
  if [ "$PLAN" = "1" ]; then plan "$*"; return 0; fi
  eval "$@"
}

# 该跑这一套吗
wanted() {
  local name=$1
  case ",$SKIP," in *",$name,"*) return 1 ;; esac
  if [ -n "$ONLY" ]; then
    case ",$ONLY," in *",$name,"*) return 0 ;; *) return 1 ;; esac
  fi
  return 0
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

wait_port() {   # wait_port <port> <秒数>
  local p=$1 t=${2:-30} i=0
  while [ "$i" -lt "$t" ]; do
    if ss -lnt 2>/dev/null | grep -q ":$p "; then return 0; fi
    i=$((i+1)); sleep 1
  done
  return 1
}

svc_state() { systemctl is-active "$1" 2>/dev/null || echo inactive; }

echo "════════════════════════════════════════════════════════════"
echo " kit端口统一平台-soun —— 全链路部署"
[ "$PLAN" = "1" ] && echo " 模式：PLAN（只看不动）"
echo " 源码目录：$SRC_DIR"
echo " vendor  ：$VENDOR"
echo "════════════════════════════════════════════════════════════"

# ═══════════════════════════════════════════════════════════════
# 0. 前置自检
# ═══════════════════════════════════════════════════════════════
step "前置自检"
if [ "$(id -u)" = "0" ]; then
  warn "当前是 root。建议用 ubuntu 用户跑 —— 单元里的 User= 是按 ubuntu 写的。"
fi
if ! sudo -n true 2>/dev/null; then
  bad "sudo 需要密码（本脚本全程非交互）。请先配置免密 sudo 或手工跑。"
  echo; echo "前置条件不满足，终止。"; exit 1
fi
ok "sudo 免密可用"
say "OS：$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo 未知)"
[ -d "$VENDOR" ] && ok "vendor 存在：$(ls "$VENDOR")" \
                 || warn "vendor 不存在 —— Charles/适配层/一花的源码要从别处拿"

# 对外 IP：UDP 中继、门户链接、证书引导都要用。
if [ -z "$PUBLIC_HOST" ]; then
  PUBLIC_HOST=$(curl -s --max-time 5 --noproxy '*' ifconfig.me 2>/dev/null || true)
  [ -n "$PUBLIC_HOST" ] && note "自动探测到对外 IP：$PUBLIC_HOST" \
                        || warn "探测不到对外 IP，且没给 --public-host"
fi

# ═══════════════════════════════════════════════════════════════
# 1. 系统依赖
# ═══════════════════════════════════════════════════════════════
step "系统依赖"
PKGS=()
need_cmd python3  || PKGS+=(python3 python3-venv python3-pip)
need_cmd tcpdump  || PKGS+=(tcpdump)
need_cmd openssl  || PKGS+=(openssl)
need_cmd curl     || PKGS+=(curl)
need_cmd docker   || PKGS+=(docker.io docker-compose-v2)
if [ ${#PKGS[@]} -gt 0 ]; then
  say "缺：${PKGS[*]}"
  run "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ${PKGS[*]}"
else
  ok "系统命令齐全（python3 / tcpdump / openssl / curl / docker）"
fi

# ═══════════════════════════════════════════════════════════════
# 2. 花瓶：Charles Proxy + 根证书 + 授权
# ═══════════════════════════════════════════════════════════════
if wanted charles; then
  step "系统 1/4 —— 花瓶 Charles Proxy"
  CH=/opt/charles-proxy

  if [ -x "$CH/bin/charles" ]; then
    ok "Charles 已安装：$CH（$(du -sh "$CH" 2>/dev/null | cut -f1)）"
  else
    # 找安装包：命令行 > vendor 目录
    TGZ=$CHARLES_TGZ
    if [ -z "$TGZ" ] && [ -d "$VENDOR/charles" ]; then
      TGZ=$(ls "$VENDOR/charles"/charles*.tar.gz 2>/dev/null | head -1 || true)
    fi
    if [ -z "$TGZ" ]; then
      bad "Charles 未安装，且没有安装包。用 --charles-tgz <路径> 指定，"
      bad "  或把 charles-proxy-*.tar.gz 放进 $VENDOR/charles/。"
      record "花瓶 Charles" "FAIL" "缺安装包"
      TGZ=""
    elif [ ! -f "$TGZ" ]; then
      bad "安装包不存在：$TGZ"
      record "花瓶 Charles" "FAIL" "安装包路径无效"
      TGZ=""
    else
      say "安装包：$TGZ（$(du -h "$TGZ" | cut -f1)）"
      # 解包到临时目录再整体搬 —— 直接解到 /opt 会留下半截目录
      run "rm -rf /tmp/_charles_x && mkdir -p /tmp/_charles_x && \
           tar xzf '$TGZ' -C /tmp/_charles_x && \
           D=\$(ls -d /tmp/_charles_x/*/ | head -1) && \
           sudo rm -rf $CH && sudo mv \"\$D\" $CH && sudo chown -R root:root $CH"
      run "sudo chmod +x $CH/bin/charles"
      if [ "$PLAN" = "0" ] && [ -x "$CH/bin/charles" ]; then
        ok "已解包到 $CH"
      fi
    fi
  fi

  # ---- 单元 ----
  if [ -f "$VENDOR/charles/charles-headless.service" ]; then
    run "sudo install -m 644 '$VENDOR/charles/charles-headless.service' \
         /etc/systemd/system/charles-headless.service"
    run "sudo systemctl daemon-reload"
    ok "单元已落位：charles-headless.service"
  else
    warn "vendor/charles/charles-headless.service 不在，跳过单元安装"
  fi

  CFG=$HOME/.charles.config
  if [ ! -f "$CFG" ]; then
    # 首次：Charles 第一次启动会生成默认配置，退出时（关闭钩子）才写盘。
    say "配置不存在，先起一次让 Charles 自己生成 ~/.charles.config"
    run "sudo systemctl enable --now charles-headless.service >/dev/null 2>&1 || true"
    run "sleep 12"
    run "sudo systemctl stop charles-headless.service || true"
    run "sleep 3"
    if [ "$PLAN" = "0" ] && [ ! -f "$CFG" ]; then
      bad "Charles 起来又停了，但 $CFG 仍未生成 —— 看 journalctl -u charles-headless"
    fi
  fi

  # ---- 授权注入 ----
  if [ -n "$LIC_NAME" ] && [ -n "$LIC_KEY" ]; then
    say "注入授权：$LIC_NAME"
    run "sudo systemctl stop charles-headless.service 2>/dev/null || true; sleep 2"
    if [ "$PLAN" = "0" ]; then
      sudo python3 - "$CFG" "$LIC_NAME" "$LIC_KEY" <<'PY'
import re, sys, shutil, os
cfg, name, key = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(cfg, encoding='utf-8').read()
shutil.copy2(cfg, cfg + '.bak_license')
block = ('<registrationConfiguration>\n'
         '    <name>%s</name>\n'
         '    <key>%s</key>\n'
         '  </registrationConfiguration>' % (name, key))
rx = re.compile(r'[ \t]*<registrationConfiguration>.*?</registrationConfiguration>', re.S)
if rx.search(s):
    s = rx.sub(lambda m: '  ' + block, s, count=1)
else:
    m = re.search(r'<configuration[^>]*>', s)
    if not m:
        sys.exit('配置里找不到 <configuration>')
    s = s[:m.end()] + '\n  ' + block + s[m.end():]
open(cfg, 'w', encoding='utf-8').write(s)
print('   授权已写入（原文件备份为 %s.bak_license）' % cfg)
PY
    fi
  else
    note "未提供 --license-name/--license-key，只读取现有授权状态"
  fi

  # ---- 起服务 + 验证 ----
  run "sudo systemctl enable --now charles-headless.service >/dev/null 2>&1 || sudo systemctl start charles-headless.service"
  if [ "$PLAN" = "0" ]; then
    if wait_port 18888 40; then
      ok "Charles 已监听 18888（HTTP 代理）+ 18889（SOCKS5）"
      LIC=$(sudo python3 -c "
import re,sys
try: s=open('$CFG',encoding='utf-8').read()
except Exception as e: print('配置读不到: %s'%e); raise SystemExit
m=re.search(r'<registrationConfiguration>.*?</registrationConfiguration>', s, re.S)
if not m: print('未注册（无 registrationConfiguration）'); raise SystemExit
n=re.search(r'<name>([^<]*)</name>', m.group(0)); k=re.search(r'<key>([^<]*)</key>', m.group(0))
print('已注册：%s / %s' % (n.group(1) if n else '?', (k.group(1)[:6]+'…'+k.group(1)[-4:]) if k else '?'))
" 2>/dev/null || echo '状态未知')
      say "授权：$LIC"
      if ls "$HOME/.charles/ca/"*.cer >/dev/null 2>&1; then
        ok "根证书已生成：$(ls "$HOME/.charles/ca/"*.cer | head -1)"
        record "花瓶 Charles" "OK" "18888/18889 在跑，证书已就绪"
      else
        warn "根证书还没生成（$HOME/.charles/ca/ 为空）"
        record "花瓶 Charles" "WARN" "服务在跑但证书缺失"
      fi
    else
      bad "18888 未监听。看：sudo journalctl -u charles-headless -n 40"
      record "花瓶 Charles" "FAIL" "端口未监听"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════
# 3. 适配层：CCProxy
# ═══════════════════════════════════════════════════════════════
if wanted ccpx; then
  step "系统 2/4 —— CCProxy 适配层"
  CC=/opt/ccpx

  id ccpx >/dev/null 2>&1 || run "sudo useradd -r -s /usr/sbin/nologin -d $CC ccpx"
  run "sudo install -d -o ccpx -g ccpx -m 750 $CC $CC/capture $CC/exports"

  if [ -f "$VENDOR/ccpx/ccproxy_adapter.py" ]; then
    run "sudo install -o ccpx -g ccpx -m 755 '$VENDOR/ccpx/ccproxy_adapter.py' $CC/ccproxy_adapter.py"
    ok "适配层已落位（单文件，纯标准库，无第三方依赖）"
  else
    warn "vendor/ccpx/ccproxy_adapter.py 不在 —— 若线上已有则不影响"
  fi

  # ---- config.json：已有就保留（里面是用户的真实配置）----
  # ★ 判定必须走 `sudo test`，不能裸 `[ -f ]`。
  #   `/opt/ccpx` 是 `ccpx:ccpx 750`，ubuntu **连目录都遍历不进去**，
  #   裸判定恒为假 —— 于是脚本认为「没有配置」，走 else 分支拿一份
  #   全新 config.json 覆盖上去：admin_password 被随机化、用户的真实
  #   设置全丢，而平台里存的 8893 凭据当场失效。实测复现过：
  #     test  -f /opt/ccpx/config.json → 不可见
  #     sudo test -f /opt/ccpx/config.json → 可见
  #   这类「权限导致的存在性误判」不报错、只静默毁配置，最难查。
  if sudo test -f "$CC/config.json"; then
    ok "config.json 已存在，保留不动"
    if [ "$PLAN" = "0" ]; then
      # 只补 udp_public_host —— 它是唯一跟「换机器」强相关的字段
      if [ -n "$PUBLIC_HOST" ]; then
        sudo python3 - "$CC/config.json" "$PUBLIC_HOST" <<'PY'
import json, sys
p, host = sys.argv[1], sys.argv[2]
d = json.load(open(p, encoding='utf-8'))
if d.get('udp_public_host') != host:
    d['udp_public_host'] = host
    open(p, 'w', encoding='utf-8').write(json.dumps(d, indent=4, ensure_ascii=False) + '\n')
    print('   udp_public_host 已更新为 %s' % host)
else:
    print('   udp_public_host 已是 %s' % host)
PY
      fi
    fi
  else
    say "生成 config.json（对外 IP $PUBLIC_HOST）"
    if [ "$PLAN" = "0" ]; then
      sudo python3 - "$CC/config.json" "${PUBLIC_HOST:-127.0.0.1}" <<'PY'
import json, secrets, sys
p, host = sys.argv[1], sys.argv[2]
cfg = {
    "admin_user": "admin",
    "admin_password": "ccpx-8893-" + secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:12],
    "admin_port": 8893,
    "http_port": 8888,
    "socks_port": 8889,
    "bind_host": "0.0.0.0",
    "chain_to_charles": True,
    "charles_host": "127.0.0.1",
    "charles_http_port": 18888,
    "charles_socks_port": 18889,
    "charles_upstream": "socks5",
    "charles_bypass_ports": [],
    "fallback_direct": True,
    "require_proxy_auth": True,
    "connect_timeout": 15.0,
    "db_path": "/opt/ccpx/accounts.db",
    "log_path": "/opt/ccpx/adapter.log",
    "verify_hosts": ["m.baidu.com"],
    "verify_fail_tls": False,
    "udp_enabled": True,
    "udp_public_host": host,
    "udp_relay_port_base": 40000,
    "udp_relay_port_count": 64,
    "udp_idle_timeout": 120.0,
    "udp_relay_port": 8888,
}
open(p, 'w', encoding='utf-8').write(json.dumps(cfg, indent=4, ensure_ascii=False) + '\n')
print('   已生成，管理口令：%s' % cfg['admin_password'])
PY
      run "sudo chown ccpx:ccpx $CC/config.json && sudo chmod 600 $CC/config.json"
    fi
  fi

  for u in ccpx ccpx-capture; do
    if [ -f "$VENDOR/ccpx/$u.service" ]; then
      run "sudo install -m 644 '$VENDOR/ccpx/$u.service' /etc/systemd/system/$u.service"
    fi
  done
  run "sudo systemctl daemon-reload"
  run "sudo systemctl enable --now ccpx.service >/dev/null 2>&1 || sudo systemctl restart ccpx.service"
  # 抓包是旁路，起不来不影响代理 —— 单独 try，不记 FAIL
  run "sudo systemctl enable --now ccpx-capture.service >/dev/null 2>&1 || true"

  if [ "$PLAN" = "0" ]; then
    if wait_port 8893 25 && wait_port 8888 10; then
      ok "适配层在跑：8888 HTTP / 8889 SOCKS5 / 8893 管理"
      ST=$(curl -s --noproxy '*' --max-time 5 -u admin:"$(sudo python3 -c "import json;print(json.load(open('$CC/config.json'))['admin_password'])")" http://127.0.0.1:8893/status 2>/dev/null | head -c 200)
      say "管理接口：${ST:-（取不到）}"
      record "适配层 CCProxy" "OK" "8888/8889/8893 在跑"
    else
      bad "端口未起。看：sudo journalctl -u ccpx -n 40"
      record "适配层 CCProxy" "FAIL" "端口未监听"
    fi
    say "抓包：$(svc_state ccpx-capture)"
  fi
fi

# ═══════════════════════════════════════════════════════════════
# 4. 卡密：一花（docker）
# ═══════════════════════════════════════════════════════════════
if wanted yihua; then
  step "系统 3/4 —— 一花卡密（docker）"
  YH=/opt/yihua

  if sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -qx yihua-app; then
    ok "一花容器已在运行（yihua-app / yihua-db）"
    record "一花卡密" "OK" "容器在跑"
  else
    TGZ=""
    [ -f "$VENDOR/yihua/yihua-deploy.tar.gz" ] && TGZ=$VENDOR/yihua/yihua-deploy.tar.gz
    if [ -z "$TGZ" ] && [ -f "$YH/yihua-deploy.tar.gz" ]; then
      TGZ=$YH/yihua-deploy.tar.gz
    fi
    if [ -z "$TGZ" ]; then
      bad "没有一花部署包（vendor/yihua/yihua-deploy.tar.gz 或 $YH/yihua-deploy.tar.gz）"
      record "一花卡密" "FAIL" "缺部署包"
    else
      say "部署包：$TGZ"
      run "sudo mkdir -p $YH && sudo tar xzf '$TGZ' -C $YH && sudo chown -R ubuntu:ubuntu $YH"
      # 数据卷目录要先建好，否则 compose 会以 root 建，mysql 起不来
      run "sudo mkdir -p $YH/mysql-data"
      run "cd $YH && sudo docker compose up -d --build"
      say "等 db healthy（最多 180s）…"
      if [ "$PLAN" = "0" ]; then
        st=none
        for i in $(seq 1 60); do
          st=$(sudo docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' yihua-db 2>/dev/null || echo missing)
          [ "$st" = "healthy" ] && break
          sleep 3
        done
        if [ "$st" != "healthy" ]; then
          bad "yihua-db 未 healthy（当前 $st）"
          run "sudo docker logs --tail 30 yihua-db 2>&1 | tail -20"
          record "一花卡密" "FAIL" "db 未就绪"
        else
          ok "yihua-db healthy"
          say "等 Apache 应答…"
          code=000
          for i in $(seq 1 30); do
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
                   -H 'Cookie: sec_defend=1' http://127.0.0.1:8081/install/index.php || echo 000)
            [ "$code" != "000" ] && break
            sleep 2
          done
          if [ "$code" = "000" ]; then
            bad "yihua-app 无应答"
            record "一花卡密" "FAIL" "app 无应答"
          else
            if [ -f "$YH/app/install.lock" ]; then
              ok "已安装过（app/install.lock 存在），跳过安装器"
            else
              say "跑安装器（建库 + 写 config.php + 落 install.lock）"
              run "curl -s -X POST 'http://127.0.0.1:8081/install/ajax.php?act=1' \
                   -H 'Host: ${PUBLIC_HOST}:8081' -H 'Cookie: sec_defend=1' \
                   -H 'Content-Type: application/x-www-form-urlencoded' \
                   --data-urlencode 'host=db' --data-urlencode 'port=3306' \
                   --data-urlencode 'user=root' --data-urlencode 'pwd=$DBPASS' \
                   --data-urlencode 'dbname=$DBNAME' --data-urlencode 'state=1'"
            fi
            # siteurl 必须精确等于访问用的 Host，否则 includes/common.php 直接
            # sysmsg('您的站点没有绑定') exit；over_date 初始是过去时间，会全站拒绝。
            say "修正 siteurl / over_date"
            run "sudo docker exec yihua-db mysql -uroot -p'$DBPASS' $DBNAME -e \"
                 UPDATE sub_admin SET siteurl='${PUBLIC_HOST}:8081',
                        over_date='2036-01-01 00:00:00', state=1 WHERE username='admin';\" 2>/dev/null"
            H=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H 'Cookie: sec_defend=1' http://127.0.0.1:8081/ || echo 000)
            if [ "$H" = "200" ]; then
              ok "一花首页 200（127.0.0.1:8081）"
              record "一花卡密" "OK" "首页 200，已绑定 ${PUBLIC_HOST}:8081"
            else
              bad "一花首页 HTTP $H"
              record "一花卡密" "FAIL" "首页 HTTP $H"
            fi
          fi
        fi
      fi
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════
# 5. 平台：rewrite
# ═══════════════════════════════════════════════════════════════
if wanted rewrite; then
  step "系统 4/4 —— 统一平台 rewrite（8890）"
  if [ ! -f "$SRC_DIR/deploy/deploy.sh" ]; then
    bad "找不到 $SRC_DIR/deploy/deploy.sh —— 平台源码没落位"
    record "统一平台" "FAIL" "源码未落位"
  elif [ "$PLAN" = "1" ]; then
    plan "SRC_DIR=$SRC_DIR bash $SRC_DIR/deploy/deploy.sh"
    record "统一平台" "PLAN" "未执行"
  else
    SRC_DIR="$SRC_DIR" bash "$SRC_DIR/deploy/deploy.sh" 2>&1 | tail -25
    if systemctl is-active rewrite.service >/dev/null 2>&1; then
      P=$(sudo systemctl show rewrite.service -p MainPID --value)
      PORT=$(sudo tr '\0' ' ' < /proc/"$P"/cmdline 2>/dev/null | grep -oE '\-\-port [0-9]+' | awk '{print $2}')
      ok "rewrite 在跑（端口 ${PORT:-?}）"
      record "统一平台" "OK" "端口 ${PORT:-?}"
    else
      bad "rewrite.service 未 active"
      record "统一平台" "FAIL" "服务未起"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════
# 6. 汇总
# ═══════════════════════════════════════════════════════════════
echo
echo "════════════════════════════════════════════════════════════"
echo " 部署结果"
echo "════════════════════════════════════════════════════════════"
printf ' %-18s %-6s %s\n' "系统" "状态" "说明"
printf ' %-18s %-6s %s\n' "──────────────────" "──────" "──────────────────────────────"
for r in "${RESULT[@]:-}"; do
  IFS='|' read -r a b c <<< "$r"
  printf ' %-18s %-6s %s\n' "$a" "$b" "$c"
done

echo
echo "── 端口 ──"
sudo ss -lntp 2>/dev/null | grep -E ':(8890|8893|8888|8889|18888|18889|8081)\b' | awk '{print "   "$4"  "$6}' || true

echo
echo "── 入口 ──"
echo "   管理台    http://${PUBLIC_HOST:-<公网IP>}:8890/"
echo "   MCP       http://${PUBLIC_HOST:-<公网IP>}:8890/mcp"
echo "   证书引导  http://${PUBLIC_HOST:-<公网IP>}:8890/ca"
echo "   用户门户  http://${PUBLIC_HOST:-<公网IP>}:8890/portal"
# ★ 一花后台**只绑 127.0.0.1:8081**，外网打不开 —— 这是有意的（管理入口
#   统一收在 8890）。这里如实写「本机」，别印成公网 URL：印了会让人以为
#   外网能进，然后拿着一个打不开的链接来问为什么。
echo "   一花后台  http://127.0.0.1:8081/sub_admin/login.php（仅本机；外网请走 8890 的统一入口）"
echo "   代理      HTTP ${PUBLIC_HOST:-<公网IP>}:8888 ／ SOCKS5 ${PUBLIC_HOST:-<公网IP>}:8889"

echo
if [ "$PLAN" = "1" ]; then
  echo "PLAN 模式：什么都没动。去掉 --plan 再跑一次。"
  exit 0
fi
if [ "$FAILS" -gt 0 ]; then
  echo "有 $FAILS 项失败 —— 上面每条都写了真实错误和下一步命令。"
  exit 1
fi
echo "全链路就绪。"
