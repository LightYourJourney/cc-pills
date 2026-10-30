#!/usr/bin/env bash
# cc-pills 诊断（只读）
#
#   bash tools/doctor.sh                    检查本机安装与运行时状态
#   bash tools/doctor.sh --dir <路径>        指定当前项目目录（git 缓存 / 仓库判定用；默认 $PWD）
#   bash tools/doctor.sh --transcript <路径>  检查某个会话的 transcript 是否可读
#   bash install.sh --doctor                同一实现的别名
#
# 输出每行「[状态] 分组/项  说明」，状态 ∈ PASS / WARN / FAIL / SKIP / UNKNOWN：
#   PASS 正常；WARN 能工作但有隐患；FAIL 状态栏很可能不工作；
#   SKIP 不适用（没装、没配、没用到）；UNKNOWN 取决于运行时数据，这里无法确定。
# 末行汇总。退出码：0 = 无 FAIL，1 = 有 FAIL，2 = 参数错误。
#
# 只读承诺
# --------
# - 不改写用户配置、缓存、数据库与系统文件；唯一的临时写是冒烟用的 mktemp -d（跑完即删）。
# - 不联网：不调用 curl，不访问 status.claude.com。
# - CC Switch 数据库只用 `file:<path>?mode=ro` 打开（与 statusline-tokens.py 约束 7 同口径），
#   且只在中转站模式下打开。WAL 模式的库即使只读打开，SQLite 也可能在库旁建 -wal / -shm
#   旁文件——生产脚本是同样的访问方式，诊断不额外写库内容。
# - 不打印环境变量全集；ANTHROPIC_BASE_URL 只给 scheme://host（连 user:pass@ 一并去掉）；
#   settings 里的命令行经 scrub() 抹掉 sk-… / *_KEY=… / *_TOKEN=… / URL 凭据 / Bearer … 后显示。
# - 正常渲染路径不调用本脚本（statusline-command.sh 与 subagent-statusline.sh 都不引用它，
#   由 tests/test_doctor.py 的契约测试守护）。
#
# 为什么是独立脚本
# ----------------
# install.sh 是写工具（复制 / 备份 / 删除），诊断是读工具；分开才能从结构上保证「默认只读」。
# install.sh --doctor 只是 exec 别名，不重复任何逻辑。
#
# 本文件只在 bash ≥ 3.2 上要求可运行（诊断要在最坏环境里也能跑），因此不用关联数组。
# 下列「与渲染脚本同源」的常量由 tests/test_doctor.py 守护，改了渲染脚本这里要跟着改。

set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/claude-statusline.conf
CCSWITCH_DB=$HOME/.cc-switch/cc-switch.db

# ── 与 statusline-command.sh 同源的常量（契约测试比对）──────────────────
THEME_TTL=300        # 色板重算间隔
TOK_STALE=900        # token 缓存超此秒数即判陈旧（派生项隐藏）
LOCK_STALE=30        # 后台刷新残锁的判定秒数
PALETTE_V=4          # 色板缓存格式版本
TOK_FIELDS=18        # 18 段 token 协议（与 statusline-tokens.py 的 FIELDS 一致）
BASH_MIN=4.2         # 渲染脚本要求的最低 bash
PY_MIN=3.9           # 渲染脚本用到 math.dist（3.8）与 str.removesuffix（3.9）
# 配置文件认的键（必须与 statusline-command.sh 里 case "$k" 的分支一致）
CONF_KEYS="theme glyphs status_page pill_style safe_margin truncate_length warn_at bad_at
  ctx_warn ctx_bad crit_at pill_chroma step"

DIR=$PWD
TRANSCRIPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        [ $# -ge 2 ] || { echo "--dir 需要一个路径" >&2; exit 2; }; DIR=$2; shift 2 ;;
    --transcript) [ $# -ge 2 ] || { echo "--transcript 需要一个路径" >&2; exit 2; }; TRANSCRIPT=$2; shift 2 ;;
    -h|--help)    sed -n '2,/^set -u$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "未知参数：${1}（--help 看用法）" >&2; exit 2 ;;
  esac
done
case "$DIR" in /*) ;; *) DIR=$PWD/$DIR ;; esac

NOW=${EPOCHSECONDS:-$(date +%s)}
n_pass=0; n_warn=0; n_fail=0; n_skip=0; n_unk=0
res() {  # res <状态> <分组/项> [说明] [修复建议]
  local st=$1 name=$2 detail=${3:-} fix=${4:-}
  printf '[%s] %s  %s\n' "$st" "$name" "$detail"
  [ -n "$fix" ] && printf '         修：%s\n' "$fix"
  case $st in
    PASS) n_pass=$((n_pass + 1)) ;; WARN) n_warn=$((n_warn + 1)) ;;
    FAIL) n_fail=$((n_fail + 1)) ;; SKIP) n_skip=$((n_skip + 1)) ;;
    *) n_unk=$((n_unk + 1)) ;;
  esac
  return 0
}
sec() { printf '\n── %s ──\n' "$1"; }

# 凭据形态从要显示的任何字符串里抹掉；其余内容原样保留（否则用户没法照着修）
scrub() {
  sed -E \
    -e 's/sk-[A-Za-z0-9_-]{8,}/sk-***/g' \
    -e 's/([A-Za-z0-9_]*(KEY|TOKEN)=)[^ ]+/\1***/g' \
    -e 's#//[^/@ ]+@#//***@#g' \
    -e "s/([Xx]-[Aa]pi-[Kk]ey[ ]*:?[ ]*[\"']?)[^ \";']+/\1***/g" \
    -e "s/([Aa]uthorization[ ]*:[ ]*[A-Za-z]+[ ]+[\"']?)[^ \";']+/\1***/g" \
    -e 's/([Bb]earer )[A-Za-z0-9._~+\/-]+/\1***/g'
}

