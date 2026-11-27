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
# 末行汇总。退出码：0 = 无 FAIL；1 = 有 FAIL 或诊断模块无法加载；2 = 参数错误。
#
# 只读承诺
# --------
# - 不改写用户配置、缓存、数据库与系统文件；唯一的临时写是冒烟用的 mktemp -d（跑完即删）。
# - 不联网：不调用 curl，不访问 status.claude.com。
# - CC Switch 数据库只用 `file:<path>?mode=ro` 打开（与 statusline-tokens.py 约束 7 同口径），
#   且只在中转站模式下打开。WAL 模式的库即使只读打开，SQLite 也可能在库旁建 -wal / -shm
#   旁文件——生产脚本是同样的访问方式，诊断不额外写库内容。
# - 不打印环境变量全集；中转站备注里的 ANTHROPIC_BASE_URL 只给 host（连 user:pass@ 一并去掉）；
#   settings 里的命令行经 scrub() 抹掉 sk-… / ghp_… / *_KEY=… / *_TOKEN=… / *PASSWORD=… /
#   *SECRET=… / *PAT=… / --api-key|--token|--password|--secret / URL 凭据与 query 参数 /
#   API-key 与 Authorization 请求头 / Bearer … 后显示。
# - 正常渲染路径不调用本脚本（statusline-command.sh 与 subagent-statusline.sh 都不引用它，
#   由 tests/test_doctor.py 的契约测试守护）。
#
# 为什么是独立脚本
# ----------------
# install.sh 是写工具（复制 / 备份 / 删除），诊断是读工具；分开才能从结构上保证「默认只读」。
# install.sh --doctor 只是 exec 别名，不重复任何逻辑。
#
# 本入口与 tools/doctor/*.sh 只在 bash ≥ 3.2 上要求可运行（诊断要在最坏环境里也能跑），
# 因此不用关联数组；模块随入口一起 source，同样不得使用 3.2 之后的语法。
# 渲染侧的配置、路径、阈值不在这里另抄：见下面的「事实来源」。

set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${CLAUDE_CONFIG_DIR:-$HOME/.claude}

# 诊断自己的阈值（渲染侧没有对应常量）
PANEL_FAIL_STALE=86400  # 面板写回失败标记超过此秒数视为「此后没有写回尝试」
FACTS_V=3               # 本工具读得懂的自述格式版本（渲染脚本 --facts 的首行）

