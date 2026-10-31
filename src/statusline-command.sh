#!/usr/bin/env bash
#
# cc-pills 主状态栏（表面色胶囊单行 · 响应式 · 缓存渲染 + 后台刷新）
# =================================================================
#
# 渲染示例（去色；( ) 为胶囊圆角端帽）：
#   宽  (ccchat  main [+?]) (Opus 5.5 high) (ctx 42% 419k ↻1) (5h 60% ↺2h26m out 1h42m)
#       (7d 82% ↺2d3h out 1d1h) ($17.54 $4.9/h) (cache 20m hit 97.4% ↑30.1M ↓290k api 52m)
#   窄  (ccchat  main) (O5.5·H) (ctx42% 419k) (5h60%) (99% ↑30.1M)
#   中转站：($3.21 $1.8/h) ($23.40/$50 out 3h10m)；估算 $17.54*、下限 $0.42+
#   告警胶囊：(cache 8m +275k) (claude major)
#
# 设计原因与沿革见 docs/DECISIONS.md；这里只写改代码必须遵守的契约。
#
# 架构
# ----
# 同步路径（每次刷新执行，目标 <20 ms）：
#   stdin → 1 次 jq 提取全部字段 → 读缓存（配色 / git / token / 中转站 / claude 状态）
#   → 按降级序列缩短 / 丢弃、放得下后按逆序回填 → 组装输出 → 脱钩后台刷新进程 → 退出。
# 后台刷新（--refresh 模式，本脚本自调用；同会话同一时刻只跑一个，见约束 9）：
#   （下列文件都在 $SLT：默认 ${TMPDIR:-/tmp}/claude-sl-<uid>（0700，只读本人）；
#     预览 / 测试用 CLAUDE_SL_TMP 指定隔离目录。旧版写在 /tmp 的本会话文件在首次刷新时迁入；
#     默认根内 30 天没动过的会话 / 路径缓存由后台每天清一次，显式根由调用方负责）
#     $SLT/claude-sl-theme-<终端键>.cache  （statusline-theme.py 产出的完整色板，首行
#                                           THEME=<主题>/<彩度>/<风格>/v<PALETTE_V>；THEME_TTL 节流）
#     $SLT/claude-sl-git-<路径键>.cache    （repo_root \t branch \t 状态旗标 \t 写入时刻；GIT_TTL 节流，
#                                           .git/HEAD 或 index 比缓存新则立即重算；路径键见 pathkey）
#     $SLT/claude-sl-tok-<sid>.cache       （statusline-tokens.py 的十八段输出，见其文档；
#                                           旁的 .cache.ts 是写入时刻，超过 TOK_STALE 秒
#                                           同步路径丢弃派生项，只保留 stdin 的额度百分比）
#     $SLT/claude-sl-status.cache          （拉取时刻 epoch \t indicator，全会话共享）
#   并发刷新幂等（同输入同输出 + mv 原子替换）；被 abort 杀掉的刷新进程由下一周期重试补上。
#   输入（transcript 大小 / 费用 / 额度）都没变且距上次不足 TOK_MIN_GAP 秒时跳过 python。
#
# 外观契约
# --------
# 每组一个圆角胶囊（U+E0B6 / U+E0B4），组内各项以单个空格分隔，组间一个空格。胶囊底色按组
# 取（pcolor：P_GIT / P_MODEL / P_CTX / P_Q5 / P_Q7 / P_USD / P_TAIL，告警胶囊用 P_NEU），
# 具体颜色由 PILL_STYLE 决定。区分靠文字三层：
#   - 标签（ctx / 5h / 7d / hit / api）用组色：ctx 绿、额度紫、费用青、尾段弱化；
#     降饱和且避开黄红色相，彩度封顶在告警色的 75%，标签再多也不压过告警。
#   - 主值（百分比）前景色加粗；会话费用用费用组色（字重 W_ACC）；次值（ctx 量、↻、
#     重置倒计时、$/h）灰；三级统计（hit、io、api）更弱。
#   - 语义色只给需要注意的那一项：提醒（out 预测、cache 将过期、ctx ≥ CTX_WARN）黄；
#     危险（额度已用 ≥ BAD_AT、很快用完、cache 已冷、ctx ≥ CTX_BAD、Claude major）红且加粗；
#     out 按紧迫度单独定色：已用 ≥ BAD_AT 只让百分比变红，很快用完 out 才红。
# 胶囊风格：tint（默认）/ cool / jewel / glass / mono / pastel / tokyo-glass，见
# statusline-theme.py 的 STYLES。色板由 statusline-theme.py 运行时计算并做对比度验收；
# THEME=auto 时读 iTerm 当前 profile 的 ANSI 色识别终端主题。
# 非 truecolor 终端自动降为 256 色（xterm 色块六档 + 24 级灰阶取最近）；无 Nerd Font 时设
# GLYPHS=ascii（端帽改为带胶囊底色的空格，宽度不变）。
#
# 费用口径
# --------
#   模式判定：stdin 有 5h / 7d 额度 → 订阅；否则设了非官方 ANTHROPIC_BASE_URL → 中转站；
#   都没有 → 按订阅处理（订阅会话首个 API 响应之前还没有 rate_limits）。
#   - 订阅：显示 $x——Claude Code 按标价估算的【等价价值】，订阅用户并不为它付费。
#   - 中转站：CC Switch 账本 + 尚未入账的尾部（实时），由 statusline-tokens.py 计算。
#     账本两源（代理行 / 会话日志行）互补、已由 CC Switch 去重；尾部按 CC Switch 的价格表
#     与计价规则自算。有未计价消息（模型不在价格表）时显示「$x+」下限。另显示今日
#     中转站花费（无标签，紧跟在会话费用之后）；CC Switch 给当前供应商设了日限额
#     （providers.meta.limitDailyUsd，旧库回落 limit_daily_usd 列）时显示「$x/$限额」。
#     限额的分子只算当前供应商可归属的今日费用（provider_id=它 的 proxy 行 + 当日
#     rollup），不含 '_session' 的会话日志与未入账尾部；供应商无法唯一确定时整项隐藏。
#   - 拿不到 CC Switch 数据（未装 / 读库失败）：回落「$x*」（Claude Code 标价估算）。
#   - 网关花费限额（rate_limits.spend_limit）：显示「今日 / 本周 / 本月 $已用/$限额」
#     （无美元时显示百分比），按节奏告警；存在时优先于 CC Switch 的日限额。
#   标记一律用后缀 ASCII：≈ / ≥ 是歧义宽度字符，紧贴 $ 时字形挤在一起。
#
# ⚠️ 估算值声明
# -------------
# ctx / 额度 / hit / io / 费用均为客户端估算值，不是账单权威值。任一字段缺失时对应项
# 【整项隐藏】，绝不显示推算值或占位数字。字段口径：
#   - ctx：context_window.used_percentage（已用；缺失时回退 100-remaining）；绝对值
#     total_input_tokens 是「当前上下文」（= input + cache 读写，非累计）。不显示窗口大小。
#   - 5h / 7d：rate_limits.five_hour / seven_day 的已用 %（账号级）+ resets_at 倒计时；
#     节奏告警与「out 预计用完」由 statusline-tokens.py 计算（窗口均速与近期速率取大）。
#     低信息量时隐藏：5h 的重置倒计时、以及整个 7d，只在已用 ≥ WARN_AT 或节奏告警时出现。
#     已用 ≥ 100% 判最高级并显示「用尽」。
#   - $/h：statusline-tokens.py 对费用做半衰期 20 分钟的 EWMA。
#   - cache：prompt_cache.expires_at 倒计时（TTL 取 prompt_cache.ttl）。剩余 ≥ TTL/2
#     时隐藏；TTL/6 ~ TTL/2 在尾段弱化显示；不足 TTL/6 升为黄色胶囊；expires_at 已过才判
#     cold（红），并附「+<recache_tokens_if_cold>」= 过期后下一轮要按 cache write 重写的
#     token 数。warm=false 而未到期不显示（它也包含「最近一次响应没报告缓存 token」）。
#   - hit：会话级累计命中率，由 statusline-tokens.py 从 transcript 算出（含 subagent）；
#     stdin 的 prompt_cache.hit_ratio 是每个进程各自的统计，重启后从零开始，仅作回落。
#   - ↑/↓：transcript 聚合的 session 累计 总输入（含缓存读写）/ 输出，含同会话 subagent。
#   - 时长：只显示 api = cost.total_api_duration_ms；墙钟时长含离开的空闲时间，不显示。
#   - ↻：主对话 compact_boundary 次数，为 0 时隐藏。
#   - m：本会话消息数（assistant 响应的不同 message.id 个数，含 subagent），见
#     statusline-tokens.py 的计数口径。
#   - claude：status.claude.com 总体 indicator，none 时隐藏；不含中转站。minor /
#     maintenance 只在尾段弱化显示，major / critical 才是红色告警。status_page=off 时
#     不请求、不显示。
#
# 响应式
# ------
# 每项有从长到短的若干写法。超宽时按「降级序列」逐步执行（drop 丢弃 / shrink 换更短写法），
# 放得下即停；没有 drop 步骤的项永不丢弃，默认序列见「默认降级序列」，配置文件可整体替换。
# 额度进入风险态时其 drop 失效，免得只剩一个看不出属于谁的「out 1h42m」。
# 预算 = COLUMNS − SAFE_MARGIN（界面在状态栏外另有内置边距，见 docs/DECISIONS.md）。
# 宽度用 dw() 的显示宽度模型计算，不用 ${#s}。
#
# 工程约束（修改时必须保留）
# --------------------------
#   1. 所有 git 调用带 --no-optional-locks，避免高频刷新争抢 index 锁
#   2. jq 或任何依赖缺失时静默降级，绝不向状态栏输出错误信息
#   3. 末尾 `true` 保证退出码恒为 0（exit code ≠ 0 时输出会被整体丢弃）
#   4. 缓存写入必须 tmp + mv 原子替换（写一半被杀不能污染缓存）
#   5. 跨进程传字段用 tab 分隔 + 空字段占位符 "-"（IFS=tab 的 read 会吞连续 tab，
#      无占位符会导致字段错位）
#   6. 变量展开后紧跟多字节字符（· 等）必须写 ${var} 花括号——zh_CN.UTF-8 下
#      bash 5.3 会把多字节首字节吞进变量名
#   7. 私用区字形（powerline / nerd font）一律写成 $'\xee..' 字节转义——编辑工具
#      会静默吞掉裸字形
#   8. 渲染与宽度计算全在当前 shell 内用全局变量完成，不可 $(...) 取值：子 shell 改不到
#      父 shell 的数组，且每次 fork 都是开销
#   9. 后台刷新按会话加 mkdir 锁（原子），超过 LOCK_STALE 秒视为残锁清除
#  10. token 助手输出为空（未追平 / 异常）时保留旧缓存不覆盖，否则该项闪烁消失
#  11. 色板缓存缺失或与 THEME 不符时整行无色输出，不在同步路径算颜色
#  12. 可移植：stat 先试 GNU（-c）再试 BSD（-f）——GNU 的 -f 是查文件系统，
#      不报错但结果是错的，顺序不能反
#  13. 显示宽度：CJK / 全角字符占两列而 ${#s} 只算一个，dw() 按 UTF-8 字节数推算
#      （三字节字符除已知的窄符号外按宽字符计）；每种写法的宽度在 item() 里算一次存入 VW
#  14. 需要 bash ≥ 4.2（关联数组、${!var} 间接展开）；低于 4.2 时开头的守卫换用 ≥ 4.2 的
#      新版 bash 重跑，找不到则降级为最简行
#  15. 配置文件只按白名单逐行解析，绝不 source（避免把配置当代码执行）
#  16. 输出用 printf '%s'、转义序列用真实 ESC 字节拼接，外部文本（路径 / 分支 / 模型名）
#      先剔除控制字符——printf '%b' 会把目录名里的 \c 当「停止输出」、\033[41m 当真转义
#  17. 同步路径只允许 jq 一次外部进程，其余全用 bash 内建（cd -P / 参数展开 / read）；
#      唯一例外：某目录首次渲染、还没有 git 缓存时同步跑一次 git_data（约 4 个进程）
#  18. 外部输入一律容错：stdin 任一字段类型不符只影响该项（jq 里先 objects 过滤）；
#      配置数值按十进制规范化（10#，免得 08 被当成非法八进制）
#  19. 缓存读取拒绝符号链接（-L）；生命周期清理只在默认私有根的第一层删普通文件，
#      不递归、不碰锁 / 色板 / 状态页，显式 CLAUDE_SL_TMP 不清理
#
# 开发：bash run_tests.sh（隔离沙箱，不碰线上缓存）；安装：bash install.sh
# 调试渲染：CLAUDE_SL_NO_REFRESH=1 bash install.sh 后手工喂 stdin（见 tests/_util.py）
#