if [ "${OSTYPE#darwin}" != "$OSTYPE" ] && [ -x /usr/bin/stat ]; then
  mtime() { /usr/bin/stat -f %m "$1" 2>/dev/null; }
  stat_mode() { /usr/bin/stat -f %Lp "$1" 2>/dev/null; }
  fsize() { /usr/bin/stat -f %z "$1" 2>/dev/null; }
else
  mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
  stat_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null; }
  fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null; }
fi

# 点分版本比较：ver_ge <实际> <最低>；无法解析时返回 1（当作不达标，宁可报出来）
ver_ge() {
  local a1=${1%%.*} b1=${2%%.*} a2 b2
  case "$a1$b1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$a1" -gt "$b1" ] && return 0
  [ "$a1" -lt "$b1" ] && return 1
  a2=${1#*.}; a2=${a2%%.*}; b2=${2#*.}; b2=${b2%%.*}
  case "$a2$b2" in ''|*[!0-9]*) return 1 ;; esac
  [ "$a2" -ge "$b2" ]
}

# ── 配置（只按白名单解析，与渲染脚本同一套规则；非法值按默认处理）──────
# 降级序列的合法项名从渲染脚本的 ITEM_IDS 取（单一来源）：取不到就跳过 step 校验，
# 宁可少判一项，也不在这里再抄一份会漂移的清单。
stepids=""
for s in "$DEST/statusline-command.sh" "$REPO/src/statusline-command.sh"; do
  [ -r "$s" ] || continue
  raw=$(sed -n '/^ITEM_IDS="/,/"/p' "$s" 2>/dev/null)
  [ -n "$raw" ] || continue
  raw=${raw#*ITEM_IDS=}; raw=${raw#\"}; raw=${raw%\"*}
  stepids=" $(echo $raw) "
  break
done
C_THEME=auto; C_CHROMA=12; C_STYLE=tint; C_PAGE=on
CFG_BAD=""; CFG_UNKNOWN=""; CFG_READABLE=0
CONF_KEYS_ONE=" $(echo $CONF_KEYS) "     # 折叠成一行，下面逐行比对时不必每行 fork
if [ -r "$CONF" ]; then
  CFG_READABLE=1
  while IFS='=' read -r k v; do
    k=${k//[[:space:]]/}; v=${v%%#*}; rv=$v; v=${v//[[:space:]]/}
    case "$k" in ''|\#*) continue ;; esac
    case "$CONF_KEYS_ONE" in
      *" $k "*) ;;
      *) CFG_UNKNOWN="$CFG_UNKNOWN $k"; continue ;;
    esac
    case "$k" in
      theme) case "$v" in auto|tokyonight|dracula|catppuccin|onedark|nord) C_THEME=$v ;;
                            *) CFG_BAD="$CFG_BAD $k" ;; esac ;;
      glyphs) case "$v" in nerd|ascii) ;; *) CFG_BAD="$CFG_BAD $k" ;; esac ;;
      status_page) case "$v" in on|off) C_PAGE=$v ;; *) CFG_BAD="$CFG_BAD $k" ;; esac ;;
      pill_style) case "$v" in tint|cool|jewel|glass|mono|pastel|tokyo-glass) C_STYLE=$v ;;
                              *) CFG_BAD="$CFG_BAD $k" ;; esac ;;
      pill_chroma) case "$v" in ''|*[!0-9]*) CFG_BAD="$CFG_BAD $k" ;;
                              *) [ $((10#$v)) -le 40 ] && C_CHROMA=$((10#$v)) || CFG_BAD="$CFG_BAD $k" ;; esac ;;
      step)
        if [ -n "$stepids" ]; then
          read -r act sid_ _ <<< "$rv"
          case "$act" in
            drop|shrink) case "$stepids" in
                           *" $sid_ "*) ;;
                           *) CFG_BAD="$CFG_BAD step($act $sid_)" ;;
                         esac ;;
            *) CFG_BAD="$CFG_BAD step($act)" ;;
          esac
        fi ;;
      *) case "$v" in ''|*[!0-9]*) CFG_BAD="$CFG_BAD $k" ;; esac ;;
    esac
  done < "$CONF"
fi

# ═══ 运行环境 ══════════════════════════════════════════════════════════
sec 运行环境
os=$(uname -s 2>/dev/null); arch=$(uname -m 2>/dev/null)
osver=""
[ "$os" = Darwin ] && osver=" $(sw_vers -productVersion 2>/dev/null)"
res PASS 环境/系统 "$os$osver $arch"

