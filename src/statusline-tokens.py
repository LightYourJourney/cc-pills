#!/usr/bin/env python3
"""cc-pills token / 节奏 / 中转站账本助手（由 statusline-command.sh 的 --refresh 后台模式调用）。

用法：statusline-tokens.py <transcript_path> <session_id> [key=value ...]
  cost=<美元>            stdin cost.total_cost_usd（订阅模式的等价价值；中转站无账本时的回落）
  q5=<已用%>,<resets_at>  5 小时额度（rate_limits.five_hour）
  q7=<已用%>,<resets_at>  7 天额度（rate_limits.seven_day）
  sp=<已用%>,<resets_at>,<period>  网关花费限额（rate_limits.spend_limit；period = daily|weekly|monthly）
  relay=<cc-switch.db>   中转站模式：读 CC Switch 账本（见 4）
  th=<WARN_AT>,<BAD_AT>  告警阈值（与 bash 侧同源）
环境变量 CLAUDE_SL_TMP：状态 / 会话列表文件的目录（默认 ${TMPDIR:-/tmp}/claude-sl-<uid>；
预览与测试用它隔离）

设计原因与沿革见 docs/DECISIONS.md。产出 stdin JSON 直接给不了的指标：

  1. session 累计 总输入/输出 tokens：主 transcript + <sid>/subagents/*.jsonl +
     <sid>/subagents/workflows/*/*.jsonl（与 CC Switch 的会话归属一致，subagent 计入）；
     总输入 = input + cache 读 + cache 写。
  2. 花费速率 $/h：对费用逐次采样做时间加权 EWMA（半衰期 1200s，偏差校正同 Adam——
     不校正时恒定 $12/h 跑满 20 分钟只显示 $8.8/h）。半衰期从 600s 提到 1200s：实测单笔
     大请求会让 600s 读数一分钟内跳 $14，1200s 把最大跳变压到 $7、超 $8 的跳变归零
     （滑动窗口反而更跳：单笔花费进出窗口即阶跃）。
  3. 主对话 compact 次数（主 transcript 的 system/compact_boundary 且非 sidechain）。
  4. 中转站会话花费 =【CC Switch 账本】+【尚未入账的尾部】。
     账本 = proxy_request_logs 中本会话的全部行。代理行（data_source='proxy'，上游模型价
     × 供应商倍率，实时）与会话日志行（'session_log'，CC Switch 每 10 分钟回扫 transcript
     补记直连请求，倍率恒为 1）两源互补——CC Switch 写会话日志前按「四种 token 全等、2xx、
     ±10 分钟」与代理行去重（usage_stats.rs has_matching_proxy_usage_log）。
     尾部 = transcript 中 request_id「session:<message.id>」不在账本、且匹配不到代理行的
     消息，用 CC Switch 的价格表与计价规则自算（calculator.rs：1h 缓存写入按 5m 单价 ×1.6；
     会话日志路径倍率 1），故显示值实时，且在 CC Switch 下一轮同步后与其用量页一致。
     CC Switch 只保留约 30 天：早于库内最早记录的消息不计入尾部、直接移出未入账集合，
     与用量页同口径（否则老会话一旦重建状态，会把已被清掉的消息当未入账重新自算）。
     今日分三个口径（2026-10-29 与 CC Switch 4.0.21 check_provider_limits 对齐）：
       global = 本地零点以来全部 Claude 明细（含无头会话）+ 本会话尾部；Codex 不计。
                没有日限额时，today 项显示它。
       cur    = 当前供应商可归属 = provider_id=当前供应商 的 proxy 明细 + 当日 rollup。
                有日限额时，日限额项与「out」用它做分子（上游同口径）。
       unattr = global - cur = 其它供应商 + provider_id='_session' 的会话日志 + 尾部。
     '_session' 是 CC Switch 给会话日志的哨兵（session_usage.rs），无法可靠归属供应商；
     上游的日限额检查同样不把它算进任何供应商。没有 / 多个 is_current 或限额值无效时
     隐藏日限额项与预测——未知不能当 0。供应商切换时 day 追踪器按 provider_id 重置。
     $/h 的追踪器按来源（账本 / stdin）分 epoch；中转站模式下没追平或读库失败时不再改喂
     stdin 费用——两种口径共用一个追踪器，切换一次即虚报几百美元每小时。
  5. 额度节奏（5h / 7d / 中转站日限额 / 网关花费限额）：速率取「窗口均速 = 已用 ÷ 窗口已过
     时长」与「近期速率 = 已用量的 EWMA 斜率」中的较大者（偏保守），外推到重置时刻得 proj
     与预计用完倒计时 eta。分级「现状定色、预测只提醒」：
       bad  = eta < URGENT[窗口]（很快就要用完）：百分比与 out 都红
       high = 已用 ≥ th[1]：百分比红，out（若有）仍黄——out 按紧迫度单独定色
       warn = proj ≥ 100%（按节奏会在重置前用完，仅提醒）：只有 out 黄
     两种速率都不可得时只按已用 ≥ th[1] 判 high。已用 ≥ 100%（用尽）一律判 bad、
     用完倒计时写「用尽」。
     中转站日限额的窗口均速 = 当前供应商可归属今日 ÷ 自零点以来时长；网关日限额仍用
     网关上报的百分比。日限额的速率不并本会话 $/h（可能来自其它供应商 / '_session'）。
     例外：7d 只用窗口均速，且窗口过满 1 天才外推——周窗口有昼夜节律，开窗头几小时按
     24h 连续外推一整周会系统性高估（实测：开窗 6.9h 用 30% 即被判「16 小时后用完」）。

  6. 会话级缓存命中率 = cache 读 ÷（input + cache 读 + cache 写），由 1 的累计量算出。
     不用 stdin 的 prompt_cache.hit_ratio：那是每个 Claude Code 进程各自的统计，
     /restart 或 resume 后从零开始，读数不代表会话。

输出单行、tab 分隔的十八段纯文本，不可得的段写 "-"（bash 侧 IFS=tab read 需占位）：
  总输入 输出 $/h compact次数 5h等级 5h用完倒计时 7d等级 7d用完倒计时 日等级 日用完倒计时
  中转站会话花费 未计价消息数 中转站今日 日限额 命中率%（一位小数）
  花费限额等级 花费限额用完倒计时 消息数
（等级 ∈ ok / warn / high / bad，见 5）。transcript 未追平或任何异常时输出空——bash 侧
保留旧缓存（其约束 10），表现为数字停在上一次的值；超过 TOK_STALE 秒后由 bash 侧丢弃
派生项、只保留 stdin 的额度百分比。

⚠️ 估算值声明：这是客户端估算，不是账单权威值。

工程约束（修改时必须保留）：
  1. 本脚本跑在后台刷新进程里，不阻塞渲染，但仍可能被 abort 波及杀死——按 MAX_CHUNK 分片
     解析、每片之后原子落盘（mkstemp + os.replace，0600，断点在状态文件），被杀
     只丢一片进度、绝不污染状态；单次调用在 TIME_BUDGET 内连读多片，预算跨文件共用
     （进入下一个文件前也检查）。
  2. 追平全部文件尾之前不输出：宁缺勿假，不显示爬升中的半截数字。
  3. 绝不向 stdout 输出错误信息；任何异常静默吞掉。
  4. 同一 API 响应会以多行重复出现（每内容块一行），后写的行会修正 usage（首行可为全 0
     占位）。去重按 message.id（CC Switch session_usage.rs:831 同规则）：新行有 stop_reason
     而旧行没有 → 替换；两者同有或同无 → 取 output 更大者。按差值修正总量；pending 映射
     FIFO 上限 PEND_KEEP，挤出的贡献冻结。
  5. 状态版本只认文件自身的 v：EMPTY 自带 v，先 update 再判版本恒为真。
  6. 每个文件记录游标前 FP_BYTES 字节的 crc32；不符说明文件被原地改写（变短之外的情形），
     整体重扫。已记账的文件消失（subagent 目录被清理）同样整体重扫——persist() 对消失的
     文件取指纹会抛异常，助手从此每次都无输出。
  7. 只读打开 CC Switch 数据库（mode=ro），且只在中转站模式下访问。
  8. 预筛子串只用来跳过不相干的行：含 "compact_boundary" 的行先确认真是 compact 行才跳过，
     否则照常累计 usage（tool_use 的 input 里出现这个词很常见）。
"""
import calendar
import glob
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import zlib

