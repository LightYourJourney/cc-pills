# ═══ 运行环境 ══════════════════════════════════════════════════════════
# 随 tools/doctor.sh 一起 source；必须保持 bash 3.2 兼容。
sec 运行环境
os=$(uname -s 2>/dev/null); arch=$(uname -m 2>/dev/null)
osver=""
[ "$os" = Darwin ] && osver=" $(sw_vers -productVersion 2>/dev/null)"
res PASS 环境/系统 "$os$osver $arch"

# Claude Code 用 PATH 里的 bash 执行 statusLine 命令；渲染脚本自带 ≥5 的换壳守卫
bbash=$(command -v bash 2>/dev/null)
bver=""
[ -n "$bbash" ] && bver=$("$bbash" -c 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"' 2>/dev/null)
if [ -z "$bbash" ]; then
  res FAIL 环境/bash "PATH 里没有 bash" "安装 bash 或修正 PATH"
elif ver_ge "$bver" "$BASH_MIN"; then
  res PASS 环境/bash "${bver}（${bbash}）"
else
  alt=""
  for b in /opt/homebrew/bin/bash /usr/local/bin/bash /usr/bin/bash; do
    [ -x "$b" ] || continue
    v=$("$b" -c 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"' 2>/dev/null)
    if ver_ge "$v" 5; then alt="$b $v"; break; fi
  done
  if [ -n "$alt" ]; then
    res WARN 环境/bash "PATH 里的 bash 是 ${bver}，低于 ${BASH_MIN}；渲染脚本会换用 $alt" \
      "把新版 bash 放到 PATH 前面：brew install bash"
  else
    res FAIL 环境/bash "PATH 里的 bash 是 ${bver}，低于 ${BASH_MIN}，且找不到 ≥5 的替代" \
      "brew install bash（脚本会退化成「模型 · ctx%」最简行）"
  fi
fi

py=$(command -v python3 2>/dev/null)
pver=""
[ -n "$py" ] && pver=$("$py" -V 2>&1 | sed 's/[^0-9.]//g' | head -1)
if [ -z "$py" ]; then
  res FAIL 环境/python3 "未找到 python3" "后台的 token / 色板助手无法运行：brew install python"
elif ver_ge "$pver" "$PY_MIN"; then
  res PASS 环境/python3 "${pver}（${py}）"
else
  res FAIL 环境/python3 "$pver 低于最低 ${PY_MIN}（math.dist / str.removesuffix）" \
    "升级 python3 到 $PY_MIN 或更高（brew install python）"
fi

jq=$(command -v jq 2>/dev/null)
if [ -z "$jq" ]; then
  res FAIL 环境/jq "未找到 jq" "brew install jq；缺失时同步路径的字段提取整体失败，状态栏会空"
else
  res PASS 环境/jq "$(jq --version 2>/dev/null)（${jq}）"
fi

git=$(command -v git 2>/dev/null)
if [ -z "$git" ]; then
  res WARN 环境/git "未找到 git" "分支 / 目录状态项会消失，其余照常：xcode-select --install"
else
  res PASS 环境/git "$(git --version 2>/dev/null | sed 's/^git version //')（${git}）"
fi

curl=$(command -v curl 2>/dev/null)
if [ "$C_PAGE" = off ]; then
  res SKIP 环境/curl "配置 status_page = off，不使用状态页"
elif [ -z "$curl" ]; then
  res WARN 环境/curl "未找到 curl，但 status_page = on" "brew install curl，或设 status_page = off"
else
  res PASS 环境/curl "可用；status_page = on（每分钟请求 status.claude.com）"
fi

case "${COLORTERM:-}" in
  truecolor|24bit) cdepth="真彩（24 位）" ;;
  *) case "${TERM:-}" in *256color*) cdepth="256 色" ;; *) cdepth="无色（状态栏退化为纯文本）" ;; esac ;;
esac
case "$cdepth" in
  无色*) res WARN 环境/终端 "TERM=${TERM:-<未设>} COLORTERM=${COLORTERM:-<未设>} → $cdepth" \
           "在支持颜色的终端里运行，或设 COLORTERM=truecolor" ;;
  *) res PASS 环境/终端 "TERM=${TERM:-<未设>} TERM_PROGRAM=${TERM_PROGRAM:-<未设>} → $cdepth" ;;
esac

