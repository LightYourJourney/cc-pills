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
#   - stdin：每个刷新 tick 一个 JSON 对象：columns（行可用宽度）+ tasks[]，
#     每个 task 含 id/name/type/status/description/label/startTime/model/
#     effort/contextWindowSize/tokenCount/tokenSamples/cwd
#   - stdout：每行一个 {"id": "<task id>", "content": "<行内容>"}，
#     content 原样渲染（支持 ANSI）；不输出某 id = 该行保留默认渲染
#
# 字段格式（从 Claude Code 二进制核实，版本随升级可能变化）
# ----------------------------------------------------------------------
#   - startTime：epoch 毫秒（任务创建处 `startTime:Date.now()`）
#   - tokenSamples：纯数字数组（每 tick 推入当时 tokenCount，环形缓冲上限
#     NWm=16，面板 tick 常量 VHi=2500ms）。元素无时间戳，换算速率需假设
#     tick 间隔——违背「不显示推算值」原则，故【不使用】；速率改为脚本
#     自记状态实测：上次运行的 (时刻, tokenCount) 存进状态文件，本次求差。
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
#   覆盖别家条目）；抢锁超过 2s 就放弃本次写回。条目超 600s 剪枝，mkstemp/mktemp + mv
#   原子替换，状态文件 0600；entry 更新间隔 ≥2s（事件驱动的密集重跑不冲刷速率基线）。
#   锁路径被普通文件 / 符号链接占住，或残锁超过 5s（含非空目录）时先清掉再重试。
#   缓存根不是本人拥有的真实目录时只渲染、不读写状态。旧版写在共享 /tmp 的状态不迁移
#   （只有速率基线，丢失代价是一帧）。
#
# 工程约束（修改时必须保留）
# --------------------------
#   1. jq 缺失、输入/状态解析失败：静默不输出或按空状态处理，绝不输出错误
#   2. 末尾 true 保证退出码恒 0
#   3. 截断按 codepoint 计，CJK 实占 2 列按 1 计——已知偏差，面板会兜底裁剪
#
# 配套设置键：subagentStatusLine（用户级 settings.json + CC Switch 共享层）
#

command -v jq >/dev/null 2>&1 || { while IFS= read -r _; do :; done; exit 0; }  # 纯 builtin 排空 stdin，不依赖 PATH

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
  [ "$make_root" = 1 ] && [ ! -d "$sroot" ] && mkdir -m 700 "$sroot" 2>/dev/null
  [ -d "$sroot" ] && [ ! -L "$sroot" ] && [ -O "$sroot" ] && state_ok=1
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
printf '%s' "$prev" | jq -e 'type == "object"' >/dev/null 2>&1 || prev='{}'
initprev=$prev                              # 锁内重读后与它比较：未变就省一次 jq

# ── 渲染各行 ────────────────────────────────────────────────────────────
printf '%s' "$input" | jq -c --argjson prev "$prev" '
  (.columns // 120) as $cols
  | (.tasks // [])[]
  | select(.id != null)
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

  # ── 描述按剩余宽度截断（截断符 …）────────────────────────────────────
  | ($cols - ($name | length) - ($tail | length) - ($stTag | length) - 5) as $budget
  | (if $desc == "" or $budget < 4 then ""
     elif ($desc | length) > $budget then ($desc[0:($budget - 1)] + "…")
     else $desc end) as $desc2

  | { id: .id,
      content: ((if $name != "" then "\($name) " else "" end)
                + $desc2
                + (if $tail != "" then "  — " + $tail else "" end)
                + $stTag) }
' 2>/dev/null

# ── 状态写回（共享合并、600s 剪枝、entry 更新间隔 ≥2s、原子替换）────────
# 读-改-写全程持锁，锁内重新读取状态：否则两个实例都用开始时的快照，后写者会覆盖
# 先写者的条目。抢不到锁（>1s）就放弃本次写回——宁少一帧速率，也不丢别人的更新。
sdir=${STATE%/*}; [ "$sdir" = "$STATE" ] && sdir=.
lock="$STATE.lock"; got=0; nowsec=""
dbg() { [ -n "${CLAUDE_SUBAGENT_SL_DEBUG:-}" ] && printf 'slpanel[%s] %s\n' "$$" "$*" >&2; }
dbg "state=$STATE sdir=$sdir exists=$([ -e "$STATE" ] && echo y || echo n) isdir=$([ -d "$STATE" ] && echo y || echo n) sdir_ok=$([ -d "$sdir" ] && echo y || echo n)"
if [ "$state_ok" = 1 ] && [ ! -d "$STATE" ] && [ -d "$sdir" ]; then
  # 40 次 × 50ms ≈ 2s：2 核 CI runner 上 1s 窗口在 30 轮并发里会丢 2 轮（实测），
  # 而丢一轮就是少一帧速率。锁本身只持有几十到几百毫秒，2s 足够，又不至于卡住面板。
  for _ in {1..40}; do
    if mkdir "$lock" 2>/dev/null; then got=1; break; fi
    [ -n "$nowsec" ] || nowsec=${EPOCHSECONDS:-$(date +%s)}   # 只在抢锁失败时才取时间
    # 锁路径被普通文件 / 符号链接占住时 mkdir 永远失败：不是我们的锁，直接清掉重试。
    if [ -L "$lock" ] || { [ -e "$lock" ] && [ ! -d "$lock" ]; }; then
      rm -f "$lock" 2>/dev/null; continue
    fi
    lt=$(mtime "$lock"); lt=${lt:-0}
    dbg "mkdir-fail kind=$([ -L "$lock" ] && echo link || { [ -d "$lock" ] && echo dir || echo other; }) mtime=$lt now=$nowsec empty=$([ -z "$(find "$lock" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ] && echo y || echo n)"
    if [ "$lt" -gt 0 ] && [ $((nowsec - lt)) -ge 5 ]; then
      rm -rf "$lock" 2>/dev/null; continue          # 残锁 / 非空异常目录（被杀的进程遗留）
    fi
    sleep 0.05
  done
fi
dbg "got=$got"
if [ "$got" = 1 ]; then
  trap 'rmdir "$lock" 2>/dev/null' EXIT
  prev='{}'
  [ -f "$STATE" ] && [ ! -L "$STATE" ] && [ -O "$STATE" ] && prev=$(< "$STATE")
  [ "$prev" = "$initprev" ] || { printf '%s' "$prev" | jq -e 'type == "object"' >/dev/null 2>&1 || prev='{}'; }
  new=$(printf '%s' "$input" | jq -c --argjson prev "$prev" '
    now as $n
    | ($prev | with_entries(select(
        (.value | type == "object") and ((.value.t // 0) > ($n - 600))))) as $base
    | reduce ((.tasks // [])[] | select(.id != null)
              | select((.status // "running" | tostring) as $s
                       | $s == "running" or $s == "" or $s == "pending")) as $t
        ($base;
         $t.id as $i
         | if ((.[$i] | type) != "object") or (($n - .[$i].t) >= 2)
           then .[$i] = {t: $n, k: ($t.tokenCount // 0)}
           else . end)
  ' 2>/dev/null)
  if [ -n "$new" ]; then
    tmp=$(mktemp "$STATE.XXXXXX" 2>/dev/null)
    [ -n "$tmp" ] && printf '%s' "$new" > "$tmp" 2>/dev/null \
      && mv -f "$tmp" "$STATE" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  fi
  rmdir "$lock" 2>/dev/null
fi

true