MAX_CHUNK = 32 * 1024 * 1024  # 每片解析字节数（每片之后落盘一次）
TIME_BUDGET = 1.0             # 单次调用连读分片的时间预算（秒）
PEND_KEEP = 4096              # 去重 pending 映射的 FIFO 上限（见约束 4；全部文件共用一个映射）
FP_BYTES = 64                 # 尾部指纹字节数（约束 6）
STATE_V = 9                   # 状态结构版本；不一致即重扫（v9：每文件消息数 last_id/msgs；v8：多文件、指纹、未入账集合）
PROXY_DEDUP_WINDOW = 600      # 与 CC Switch SESSION_PROXY_DEDUP_WINDOW_SECONDS 一致
PROXY_SETTLE = 900            # 消息发出超过此秒数仍匹配不到代理行，不再反复查

# 各追踪器：(EWMA 半衰期秒, 预热秒)
TRACK = {'cost': (1200, 120), 'q5': (900, 300), 'day': (600, 120), 'sp': (900, 300)}
# 额度窗口：(窗口秒数, 窗口均速至少需已过的秒数, 是否并用近期速率)
WINDOW = {'q5': (18000, 1800, True), 'q7': (604800, 86400, False)}
# 网关花费限额按 period 取窗口（月按 30 天）；日限额并用近期速率，周 / 月同 7d 只用窗口均速
SP_WINDOW = {'daily': (86400, 1800, True), 'weekly': (604800, 86400, False),
             'monthly': (2592000, 86400, False)}
