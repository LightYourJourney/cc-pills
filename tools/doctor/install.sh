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
elif [ ! -e "$sj" ] && [ ! -L "$sj" ]; then
  res FAIL 安装/settings.json "$sj 不存在" \
    '写入 "statusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\""}'
elif [ ! -f "$sj" ]; then
  res FAIL 安装/settings.json "$sj 不是普通文件（目录或悬空符号链接），读不到 statusLine 配置" \
    "修好该路径：删除它后重新运行 bash install.sh"
elif [ ! -r "$sj" ]; then
  res FAIL 安装/settings.json "$sj 不可读（权限）：Claude Code 读不到 statusLine 配置" \
    "chmod u+r \"$sj\""
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

if [ ! -e "$CONF" ] && [ ! -L "$CONF" ]; then
  res SKIP 安装/配置文件 "$CONF 不存在（全部使用内置默认）"
elif [ ! -f "$CONF" ]; then
  res WARN 安装/配置文件 "$CONF 不是普通文件（目录或悬空符号链接）；渲染会静默改用内置默认" \
    "修好该路径：删除它，或让 $CONF 指向一个普通文件"
elif [ "$CFG_READABLE" = 0 ]; then
  res WARN 安装/配置文件 "$CONF 存在但不可读；渲染会静默改用内置默认，状态栏照常工作" "chmod 600 \"$CONF\""
elif [ -n "$CFG_UNKNOWN" ]; then
  res WARN 安装/配置文件 "这些键不认识、会被静默忽略：$CFG_UNKNOWN" "删掉它们，或核对 config/claude-statusline.conf.example"
elif [ -n "$CFG_BAD" ]; then
  res WARN 安装/配置文件 "这些键的值不合法、会被静默忽略：$CFG_BAD" "核对 config/claude-statusline.conf.example 里的可选值"
else
  res PASS 安装/配置文件 "可解析：theme=$C_THEME pill_style=$C_STYLE status_page=$C_PAGE"
fi

