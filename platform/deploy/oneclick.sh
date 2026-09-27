#!/usr/bin/env bash
# 完整包一键部署 —— 在服务器上执行，从**一个 tgz** 到服务跑起来。
#
# 用法（服务器上，ubuntu 用户）：
#     bash oneclick.sh /tmp/rewrite-full-20260927.tgz
#     bash oneclick.sh /tmp/rewrite-full-20260927.tgz --fresh
#     bash oneclick.sh /tmp/rewrite-full-20260927.tgz --fresh --switch
#     bash oneclick.sh /tmp/rewrite-full-20260927.tgz --purge-old
#
# 完整包内容（由 deploy/oneclick_deploy.py 打包）：
#     backend/            后端源码（含 requirements.txt、_smoke.py）
#     frontend/dist/      前端构建产物
#     deploy/             部署脚本 + systemd 单元 + env 模板
#     VERSION             打包时间与来源，便于事后对账
#
# 为什么单独有这一层
# ------------------
# `deploy.sh` 假设源码已经在 `$SRC_DIR`（默认 /tmp/rewrite_src）。
# 这个脚本负责「把 tgz 解开到那个位置并校验结构」，
# 让「拿到包 → 一条命令 → 服务起来」成立，而不用手工解包。
#
# ★ 校验结构是必须的：一个缺了 backend/app 的包如果直接进 deploy.sh，
#   表现是 rsync 把线上代码删空（--delete）+ 服务起不来。
#   先验后动，失败时线上什么都没变。
set -euo pipefail

TGZ=${1:-}
shift || true

FRESH=0
SWITCH=0
PURGE=0
for a in "$@"; do
  case "$a" in
    --fresh)     FRESH=1 ;;
    --switch)    SWITCH=1 ;;
    --purge-old) PURGE=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

if [ -z "$TGZ" ]; then
  echo "用法：bash oneclick.sh <完整包.tgz> [--fresh] [--switch] [--purge-old]"
  exit 2
fi
if [ ! -f "$TGZ" ]; then
  echo "找不到包：$TGZ"
  exit 2
fi

SRC_DIR=${SRC_DIR:-/tmp/rewrite_src}
STAGE=$(mktemp -d /tmp/rewrite_stage.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT

echo "== 1/4 解包到暂存区 =="
tar xzf "$TGZ" -C "$STAGE"
echo "   $(du -sh "$STAGE" | cut -f1) 已解开 → $STAGE"
[ -f "$STAGE/VERSION" ] && sed 's/^/   /' "$STAGE/VERSION"

echo "== 2/4 校验包结构（验不过就中止，线上不动）=="
fail=0
need_dir=(backend backend/app backend/app/routers deploy)
need_file=(backend/requirements.txt backend/_smoke.py \
           deploy/deploy.sh deploy/rewrite.service deploy/rewrite.env.example)
for d in "${need_dir[@]}"; do
  [ -d "$STAGE/$d" ] || { echo "   缺目录：$d"; fail=1; }
done
for f in "${need_file[@]}"; do
  [ -f "$STAGE/$f" ] || { echo "   缺文件：$f"; fail=1; }
done
# 前端 dist 可以缺（后端会如实回 404），但要说清楚，不能静默。
if [ -f "$STAGE/frontend/dist/index.html" ]; then
  echo "   前端 dist：有（index.html 存在）"
else
  echo "   ! 前端 dist：缺 —— 部署后管理台页面会 404，后端 API 正常"
fi
if [ "$fail" = "1" ]; then
  echo "包结构不完整，已中止。线上未做任何改动。"
  exit 1
fi
echo "   结构 OK"

# ★ 行尾兜底：CRLF 的 .sh 在 Linux 上会报 `set: pipefail: invalid option name`，
#   看着像语法错，其实是行尾。打包时已归一化（oneclick_deploy.py 的 _add_tree），
#   这里只是第二道 —— 手工塞进包的脚本也拦得住。
#
# ★ 别写成 `CRLF_HIT=$(find ... | wc -l)`：本脚本开头是 `set -euo pipefail`，
#   而 `grep -l` 无匹配时返回 1，`find -exec +` 会把那个非零带出来，
#   pipefail 取「最后一个非零退出码」→ 整个赋值语句失败 → `set -e` 直接退出。
#   表现是「校验通过之后什么都没发生就退出 1」，非常难看出跟行尾检查有关。
#   用 while-read 循环，退出码只来自 find 且被 `< <(...)` 吞掉。
CRLF_HIT=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if grep -qU $'\r' "$f" 2>/dev/null; then
    CRLF_HIT=$((CRLF_HIT + 1))
  fi
done < <(find "$STAGE/deploy" -name '*.sh' 2>/dev/null)
if [ "$CRLF_HIT" -gt 0 ]; then
  echo "   ! 发现 $CRLF_HIT 个 CRLF 行尾的 .sh —— 就地归一化为 LF"
  find "$STAGE/deploy" -name '*.sh' -exec sed -i 's/\r$//' {} +
fi

echo "== 3/4 落到 $SRC_DIR =="
# 先清空再落位：避免上一次部署残留的文件混进这次（尤其是已删除的模块）。
sudo rm -rf "$SRC_DIR"
sudo mkdir -p "$SRC_DIR"
sudo cp -a "$STAGE/." "$SRC_DIR/"
sudo chown -R ubuntu:ubuntu "$SRC_DIR"
echo "   已落位（旧内容已清空，不会混入上次残留）"

echo "== 4/4 交给 deploy.sh =="
args=()
[ "$FRESH" = "1" ]  && args+=(--fresh)
[ "$SWITCH" = "1" ] && args+=(--switch)
[ "$PURGE" = "1" ]  && args+=(--purge-old)
# ★ 不能写 `"${args[@]:-}"`：数组为空时它会产生**一个空字符串参数**，
#   传到 deploy.sh 里就变成「未知参数：（空）」直接退出 2。
#   这个坑只有真跑一次才会现形 —— 因为 --fresh/--switch 都带上时数组非空，
#   反而不会触发。用 `+"${args[@]}"` 展开：空数组时整个参数消失。
SRC_DIR="$SRC_DIR" bash "$SRC_DIR/deploy/deploy.sh" ${args[@]+"${args[@]}"}

echo
echo "一键部署结束。"
if [ "$SWITCH" != "1" ] && [ "$PURGE" != "1" ]; then
  echo "还没切到 8890。确认后：bash $SRC_DIR/deploy/switchover.sh"
fi
