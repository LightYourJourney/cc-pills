# ═══ 缓存与后台任务 ════════════════════════════════════════════════════
# 随 tools/doctor.sh 一起 source；必须保持 bash 3.2 兼容。
sec 缓存与后台任务
SLT=$F_slt; src_note=""                 # 缓存根由渲染脚本自述；显式 CLAUDE_SL_TMP 时注明来源
[ "$F_slt_explicit" = 1 ] && src_note="（CLAUDE_SL_TMP）"
SLT_OK=$F_slt_ok                        # 可不可用由渲染脚本说了算；下面只负责解释为什么不可用
if [ "$SLT_OK" = 1 ]; then
  res PASS 缓存/根 "$SLT$src_note"
elif [ -L "$SLT" ]; then
  res FAIL 缓存/根 "$SLT$src_note 是符号链接：渲染不会跟随" "换成真实目录，或删掉链接后让刷新重建"
elif [ ! -e "$SLT" ]; then
  if [ "$F_slt_explicit" = 1 ]; then
    res FAIL 缓存/根 "${SLT}（CLAUDE_SL_TMP）不存在：显式路径必须由调用方建好，后台刷新不会创建" \
      "mkdir -m 700 \"$SLT\"，或取消 CLAUDE_SL_TMP"
  else
    res SKIP 缓存/根 "$SLT 不存在；首次后台刷新会以 0700 创建"
  fi
elif [ ! -d "$SLT" ]; then
  res FAIL 缓存/根 "$SLT$src_note 存在但不是目录" "删掉它：rm -f \"$SLT\"（下次刷新重建）"
else
  res FAIL 缓存/根 "$SLT$src_note 不属于当前用户；渲染会拒绝读取" "换一个 CLAUDE_SL_TMP，或删掉该目录"
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

