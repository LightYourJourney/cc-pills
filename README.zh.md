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
- bash ≥ 4.2（macOS 自带 3.2，需 `brew install bash`；脚本会先尝试
  `/opt/homebrew/bin/bash` 等位置的 ≥4.2 版本重跑）
- `jq`、`python3` ≥ 3.9（`str.removesuffix`）、`git`；`curl` 仅在 `status_page = on` 时使用

| 环境 | 支持 | 说明 |
|---|---|---|
| macOS + Homebrew bash 5.x | 是 | 主目标 |
| macOS 自带 `/bin/bash` 3.2 | 换壳 | 找 `/opt/homebrew/bin/bash` 等位置的 ≥4.2 重跑，找不到才退化成最简行 |
| Linux（GNU coreutils） | 是 | 无 iTerm profile 时用内置主题；`stat` 先试 GNU `-c` |
| BSD `stat` / `mktemp` | 是 | `mktemp` 模板带 6 个 X，原子 `mv` |
| `TMPDIR` 未设置 / 带尾斜杠 / 含空格与中文 | 是 | 归一后落到同一个私有根 |
| python3 ≥ 3.9 | 是 | 低于 3.9 安装会拒绝，doctor 报 FAIL |

## 缓存

主栏与 subagent 面板共用同一个每用户私有目录 `${TMPDIR:-/tmp}/claude-sl-<uid>`
（目录 0700、文件 0600）：色板、git、token、状态页、刷新锁，以及面板的速率状态。
读取前校验「属于本人、是普通文件、不是符号链接」——符号链接一律不跟随；写入一律
`mktemp`（O_EXCL）+ 原子 `mv`，Python 侧用 `mkstemp` + `os.replace`。

| 变量 | 作用 | 生命周期 |
|---|---|---|
| `CLAUDE_SL_TMP=<目录>` | 整体换根（预览 / 测试用），目录必须已存在且属于本人，渲染不代为创建 | 由调用方负责，**不做**自动清理 |
| `CLAUDE_SUBAGENT_SL_STATE=<文件>` | 只覆盖面板状态文件，优先级高于 `CLAUDE_SL_TMP` | 由调用方负责 |

**自动清理**：默认根里 30 天没动过的 token / 跳过标记 / git / 会话状态与残留临时文件，
由后台刷新每天清一次。用 `find -maxdepth 1 -type f`，不递归、不跟随符号链接，不碰锁目录、
色板、状态页与面板状态。所有缓存都能重建，删掉最多让下一轮慢一次。

**迁移**：旧版写在 `/tmp` 的本会话 token / 跳过标记 / 会话状态在首次刷新时搬入；旧的面板
状态不迁移（只有速率基线，丢失代价是一帧）。**回滚**：删掉整个缓存目录即回到冷启动，
下一轮自动重建；显式 `CLAUDE_SL_TMP` 删掉后需自己重建（doctor 会报 FAIL）。

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
`sk-…` / `*_KEY=…` / `*_TOKEN=…`（大小写不敏感、值可带引号）/ URL 凭据与 query 参数 /
`Bearer …` 再显示。唯一的写是冒烟用的临时目录（跑完即删）；WAL 模式的 SQLite 只读打开时
可能生成 `-wal`/`-shm` 旁文件，与渲染脚本自己的读取方式相同。

```bash
bash tools/doctor.sh                                      # 或 bash install.sh --doctor
bash tools/doctor.sh --transcript ~/.claude/projects/<项目>/<会话>.jsonl   # 连某个会话一起查
```

每行一个结论：`[PASS] 分组/项  说明`，状态为 PASS / WARN / FAIL / SKIP / UNKNOWN
（UNKNOWN = 取决于运行时 stdin，命令行里无法判定；SKIP = 不适用），末行汇总。
退出码：0 无 FAIL；1 有 FAIL 或诊断模块无法加载；2 参数错误。需要处理时下方会给出可直接照做的「修：」。

