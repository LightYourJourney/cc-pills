#!/usr/bin/env bash
# 状态栏风格并排预览：用真实的 statusline-command.sh 渲染，须在 iTerm 等真彩终端里直接运行
# （Claude Code 的 `!` 前缀不渲染颜色）。
#   用法：bash tools/preview.sh [风格 ...]      默认 cool jewel glass tokyo-glass
#   环境：PREVIEW_COLS="160 100 70"（逐个宽度渲染）
#         CC_PILLS_SRC=<目录>（要渲染的脚本所在目录；默认本仓库的 src/，即未安装的开发版）
# 隔离：设 CLAUDE_SL_TMP 为临时目录，色板 /
# git / token / 状态 / 锁全部写在那里；token 数据直接写成假缓存（不扫真实 transcript，秒出结果，
# 场景数据确定）；Claude 状态也写在临时目录里——不影响任何正在使用的会话。结束（含 Ctrl-C）
# 时等后台刷新退出再整目录删除。此前借用共享的 /tmp/claude-sl-status.cache，播放告警场景时
# 所有会话都会看到伪造的「claude major」，被中断还会残留（2026-10-28 终审）。
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${CC_PILLS_SRC:-$HERE/../src}
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/claude-statusline.conf"
STYLES=("$@"); [ ${#STYLES[@]} -eq 0 ] && STYLES=(cool jewel glass tokyo-glass)
for st in "${STYLES[@]}"; do
  case "$st" in
    tint|cool|jewel|glass|mono|pastel|tokyo-glass) ;;
    *) echo "未知的 pill_style：$st" >&2; exit 2 ;;
  esac
done
COLS_LIST=${PREVIEW_COLS:-"160 100 70"}
work=$(mktemp -d) || { echo "无法创建临时目录" >&2; exit 1; }
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
trap 'exit 130' INT TERM
now=$(date +%s)

# 场景：名称|ctx%|ctx token|5h%|7d%|cache 剩余秒（cold = 已冷）|Claude 状态|假 token 缓存（空格分隔的 键=值，键见
# statusline-tokens.py 的 FIELDS）
SCENES=(
  "常态|35|350000|20|40|3000|none|in=118M out=736k rate=\$4.9/h comp=1 q5lv=ok q7lv=ok hit=98.2 msgs=517"
  "截图（5h 将提前用完、7d 88%）|18|175000|58|88|3500|none|in=118M out=736k rate=\$13/h comp=1 q5lv=warn q5out=2h00m q7lv=high q7out=16h21m hit=98.6 msgs=517"
  "告警（ctx 96%、5h 82%、7d 97%、cache 已冷、Claude major）|96|960000|82|97|cold|major|in=118M out=736k rate=\$24/h comp=1 q5lv=warn q5out=36m q7lv=bad q7out=3h42m hit=98.6 msgs=517"
)
TOK_V=$("$BASH" "$SRC/statusline-command.sh" --facts </dev/null | sed -n 's/^tok_v=//p')   # 缓存格式版本问渲染脚本
cwd_json=$(jq -n --arg dir "$PWD" '$dir') || exit 1
mk() {   # mk <sid> <ctx%> <ctx tok> <5h%> <7d%> <cache>
  local exp=$((now + ${6/cold/0})) warm=true
  [ "$6" = cold ] && { exp=$((now - 60)); warm=false; }
  printf '{"session_id":"%s","transcript_path":"","cwd":%s,"model":{"display_name":"Opus 5.5"},"effort":{"level":"high"},"cost":{"total_cost_usd":36.32,"total_api_duration_ms":3900000},"context_window":{"total_input_tokens":%s,"context_window_size":1000000,"used_percentage":%s},"prompt_cache":{"warm":%s,"caching_observed":true,"ttl":"1h","expires_at":%s,"recache_tokens_if_cold":%s},"rate_limits":{"five_hour":{"used_percentage":%s,"resets_at":%s},"seven_day":{"used_percentage":%s,"resets_at":%s}}}' \
    "$1" "$cwd_json" "$3" "$2" "$warm" "$exp" "$3" "$4" $((now + 7980)) "$5" $((now + 172800))
}

i=0
for sc in "${SCENES[@]}"; do
  IFS='|' read -r _ c ct q5 q7 cache _ tok <<< "$sc"
  mk "slpv-$i" "$c" "$ct" "$q5" "$q7" "$cache" > "$work/s$i.json"
  { printf 'TOK=%s\n' "$TOK_V"; printf '%s\n' $tok; } > "$work/claude-sl-tok-slpv-$i.cache"   # $tok 不加引号：按空格拆成每行一个 键=值
  i=$((i + 1))
done
# 每种风格一份配置（沿用用户配置、只换 pill_style，且开 status_page 展示告警场景）；
# 先写 .try 避免预热 --refresh 联网阻塞，再同步跑一次刷新生成色板与 git 缓存
for st in "${STYLES[@]}"; do
  mkdir -p "$work/$st"
  { grep -Ev '^[[:space:]]*(pill_style|status_page)' "$CONF" 2>/dev/null; printf '\npill_style = %s\nstatus_page = on\n' "$st"; } > "$work/$st/claude-statusline.conf"
  printf '%s\n' "$now" > "$work/$st/claude-sl-status.try"
  CLAUDE_SL_TMP="$work/$st" XDG_CONFIG_HOME="$work/$st" "$BASH" "$SRC/statusline-command.sh" --refresh "$PWD" - "slpv-init" sub
  for j in $(seq 0 $((i - 1))); do cp "$work/claude-sl-tok-slpv-$j.cache" "$work/$st/"; done
done

i=0
for sc in "${SCENES[@]}"; do
  IFS='|' read -r name _ _ _ _ _ status _ <<< "$sc"
  printf '\n\033[1m━━ 场景：%s\033[22m\n' "$name"
  for cols in $COLS_LIST; do
    printf '\033[2m  %s 列\033[22m\n' "$cols"
    for st in "${STYLES[@]}"; do
      printf '%s\t%s\n' "$now" "$status" > "$work/$st/claude-sl-status.cache"
      line=$(CLAUDE_SL_TMP="$work/$st" CLAUDE_SL_NO_REFRESH=1 XDG_CONFIG_HOME="$work/$st" COLUMNS=$cols "$BASH" "$SRC/statusline-command.sh" < "$work/s$i.json")
      printf '  %-12s %s\n' "$st" "$line"
    done
  done
  i=$((i + 1))
done
echo