# ── bash 版本守卫（约束 14）─────────────────────────────────────────────
# 关联数组需要 bash ≥ 4.2；macOS 自带 /bin/bash 是 3.2。
# 先换用常见位置的 ≥4.2 bash 重跑，找不到就只输出「模型 · ctx%」的最简行。
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 2 ]; }; then
  for b in /opt/homebrew/bin/bash /usr/local/bin/bash /usr/bin/bash; do
    [ -x "$b" ] && "$b" -c 'v=$((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1])); [ "$v" -ge 402 ]' 2>/dev/null \
      && exec "$b" "$0" "$@"
  done
  [ "${1:-}" = "--refresh" ] && exit 0
  jq -r '[.model.display_name, ((.context_window.used_percentage // empty | round | tostring) + "%")] | map(select(. != null)) | join(" · ")' 2>/dev/null
  exit 0
fi

# 缓存 / 锁 / 状态文件按不可信磁盘数据对待：只给本人读写（0600 / 0700）。
# 后台临时文件另用 mktemp（O_EXCL），防止共享 /tmp 里的符号链接预置。
umask 077

# ── 可调参数 ────────────────────────────────────────────────────────────
THEME=auto               # auto | tokyonight | dracula | catppuccin | onedark | nord
GLYPHS=nerd              # nerd | ascii（终端字体没有 Nerd Font 时用 ascii）
SAFE_MARGIN=4            # 预留给界面内置边距的列数（截图反推，见「响应式」）
TRUNCATE_LENGTH=3        # 目录保留的末尾段数（starship truncation_length）
WARN_AT=50               # 额度已用 ≥ 此值才显示 5h 重置时间与 7d（低于且无风险时隐藏）
BAD_AT=80                # 额度已用 ≥ 此值 → 红
CTX_WARN=70              # ctx 已用 ≥ 此值 → 黄（与额度阈值分开）
CTX_BAD=90               # ctx 已用 ≥ 此值 → 红
CRIT_AT=95               # ctx / 额度已用 ≥ 此值 → 强红（T_CRIT；只有 tokyo-glass 与红色有别）
PILL_STYLE=tint          # 胶囊风格：tint cool jewel glass mono pastel tokyo-glass（见 statusline-theme.py STYLES）
PILL_CHROMA=12           # tint 风格的淡染彩度（0 = 所有胶囊同一底色）
STATUS_PAGE=on           # on | off：是否定时请求 status.claude.com（off 时零网络请求、不显示服务状态）
STATUS_TTL=60            # claude 状态页拉取间隔（秒）
STATUS_STALE=600         # 状态缓存超过此秒数未更新 → 整项隐藏（宁缺勿假）
STATUS_URL=https://status.claude.com/api/v2/status.json
THEME_TTL=300            # 色板重算间隔（秒）：跟随 iTerm profile / 系统深浅色切换
TOK_MIN_GAP=10           # 输入都未变时，token 助手最少间隔（秒）
TOK_STALE=900            # token 缓存超过此秒数判陈旧：派生项隐藏（宁缺勿假）
LOCK_STALE=30            # 后台刷新锁超过此秒数视为残锁
CACHE_MAX_AGE=30         # 默认根内 token / skip / git 缓存与残留临时文件的保留天数
                         # （会话状态另外要求记录里的 transcript 已不存在；后台每天清一次）
PRUNE_GAP=86400          # 两次清理的最小间隔（秒）
PALETTE_V=4              # 色板缓存格式版本：theme.py 新增 / 改名键时 +1，首行不符即重算
# 全部缓存 / 锁 / 状态文件的根。显式 CLAUDE_SL_TMP（预览 / 测试）由调用方负责隔离；
# 否则用每用户私有目录 $TMPDIR/claude-sl-<uid>（0700），不再落到共享 /tmp。
SLT_OK=0
if [ -n "${CLAUDE_SL_TMP:-}" ]; then
  SLT=$CLAUDE_SL_TMP
else
  tmpdir=${TMPDIR:-/tmp}; tmpdir=${tmpdir%/}
  SLT=$tmpdir/claude-sl-${UID:-0}
fi
# 根必须是本人拥有的真实目录：符号链接（哪怕是本人建的）一律不跟随。
[ -d "$SLT" ] && [ ! -L "$SLT" ] && [ -O "$SLT" ] && SLT_OK=1
GIT_TTL=5                # git 缓存刷新间隔（秒）；.git/HEAD 或 index 变了立即刷新
CCSWITCH_DB="$HOME/.cc-switch/cc-switch.db"

# ── 默认降级序列（逐项裁定去留的产物，见 docs/DECISIONS.md）────────────────
# 宽度不够时从前往后逐步执行，放得下即停。每步一个动作：
#   drop <项>    丢弃该项            shrink <项>  换成下一种更短的写法（多级写法可出现多次）
# 没有 drop 步骤的项永不丢弃（现为 dir git gitst model ctx ctxin q5 cache hit io）；
# 额度（q7 / day）进入风险态时，其 drop 步骤自动失效。
# 配置文件里写了任何 step 行，就整体替换这份默认序列。
STEPS=(                                               # 逐项裁定去留，见 docs/DECISIONS.md
  "drop api" "drop msgs" "drop today" "drop claudem" "drop q7r" "shrink io" "drop q5r" "drop cachet"
  "drop comp" "drop recache" "shrink git" "shrink dir" "shrink hit" "drop q7" "drop rate"
  "shrink q5out" "shrink q7out" "shrink dout" "shrink q5" "shrink q7" "shrink spend" "shrink spout" "drop day" "drop spend" "drop cost"
  "shrink claude" "drop claude" "drop dout" "drop spout" "drop q7out" "drop q5out"
  "shrink ctx" "shrink cache" "shrink model" "shrink model" "shrink model"
)
# 合法的项名（配置文件里写错的项名会被忽略）
ITEM_IDS=" dir git gitst model ctx ctxin q5 q5r q5out q7 q7r q7out cost rate day dout today
  spend spout cache recache claude claudem comp cachet hit io api msgs "

