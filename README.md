# cc-pills

A single-line, pill-styled status line for [Claude Code](https://claude.com/claude-code) —
responsive, cached, and honest about what it cannot know.

[![tests](https://github.com/LightYourJourney/cc-pills/actions/workflows/tests.yml/badge.svg)](https://github.com/LightYourJourney/cc-pills/actions/workflows/tests.yml)

> Not affiliated with Anthropic. "Claude" is a trademark of Anthropic, PBC.

```
 ccchat  main [+?]  Opus 5.5 high  ctx 42% 419k ↻1  5h 60% ↺2h26m out 1h42m
 7d 82% ↺2d3h out 1d1h  $17.54 $4.9/h  cache 20m hit 97.4% ↑30.1M ↓290k api 52m
```

(rendered as rounded pills with per-group tints; the plain text above is what the widths are
measured against)

- **One line, degrades by priority.** Every item has shorter forms and a place in a
  drop/shrink sequence; ten items are never dropped. After a step frees a lot of space,
  sacrificed items are restored.
- **Cached render + background refresh.** The synchronous path does one `jq` call and reads
  caches; git, transcripts, palettes and the status page are refreshed in a detached process.
  Typical render: ~20 ms.
- **Seven pill styles, contrast-validated.** `tint` (default), `cool`, `jewel`, `glass`,
  `mono`, `pastel`, `tokyo-glass`. Palettes are generated from your terminal's iTerm profile
  and checked against WCAG contrast on every text/pill pair.
- **Quota pacing, not just percentages.** Projects "will run out in…" from the window average
  and a recent-rate EWMA; colours the current state, only warns about the forecast.
- **Relay (CC Switch) accounting.** Subscription shows the estimated equivalent value; a
  relay shows ledger + not-yet-settled tail, with `+` when some messages are unpriced.
- **Never invents numbers.** A missing field hides its item. `warm=false` alone is not "cold".
  Used ≥ 100 % reads "用尽" (exhausted) rather than a negative remainder.

Chinese README: [README.zh.md](README.zh.md). Design rationale and past rulings:
[docs/DECISIONS.md](docs/DECISIONS.md).

## Requirements

- macOS (iTerm2 profile detection; other terminals fall back to the built-in themes)
- bash ≥ 4.2 — macOS ships 3.2, so `brew install bash`
- `jq`, `python3`, `git`; `curl` is only used when `status_page = on`

## Install

```bash
git clone https://github.com/LightYourJourney/cc-pills.git
cd cc-pills
bash install.sh --with-subagent --with-conf
```

`install.sh` copies the scripts into `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` (the panel
script only with `--with-subagent`) and prints whether `statusLine` and
`subagentStatusLine` already point at them. If they do not, add:

```json
"statusLine": { "type": "command",
                "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh\"" },
"subagentStatusLine": { "type": "command",
                        "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/subagent-statusline.sh\"" }
```

Copy-install means the repo is the source of truth and the installed files are snapshots:
re-run `bash install.sh` after pulling. `bash install.sh --check` reports drift;
`--uninstall` removes the copies (and refuses if they were edited locally).

## Configure

`~/.config/claude-statusline.conf` — one `key = value` per line, never sourced. Start from
[config/claude-statusline.conf.example](config/claude-statusline.conf.example).

| Key | Meaning |
|---|---|
| `theme` | `auto` (read the iTerm profile) or `tokyonight` / `dracula` / `catppuccin` / `onedark` / `nord` |
| `pill_style`, `pill_chroma` | pill style and tint chroma |
| `glyphs` | `nerd` or `ascii` |
| `warn_at`, `bad_at`, `ctx_warn`, `ctx_bad`, `crit_at` | thresholds (percent used) |
| `safe_margin` | columns reserved for Claude Code's own padding (default 4) |
| `status_page` | `on` polls status.claude.com once a minute; `off` makes zero network requests |
| `step = drop/shrink <item>` | replaces the whole degradation sequence |

Preview styles side by side inside iTerm:

```bash
bash tools/preview.sh                 # default four styles
bash tools/preview.sh tint tokyo-glass
```

Preview runs against `src/` in an isolated cache directory with fake data; it never touches a
live session's caches.

## Troubleshooting

When an item disappears, the line loses its colours, or nothing renders at all, run the
diagnostic first. It never modifies your config, cache or database content, makes no network
requests, never dumps the environment, and scrubs `sk-…` / `*_KEY=…` / `*_TOKEN=…` / URL
credentials / `Bearer …` out of the `settings.json` command before printing it. Its only write
is a throwaway temp dir; a WAL-mode SQLite database may get `-wal`/`-shm` sidecars on read-only
open, exactly as the renderer's own read does.

```bash
bash tools/doctor.sh                                      # alias: bash install.sh --doctor
bash tools/doctor.sh --transcript ~/.claude/projects/<project>/<session>.jsonl
```

One line per check — `[PASS] group/item  detail` — with PASS / WARN / FAIL / SKIP / UNKNOWN
(UNKNOWN = undecidable without runtime stdin, SKIP = not applicable), a summary line, and a
`修:` (fix) line whenever something needs doing. Exit code 0 = no FAIL, 1 = some FAIL, 2 = bad
arguments.

| Symptom | Look at |
|---|---|
| Empty line | `环境/jq`, `环境/bash`, `数据/stdin` (runs the installed script once on a fixture, in a temp dir) |
| Text but no pill colours | `缓存/色板`, `缓存/退避`, `环境/终端` |
| No `$/h`, `hit`, `io`, `msgs`, `out` | `缓存/token` (older than `TOK_STALE` drops derived items) |
| No branch / directory | `环境/git`, `缓存/git` |
| Relay shows `$x*` | `数据/CCSwitch库` (opened read-only; never modified) |
| Config change has no effect | `安装/配置文件` (unknown keys and bad values are ignored silently) |
| Panel has no custom lines | `安装/subagentStatusLine` |

The render path never calls the diagnostic, so refreshes do not get slower.

### Scenario → result

| Scenario | Expected | Check |
|---|---|---|
| Complete install, valid config | no FAIL | all |
| Scripts not installed | FAIL | `安装/副本-statusline-command.sh` |
| `statusLine` missing / dangling / pointing at `.bak` | FAIL | `安装/statusLine` |
| `settings.json` missing / unreadable / not a regular file / invalid JSON | FAIL | `安装/settings.json` |
| `jq` missing | FAIL | `环境/jq` |
| `python3` < 3.9 or missing | FAIL | `环境/python3` |
| `bash` < 4.2 with no ≥5 fallback | FAIL | `环境/bash` |
| `bash` < 4.2 but a ≥5 build exists (re-exec guard) | WARN | `环境/bash` |
| Installed copy drifts from the repo | WARN | `安装/副本-*` |
| Unknown key / bad value / misspelled `step` | WARN | `安装/配置文件` |
| Cache directory not 0700 | WARN | `缓存/权限` |
| Palette cache missing / header mismatch / invalid `P_CTX` | WARN | `缓存/色板` |
| Palette generation failing (backing off) | WARN | `缓存/退避` |
| Token cache older than 15 min (by `.ts` or file mtime) | WARN | `缓存/token` |
| Token cache not 18 fields | WARN | `缓存/token` |
| Lock path occupied by a plain file (refresh never runs) | FAIL | `缓存/锁` |
| Cache directory not writable | FAIL | `缓存/后台刷新` |
| Relay mode but no CC Switch database | WARN | `数据/CCSwitch库` |
| CC Switch provider not unique / invalid daily limit | WARN | `数据/CCSwitch库` |
| Daily limit not set (single current provider) | SKIP | `数据/CCSwitch库` |
| Subscription mode (database not opened) | SKIP | `数据/CCSwitch库` |
| Installed script broken or replaced by a stub | FAIL | `数据/stdin` |
| Billing mode | UNKNOWN | `数据/计费模式` |

Exit code: 0 = no FAIL, 1 = some FAIL, 2 = bad arguments.

## Layout

```
src/statusline-command.sh     main renderer (bash)
src/statusline-tokens.py      transcript aggregation, rates, quota pacing, relay ledger
src/statusline-theme.py       palette generation and contrast validation
src/subagent-statusline.sh    the agent panel's per-subagent lines
tools/preview.sh              side-by-side style preview
tools/doctor.sh               read-only diagnostics (install / cache / data sources)
tests/                        unit + golden + property + contrast-matrix tests
install.sh  run_tests.sh
```

## Tests

```bash
bash run_tests.sh              # ~1.5–5 min depending on load, fully isolated (no live caches, no network)
bash run_tests.sh tokens       # one module: render / tokens / theme / cache / subagent / freshness / install / doctor
UPDATE_GOLDEN=1 bash run_tests.sh   # after an intentional rendering change
```

## Accuracy caveats

Token counts, costs, `$/h`, hit rates and quota pacing are **client-side estimates**, not
billing records. When a field is missing the item is hidden rather than guessed. On a relay,
the ledger comes from CC Switch's database (≈30-day retention) and is reconciled with its
usage page; unpriced models are counted as a `+` lower bound.

## License

MIT — see [LICENSE](LICENSE).
