#!/usr/bin/env bash
# 跑全部测试。每个渲染测试都在独立沙箱里跑（CLAUDE_SL_TMP + 假 iTerm profile），
# 不读也不写线上缓存、不联网。约 1.5–5 分钟（随负载；隔离沙箱要起数百次真实渲染）。
#
#   bash run_tests.sh            全部
#   bash run_tests.sh tokens     只跑某个模块（render / tokens / theme / cache / subagent / freshness / install / doctor）
#   UPDATE_GOLDEN=1 bash run_tests.sh   重新生成渲染金样（有意改动渲染结果时）
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/tests" || exit 1
mods=(test_render test_tokens test_theme test_cache test_subagent test_freshness test_install test_doctor)
if [ $# -gt 0 ]; then mods=(); for m in "$@"; do mods+=("test_$m"); done; fi
python3 -m unittest "${mods[@]}"
