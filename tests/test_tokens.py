"""statusline-tokens.py 单元测试：计数去重、追踪器、节奏分级、花费限额、中转站对账。"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from _util import FIELDS, IDX, RENDERER_FACTS, REPO, SRC, iso_utc

PATH = os.path.join(SRC, 'statusline-tokens.py')


def load():
    spec = importlib.util.spec_from_file_location('sltokens', PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def line(mid, inp, out, stop, ts='2026-10-28T06:00:00Z', extra=None):
    m = {'id': mid, 'model': 'claude-opus-5-5', 'stop_reason': stop,
         'usage': {'input_tokens': inp, 'output_tokens': out,
                   'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}
    if extra:
        m['content'] = [extra]
    return json.dumps({'type': 'assistant', 'timestamp': ts, 'message': m})


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='ccpills-tok-')
        self.env = dict(os.environ, CLAUDE_SL_TMP=self.d)
        self.t = load()
        self.t.TMP = self.d

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_tok(self, tp, sid, *args):
        return subprocess.run([sys.executable, PATH, tp, sid, *args], capture_output=True, text=True, env=self.env)

    def fields(self, stdout):
        """「键=值」输出 → 按 FIELDS 排好的列表（没输出的键记 '-'）；整体无输出返回 []。"""
        if not stdout.strip():
            return []
        kv = dict(l.split('=', 1) for l in stdout.splitlines())
        self.assertEqual(set(kv) - set(FIELDS), {'TOK', 'tp'}, '输出了 FIELDS 之外的键，或缺格式版本行 / tp 行')
        return [kv.get(k, '-') for k in FIELDS]

    def state(self, sid):
        with open(os.path.join(self.d, f'claude-statusline-{sid}.json')) as f:
            return json.load(f)

    def write(self, name, lines):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return p


class Counting(Base):
    def test_dedup_stop_reason_then_larger_output(self):
        tp = self.write('a.jsonl', [line('m1', 10, 5, None), line('m1', 10, 300, 'end_turn'),
                                    line('m1', 10, 100, None), line('m2', 20, 40, 'end_turn')])
        out = self.fields(self.run_tok(tp, 'c1').stdout)
        self.assertEqual([out[IDX['in']], out[IDX['out']]], ['30', '340'])

    def test_malformed_lines_do_not_stall_counting(self):
        """形状不对的行只当没有：不能让异常在游标落盘前抛出——那样每轮都从同一片重读、
        再死一次，该会话的统计永久停更（外层兜底把异常吞掉，表面看不出）。"""
        bad_tok = json.dumps({'type': 'assistant', 'message': {
            'id': 'mbad', 'usage': {'input_tokens': '7', 'output_tokens': 1}}})
        tp = self.write('mal.jsonl', [
            '"compact_boundary"',                                   # 合法 JSON，但不是对象
            bad_tok,                                                # token 数是字符串
            line('minf', 1, 1, 'end_turn').replace('"input_tokens": 1', '"input_tokens": 1e400'),
            json.dumps({'type': 'assistant', 'message': {'id': ['x'], 'usage': {'input_tokens': 3}}}),
            line('mneg', -4, 2, 'end_turn'),                        # 负数
            line('m2', 20, 40, 'end_turn')])
        for _ in range(2):
            out = self.fields(self.run_tok(tp, 'mal').stdout)
            self.assertTrue(out, '一行形状不对就让整个会话不出数')
        self.assertEqual([out[IDX['in']], out[IDX['out']]], ['23', '44'])   # 3 + 20；形状不对的值按 0

    def test_unforeseen_extraction_error_drops_only_that_line(self):
        """字段守卫之外的意外（将来新加字段漏了守卫）只丢那一行：不能让异常逃出 scan_chunk，
        也不能留下改了一半的状态。用会抛异常的 nonneg / parse_ts 模拟漏掉的守卫。"""
        ts = '2026-10-28T06:00:00Z'
        tp = self.write('ux.jsonl', [line('m1', 7777, 1, 'end_turn', ts=ts), line('m2', 20, 40, 'end_turn', ts=ts),
                                     line('m3', 5, 5, 'end_turn', ts='boom')])
        orig_nn, orig_ts = self.t.nonneg, self.t.parse_ts

        def nn(v):
            if v == 7777:
                raise ValueError(v)
            return orig_nn(v)

        def pts(s):
            if s == 'boom':
                raise ValueError(s)
            return orig_ts(s)
        self.t.nonneg, self.t.parse_ts = nn, pts
        try:
            for track_unl in (False, True):
                st = json.loads(json.dumps(self.t.EMPTY))
                fs = {'off': 0, 'fp': 0}
                self.t.scan_chunk(tp, fs, st, {}, [], True, track_unl)
                self.assertEqual(fs['off'], os.path.getsize(tp), '被丢的行也要推进游标')
                # 中转站模式下 m3 的时间戳也要取，所以只有它被丢；普通模式只丢 m1
                self.assertEqual((fs['last_id'], fs['msgs']), ('m2', 1) if track_unl else ('m3', 2),
                                 '被丢的行不能留在状态里')
                self.assertEqual((st['in'], st['out']), (20, 40) if track_unl else (25, 45), track_unl)
                self.assertEqual(sorted(st['unl']), ['m2'] if track_unl else [])
        finally:
            self.t.nonneg, self.t.parse_ts = orig_nn, orig_ts

    def test_zero_usage_message_is_not_queued_for_ledger(self):
        """零用量的合成消息（usage 全 0）不进未入账集合：它没有任何花费可算，进去也只是占位。"""
        ts = '2026-10-28T06:00:00Z'
        tp = self.write('z0.jsonl', [line('m0', 0, 0, 'end_turn', ts=ts), line('m1', 10, 1, 'end_turn', ts=ts)])
        st = json.loads(json.dumps(self.t.EMPTY))
        self.t.scan_chunk(tp, {'off': 0, 'fp': 0}, st, {}, [], True, True)
        self.assertEqual(sorted(st['unl']), ['m1'])
        self.assertEqual((st['in'], st['out']), (10, 1), '零用量的行仍要计入消息数等统计')

    def test_compact_substring_does_not_drop_usage(self):
        tp = self.write('b.jsonl', [
            line('m1', 10, 300, 'tool_use', extra={'type': 'tool_use', 'name': 'Grep',
                                                   'input': {'pattern': 'compact_boundary'}}),
            json.dumps({'type': 'system', 'subtype': 'compact_boundary'})])
        out = self.fields(self.run_tok(tp, 'c2').stdout)
        self.assertEqual((out[IDX['in']], out[IDX['out']], out[IDX['comp']]), ('10', '300', '1'))

    def test_deleted_subagent_triggers_rescan(self):
        tp = self.write('c.jsonl', [line('m1', 10, 5, 'end_turn')])
        sub = self.write('c/subagents/agent-x.jsonl', [line('s1', 7, 3, 'end_turn')])
        o = self.fields(self.run_tok(tp, 'c3').stdout)
        self.assertEqual([o[IDX['in']], o[IDX['out']]], ['17', '8'])
        os.remove(sub)
        r = self.run_tok(tp, 'c3')
        o = self.fields(r.stdout)
        self.assertEqual([o[IDX['in']], o[IDX['out']]], ['10', '5'])
        self.assertEqual(r.stderr, '')

    def test_subagents_found_under_glob_metacharacters(self):
        """transcript 路径里有 [ ] * ?（如 CLAUDE_CONFIG_DIR=~/claude[1]）时 subagent 照样计入：
        路径要先转义再拼 glob 模式，否则 [1] 被当成字符类，只匹配到别处。"""
        tp = self.write('cfg[1]*?/g.jsonl', [line('m1', 10, 5, 'end_turn')])
        self.write('cfg[1]*?/g/subagents/agent-x.jsonl', [line('s1', 7, 3, 'end_turn')])
        self.write('cfg[1]*?/g/subagents/workflows/w1/agent-y.jsonl', [line('s2', 1, 1, 'end_turn')])
        o = self.fields(self.run_tok(tp, 'cg').stdout)
        self.assertEqual([o[IDX['in']], o[IDX['out']], o[IDX['msgs']]], ['18', '9', '3'])

    def test_output_is_versioned_key_value_lines(self):
        """首行是调用方传入的格式版本（版本号只在 bash 一处维护），其后每行「键=值」；
        拿不到的指标整行不写，不用占位符。"""
        tp = self.write('d.jsonl', [line('m1', 1, 1, 'end_turn')])
        out = self.run_tok(tp, 'c4', 'fmt=7').stdout.splitlines()
        self.assertEqual(out[0], 'TOK=7')
        kv = dict(l.split('=', 1) for l in out[1:])
        self.assertEqual((kv['in'], kv['out'], kv['msgs']), ('1', '1', '1'))
        # tp 只给诊断：主 transcript 路径，用来判断已陈旧的缓存属于活跃会话还是已结束的会话
        self.assertEqual(kv.pop('tp'), tp)
        self.assertLessEqual(set(kv), set(FIELDS))
        self.assertNotIn('-', kv.values())
        self.assertNotIn('q5lv', kv, '没给 5h 额度却输出了等级')

    def test_message_count_by_id_changes(self):
        """同一 id 的多行只计一次；id 连续变化逐个计；subagent 也计入。"""
        tp = self.write('m.jsonl', [line('a', 10, 1, None), line('a', 10, 2, None),
                                    line('a', 10, 5, 'end_turn'), line('b', 10, 1, 'end_turn'),
                                    line('c', 10, 1, 'end_turn')])
        self.write('m/subagents/agent-1.jsonl', [line('s1', 1, 1, 'end_turn')])
        out = self.fields(self.run_tok(tp, 'c7').stdout)
        self.assertEqual(out[IDX['msgs']], '4', '主对话 3 条（a 的三行只算一次）+ subagent 1 条')

    def test_old_state_version_rescans_message_count(self):
        """状态版本升级后必须整体重扫，否则消息数只从升级那一刻起算（线上曾显示 1）。"""
        tp = self.write('v.jsonl', [line('a', 1, 1, 'end_turn'), line('b', 1, 1, 'end_turn')])
        off = os.path.getsize(tp)
        with open(os.path.join(self.d, 'claude-statusline-v1.json'), 'w') as f:
            # 指纹必须写对：fp 不符也会触发重扫，会掩盖「版本检查失效」
            json.dump({'v': self.t.STATE_V - 1,
                       'files': {tp: {'off': off, 'fp': self.t.tail_fp(tp, off)}}}, f)
        self.assertEqual(self.fields(self.run_tok(tp, 'v1').stdout)[IDX['msgs']], '2')

    def test_time_budget_shared_across_files(self):
        tp = self.write('e.jsonl', [line('m1', 1, 1, 'end_turn')])
        for i in range(6):
            self.write(f'e/subagents/agent-{i}.jsonl', [line(f's{i}', 1, 1, 'end_turn')])
        orig = self.t.scan_chunk

        def slow(*a, **k):
            time.sleep(0.4)
            return orig(*a, **k)
        self.t.scan_chunk = slow
        sys.argv = ['x', tp, 'c5']
        t0 = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            self.t.main()
        self.assertLess(time.time() - t0, 3.0)  # 共享预算约 1.2 s；6 倍则是 2.4 s+，3.0 s 仍能区分且容忍慢 runner


class Incremental(Base):
    """跨调用的增量扫描：游标续读、半行保留、去重表持久化、原地改写重扫、被杀只丢一片。"""

    def io(self, tp, sid):
        o = self.fields(self.run_tok(tp, sid).stdout)
        return [o[IDX['in']], o[IDX['out']], o[IDX['msgs']]]

    def test_appended_lines_are_counted_once(self):
        tp = self.write('i1.jsonl', [line('a', 10, 5, 'end_turn'), line('b', 20, 7, 'end_turn')])
        self.assertEqual(self.io(tp, 'i1'), ['30', '12', '2'])
        with open(tp, 'a') as f:
            f.write(line('c', 100, 1, 'end_turn') + '\n')
        self.assertEqual(self.io(tp, 'i1'), ['130', '13', '3'], '追加的行没计入，或旧行被重复计')
        self.assertEqual(self.state('i1')['files'][tp]['off'], os.path.getsize(tp))

    def test_partial_last_line_waits_for_newline(self):
        """写到一半的行留给下次：既不能丢，也不能在补全后被跳过。"""
        tp = self.write('i2.jsonl', [line('a', 10, 5, 'end_turn')])
        rest = line('b', 20, 7, 'end_turn')
        with open(tp, 'a') as f:
            f.write(rest[:40])
        self.assertEqual(self.io(tp, 'i2'), ['10', '5', '1'])
        with open(tp, 'a') as f:
            f.write(rest[40:] + '\n')
        self.assertEqual(self.io(tp, 'i2'), ['30', '12', '2'])

    def test_dedup_survives_across_invocations(self):
        """同一 message.id 的修正行出现在下一次调用里：靠持久化的去重表按差值修正，不重复累计。"""
        tp = self.write('i3.jsonl', [line('a', 10, 5, None)])
        self.assertEqual(self.io(tp, 'i3'), ['10', '5', '1'])
        with open(tp, 'a') as f:
            f.write(line('a', 10, 300, 'end_turn') + '\n')
        self.assertEqual(self.io(tp, 'i3'), ['10', '300', '1'])

    def test_in_place_rewrite_triggers_rescan(self):
        """文件没变短但内容被原地改写（尾部指纹不符）：整体重扫，而不是从旧游标接着读。"""
        tp = self.write('i4.jsonl', [line('a', 10, 5, 'end_turn'), line('b', 20, 7, 'end_turn')])
        self.assertEqual(self.io(tp, 'i4'), ['30', '12', '2'])
        size = os.path.getsize(tp)
        # 指纹只看游标前 FP_BYTES 字节，所以改写要落在末行尾部（真实 transcript 的行尾是 uuid / 时间戳）
        last = line('b', 88, 7, 'end_turn').replace('"cache_read_input_tokens": 0', '"cache_read_input_tokens": 9')
        self.write('i4.jsonl', [line('a', 77, 5, 'end_turn'), last])
        self.assertEqual(os.path.getsize(tp), size, '夹具前提：改写前后等长，只有指纹能发现')
        self.assertEqual(self.io(tp, 'i4'), ['174', '12', '2'])

    def test_progress_is_saved_after_each_chunk(self):
        """约束 1：每片之后落盘。读到第 3 片时被杀，前两片的进度必须已经在状态文件里。"""
        rows = [line(f'm{i}', 10, 1, 'end_turn') for i in range(9)]
        tp = self.write('i5.jsonl', rows)
        self.t.MAX_CHUNK = (len(rows[0]) + 1) * 3            # 每片正好 3 行
        orig, n = self.t.scan_chunk, [0]

        def killed(*a, **k):
            n[0] += 1
            if n[0] == 3:
                raise KeyboardInterrupt
            return orig(*a, **k)
        self.t.scan_chunk = killed
        sys.argv = ['x', tp, 'i5']
        with self.assertRaises(KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            self.t.main()
        st = self.state('i5')
        self.assertEqual(st['files'][tp]['off'], self.t.MAX_CHUNK * 2, '前两片的进度没有落盘')
        self.assertEqual(st['in'], 60)
        self.assertEqual(self.io(tp, 'i5'), ['90', '9', '9'], '从断点续读后总量不对')

    def test_idle_call_with_many_files_still_outputs(self):
        """已追平的文件不落盘。否则每个文件写一遍整份状态，文件一多就把 1 s 预算耗光：
        排在后面的文件永远轮不到、永远不出数（实测去重表满时约 75 个文件即触发）。"""
        tp = self.write('many.jsonl', [line('m', 10, 1, 'end_turn')])
        for i in range(30):
            self.write(f'many/subagents/agent-{i:02d}.jsonl', [line(f's{i}', 1, 1, 'end_turn')])
        self.assertEqual(self.io(tp, 'many')[2], '31')
        orig, n = self.t.save, [0]

        def slow(*a, **k):                                  # 模拟大状态文件的序列化成本
            n[0] += 1
            time.sleep(0.05)
            return orig(*a, **k)
        self.t.save = slow
        sys.argv = ['x', tp, 'many']
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.t.main()
        self.assertEqual(len(self.fields(buf.getvalue())), len(FIELDS), '文件一多就不出数')
        self.assertLessEqual(n[0], 1, '没有新内容却反复落盘')


class Trackers(Base):
    def test_small_drop_decays_rate(self):
        st = {'trk': {}}
        for k in range(0, 1801, 60):
            self.t.track(st, 'cost', 10 + 30 * k / 3600, 1000 + k, epoch='stdin')
        self.t.track(st, 'cost', st['trk']['cost']['lv'] * 0.98, 10000, epoch='stdin')
        self.assertLess(self.t.rate(st, 'cost', 10000) * 3600, 1)

    def test_bias_correction_warms_to_true_rate(self):
        st = {'trk': {}}
        for k in range(0, 1201, 10):
            self.t.track(st, 'cost', 12 * k / 3600, 1000 + k, epoch='stdin')
        self.assertAlmostEqual(self.t.rate(st, 'cost', 2200) * 3600, 12, delta=0.5)

    def test_cost_half_life_is_1200s(self):
        """半衰期 600s → 1200s（DECISIONS：把单笔大请求的跳变压到 $7）。

        $12/h 稳态后小幅回落，1200s 后再读是 $4/h（600s 会读到 $2.4/h）——只用公开的
        track / rate，不碰内部状态。
        """
        rate_per_h, drop, ramp = 12.0, 0.98, 1200
        st = {'trk': {}}
        for k in range(0, ramp + 1, 10):
            self.t.track(st, 'cost', rate_per_h * k / 3600, 1000 + k, epoch='stdin')
        self.t.track(st, 'cost', rate_per_h * ramp / 3600 * drop, 1000 + ramp + 1200, epoch='stdin')
        self.assertAlmostEqual(self.t.rate(st, 'cost', 1000 + ramp + 1200) * 3600, 4.0, delta=0.1)


class Pace(Base):
    def test_levels(self):
        p, th = self.t.pace, [50, 80]
        self.assertEqual(p(36, 100, 13020, [64 / 8820], th, 1800)[0], 'warn')
        self.assertEqual(p(70, 100, 7200, [30 / 1200], th, 1800)[0], 'bad')
        self.assertEqual(p(82, 100, 7200, [1 / 3600], th, 1800), ('high', '-'))
        self.assertEqual(p(40, 100, 9000, [40 / 9000], th, 1800), ('ok', '-'))
        self.assertEqual(p(85, 100, 9000, [], th, 1800), ('high', '-'))

    def test_exhausted_and_zero_cap(self):
        self.assertEqual(self.t.pace(100, 100, 3600, [0.01], [50, 80], 1800), ('bad', '用尽'))
        self.assertEqual(self.t.pace(104, 100, 3600, [], [50, 80], 1800), ('bad', '用尽'))
        self.assertEqual(self.t.pace(5, 0, 100, [1], [50, 80], 1800), ('ok', '-'))

    def test_spend_limit_fields(self):
        tp = self.write('f.jsonl', [line('m1', 1, 1, 'end_turn')])
        now = int(time.time())
        out = self.fields(self.run_tok(tp, 'c6', f'sp=60,{now + 40000},daily').stdout)
        self.assertEqual(out[IDX['splv']], 'warn')
        self.assertNotEqual(out[IDX['spout']], '-')
        out = self.fields(self.run_tok(tp, 'c6', f'sp=60,{now + 40000},hourly').stdout)
        self.assertEqual([out[IDX['splv']], out[IDX['spout']]], ['-', '-'])


class Format(Base):
    def test_no_four_digit_mantissa(self):
        """「1000k」「1000M」都该进位到下一档：三位数 M 不留小数时，999.5M 会被格式化成 1000M。"""
        for n in (999_499, 999_500, 99_950_000, 999_449_999, 999_450_000, 999_499_999, 999_500_000):
            self.assertNotRegex(self.t.fmt(n), r'^1000', n)


class DayBounds(Base):
    def test_dst_day_is_not_assumed_86400s(self):
        """日限额的「距重置」= 下一个本地零点 − 现在；夏令时结束那天有 25 小时，不能按 86400 秒算。"""
        old = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            noon = time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))      # 当天凌晨 2 点回拨一小时
            midnight, nxt = self.t.day_bounds(noon)
            self.assertEqual((noon - midnight, nxt - noon), (13 * 3600, 12 * 3600))
            noon = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
            midnight, nxt = self.t.day_bounds(noon)
            self.assertEqual((noon - midnight, nxt - noon), (12 * 3600, 12 * 3600))
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()


class Relay(Base):
    def make_db(self, now):
        db = os.path.join(self.d, 'cc.db')
        c = sqlite3.connect(db)
        c.execute('create table proxy_request_logs(request_id text, session_id text, provider_id text, '
                  'app_type text, data_source text, status_code int, created_at int, input_tokens int, '
                  'output_tokens int, cache_read_tokens int, cache_creation_tokens int, total_cost_usd text)')
        c.execute('create table model_pricing(model_id text, input_cost_per_million real, output_cost_per_million real, '
                  'cache_read_cost_per_million real, cache_creation_cost_per_million real)')
        c.execute("insert into model_pricing values('claude-opus-5-5', 5, 25, 0.5, 6.25)")
        c.execute('create table providers(id text, app_type text, is_current int, limit_daily_usd text, meta text)')
        c.execute("insert into proxy_request_logs values('session:old', 'r1', '_session', 'claude', 'session_log', "
                  "200, ?, 1, 1, 0, 0, '30')", (now - 86400 * 5,))
        c.commit()
        c.close()
        return db

    def test_ledger_plus_tail_and_retention(self):
        now = int(time.time())
        db = self.make_db(now)
        tp = self.write('g.jsonl', [line('mold', 1000000, 0, 'end_turn', ts=iso_utc(now - 86400 * 40)),
                                    line('mnew', 1000, 100, 'end_turn', ts=iso_utc(now - 60))])
        out = self.fields(self.run_tok(tp, 'r1', f'relay={db}', 'cost=10').stdout)
        self.assertAlmostEqual(float(out[IDX['rcost']]), 30 + (1000 * 5 + 100 * 25) / 1e6, places=6,
                               msg='早于库内最早记录的消息不应再自算')
        st = self.state('r1')
        self.assertEqual(st['trk']['cost']['ep'], 'ledger')
        # 读库失败时不把 stdin 费用喂进追踪器
        os.rename(db, db + '.bak')
        with open(db, 'w') as f:
            f.write('not a db')
        self.run_tok(tp, 'r1', f'relay={db}', 'cost=999')
        st = self.state('r1')
        self.assertEqual(st['trk']['cost']['ep'], 'ledger')
        self.assertLess(st['trk']['cost']['lv'], 100)

    def test_malformed_relay_fields_do_not_stall_counting(self):
        """中转站模式多读 model 与 cache_creation 两个字段，形状不对同样只当没有：
        异常在游标落盘前抛出，就每轮从同一片重读、再死一次，该会话永久停更。"""
        now = int(time.time())
        db = self.make_db(now)
        ts = iso_utc(now - 60)

        def bad(mid, usage=None, msg=None):
            o = json.loads(line(mid, 10, 1, 'end_turn', ts=ts))
            o['message']['usage'].update(usage or {})
            o['message'].update(msg or {})
            return json.dumps(o)
        tp = self.write('rm.jsonl', [bad('m1', usage={'cache_creation': 'x'}),
                                     bad('m2', usage={'cache_creation': [1]}),
                                     bad('m3', usage={'cache_creation': {'ephemeral_1h_input_tokens': '5'}}),
                                     bad('m4', msg={'model': {'id': 'x'}}), line('m5', 10, 1, 'end_turn', ts=ts)])
        for _ in range(2):
            out = self.fields(self.run_tok(tp, 'rm', f'relay={db}').stdout)
            self.assertTrue(out, '一行形状不对就让整个会话不出数')
        self.assertEqual([out[IDX['in']], out[IDX['out']]], ['50', '5'])
        self.assertNotEqual(out[IDX['rcost']], '-')

    def test_unreadable_db_hides_rate_instead_of_freezing_it(self):
        """库读不了时追踪器停在上一次账本读数：不能把那个旧速率当现值输出（bash 侧每轮都
        刷新 .ts，陈旧判据拦不住它）。"""
        now = int(time.time())
        db = self.make_db(now)
        tp = self.write('fz.jsonl', [line('m1', 1, 1, 'end_turn')])
        self.run_tok(tp, 'fz', f'relay={db}', 'cost=10')
        p = os.path.join(self.d, 'claude-statusline-fz.json')
        st = self.state('fz')
        st['trk']['cost'] = {'ew': 0.001, 'lt': now - 60, 'lv': 0.0, 't0': now - 10800, 'ep': 'ledger'}
        with open(p, 'w') as f:
            json.dump(st, f)
        out = self.fields(self.run_tok(tp, 'fz', f'relay={db}', 'cost=10').stdout)
        self.assertNotEqual(out[IDX['rate']], '-', '前提：库可读时这个追踪器给得出速率')
        with open(p, 'w') as f:
            json.dump(st, f)
        os.rename(db, db + '.bak')
        with open(db, 'w') as f:
            f.write('not a db')
        out = self.fields(self.run_tok(tp, 'fz', f'relay={db}', 'cost=10').stdout)
        self.assertEqual(out[IDX['rate']], '-', '读库失败时输出了冻结的旧速率')

    def test_db_path_with_url_metacharacters_stays_read_only(self):
        """库路径拼进 URI：# / ? 会截断 mode=ro，SQLite 改以读写方式打开截断后的路径（约束 7）。"""
        now = int(time.time())
        sub = os.path.join(self.d, 'a#b?c')
        os.makedirs(sub)
        db = os.path.join(sub, 'cc.db')
        os.rename(self.make_db(now), db)
        tp = self.write('ro.jsonl', [line('m1', 1000, 100, 'end_turn')])
        out = self.fields(self.run_tok(tp, 'ro', f'relay={db}').stdout)
        self.assertNotEqual(out[IDX['rcost']], '-', '路径带 # ? 的库读不出账本')
        r = subprocess.run([sys.executable, PATH, '--provider-state', db], capture_output=True, text=True,
                           env=self.env)
        self.assertTrue(r.stdout.strip(), '探针读不出路径带 # ? 的库')
        self.assertEqual(sorted(os.listdir(self.d)), sorted(['a#b?c', 'claude-statusline-ro.json', 'ro.jsonl']),
                         '在截断后的路径上建出了新文件（读写打开）')

    def test_ledger_reads_share_one_snapshot(self):
        """账本的几条 SELECT 要在同一个读事务里：autocommit 下每条各看各的已提交状态，CC Switch
        在「是否已入账」与「求和」之间提交一批会话日志时，同一批消息会被账本与自算尾部各算一次。"""
        import sqlite3 as real
        now = int(time.time())
        db = self.make_db(now)
        seen = []

        class Shim:
            def __getattr__(self, k):
                return getattr(real, k)

            def connect(self, *a, **kw):
                c = real.connect(*a, **kw)
                c.set_trace_callback(seen.append)
                return c
        self.t.sqlite3 = Shim()
        st = dict(self.t.EMPTY, unl={'m1': ['claude-opus-5-5', 1, 1, 0, 0, 0, now - 60, False]})
        lt = time.localtime(now)
        self.t.relay_ledger(db, 'r1', st, now, time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        selects = [i for i, q in enumerate(seen) if q.lstrip().upper().startswith('SELECT')]
        self.assertTrue(selects)
        self.assertTrue(seen[0].strip().upper().startswith('BEGIN'), seen[:2])

    def test_today_counts_headless_claude_not_codex(self):
        """今日按 app_type 统计：没渲染过状态栏的 Claude 会话（无头）也计入，Codex 不计。"""
        now = int(time.time())
        db = self.make_db(now)
        c = sqlite3.connect(db)
        c.execute("insert into proxy_request_logs values('h1', 'headless-sid', '_session', 'claude', 'session_log', "
                  "200, ?, 10, 1, 0, 0, '2.5')", (now,))
        c.execute("insert into proxy_request_logs values('c1', 'codex-sid', 'codex-provider', 'codex', 'codex_session', "
                  "200, ?, 10, 1, 0, 0, '9')", (now,))
        c.commit()
        c.close()
        tp = self.write('i.jsonl', [line('m1', 1, 1, 'end_turn')])
        out = self.fields(self.run_tok(tp, 'r3', f'relay={db}').stdout)
        # 今日 = 本会话账本 0 + 无头 Claude 2.5 + 本会话尾部 0.00003（四位小数下不可见），Codex 的 9 排除
        self.assertAlmostEqual(float(out[IDX['rtoday']]), 2.5, places=4)

    def test_proxy_rows_dedup_only_claude(self):
        """代理已记账的消息不重复自算；Codex 的同 token 行不能顶替 Claude 消息。"""
        now = int(time.time())
        db = self.make_db(now)
        c = sqlite3.connect(db)
        c.execute("insert into proxy_request_logs values('p1', 'r2', 'B', 'claude', NULL, 200, ?, 2000, 200, 0, 0, '0.5')",
                  (now - 60,))
        c.execute("insert into proxy_request_logs values('p2', 'cx', 'codex-provider', 'codex', 'proxy', 200, ?, 3000, 300, 0, 0, '9')",
                  (now - 60,))
        c.commit()
        c.close()
        ts = iso_utc(now - 60)
        tp = self.write('h.jsonl', [line('mprox', 2000, 200, 'end_turn', ts=ts),
                                    line('mcodex', 3000, 300, 'end_turn', ts=ts)])
        out = self.fields(self.run_tok(tp, 'r2', f'relay={db}').stdout)
        self.assertAlmostEqual(float(out[IDX['rcost']]), 0.5 + (3000 * 5 + 300 * 25) / 1e6, places=6)

    def test_pricing_candidates(self):
        c = self.t.pricing_candidates
        self.assertIn('claude-opus-5-5', c('anthropic/claude-opus-5-5-20261001[1m]'))
        self.assertIn('claude-sonnet-5-5', c('Claude-Sonnet-5-5@20261001'))
        self.assertEqual(c('unknown'), [])


class DailyLimit(Base):
    """日限额口径：与 CC Switch check_provider_limits 一致——只算当前供应商可归属的行。

    上游 v4.0.21：限额在 providers.meta.limitDailyUsd（旧库只有 limit_daily_usd 列）；
    今日 = proxy_request_logs + usage_daily_rollups 中 provider_id=当前供应商 的行。
    provider_id='_session' 是会话日志的哨兵，无法归属供应商，上游也不把它算进限额。
    全局今日（无日限额时显示）仍是全部 Claude 行 + 本会话尾部。
    """

    def provider_db(self, now, current=('B',), a_meta=None, b_meta='{"limitDailyUsd":"50"}',
                    a_col='', b_col='', rollup=0.0):
        fd, db = tempfile.mkstemp(dir=self.d, prefix='lim-', suffix='.db')
        os.close(fd)
        c = sqlite3.connect(db)
        c.execute('create table proxy_request_logs(request_id text primary key, session_id text, '
                  'provider_id text, app_type text, data_source text, status_code int, created_at int, '
                  'input_tokens int, output_tokens int, cache_read_tokens int, cache_creation_tokens int, '
                  'total_cost_usd text)')
        c.execute('create table model_pricing(model_id text, input_cost_per_million real, '
                  'output_cost_per_million real, cache_read_cost_per_million real, '
                  'cache_creation_cost_per_million real)')
        c.execute("insert into model_pricing values('claude-opus-5-5', 5, 25, 0.5, 6.25)")
        c.execute('create table providers(id text, app_type text, is_current int, '
                  'limit_daily_usd text, meta text)')
        c.execute("insert into providers values('A','claude',?,?,?)",
                  (1 if 'A' in current else 0, a_col, a_meta))
        c.execute("insert into providers values('B','claude',?,?,?)",
                  (1 if 'B' in current else 0, b_col, b_meta))
        # 用 now 而不是 now-1h：刚过本地零点时，减一小时会落到昨天，测试会误判「今日」
        c.execute("insert into proxy_request_logs values('a1','other','A','claude','proxy',200,?,0,0,0,0,'30')",
                  (now,))
        c.execute("insert into proxy_request_logs values('b1','sess','B','claude','proxy',200,?,0,0,0,0,'5')",
                  (now,))
        c.execute("insert into proxy_request_logs values('s1','direct','_session','claude','session_log',200,?,0,0,0,0,'7')",
                  (now,))
        if rollup:
            c.execute('create table usage_daily_rollups(date text, app_type text, provider_id text, '
                      'total_cost_usd text)')
            c.execute("insert into usage_daily_rollups values(?, 'claude', 'B', ?)",
                      (time.strftime('%Y-%m-%d', time.localtime(now)), str(rollup)))
        c.commit()
        c.close()
        return db

    def test_limit_uses_current_provider_only(self):
        """当前供应商 B 今日 $5；全局今日 $42（A $30 + B $5 + _session $7）。"""
        now = int(time.time())
        db = self.provider_db(now)
        tp = self.write('lim1.jsonl', [])
        out = self.fields(self.run_tok(tp, 'l1', f'relay={db}').stdout)
        self.assertEqual(out[IDX['rtoday']], '5.0000', '日限额分子只能是当前供应商可归属费用')
        self.assertEqual(out[IDX['rlimit']], '50.0000')
        # 无日限额时 today 项回到全局口径（DECISIONS「今日花费」）
        db2 = self.provider_db(now, b_meta='{}')
        tp2 = self.write('lim2.jsonl', [])
        out2 = self.fields(self.run_tok(tp2, 'l2', f'relay={db2}').stdout)
        self.assertEqual(out2[IDX['rtoday']], '42.0000')
        self.assertEqual(out2[IDX['rlimit']], '-')

    def test_limit_meta_wins_over_legacy_column(self):
        now = int(time.time())
        db = self.provider_db(now, b_meta='{"limitDailyUsd":"50"}', b_col='60')
        tp = self.write('lim3.jsonl', [])
        out = self.fields(self.run_tok(tp, 'l3', f'relay={db}').stdout)
        self.assertEqual(out[IDX['rlimit']], '50.0000')

    def test_limit_hidden_when_unknown(self):
        """多个 current / 限额值损坏：不猜、不显示、不外推。未知不能当 0。"""
        now = int(time.time())
        db = self.provider_db(now, current=('A', 'B'), b_meta='{}', a_col='20', b_col='50')
        tp = self.write('lim4.jsonl', [])
        out = self.fields(self.run_tok(tp, 'l4', f'relay={db}').stdout)
        self.assertEqual(out[IDX['rlimit']], '-')
        self.assertEqual(out[IDX['dlv']], '-')
        self.assertEqual(out[IDX['dout']], '-')
        self.assertEqual(out[IDX['rtoday']], '42.0000')
        db2 = self.provider_db(now, b_meta='{"limitDailyUsd":"abc"}', b_col='50')
        out2 = self.fields(self.run_tok(tp, 'l5', f'relay={db2}').stdout)
        self.assertEqual(out2[IDX['rlimit']], '-', 'meta 明确写了非法值时不回落到旧列')
        self.assertEqual(out2[IDX['dlv']], '-')
        for i, bad in enumerate(('not json', '[]', 'null')):
            db3 = self.provider_db(now, b_meta=bad, b_col='50')
            out3 = self.fields(self.run_tok(tp, f'l5b{i}', f'relay={db3}').stdout)
            self.assertEqual(out3[IDX['rlimit']], '-', f'meta={bad!r} 时不应回落旧列')

    def test_limit_hidden_on_rollup_schema_drift(self):
        """汇总表存在但列漂移 = 未知，不能当 0（否则限额分子偏小、预测错误）。"""
        now = int(time.time())
        db = self.provider_db(now)
        c = sqlite3.connect(db)
        c.execute('create table usage_daily_rollups(date text, provider_id text)')  # 缺 total_cost_usd
        c.commit()
        c.close()
        tp = self.write('lim7.jsonl', [])
        out = self.fields(self.run_tok(tp, 'l8', f'relay={db}').stdout)
        self.assertEqual(out[IDX['rlimit']], '-')
        self.assertEqual(out[IDX['dlv']], '-')
        self.assertEqual(out[IDX['rtoday']], '42.0000')

    def test_limit_includes_rollup_excludes_tail(self):
        """日限额分子含 usage_daily_rollups；不含尚未入账的尾部（尾部将记为 _session）。"""
        now = int(time.time())
        db = self.provider_db(now, rollup=2.0)
        ts = iso_utc(now)
        tp = self.write('lim5.jsonl', [line('mt', 1000, 100, 'end_turn', ts=ts)])
        out = self.fields(self.run_tok(tp, 'l6', f'relay={db}').stdout)
        self.assertEqual(out[IDX['rtoday']], '7.0000', 'B 的 $5 + rollup $2；尾部不计入限额分子')
        self.assertAlmostEqual(float(out[IDX['rcost']]), (1000 * 5 + 100 * 25) / 1e6, places=6)

    def test_day_tracker_resets_on_provider_switch(self):
        now = int(time.time())
        db = self.provider_db(now, a_meta='{"limitDailyUsd":"20"}',
                              b_meta='{"limitDailyUsd":"50"}')
        tp = self.write('lim6.jsonl', [])
        out1 = self.fields(self.run_tok(tp, 'l7', f'relay={db}').stdout)
        self.assertEqual((out1[IDX['rtoday']], out1[IDX['rlimit']]), ('5.0000', '50.0000'))
        ep1 = self.state('l7')['trk']['day']['ep']
        self.assertTrue(str(ep1).endswith(':B'))
        c = sqlite3.connect(db)
        c.execute("update providers set is_current = (id = 'A')")
        c.commit()
        c.close()
        out2 = self.fields(self.run_tok(tp, 'l7', f'relay={db}').stdout)
        self.assertEqual((out2[IDX['rtoday']], out2[IDX['rlimit']]), ('30.0000', '20.0000'))
        ep2 = self.state('l7')['trk']['day']['ep']
        self.assertTrue(str(ep2).endswith(':A'))
        self.assertNotEqual(ep1, ep2, '切换供应商必须重置日速率追踪，不能把两家的速率混在一起')

    def test_ledger_distinguishes_global_cur_unattr(self):
        """返回值必须同时保留三个口径，供展示层选择；unattr 不能悄悄并进 cur。"""
        now = int(time.time())
        db = self.provider_db(now)
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        r = self.t.relay_ledger(db, 'nobody', {'unl': {}}, now, midnight)
        self.assertAlmostEqual(r['today_global'], 42.0, places=6)
        self.assertAlmostEqual(r['today_cur'], 5.0, places=6)
        self.assertAlmostEqual(r['today_global'] - r['today_cur'], 37.0, places=6,
                               msg='不可归属 = 全局 - 当前供应商（其它供应商 + _session + 尾部）')

    def probe(self, db):
        return subprocess.run([sys.executable, PATH, '--provider-state', db],
                              capture_output=True, text=True, env=self.env).stdout

    def test_provider_probe_reports_what_the_renderer_would_use(self):
        """--provider-state：诊断工具问「限额口径能不能确定」，答案来自渲染用的同一段代码。"""
        now = int(time.time())
        cases = [(dict(), '1\tok'),
                 (dict(current=()), '0\t-'),
                 (dict(current=('A', 'B')), '2\t-'),
                 (dict(b_meta=None, b_col=None), '1\tnone'),
                 (dict(b_meta='{"limitDailyUsd":"abc"}'), '1\tbad'),
                 (dict(b_meta='not json'), '1\tbad'),
                 (dict(b_meta='{}', b_col='12.5'), '1\tok')]
        for kw, want in cases:
            out = self.probe(self.provider_db(now, **kw)).rstrip('\n')
            n, rest = out.split('\t', 1)
            self.assertTrue(n.isdigit() and int(n) > 0, out)
            self.assertEqual(rest, want, kw)
        self.assertEqual(self.probe(os.path.join(self.d, 'nope.db')), '', '打不开的库应当无输出')


class TokenProtocol(unittest.TestCase):
    """token 缓存的跨语言契约：生产者能写的键，渲染脚本与预览假数据都要认。"""

    def test_renderer_reads_every_key_the_producer_emits(self):
        self.assertEqual(RENDERER_FACTS['tok_keys'].split(), list(FIELDS),
                         'statusline-tokens.py 的 FIELDS 与渲染脚本的 TOK_MAP 不一致')

    def test_preview_fake_tokens_use_known_keys(self):
        """preview.sh 的假缓存只能用 FIELDS 里的键，且要带消息数——曾少一段，预览里看不到 m517。"""
        with open(os.path.join(REPO, 'tools', 'preview.sh')) as f:
            src = f.read()
        block = re.search(r'^SCENES=\((.*?)^\)', src, re.S | re.M).group(1)
        scenes = [l for l in block.splitlines() if '|' in l]
        self.assertTrue(scenes, '没找到 preview.sh 的场景表')
        for l in scenes:
            keys = [w.split('=', 1)[0] for w in l.split('|')[-1].strip().strip('"').split()]
            self.assertLessEqual(set(keys), set(FIELDS), l.strip())
            self.assertIn('msgs', keys, l.strip())


class ConfigProtocol(unittest.TestCase):
    """示例配置与脚本内置默认序列的契约：默认不冻结、注释里的顺序与 STEPS 一致。"""

    def test_example_has_no_active_steps_and_matches_default(self):
        with open(os.path.join(SRC, 'statusline-command.sh')) as f:
            src = f.read()
        block = re.search(r'^STEPS=\((.*?)^\)', src, re.S | re.M).group(1)
        default = re.findall(r'"(\w+) (\w+)"', block)
        with open(os.path.join(REPO, 'config', 'claude-statusline.conf.example')) as f:
            conf = f.read()
        active = re.findall(r'^\s*step\s*=\s*(\w+)\s+(\w+)', conf, re.M)
        self.assertEqual(active, [], '示例配置有 active step，会冻结用户拿到的默认序列')
        commented = re.findall(r'^\s*#\s*step\s*=\s*(\w+)\s+(\w+)', conf, re.M)
        self.assertEqual(commented, default, '注释里的默认序列与脚本 STEPS 不一致')
        ids = re.search(r'^ITEM_IDS="(.*?)"', src, re.S | re.M).group(1).split()
        self.assertEqual([i for _, i in default if i not in ids], [], 'STEPS 用了 ITEM_IDS 之外的项')


if __name__ == '__main__':
    unittest.main()
