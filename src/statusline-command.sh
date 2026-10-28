#!/usr/bin/env bash
#
# Claude Code 状态栏脚本（缓存渲染 + 后台刷新）
# ============================================
#
# 渲染示例：
#   ccchat on  main [!?] Fable 5 max · ctx 68%·183k/1M · hit 98.7% · io 4.2M/840k · 2.1k/min · 5h 42%
#   └─青粗    └─品红粗 └─红粗 └─暗灰(含effort) └─按阈值变色     └─暗灰实时命中 └─暗灰(transcript增量) └─变色
#
# 架构（2026-08-19 方案 C 改造）
# ------------------------------
# 同步路径（每次刷新执行，目标 <30ms）：
#   stdin → 1 次 jq 提取全部字段 → 读两个缓存文件（git 段 / token 段）
#   → 按 $COLUMNS 自适应丢段 → 组装输出 → 脱钩后台刷新进程 → 退出。
# 后台刷新（--refresh 模式，本脚本自调用）：
#   git×3 与 statusline-tokens.py（transcript 增量聚合）在后台算，结果原子写入
#     /tmp/claude-sl-git-<dir>.cache   （repo_root \t branch \t 状态旗标）
#     /tmp/claude-sl-tok-<sid>.cache   （io 段 \t 烧速段，纯文本无色）
#   无锁设计：并发刷新幂等（同输入同输出 + mv 原子替换），重复只浪费不出错；
#   被 abort 杀掉的刷新进程由下一周期重试补上。
#   慢段数据滞后 ≤ 一个刷新周期（refreshInterval=2s）；cd 换目录后 git 段
#   首次同步回退计算，之后走缓存。/tmp 重启清空后自动重建（自愈）。
#
# 设计来源
# --------
# 镜像用户实际生效的 shell prompt，即 starship 默认预设：
#   - ~/.zshrc 中 Powerlevel10k 主题被后加载的 `starship init zsh` 覆盖
#   - 不存在 ~/.config/starship.toml，故以 starship 内置默认值为准
#   - .zshrc / .bashrc / .bash_profile / .profile 四个文件均无 PS1 定义
# 末尾的 `❯` 提示符有意省略：状态栏里的尾随提示符只是噪音。
#
# ⚠️ 估算值声明
# -------------
# ctx / 5h / hit / io / 烧速均为客户端估算值，不是账单权威值，权威用量只在
# 服务端账单。任一字段缺失时对应段【整段隐藏】，绝不显示推算值或占位数字。
#   - ctx 百分比与绝对值、hit（最近一次 API 调用的缓存命中率）读 stdin 的
#     .context_window（v2.1.235 实测；current_usage 在首次 API 响应前与
#     /compact 后为 null，对应段随之隐藏）。
#   - effort 段读顶层 .effort.level（仅支持 reasoning effort 的模型才有）。
#   - rate_limits 仅在 Claude.ai 订阅会话中存在；走第三方中转时 5h 段持续隐藏。
#   - io 累计与烧速（计费口径 tokens/min）来自 statusline-tokens.py 对
#     transcript 的增量聚合（断点续扫，追平前整段隐藏）。
#
# 显示自适应（$COLUMNS 由 v2.1.235 注入）
# ----------------------------------------
# 行宽超出时按优先级丢段：hit → 烧速 → ctx 绝对值 → 5h → io。
# COLUMNS 缺失或非数字时不做自适应（输出全量，由渲染端截断）。
#
# 工程约束（修改时必须保留）
# --------------------------
#   1. 所有 git 调用带 --no-optional-locks，避免高频刷新争抢 index 锁
#   2. jq 或任何依赖缺失时静默降级，绝不向状态栏输出错误信息
#   3. 末尾 `true` 保证退出码恒为 0（exit code ≠ 0 时输出会被整体丢弃）
#   4. 缓存写入必须 tmp + mv 原子替换（写一半被杀不能污染缓存）
#   5. 跨进程传字段用 tab 分隔 + 空字段占位符 "-"（IFS=tab 的 read 会吞
#      连续 tab，无占位符会导致字段错位）
#   6. 变量展开后紧跟多字节字符（· 等）必须写 ${var} 花括号——zh_CN.UTF-8
#      下 bash 5.3 会把多字节首字节吞进变量名（实测 "$a·" 展开为空 + 半个 ·）
#
# 维护约定（2026-08-19 更新）：设计类改动可走 statusline-setup agent；涉及
# 测试或新建文件的改动走主会话 + 假 stdin JSON 实测（statusline-setup 仅有
# Read+Edit，无法建文件也无法验证，见知识库 tools.md 2026-08-19 条目）。
# 上一版备份：~/.claude/statusline-command.sh.bak.20260819-1217
#

