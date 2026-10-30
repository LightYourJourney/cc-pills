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

`install.sh` 把脚本复制到 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`（面板脚本仅在
`--with-subagent` 时），并告诉你 `settings.json` 的 `statusLine` 与
`subagentStatusLine` 是否已指向它们。没有的话加上：

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

## 故障排查

状态栏少了一项、整行无色、或者干脆不显示时，先跑诊断。它不改写你的配置 / 缓存 / 数据库
内容，不联网，不打印环境变量全集；`settings.json` 里的命令行会先抹掉
`sk-…` / `*_KEY=…` / `*_TOKEN=…` / URL 里的 `user:pass@` / `Bearer …` 再显示。唯一的写是冒烟
用的临时目录（跑完即删）；WAL 模式的 SQLite 只读打开时可能生成 `-wal`/`-shm` 旁文件，与
渲染脚本自己的读取方式相同。

```bash
bash tools/doctor.sh                                      # 或 bash install.sh --doctor
bash tools/doctor.sh --transcript ~/.claude/projects/<项目>/<会话>.jsonl   # 连某个会话一起查
```

每行一个结论：`[PASS] 分组/项  说明`，状态为 PASS / WARN / FAIL / SKIP / UNKNOWN
（UNKNOWN = 取决于运行时 stdin，命令行里无法判定；SKIP = 不适用），末行汇总。
退出码：0 无 FAIL，1 有 FAIL，2 参数错误。需要处理时下方会给出可直接照做的「修：」。

| 症状 | 先看哪一项 |
|---|---|
| 整行为空 | `环境/jq`、`环境/bash`、`数据/stdin`（会在临时目录里用夹具真跑一次） |
| 只有文字、没有胶囊底色 | `缓存/色板`、`缓存/退避`、`环境/终端` |
| 少了 `$/h`、`hit`、`io`、`msgs`、`out` | `缓存/token`（超过 `TOK_STALE` 会丢弃派生项） |
| 少了分支 / 目录 | `环境/git`、`缓存/git` |
| 中转站显示 `$x*` | `数据/CCSwitch库`（只读打开，不改库） |
| 改了配置没生效 | `安装/配置文件`（认不出的键与非法值会被静默忽略） |
| 面板没有自定义行 | `安装/subagentStatusLine` |

诊断不会在正常渲染路径上被调用：状态栏的每次刷新都不会因此变慢。

### 故障场景与诊断结果

| 场景 | 期望状态 | 检查项 |
|---|---|---|
| 安装完整、配置合法 | 无 FAIL | 全部 |
| 脚本没装 | FAIL | `安装/副本-statusline-command.sh` |
| `statusLine` 没配 / 指向不存在 / 指向 `.bak` | FAIL | `安装/statusLine` |
| `settings.json` 语法错误 | FAIL | `安装/settings.json` |
| `jq` 缺失 | FAIL | `环境/jq` |
| `python3` < 3.9 或缺失 | FAIL | `环境/python3` |
| `bash` < 4.2 且没有 ≥5 的替代 | FAIL | `环境/bash` |
| `bash` < 4.2 但有 ≥5 的替代（靠脚本换壳） | WARN | `环境/bash` |
| 线上副本与仓库不一致 | WARN | `安装/副本-*` |
| 配置里有不认识的键 / 非法值 / 写错的 `step` | WARN | `安装/配置文件` |
| 缓存目录权限不是 0700 | WARN | `缓存/权限` |
| 色板缓存缺失 / 首行与配置不符 / `P_CTX` 非法 | WARN | `缓存/色板` |
| 色板生成失败（仍在退避） | WARN | `缓存/退避` |
| token 缓存超过 15 分钟，或没有 `.ts` 时间戳 | WARN | `缓存/token` |
| token 缓存字段数不是 18 | WARN | `缓存/token` |
| 刷新锁路径被普通文件占住（后台刷新永久失败） | FAIL | `缓存/锁` |
| 缓存目录不可写 | FAIL | `缓存/后台刷新` |
| 中转站模式但找不到 CC Switch 库 | WARN | `数据/CCSwitch库` |
| CC Switch 供应商不唯一 / 日限额未设置 | WARN | `数据/CCSwitch库` |
| 订阅模式（不打开数据库） | SKIP | `数据/CCSwitch库` |
| 已安装脚本坏掉 / 被换成占位脚本 | FAIL | `数据/stdin` |
| 计费模式 | UNKNOWN | `数据/计费模式` |

退出码：0 = 无 FAIL，1 = 有 FAIL，2 = 参数错误。

## 目录

```
src/statusline-command.sh     主渲染器（bash）
src/statusline-tokens.py      transcript 聚合、速率、额度节奏、中转站账本
src/statusline-theme.py       色板生成与对比度验收
src/subagent-statusline.sh    agent 面板里每个 subagent 的状态行
tools/preview.sh              并排预览
tools/doctor.sh               只读诊断（安装 / 缓存 / 数据来源）
tests/                        单元 + 金样 + 性质 + 对比度矩阵测试
install.sh  run_tests.sh
```

## 测试

```bash
bash run_tests.sh              # 约 1.5–5 分钟（随负载），全隔离（不读线上缓存、不联网）
bash run_tests.sh tokens       # 单模块：render / tokens / theme / cache / subagent / freshness / install / doctor
UPDATE_GOLDEN=1 bash run_tests.sh   # 有意改变渲染结果后更新金样
```

## 精度声明

token 计数、费用、`$/h`、命中率、额度节奏都是**客户端估算**，不是账单记录。字段缺失时
隐藏该项而不是猜。中转站的账本来自 CC Switch 数据库（约 30 天保留），与它用量页同口径；
价格表里没有的模型按 `+` 计为下限。

## 许可

MIT，见 [LICENSE](LICENSE)。
