#!/usr/bin/env bash
# 黑盒演示/校验脚本：正常裁剪、读取中换版、不支持 Range、弱/无 ETag、
# 窗口约束、并发同 URL 不串版。
#
# 环境变量：
#   API_BASE      取数 API 地址（默认 http://localhost:8000）
#   SOURCE_BASE   影像源地址（宿主机可达，默认 http://localhost:8001）
#   SRC_INTERNAL  取数 API 容器/进程访问影像源用的地址（默认同 SOURCE_BASE）
#   VERIFY_MODE   docker | local（docker 模式在 api 容器内校验 GeoTIFF）
#   PYTHON        local 模式校验用的 Python（默认 python3）
set -uo pipefail

API_BASE=${API_BASE:-http://localhost:8000}
SOURCE_BASE=${SOURCE_BASE:-http://localhost:8001}
SRC_INTERNAL=${SRC_INTERNAL:-$SOURCE_BASE}
VERIFY_MODE=${VERIFY_MODE:-local}
PYTHON=${PYTHON:-python3}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

FAILED=0
say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }

clip() { # clip <url> <col> <row> <w> <h> <outfile> -> http code
  curl -sS -o "$6" -w '%{http_code}' -X POST "$API_BASE/clip" \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"$1\",\"col_off\":$2,\"row_off\":$3,\"width\":$4,\"height\":$5}"
}

verify_tif() { # verify_tif <file> <col> <row> <w> <h>
  local f=$1; shift
  if [ "$VERIFY_MODE" = docker ]; then
    docker compose cp "$f" api:/tmp/_verify.tif >/dev/null 2>&1
    docker compose exec -T api python /app/verify_clip.py /tmp/_verify.tif "$@"
  else
    "$PYTHON" "$SCRIPT_DIR/../app/verify_clip.py" "$f" "$@"
  fi
}

expect_error() { # expect_error <描述> <期望码> <实际码> <body 文件>
  if [ "$3" = "$2" ] && head -c1 "$4" | grep -q '{'; then
    ok "$1（HTTP $3: $(head -c 120 "$4" | tr -d '\n')…）"
  else
    bad "$1（期望 HTTP $2 + JSON 错误，实际 HTTP $3）"
  fi
}

# --------------------------------------------------------------------- #
say "场景 1：正常裁剪 512×512（窗口 300,200）"
code=$(clip "$SRC_INTERNAL/images/demoland.tif" 300 200 512 512 "$WORK/normal.tif")
if [ "$code" = 200 ] && verify_tif "$WORK/normal.tif" 300 200 512 512; then
  ok "正常裁剪（HTTP 200，GeoTIFF 双波段/坐标系/NoData/定位/像元全部正确）"
else
  bad "正常裁剪（HTTP $code）"
fi

say "场景 2：覆盖 NoData 空洞的窗口（0,0 起 64×64）"
code=$(clip "$SRC_INTERNAL/images/demoland.tif" 0 0 64 64 "$WORK/nodata.tif")
if [ "$code" = 200 ] && verify_tif "$WORK/nodata.tif" 0 0 64 64; then
  ok "NoData 空洞逐像元保留"
else
  bad "NoData 窗口（HTTP $code）"
fi

say "场景 3：读取中换版（flaky 源）"
curl -sS -X POST "$SOURCE_BASE/flaky/reset" >/dev/null
code=$(clip "$SRC_INTERNAL/flaky/image.tif" 300 200 512 512 "$WORK/flaky.out")
expect_error "读取中换版明确失败" 409 "$code" "$WORK/flaky.out"

say "场景 4：源不支持 Range"
code=$(clip "$SRC_INTERNAL/norange/image.tif" 0 0 64 64 "$WORK/norange.out")
expect_error "不支持 Range 明确失败" 502 "$code" "$WORK/norange.out"

say "场景 5：弱 ETag / 无 ETag"
code=$(clip "$SRC_INTERNAL/weak-etag/image.tif" 0 0 64 64 "$WORK/weak.out")
expect_error "弱 ETag 明确失败" 502 "$code" "$WORK/weak.out"
code=$(clip "$SRC_INTERNAL/no-etag/image.tif" 0 0 64 64 "$WORK/noetag.out")
expect_error "无 ETag 明确失败" 502 "$code" "$WORK/noetag.out"

say "场景 6：窗口约束"
code=$(clip "$SRC_INTERNAL/images/demoland.tif" 0 0 513 8 "$WORK/w513.out")
expect_error "窗口超过 512 被拒绝" 422 "$code" "$WORK/w513.out"
code=$(clip "$SRC_INTERNAL/images/demoland.tif" 2000 0 64 64 "$WORK/oob.out")
expect_error "窗口越界被拒绝" 422 "$code" "$WORK/oob.out"

say "场景 7：并发读取同一 URL（换版进行中）不串版"
curl -sS -X POST "$SOURCE_BASE/flaky/reset" >/dev/null
pids=()
for i in $(seq 1 6); do
  clip "$SRC_INTERNAL/flaky/image.tif" 300 200 512 512 "$WORK/par_$i.out" >"$WORK/par_$i.code" &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done
n409=0; n200=0; mix=0
for i in $(seq 1 6); do
  c=$(cat "$WORK/par_$i.code")
  if [ "$c" = 409 ]; then
    n409=$((n409 + 1))
  elif [ "$c" = 200 ]; then
    n200=$((n200 + 1))
    if ! verify_tif "$WORK/par_$i.out" 300 200 512 512 >/dev/null; then
      mix=1; bad "并发结果 #$i 像元与任何版本都不一致（疑似串版）"
    fi
  else
    mix=1; bad "并发请求 #$i 返回意外状态 $c"
  fi
done
if [ "$mix" = 0 ] && [ "$n409" -ge 1 ] && [ "$n200" -ge 1 ]; then
  ok "并发 $((n409 + n200)) 个请求：$n409 个明确 409，$n200 个完整一致，无串版"
elif [ "$mix" = 0 ]; then
  ok "并发请求全部一致处理（409×$n409, 200×$n200），无串版"
fi

echo
if [ "$FAILED" = 0 ]; then
  echo "全部演示场景通过 ✅"
else
  echo "存在失败场景 ❌"
fi
exit $FAILED