# Claude Code 用 PATH 里的 bash 执行 statusLine 命令；渲染脚本自带 ≥5 的换壳守卫
bbash=$(command -v bash 2>/dev/null)
bver=""
[ -n "$bbash" ] && bver=$("$bbash" -c 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"' 2>/dev/null)
if [ -z "$bbash" ]; then
  res FAIL 环境/bash "PATH 里没有 bash" "安装 bash 或修正 PATH"
elif ver_ge "$bver" "$BASH_MIN"; then
  res PASS 环境/bash "${bver}（${bbash}）"
else
  alt=""
  for b in /opt/homebrew/bin/bash /usr/local/bin/bash /usr/bin/bash; do
    [ -x "$b" ] || continue
    v=$("$b" -c 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"' 2>/dev/null)
    if ver_ge "$v" 5; then alt="$b $v"; break; fi
  done
  if [ -n "$alt" ]; then
    res WARN 环境/bash "PATH 里的 bash 是 ${bver}，低于 ${BASH_MIN}；渲染脚本会换用 $alt" \
      "把新版 bash 放到 PATH 前面：brew install bash"
  else
    res FAIL 环境/bash "PATH 里的 bash 是 ${bver}，低于 ${BASH_MIN}，且找不到 ≥5 的替代" \
      "brew install bash（脚本会退化成「模型 · ctx%」最简行）"
  fi
fi

py=$(command -v python3 2>/dev/null)
pver=""
[ -n "$py" ] && pver=$("$py" -V 2>&1 | sed 's/[^0-9.]//g' | head -1)
if [ -z "$py" ]; then
  res FAIL 环境/python3 "未找到 python3" "后台的 token / 色板助手无法运行：brew install python"
elif ver_ge "$pver" "$PY_MIN"; then
  res PASS 环境/python3 "${pver}（${py}）"
else
  res FAIL 环境/python3 "$pver 低于最低 ${PY_MIN}（math.dist / str.removesuffix）" \
    "升级 python3 到 $PY_MIN 或更高（brew install python）"
fi

jq=$(command -v jq 2>/dev/null)
if [ -z "$jq" ]; then
  res FAIL 环境/jq "未找到 jq" "brew install jq；缺失时同步路径的字段提取整体失败，状态栏会空"
else
  res PASS 环境/jq "$(jq --version 2>/dev/null)（${jq}）"
fi

git=$(command -v git 2>/dev/null)
if [ -z "$git" ]; then
  res WARN 环境/git "未找到 git" "分支 / 目录状态项会消失，其余照常：xcode-select --install"
else
  res PASS 环境/git "$(git --version 2>/dev/null | sed 's/^git version //')（${git}）"
fi

curl=$(command -v curl 2>/dev/null)
if [ "$C_PAGE" = off ]; then
  res SKIP 环境/curl "配置 status_page = off，不使用状态页"
elif [ -z "$curl" ]; then
  res WARN 环境/curl "未找到 curl，但 status_page = on" "brew install curl，或设 status_page = off"
else
  res PASS 环境/curl "可用；status_page = on（每分钟请求 status.claude.com）"
fi

case "${COLORTERM:-}" in
  truecolor|24bit) cdepth="真彩（24 位）" ;;
  *) case "${TERM:-}" in *256color*) cdepth="256 色" ;; *) cdepth="无色（状态栏退化为纯文本）" ;; esac ;;
esac
case "$cdepth" in
  无色*) res WARN 环境/终端 "TERM=${TERM:-<未设>} COLORTERM=${COLORTERM:-<未设>} → $cdepth" \
           "在支持颜色的终端里运行，或设 COLORTERM=truecolor" ;;
  *) res PASS 环境/终端 "TERM=${TERM:-<未设>} TERM_PROGRAM=${TERM_PROGRAM:-<未设>} → $cdepth" ;;
esac

# ═══ 安装一致性 ════════════════════════════════════════════════════════
sec 安装一致性
if [ -d "$DEST" ]; then
  res PASS 安装/目录 "$DEST${CLAUDE_CONFIG_DIR:+（来自 CLAUDE_CONFIG_DIR）}"
else
  res FAIL 安装/目录 "$DEST 不存在" "bash install.sh"
fi

sj=$DEST/settings.json
SJ_OK=0; SJ_STATE=FAIL      # settings.json 不可用时的连带状态：文件缺失 / 损坏 = FAIL，jq 缺失 = SKIP
if [ -n "$jq" ] && [ -r "$sj" ] && jq -e . "$sj" >/dev/null 2>&1; then
  SJ_OK=1
  res PASS 安装/settings.json "可解析"
elif [ -z "$jq" ]; then
  SJ_STATE=SKIP
  res SKIP 安装/settings.json "jq 缺失，无法校验（见 环境/jq）"
elif [ ! -e "$sj" ]; then
  res FAIL 安装/settings.json "$sj 不存在" \
    '写入 "statusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\""}'
else
  res FAIL 安装/settings.json "$sj 无法解析（JSON 语法错误）" "修好该文件的 JSON，状态栏配置才不会整体失效"
fi

# 与 install.sh 的 points_to 同一判据：0 明确指向已安装副本 / 2 只含文件名 / 1 未指向
# 剥掉「文件名后紧跟单词字符」的出现（statusline-command.sh.bak / .tmp 指向的是另一个文件），
# 剩下的才是真的引用。全都被后缀黏住时结果不含文件名，points_to 判未指向。
# → 全局 CLEAN。逐次剥离而非一次 case：同一命令里可能既有 .bak 又有真引用。
strip_dirty() {
  local rest=$1 out=""
  while case "$rest" in *"$2"*) true ;; *) false ;; esac; do
    out=$out${rest%%"$2"*}
    rest=${rest#*"$2"}
    case "$rest" in
      [A-Za-z0-9_.-]*) ;;      # 脏：不把文件名放进结果，后缀留给下一轮
      *) out=$out$2 ;;         # 干净
    esac
  done
  CLEAN=$out$rest
}

# 与 install.sh 的 points_to 同一判据：0 明确指向已安装副本 / 2 只含文件名 / 1 未指向
points_to() {
  strip_dirty "$1" "$2"
  case "$CLEAN" in
    *"$3/$2"*) return 0 ;;
    *'${CLAUDE_CONFIG_DIR'*"$2"*) return 0 ;;
  esac
  if [ "$3" = "$HOME/.claude" ]; then
    case "$CLEAN" in *'$HOME/.claude/'*"$2"*|*'~/.claude/'*"$2"*) return 0 ;; esac
  fi
  case "$CLEAN" in *"$2"*) return 2 ;; esac
  return 1
}

