# settings.json 里的命令是否指向已安装的脚本。install.sh 与 tools/doctor.sh 共用这一份
# （此前两边各抄一份、靠测试比对结论）。只随仓库提供，不进安装副本；必须保持 bash 3.2 兼容。

# 剥掉「文件名后紧跟单词字符或 /」的出现（statusline-command.sh.bak / .tmp / 同名目录
# 指向的都是另一个路径），剩下的才是真的引用；全都被后缀黏住时结果不含文件名。→ 全局 CLEAN。
# 逐次剥离而不是一次 case：同一条命令里可能既有 .bak 又有真引用。
strip_dirty() {
  local rest=$1 out=""
  while case "$rest" in *"$2"*) true ;; *) false ;; esac; do
    out=$out${rest%%"$2"*}
    rest=${rest#*"$2"}
    case "$rest" in
      [A-Za-z0-9_./-]*) ;;     # 脏：不把文件名放进结果，后缀留给下一轮
      *) out=$out$2 ;;         # 干净
    esac
  done
  CLEAN=$out$rest
}

# 路径片段左边界校验：须在开头或紧跟空白 / 引号 / 赋值 / 命令分隔符，不能接在别的目录后
# （避免 /tmp/backup/Users/leo/.claude/statusline-command.sh 被误判为指向已安装副本）。
has_path_tok() {
  local rest=$1 tok=$2 pre
  while case "$rest" in *"$tok"*) true ;; *) false ;; esac; do
    pre=${rest%%"$tok"*}
    case "$pre" in
      ''|*[[:space:]\"\'\=\;\|\&\(]) return 0 ;;
    esac
    rest=${rest#*"$tok"}
  done
  return 1
}

points_to() {  # points_to <command> <脚本名> <DEST> → 0 明确指向 / 2 只含名字 / 1 未指向
  local d=$3
  while [ "${d%/}" != "$d" ] && [ "$d" != "/" ]; do d=${d%/}; done   # 剥净全部尾斜杠
  strip_dirty "$1" "$2"
  has_path_tok "$CLEAN" "$d/$2" && return 0                          # 指向已安装副本
  has_path_tok "$CLEAN" '$CLAUDE_CONFIG_DIR/'"$2" && return 0        # 无花括号等价写法
  case "$CLEAN" in
    # 建议写法：运行时展开到同一目录。默认值只认默认目录——${CLAUDE_CONFIG_DIR:-/别处}
    # 在变量未设时跑的是别处的脚本
    *'${CLAUDE_CONFIG_DIR}/'"$2"*|*'${CLAUDE_CONFIG_DIR:-$HOME/.claude}/'"$2"*|\
    *'${CLAUDE_CONFIG_DIR:-~/.claude}/'"$2"*|*'${CLAUDE_CONFIG_DIR:-'"$HOME/.claude}/$2"*) return 0 ;;
  esac
  if [ "$d" = "$HOME/.claude" ]; then
    has_path_tok "$CLEAN" '$HOME/.claude/'"$2" && return 0
    has_path_tok "$CLEAN" '${HOME}/.claude/'"$2" && return 0
    # shellcheck disable=SC2088  # 有意匹配 settings.json 里的字面 ~/.claude/ 写法
    has_path_tok "$CLEAN" '~/.claude/'"$2" && return 0
  fi
  case "$CLEAN" in *"$2"*) return 2 ;; esac              # 只是文件名相同，路径不明
  return 1
}
