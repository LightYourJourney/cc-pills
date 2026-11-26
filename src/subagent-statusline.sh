#!/usr/bin/env bash
#
# Claude Code subagent 状态行脚本
# ================================
#
# 渲染 agent 面板中每个 subagent 行，替代默认的「name · description · tokens」：
#   graphify 提取 chunk 1/9  — opus-5 · tok 145k ↑480/s · ctx 剩85% · 4m04s
#   └─任务标签（按宽截断）      └─模型  └─已用token+自测速率 └─其上下文剩余 └─运行时长
#
# ⚠️ 纯文本输出：官方文档称面板支持 ANSI 颜色，但实测（2026-08-07）转义码被
# 当作字面文本显示（`[1;36m` 直接上屏）。故全部段落零 ANSI；ctx <20% 用 ⚠
# 标记替代红色。若未来版本真渲染 ANSI 再考虑加回。
#
# 契约（来源：code.claude.com/docs/en/statusline · Subagent status lines）
# ----------------------------------------------------------------------
# ⚠️ 上游已弃用这个设置键：Claude Code 2.1.330–2.1.332 的二进制里有
#   「subagentStatusLine is deprecated in favor of mods and will stop working in a
#   future release」，每个会话都会提示一次，官方文档也已删掉这一节。替代品是 mod 的
#   AgentRow 渲染点（同样的数据 + 面板实测的 columns）。在决定迁移前本脚本继续维护。
#   - stdin：每个刷新 tick 一个 JSON 对象：columns（行可用宽度）+ tasks[]，
#     每个 task 含 id/name/type/status/description/label/startTime/model/
#     effort/contextWindowSize/tokenCount/tokenSamples/cwd
#   - stdout：每行一个 {"id": "<task id>", "content": "<行内容>"}，
#     content 原样渲染（支持 ANSI）；不输出某 id = 该行保留默认渲染
#
# 字段格式（从 Claude Code 二进制核实，版本随升级可能变化）
# ----------------------------------------------------------------------
#   - startTime：epoch 毫秒（任务创建处 `startTime:Date.now()`）
#   - tokenSamples：纯数字数组（最后 16 次 tokenCount，文档称「约五秒一次」）。
#     元素无时间戳，换算速率要假设 tick 间隔——违背「不显示推算值」原则，故
#     【不使用】；速率改为脚本自记状态实测：上次运行的 (时刻, tokenCount)
#     存进状态文件，本次求差。
#
# ⚠️ 估算值声明（与主状态栏脚本同一原则：字段缺失/不可靠即整段隐藏）
# ----------------------------------------------------------------------
#   - ctx 段需 contextWindowSize/tokenCount（v2.1.205+，模型未解析时缺失）
#   - effort 段需 v2.1.214+，继承会话档位时缺失
#   - 时长段仅在 startTime 为可信 epoch 毫秒（>1e12）且任务 running/pending
#     时显示；速率段仅在有上次状态、间隔 ≥1s、增量非负且 ≥1 tok/s 时显示
#
# 状态文件：显式 CLAUDE_SUBAGENT_SL_STATE 优先；否则跟随主栏缓存根——
#   $CLAUDE_SL_TMP/claude-subagent-sl-<uid>.json（调用方建好目录），或
#   ${TMPDIR:-/tmp}/claude-sl-<uid>/claude-subagent-sl-<uid>.json（需要时 0700 创建）。
#   多会话共享：读-改-写全程在同一把 mkdir 锁内，锁内重新读取后再合并（不会用旧快照
#   覆盖别家条目）；抢锁超过 1s 就放弃本次写回。条目超 600s 剪枝，mkstemp/mktemp + mv
#   原子替换，状态文件 0600；entry 更新间隔 ≥2s（事件驱动的密集重跑不冲刷速率基线）。
#   锁路径被普通文件 / 符号链接占住时先清掉再重试；残锁超过 5s 后回收，但有 owner 且
#   持有者 PID 仍存活时只放弃本轮、不动别人的锁。
#   锁里记 owner（PID.token），释放只删自己的锁。
#   写回失败落 STATE.fail（单行 "epoch 原因码"），成功写回时删除；原因码供诊断工具解码。
#   显式 CLAUDE_SUBAGENT_SL_STATE 时标记同样落在状态文件旁，生命周期归调用方。
#   缓存根不是本人拥有的真实目录时只渲染、不读写状态。旧版写在共享 /tmp 的状态不迁移
#   （只有速率基线，丢失代价是一帧）。
#
# 工程约束（修改时必须保留）
# --------------------------
#   1. jq 缺失、输入/状态解析失败：静默不输出或按空状态处理，绝不输出错误
#   2. 末尾 true 保证退出码恒 0
#   3. 截断按显示宽度算（1/2 字节 UTF-8 字符 1 列，3/4 字节 2 列，与主栏 dw() 同模型）；
#      emoji 组合序列仍可能被切开（不产生乱码，只是显示残缺）
#
# 配套设置键：subagentStatusLine（用户级 settings.json + CC Switch 共享层）
#