| 症状 | 先看哪一项 |
|---|---|
| 整行为空 | `环境/jq`、`环境/bash`、`数据/stdin`（会在临时目录里用两份夹具各跑一次，要求输出不同且包含目录与 ctxin） |
| 只有文字、没有胶囊底色 | `缓存/色板`、`缓存/退避`、`环境/终端` |
| 少了 `$/h`、`hit`、`io`、`msgs`、`out` | `缓存/token`（超过 `TOK_STALE` 会丢弃派生项） |
| 少了分支 / 目录 | `环境/git`、`缓存/git` |
| 少了服务状态 | `缓存/状态页`（超过 `STATUS_STALE` 会隐藏） |
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
| `settings.json` 缺失 / 不可读 / 不是普通文件 / 语法错误 | FAIL | `安装/settings.json` |
| `jq` 缺失 | FAIL | `环境/jq` |
| `python3` < 3.9 或缺失 | FAIL | `环境/python3` |
| `bash` < 4.2 且没有 ≥5 的替代 | FAIL | `环境/bash` |
| `bash` < 4.2 但有 ≥5 的替代（靠脚本换壳） | WARN | `环境/bash` |
| 线上副本与仓库不一致 | WARN | `安装/副本-*` |
| 配置里有不认识的键 / 非法值 / 写错的 `step` | WARN | `安装/配置文件` |
| 缓存目录权限不是 0700 | WARN | `缓存/权限` |
| 缓存根是符号链接（渲染不跟随） | FAIL | `缓存/根` |
| 面板状态权限不是 0600 / 不是普通文件 / 不属于本人 | WARN | `缓存/面板状态` |
| 色板缓存缺失 / 首行与配置不符 / `P_CTX` 非法 | WARN | `缓存/色板` |
| 色板生成失败（仍在退避） | WARN | `缓存/退避` |
| 状态页缓存超过 10 分钟 / 格式损坏 / 缺少 indicator / 不是普通文件 | WARN | `缓存/状态页` |
| token 缓存超过 15 分钟（按 `.ts` 或文件 mtime 计） | WARN | `缓存/token` |
| token 缓存字段数不是 18 | WARN | `缓存/token` |
| token 缓存是符号链接（会被忽略） | WARN | `缓存/token` |
| 刷新锁路径被普通文件 / 符号链接 / 非空目录占住（后台刷新永久失败） | FAIL | `缓存/锁` |
| 面板锁被普通文件 / 符号链接 / 非空目录占住（写回永久失败） | FAIL | `缓存/面板锁` |
| 面板锁为空但超过 5 秒（下次渲染自行清除） | WARN | `缓存/面板锁` |
| 缓存目录不可写 | FAIL | `缓存/后台刷新` |
| 显式 `CLAUDE_SL_TMP` 指向的目录不存在（渲染不会创建） | FAIL | `缓存/根` |
| 中转站模式但找不到 CC Switch 库 | WARN | `数据/CCSwitch库` |
| CC Switch 供应商不唯一 / 日限额值非法 | WARN | `数据/CCSwitch库` |
| 日限额未设置（当前供应商唯一） | SKIP | `数据/CCSwitch库` |
| 订阅模式（不打开数据库） | SKIP | `数据/CCSwitch库` |
| 已安装脚本坏掉 / 被换成占位脚本 | FAIL | `数据/stdin` |
| 计费模式 | UNKNOWN | `数据/计费模式` |

退出码：0 = 无 FAIL；1 = 有 FAIL 或诊断模块无法加载；2 = 参数错误。

## 目录

```
src/statusline-command.sh     主渲染器（bash）
src/statusline-tokens.py      transcript 聚合、速率、额度节奏、中转站账本
src/statusline-theme.py       色板生成与对比度验收
src/subagent-statusline.sh    agent 面板里每个 subagent 的状态行
tools/preview.sh              并排预览
tools/doctor.sh               只读诊断（入口）
tools/doctor/*.sh             检查分组：环境 / 安装 / 缓存 / 数据来源
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