keycheck() {  # keycheck <settings 键> <脚本名> <缺失时的状态>
  local key=$1 f=$2 miss=$3 cmd st
  if [ "$SJ_OK" != 1 ]; then res "$SJ_STATE" "安装/$key" "settings.json 不可用，无法校验"; return; fi
  cmd=$(jq -r --arg k "$key" '.[$k].command // empty' "$sj" 2>/dev/null)
  if [ -z "$cmd" ]; then
    res "$miss" "安装/$key" "未配置" \
      "写入 \"$key\": {\"type\": \"command\", \"command\": \"bash \\\"\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}/$f\\\"\"}"
    return
  fi
  points_to "$cmd" "$f" "$DEST"; st=$?
  case $st in
    0) if [ -f "$DEST/$f" ]; then
         res PASS "安装/$key" "指向 $DEST/$f"
       else
         res FAIL "安装/$key" "命令指向 $DEST/${f}，但该文件不存在" "bash install.sh"
       fi ;;
    2) res FAIL "安装/$key" "命令里只有文件名、路径不明：$(printf '%s' "$cmd" | scrub)" "改成指向 $DEST/$f 的绝对路径" ;;
    *) res FAIL "安装/$key" "命令未指向已安装副本：$(printf '%s' "$cmd" | scrub)" "改成指向 $DEST/$f 的绝对路径" ;;
  esac
}
keycheck statusLine statusline-command.sh FAIL

sub_installed=0
[ -f "$DEST/subagent-statusline.sh" ] && sub_installed=1
sub_cfg=0
if [ "$SJ_OK" = 1 ]; then
  [ -n "$(jq -r '.subagentStatusLine.command // empty' "$sj" 2>/dev/null)" ] && sub_cfg=1
fi
if [ "$sub_installed" = 0 ] && [ "$sub_cfg" = 0 ]; then
  res SKIP 安装/subagentStatusLine "面板脚本未安装、也未配置"
else
  keycheck subagentStatusLine subagent-statusline.sh "$([ "$sub_installed" = 1 ] && echo WARN || echo FAIL)"
fi

for f in statusline-command.sh statusline-tokens.py statusline-theme.py subagent-statusline.sh; do
  dst=$DEST/$f; src=$REPO/src/$f
  if [ ! -e "$dst" ]; then
    if [ "$f" = subagent-statusline.sh ]; then
      res SKIP "安装/副本-$f" "未安装（未使用 agent 面板）"
    else
      res FAIL "安装/副本-$f" "未安装" "bash install.sh"
    fi
  elif [ ! -f "$src" ]; then
    res SKIP "安装/副本-$f" "仓库里没有对应源文件（从已安装副本运行？）"
  elif cmp -s "$src" "$dst"; then
    res PASS "安装/副本-$f" "与仓库一致"
  else
    res WARN "安装/副本-$f" "与仓库不一致（手工改过，或仓库更新后没重装）" "bash install.sh"
  fi
done

if [ ! -e "$CONF" ]; then
  res SKIP 安装/配置文件 "$CONF 不存在（全部使用内置默认）"
elif [ "$CFG_READABLE" = 0 ]; then
  res WARN 安装/配置文件 "$CONF 存在但不可读；渲染会静默改用内置默认，状态栏照常工作" "chmod 600 \"$CONF\""
elif [ -n "$CFG_UNKNOWN" ]; then
  res WARN 安装/配置文件 "这些键不认识、会被静默忽略：$CFG_UNKNOWN" "删掉它们，或核对 config/claude-statusline.conf.example"
elif [ -n "$CFG_BAD" ]; then
  res WARN 安装/配置文件 "这些键的值不合法、会被静默忽略：$CFG_BAD" "核对 config/claude-statusline.conf.example 里的可选值"
else
  res PASS 安装/配置文件 "可解析：theme=$C_THEME pill_style=$C_STYLE status_page=$C_PAGE"
fi

# ═══ 缓存与后台任务 ════════════════════════════════════════════════════
sec 缓存与后台任务
if [ -n "${CLAUDE_SL_TMP:-}" ]; then
  SLT=$CLAUDE_SL_TMP; src_note="（CLAUDE_SL_TMP）"
else
  tmpdir=${TMPDIR:-/tmp}; tmpdir=${tmpdir%/}       # TMPDIR 常以 / 结尾，显示时避免 //
  SLT=$tmpdir/claude-sl-${UID:-0}; src_note=""
fi
SLT_OK=0
if [ ! -e "$SLT" ]; then
  res SKIP 缓存/根 "$SLT$src_note 不存在；首次后台刷新会以 0700 创建"
elif [ ! -d "$SLT" ]; then
  res FAIL 缓存/根 "$SLT$src_note 存在但不是目录" "删掉它：rm -f \"$SLT\"（下次刷新重建）"
elif [ ! -O "$SLT" ]; then
  res FAIL 缓存/根 "$SLT$src_note 不属于当前用户；渲染会拒绝读取" "换一个 CLAUDE_SL_TMP，或删掉该目录"
else
  SLT_OK=1
  res PASS 缓存/根 "$SLT$src_note"
fi

if [ "$SLT_OK" = 1 ]; then
  mode=$(stat_mode "$SLT")
  if [ "$mode" = 700 ]; then
    res PASS 缓存/权限 "0700"
  else
    res WARN 缓存/权限 "目录权限 ${mode}，应为 700（同机其他用户可读你的用量与路径）" \
      "chmod 700 \"$SLT\""
  fi
else
  res SKIP 缓存/权限 "缓存根不可用"
fi

# 色板：PKEY 与渲染脚本同一算法（iTerm 按 profile，其余按终端程序名）
if [ "${TERM_PROGRAM:-}" = iTerm.app ] || [ "${LC_TERMINAL:-}" = iTerm2 ]; then
  PKEY="iterm-${ITERM_PROFILE:-default}"
