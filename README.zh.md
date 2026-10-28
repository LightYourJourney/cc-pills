# cc-pills

Claude Code 的单行胶囊状态栏——响应式、缓存渲染，并且对「算不出来的东西」保持诚实。

[![tests](https://github.com/LightYourJourney/cc-pills/actions/workflows/tests.yml/badge.svg)](https://github.com/LightYourJourney/cc-pills/actions/workflows/tests.yml)

> 非官方项目，与 Anthropic 无关。「Claude」是 Anthropic, PBC 的商标。

```
 ccchat  main [+?]  Opus 5.5 high  ctx 42% 419k ↻1  5h 60% ↺2h26m out 1h42m
 7d 82% ↺2d3h out 1d1h  $17.54 $4.9/h  cache 20m hit 97.4% ↑30.1M ↓290k api 52m
```

（实际渲染为圆角胶囊 + 按组淡染底色，上面是宽度计算所依据的纯文本）

- **单行，按优先级降级。** 每项有从长到短的写法，以及它在「丢弃 / 缩短」序列里的位置；
  十项永不丢弃。某一步一次省出很多列后，被牺牲的项会按逆序恢复。
- **缓存渲染 + 后台刷新。** 同步路径只调一次 `jq` 再读缓存；git、transcript、色板、状态页
  都在脱钩的后台进程里算。典型耗时约 20 ms。
- **七种胶囊风格，对比度验收。** `tint`（默认）/ `cool` / `jewel` / `glass` / `mono` /
  `pastel` / `tokyo-glass`。色板从当前 iTerm profile 生成，每一组「文字 × 胶囊」都过
  WCAG 对比度检查。
- **额度不只看百分比。** 用窗口均速与近期速率的 EWMA 外推「还要多久用完」；颜色只反映
  现状，预测只用来提醒。
- **中转站（CC Switch）账本。** 订阅显示标价估算的等价价值；中转站显示账本 + 尚未入账的
  尾部，有未计价消息时以 `+` 表示下限。
- **绝不编数字。** 字段缺失就隐藏该项；`warm=false` 不等于「冷」；已用 ≥ 100% 显示
  「用尽」而不是负数剩余。

英文说明见 [README.md](README.md)；设计原因与历次裁定见 [docs/DECISIONS.md](docs/DECISIONS.md)。

## 依赖

- macOS（用于读取 iTerm2 profile；其他终端回落内置主题）
- bash ≥ 4.2（macOS 自带 3.2，需 `brew install bash`）
- `jq`、`python3`、`git`；`curl` 仅在 `status_page = on` 时使用

## 安装

```bash
git clone https://github.com/LightYourJourney/cc-pills.git
cd cc-pills
bash install.sh --with-subagent --with-conf
```

`install.sh` 把四个脚本复制到 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`，并告诉你
`settings.json` 是否已指向它们。没有的话加上：

```json
"statusLine": { "type": "command",
                "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\"" },
"subagentStatusLine": { "type": "command",
                        "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/subagent-statusline.sh\"" }
```

复制安装意味着仓库是源、线上文件是快照：`git pull` 之后要重新跑一次 `bash install.sh`。
`bash install.sh --check` 检查两边是否一致；`--uninstall` 删除线上副本（本地改过则拒绝）。

## 配置

`~/.config/claude-statusline.conf`，每行 `键 = 值`，从不 source。从
[config/claude-statusline.conf.example](config/claude-statusline.conf.example) 抄一份开始。

| 键 | 含义 |
|---|---|
| `theme` | `auto`（读 iTerm profile）或 `tokyonight` / `dracula` / `catppuccin` / `onedark` / `nord` |
| `pill_style`、`pill_chroma` | 胶囊风格与淡染彩度 |
| `glyphs` | `nerd` 或 `ascii` |
| `warn_at`、`bad_at`、`ctx_warn`、`ctx_bad`、`crit_at` | 已用百分比阈值 |
| `safe_margin` | 预留给 Claude Code 自身边距的列数（默认 4） |
| `status_page` | `on` 每分钟请求一次 status.claude.com；`off` 则零网络请求 |
| `step = drop/shrink <项>` | 整体替换降级序列 |

在 iTerm 里并排预览：

```bash
bash tools/preview.sh                 # 默认四种
bash tools/preview.sh tint tokyo-glass
```

预览在独立的缓存目录里对 `src/` 跑假数据，不碰线上会话的任何缓存。

## 目录

```
src/statusline-command.sh     主渲染器（bash）
src/statusline-tokens.py      transcript 聚合、速率、额度节奏、中转站账本
src/statusline-theme.py       色板生成与对比度验收
src/subagent-statusline.sh    agent 面板里每个 subagent 的状态行
tools/preview.sh              并排预览
tests/                        单元 + 金样 + 性质 + 对比度矩阵测试
install.sh  run_tests.sh
```

## 测试

```bash
bash run_tests.sh              # 约 40 秒，全隔离（不读线上缓存、不联网）
bash run_tests.sh tokens       # 单模块：render / tokens / theme
UPDATE_GOLDEN=1 bash run_tests.sh   # 有意改变渲染结果后更新金样
```

## 精度声明

token 计数、费用、`$/h`、命中率、额度节奏都是**客户端估算**，不是账单记录。字段缺失时
隐藏该项而不是猜。中转站的账本来自 CC Switch 数据库（约 30 天保留），与它用量页同口径；
价格表里没有的模型按 `+` 计为下限。

## 许可

MIT，见 [LICENSE](LICENSE)。