# ── 用户配置文件（覆盖上面的参数与优先级；不存在则全用默认）──────────────
# 格式：每行「键 = 值」，# 开头为注释。只认白名单里的键，值不合法则忽略——
# 用 read 逐行解析而非 source，配置文件里的任何内容都不会被当作代码执行。
SL_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/claude-statusline.conf"
if [ -r "$SL_CONF" ]; then
  CONF_STEPS=()
  while IFS='=' read -r k v; do
    k=${k//[[:space:]]/}; v=${v%%#*}; rv=$v; v=${v//[[:space:]]/}
    case "$k" in
      ''|\#*) ;;
      theme)  case "$v" in auto|tokyonight|dracula|catppuccin|onedark|nord) THEME=$v ;; esac ;;
      glyphs) case "$v" in nerd|ascii) GLYPHS=$v ;; esac ;;
      status_page) case "$v" in on|off) STATUS_PAGE=$v ;; esac ;;
      pill_style) case "$v" in tint|cool|jewel|glass|mono|pastel|tokyo-glass) PILL_STYLE=$v ;; esac ;;
      safe_margin|truncate_length|warn_at|bad_at|ctx_warn|ctx_bad|crit_at|pill_chroma)
        case "$v" in ''|*[!0-9]*) ;; *) v=$((10#$v)); case "$k" in   # 约束 18：08 不当八进制
          safe_margin) SAFE_MARGIN=$v ;; truncate_length) TRUNCATE_LENGTH=$v ;;
          warn_at) WARN_AT=$v ;; bad_at) BAD_AT=$v ;;
          ctx_warn) CTX_WARN=$v ;; ctx_bad) CTX_BAD=$v ;; crit_at) CRIT_AT=$v ;;
          pill_chroma) [ "$v" -le 40 ] && PILL_CHROMA=$v ;; esac ;; esac ;;
      step)
        read -r act sid_ _ <<< "$rv"
        case "$act" in
          drop|shrink) [[ "$ITEM_IDS" == *" $sid_ "* ]] && CONF_STEPS+=("$act $sid_") ;;
        esac ;;
    esac
  done < "$SL_CONF"
  [ "${#CONF_STEPS[@]}" -gt 0 ] && STEPS=("${CONF_STEPS[@]}")
fi