# ── 可调参数 ────────────────────────────────────────────────────────────
TRUNCATE_LENGTH=3        # 目录保留的末尾段数（starship truncation_length）
THRESHOLD_GREEN=50       # 剩余 > 此值 → 绿
THRESHOLD_YELLOW=20      # 剩余 ≥ 此值 → 黄；低于则红

# ── 配色 ────────────────────────────────────────────────────────────────
C_DIR=$'\033[1;36m'      # 目录：青色粗体
C_BRANCH=$'\033[1;35m'   # 分支：品红粗体
C_GIT=$'\033[1;31m'      # git 状态：红色粗体
C_MODEL=$'\033[90m'      # 模型名/信息段：暗灰
C_RESET=$'\033[0m'
GLYPH_BRANCH=''         # nerd font 分支字形 U+E0A0
SEP=' · '                # 尾部各段分隔符

# 按剩余百分比返回 ANSI 颜色码（32 绿 / 33 黄 / 31 红）
hue() {
  if   [ "$1" -gt "$THRESHOLD_GREEN"  ]; then printf 32
  elif [ "$1" -ge "$THRESHOLD_YELLOW" ]; then printf 33
  else                                        printf 31
  fi
}

# git 三件套 → "repo_root \t branch \t 状态旗标"（空字段用 "-" 占位，约束 5）
git_data() {
  local root branch status
  root=$(git --no-optional-locks rev-parse --show-toplevel 2>/dev/null)
  branch=$(git --no-optional-locks symbolic-ref --short HEAD 2>/dev/null)
  [ -z "$branch" ] && branch=$(git --no-optional-locks rev-parse --short HEAD 2>/dev/null)  # detached HEAD 回退短 SHA
  status=""
  if [ -n "$branch" ]; then
    # --porcelain=v1 的分支头在任何 locale 下都输出英文 ahead/behind，
    # 不可换成 `status -sb`（后者会本地化成「领先/落后」导致匹配失效）。
    status=$(git --no-optional-locks status --porcelain=v1 -b 2>/dev/null | awk '
      NR == 1 {
        if (match($0, /ahead [0-9]+/))  a = substr($0, RSTART + 6, RLENGTH - 6)
        if (match($0, /behind [0-9]+/)) h = substr($0, RSTART + 7, RLENGTH - 7)
        next
      }
      /^\?\?/                      { u = "?"; next }   # 未跟踪
      /^(DD|AU|UD|UA|DU|AA|UU)/    { c = "="; next }   # 冲突
      {
        if (substr($0, 1, 1) != " ") g = "+"          # 已暂存
        if (substr($0, 2, 1) != " ") m = "!"          # 已修改
      }
      END {
        printf "%s%s%s%s%s%s", c, m, g, u,
               (a != "" ? "\342\207\241" a : ""),      # ⇡n 领先
               (h != "" ? "\342\207\243" h : "")       # ⇣n 落后
      }')
  fi
  printf '%s\t%s\t%s' "${root:--}" "${branch:--}" "${status:--}"
}

# ── 后台刷新模式（--refresh <dir> <transcript_path> <session_id>）────────
if [ "${1:-}" = "--refresh" ]; then
  dir=${2:-}
  tp=${3:-}
  sid=${4:-}
  [ -n "$dir" ] && cd "$dir" 2>/dev/null
  key=${dir//\//_}; key=${key// /_}
  tmp="/tmp/.claude-sl-git-${key}.$$"
  git_data > "$tmp" 2>/dev/null && mv -f "$tmp" "/tmp/claude-sl-git-${key}.cache" 2>/dev/null
  if [ -n "$tp" ] && [ "$tp" != "-" ] && [ -n "$sid" ] && [ "$sid" != "-" ]; then
    tok=$(python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-tokens.py" "$tp" "$sid" 2>/dev/null)
    skey=${sid//\//_}; skey=${skey// /_}
    tmp2="/tmp/.claude-sl-tok-${skey}.$$"
    printf '%s\n' "$tok" > "$tmp2" 2>/dev/null && mv -f "$tmp2" "/tmp/claude-sl-tok-${skey}.cache" 2>/dev/null
  fi
  exit 0
fi

# ── 同步路径：1 次 jq 提取全部字段（约束 5：空字段占位 "-"）──────────────
input=$(cat)

fields=$(printf '%s' "$input" | jq -r '
  def hfmt:
    if . >= 1000000 then
      ((. / 1000000) as $v |
       if ($v | floor) == $v then "\($v | floor)M" else "\($v * 10 | round / 10)M" end)
    elif . >= 1000 then "\(. / 1000 | round)k"
    else tostring end;
  [ (.workspace.current_dir // .cwd // "-"),
    (.model.display_name // "-"),
    (.effort.level // "-"),
    (if (.context_window.remaining_percentage // null) == null then "-"
     else (.context_window.remaining_percentage | if . < 0 then 0 else . end | round | tostring) end),
    (if (.context_window.total_input_tokens // null) == null
        or (.context_window.context_window_size // null) == null then "-"
     else ((.context_window.total_input_tokens | hfmt) + "/" + (.context_window.context_window_size | hfmt)) end),
    ((.context_window.current_usage // null) as $u |
     if $u == null then "-" else
       ((($u.input_tokens // 0) + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0)) as $d |
        if $d <= 0 then "-" else (($u.cache_read_input_tokens // 0) / $d * 1000 | round / 10 | tostring) end)
     end),
    (if (.rate_limits.five_hour.used_percentage // null) == null then "-"
     else ((100 - .rate_limits.five_hour.used_percentage) | if . < 0 then 0 else . end | round | tostring) end),
    (.transcript_path // "-"),
    (.session_id // "-")
  ] | join("\t")' 2>/dev/null)

IFS=$'\t' read -r dir model eff ctx ctxabs hit quota tp sid <<< "$fields"
[ "$dir" = "-" ] && dir=""
[ "$model" = "-" ] && model=""
[ "$eff" = "-" ] && eff=""
[ "$ctx" = "-" ] && ctx=""
[ "$ctxabs" = "-" ] && ctxabs=""
[ "$hit" = "-" ] && hit=""
[ "$quota" = "-" ] && quota=""
[ "$tp" = "-" ] && tp=""
[ "$sid" = "-" ] && sid=""
[ -n "$model" ] && [ -n "$eff" ] && model="$model $eff"

# ── 目录归一化 ─────────────────────────────────────────────────────────
cd "$dir" 2>/dev/null
dir=$(pwd -P)

# ── git 段：优先读缓存，缺失时同步回退（首次/换目录后的那一轮）──────────
key=${dir//\//_}; key=${key// /_}
gitcache="/tmp/claude-sl-git-${key}.cache"
if [ -r "$gitcache" ]; then
  IFS=$'\t' read -r repo_root branch gstatus < "$gitcache"
else
  IFS=$'\t' read -r repo_root branch gstatus <<< "$(git_data)"
fi
[ "$repo_root" = "-" ] && repo_root=""
[ "$branch" = "-" ] && branch=""
[ "$gstatus" = "-" ] && gstatus=""

# ── token 段缓存（io 段 \t 烧速段）────────────────────────────────────
p_io=""; p_rate=""
if [ -n "$sid" ]; then
  skey=${sid//\//_}; skey=${skey// /_}
  tokcache="/tmp/claude-sl-tok-${skey}.cache"
  [ -r "$tokcache" ] && IFS=$'\t' read -r p_io p_rate < "$tokcache"
fi

# ── 目录显示（starship truncate_to_repo 语义）──────────────────────────
if [ -n "$repo_root" ]; then
  path="$(basename "$repo_root")${dir#$repo_root}"
elif [ "$dir" = "$HOME" ]; then
  path="~"
else
  case "$dir" in
    "$HOME"/*) path="~${dir#$HOME}" ;;
    *)         path="$dir" ;;
  esac
fi
path=$(printf '%s\n' "$path" | awk -F/ -v n="$TRUNCATE_LENGTH" '{
  if (NF <= n) { print; exit }
  s = ""
  for (i = NF - n + 1; i <= NF; i++) s = (s == "" ? $i : s "/" $i)
  print s
}')

# ── 组装纯文本段（先算宽度再上色）──────────────────────────────────────
p_prefix=$path
if [ -n "$branch" ]; then
  p_prefix="$p_prefix on $GLYPH_BRANCH $branch"
  [ -n "$gstatus" ] && p_prefix="$p_prefix [$gstatus]"
fi
p_model=$model
p_ctx=""
case "$ctx" in
  ''|*[!0-9]*) ;;
  *) p_ctx="ctx ${ctx}%"; [ -n "$ctxabs" ] && p_ctx="${p_ctx}·${ctxabs}" ;;
esac
p_hit=""
[ -n "$hit" ] && p_hit="hit ${hit}%"
p_5h=""
case "$quota" in
  ''|*[!0-9]*) ;;
  *) p_5h="5h ${quota}%" ;;
esac

# 计算一行的可见宽度（prefix + 两空格 + 各段以 SEP 相连）
line_len() {
  local t="$p_prefix" s tp_=""
  for s in "$@"; do [ -n "$s" ] && tp_="${tp_:+$tp_$SEP}$s"; done
  [ -n "$tp_" ] && t="$t  $tp_"
  printf '%s' "${#t}"
}

# ── $COLUMNS 自适应：超宽按优先级丢段 hit → 烧速 → ctx绝对值 → 5h → io ──
if [ -n "${COLUMNS:-}" ] && [ "$COLUMNS" -gt 10 ] 2>/dev/null; then
  budget=$((COLUMNS - 1))
  [ "$(line_len "$p_model" "$p_ctx" "$p_hit" "$p_io" "$p_rate" "$p_5h")" -gt "$budget" ] && p_hit=""
  [ "$(line_len "$p_model" "$p_ctx" "$p_hit" "$p_io" "$p_rate" "$p_5h")" -gt "$budget" ] && p_rate=""
  if [ "$(line_len "$p_model" "$p_ctx" "$p_hit" "$p_io" "$p_rate" "$p_5h")" -gt "$budget" ]; then
    case "$ctx" in ''|*[!0-9]*) ;; *) p_ctx="ctx ${ctx}%" ;; esac
  fi
  [ "$(line_len "$p_model" "$p_ctx" "$p_hit" "$p_io" "$p_rate" "$p_5h")" -gt "$budget" ] && p_5h=""
  [ "$(line_len "$p_model" "$p_ctx" "$p_hit" "$p_io" "$p_rate" "$p_5h")" -gt "$budget" ] && p_io=""
fi

# ── 上色输出 ───────────────────────────────────────────────────────────
c_prefix="${C_DIR}${path}${C_RESET}"
if [ -n "$branch" ]; then
  c_prefix="$c_prefix on ${C_BRANCH}${GLYPH_BRANCH} ${branch}${C_RESET}"
  [ -n "$gstatus" ] && c_prefix="$c_prefix ${C_GIT}[${gstatus}]${C_RESET}"
fi

ctail=""
[ -n "$p_model" ] && ctail="${ctail:+$ctail$SEP}${C_MODEL}${p_model}${C_RESET}"
[ -n "$p_ctx" ]   && ctail="${ctail:+$ctail$SEP}$(printf '\033[%sm%s\033[0m' "$(hue "$ctx")" "$p_ctx")"
[ -n "$p_hit" ]   && ctail="${ctail:+$ctail$SEP}${C_MODEL}${p_hit}${C_RESET}"
[ -n "$p_io" ]    && ctail="${ctail:+$ctail$SEP}${C_MODEL}${p_io}${C_RESET}"
[ -n "$p_rate" ]  && ctail="${ctail:+$ctail$SEP}${C_MODEL}${p_rate}${C_RESET}"
[ -n "$p_5h" ]    && ctail="${ctail:+$ctail$SEP}$(printf '\033[%sm%s\033[0m' "$(hue "$quota")" "$p_5h")"

printf '%s' "$c_prefix"
[ -n "$ctail" ] && printf '  %s' "$ctail"

# ── 脱钩后台刷新（慢段下一周期更新；abort 杀掉也无害，下轮重试）──────────
( bash "${BASH_SOURCE[0]}" --refresh "$dir" "${tp:--}" "${sid:--}" >/dev/null 2>&1 & ) 2>/dev/null

true