# 预计用完倒计时短于此值即判 bad（「很快用完」）
URGENT = {'q5': 1800, 'q7': 21600, 'day': 3600, 'sp': 3600}
EXHAUSTED = '用尽'            # 已用 ≥ 100% 时的「用完倒计时」段
# 与 bash 侧同源：显式 CLAUDE_SL_TMP，否则每用户私有目录（不再默认写共享 /tmp）
TMP = os.environ.get('CLAUDE_SL_TMP') or os.path.join(
    os.environ.get('TMPDIR') or '/tmp', f'claude-sl-{os.getuid()}')
RESET_DROP = 0.5              # 追踪值回落超过一半才视为重置；小幅回落（重启后费用重算）只更新基准

EMPTY = {'v': STATE_V, 'files': {}, 'in': 0, 'out': 0, 'cw': 0, 'cr': 0,
         'comp': 0, 'pend': [], 'trk': {}, 'unl': {}}
KEYS = ('in', 'out', 'cw', 'cr')
# 输出字段顺序：bash 的 IFS=tab read、tests/_util.py、tools/preview.sh 都以它为准
FIELDS = ('in', 'out', 'rate', 'comp', 'q5lv', 'q5out', 'q7lv', 'q7out', 'dlv', 'dout',
          'rcost', 'runpriced', 'rtoday', 'rlimit', 'hit', 'splv', 'spout', 'msgs')


def fmt(n):
    if n >= 999_500_000:                   # 十亿档：1.2GB transcript 实测累计 3.9B
        v = round(n / 1_000_000_000, 1)
        return f'{int(v)}B' if v == int(v) else f'{v:.1f}B'
    if n >= 999_500:                       # 999.5k 起按 M 显示，避免 "1000k"
        v = round(n / 1_000_000, 1)
        if v >= 100:                       # 三位数 M 不保留小数，免得段过宽
            return f'{v:.0f}M'
        return f'{int(v)}M' if v == int(v) else f'{v:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}k'
    return str(int(n))


def fmt_usd(r):
    if r < 1:
        return f'${r:.2f}/h'
    if r < 10:
        return f'${r:.1f}/h'
    return f'${r:.0f}/h'


def transcript_files(path):
    """主 transcript + 同会话 subagent / workflow transcript（CC Switch 同口径）。"""
    base = path[:-6] if path.endswith('.jsonl') else path
    subs = glob.glob(os.path.join(base, 'subagents', '*.jsonl'))
    subs += glob.glob(os.path.join(base, 'subagents', 'workflows', '*', '*.jsonl'))
    return [path] + sorted(subs)


def tail_fp(path, off):
    if off <= 0:
        return 0
    with open(path, 'rb') as f:
        f.seek(max(0, off - FP_BYTES))
        return zlib.crc32(f.read(min(FP_BYTES, off)))


def parse_ts(s):
    """transcript 时间戳是 UTC ISO8601（…Z）。"""
    try:
        return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        return None