# 助手脚本（statusline-tokens.py / statusline-theme.py）与本脚本放在同一目录，按本脚本的
# 绝对路径定位——不依赖 CLAUDE_CONFIG_DIR，也不怕被相对路径调用后 cd 走（后台刷新会 cd 到工作目录）
SELF=${BASH_SOURCE[0]}; [ "${SELF#/}" = "$SELF" ] && SELF="$PWD/$SELF"
HERE=${SELF%/*}
if [ "${TERM_PROGRAM:-}" = iTerm.app ] || [ "${LC_TERMINAL:-}" = iTerm2 ]; then   # tmux 里也有 LC_TERMINAL
  PKEY="iterm-${ITERM_PROFILE:-default}"
else
  PKEY="term-${TERM_PROGRAM:-other}"     # 非 iTerm 不读 profile，同一种终端共用一份
fi
PKEY=${PKEY//[^A-Za-z0-9_-]/_}
THEME_CACHE="${SLT}/claude-sl-theme-${PKEY}.cache"

if [ "$GLYPHS" = ascii ]; then
  GLYPH_BRANCH=""; PL_LCAP=" "; PL_RCAP=" "
else
  GLYPH_BRANCH=$'\xee\x82\xa0 '  # U+E0A0 分支 + 空格
  PL_LCAP=$'\xee\x82\xb6'        # U+E0B6 胶囊左半圆
  PL_RCAP=$'\xee\x82\xb4'        # U+E0B4 胶囊右半圆
fi

# 约束 12：mtime / size。macOS 直接调 /usr/bin/stat（BSD）；其余先 GNU 后 BSD
if [ "${OSTYPE#darwin}" != "$OSTYPE" ] && [ -x /usr/bin/stat ]; then
  mtime() { /usr/bin/stat -f %m "$1" 2>/dev/null; }
  fsize() { /usr/bin/stat -f %z "$1" 2>/dev/null; }
else
  mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
  fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null; }
fi
# 缓存读取前确认：可读、不是符号链接、且属于当前用户。跨用户预置的同名文件一律不读
# （-r / -L / -O 都是内建，不 fork）。
cache_ok() { [ -r "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]; }

# 路径 → 缓存文件名用的键 → 全局 PK。可逆转义（_ → _u、空格 → _s、/ → _d），不同路径不撞名；
# 超过 180 字节（APFS 文件名上限 255 字节）改为 FNV-1a 64 位哈希 + 末段可读部分（2026-10-28
# 终审：此前直接用全路径，超长目录的 git 缓存永远写不进去，每次渲染都同步跑 git）
pathkey() {
  local k=$1 h i c
  k=${k//_/_u}; k=${k// /_s}; k=${k//\//_d}
  local LC_ALL=C
  if [ "${#k}" -gt 180 ]; then
    h=-3750763034362895579                       # FNV-1a 64 偏移基（0xcbf29ce484222325）
    for (( i = 0; i < ${#k}; i++ )); do
      printf -v c '%d' "'${k:i:1}"; [ "$c" -lt 0 ] && c=$((c + 256))
      h=$(( (h ^ c) * 1099511628211 ))
    done
    c=${k: -60}; c=${c//[^A-Za-z0-9_]/}
    printf -v PK 'h%016x_%s' "$h" "$c"
  else
    PK=$k
  fi
}

# git 三件套 → "repo_root \t branch \t 状态旗标"（空字段用 "-" 占位，约束 5）；
# 写缓存时后台刷新再追加第 4 段「写入时刻」（GIT_TTL 判定用）
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

# ── 后台刷新模式（--refresh <dir> <tp> <sid> <mode> <cost> <q5> <q7> <sp>）──────
if [ "${1:-}" = "--refresh" ]; then
  dir=${2:-}; tp=${3:-}; sid=${4:-}; mode=${5:-sub}; costraw=${6:--}; q5arg=${7:--}; q7arg=${8:--}; sparg=${9:--}
  now=${EPOCHSECONDS:-$(date +%s)}
  skey=${sid//\//_}; skey=${skey// /_}
  # 私有缓存目录：默认位置不存在则创建（0700）；已存在但不是我们的就整轮放弃写缓存。
  # 显式 CLAUDE_SL_TMP 也要求目录存在且属于本人（预览 / 测试会自己建好）。
  if [ -z "${CLAUDE_SL_TMP:-}" ] && [ "$SLT_OK" != 1 ]; then
    mkdir -m 700 "$SLT" 2>/dev/null
    [ -d "$SLT" ] && [ ! -L "$SLT" ] && [ -O "$SLT" ] && SLT_OK=1
  fi
  [ "$SLT_OK" = 1 ] || exit 0
  [ -z "${CLAUDE_SL_TMP:-}" ] && chmod 700 "$SLT" 2>/dev/null
  # 旧版本把缓存写在共享 /tmp：把本会话的 token / 跳过标记 / 状态迁到私有目录，
  # 避免整段重扫。只搬自己的普通文件（非符号链接），目标已存在则不覆盖。
  if [ -z "${CLAUDE_SL_TMP:-}" ] && [ "$SLT" != "/tmp" ] && [ -d /tmp ]; then
    sbase=${sid##*/}
    for f in "claude-sl-tok-${skey}.cache" "claude-sl-tok-${skey}.cache.ts" \
             "claude-sl-skip-${skey}" "claude-statusline-${sbase}.json"; do
      src="/tmp/$f"; dst="$SLT/$f"
      [ ! -e "$dst" ] && [ -f "$src" ] && [ ! -L "$src" ] && [ -O "$src" ] &&
        mv -f "$src" "$dst" 2>/dev/null && chmod 600 "$dst" 2>/dev/null
    done
  fi
  # 升级迁移来的旧 token 缓存没有 .ts：用缓存自身的 mtime 补一个（一次）。否则同步
  # 路径会一直按「无时间戳 = 新鲜」展示，长期失效时等于没修。
  tokf0="${SLT}/claude-sl-tok-${skey}.cache"
  if cache_ok "$tokf0" && [ ! -e "${tokf0}.ts" ]; then
    m=$(mtime "$tokf0"); m=${m:-$now}
    tsmf=$(mktemp "${SLT}/.claude-sl-tokts-${skey}.XXXXXX" 2>/dev/null)
    [ -n "$tsmf" ] && printf '%s\n' "$m" > "$tsmf" 2>/dev/null \
      && mv -f "$tsmf" "${tokf0}.ts" 2>/dev/null || rm -f "$tsmf" 2>/dev/null
  fi
  # 约束 9：同会话刷新互斥；残锁（被杀进程遗留）超时后清除再抢
  lock="${SLT}/claude-sl-lock-${skey}"
  if ! mkdir "$lock" 2>/dev/null; then
    lt=$(mtime "$lock"); lt=${lt:-$now}
    [ $((now - lt)) -lt "$LOCK_STALE" ] && exit 0
    rmdir "$lock" 2>/dev/null; mkdir "$lock" 2>/dev/null || exit 0
  fi
  trap 'rmdir "$lock" 2>/dev/null' EXIT

  # 色板：缺失 / THEME、PILL_CHROMA 或 PILL_STYLE 改过 / 超过 THEME_TTL 时重算
  tline=""; cache_ok "$THEME_CACHE" && read -r tline < "$THEME_CACHE"
  tm=$(mtime "$THEME_CACHE"); tage=$(( now - ${tm:-0} ))
  # 首行带格式版本 v$PALETTE_V：旧版脚本生成的缓存首行不符，立即重算而不等 THEME_TTL
  want="THEME=${THEME}/${PILL_CHROMA}/${PILL_STYLE}/v${PALETTE_V}"
  failf="${THEME_CACHE}.fail"; ft=0; cache_ok "$failf" && read -r ft < "$failf"
  case "$ft" in ''|*[!0-9]*) ft=0 ;; esac
  if { [ "$tline" != "$want" ] || [ "$tage" -ge "$THEME_TTL" ]; } \
     && ! { [ "$tline" != "$want" ] && [ $((now - ft)) -lt "$THEME_TTL" ]; }; then
    python3 "${HERE}/statusline-theme.py" "$THEME" "$THEME_CACHE" "$PILL_CHROMA" "$PILL_STYLE" "$PALETTE_V" >/dev/null 2>&1
    tline=""; cache_ok "$THEME_CACHE" && read -r tline < "$THEME_CACHE"
    if [ "$tline" = "$want" ]; then rm -f "$failf" 2>/dev/null
    else  # 失败退避：THEME_TTL 内不再每轮重跑
      tmp=$(mktemp "${SLT}/.claude-sl-theme-fail.XXXXXX" 2>/dev/null) &&
        printf '%s\n' "$now" > "$tmp" 2>/dev/null && mv -f "$tmp" "$failf" 2>/dev/null ||
        rm -f "$tmp" 2>/dev/null
    fi
  fi

  [ -n "$dir" ] && cd "$dir" 2>/dev/null
  pathkey "$dir"; key=$PK
  # git：缓存不足 GIT_TTL 秒且 .git/HEAD、.git/index 都没在缓存之后改过 → 跳过（本仓库
  # git status 约 34 ms，每 2 秒一次是后台刷新里最贵的一项）。worktree 的 .git 是文件，只按 TTL。
  # 缓存第 4 段是写入时刻（免 stat fork 取 mtime）；HEAD / index 新旧用内建 -nt 比较。
  gc="${SLT}/claude-sl-git-${key}.cache"; need=1
  if cache_ok "$gc"; then
    groot=""; gts=0; IFS=$'\t' read -r groot _ _ gts < "$gc"
    case "$gts" in ''|*[!0-9]*) gts=0 ;; esac
    if [ $((now - gts)) -lt "$GIT_TTL" ]; then
      need=0
      # -nt / -ot 在 bash 4.2 只比较到秒：同一秒内的切换会被当成「没变」而沿用旧缓存
      # （5.3 精确到纳秒，所以 macOS 上看不出来）。改成「不比缓存旧」即算变，代价是
      # HEAD / index 刚改过后的一秒内多算几次 git（仅后台）。
      [ -d "$groot/.git" ] && { [ ! "$groot/.git/HEAD" -ot "$gc" ] || [ ! "$groot/.git/index" -ot "$gc" ]; } && need=1
    fi
  fi
  if [ "$need" = 1 ]; then
    tmp=$(mktemp "${SLT}/.claude-sl-git-${key}.XXXXXX" 2>/dev/null)
    if [ -n "$tmp" ]; then
      { git_data; printf '\t%s' "$now"; } > "$tmp" 2>/dev/null && mv -f "$tmp" "$gc" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    fi
  fi

  relayarg="-"
  [ "$mode" = relay ] && [ -r "$CCSWITCH_DB" ] && relayarg=$CCSWITCH_DB

  if [ -n "$tp" ] && [ "$tp" != "-" ] && [ -n "$sid" ] && [ "$sid" != "-" ]; then
    # 输入都没变、且距上次不足 TOK_MIN_GAP 秒 → 跳过 python
    sig="$(fsize "$tp")|${costraw}|${q5arg}|${q7arg}|${sparg}|${relayarg}"
    skipf="${SLT}/claude-sl-skip-${skey}"
    lsig=""; lts=0
    cache_ok "$skipf" && IFS=$'\t' read -r lsig lts < "$skipf"
    case "$lts" in ''|*[!0-9]*) lts=0 ;; esac
    if [ "$sig" != "$lsig" ] || [ $((now - lts)) -ge "$TOK_MIN_GAP" ]; then
      args=("th=${WARN_AT},${BAD_AT}")
      [ "$costraw" != "-" ] && args+=("cost=${costraw}")
      [ "$q5arg" != "-" ] && args+=("q5=${q5arg}")
      [ "$q7arg" != "-" ] && args+=("q7=${q7arg}")
      [ "$sparg" != "-" ] && args+=("sp=${sparg}")
      [ "$relayarg" != "-" ] && args+=("relay=${relayarg}")
      tok=$(CLAUDE_SL_TMP=$SLT python3 "${HERE}/statusline-tokens.py" "$tp" "$sid" "${args[@]}" 2>/dev/null)
      if [ -n "$tok" ]; then                    # 约束 10
        tmp2=$(mktemp "${SLT}/.claude-sl-tok-${skey}.XXXXXX" 2>/dev/null)
        if [ -n "$tmp2" ] && printf '%s\n' "$tok" > "$tmp2" 2>/dev/null \
           && mv -f "$tmp2" "${SLT}/claude-sl-tok-${skey}.cache" 2>/dev/null; then
          # 新鲜度时间戳：同步路径据此丢弃长期未更新的派生值（约束 10 只防抖，不防永久陈旧）
          tsf="${SLT}/claude-sl-tok-${skey}.cache.ts"
          tsmf=$(mktemp "${SLT}/.claude-sl-tokts-${skey}.XXXXXX" 2>/dev/null)
          [ -n "$tsmf" ] && printf '%s\n' "$now" > "$tsmf" 2>/dev/null \
            && mv -f "$tsmf" "$tsf" 2>/dev/null || rm -f "$tsmf" 2>/dev/null
          stmp=$(mktemp "${SLT}/.claude-sl-skip-${skey}.XXXXXX" 2>/dev/null)
          [ -n "$stmp" ] && printf '%s\t%s\n' "$sig" "$now" > "$stmp" 2>/dev/null \
            && mv -f "$stmp" "$skipf" 2>/dev/null || rm -f "$stmp" 2>/dev/null
        else
          rm -f "$tmp2" 2>/dev/null
        fi
      fi
    fi
  fi

  # claude 状态页：先写尝试时刻再拉取（多会话并发时最多重复一次，幂等）
  try=${SLT}/claude-sl-status.try
  last=0; cache_ok "$try" && read -r last < "$try"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ "$STATUS_PAGE" = on ] && [ $((now - last)) -ge "$STATUS_TTL" ]; then
    ttmp=$(mktemp "${SLT}/.claude-sl-status-try.XXXXXX" 2>/dev/null)
    [ -n "$ttmp" ] && printf '%s\n' "$now" > "$ttmp" 2>/dev/null && mv -f "$ttmp" "$try" 2>/dev/null || rm -f "$ttmp" 2>/dev/null
    ind=$(curl -s -m 4 "$STATUS_URL" 2>/dev/null | jq -r '.status.indicator // empty' 2>/dev/null)
    if [ -n "$ind" ]; then
      stmp=$(mktemp "${SLT}/.claude-sl-status.XXXXXX" 2>/dev/null)
      [ -n "$stmp" ] && printf '%s\t%s\n' "$now" "$ind" > "$stmp" 2>/dev/null &&
        mv -f "$stmp" "${SLT}/claude-sl-status.cache" 2>/dev/null || rm -f "$stmp" 2>/dev/null
    fi
  fi

  # 缓存生命周期（约束 19）：只清默认私有根，每天最多一次。find -type f 不跟随符号
  # 链接，-maxdepth 1 不递归；锁目录、色板、状态页与面板状态不在清理范围。缓存都可
  # 重建，误删最多丢一轮加速；会话状态另由 transcript 是否存在把关（见下）。
  if [ -z "${CLAUDE_SL_TMP:-}" ]; then
    pstamp="${SLT}/claude-sl-prune.stamp"
    pst=$(mtime "$pstamp"); pst=${pst:-0}
    if [ $((now - pst)) -ge "$PRUNE_GAP" ]; then
      find "$SLT" -mindepth 1 -maxdepth 1 -type f \
        \( -name 'claude-sl-tok-*.cache' -o -name 'claude-sl-tok-*.cache.ts' \
           -o -name 'claude-sl-skip-*' -o -name 'claude-sl-git-*.cache' \
           -o -name '.claude-sl-*' \) \
        -mtime +"$CACHE_MAX_AGE" -exec rm -f {} + 2>/dev/null
      # 会话状态是躲开整段重扫的断点，比 token 缓存贵得多：只有记录里的 transcript
      # 都不在了才删。读不出（损坏 / 无 jq）时视为没有 transcript——那种状态本来也会
      # 触发重扫，删掉不损失任何东西。
      cutoff=$((now - CACHE_MAX_AGE * 86400))
      for f in "$SLT"/claude-statusline-*.json; do
        [ -f "$f" ] && [ ! -L "$f" ] && [ -O "$f" ] || continue
        m=$(mtime "$f"); [ "${m:-0}" -lt "$cutoff" ] || continue
        keep=0
        while IFS= read -r p; do
          [ -n "$p" ] && [ -e "$p" ] && { keep=1; break; }
        done < <(jq -r '.files | keys[]?' "$f" 2>/dev/null)
        [ "$keep" = 0 ] && rm -f "$f"
      done
      ptmp=$(mktemp "${SLT}/.claude-sl-prune.XXXXXX" 2>/dev/null)
      [ -n "$ptmp" ] && printf '%s\n' "$now" > "$ptmp" 2>/dev/null \
        && mv -f "$ptmp" "$pstamp" 2>/dev/null || rm -f "$ptmp" 2>/dev/null
    fi
  fi
  exit 0
fi

# ── 同步路径：1 次 jq 提取全部字段（约束 5：空字段占位 "-"）──────────────
IFS= read -r -d '' input || true      # 约束 17：读完 stdin 不 fork cat
now=${EPOCHSECONDS:-$(date +%s)}

