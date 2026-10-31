# ═══ 缓存与后台任务 ════════════════════════════════════════════════════
# 随 tools/doctor.sh 一起 source；必须保持 bash 3.2 兼容。
sec 缓存与后台任务
if [ -n "${CLAUDE_SL_TMP:-}" ]; then
  SLT=$CLAUDE_SL_TMP; src_note="（CLAUDE_SL_TMP）"
else
  tmpdir=${TMPDIR:-/tmp}; tmpdir=${tmpdir%/}       # TMPDIR 常以 / 结尾，显示时避免 //
  SLT=$tmpdir/claude-sl-${UID:-0}; src_note=""
fi
SLT_OK=0
if [ -L "$SLT" ]; then
  res FAIL 缓存/根 "$SLT$src_note 是符号链接：渲染不会跟随" "换成真实目录，或删掉链接后让刷新重建"
elif [ ! -e "$SLT" ]; then
  if [ -n "${CLAUDE_SL_TMP:-}" ]; then
    res FAIL 缓存/根 "${SLT}（CLAUDE_SL_TMP）不存在：显式路径必须由调用方建好，后台刷新不会创建" \
      "mkdir -m 700 \"$SLT\"，或取消 CLAUDE_SL_TMP"
  else
    res SKIP 缓存/根 "$SLT 不存在；首次后台刷新会以 0700 创建"
  fi
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
elif [ -L "$tc" ] || [ ! -f "$tc" ] || [ ! -O "$tc" ]; then
  res WARN 缓存/色板 "色板缓存不是属于本人的普通文件（目录或符号链接），渲染会拒绝读取（整行无色）" "删掉它：rm -f \"$tc\""
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
tok_newest=-1; tok_n=0; tok_notmine=0; tok_tsbad=0; tok_malformed=0; tok_link=0
tok_old_ts=0; tok_no_ts_old=0
for f in "$SLT"/claude-sl-tok-*.cache; do
  if [ -L "$f" ]; then tok_link=$((tok_link + 1)); continue; fi
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
elif [ "$tok_link" -gt 0 ]; then
  res WARN 缓存/token "有 $tok_link 个 token 缓存是符号链接，渲染会拒绝读取" "rm -f \"$SLT\"/claude-sl-tok-*"
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
gcache_n=0; gcache_bad=0; gcache_link=0
for f in "$SLT"/claude-sl-git-*.cache; do
  if [ -L "$f" ]; then gcache_link=$((gcache_link + 1)); continue; fi
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
  [ "$gcache_link" -gt 0 ] && badnote="（${gcache_link} 个符号链接会被后台原子替换）"
  [ "$gcache_bad" -gt 0 ] && badnote="${badnote}（${gcache_bad} 个格式异常，后台会重算）"
  res PASS 缓存/git "$DIR 是 git 仓库；缓存 ${gcache_n} 个${badnote}（缺失时首次渲染会同步算一次）"
else
  res SKIP 缓存/git "$DIR 不是 git 仓库；缓存 $gcache_n 个"
fi

# 状态页缓存：只报年龄，不主动拉取（默认不联网）。缺失 / status_page=off 都是 SKIP。
stf="${SLT}/claude-sl-status.cache"
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/状态页 "缓存根不可用"
elif [ "$C_PAGE" = off ]; then
  res SKIP 缓存/状态页 "配置 status_page = off，不使用状态页"
elif [ ! -e "$stf" ] && [ ! -L "$stf" ]; then
  res SKIP 缓存/状态页 "暂无状态缓存（后台刷新还没拉取过）"
elif [ -L "$stf" ] || [ ! -f "$stf" ]; then
  res WARN 缓存/状态页 "状态缓存不是普通文件（目录或符号链接）：服务状态项会被隐藏" \
    "rm -rf \"$stf\""
elif [ ! -O "$stf" ]; then
  res WARN 缓存/状态页 "状态缓存不属于当前用户，会被忽略" "rm -f \"$stf\""
else
  st_ts=""; st_ind=""; IFS=$'\t' read -r st_ts st_ind < "$stf" 2>/dev/null
  case "$st_ts" in
    ''|*[!0-9]*) res WARN 缓存/状态页 "状态缓存格式损坏：服务状态项会被隐藏" "删掉它，后台会重新拉取" ;;
    *) if [ -z "$st_ind" ]; then
         res WARN 缓存/状态页 "状态缓存缺少 indicator：服务状态项会被隐藏" "删掉它，后台会重新拉取"
       else
         age=$(( NOW - st_ts ))
         if [ "$age" -gt "$STATUS_STALE" ]; then
           res WARN 缓存/状态页 "状态缓存 ${age}s 未更新（超过 STATUS_STALE=${STATUS_STALE}）：服务状态项被隐藏" \
             "确认网络与 curl 可用（见 环境/curl）；后台每分钟会重试"
         else
           res PASS 缓存/状态页 "${age}s 前更新"
         fi
       fi ;;
  esac