def scan_chunk(path, fs, st, pend, order, main, track_unl):
    """从 fs['off'] 读一片并累加进 st；返回是否已追平该文件尾。"""
    with open(path, 'rb') as f:
        f.seek(fs['off'])
        raw = f.read(MAX_CHUNK)
    caught_up = len(raw) < MAX_CHUNK           # 读不满一片 = 已到当时的文件尾
    nl = raw.rfind(b'\n')                      # 末尾不完整行留给下次
    if nl < 0 and not caught_up:               # 单行超过一整片：跳过，否则游标永不前进
        fs['off'] += len(raw)
        return False
    raw = raw[:nl + 1] if nl >= 0 else b''
    fs['off'] += len(raw)

    for line in raw.split(b'\n'):
        obj = None
        if b'"compact_boundary"' in line:      # 约束 8：先确认真是 compact 行才跳过
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('type') == 'system' and obj.get('subtype') == 'compact_boundary':
                if main and not obj.get('isSidechain'):   # 每行只扫一次（游标推进），无需去重
                    st['comp'] += 1
                continue
        if b'"usage"' not in line:
            continue
        if obj is None:
            try:
                obj = json.loads(line)
            except Exception:
                continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get('message')
        if not isinstance(msg, dict) or not isinstance(msg.get('usage'), dict):
            continue
        u = msg['usage']
        vals = (u.get('input_tokens') or 0, u.get('output_tokens') or 0,
                u.get('cache_creation_input_tokens') or 0,
                u.get('cache_read_input_tokens') or 0)
        stop = msg.get('stop_reason') is not None
        mid = msg.get('id')
        # 消息数：同一个 message.id 的行在 transcript 里连续出现（本机实测非连续复现 0 次），
        # 按 id 变化计数即精确值——不必存 id 表，状态只多两个标量
        if mid and mid != fs.get('last_id'):
            fs['last_id'] = mid
            fs['msgs'] = fs.get('msgs', 0) + 1
        if mid and mid in pend:                # 约束 4：stop_reason 优先，其次 output 最大
            old, ostop = pend[mid]
            if not ((stop and not ostop) or (stop == ostop and vals[1] > old[1])):
                continue
            for k, nv, ov in zip(KEYS, vals, old):
                st[k] += nv - ov
        else:
            for k, v in zip(KEYS, vals):
                st[k] += v
            if mid:
                order.append(mid)
                while len(order) > PEND_KEEP:
                    pend.pop(order.pop(0), None)
        if mid:
            pend[mid] = (vals, stop)
            if track_unl and any(vals):        # 中转站：记入未入账集合（零用量的合成消息不记）
                cw1 = (u.get('cache_creation') or {}).get('ephemeral_1h_input_tokens') or 0
                st['unl'][mid] = [msg.get('model') or '', vals[0], vals[1], vals[3], vals[2],
                                  cw1, parse_ts(obj.get('timestamp') or '') or time.time(), 0]
    return caught_up


def save(state_path, st):
    """原子写；mkstemp 保证临时文件 0600 且不会跟随预置的符号链接。"""
    tmp = None
    try:
        d = os.path.dirname(state_path) or '.'
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(state_path) + '.', text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        os.replace(tmp, state_path)
        tmp = None
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── CC Switch 查价（移植自 usage_stats.rs find_model_pricing_row）──────────
NON_ANTHROPIC = ('abab', 'ark-code', 'arctic', 'astron', 'codex', 'command-r', 'deepseek',
                 'doubao', 'ernie', 'gemini', 'gemma', 'glm', 'gpt', 'grok', 'hermes', 'hy3',
                 'hunyuan', 'jamba', 'kimi', 'lfm', 'llama', 'longcat', 'mercury', 'mimo',
                 'minimax', 'mistral', 'mixtral', 'moonshot', 'nemotron', 'nova-', 'openai',
                 'qianfan', 'qwen', 'seed-', 'solar', 'stepfun')
PREFIX_FAMILIES = ('gpt-', 'gemini-', 'deepseek-', 'qwen-', 'glm-', 'kimi-', 'minimax-')


def pricing_candidates(model):
    m = model.rsplit('/', 1)[-1].split(':', 1)[0].strip().replace('@', '-').lower()
    m = m.removesuffix('[1m]').strip()
    if m in ('', 'unknown', 'null', 'none'):
        return []
    out, queue = [], [m]
    while queue:
        c = queue.pop()
        if c in out:
            continue
        out.append(c)
        pos = c.rfind('claude-')
        if pos > 0:
            queue.append(c[pos:])
        for mk in ('openai.', 'anthropic.', 'google.', 'moonshot.', 'moonshotai.', 'bedrock.', 'global.'):
            if c.startswith(mk):
                queue.append(c[len(mk):])
        if c.startswith('claude-') and c[7:].startswith(NON_ANTHROPIC):
            queue.append(c[7:])
        base, _, suf = c.rpartition('-v')
        if base and suf.isdigit():
            queue.append(base)
        if len(c) > 11 and c[-11] == '-' and c[-10:-6].isdigit() and c[-6] == '-' \
                and c[-5:-3].isdigit() and c[-3] == '-' and c[-2:].isdigit():
            queue.append(c[:-11])
        base, _, suf = c.rpartition('-')
        if base and suf.isdigit() and (len(suf) == 8 or (len(suf) == 6 and 1 <= int(suf[2:4]) <= 12
                                                         and 1 <= int(suf[4:6]) <= 31)):
            queue.append(base)
        for e in ('-minimal', '-low', '-medium', '-high', '-xhigh'):
            if c.endswith(e) and len(c) > len(e):
                queue.append(c[:-len(e)])
    return out