DIR=$PWD
TRANSCRIPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)        [ $# -ge 2 ] && [ -n "$2" ] || { echo "--dir 需要一个非空路径" >&2; exit 2; }; DIR=$2; shift 2 ;;
    --transcript) [ $# -ge 2 ] && [ -n "$2" ] || { echo "--transcript 需要一个非空路径" >&2; exit 2; }; TRANSCRIPT=$2; shift 2 ;;
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

# 凭据形态从要显示的任何字符串里抹掉；其余内容原样保留（否则用户没法照着修）。
# 认的形态见 docs/DECISIONS.md「凭据过滤在显示层」。*_KEY= / *_TOKEN= 等的值到空白、; " ' & 为止（& 在 shell 里结束这个词、在 URL 里是下一个参数，
# 要留下结构）；# 在词中间是字面字符，不能截断。Authorization 的 scheme 可有可无
scrub() {
  sed -E \
    -e 's/sk-[A-Za-z0-9_-]{8,}/sk-***/g' \
    -e 's/(gh[opusr])_[A-Za-z0-9]{16,}/\1_***/g' -e 's/github_pat_[A-Za-z0-9_]{16,}/github_pat_***/g' \
    -e "s/([A-Za-z0-9_]*([Kk][Ee][Yy]|[Tt][Oo][Kk][Ee][Nn]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt])=)([\"'][^\"']*[\"']|[^ ;\"&']+)/\\1***/g" \
    -e "s/((^|[ ;\"'&_-])[Pp][Aa][Tt]=)([\"'][^\"']*[\"']|[^ ;\"&']+)/\\1***/g" \
    -e "s/(--(api-key|token|password|secret)[ =])([\"'][^\"']*[\"']|[^ ;\"']+)/\\1***/g" \
    -e "s#//[^ \"';]+@#//***@#g" \
    -e "s/([Xx]-[Aa][Pp][Ii]-[Kk][Ee][Yy][ ]*:?[ ]*)([\"'][^\"']*[\"']|[^ \";']+)/\\1***/g" \
    -e "s/(([Aa][Pp][Ii]-[Kk][Ee][Yy]|[Xx]-[Aa][Uu][Tt][Hh]-[Tt][Oo][Kk][Ee][Nn])[ ]*:[ ]*)([\"'][^\"']*[\"']|[^ \";']+)/\\1***/g" \
    -e "s/([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][ ]*:[ ]*([A-Za-z]+[ ]+)?)([\"'][^\"']*[\"']|[^ \";']+)/\\1***/g" \
    -e "s/([Bb][Ee][Aa][Rr][Ee][Rr] )([\"'][^\"']*[\"']|[^ \"';]+)/\\1***/g" \
    -e "s/([?&#])([A-Za-z0-9_.-]+)=[^ ;\"&#']+/\\1\\2=***/g"
}

if [ "${OSTYPE#darwin}" != "$OSTYPE" ] && [ -x /usr/bin/stat ]; then
  mtime() { /usr/bin/stat -f %m "$1" 2>/dev/null; }
  mtimes() { /usr/bin/stat -f %m "$@" 2>/dev/null; }     # 一次取多个文件，每行一个（文件须都存在）
  stat_mode() { /usr/bin/stat -f %Lp "$1" 2>/dev/null; }
  fsize() { /usr/bin/stat -f %z "$1" 2>/dev/null; }
elif stat -c %Y / >/dev/null 2>&1; then
  mtime() { stat -c %Y "$1" 2>/dev/null; }
  mtimes() { stat -c %Y "$@" 2>/dev/null; }
  stat_mode() { stat -c %a "$1" 2>/dev/null; }
  fsize() { stat -c %s "$1" 2>/dev/null; }
else
  mtime() { stat -f %m "$1" 2>/dev/null; }
  mtimes() { stat -f %m "$@" 2>/dev/null; }
  stat_mode() { stat -f %Lp "$1" 2>/dev/null; }
  fsize() { stat -f %z "$1" 2>/dev/null; }
fi

# 三段点分版本比较：ver3_ge <实际> <最低>（如 1.0.9 >= 1.0.9；解析失败返回 1）
ver3_ge() {
  local a1 a2 a3 b1 b2 b3 IFS=.
  read -r a1 a2 a3 <<EOF
$1
EOF
  read -r b1 b2 b3 <<EOF
$2
EOF
  a2=${a2:-0}; a3=${a3:-0}; b2=${b2:-0}; b3=${b3:-0}
  case "$a1$a2$a3$b1$b2$b3" in ''|*[!0-9]*) return 1 ;; esac
  [ "$a1" -ne "$b1" ] && { [ "$a1" -gt "$b1" ]; return; }
  [ "$a2" -ne "$b2" ] && { [ "$a2" -gt "$b2" ]; return; }
  [ "$a3" -ge "$b3" ]
}

# ── 诊断模块：每组检查一个文件，入口只负责共享状态、加载顺序与汇总 ──────
DOCTOR_LIB=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/doctor
for _m in env install cache data; do
  if [ ! -f "$DOCTOR_LIB/$_m.sh" ]; then
    echo "缺少诊断模块：$DOCTOR_LIB/$_m.sh" >&2
    exit 1
  fi
done

# ── 事实来源：渲染脚本自述（--facts）──────────────────────────────────────
# 配置解析结果、缓存路径、阈值与版本下限都去问渲染脚本，不在这里另抄一份。优先问已安装的
# 副本——诊断要回答的是「装着的那一版会怎么做」；它答不出（没装、旧版、被换成别的脚本）
# 再问仓库里的源文件。用跑本工具的同一个 bash 去执行：自述模式在 bash 3.2 上也能工作，
# 且不读 stdin、不写缓存、不派生后台刷新。空 stdin 与 CLAUDE_SL_NO_REFRESH=1 是兜底：
# 万一自述分支失灵、落进普通渲染，也不读终端、不派生刷新。
load_facts() {  # load_facts <脚本> <变量前缀>：首行必须是 cc-pills-facts=<FACTS_V>，否则返回 1
  local k v first=1
  [ -f "$1" ] || return 1
  # 只读承诺：不认识 --facts 的旧副本会把它当普通参数照常跑一遍（建缓存目录、写面板状态），
  # 所以源码里没有自述标记就不执行它。
  case "$(<"$1")" in *cc-pills-facts=*) ;; *) return 1 ;; esac
  while IFS='=' read -r k v; do
    if [ "$first" = 1 ]; then
      first=0
      [ "$k=$v" = "cc-pills-facts=$FACTS_V" ] || return 1
      continue
    fi
    case "$k" in ''|*[!a-z_]*) continue ;; esac     # 键要当变量名用，只收小写字母与下划线
    printf -v "$2$k" '%s' "$v"
  done <<EOF