fields=$(jq -r --argjson now "$now" --argjson warn "$CTX_WARN" --argjson bad "$CTX_BAD" '
  def hfmt:
    if . >= 999500 then
      ((. / 100000 | round / 10) as $v |
       if ($v | floor) == $v then "\($v | floor)M" else "\($v)M" end)
    elif . >= 1000 then "\(. / 1000 | round)k"
    else tostring end;
  def dfmt:                                   # 秒 → 45s / 12m / 3h05m / 2d4h
    if . < 60 then "\(.)s"
    elif . < 3600 then "\(. / 60 | floor)m"
    elif . < 86400 then "\(. / 3600 | floor)h\(. % 3600 / 60 | floor | if . < 10 then "0\(.)" else tostring end)m"
    else "\(. / 86400 | floor)d\(. % 86400 / 3600 | floor)h" end;
  def num: if type == "number" then . else null end;
  def obj: if type == "object" then . else {} end;          # 约束 18：类型不符当作缺失
  def clamp: if . < 0 then 0 elif . > 100 then 100 else . end;
  def quota($q):                              # → [已用%, 重置倒计时, "已用,resets_at"]
    if ($q.used_percentage | num) == null then ["-", "-", "-"]
    else [ ($q.used_percentage | clamp | round | tostring),
           (if ($q.resets_at | num) == null or $q.resets_at <= $now then "-"
            else ($q.resets_at - $now | floor | dfmt) end),
           (if ($q.resets_at | num) == null then "-"
            else "\($q.used_percentage | clamp),\($q.resets_at)" end) ] end;
  obj as $in | ($in.context_window | obj) as $cw | ($in.prompt_cache | obj) as $p |
  ($in.rate_limits | obj) as $rl | ($in.cost | obj) as $c | ($rl.spend_limit | obj) as $sl |
  # ctx 先取整再比阈值：显示 70% 就该是黄（约束 13 同理，显示值与着色同源）
  (if ($cw.used_percentage | num) != null then ($cw.used_percentage | clamp | round)
   elif ($cw.remaining_percentage | num) != null then (100 - $cw.remaining_percentage | clamp | round)
   else null end) as $ctx |
  [ (($in.workspace | obj).current_dir // $in.cwd // "-"),
    (($in.model | obj).display_name // "-"),
    (($in.effort | obj).level // "-"),
    (if $ctx == null then "-" else ($ctx | tostring) end),
    (if $ctx == null then "-" elif $ctx >= $bad then "bad" elif $ctx >= $warn then "warn" else "ok" end),
    (if ($cw.total_input_tokens | num) == null then "-" else ($cw.total_input_tokens | hfmt) end),
    (if ($cw.context_window_size | num) == null then "-" else ($cw.context_window_size | hfmt) end),
    (quota($rl.five_hour | obj) | .[]),
    (quota($rl.seven_day | obj) | .[]),
    (if ($c.total_cost_usd | num) == null then "-" else ($c.total_cost_usd | tostring) end),
    (if ($c.total_duration_ms | num) == null then "-" else ($c.total_duration_ms / 1000 | floor | dfmt) end),
    (if ($c.total_api_duration_ms | num) == null then "-" else ($c.total_api_duration_ms / 1000 | floor | dfmt) end),
    # cache 项："<剩余秒数> <TTL 秒数>"；expires_at 已过记作 "cold"；warm=false 而未到期、
    # 未观察到缓存或缺 expires_at 为 "-"（只按过期时间判冷）
    (if $p.caching_observed != true or ($p.expires_at | num) == null then "-"
     elif $p.expires_at <= $now then "cold"
     elif $p.warm != true then "-"
     else "\($p.expires_at - $now | floor) \(if $p.ttl == "1h" then 3600 else 300 end)" end),
    (if ($p.recache_tokens_if_cold | num) == null then "-" else ($p.recache_tokens_if_cold | hfmt) end),
    (if ($p.hit_ratio | num) == null then "-"
     else ($p.hit_ratio * 1000 | round) as $h | "\($h / 10 | floor).\($h % 10)" end),
    (if ($p.hit_ratio | num) == null then "-" else ($p.hit_ratio * 100 | round | tostring) end),
    ($in.transcript_path // "-"),
    ($in.session_id // "-"),
    # 网关花费限额 rate_limits.spend_limit（官方 schema：已用可超过 100；resets_at 已过则不出现）
    (if ($sl.used_percentage | num) == null then "-" else ($sl.used_percentage | if . < 0 then 0 elif . > 999 then 999 else . end | round | tostring) end),
    (if ($sl.resets_at | num) == null or $sl.resets_at <= $now then "-" else ($sl.resets_at - $now | floor | dfmt) end),
    (if ($sl.used_percentage | num) == null or ($sl.resets_at | num) == null
        or (["daily", "weekly", "monthly"] | index($sl.period)) == null then "-"
     else "\($sl.used_percentage),\($sl.resets_at),\($sl.period)" end),
    ($sl.period // "-"),
    (if ($sl.used_usd | num) == null then "-" else ($sl.used_usd | tostring) end),
    (if ($sl.limit_usd | num) == null then "-" else ($sl.limit_usd | tostring) end)
  ] | map(tostring | gsub("[\t\n]"; " ") | if . == "" then "-" else . end)   # 空串也占位（// 只管 null）
    | join("\t")' 2>/dev/null <<< "$input")

IFS=$'\t' read -r dir model eff ctx ctxlv ctxin ctxwin q5 q5r q5arg q7 q7r q7arg costraw dur apidur cache recache hit hit0 tp sid \
  sp spr sparg spper spusd splim <<< "$fields"
for v in dir model eff ctx ctxlv ctxin ctxwin q5 q5r q5arg q7 q7r q7arg costraw dur apidur cache recache hit hit0 tp sid \
  sp spr sparg spper spusd splim; do
  [ "${!v}" = "-" ] && printf -v "$v" '%s' ''    # 4.2 的空格式 printf -v 不清空变量
done
# 模式（见头部「费用口径」）：有 5h / 7d → 订阅；否则非官方 ANTHROPIC_BASE_URL → 中转站；都没有 → 订阅
mode=sub
if [ -z "$q5" ] && [ -z "$q7" ]; then
  case "${ANTHROPIC_BASE_URL:-}" in ''|https://api.anthropic.com|https://api.anthropic.com/*) ;; *) mode=relay ;; esac
fi

# ── 色板缓存（约束 11）→ SGR 片段：truecolor "2;R;G;B"，否则 256 色 "5;N" ──
NC=1; cache_theme=""
if [ "$SLT_OK" = 1 ] && cache_ok "$THEME_CACHE"; then
  while IFS='=' read -r k v; do
    case "$k" in
      THEME)   cache_theme=$v ;;                    # 不可直接赋给 THEME：会覆盖脚本设定
      P_*|T_*|L_*|W_*)                              # W_ = 字重 SGR，不经 sgr 换算
        case "$v" in ''|*[!0-9\;]*) ;; *) printf -v "$k" '%s' "$v" ;; esac ;;   # 只认 R;G;B / 字重
    esac
  done < "$THEME_CACHE"
  # 只改了 pill_chroma / pill_style 时先沿用旧色板（后台按首行不符重算），免得闪一次无色
  [ "${cache_theme%%/*}" = "$THEME" ] && [ -n "${P_CTX:-}" ] && NC=0
fi
case "${COLORTERM:-}" in
  truecolor|24bit) CDEPTH=2 ;;
  *) case "${TERM:-}" in *256color*) CDEPTH=5 ;; *) NC=1 ;; esac ;;
esac
# 256 色：xterm 色块的六档是 0/95/135/175/215/255（不是等距），另有 232–255 共 24 级灰
# （8 + 10k）；两者各取最近，再按平方距离择优。此前按等距六档换算且不看灰阶，
# #44475a 这类中性胶囊会落到偏色的色块上（2026-10-28 复核）。
c6() { if [ "$1" -lt 48 ]; then C6=0; elif [ "$1" -lt 115 ]; then C6=1; else C6=$(( ($1 - 35) / 40 )); fi
       [ "$C6" -gt 5 ] && C6=5; L6=$(( C6 ? 55 + 40 * C6 : 0 )); }
