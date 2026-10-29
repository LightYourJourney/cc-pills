#!/usr/bin/env bash
# cc-pills 安装 / 检查 / 卸载
#
#   bash install.sh                 复制脚本到 ${CLAUDE_CONFIG_DIR:-~/.claude}
#   bash install.sh --with-subagent  同时安装 subagent 状态行
#   bash install.sh --with-conf      示例配置不存在时写入 ~/.config/claude-statusline.conf
#   bash install.sh --check          核对线上副本与 settings 两个键（不改任何文件）
#   bash install.sh --uninstall      删除已安装的副本（线上有改动时拒绝，需 --force），
#                                    并提示仍指向已删除脚本的 settings 键
#
# 复制安装：线上文件是独立副本，改仓库后必须重新运行本脚本才生效。
# 覆盖前把旧副本存为 <文件名>.bak.<时间戳>（内容相同则不备份）。
set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEST=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/claude-statusline.conf
FILES=(statusline-command.sh statusline-tokens.py statusline-theme.py)
WITH_SUB=0; WITH_CONF=0; MODE=install; FORCE=0
for a in "$@"; do
  case $a in
    --with-subagent) WITH_SUB=1 ;;
    --with-conf) WITH_CONF=1 ;;
    --check) MODE=check ;;
    --uninstall) MODE=uninstall ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$a（--help 看用法）" >&2; exit 2 ;;
  esac
done
# install 只装 --with-subagent 指定的；check / uninstall 覆盖实际装过的副本
if [ "$WITH_SUB" = 1 ] || { [ "$MODE" != install ] && [ -e "$DEST/subagent-statusline.sh" ]; }; then
  FILES+=(subagent-statusline.sh)
fi

check_deps() {
  local miss=0
  for c in bash jq python3 git; do
    command -v "$c" >/dev/null 2>&1 || { echo "缺少依赖：$c" >&2; miss=1; }
  done
  # curl 只在 status_page = on 时用到（见 README），缺了只提示，不拦安装
  command -v curl >/dev/null 2>&1 || \
    echo "提示：未找到 curl；status_page = on（默认）时状态页不可用，可设 status_page = off" >&2
  local major=${BASH_VERSINFO[0]} minor=${BASH_VERSINFO[1]}
  if [ "$major" -lt 4 ] || { [ "$major" -eq 4 ] && [ "$minor" -lt 2 ]; }; then
    echo "需要 bash ≥ 4.2（当前 $major.$minor）；macOS 自带 3.2 请装 Homebrew bash" >&2
    miss=1
  fi
  return "$miss"
}

settings_cmd() {  # settings_cmd <settings.json 路径> <键> → 打印该键的 command
  [ -r "$1" ] || return 1
  jq -r --arg k "$2" '.[$k].command // empty' "$1" 2>/dev/null || return 1
}
has_file() {  # 当前 FILES 里是否有某个脚本
  local f; for f in "${FILES[@]}"; do [ "$f" = "$1" ] && return 0; done; return 1
}
points_to() {  # points_to <command> <脚本名> <DEST> → 0 明确指向 / 2 只含名字 / 1 未指向
  case "$1" in
    *"$3/$2"*)                  return 0 ;;          # 指向已安装副本
    *'${CLAUDE_CONFIG_DIR'*"$2"*) return 0 ;;        # 建议写法：运行时展开到同一目录
    *"$2"*)                     return 2 ;;          # 只是文件名相同，路径不明
  esac
  return 1
}