fi

# 刷新锁：残锁目录会被下一轮自动清除；锁路径被普通文件占住时 mkdir 永远失败、后台刷新
# 从此每轮直接退出（渲染侧不会自愈），必须当故障报出来
lock_n=0; lock_old=""; lock_bad=""; lock_full=""
for l in "$SLT"/claude-sl-lock-*; do
  { [ -e "$l" ] || [ -L "$l" ]; } || continue
  if [ -L "$l" ] || [ ! -d "$l" ]; then lock_bad="$lock_bad ${l##*/}"; continue; fi
  if [ -n "$(find "$l" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]; then
    lock_full="$lock_full ${l##*/}"; continue
  fi
  lock_n=$((lock_n + 1))
  lt=$(mtime "$l"); lt=${lt:-$NOW}
  age=$(( NOW - lt ))
  if [ "$age" -ge "$LOCK_STALE" ]; then lock_old="$lock_old ${l##*/}(${age}s)"; fi
done
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/锁 "缓存根不可用"
elif [ -n "$lock_full" ]; then
  res FAIL 缓存/锁 "锁目录非空${lock_full}：渲染侧只用 rmdir，永远删不掉，后台刷新会一直退出（不会自愈）" \
    "rm -rf \"$SLT\"/claude-sl-lock-*"
elif [ -n "$lock_bad" ]; then
  res FAIL 缓存/锁 "锁路径不是目录（普通文件或符号链接）${lock_bad}：mkdir 永远失败，后台刷新会一直退出（不会自愈）" \
    "rm -f \"$SLT\"/claude-sl-lock-*"
elif [ -n "$lock_old" ]; then
  res WARN 缓存/锁 "残锁${lock_old}（超过 LOCK_STALE=${LOCK_STALE}）：下次刷新会自动清除，无需手工处理"
elif [ "$lock_n" -gt 0 ]; then
  res PASS 缓存/锁 "有刷新正在进行"
else
  res PASS 缓存/锁 "无锁残留"
fi

# 面板共享状态（subagent-statusline.sh）：与主栏共用缓存根，文件名固定带 uid。
# 只判归属、类型与权限，不读内容（里面只有每个 task 的速率基线）。
sstate="$SLT/claude-subagent-sl-${UID:-0}.json"
if [ -n "${CLAUDE_SUBAGENT_SL_STATE:-}" ]; then
  res SKIP 缓存/面板状态 "CLAUDE_SUBAGENT_SL_STATE 指定了文件，归属与生命周期由调用方负责"
elif [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/面板状态 "缓存根不可用"
elif [ ! -e "$sstate" ] && [ ! -L "$sstate" ]; then
  res SKIP 缓存/面板状态 "暂无面板状态（还没渲染过 subagent 行）"
elif [ -L "$sstate" ] || [ ! -f "$sstate" ]; then
  res WARN 缓存/面板状态 "面板状态不是普通文件（目录或符号链接）：渲染会忽略它，速率段一直缺失" \
    "rm -rf \"$sstate\""
elif [ ! -O "$sstate" ]; then
  res WARN 缓存/面板状态 "面板状态不属于当前用户，会被忽略：速率段缺失" "rm -f \"$sstate\""
else
  smode=$(stat_mode "$sstate")
  if [ "$smode" = 600 ]; then
    res PASS 缓存/面板状态 "0600，$(( NOW - $(mtime "$sstate") ))s 前写入"
  else
    res WARN 缓存/面板状态 "面板状态权限 ${smode}，应为 600（同机其他用户可读你的 agent 活动）" \
      "chmod 600 \"$sstate\""
  fi
fi

failf=$tc.fail
if [ -L "$failf" ]; then
  res WARN 缓存/退避 "退避文件是符号链接，渲染不会读取：色板生成失败时会每轮重试" "rm -f \"$failf\""
elif [ -e "$failf" ]; then
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
elif ! ver_ge "$pver" "$PY_MIN"; then
  res SKIP 缓存/后台刷新 "python3 ${pver} 低于 ${PY_MIN}：后台刷新算不了 token 与色板（见 环境/python3）"
elif [ -w "$SLT" ]; then
  res PASS 缓存/后台刷新 "缓存目录可写；python3 ${pver}；刷新不阻塞渲染（脱钩进程）"
else
  res FAIL 缓存/后台刷新 "缓存目录不可写：后台刷新无法落盘" "chown/chmod 修好 \"$SLT\"，或设 CLAUDE_SL_TMP 到可写目录"
fi