# --facts（自述模式，见下）不需要 jq，也不读 stdin
[ "${1:-}" = --facts ] || command -v jq >/dev/null 2>&1 || { while IFS= read -r _; do :; done; exit 0; }  # 纯 builtin 排空 stdin，不依赖 PATH

umask 077                                    # 状态文件只给本人读写
# 显式 CLAUDE_SUBAGENT_SL_STATE 最高优先；否则与主栏共用私有根，让
# CLAUDE_SL_TMP（预览 / 测试）只设一处就能同时隔离两套缓存。
state_ok=0; make_root=0
if [ -n "${CLAUDE_SUBAGENT_SL_STATE:-}" ]; then
  STATE=$CLAUDE_SUBAGENT_SL_STATE; state_ok=1   # 显式文件：父目录由调用方负责
else
  if [ -n "${CLAUDE_SL_TMP:-}" ]; then
    sroot=${CLAUDE_SL_TMP%/}
  else
    t=${TMPDIR:-/tmp}; t=${t%/}
    sroot="$t/claude-sl-${UID:-0}"; make_root=1
  fi
  STATE="$sroot/claude-subagent-sl-${UID:-0}.json"
  [ "$make_root" = 1 ] && [ "${1:-}" != --facts ] && [ ! -d "$sroot" ] && mkdir -m 700 "$sroot" 2>/dev/null
  [ -d "$sroot" ] && [ ! -L "$sroot" ] && [ -O "$sroot" ] && state_ok=1
fi
# 自述模式：把状态文件路径报给仓库里的诊断工具（它不必再抄一份路径规则）。只读，不建目录。
if [ "${1:-}" = --facts ]; then
  printf '%s\n' "cc-pills-facts=3" "state=$STATE" "state_explicit=$([ -n "${CLAUDE_SUBAGENT_SL_STATE:-}" ] && echo 1 || echo 0)"
  exit 0
fi

# 残锁判定：lock 目录的 mtime（BSD / GNU stat 顺序同主脚本，不能反）
if [ "${OSTYPE#darwin}" != "$OSTYPE" ] && [ -x /usr/bin/stat ]; then
  mtime() { /usr/bin/stat -f %m "$1" 2>/dev/null; }
else
  mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
fi

input=$(cat 2>/dev/null)

prev='{}'
# $(<file) 是内建读，不 fork；状态文件必须是本人的普通文件（不跟随符号链接）。
[ "$state_ok" = 1 ] && [ -f "$STATE" ] && [ ! -L "$STATE" ] && [ -O "$STATE" ] &&
  prev=$(< "$STATE")