sgr() {   # sgr <变量名>：把 "R;G;B" 改写成当前色深的 SGR 片段
  local v=${!1} r g b ri gi bi cd gl gv gd
  [ -z "$v" ] && return
  if [ "$CDEPTH" = 2 ]; then printf -v "$1" '2;%s' "$v"; return; fi
  r=${v%%;*}; v=${v#*;}; g=${v%%;*}; b=${v#*;}
  c6 "$r"; ri=$C6; cd=$(( (r - L6) * (r - L6) ))
  c6 "$g"; gi=$C6; cd=$(( cd + (g - L6) * (g - L6) ))
  c6 "$b"; bi=$C6; cd=$(( cd + (b - L6) * (b - L6) ))
  gl=$(( ((r + g + b) / 3 - 3) / 10 )); [ "$gl" -lt 0 ] && gl=0; [ "$gl" -gt 23 ] && gl=23
  gv=$(( 8 + 10 * gl )); gd=$(( (r - gv) * (r - gv) + (g - gv) * (g - gv) + (b - gv) * (b - gv) ))
  if [ "$gd" -lt "$cd" ]; then printf -v "$1" '5;%d' $(( 232 + gl ))
  else printf -v "$1" '5;%d' $(( 16 + 36 * ri + 6 * gi + bi )); fi
}
# 旧版色板缓存缺少后加的键时回退到同层级的旧键：否则会拼出 "38;m" 这种非法 SGR，
# iTerm 连带丢掉该格底色（2026-10-28 实测：模型、尾段胶囊只剩两端圆角帽）
: "${P_GIT:=$P_NEU}" "${P_MODEL:=$P_NEU}" "${P_CTX:=$P_NEU}" "${P_Q5:=$P_NEU}" "${P_Q7:=$P_NEU}"
: "${P_USD:=$P_NEU}" "${P_TAIL:=$P_NEU}" "${T_MODEL:=$T_NEU}" "${T_TER:=$T_SEC}" "${T_CRIT:=$T_BAD}"
: "${L_Q7:=$L_QUOTA}"
if [ "$NC" = 0 ]; then
  for v in P_NEU P_GIT P_MODEL P_CTX P_Q5 P_Q7 P_USD P_TAIL T_NEU T_SEC T_DIR T_GIT T_MODEL T_TER T_CRIT L_CTX L_QUOTA L_Q7 L_USD T_WARN T_BAD T_MUTED T_DIM; do sgr "$v"; done
fi

# ── 目录归一化 ─────────────────────────────────────────────────────────
{ [ -n "$dir" ] && cd -P "$dir" 2>/dev/null; } || cd -P . 2>/dev/null
dir=$PWD                                 # cd -P 后 PWD 即物理路径（约束 17：不 fork pwd）

# ── git 段：优先读缓存，缺失时同步回退（首次/换目录后的那一轮）──────────
pathkey "$dir"; key=$PK
gitcache="${SLT}/claude-sl-git-${key}.cache"
if [ "$SLT_OK" = 1 ] && cache_ok "$gitcache"; then
  IFS=$'\t' read -r repo_root branch gstatus _ < "$gitcache"     # 第 4 段为写入时刻
else
  IFS=$'\t' read -r repo_root branch gstatus <<< "$(git_data)"
fi
[ "$repo_root" = "-" ] && repo_root=""
branch=${branch//[[:cntrl:]]/}; model=${model//[[:cntrl:]]/}; eff=${eff//[[:cntrl:]]/}   # 约束 16
gstatus=${gstatus//[[:cntrl:]]/}
[ "$branch" = "-" ] && branch=""
[ "$gstatus" = "-" ] && gstatus=""

# ── token / 节奏 / 中转站缓存（十八段，见 statusline-tokens.py）──────────
skey=${sid//\//_}; skey=${skey// /_}
t_in="" t_out="" p_rate="" comp="" q5lv="" q5out="" q7lv="" q7out="" dlv="" dout=""
r_cost="" r_unpriced="" r_today="" r_limit="" t_hit="" splv="" spout="" msgs=""
tokf="${SLT}/claude-sl-tok-${skey}.cache"
tok_ok=1
# 新鲜度：生产者写 <cache>.ts。超过 TOK_STALE 秒不读派生值（$/h、out、等级、hit、io、
# msgs、comp），只保留 stdin 的额度百分比；没有 .ts 的旧缓存 / 预览假缓存按新鲜处理。
if [ "$SLT_OK" = 1 ] && [ -n "$sid" ] && { [ -e "${tokf}.ts" ] || [ -L "${tokf}.ts" ]; }; then
  if [ -f "${tokf}.ts" ] && [ ! -L "${tokf}.ts" ] && cache_ok "${tokf}.ts"; then
    tok_ts=""; read -r tok_ts < "${tokf}.ts"
    case "$tok_ts" in
      ''|*[!0-9]*) tok_ok=0 ;;                    # 坏的 .ts：按陈旧处理，等刷新重写
      *) [ $((now - tok_ts)) -gt "$TOK_STALE" ] && tok_ok=0 ;;
    esac
  else
    tok_ok=0                                      # 有 .ts 但不是本人的普通文件：不读缓存
  fi
fi
if [ "$tok_ok" = 1 ] && [ "$SLT_OK" = 1 ] && [ -n "$sid" ] && cache_ok "$tokf"; then
  IFS=$'\t' read -r t_in t_out p_rate comp q5lv q5out q7lv q7out dlv dout \
    r_cost r_unpriced r_today r_limit t_hit splv spout msgs < "$tokf"
fi
for v in t_in t_out p_rate comp q5lv q5out q7lv q7out dlv dout r_cost r_unpriced r_today r_limit t_hit splv spout msgs; do
  [ "${!v}" = "-" ] && printf -v "$v" '%s' ''    # 4.2 的空格式 printf -v 不清空变量
  printf -v "$v" '%s' "${!v//[[:cntrl:]]/}"       # 约束 16：缓存内容同样不可信
done
# 跨窗口一致性：当前 stdin 已用 <100% 时，缓存里的「用尽」不可能属于这个窗口，
# 直接丢弃等级与 out。bad 但 out 是真实倒计时仍保留——那是「很快用完」的预测。
[ -n "$q5" ] && [ "$q5" -lt 100 ] && [ "$q5out" = 用尽 ] && { q5out=""; q5lv=""; }
[ -n "$q7" ] && [ "$q7" -lt 100 ] && [ "$q7out" = 用尽 ] && { q7out=""; q7lv=""; }
[ -n "$sp" ] && [ "$sp" -lt 100 ] && [ "$spout" = 用尽 ] && { spout=""; splv=""; }
[ "$mode" = relay ] || { r_cost=""; r_today=""; r_limit=""; }
case "$comp" in ''|*[!0-9]*) comp="" ;; esac

# ── claude 状态缓存（过期即隐藏）─────────────────────────────────────────
st_ind=""
if [ "$STATUS_PAGE" = on ] && [ "$SLT_OK" = 1 ] && cache_ok "${SLT}/claude-sl-status.cache"; then
  IFS=$'\t' read -r st_ts st_ind < "${SLT}/claude-sl-status.cache"
  case "$st_ts" in ''|*[!0-9]*) st_ind="" ;; *) [ $((now - st_ts)) -gt "$STATUS_STALE" ] && st_ind="" ;; esac
fi

# ── 目录显示（starship truncate_to_repo 语义）──────────────────────────
if [ -n "$repo_root" ]; then
  path="${repo_root##*/}${dir#"$repo_root"}"
elif [ "$dir" = "$HOME" ]; then
  path="~"
