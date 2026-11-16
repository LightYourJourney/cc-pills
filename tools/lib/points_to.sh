# settings.json 里的命令是否指向已安装的脚本。install.sh 与 tools/doctor.sh 共用这一份
# （此前两边各抄一份、靠测试比对结论）。只随仓库提供，不进安装副本；必须保持 bash 3.2 兼容。

# 剥掉「文件名后紧跟单词字符」的出现（statusline-command.sh.bak / .tmp 指向的是另一个文件），
# 剩下的才是真的引用；全都被后缀黏住时结果不含文件名。→ 全局 CLEAN。
# 逐次剥离而不是一次 case：同一条命令里可能既有 .bak 又有真引用。
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

points_to() {  # points_to <command> <脚本名> <DEST> → 0 明确指向 / 2 只含名字 / 1 未指向
  strip_dirty "$1" "$2"
  case "$CLEAN" in
    *"$3/$2"*)                    return 0 ;;        # 指向已安装副本
    *'${CLAUDE_CONFIG_DIR'*"$2"*) return 0 ;;        # 建议写法：运行时展开到同一目录
  esac
  if [ "$3" = "$HOME/.claude" ]; then
    case "$CLEAN" in
      *'$HOME/.claude/'*"$2"*|*'~/.claude/'*"$2"*) return 0 ;;   # 默认目录的等价写法
    esac
  fi
  case "$CLEAN" in *"$2"*) return 2 ;; esac              # 只是文件名相同，路径不明
  return 1
}
