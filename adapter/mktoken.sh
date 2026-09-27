#!/usr/bin/env bash
# 造一个一花后台管理会话 cookie（sub_admin_token）。
#
# 为什么要有这个包装：_ccpx_mktoken.php 必须和 includes/ 同级才能 require，
# 也就是必须临时进 webroot。裸放一个「免鉴权就吐 token」的 PHP 在 webroot 上
# 等于开后门，所以固定流程是：拷进去 -> 跑 -> 立刻删。
#
# 用法: sudo /opt/ccpx/tools/mktoken.sh <管理员用户名> <管理员密码>
set -euo pipefail

CONTAINER="${CCPX_APP_CONTAINER:-yihua-app}"
WEBROOT="${CCPX_WEBROOT:-/var/www/html}"
TOOL="$(dirname "$0")/_ccpx_mktoken.php"
NAME="_mktoken_$$.php"

[ $# -eq 2 ] || { echo "用法: $0 <用户名> <密码>" >&2; exit 2; }

cleanup() { docker exec "$CONTAINER" rm -f "$WEBROOT/$NAME" 2>/dev/null || true; }
trap cleanup EXIT

docker cp "$TOOL" "$CONTAINER:$WEBROOT/$NAME"
docker exec "$CONTAINER" php "$WEBROOT/$NAME" "$1" "$2"
echo