else
  PKEY="term-${TERM_PROGRAM:-other}"
fi
PKEY=${PKEY//[^A-Za-z0-9_-]/_}
tc=$SLT/claude-sl-theme-$PKEY.cache
COLOUR_OK=0     # 渲染侧此刻能不能上色（statusline-command.sh 的 NC=0）
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/色板 "缓存根不可用"
elif [ ! -e "$tc" ]; then
  res WARN 缓存/色板 "本终端（${PKEY}）没有色板缓存：整行会无色，等下一轮后台刷新（约 2 秒）" \
    "若持续无色，检查 环境/python3 与 缓存/退避"
elif [ ! -f "$tc" ] || [ ! -O "$tc" ]; then
  res WARN 缓存/色板 "色板缓存不是属于本人的普通文件，渲染会拒绝读取（整行无色）" "删掉它：rm -f \"$tc\""
else
  # 上色的实际条件（statusline-command.sh:536-543）：
  #   1) 某行 THEME=<主题>/… 的主题部分等于当前 theme；
  #   2) 某行 P_CTX 的值只含数字与分号（渲染侧 ''|*[!0-9;]*) 会整条丢弃非法值）。
  # chroma / 风格 / 格式版本不符只触发后台重算，本轮照常上色。
  cache_theme=""; have_ctx=0
  while IFS='=' read -r k v; do
    case "$k" in
      THEME)   cache_theme=$v ;;
      P_CTX)   case "$v" in ''|*[!0-9\;]*) ;; *) have_ctx=1 ;; esac ;;
    esac
  done < "$tc"
  [ "${cache_theme%%/*}" = "$C_THEME" ] && [ "$have_ctx" = 1 ] && COLOUR_OK=1
  line=""; read -r line < "$tc"
  line=${line//[[:cntrl:]]/}                       # 与渲染侧约束 16 一致：别把控制字符打上屏
  want="THEME=$C_THEME/$C_CHROMA/$C_STYLE/v$PALETTE_V"
  if [ "$line" != "$want" ]; then
    if [ "$COLOUR_OK" = 1 ]; then
      res WARN 缓存/色板 "首行是「${line}」，与当前配置不符（期望 ${want}）；仍按旧色板上色，后台会重算" \
        "等下一轮刷新；若一直不变，看 缓存/退避"
    else
      res WARN 缓存/色板 "首行是「${line}」，与当前配置不符且不能上色（主题不同或缺合法 P_CTX）；后台会重算" \
        "等下一轮刷新；若一直不变，看 缓存/退避"
    fi
  elif [ "$COLOUR_OK" = 1 ]; then
    age=$(( NOW - $(mtime "$tc") ))
    if [ "$age" -ge "$THEME_TTL" ]; then
      res PASS 缓存/色板 "${want}；上次重算 ${age}s 前（后台按需触发，没有活跃会话时不上涨是正常的）"
    else
      res PASS 缓存/色板 "${want}，${age}s 前写入"
    fi
  else
    res WARN 缓存/色板 "首行匹配但 P_CTX 缺失或非法（文件损坏）：本轮无色，后台会重算" "等下一轮刷新"
  fi
fi

# token 缓存：只看新鲜度与归属，不读内容
# 与渲染侧同判据：.ts 内容不是数字、或 .ts 不是本人的普通文件（含悬空符号链接）→ 判陈旧
# （tok_ok=0，派生项隐藏）。没有 .ts 的旧缓存本轮按「新鲜」处理，但后台刷新会按缓存 mtime
# 补写 .ts，下一轮就判陈旧并隐藏派生项（3a8679e）；这里报的是刷新前这段静默陈旧。
tok_newest=-1; tok_n=0; tok_notmine=0; tok_tsbad=0; tok_malformed=0
tok_old_ts=0; tok_no_ts_old=0
for f in "$SLT"/claude-sl-tok-*.cache; do
  [ -f "$f" ] || continue
  tok_n=$((tok_n + 1))
  [ -O "$f" ] || tok_notmine=$((tok_notmine + 1))
  _parts=(); IFS=$'\t' read -r -a _parts < "$f" 2>/dev/null
  [ "${#_parts[@]}" -eq "$TOK_FIELDS" ] || tok_malformed=$((tok_malformed + 1))
  ts=$f.ts
  if [ -f "$ts" ] && [ ! -L "$ts" ] && [ -O "$ts" ]; then
    tv=""; read -r tv < "$ts"
    case "$tv" in
      ''|*[!0-9]*) tok_tsbad=$((tok_tsbad + 1)); t=0 ;;
      *) t=$tv; [ $(( NOW - tv )) -gt "$TOK_STALE" ] && tok_old_ts=$((tok_old_ts + 1)) ;;
    esac
  elif [ -e "$ts" ] || [ -L "$ts" ]; then
    tok_tsbad=$((tok_tsbad + 1)); t=0
  else
    t=$(( $(mtime "$f") ))
    [ $(( NOW - t )) -gt "$TOK_STALE" ] && tok_no_ts_old=$((tok_no_ts_old + 1))
  fi
  [ "$t" -gt "$tok_newest" ] && tok_newest=$t
done
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/token "缓存根不可用"
elif [ "$tok_n" = 0 ]; then
  res SKIP 缓存/token "暂无 token 缓存（还没有会话渲染过）"