# 色板：路径、期望首行、此刻能不能上色都由渲染脚本自述（终端键只为显示从文件名里取）
tc=$F_theme_cache
PKEY=${tc##*/claude-sl-theme-}; PKEY=${PKEY%.cache}
COLOUR_OK=0     # 渲染侧此刻能不能上色（不看终端色深；色深见 环境/终端）
if [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/色板 "缓存根不可用"
elif [ ! -e "$tc" ]; then
  res WARN 缓存/色板 "本终端（${PKEY}）没有色板缓存：整行会无色，等下一轮后台刷新（约 2 秒）" \
    "若持续无色，检查 环境/python3 与 缓存/退避"
elif [ -L "$tc" ] || [ ! -f "$tc" ] || [ ! -O "$tc" ]; then
  res WARN 缓存/色板 "色板缓存不是属于本人的普通文件（目录或符号链接），渲染会拒绝读取（整行无色）" "删掉它：rm -f \"$tc\""
else
  # 上色条件由渲染脚本判（主题部分与当前 theme 相同且有合法的 P_CTX）；chroma / 风格 /
  # 格式版本不符只触发后台重算，本轮照常上色。
  COLOUR_OK=$F_palette_usable
  line=""; read -r line < "$tc"
  line=${line//[[:cntrl:]]/}                       # 与渲染侧约束 16 一致：别把控制字符打上屏
  want=$F_theme_want
  # 装着旧副本（COPY_OLD）时色板是它按自己的格式版本写的：只比主题 / 彩度 / 风格
  [ "$COPY_OLD" = 1 ] && { line=${line%/v*}; want=${want%/v*}; }
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
# 会话是否仍活跃：会话状态里记的任一 transcript 在 TOK_STALE 内写过 → 真。已结束的会话缓存陈旧是
# 常态（不再刷新），不该报 WARN；判断不了（没有状态文件 / 没有 jq / 解析失败 / files 不是对象或
# 一条路径都没记）按活跃处理——宁可多报一条，不漏掉后台停更
sess_active() {  # sess_active <token 缓存路径>
  local sid st paths p m
  sid=${1##*/claude-sl-tok-}; sid=${sid%.cache}
  st="$SLT/claude-statusline-$sid.json"
  [ -L "$st" ] && return 0                        # 没有 jq / 文件不在时下面的 jq 自己失败
  paths=$(jq -r '.files | if type == "object" and length > 0 then keys[] else error end' "$st" 2>/dev/null) || return 0
  while IFS= read -r p; do
    m=$(mtime "$p")
    [ -n "$m" ] && [ $(( NOW - m )) -le "$TOK_STALE" ] && return 0
  done <<< "$paths"
  return 1
}
tok_newest=-1; tok_n=0; tok_notmine=0; tok_tsbad=0; tok_malformed=0; tok_link=0
tok_old_ts=0; tok_no_ts_old=0; tok_idle=0
for f in "$SLT"/claude-sl-tok-*.cache; do
  stale=""                                        # 陈旧时是哪一种：old_ts（有 .ts）/ no_ts（按 mtime）
  if [ -L "$f" ]; then tok_link=$((tok_link + 1)); continue; fi
  [ -f "$f" ] || continue
  tok_n=$((tok_n + 1))
  [ -O "$f" ] || tok_notmine=$((tok_notmine + 1))
  # 首行是格式版本；不符的缓存渲染侧整份不读，所以也不参与下面的新鲜度统计。装着旧副本
  # （COPY_OLD）时不判：那些缓存是它写的、它照常读，仓库这一版的格式不适用。
  _first=""; read -r _first < "$f" 2>/dev/null
  if [ "$COPY_OLD" != 1 ] && [ "$_first" != "TOK=$F_tok_v" ]; then
    tok_malformed=$((tok_malformed + 1)); continue
  fi
  ts=$f.ts
  if [ -f "$ts" ] && [ ! -L "$ts" ] && [ -O "$ts" ]; then
    tv=""; read -r tv < "$ts"
    case "$tv" in
      ''|*[!0-9]*) tok_tsbad=$((tok_tsbad + 1)); t=0 ;;
      *) t=$((10#$tv)); [ $(( NOW - t )) -gt "$TOK_STALE" ] && stale=old_ts ;;
    esac
  elif [ -e "$ts" ] || [ -L "$ts" ]; then
    tok_tsbad=$((tok_tsbad + 1)); t=0
  else
    t=$(( $(mtime "$f") ))
    [ $(( NOW - t )) -gt "$TOK_STALE" ] && stale=no_ts
  fi
  if [ -n "$stale" ]; then
    if ! sess_active "$f"; then tok_idle=$((tok_idle + 1))
    elif [ "$stale" = old_ts ]; then tok_old_ts=$((tok_old_ts + 1))
    else tok_no_ts_old=$((tok_no_ts_old + 1)); fi
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
  elif [ "$tok_tsbad" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_tsbad 个新鲜度时间戳损坏或不属于本人：渲染侧会判陈旧，\$/h、out、hit、io、msgs 被隐藏" \
      "删掉对应的 .ts，让后台刷新重写"
  elif [ "$tok_old_ts" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_old_ts 个缓存超过 TOK_STALE=${TOK_STALE}，其会话仍在活跃（transcript 近 ${TOK_STALE}s 内有写入）或无法判断：\$/h、out、hit、io、msgs 会被隐藏" \
      "回到那个会话让它刷新；若持续不更新，看 数据/stdin 与 缓存/后台刷新"
  elif [ "$tok_no_ts_old" -gt 0 ]; then
    res WARN 缓存/token "有 $tok_no_ts_old 个缓存没有新鲜度时间戳、且已超过 TOK_STALE：本轮仍沿用旧值，后台刷新按 mtime 回填 .ts 后派生项会被隐藏" \
      "让该会话刷新一次即可补写 .ts；持续出现就查后台 token 计算是否在更新缓存"
  elif [ "$tok_malformed" -gt 0 ]; then
    # 排在陈旧之后：升级后已结束会话的旧格式缓存会留很久，不能盖住活跃会话的陈旧告警
    res WARN 缓存/token "有 $tok_malformed 个缓存不是当前格式（首行应为 TOK=${F_tok_v}；旧版留下的或被截断）：渲染侧整份不读，派生项消失" \
      "回到那个会话让它刷新一次即可重写；已结束的会话留着无害"
  else
    idle_note=""; [ "$tok_idle" -gt 0 ] && idle_note="；其中 $tok_idle 个属已闲置的会话（transcript 超过 TOK_STALE 没写过），缓存陈旧是正常的"
    res PASS 缓存/token "$tok_n 个会话缓存，最新 ${age}s 前${idle_note}"
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
  linknote=""
  [ "$gcache_link" -gt 0 ] && linknote="（${gcache_link} 个符号链接，渲染不会读取）"
  res SKIP 缓存/git "$DIR 不是 git 仓库；缓存 $gcache_n 个${linknote}"
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
         age=$(( NOW - 10#$st_ts ))
         if [ "$age" -gt "$STATUS_STALE" ]; then
           res WARN 缓存/状态页 "状态缓存 ${age}s 未更新（超过 STATUS_STALE=${STATUS_STALE}）：服务状态项被隐藏" \
             "确认网络与 curl 可用（见 环境/curl）；后台每分钟会重试"
         else
           res PASS 缓存/状态页 "${age}s 前更新"
         fi
       fi ;;
  esac
fi

# 刷新锁：活跃会话的残锁下一轮就被抢掉，已结束会话的由每日清理删；锁路径被普通文件 / 符号链接占住时，渲染侧会先
# rm -f 再重试、下一轮自愈，但私有目录里出现这种占位不正常，报 WARN 提醒
lock_n=0; lock_old=""; lock_bad=""; lock_full=""
for l in "$SLT"/claude-sl-lock-*; do
  { [ -e "$l" ] || [ -L "$l" ]; } || continue
  if [ -L "$l" ] || [ ! -d "$l" ]; then lock_bad="$lock_bad ${l##*/}"; continue; fi
  # 不递归、不跟随符号链接；空锁目录就是正常的锁
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
  res WARN 缓存/锁 "锁路径不是目录（普通文件或符号链接）${lock_bad}：刷新会先清掉它再抢锁" \
    "无需手工处理；反复出现再检查缓存目录：rm -f \"$SLT\"/claude-sl-lock-*"
elif [ -n "$lock_old" ]; then
  # 自述不报 cache_max_age 的副本（1.0.6 之前）根本不清理锁：不能说「会被删」，给出手工命令
  gone="会话已结束的由每日清理在 ${CACHE_MAX_AGE} 天后删掉"
  [ -n "$CACHE_MAX_AGE" ] || gone="会话已结束的留着（已安装副本没报清理天数，多半是不清理锁的旧版；重装即可，或手工 rmdir \"$SLT\"/claude-sl-lock-*）"
  [ "$F_slt_explicit" = 1 ] && gone="会话已结束的留着（显式 CLAUDE_SL_TMP 不清理，归调用方）"
  res PASS 缓存/锁 "残锁${lock_old}（超过 LOCK_STALE=${LOCK_STALE}）：该会话下次刷新时会清除，${gone}；留着无害"
elif [ "$lock_n" -gt 0 ]; then
  res PASS 缓存/锁 "有刷新正在进行"
else
  res PASS 缓存/锁 "无锁残留"
fi

# 面板共享状态（subagent-statusline.sh）：与主栏共用缓存根，文件名固定带 uid。
# 只判归属、类型与权限，不读内容（里面只有每个 task 的速率基线）。
sstate=$P_state                         # 面板脚本自述的状态文件路径
if [ "$P_state_explicit" = 1 ]; then
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

# 面板锁：结构问题（锁路径被非目录占住）当场判；写回失败由面板自己落 STATE.fail，
# 这里只读结果，不再推演锁目录的年龄与 PID。
plock="$sstate.lock"; pmark="$sstate.fail"
if [ "$P_state_explicit" = 1 ]; then
  res SKIP 缓存/面板锁 "CLAUDE_SUBAGENT_SL_STATE 指定了状态文件，锁由调用方负责"
elif [ "$SLT_OK" != 1 ]; then
  res SKIP 缓存/面板锁 "缓存根不可用"
elif [ -L "$plock" ] || { [ -e "$plock" ] && [ ! -d "$plock" ]; }; then
  res WARN 缓存/面板锁 "面板锁路径被普通文件或符号链接占住（面板每轮会先清掉再抢锁）" \
    "反复出现时检查缓存目录；手工处理：rm -f \"$plock\""
elif [ -L "$pmark" ] || { [ -e "$pmark" ] && [ ! -f "$pmark" ]; }; then
  res WARN 缓存/面板锁 "写回失败标记不是普通文件（目录或符号链接）：读不出失败原因；是目录时面板也写不进去" \
    "rm -rf \"$pmark\""
elif [ -e "$pmark" ]; then
  ft=""; why=""; { read -r ft why < "$pmark"; } 2>/dev/null
  # 读不出 / 非数字 / 写在未来的时间戳都按「刚刚」算；10# 免得前导 0 被当成八进制
  case "$ft" in ''|*[!0-9]*) ft=$NOW ;; *) ft=$((10#$ft)) ;; esac
  age=$(( NOW - ft )); [ "$age" -lt 0 ] && age=0
  case "$why" in
    busy)  why="抢锁超时" ;;
    merge) why="状态合并失败（stdin 或状态无法解析）" ;;
    tmp)   why="临时文件创建失败" ;;
    write) why="写入失败" ;;
    mv)    why="替换失败" ;;
    *)     why="未知原因" ;;
  esac
  if [ "$age" -ge "$PANEL_FAIL_STALE" ]; then
    res WARN 缓存/面板锁 "上次写回失败在 ${age}s 前，此后没有写回尝试（面板可能已停跑）：${why}" \
      "确认面板已启用；清掉标记：rm -f \"$pmark\""
  else
    res WARN 缓存/面板锁 "上次写回失败（${age}s 前，原因：${why}）：速率段可能缺失，下次成功写回会自动清除"
  fi
else
  res PASS 缓存/面板锁 "无写回失败记录"
fi

failf=$tc.fail
if [ -L "$failf" ]; then
  res WARN 缓存/退避 "退避文件是符号链接，渲染不会读取：色板生成失败时会每轮重试" "rm -f \"$failf\""
elif [ -e "$failf" ]; then
  ft=""; read -r ft < "$failf" 2>/dev/null
  case "$ft" in ''|*[!0-9]*) ft=$NOW ;; *) ft=$((10#$ft)) ;; esac
  if [ "$COLOUR_OK" = 1 ]; then
    # 渲染侧只有「首行与配置不符」时才让 .fail 阻止重试；色板有效时退避不影响任何渲染
    res WARN 缓存/退避 "色板生成上次失败（$(( NOW - ft ))s 前）；当前色板缓存有效，渲染不受影响，后台按 THEME_TTL 重试"
  else
    res WARN 缓存/退避 "色板生成上次失败（$(( NOW - ft ))s 前），且没有可用色板：整行无色，后台按 THEME_TTL 重试" \
      "手工重跑看报错：python3 \"$DEST/statusline-theme.py\" $C_THEME /tmp/palette.txt $C_CHROMA $C_STYLE ${F_theme_want##*/v}"
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

