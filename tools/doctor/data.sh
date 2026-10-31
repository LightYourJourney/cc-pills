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
  # 差分只能证明 stdin 进了进程；只提取固定几个字段的占位脚本也会因两份夹具不同而通过。
  # 目录与 ctxin 是渲染脚本默认不降级的两项，要求它们都出现，才算主渲染路径真的跑了。
  dir_a="${scratch##*/}/a"; dir_b="${scratch##*/}/b"
  ha=0; hb=0; ca=0; cb=0
  case "$outA" in *"$dir_a"*) ha=1;; esac
  case "$outA" in *'123k'*) ca=1;; esac
  case "$outB" in *"$dir_b"*) hb=1;; esac
  case "$outB" in *'654k'*) cb=1;; esac
  # 非空 + 不是最简行 + 两份夹具结果不同 + 目录与 ctxin 都真的被渲染出来
  case "$outA" in
    '')            res FAIL 数据/stdin "冒烟无输出（${smoke}）" \
                     "依次看 环境/jq、环境/bash、安装/副本-statusline-command.sh" ;;
    'Opus 5.5 · 37%') res FAIL 数据/stdin "只渲染出版本守卫的最简行（模型 · ctx%）：主渲染路径没跑起来" \
                     "看 环境/bash：PATH 里的 bash 需要 ≥${BASH_MIN}，或让脚本换到 ≥5 的 bash" ;;
    *'"session_id"'*|*'"context_window"'*|*'"prompt_cache"'*)
                     res FAIL 数据/stdin "输出包含 stdin 原文（${smoke}）：疑似透传/占位脚本，主渲染路径没有执行" \
                     "确认该文件是 cc-pills 的 statusline-command.sh，而不是包装脚本或占位脚本" ;;
    *) if [ "$outA" = "$outB" ]; then
         res FAIL 数据/stdin "两份不同夹具渲染出相同结果：stdin 没有真正进入渲染（${smoke}）" \
           "确认该文件是 cc-pills 的 statusline-command.sh，而不是包装脚本或占位脚本"
       elif [ "$ha$ca$hb$cb" != 1111 ]; then
         res FAIL 数据/stdin "缺少目录或 ctxin 的渲染形态（目录 ${ha}/${hb}，ctxin ${ca}/${cb}）：疑似占位脚本，或显示项被全部 drop（${smoke}）" \
           "确认该文件是 cc-pills 的 statusline-command.sh；若你确实 drop 了 dir 与 ctxin，可忽略此项"
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
# 才注入到状态栏进程；因此 settings 有值时以它为准（shell 里的同名变量可能只是旧的官方地址），
# 没有 settings 值时才回退到 shell。
base=""; base_src=""
if [ "$SJ_OK" = 1 ]; then
  base=$(jq -r '.env.ANTHROPIC_BASE_URL // empty' "$sj" 2>/dev/null)
  [ -n "$base" ] && base_src="settings.json 的 env"
fi
if [ -z "$base" ]; then
  base=${ANTHROPIC_BASE_URL:-}; base_src="shell 环境变量"
fi
relay=0
case "$base" in
  ''|https://api.anthropic.com|https://api.anthropic.com/*) ;;
  *) relay=1 ;;
esac
if [ "$relay" = 1 ]; then
  host=${base#*://}; host=${host%%/*}
  host=${host##*@}                       # 去掉 user:pass@，凭据不进屏幕
  host=${host%%[?#]*}                    # query / fragment 也不进屏幕，只给 host
  note="ANTHROPIC_BASE_URL=${host}（${base_src}）"
elif [ "$SJ_OK" = 1 ]; then
  note="未设置自定义 ANTHROPIC_BASE_URL（shell 环境变量与 settings.json 的 env 都查过）"
else
  note="未设置自定义 ANTHROPIC_BASE_URL（shell 环境变量已查；settings.json 无法读取）"
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
    print(n, '-', '-', sep='\t')     # providers 表 / 列不完整：渲染侧返回 (None, None)
    sys.exit(0)
print(n, len(rows), limit_state(rows[0]) if len(rows) == 1 else '-', sep='\t')
PY
  )
  case "$probe" in
    '') res WARN 数据/CCSwitch库 "无法只读打开 ${CCSWITCH_DB}：中转站费用会回落为 \$x*（标价估算）" \
          "确认没有被其他进程独占，且文件确实是 SQLite 库；本工具不会写它" ;;
    *)  p_n=""; p_cnt=""; p_state=""
        IFS=$'\t' read -r p_n p_cnt p_state <<EOF
$probe
EOF
        case "$p_cnt" in
          -) res WARN 数据/CCSwitch库 "只读打开成功（代理日志 ${p_n} 行），但 providers 表/列不完整：日限额无法确定、会隐藏" \
                "与渲染侧同口径：不确定就不显示，不拿未知当 0" ;;
          1) case "$p_state" in
               ok)   res PASS 数据/CCSwitch库 "只读打开成功；代理日志 ${p_n} 行；当前供应商唯一且日限额有效" ;;
               none) res SKIP 数据/CCSwitch库 "日限额未设置（当前供应商唯一）：日限额项本来就不显示，属正常配置" ;;
               *)    res WARN 数据/CCSwitch库 "只读打开成功（代理日志 ${p_n} 行），当前供应商唯一但日限额值非法：日限额项与预测会隐藏" \
                       "在 CC Switch 里改正当前供应商的日限额，或清空它" ;;
             esac ;;
          *) res WARN 数据/CCSwitch库 "只读打开成功（代理日志 ${p_n} 行），但当前供应商为 ${p_cnt:-0} 个：日限额与预测会隐藏（未知不当 0）" \
                "在 CC Switch 里选定唯一的当前供应商" ;;
        esac ;;
  esac
fi

if [ "$relay" = 1 ]; then guess=中转站; else guess=订阅; fi
res UNKNOWN 数据/计费模式 "按配置推断为「${guess}」，但权威答案取决于运行时 stdin（有 5h / 7d 额度即订阅）"