else
  age=$(( NOW - tok_newest ))
  if [ "$tok_notmine" -gt 0 ]; then
    res WARN 缓存/token "$tok_n 个缓存里有 $tok_notmine 个不属于当前用户，会被跳过" "rm -f \"$SLT\"/claude-sl-tok-*"
  elif [ "$tok_malformed" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_malformed 个缓存不是 $TOK_FIELDS 段（被截断或旧格式）：对应派生项会错位或消失" \
      "删掉该会话的 token 缓存，后台会整段重算"
  elif [ "$tok_tsbad" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_tsbad 个新鲜度时间戳损坏或不属于本人：渲染侧会判陈旧，\$/h、out、hit、io、msgs 被隐藏" \
      "删掉对应的 .ts，让后台刷新重写"
  elif [ "$tok_old_ts" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_old_ts 个缓存超过 TOK_STALE=${TOK_STALE}：\$/h、out、hit、io、msgs 会被隐藏" \
      "回到那个会话让它刷新；若持续不更新，看 数据/stdin 与 缓存/后台刷新"
  elif [ "$tok_no_ts_old" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_no_ts_old 个缓存没有新鲜度时间戳、且已超过 TOK_STALE：本轮仍沿用旧值，后台刷新按 mtime 回填 .ts 后派生项会被隐藏" \
      "让该会话刷新一次即可补写 .ts；持续出现就查后台 token 计算是否在更新缓存"
  else
    res PASS 缓存/token "$tok_n 个会话缓存，最新 ${age}s 前"
  fi
fi

# git 缓存：缺失会同步回退（慢但正确），所以只做粗粒度提示；格式坏了也会被后台重算
gcache_n=0; gcache_bad=0
for f in "$SLT"/claude-sl-git-*.cache; do
  [ -f "$f" ] || continue
  gcache_n=$((gcache_n + 1))
  IFS=$'\t' read -r _r _b _s _t < "$f" 2>/dev/null    # 4 段：根 / 分支 / 旗标 / 写入时刻
  case "$_t" in ''|*[!0-9]*) gcache_bad=$((gcache_bad + 1)) ;; esac
done
if [ -z "$git" ]; then
  res SKIP 缓存/git "git 不可用（见 环境/git）"
elif [ ! -d "$DIR" ]; then
  res WARN 缓存/git "--dir 指向的目录不存在：$DIR" "传一个存在的项目目录"
elif ( cd "$DIR" 2>/dev/null && git --no-optional-locks rev-parse --show-toplevel >/dev/null 2>&1 ); then
  badnote=""
  [ "$gcache_bad" -gt 0 ] && badnote="（${gcache_bad} 个格式异常，后台会重算）"
  res PASS 缓存/git "$DIR 是 git 仓库；缓存 ${gcache_n} 个${badnote}（缺失时首次渲染会同步算一次）"
else
  res SKIP 缓存/git "$DIR 不是 git 仓库；缓存 $gcache_n 个"
fi

# 刷新锁：残锁目录会被下一轮自动清除；锁路径被普通文件占住时 mkdir 永远失败、后台刷新
# 从此每轮直接退出（渲染侧不会自愈），必须当故障报出来
lock_n=0; lock_old=""; lock_bad=""
for l in "$SLT"/claude-sl-lock-*; do
  [ -e "$l" ] || continue
  if [ ! -d "$l" ]; then lock_bad="$lock_bad ${l##*/}"; continue; fi
  lock_n=$((lock_n + 1))
  lt=$(mtime "$l"); lt=${lt:-$NOW}
  age=$(( NOW - lt ))
  if [ "$age" -ge "$LOCK_STALE" ]; then lock_old="$lock_old ${l##*/}(${age}s)"; fi
done
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/锁 "缓存根不可用"
elif [ -n "$lock_bad" ]; then
  res FAIL 缓存/锁 "锁路径是普通文件${lock_bad}：mkdir 永远失败，后台刷新会一直退出（不会自愈）" \
    "rm -f \"$SLT\"/claude-sl-lock-*"
elif [ -n "$lock_old" ]; then
  res WARN 缓存/锁 "残锁${lock_old}（超过 LOCK_STALE=${LOCK_STALE}）：下次刷新会自动清除，无需手工处理"
elif [ "$lock_n" -gt 0 ]; then
  res PASS 缓存/锁 "有刷新正在进行"
else
  res PASS 缓存/锁 "无锁残留"
fi

failf=$tc.fail
if [ -e "$failf" ]; then
  ft=""; read -r ft < "$failf" 2>/dev/null
  case "$ft" in ''|*[!0-9]*) ft=$NOW ;; esac
  if [ "$COLOUR_OK" = 1 ]; then
    # 渲染侧只有「首行与配置不符」时才让 .fail 阻止重试；色板有效时退避不影响任何渲染
    res WARN 缓存/退避 "色板生成上次失败（$(( NOW - ft ))s 前）；当前色板缓存有效，渲染不受影响，后台按 THEME_TTL 重试"
  else
    res WARN 缓存/退避 "色板生成上次失败（$(( NOW - ft ))s 前），且没有可用色板：整行无色，后台按 THEME_TTL 重试" \
      "手工重跑看报错：python3 \"$DEST/statusline-theme.py\" $C_THEME /tmp/palette.txt $C_CHROMA $C_STYLE $PALETTE_V"
  fi
fi

if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/后台刷新 "缓存根不可用，后台刷新会整轮放弃写缓存"
elif [ -z "$py" ]; then
  res SKIP 缓存/后台刷新 "python3 缺失：后台刷新算不了 token 与色板（见 环境/python3）"
elif [ -w "$SLT" ]; then
  res PASS 缓存/后台刷新 "缓存目录可写；python3 ${pver}；刷新不阻塞渲染（脱钩进程）"
else
  res FAIL 缓存/后台刷新 "缓存目录不可写：后台刷新无法落盘" "chown/chmod 修好 \"$SLT\"，或设 CLAUDE_SL_TMP 到可写目录"
fi

# ═══ 数据来源 ══════════════════════════════════════════════════════════
sec 数据来源
# 冒烟：用安全夹具真跑一次已安装脚本（隔离缓存目录、不派生后台刷新、不碰用户数据）。
# 跑两份夹具比对：结果不同才说明 stdin 真的进了渲染——一个只会 echo 的脚本也能「非空」。
scratch=$(mktemp -d "${TMPDIR:-/tmp}/ccpills-doctor.XXXXXX" 2>/dev/null)
trap '[ -n "${scratch:-}" ] && rm -rf "$scratch"' EXIT
smoke=$DEST/statusline-command.sh
[ -f "$smoke" ] || smoke=$REPO/src/statusline-command.sh
mkfx() {   # mkfx <current_dir> <ctx 总输入> <已用%> <模型名> <费用> [5h%]
  # 单引号里不能换行续行（反斜杠是字面量），所以整条 JSON 写在一行。
  # 子进程的 cwd 固定为 $scratch，差异只来自 stdin 里的 workspace.current_dir —— 否则一个
  # 只 printf "$PWD" 的占位脚本也会因为两次 cd 不同而「通过」差分。
  rl=""
  [ -n "${6:-}" ] && rl=',"rate_limits":{"five_hour":{"used_percentage":'"$6"',"resets_at":'"$((NOW + 7200))"'}}'
  printf '{"session_id":"doctor-fixture","transcript_path":"","cwd":"%s","workspace":{"current_dir":"%s"},"model":{"display_name":"%s"},"effort":{"level":"high"},"cost":{"total_cost_usd":%s,"total_api_duration_ms":123000},"context_window":{"total_input_tokens":%s,"context_window_size":200000,"used_percentage":%s},"prompt_cache":{"warm":true,"caching_observed":true,"ttl":"5m","expires_at":%s,"recache_tokens_if_cold":1234}%s}' \
    "$scratch" "$1" "$4" "$5" "$2" "$3" "$((NOW + 120))" "$rl"
}
runfx() {  # runfx <夹具> → 全局 RUNOUT（固定在同一目录执行）
  RUNOUT=$( cd "$scratch" 2>/dev/null && printf '%s' "$1" | \
            CLAUDE_SL_TMP="$scratch/slt" CLAUDE_SL_NO_REFRESH=1 COLUMNS=200 \
            TERM_PROGRAM= LC_TERMINAL= bash "$smoke" 2>/dev/null )
}
if [ ! -f "$smoke" ]; then
  res SKIP 数据/stdin "找不到可执行的 statusline-command.sh"
elif [ -z "$scratch" ]; then
  res SKIP 数据/stdin "无法创建临时目录，跳过冒烟"
else
  mkdir -p "$scratch/a" "$scratch/b" 2>/dev/null
  # 差分同时落在 dir（workspace.current_dir）、ctxin（总输入）、ctx / 模型 / 费用与 5h 额度上：
  # 前两项在默认序列里永不丢弃，所以即使用户 drop 了 ctx、模型与费用，差分仍然成立。
  runfx "$(mkfx "$scratch/a" 123456 37 'Opus 5.5' 1.23 20)"; outA=$RUNOUT
  runfx "$(mkfx "$scratch/b" 654321 88 'Doctor Fixture B' 9.87)"; outB=$RUNOUT
  # 判据不依赖任何具体项（用户可能改过降级序列）：非空 + 不是最简行 + 两份夹具结果不同
  case "$outA" in
    '')            res FAIL 数据/stdin "冒烟无输出（${smoke}）" \
                     "依次看 环境/jq、环境/bash、安装/副本-statusline-command.sh" ;;
    'Opus 5.5 · 37%') res FAIL 数据/stdin "只渲染出版本守卫的最简行（模型 · ctx%）：主渲染路径没跑起来" \
                     "看 环境/bash：PATH 里的 bash 需要 ≥${BASH_MIN}，或让脚本换到 ≥5 的 bash" ;;
    *) if [ "$outA" = "$outB" ]; then
         res FAIL 数据/stdin "两份不同夹具渲染出相同结果：stdin 没有真正进入渲染（${smoke}）" \
           "确认该文件是 cc-pills 的 statusline-command.sh，而不是包装脚本或占位脚本"
       else
         res PASS 数据/stdin "冒烟通过：两份夹具结果不同（${#outA} 字符），主渲染路径与字段解析都正常"
       fi ;;
  esac