$(CLAUDE_SL_NO_REFRESH=1 "$BASH" "$1" --facts </dev/null 2>/dev/null)
EOF
  [ "$first" = 0 ]
}
FACTS_SRC=""
for s in "$DEST/statusline-command.sh" "$REPO/src/statusline-command.sh"; do
  if load_facts "$s" F_; then FACTS_SRC=$s; break; fi
done
if [ -z "$FACTS_SRC" ] \
   || ! { load_facts "$DEST/subagent-statusline.sh" P_ || load_facts "$REPO/src/subagent-statusline.sh" P_; }; then
  echo "渲染脚本答不出自述信息（--facts）：$DEST 与 $REPO/src 里的脚本都不可用" >&2
  exit 1
fi
# 首行报版本：已安装副本的；判据若取自仓库源文件（没装 / 旧版答不出）就是仓库的。两者不同
# 时都给出来——「装的是哪一版」是排查的第一个问题。
# COPY_OLD：装着一份答不出这一版 --facts 的副本（多半是旧版）。此时判据取自仓库源文件，
# 而磁盘上的缓存是那份副本写的——与格式版本有关的检查不适用（见 cache.sh）。
COPY_OLD=0
if [ "$FACTS_SRC" != "$REPO/src/statusline-command.sh" ]; then
  ver_note=已安装副本
  R_version=""; load_facts "$REPO/src/statusline-command.sh" R_ || true
  [ -n "$R_version" ] && [ "$R_version" != "$F_version" ] && ver_note="${ver_note}；仓库 ${R_version}"
elif [ -f "$DEST/statusline-command.sh" ]; then
  COPY_OLD=1
  ver_note="取自仓库源文件：已安装副本答不出 --facts，多半是旧版——先重装再诊断：bash $REPO/install.sh"
else
  ver_note="取自仓库源文件：尚未安装"
fi
printf 'cc-pills %s （%s）\n' "$F_version" "$ver_note"
CONF=$F_conf; CCSWITCH_DB=$F_ccswitch_db
THEME_TTL=$F_theme_ttl; TOK_STALE=$F_tok_stale; STATUS_STALE=$F_status_stale; LOCK_STALE=$F_lock_stale
# 装着旧副本时判据取自仓库，可它未必清理锁（1.0.6 及更早不清理）：天数留空，残锁文案改给手工命令
CACHE_MAX_AGE=${F_cache_max_age:-}; [ "$COPY_OLD" = 1 ] && CACHE_MAX_AGE=""
BASH_MIN=$F_bash_min; PY_MIN=$F_py_min; JQ_MIN=${F_jq_min:-${R_jq_min:-}}
C_THEME=$F_theme; C_CHROMA=$F_pill_chroma; C_STYLE=$F_pill_style; C_PAGE=$F_status_page
CFG_READABLE=$F_conf_readable
CFG_BAD=${F_conf_bad:+ $F_conf_bad}; CFG_UNKNOWN=${F_conf_unknown:+ $F_conf_unknown}

. "$REPO/tools/lib/version.sh" || exit 1
. "$REPO/tools/lib/points_to.sh" || { echo "缺少 $REPO/tools/lib/points_to.sh" >&2; exit 1; }

for _m in env install cache data; do
  # shellcheck source=/dev/null
  . "$DOCTOR_LIB/$_m.sh" || { echo "诊断模块加载失败：$DOCTOR_LIB/$_m.sh" >&2; exit 1; }
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
