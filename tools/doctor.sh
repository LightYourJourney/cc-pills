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
# - 不打印环境变量全集；中转站备注里的 ANTHROPIC_BASE_URL 只给 host（连 user:pass@ 一并去掉）；
#   settings 里的命令行经 scrub() 抹掉 sk-… / *_KEY=… / *_TOKEN=… / URL 凭据与 query 参数 /
#   Bearer … 后显示。
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
STATUS_STALE=600     # 状态页缓存超此秒数未更新即隐藏
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
    -e "s/([A-Za-z0-9_]*([Kk][Ee][Yy]|[Tt][Oo][Kk][Ee][Nn])=)([\"'][^\"']*[\"']|[^ ;\"&#']+)/\\1***/g" \
    -e 's#//[^/@ ]+@#//***@#g' \
    -e "s/([Xx]-[Aa]pi-[Kk]ey[ ]*:?[ ]*)([\"'][^\"']*[\"']|[^ \";']+)/\\1***/g" \
    -e "s/([Aa]uthorization[ ]*:[ ]*[A-Za-z]+[ ]+)([\"'][^\"']*[\"']|[^ \";']+)/\\1***/g" \
    -e "s/([Bb]earer )([\"']?)[^ \"';]+/\\1\\2***/g" \
    -e "s/([?&#])([A-Za-z0-9_.-]+)=[^ ;\"&#']+/\\1\\2=***/g"
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
  a2=${1#*.}; [ "$a2" = "$1" ] && a2=0; a2=${a2%%.*}      # 阈值写成 5 时 minor 按 0
  b2=${2#*.}; [ "$b2" = "$2" ] && b2=0; b2=${b2%%.*}
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
if [ -f "$CONF" ] && [ -r "$CONF" ]; then
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

# ── 诊断模块：每组检查一个文件，入口只负责共享状态、加载顺序与汇总 ──────
DOCTOR_LIB=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/doctor
for _m in env install cache data; do
  . "$DOCTOR_LIB/$_m.sh" || { echo "缺少诊断模块：$DOCTOR_LIB/$_m.sh" >&2; exit 2; }
done

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
