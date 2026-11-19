#!/usr/bin/env bash
# 跑全部测试。每个渲染测试都在独立沙箱里跑（CLAUDE_SL_TMP + 假 iTerm profile），
# 不读也不写线上缓存、不联网。全量默认并行：用例按轮转切片、每片一个进程（tests/_parallel.py），
# 对时序敏感的 tokens / subagent 在之后串行跑。耗时随核数与负载变化。
#
#   bash run_tests.sh            全部（并行）
#   bash run_tests.sh tokens     只跑某几个模块（串行；render / tokens / theme / cache / subagent / freshness / install / doctor）
#   CCPILLS_SERIAL=1 bash run_tests.sh   全部串行（排查并行相关问题时用）
#   CCPILLS_JOBS=4 bash run_tests.sh     指定并行片数（默认 min(核数, 8)）
#   UPDATE_GOLDEN=1 bash run_tests.sh render   重新生成渲染金样（有意改动渲染结果时）
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/tests" || exit 1
if [ $# -gt 0 ]; then
  mods=(); for m in "$@"; do mods+=("test_$m"); done
  exec python3 -m unittest "${mods[@]}"
fi
if [ "${CCPILLS_SERIAL:-}" = 1 ]; then
  exec python3 -m unittest test_render test_tokens test_theme test_cache test_subagent test_freshness test_install test_doctor
fi
exec python3 _parallel.py
