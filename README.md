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

`install.sh` copies the four scripts into `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` and prints
whether `settings.json` already points at them. If it does not, add:

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

## Layout

```
src/statusline-command.sh     main renderer (bash)
src/statusline-tokens.py      transcript aggregation, rates, quota pacing, relay ledger
src/statusline-theme.py       palette generation and contrast validation
src/subagent-statusline.sh    the agent panel's per-subagent lines
tools/preview.sh              side-by-side style preview
tests/                        unit + golden + property + contrast-matrix tests
install.sh  run_tests.sh
```

## Tests

```bash
bash run_tests.sh              # ~1.5–5 min depending on load, fully isolated (no live caches, no network)
bash run_tests.sh tokens       # one module: render / tokens / theme
UPDATE_GOLDEN=1 bash run_tests.sh   # after an intentional rendering change
```

## Accuracy caveats

Token counts, costs, `$/h`, hit rates and quota pacing are **client-side estimates**, not
billing records. When a field is missing the item is hidden rather than guessed. On a relay,
the ledger comes from CC Switch's database (≈30-day retention) and is reconciled with its
usage page; unpriced models are counted as a `+` lower bound.

## License

MIT — see [LICENSE](LICENSE).