case $MODE in
install)
  check_deps || exit 1
  mkdir -p "$DEST" || exit 1
  for f in "${FILES[@]}"; do
    src=$REPO/src/$f; dst=$DEST/$f
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
      bak=$dst.bak.$(date +%Y%m%d%H%M%S)
      cp -p "$dst" "$bak" && echo "  备份旧副本 → $bak"
    fi
    install -m 0755 "$src" "$dst" && echo "  已安装 $f → $dst"
  done
  if [ "$WITH_CONF" = 1 ]; then
    if [ -e "$CONF" ]; then
      echo "  配置已存在，未覆盖：$CONF"
    else
      mkdir -p "$(dirname "$CONF")" && cp "$REPO/config/claude-statusline.conf.example" "$CONF" \
        && echo "  已写入示例配置 → $CONF"
    fi
  fi
  cmd=$(settings_cmd "$DEST/settings.json" statusLine)
  points_to "$cmd" statusline-command.sh "$DEST"; st=$?
  echo
  case $st in
    0) echo "settings.json 的 statusLine 已指向它：$cmd" ;;
    2) echo "⚠ statusLine 含脚本名，但路径不是已安装副本：$cmd"
       echo '  "statusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\""}' ;;
    *) echo "⚠ settings.json 尚未配置 statusLine。请在 Claude Code 里运行 /statusline 选择，或手工写入："
       echo '  "statusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\""}' ;;
  esac
  if has_file subagent-statusline.sh; then
    cmd=$(settings_cmd "$DEST/settings.json" subagentStatusLine)
    points_to "$cmd" subagent-statusline.sh "$DEST"; st=$?
    case $st in
      0) echo "settings.json 的 subagentStatusLine 已指向它：$cmd" ;;
      2) echo "⚠ subagentStatusLine 含脚本名，但路径不是已安装副本：$cmd"
         echo '  "subagentStatusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/subagent-statusline.sh\""}' ;;
      *) echo "⚠ settings.json 尚未配置 subagentStatusLine（agent 面板会用默认渲染）。手工写入："
         echo '  "subagentStatusLine": {"type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/subagent-statusline.sh\""}' ;;
    esac
  fi
  ;;
check)
  rc=0
  for f in "${FILES[@]}"; do
    dst=$DEST/$f
    if [ ! -e "$dst" ]; then echo "  未安装：$f"; rc=1
    elif cmp -s "$REPO/src/$f" "$dst"; then echo "  一致：$f"
    else echo "  不一致：$f（仓库改过但没重新安装）"; rc=1; fi
  done
  cmd=$(settings_cmd "$DEST/settings.json" statusLine)
  points_to "$cmd" statusline-command.sh "$DEST"; st=$?
  case $st in
    0) echo "  settings.json 的 statusLine 指向：$cmd" ;;
    2) echo "  settings.json 的 statusLine 路径不是已安装副本：$cmd"; rc=1 ;;
    *) echo "  settings.json 的 statusLine 未指向 statusline-command.sh"; rc=1 ;;
  esac
  cmd=$(settings_cmd "$DEST/settings.json" subagentStatusLine)
  if has_file subagent-statusline.sh; then
    points_to "$cmd" subagent-statusline.sh "$DEST"; st=$?
    case $st in
      0) echo "  settings.json 的 subagentStatusLine 指向：$cmd" ;;
      2) echo "  settings.json 的 subagentStatusLine 路径不是已安装副本：$cmd"; rc=1 ;;
      *) echo "  settings.json 未配置 subagentStatusLine（面板脚本已安装）"; rc=1 ;;
    esac
  elif [ -n "$cmd" ]; then
    points_to "$cmd" subagent-statusline.sh "$DEST"; st=$?
    if [ "$st" != 1 ]; then
      echo "  settings.json 指向面板脚本，但文件未安装"; rc=1
    else
      echo "  settings.json 的 subagentStatusLine 未指向 subagent-statusline.sh"; rc=1
    fi
  fi
  exit "$rc"
  ;;
uninstall)
  rc=0; removed=()
  for f in "${FILES[@]}"; do
    dst=$DEST/$f
    [ -e "$dst" ] || continue
    if [ "$FORCE" != 1 ] && ! cmp -s "$REPO/src/$f" "$dst"; then
      echo "  线上 $f 与本仓库不同（可能被手工改过），未删除；确认要删加 --force" >&2; rc=1; continue
    fi
    rm -f "$dst" && { echo "  已删除 $dst"; removed+=("$dst"); }
  done
  # 不自动改用户 settings：只指出仍指向已删除脚本的键，避免每次渲染都执行失败。
  if [ ${#removed[@]} -gt 0 ]; then
    for key in statusLine subagentStatusLine; do
      c=$(settings_cmd "$DEST/settings.json" "$key")
      [ -n "$c" ] || continue
      for dst in "${removed[@]}"; do
        case "$c" in
          *"$dst"*) echo "⚠ settings.json 的 $key 仍指向已删除的 $dst；请改回或删除该键" >&2 ;;
        esac
      done
    done
  fi
  echo "  配置保留：$CONF（要删请手工处理）"
  exit "$rc"
  ;;
esac