else
  case "$dir" in
    "$HOME"/*) path="~${dir#"$HOME"}" ;;
    *)         path="$dir" ;;
  esac
fi
path=${path//[[:cntrl:]]/}               # 约束 16
IFS=/ read -ra seg <<< "$path"           # 只留末 TRUNCATE_LENGTH 段（约束 17：不 fork awk）
if [ "${#seg[@]}" -gt "$TRUNCATE_LENGTH" ] && [ "$TRUNCATE_LENGTH" -gt 0 ]; then
  path=""; for (( i = ${#seg[@]} - TRUNCATE_LENGTH; i < ${#seg[@]}; i++ )); do path="${path:+$path/}${seg[i]}"; done
fi

# 美元格式（不 fork）：两位小数；jq 对极小值会输出科学计数法，按 0 处理
money() {   # money <小数字符串> → MONEY="$x.yy"
  local v=$1 i f
  case "$v" in *[eE]*|''|*[!0-9.]*) v=0 ;; esac
  case "$v" in *.*) i=${v%%.*}; f=${v#*.}000 ;; *) i=$v; f=000 ;; esac
  i=${i:-0}; f=${f:0:3}
  local c=$(( 10#$i * 100 + (10#$f + 5) / 10 ))
  printf -v MONEY '$%d.%02d' $((c / 100)) $((c % 100))
}

# ── 响应式胶囊渲染（优先级分层）────────────────────────────────────────
# item <id> <组> <角色> <写法0> [写法1 ...]：写法从长到短；空写法0 = 不加入。
# 角色 = 值的文字色：PRI 主值（前景色加粗）/ MODEL 模型名 / SEC 次值 / TER 三级统计值 /
# NEU 前景色 / ACC 组色（字重 W_ACC）/ DIR、GIT 点缀 / WARN 提醒 / BAD 危险 / CRIT 强危险 /
# MUTED 弱化；底色按组（pcolor）。
# LB[id] = 标签（写法以它开头时，这段用组标签色 L_*，其余按角色上色）。
# 取舍顺序见「默认降级序列」（可被配置文件替换）；风险态由调用方置 NODROP。
declare -A GRP ROLE NODROP NV LV V VW LB
ITEMS=()
item() {
  local id=$1 g=$2 r=$3 k=0; shift 3
  [ -z "$1" ] && return
  ITEMS+=("$id"); GRP[$id]=$g; ROLE[$id]=$r; LV[$id]=0
  for t in "$@"; do dw "$t"; V[$id#$k]=$t; VW[$id#$k]=$DW; k=$((k + 1)); done
  NV[$id]=$k
}
E=$'\033'                # ESC 字节本身（约束 16）
JOIN=" "                 # 组内分隔：单个空格（「 · 」每处多占 2 列）

# 约束 13：显示宽度 → 全局 DW。先剔除本脚本用到的三字节窄符号（箭头、省略号、分支字形），
# 余下每个三字节字符（CJK / 全角）多占一列；多字节差 = 字节数 − 字符数，三字节字符各贡献 2。
# 非 UTF-8 locale 下 ${#s} 本就是字节数，结果偏大（宁可多降级，不溢出截断）。
dw() {
  local s=$1 n m b
  n=${#s}
  s=${s//[↑↓↺↻…⇡⇣·]/}; s=${s//$'\xee\x82\xa0'/}
  m=${#s}
  local LC_ALL=C
  b=${#s}
  DW=$(( n + (b - m) / 2 ))
}

# 整行宽度 → 全局 W：每个可见胶囊组 = 2 端帽 + 各项宽度 + 组内 (项数 − 1) 个 JOIN；组间 1 空格。
# 按组记「可见项数 GC」「宽度和 GS」，calc_width 全量建一次，之后 setlv 每改一项 O(1) 增量更新
# （此前每一步都全量重算，加上回填后 90 列时降级要多花约 5 ms，2026-10-28 实测）。
# 前提：同一组的项在 ITEMS 里连续出现（item 调用顺序保证），否则渲染会拆成两个胶囊。
declare -A GC GS
calc_width() {
  local id g
  GC=(); GS=(); SUMC=0; NG=0
  for id in "${ITEMS[@]}"; do
    g=${GRP[$id]}; : "${GC[$g]:=0}" "${GS[$g]:=0}"
    [ "${LV[$id]}" -lt 0 ] && continue
    GC[$g]=$(( GC[$g] + 1 )); GS[$g]=$(( GS[$g] + VW[$id#${LV[$id]}] ))
  done
  for g in "${!GC[@]}"; do
    [ "${GC[$g]}" -gt 0 ] && { SUMC=$(( SUMC + 2 + GS[$g] + (GC[$g] - 1) * ${#JOIN} )); NG=$((NG + 1)); }
  done
  W=$(( SUMC + (NG > 0 ? NG - 1 : 0) ))
}
# setlv <id> <新写法级别（-1 = 丢弃）>：改 LV 并增量更新 W
setlv() {
  local id=$1 nv=$2 g=${GRP[$1]} c0=0 c1=0
  [ "${GC[$g]}" -gt 0 ] && c0=$(( 2 + GS[$g] + (GC[$g] - 1) * ${#JOIN} ))
  [ "${LV[$id]}" -ge 0 ] && { GS[$g]=$(( GS[$g] - VW[$id#${LV[$id]}] )); GC[$g]=$(( GC[$g] - 1 )); }
  [ "$nv" -ge 0 ] && { GS[$g]=$(( GS[$g] + VW[$id#$nv] )); GC[$g]=$(( GC[$g] + 1 )); }
  [ "${GC[$g]}" -gt 0 ] && c1=$(( 2 + GS[$g] + (GC[$g] - 1) * ${#JOIN} ))
  [ "$c0" -gt 0 ] && [ "$c1" -eq 0 ] && NG=$((NG - 1))
  [ "$c0" -eq 0 ] && [ "$c1" -gt 0 ] && NG=$((NG + 1))
  SUMC=$(( SUMC - c0 + c1 )); LV[$id]=$nv
  W=$(( SUMC + (NG > 0 ? NG - 1 : 0) ))
}

# 按降级序列逐步执行，每步后复核宽度，放得下即停。不存在 / 已丢弃 / 已是最短写法 /
# 风险态不可丢的项，对应步骤直接跳过。
# 放得下之后回填：后面某一步可能一次省出很多列（如缩短长模型名），
# 前面那些牺牲就不必要了。从倒数第二步起逆序逐步撤销——越晚被牺牲的越重要、越先恢复——
# 撤销后仍在预算内才保留；最后一步正是让它放得下的那一步，撤销必然超宽，不试。
fit() {
  local st act id old new i ok=0 budget=$(( ${COLUMNS:-0} - SAFE_MARGIN ))
  local -a applied=()
  { [ -n "${COLUMNS:-}" ] && [ "$COLUMNS" -gt 10 ] 2>/dev/null; } || return
  calc_width; [ "$W" -le "$budget" ] && return
  for st in "${STEPS[@]}"; do
    act=${st%% *}; id=${st#* }
    case "$act" in
      drop)    [ -n "${LV[$id]+x}" ] && [ "${LV[$id]}" -ge 0 ] && [ -z "${NODROP[$id]:-}" ] || continue
               old=${LV[$id]}; setlv "$id" -1 ;;
      shrink)  [ -n "${LV[$id]+x}" ] && [ "${LV[$id]}" -ge 0 ] && [ $(( LV[$id] + 1 )) -lt "${NV[$id]}" ] || continue
               old=${LV[$id]}; setlv "$id" $(( old + 1 )) ;;
      *) continue ;;
    esac
    applied+=("$id $old ${LV[$id]}")
    [ "$W" -le "$budget" ] && { ok=1; break; }
  done
  [ "$ok" = 1 ] || return
  for (( i = ${#applied[@]} - 2; i >= 0; i-- )); do
    set -- ${applied[i]}; id=$1; old=$2; new=$3
    [ "${LV[$id]}" = "$new" ] || continue
    setlv "$id" "$old"; [ "$W" -le "$budget" ] || setlv "$id" "$new"
  done
}

# 项的文字颜色 → 全局 IC（约束 8：不用 $(...)）
icolor() {
  case "${ROLE[$1]}" in
    PRI)   IC="${E}[1;38;${T_NEU}m" ;;
    MODEL) IC="${E}[22;38;${T_MODEL}m" ;;
    SEC)   IC="${E}[22;38;${T_SEC}m" ;;
    TER)   IC="${E}[22;38;${T_TER}m" ;;
    ACC)   lcolor "$1"; IC="${E}[${W_ACC:-1};38;${LCV}m" ;;
    CRIT)  IC="${E}[1;38;${T_CRIT}m" ;;
    BAD)   IC="${E}[1;38;${T_BAD}m" ;;
    WARN)  IC="${E}[22;38;${T_WARN}m" ;;
    DIR)   IC="${E}[22;38;${T_DIR}m" ;;
    GIT)   IC="${E}[22;38;${T_GIT}m" ;;
    MUTED) IC="${E}[22;38;${T_MUTED}m" ;;
    *)     IC="${E}[22;38;${T_NEU}m" ;;
  esac
}

# 组标签色 → 全局 LCV（ctx 绿 / 额度紫 / 费用青 / 尾段弱化，以 Dracula 为例）
lcolor() {
  case "${GRP[$1]}" in
    ctx) LCV=$L_CTX ;; 5h) LCV=$L_QUOTA ;; 7d) LCV=$L_Q7 ;; usd|day) LCV=$L_USD ;; *) LCV=$T_MUTED ;;
  esac
}
# 组 → 胶囊底色 → 全局 PCV（告警胶囊 cache / claude 用 P_NEU）
pcolor() {
  case "$1" in
    git) PCV=$P_GIT ;; model) PCV=$P_MODEL ;; ctx) PCV=$P_CTX ;; 5h) PCV=$P_Q5 ;; 7d) PCV=$P_Q7 ;;
    usd|day) PCV=$P_USD ;; tail) PCV=$P_TAIL ;; *) PCV=$P_NEU ;;
  esac
}
# 一项的着色文本 → 全局 PT：标签段用组色、其余用角色色（调用前须已 icolor）
paint() {
  local l=${LB[$1]:-}
  if [ -n "$l" ] && [ "${2#"$l"}" != "$2" ]; then
    lcolor "$1"; PT="${E}[22;38;${LCV}m${l}${IC}${2#"$l"}"
  else
    PT="${IC}$2"
  fi
}

# 渲染：胶囊底色按组（pcolor），每一项按自己的角色上色（NEU 中性 / DIR、GIT 点缀 /
# WARN、BAD 语义色，BAD 加粗；MUTED 弱化）。尾段也包进胶囊。
render() {
  local id pg="" pr="" t j
  out=""
  for id in "${ITEMS[@]}"; do
    [ "${LV[$id]}" -lt 0 ] && continue
    t=${V[$id#${LV[$id]}]}
    if [ "${GRP[$id]}" = "$pg" ]; then                       # 同组续项
      j=$JOIN
      if [ "$NC" = 1 ]; then out="${out}${j}${t}"
      else icolor "$id"; paint "$id" "$t"; out="${out}${E}[22;38;${T_DIM}m${j}${PT}"; fi
      continue
    fi
    close_group
    [ -n "$pg" ] && out="${out} "
    pg=${GRP[$id]}; pr=${ROLE[$id]}
    if [ "$NC" = 1 ]; then
      out="${out}${PL_LCAP}${t}"
    else
      pcolor "$pg"; icolor "$id"; paint "$id" "$t"; out="${out}${E}[${CAPSGR};${PCV}m${PL_LCAP}${E}[48;${PCV}m${PT}"
    fi
  done
  close_group
}
close_group() {
  [ -z "$pg" ] && return
  if [ "$NC" = 1 ]; then out="${out}${PL_RCAP}"; return; fi
  pcolor "$pg"; out="${out}${E}[0m${E}[${CAPSGR};${PCV}m${PL_RCAP}${E}[0m"
}
# 无色模式下端帽一律换成空格（私用区字形在无色时没有意义）
[ "$NC" = 1 ] && { PL_LCAP=" "; PL_RCAP=" "; }
CAPSGR=38; [ "$GLYPHS" = ascii ] && CAPSGR=48           # ascii 端帽是空格：用底色填，免得两头露出终端背景

# 等级 → 角色：pl = 百分比等「现状」项；al = out 等「提醒」项
# 等级（statusline-tokens.py pace）：ok / warn 将提前用完 / high 已用 ≥ BAD_AT / bad 很快用完或用尽
# pl <等级> [已用%]：有百分比时按【当前值】定色（≥ 100 或 ≥ CRIT_AT → CRIT，≥ BAD_AT → BAD），
# 缓存等级只贡献「很快用完」（bad）——此前全看后台缓存，新会话 / 刚 /clear 时 82% 不红、
# 窗口刚重置时 3% 反被旧缓存标红（2026-10-28 终审）。无百分比（中转站日限额）时沿用等级。
pl() {
  if [ "$1" = bad ]; then R=CRIT
  elif [ -n "${2:-}" ]; then
    if [ "$2" -ge 100 ] || [ "$2" -ge "$CRIT_AT" ]; then R=CRIT; elif [ "$2" -ge "$BAD_AT" ]; then R=BAD; else R=PRI; fi
  elif [ "$1" = high ]; then R=BAD
  else R=PRI; fi
}
al() { [ "$1" = bad ] && R=CRIT || R=WARN; }
# risky <等级> [已用%]：有风险（会提前用完 / 已用 ≥ BAD_AT / 很快用完）→ 真
risky() { case "$1" in warn|high|bad) return 0 ;; esac; [ -n "${2:-}" ] && [ "$2" -ge "$BAD_AT" ]; }
# outtxt <用完倒计时> → 全局 OT：「用尽」原样，其余加 out 前缀
outtxt() { case "$1" in '') OT="" ;; 用尽) OT="用尽" ;; *) OT="out $1" ;; esac; }

# 环境
g=""; gs=""                                            # 长分支名（显示宽 > 24）可缩到 20 列 + …
if [ -n "$branch" ]; then
  g="${GLYPH_BRANCH}${branch}"; dw "$branch"
  if [ "$DW" -gt 24 ]; then                            # 按显示宽度截（中文分支名每字两列）
    b=$branch; while dw "$b"; [ "$DW" -gt 20 ]; do b=${b%?}; done
    gs="${GLYPH_BRANCH}${b}…"
  fi
fi
# 目录与分支同一胶囊；窄屏可缩成最后一段（仍永不丢弃）
dshort=""; [ "${path##*/}" != "$path" ] && dshort=${path##*/}
item dir   git DIR "$path" ${dshort:+"$dshort"}
item git   git GIT "$g" ${gs:+"$gs"}
item gitst git SEC "${gstatus:+[${gstatus}]}"
# 模型名缩写链：全名 → 去 "Claude " 前缀与 " (…)" 后缀 → 首词取首字母（Opus 5.5 → O5.5）→ effort 取首字母。
# 此前只按「两个词」处理，"Claude Opus 5.5 (1M context)" 会被缩成 "COpus 5.5 (1M context)"。
# 无空格的名字（deepseek-flash[1M]、kimi-k2-0905[256k]）取首个 "-" 段并保留方括号里的
# 上下文标记：deepseek[1M]；gpt-6.1-sol → gpt。带空格的仍按「首词首字母」缩（Opus 5.5 → O5.5）。
mbase=${model#Claude }; mbase=${mbase% (*)}
m1=${mbase%% *}; mshort=$mbase
if [ "$m1" != "$mbase" ]; then
  mshort="${m1:0:1}${mbase#* }"
elif [ "$mbase" != "${mbase%%-*}" ]; then
  mtail=""; case "$mbase" in *'['*) mtail="[${mbase#*[}" ;; esac
  mshort="${mbase%%-*}${mtail}"