def prefix_ok(c):
    d = c.count('-')
    if c.startswith('claude-'):
        return d >= 3
    if c.startswith(('o1', 'o3', 'o4', 'o5')):
        return d >= 1
    return c.startswith(PREFIX_FAMILIES) and d >= 2


def find_pricing(conn, model, cache):
    if model in cache:
        return cache[model]
    sel = ('SELECT input_cost_per_million, output_cost_per_million, cache_read_cost_per_million, '
           'cache_creation_cost_per_million FROM model_pricing ')
    cands, row = pricing_candidates(model), None
    for c in cands:
        row = conn.execute(sel + 'WHERE model_id = ?', (c,)).fetchone()
        if row:
            break
    if not row:
        for c in cands:
            if prefix_ok(c):
                row = conn.execute(sel + 'WHERE model_id LIKE ? ORDER BY LENGTH(model_id) LIMIT 1',
                                   (c + '-%',)).fetchone()
                if row:
                    break
    cache[model] = [float(x) for x in row] if row else None
    return cache[model]


def msg_cost(p, e):
    """calculator.rs 的 Claude 语义：input 不含缓存；1h 缓存写入按 5m 单价 ×1.6；倍率 1。"""
    _, inp, out, cr, cw, cw1 = e[:6]
    cw1 = min(cw1, cw)
    return (inp * p[0] + out * p[1] + cr * p[2] + ((cw - cw1) + cw1 * 1.6) * p[3]) / 1e6