fi

if [ -z "$TRANSCRIPT" ]; then
  res SKIP 数据/transcript "未指定；加 --transcript <jsonl> 可检查某个会话"
elif [ ! -e "$TRANSCRIPT" ]; then
  res FAIL 数据/transcript "$TRANSCRIPT 不存在" "确认会话 id 与路径（~/.claude/projects/<项目>/<session>.jsonl）"
elif [ ! -f "$TRANSCRIPT" ] || [ ! -r "$TRANSCRIPT" ]; then
  res FAIL 数据/transcript "$TRANSCRIPT 不是可读的普通文件" "检查权限"
else
  res PASS 数据/transcript "可读（$(fsize "$TRANSCRIPT") 字节）"
fi

# 计费模式只看运行时 stdin 才能确定；这里只做「配置层面」的推断，供 CC Switch 检查用。
# CC Switch 的标准写法是把 ANTHROPIC_BASE_URL 写进 settings.json 的 env，Claude Code 启动后
# 才注入到状态栏进程；只查 shell 变量会在最常见的部署方式上得出相反结论。
base=${ANTHROPIC_BASE_URL:-}; base_src="shell 环境变量"
if [ -z "$base" ] && [ "$SJ_OK" = 1 ]; then
  base=$(jq -r '.env.ANTHROPIC_BASE_URL // empty' "$sj" 2>/dev/null)
  [ -n "$base" ] && base_src="settings.json 的 env"