fi
case "$eff" in low) e1=L ;; medium) e1=M ;; high) e1=H ;; xhigh) e1=XH ;; max) e1=MAX ;; *) e1=$eff ;; esac
mv=(); pv=""
for v in "${model}${eff:+ ${eff}}" "${mbase}${eff:+ ${eff}}" "${mshort}${eff:+ ${eff}}" "${mshort}${e1:+·${e1}}"; do
  [ "$v" != "$pv" ] && mv+=("$v"); pv=$v                 # 去掉与上一级相同的写法
done
[ -n "$model" ] && item model model MODEL "${mv[@]}"

# 预算：ctx / 5h / 7d（ctx 阈值 CTX_WARN / CTX_BAD，额度分级见 statusline-tokens.py）
if [ -n "$ctx" ]; then
  case "$ctxlv" in bad) [ "$ctx" -ge "$CRIT_AT" ] && R=CRIT || R=BAD ;; warn) R=WARN ;; *) R=PRI ;; esac
  item ctx   ctx "$R" "ctx ${ctx}%" "ctx${ctx}%"; LB[ctx]=ctx
  item ctxin ctx SEC "$ctxin"
  # compact 次数并入 ctx 胶囊、弱化（它解释 ctx 为何骤降，不是告警）
  [ -n "$comp" ] && [ "$comp" -gt 0 ] && item comp ctx SEC "↻${comp}"
fi
if [ -n "$q5" ]; then
  [ "$q5" -ge 100 ] && { q5lv=bad; q5out=用尽; }          # 用尽不等后台缓存
  pl "$q5lv" "$q5"; item q5 5h "$R" "5h ${q5}%" "5h${q5}%"; LB[q5]=5h
  [ "$q5" -ge "$WARN_AT" ] || risky "$q5lv" "$q5" && item q5r 5h SEC "${q5r:+↺${q5r}}"
  al "$q5lv"; outtxt "$q5out"; item q5out 5h "$R" "$OT" "!"
  risky "$q5lv" "$q5" && NODROP[q5]=1                        # 风险态：百分比随 out 一起不可丢
fi
if [ -n "$q7" ] && { [ "$q7" -ge "$WARN_AT" ] || risky "$q7lv" "$q7"; }; then
  [ "$q7" -ge 100 ] && { q7lv=bad; q7out=用尽; }
  pl "$q7lv" "$q7"; item q7 7d "$R" "7d ${q7}%" "7d${q7}%"; LB[q7]=7d
  item q7r 7d SEC "${q7r:+↺${q7r}}"
  al "$q7lv"; outtxt "$q7out"; item q7out 7d "$R" "$OT" "!"
  risky "$q7lv" "$q7" && NODROP[q7]=1
fi

# 费用（见头部「费用口径」）
if [ "$mode" = relay ] && [ -n "$r_cost" ]; then
  money "$r_cost"; c=$MONEY; [ "${r_unpriced:-0}" -gt 0 ] 2>/dev/null && c="${c}+"
  item cost usd ACC "$c"
  item rate usd SEC "$p_rate"
  if [ -n "$sp" ]; then
    :                                          # 网关花费限额优先于 CC Switch 日限额（见下）
  elif [ -n "$r_limit" ]; then
    money "$r_today"; t1=$MONEY; money "$r_limit"
    pl "$dlv"; item day  day "$R" "${t1}/${MONEY}"
    al "$dlv"; outtxt "$dout"; item dout day "$R" "$OT" "!"
    risky "$dlv" && NODROP[day]=1
  else
    money "$r_today"
    item today usd SEC "${MONEY}"
  fi
elif [ -n "$costraw" ]; then
  money "$costraw"
  if [ "$mode" = sub ]; then item cost usd ACC "$MONEY"
  else                       item cost usd ACC "${MONEY}*"; fi
  item rate usd SEC "$p_rate"
fi
# 网关花费限额 rate_limits.spend_limit：今日 / 本周 / 本月 $已用/$限额，
# 缺美元时显示百分比；着色与 5h / 7d 同规则，节奏由 statusline-tokens.py 按 period 窗口外推
if [ -n "$sp" ]; then
  case "$spper" in daily) pn=今日 ;; weekly) pn=本周 ;; monthly) pn=本月 ;; *) pn=限额 ;; esac
  if [ -n "$spusd" ] && [ -n "$splim" ]; then
    money "$spusd"; t1=$MONEY; money "$splim"; v0="${pn} ${t1}/${MONEY}"; v1="${t1}/${MONEY}"
  else
    v0="${pn} ${sp}%"; v1="${sp}%"
  fi
  [ "$sp" -ge 100 ] && { splv=bad; spout=用尽; }
  pl "$splv" "$sp"; item spend day "$R" "$v0" "$v1"; LB[spend]=$pn
  al "$splv"; outtxt "$spout"; item spout day "$R" "$OT" "!"
  risky "$splv" "$sp" && NODROP[spend]=1
fi

# 告警：cache 将过期（黄）/ 已冷（红）、Claude 故障（major / critical，红）
ct=""
case "$cache" in
  '') ;;
  cold) item cache cache BAD "cache cold" "cold"; item recache cache BAD "${recache:++${recache}}" ;;
  *) read -r left ttl <<< "$cache"
     if [ "$left" -ge 60 ]; then cm="$((left / 60))m"; else cm="${left}s"; fi
     if [ $((left * 6)) -lt "$ttl" ]; then
       item cache cache WARN "cache ${cm}" "c${cm}"; item recache cache WARN "${recache:++${recache}}"
     elif [ $((left * 2)) -lt "$ttl" ]; then
       ct=$cm                                   # 过半：尾段弱化；未过半则隐藏
     fi ;;
esac
case "$st_ind" in
  major|critical) item claude claude BAD "claude ${st_ind}" "$st_ind" ;;
esac

# 辅助尾段（同一胶囊；标签弱化色、值次值灰）。hit 用会话级累计（statusline-tokens.py），拿不到再回落 stdin
case "$st_ind" in minor|maintenance) item claudem tail MUTED "claude ${st_ind}" "$st_ind" ;; esac
if [ -n "$t_hit" ]; then hit=$t_hit; hit0=${t_hit%.*}; [ "${t_hit#*.}" -ge 5 ] 2>/dev/null && hit0=$((hit0 + 1)); fi
item cachet tail TER "${ct:+cache ${ct}}" "$ct"; LB[cachet]=cache
item hit    tail TER "${hit:+hit ${hit}%}" "${hit0}%"; LB[hit]=hit
item io     tail TER "${t_in:+↑${t_in} ↓${t_out}}" "${t_in:+↑${t_in}}"
item api    tail TER "${apidur:+api ${apidur}}" "$apidur"; LB[api]=api
item msgs   tail TER "${msgs:+m${msgs}}"

fit
render
printf '%s' "$out"                     # 约束 16：%s 而非 %b，外部文本里的反斜杠不被解释

# ── 脱钩后台刷新（慢段下一周期更新；abort 杀掉也无害，下轮重试）──────────
[ -n "${CLAUDE_SL_NO_REFRESH:-}" ] || \
( "$BASH" "$SELF" --refresh "$dir" "${tp:--}" "${sid:--}" "$mode" "${costraw:--}" "${q5arg:--}" "${q7arg:--}" "${sparg:--}" >/dev/null 2>&1 & ) 2>/dev/null   # 测试 / 预览设 CLAUDE_SL_NO_REFRESH=1：不派生后台刷新，结果确定、无残留进程

true