def _limit_usd(v):
    """限额字符串 → 正数；空 / 非数字 / 0 与负数视为未设置（不猜）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def current_provider(conn):
    """→ (provider_id, 日限额 USD 或 None)。

    与 CC Switch 4.x 的 check_provider_limits 同源：限额写在 providers.meta 的
    limitDailyUsd（旧库只有 limit_daily_usd 列）。没有或不止一个 is_current 时返回
    (None, None)——供应商归属不可靠，额度项必须隐藏，不能拿未知当 0。meta 存在但
    解析失败 / 不是对象，同样按未知处理，不回落旧列（上游已经不读那一列）。
    """
    try:
        rows = conn.execute(
            "SELECT id, limit_daily_usd, meta FROM providers "
            "WHERE app_type = 'claude' AND is_current = 1").fetchall()
    except sqlite3.OperationalError:            # 旧库没有 meta 列
        try:
            rows = conn.execute(
                "SELECT id, limit_daily_usd, NULL FROM providers "
                "WHERE app_type = 'claude' AND is_current = 1").fetchall()
        except sqlite3.OperationalError:
            return None, None
    if len(rows) != 1:
        return None, None
    pid, col, meta = rows[0]
    lim, has_meta = None, False
    if meta:
        try:
            m = json.loads(meta)
        except Exception:
            m = None
        if isinstance(m, dict):
            if 'limitDailyUsd' in m:            # 明确写了就以它为准，值坏了也不回落到旧列
                has_meta = True
                lim = _limit_usd(m.get('limitDailyUsd'))
        else:
            has_meta = True                     # meta 坏了：未知，不回落旧列
    if not has_meta:                            # 旧库没有 meta / meta 是空对象
        lim = _limit_usd(col)
    return pid, lim


def provider_today(conn, pid, midnight, day):
    """当前供应商今日可归属费用 = 明细行 + 当日 rollup（与上游 check_provider_limits 同口径）。
    明细按 provider_id 精确过滤；provider_id='_session' 的会话日志不可归属，天然不在内。"""
    detail = conn.execute(
        "SELECT COALESCE(SUM(CAST(total_cost_usd AS REAL)), 0) FROM proxy_request_logs "
        "WHERE provider_id = ? AND app_type = 'claude' AND created_at >= ?",
        (pid, int(midnight))).fetchone()[0]
    roll = 0.0
    try:
        roll = conn.execute(
            "SELECT COALESCE(SUM(CAST(total_cost_usd AS REAL)), 0) FROM usage_daily_rollups "
            "WHERE provider_id = ? AND app_type = 'claude' AND date = ?",
            (pid, day)).fetchone()[0]
    except sqlite3.OperationalError as e:
        # 只有「旧库没有汇总表」可以按 0；锁 / 列漂移等是未知，交给上层隐藏限额。
        if 'no such table: usage_daily_rollups' not in str(e):
            raise
        roll = 0.0
    return float(detail) + float(roll)


def relay_ledger(db, sid, st, now, midnight):
    """→ dict（会话花费 / 未计价数 / 全局今日 / 当前供应商今日 / 日限额 / 供应商 id），
    同时把已入账消息移出 st['unl']。"""
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1)   # 约束 7
    try:
        unl = st['unl']
        ids = list(unl)
        for i in range(0, len(ids), 500):      # 会话日志已入账：request_id = 'session:<message.id>'
            chunk = ids[i:i + 500]
            q = 'SELECT request_id FROM proxy_request_logs WHERE request_id IN (%s)' % ','.join('?' * len(chunk))
            for (rid,) in conn.execute(q, ['session:' + x for x in chunk]):
                unl.pop(rid[8:], None)
        # CC Switch 只保留约 30 天：早于库内最早记录的消息与用量页同口径，不再自算（见文档 4）
        oldest = conn.execute('SELECT MIN(created_at) FROM proxy_request_logs').fetchone()[0]
        if oldest is not None:
            for mid in [m for m, e in unl.items() if e[6] < oldest]:
                unl.pop(mid)
        for mid, e in list(unl.items()):       # 代理已记账：按 CC Switch 去重键匹配
            if e[7]:
                continue
            # app_type + COALESCE(data_source) 与索引 idx_request_logs_dedup_lookup_expr 一致：
            # 每条 19 ms → 0.01 ms（30 万行实测）；且只与 Claude 的代理行匹配
            hit = conn.execute(
                "SELECT 1 FROM proxy_request_logs WHERE app_type = 'claude' "
                "AND COALESCE(data_source, 'proxy') = 'proxy' AND status_code BETWEEN 200 AND 299 "
                'AND created_at BETWEEN ? AND ? AND input_tokens = ? AND output_tokens = ? '
                'AND cache_read_tokens = ? AND cache_creation_tokens = ? LIMIT 1',
                (e[6] - PROXY_DEDUP_WINDOW, e[6] + PROXY_DEDUP_WINDOW, e[1], e[2], e[3], e[4])).fetchone()
            if hit:
                unl.pop(mid)
            elif now - e[6] > PROXY_SETTLE:
                e[7] = 1                       # 久未匹配：定为直连，之后只等会话日志入账
        cost, unpriced = conn.execute(
            'SELECT COALESCE(SUM(CAST(total_cost_usd AS REAL)), 0), '          # CC Switch 以 TEXT 存 Decimal
            'COALESCE(SUM(CAST(total_cost_usd AS REAL) = 0 AND input_tokens + output_tokens > 0), 0) '
            'FROM proxy_request_logs WHERE session_id = ?', (sid,)).fetchone()
        tail = tail_today = 0.0
        cache = {}
        for e in unl.values():
            p = find_pricing(conn, e[0], cache)
            if p is None:
                unpriced += 1 if e[1] + e[2] > 0 else 0
                continue
            c = msg_cost(p, e)
            tail += c
            tail_today += c if e[6] >= midnight else 0
        # 今日费用分三个口径（见文档 4）：
        #   global = 全部 Claude 明细 + 本会话尾部（无日限额时显示）
        #   cur    = 当前供应商可归属（proxy 明细 + 当日 rollup；日限额的分子）
        #   unattr = global - cur（其它供应商、'_session' 会话日志、尚未入账的尾部；
        #            由调用方按差值得到，不单独回传）
        day = time.strftime('%Y-%m-%d', time.localtime(now))   # 本地日期，与 rollup.date 同口径
        try:
            pid, lim = current_provider(conn)
            cur = provider_today(conn, pid, midnight, day) if (pid and lim is not None) else None
        except sqlite3.OperationalError:           # 无法可靠归属：隐藏限额，不拿未知当 0
            pid, lim, cur = None, None, None
        today_all = conn.execute(
            "SELECT COALESCE(SUM(CAST(total_cost_usd AS REAL)), 0) FROM proxy_request_logs "
            "WHERE created_at >= ? AND app_type = 'claude'", (int(midnight),)).fetchone()[0]
        today_global = today_all + tail_today
        return {
            'cost': cost + tail,
            'unpriced': int(unpriced),
            'today_global': today_global,
            'today_cur': cur,
            'limit': lim,
            'provider_id': pid,
        }
    finally:
        conn.close()


def track(st, key, value, now, epoch=None):
    """把 value 并入 key 追踪器的 EWMA 斜率。所属窗口（epoch）变化或 value 回落过半时
    重新起算；小幅回落（/restart 后 Claude Code 重算费用，实测会略降）只挪基准、不重置——
    此前任何回落都重置，重启后 $/h 要重新预热。"""
    hl = TRACK[key][0]
    t = st['trk'].get(key)
    if t is None or t.get('ep') != epoch or value < t['lv'] * RESET_DROP:
        st['trk'][key] = {'ew': 0.0, 'lt': now, 'lv': value, 't0': now, 'ep': epoch}
        return
    if value < t['lv']:                        # 小幅回落：这段时间按斜率 0 衰减，再挪基准
        if now > t['lt']:
            t['ew'] *= math.exp(-(now - t['lt']) * math.log(2) / hl)
        t['lt'], t['lv'] = now, value
        return
    if now > t['lt']:
        dt = now - t['lt']
        a = 1 - math.exp(-dt * math.log(2) / hl)
        t['ew'] += a * ((value - t['lv']) / dt - t['ew'])
        t['lt'], t['lv'] = now, value


def rate(st, key, now):
    """key 追踪器经偏差校正的速率（单位/秒）；预热未满返回 None。"""
    t = st['trk'].get(key)
    hl, warm = TRACK[key]
    if t is None or now - t['t0'] < warm:
        return None
    w = 1 - math.exp(-(t['lt'] - t['t0']) * math.log(2) / hl)
    return max(t['ew'] / w, 0) if w > 0 else None


def dur(sec):
    sec = int(sec)
    if sec < 60:
        return f'{sec}s'
    if sec < 3600:
        return f'{sec // 60}m'
    if sec < 86400:
        return f'{sec // 3600}h{sec % 3600 // 60:02d}m'
    return f'{sec // 86400}d{sec % 86400 // 3600}h'


def pace(used, cap, left, rates, th, urgent):
    """额度节奏 → (等级, 用完倒计时)。used/cap 同单位；left = 距重置秒数；
    rates = 可得的速率列表（单位/秒），取最大者外推；分级见文档第 5 条。"""
    if cap <= 0:
        return 'ok', '-'
    pct = used / cap * 100
    if pct >= 100:
        return 'bad', EXHAUSTED
    if not rates:
        return ('high' if pct >= th[1] else 'ok'), '-'
    r = max(rates)
    proj = (used + r * left) / cap * 100
    eta_s = (cap - used) / r if proj >= 100 and r > 0 and used < cap else None
    if eta_s is not None and eta_s < urgent:
        lvl = 'bad'
    elif pct >= th[1]:
        lvl = 'high'
    elif proj >= 100:
        lvl = 'warn'
    else:
        lvl = 'ok'
    return lvl, dur(eta_s) if eta_s is not None else '-'


def main():
    if len(sys.argv) < 3:
        return
    path, sid = sys.argv[1], sys.argv[2]
    kv = dict(a.split('=', 1) for a in sys.argv[3:] if '=' in a)

    def nums(k):
        try:
            return [float(x) for x in kv[k].split(',')]
        except (KeyError, ValueError):
            return None

    cost = (nums('cost') or [None])[0]
    th = nums('th') or [50, 80]
    db = kv.get('relay')
    db = db if db and os.path.isfile(db) else None
    if not os.path.isfile(path):
        return
    state_path = os.path.join(TMP, f'claude-statusline-{os.path.basename(sid)}.json')
    st = dict(EMPTY)
    try:
        with open(state_path, encoding='utf-8') as f:
            loaded = json.load(f)
        if loaded.get('v') == STATE_V:     # 约束 5
            st.update(loaded)
    except Exception:
        pass
    files = transcript_files(path)
    for p in files:                            # 约束 6：变短或原地改写 → 整体重扫
        fs = st['files'].get(p)
        if fs and (os.path.getsize(p) < fs['off'] or tail_fp(p, fs['off']) != fs['fp']):
            st = dict(EMPTY)
            break
    if any(not os.path.isfile(p) for p in st['files']):   # 约束 6：已记账的文件消失 → 整体重扫
        st = dict(EMPTY)
    st['files'] = {k: dict(v) for k, v in st['files'].items()}
    st['trk'] = dict(st['trk'])
    st['unl'] = dict(st['unl'])
    pend = {k: (tuple(v[0]), v[1]) for k, v in st['pend']}
    order = [k for k, _ in st['pend']]

    def persist():
        for p, fs in st['files'].items():
            fs['fp'] = tail_fp(p, fs['off'])
        st['pend'] = [[k, [list(pend[k][0]), pend[k][1]]] for k in order if k in pend]
        save(state_path, st)

    t0 = time.time()
    caught_up = True
    for p in files:
        if time.time() - t0 >= TIME_BUDGET:   # 约束 1：预算跨文件共用
            caught_up = False
            break
        fs = st['files'].setdefault(p, {'off': 0, 'fp': 0})
        while True:
            done = scan_chunk(p, fs, st, pend, order, p == path, db is not None)
            persist()                          # 每片落盘：被杀只丢当前一片（约束 1）
            if done or time.time() - t0 >= TIME_BUDGET:
                break
        caught_up = caught_up and done

    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    relay = None
    if db and caught_up:
        try:
            relay = relay_ledger(db, sid, st, now, midnight)
        except Exception:
            relay = None
    if relay:
        track(st, 'cost', relay['cost'], now, epoch='ledger')   # 中转站：$/h 跟账本走（见文档 4）
        if relay['limit'] is not None and relay['today_cur'] is not None:
            # 供应商切换 = 新窗口：epoch 带上 provider_id，避免把两家的速率混在一起
            track(st, 'day', relay['today_cur'], now,
                  epoch=f"{int(midnight)}:{relay['provider_id']}")
    elif cost is not None and not db:          # 中转站模式下不改喂 stdin 费用（口径不同）
        track(st, 'cost', cost, now, epoch='stdin')
    spv = kv.get('sp', '').split(',')
    sp = None
    if len(spv) == 3 and spv[2] in SP_WINDOW:
        try:
            sp = (float(spv[0]), float(spv[1]), spv[2])
        except ValueError:
            sp = None
    if sp and SP_WINDOW[sp[2]][2]:
        track(st, 'sp', sp[0], now, epoch=sp[1])
    quota = {}
    for k in ('q5', 'q7'):
        v = nums(k)
        if v and len(v) == 2:
            if WINDOW[k][2]:
                track(st, k, v[0], now, epoch=v[1])      # resets_at 变了 = 新窗口
            quota[k] = v
    persist()

    if not caught_up:
        return                                 # 未追平不出数（约束 2）
    r = rate(st, 'cost', now) if (relay or cost is not None) else None
    rate_s = fmt_usd(r * 3600) if r is not None else '-'
    d = {'in': fmt(st['in'] + st['cw'] + st['cr']), 'out': fmt(st['out']),
         'rate': rate_s, 'comp': str(st['comp'])}
    for k in ('q5', 'q7'):
        if k not in quota or quota[k][1] <= now:
            d[k + 'lv'] = d[k + 'out'] = '-'
            continue
        used, resets = quota[k]
        win, minel, recent = WINDOW[k]
        left = min(resets - now, win)
        el = win - left
        rates = [x for x in (used / el if el >= minel and used > 0 else None,
                             rate(st, k, now) if recent else None) if x is not None]
        d[k + 'lv'], d[k + 'out'] = pace(used, 100, left, rates, th, URGENT[k])
    if relay and relay['limit'] is not None and relay['today_cur'] is not None:
        used, cap = relay['today_cur'], relay['limit']
        el = now - midnight                    # 窗口均速 = 供应商今日已用 ÷ 自零点以来时长（文档 5）
        # 不并本会话 $/h：它可能来自其它供应商或 '_session'，与当前供应商限额不是同一总体
        rates = [x for x in (used / el if el >= 1800 and used > 0 else None,
                             rate(st, 'day', now)) if x is not None]
        d['dlv'], d['dout'] = pace(used, cap, midnight + 86400 - now, rates, th, URGENT['day'])
    else:
        d['dlv'] = d['dout'] = '-'
    if relay:
        d['rcost'], d['runpriced'] = f"{relay['cost']:.4f}", str(relay['unpriced'])
        cur_ok = relay['limit'] is not None and relay['today_cur'] is not None
        shown = relay['today_cur'] if cur_ok else relay['today_global']
        d['rtoday'] = f'{shown:.4f}'
        d['rlimit'] = f"{relay['limit']:.4f}" if relay['limit'] is not None else '-'
    else:
        d['rcost'] = d['runpriced'] = d['rtoday'] = d['rlimit'] = '-'
    tin = st['in'] + st['cw'] + st['cr']
    d['hit'] = f"{st['cr'] / tin * 100:.1f}" if tin else '-'
    if sp and sp[1] > now:                     # 网关花费限额（百分比口径，cap = 100）
        win, minel, recent = SP_WINDOW[sp[2]]
        left = min(sp[1] - now, win)
        el = win - left
        rates = [x for x in (sp[0] / el if el >= minel and sp[0] > 0 else None,
                             rate(st, 'sp', now) if recent else None) if x is not None]
        d['splv'], d['spout'] = pace(sp[0], 100, left, rates, th, URGENT['sp'])
    else:
        d['splv'] = d['spout'] = '-'
    d['msgs'] = str(sum(fs.get('msgs', 0) for fs in st['files'].values()))   # 消息数（见 1 的计数）
    print('\t'.join(d[f] for f in FIELDS))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass                                   # 约束 3：状态栏绝不见错误