# 必须恰好一个对象：-e 只看最后一个输出，「{} {}」能过 type 校验，随后 --argjson 拒收多值，
# 渲染与合并的 jq 每轮都失败、状态永不修复
is_obj() { printf '%s' "$1" | jq -se 'length == 1 and (.[0] | type == "object")' >/dev/null 2>&1; }
is_obj "$prev" || prev='{}'
initprev=$prev                              # 锁内重读后与它比较：未变就省一次 jq

# ── 渲染各行 ────────────────────────────────────────────────────────────
printf '%s' "$input" | jq -c --argjson prev "$prev" '
  # 显示宽度：与主栏 dw() 同一模型——1/2 字节 UTF-8 字符各占 1 列，3/4 字节占 2 列。
  # jq 的 length 数的是码点，中文与 emoji 只算 1，按它分预算会让中文描述超宽，
  # 面板于是把行尾（tok、ctx 剩余、时长）裁掉。
  # 已知按 1 列显示的 3 字节符号（与主栏 dw() 里被剔除的那一组同源）
  def narrow: . == 8230 or . == 8593 or . == 8595 or . == 8634 or . == 8635
              or . == 8673 or . == 8675;
  def cw: if narrow then 1 elif . > 2047 then 2 else 1 end;
  def dw: [explode[] | cw] | add // 0;
  def cut($n):                                # 截到显示宽度 ≤ $n（含截断符 …）
    if dw <= $n then .
    elif $n < 2 then ""
    else (reduce explode[] as $c ({s: "", w: 0};
            if .w > $n then .
            else ([$c] | implode) as $ch | ($c | cw) as $w
              | if .w + $w > $n - 1 then {s: .s, w: $n + 1}
                else {s: (.s + $ch), w: (.w + $w)} end
            end)
          | .s) + "…"
    end;
  (.columns | if type == "number" then . else 120 end) as $cols
  | (.tasks // [])[]
  # 逐行兜底：某个 task 的字段类型不对（tokenCount 是字符串、description 是数字……）只让那一行
  # 回到默认渲染；不兜的话整个 jq 中止，坏行之后的行全部丢失
  | try (
    select(.id | type == "string")             # 契约里 id 是字符串；别的类型拿去索引 $prev 会出错
  | (.status // "" | tostring) as $st
  | ($st == "" or $st == "running" or $st == "pending") as $live

  # ── ctx 剩余百分比（纯文本；<20% 追加 ⚠ 提醒）──────────────────────
  | (if (.tokenCount != null) and ((.contextWindowSize // 0) > 0)
     then ((100 - (.tokenCount * 100 / .contextWindowSize)) | floor
           | if . < 0 then 0 else . end)
     else null end) as $pct
  | (if $pct == null then null
     elif $pct < 20 then "ctx 剩\($pct)%⚠"
     else "ctx 剩\($pct)%" end) as $ctxS

  # ── 运行时长（startTime 为 epoch 毫秒；非 running/pending 不显示）────
  | (if $live and ((.startTime // null) | type == "number") and (.startTime > 1e12)
     then ((now - .startTime / 1000) | floor | if . < 0 then 0 else . end)
     else null end) as $dur
  | (if $dur == null then null
     elif $dur < 60   then "\($dur)s"
     elif $dur < 3600 then "\(($dur / 60) | floor)m\(("0" + (($dur % 60) | tostring))[-2:])s"
     else "\(($dur / 3600) | floor)h\(("0" + ((($dur % 3600) / 60 | floor) | tostring))[-2:])m"
     end) as $durS

  # ── token 增速（自记状态实测：距上次 ≥1s、增量非负、≥1 tok/s 才显示）──
  | ($prev[.id] // null) as $p
  | (if ($st == "running") and ($p | type == "object")
        and (($p.t | type) == "number") and (($p.k | type) == "number")
        and ((now - $p.t) >= 1) and (((.tokenCount // 0) - $p.k) >= 0)
     then (((.tokenCount // 0) - $p.k) / (now - $p.t))
     else null end) as $r
  | (if $r == null or $r < 1 then null
     elif $r >= 1000 then "↑\((($r / 100) | floor) / 10)k/s"
     else "↑\($r | round)/s" end) as $rateS

  # ── 其余各段（纯文本，无 ANSI——面板会把转义码当字面文本显示）─────────
  | (if .tokenCount == null then ""
     elif .tokenCount >= 1000 then "\(.tokenCount / 1000 | floor)k"
     else "\(.tokenCount)" end) as $tok
  | ((.model // "") | sub("^claude-"; "") | sub("\\[1m\\]$"; "")) as $model
  | ((.effort // "") | tostring) as $eff
  | ((.name // "") | tostring) as $rawname
  | ((.label // .description // "")) as $desc
  # name 通常是无信息量的 "agent"，只有携带真实名字时才显示
  | (if $rawname != "" and $rawname != "agent" and ($desc | contains($rawname) | not)
     then $rawname else "" end) as $name
  | (if $tok == "" then "" else ("tok \($tok)" + (if $rateS then " \($rateS)" else "" end)) end) as $tokS

  | ([ (if $model != "" then $model else empty end),
       (if $eff   != "" then $eff   else empty end),
       (if $tokS  != "" then $tokS  else empty end),
       (if $ctxS != null then $ctxS else empty end),
       (if $durS != null then $durS else empty end)
     ] | join(" · ")) as $tail
  | (if $st != "" and $st != "running" then " [\($st)]" else "" end) as $stTag
  | (if $name != "" then "\($name) " else "" end) as $head
  | (if $tail != "" then "  — " + $tail else "" end) as $sepTail

  # ── 描述按剩余显示宽度截断（截断符 …）────────────────────────────────
  # 预算用各段实际拼出的字符串算，不再用魔法数（— 在 dw 里就算 2 列）；$tail 里有
  # 「ctx 剩85%」这样的中文，按码点算预算会偏窄
  | ($cols - ($head | dw) - ($sepTail | dw) - ($stTag | dw)) as $budget
  | (if $desc == "" or $budget < 4 then "" else ($desc | cut($budget)) end) as $desc2

  | { id: .id, content: ($head + $desc2 + $sepTail + $stTag) }
  ) catch empty
' 2>/dev/null

# ── 状态写回（共享合并、600s 剪枝、entry 更新间隔 ≥2s、原子替换）────────
# 读-改-写全程持锁，锁内重新读取状态：否则两个实例都用开始时的快照，后写者会覆盖
# 先写者的条目。抢不到锁（1s）就放弃本次写回——宁少一帧速率，也不丢别人的更新。
# 锁里记 owner（PID.token）：释放只删自己的锁；残锁超 5s 后回收，有 owner 且持有者
# PID 仍存活时不抢（owner 缺失说明创建者没走到那步，直接回收）。
sdir=${STATE%/*}; [ "$sdir" = "$STATE" ] && sdir=.
# 写回失败落 STATE.fail（单行 "epoch 原因码"），供诊断工具读取，不再推演锁目录。
# 标记是符号链接时先删链接本身：mv -f 对「指向目录的链接」会把文件搬进目标目录。
# 真目录不是我们的标记，不往里写（诊断会报）。
mark_fail() {
  local mark="${STATE}.fail" t
  [ -L "$mark" ] && rm -f "$mark" 2>/dev/null
  [ -d "$mark" ] && return 0
  t=$(mktemp "$sdir/.claude-sl-panel-fail.XXXXXX" 2>/dev/null) || return 0
  { printf '%s %s\n' "${EPOCHSECONDS:-$(date +%s)}" "$1" > "$t" &&
    mv -f "$t" "$mark"; } 2>/dev/null || rm -f "$t" 2>/dev/null
  return 0
}
# 空 stdin（手工运行）没有可记的 task；目录不可写时 mkdir 必然失败——两种情况都不抢锁，
# 免得白等 1 秒，也不把它们记成写回失败（后者由诊断的 缓存/后台刷新 报）。
lock="$STATE.lock"; got=0; tried=0; nowsec=""
if [ -n "$input" ] && [ "$state_ok" = 1 ] && [ ! -d "$STATE" ] && [ -d "$sdir" ] && [ -w "$sdir" ]; then
  tried=1
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if mkdir "$lock" 2>/dev/null; then got=1; break; fi
    [ -n "$nowsec" ] || nowsec=${EPOCHSECONDS:-$(date +%s)}   # 只在抢锁失败时才取时间
    # 锁路径被普通文件 / 符号链接占住时 mkdir 永远失败：不是我们的锁，直接清掉重试。
    if [ -L "$lock" ] || { [ -e "$lock" ] && [ ! -d "$lock" ]; }; then
      rm -f "$lock" 2>/dev/null; continue
    fi
    lt=$(mtime "$lock"); lt=${lt:-0}
    if [ "$lt" -gt 0 ] && [ $((nowsec - lt)) -ge 5 ]; then
      # 超 5 秒的残锁：持有者 PID 还活着就继续等（PID 复用最多让本次放弃），否则回收
      # （无 owner 说明创建者没走到写 owner 那步）。
      owner=""; [ -f "$lock/owner" ] && owner=$(< "$lock/owner")
      pid=${owner%%.*}
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then sleep 0.05; continue; fi
      rm -rf "$lock" 2>/dev/null; continue          # 残锁 / 非空异常目录（被杀的进程遗留）
    fi
    sleep 0.05
  done
fi
release_lock() {                                  # 只删自己的锁（token 不符时不动）
  owner=""; [ -f "$lock/owner" ] && owner=$(< "$lock/owner")
  [ "$owner" = "$tok" ] && rm -rf "$lock" 2>/dev/null
}
if [ "$got" = 1 ]; then
  tok="$$.${RANDOM:-0}"
  printf '%s' "$tok" > "$lock/owner" 2>/dev/null
  trap release_lock EXIT
  prev='{}'
  [ -f "$STATE" ] && [ ! -L "$STATE" ] && [ -O "$STATE" ] && prev=$(< "$STATE")
  [ "$prev" = "$initprev" ] || is_obj "$prev" || prev='{}'
  new=$(printf '%s' "$input" | jq -c --argjson prev "$prev" '
    now as $n
    | ($prev | with_entries(select(
        (.value | type == "object") and ((.value.t // 0) > ($n - 600))))) as $base
    | reduce ((.tasks // [])[] | objects | select(.id | type == "string")
              | select((.status // "running" | tostring) as $s
                       | $s == "running" or $s == "" or $s == "pending")) as $t
        ($base;
         $t.id as $i
         | if ((.[$i] | type) != "object") or (($n - .[$i].t) >= 2)
           then .[$i] = {t: $n, k: ($t.tokenCount // 0)}
           else . end)
  ' 2>/dev/null)
  case $new in *$'\n'*) new="" ;; esac       # stdin 违约给了多个对象：多行结果不能落盘
  if [ -n "$new" ]; then
    tmp=$(mktemp "$sdir/.claude-sl-panel.XXXXXX" 2>/dev/null)   # .claude-sl-*：残留由主栏每日清理收走
    if [ -z "$tmp" ]; then mark_fail tmp
    elif ! printf '%s' "$new" > "$tmp" 2>/dev/null; then rm -f "$tmp"; mark_fail write
    elif ! mv -f "$tmp" "$STATE" 2>/dev/null; then rm -f "$tmp"; mark_fail mv
    else { [ -e "${STATE}.fail" ] || [ -L "${STATE}.fail" ]; } && rm -f "${STATE}.fail" 2>/dev/null
    fi
  else
    mark_fail merge
  fi
  release_lock
elif [ "$tried" = 1 ]; then
  # 真正尝试过抢锁才记 busy；没尝试是路径 / 权限问题，由 缓存/根 与 缓存/面板状态 报
  mark_fail busy
fi

true