fi
relay=0
case "$base" in
  ''|https://api.anthropic.com|https://api.anthropic.com/*) ;;
  *) relay=1 ;;
esac
if [ "$relay" = 1 ]; then
  host=${base#*://}; host=${host%%/*}
  host=${host##*@}                       # 去掉 user:pass@，凭据不进屏幕
  note="ANTHROPIC_BASE_URL=${host}（${base_src}）"
else
  note="未设置自定义 ANTHROPIC_BASE_URL（shell 环境变量与 settings.json 的 env 都查过）"
fi
if [ "$relay" = 0 ]; then
  # 订阅模式下渲染侧从不打开 CC Switch 库（statusline-tokens.py 约束 7），诊断也不打开
  res SKIP 数据/CCSwitch库 "未使用中转站（${note}），不会打开数据库"
elif [ ! -e "$CCSWITCH_DB" ]; then
  res WARN 数据/CCSwitch库 "未找到 ${CCSWITCH_DB}（${note}）：中转站费用会回落为 \$x*（标价估算），日限额不显示" \
    "安装 CC Switch 并让它记录用量，或忽略（只是估算口径不同）"
elif [ -z "$py" ]; then
  res SKIP 数据/CCSwitch库 "python3 缺失，无法只读校验"
elif [ ! -r "$CCSWITCH_DB" ]; then
  res WARN 数据/CCSwitch库 "${CCSWITCH_DB} 不可读：中转站费用会回落为 \$x*" "chmod 644 \"$CCSWITCH_DB\"（只读访问需要读权限）"
else
  probe=$(python3 - "$CCSWITCH_DB" <<'PY' 2>/dev/null
import json, math, sqlite3, sys
try:
    con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True, timeout=2)
    n = con.execute('SELECT COUNT(*) FROM proxy_request_logs').fetchone()[0]
except Exception:
    sys.exit(1)                      # 账本表都读不到 = 真的用不了


def limit_state(row):
    """与 statusline-tokens.py 的 current_provider / _limit_usd 同口径。
    → 'ok' 有效 / 'none' 未设置 / 'bad' 值非法或元数据损坏。"""
    _, col, meta = row
    lim = col
    if meta:
        try:
            m = json.loads(meta)
        except Exception:
            return 'bad'             # 元数据坏了：未知，不回落旧列
        if isinstance(m, dict):
            if 'limitDailyUsd' in m:
                lim = m.get('limitDailyUsd')
            # 字典里没有该键：回落旧列（与上游一致）
        else:
            return 'bad'
    if lim is None:
        return 'none'
    try:
        f = float(lim)
    except (TypeError, ValueError):
        return 'bad'
    return 'ok' if math.isfinite(f) and f > 0 else 'bad'


try:
    try:
        rows = con.execute("SELECT id, limit_daily_usd, meta FROM providers "
                           "WHERE app_type='claude' AND is_current=1").fetchall()
    except Exception:                # 旧库没有 meta 列
        rows = con.execute("SELECT id, limit_daily_usd, NULL FROM providers "
                           "WHERE app_type='claude' AND is_current=1").fetchall()
except Exception:
    print(n, '-', '-')               # providers 表 / 列不完整：渲染侧返回 (None, None)
    sys.exit(0)
print(n, len(rows), limit_state(rows[0]) if len(rows) == 1 else '-')
PY
  )
  case "$probe" in
    '') res WARN 数据/CCSwitch库 "无法只读打开 ${CCSWITCH_DB}：中转站费用会回落为 \$x*（标价估算）" \
          "确认没有被其他进程独占，且文件确实是 SQLite 库；本工具不会写它" ;;
    *)  set -- $probe
        case "${2:-}" in
          -) res WARN 数据/CCSwitch库 "只读打开成功（代理日志 $1 行），但 providers 表/列不完整：日限额无法确定、会隐藏" \
                "与渲染侧同口径：不确定就不显示，不拿未知当 0" ;;
          1) case "${3:-}" in
               ok)   res PASS 数据/CCSwitch库 "只读打开成功；代理日志 $1 行；当前供应商唯一且日限额有效" ;;
               none) res SKIP 数据/CCSwitch库 "日限额未设置（当前供应商唯一）：日限额项本来就不显示，属正常配置" ;;
               *)    res WARN 数据/CCSwitch库 "只读打开成功（代理日志 $1 行），当前供应商唯一但日限额值非法：日限额项与预测会隐藏" \
                       "在 CC Switch 里改正当前供应商的日限额，或清空它" ;;
             esac ;;
          *) res WARN 数据/CCSwitch库 "只读打开成功（代理日志 $1 行），但当前供应商为 ${2:-0} 个：日限额与预测会隐藏（未知不当 0）" \
                "在 CC Switch 里选定唯一的当前供应商" ;;
        esac ;;
  esac
fi

if [ "$relay" = 1 ]; then guess=中转站; else guess=订阅; fi
res UNKNOWN 数据/计费模式 "按配置推断为「${guess}」，但权威答案取决于运行时 stdin（有 5h / 7d 额度即订阅）"

# ═══ 汇总 ══════════════════════════════════════════════════════════════
printf '\n汇总：PASS %d，WARN %d，FAIL %d，SKIP %d，UNKNOWN %d\n' \
  "$n_pass" "$n_warn" "$n_fail" "$n_skip" "$n_unk"
if [ "$n_fail" -gt 0 ]; then
  echo "结论：有 $n_fail 项 FAIL，状态栏很可能不工作。按上面的「修」逐项处理。"
  exit 1
elif [ "$n_warn" -gt 0 ]; then
  echo "结论：可以工作，有 $n_warn 项需要留意。"
else
  echo "结论：全部通过。"
fi
exit 0
