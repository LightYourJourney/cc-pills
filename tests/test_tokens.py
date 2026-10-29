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

from _util import FIELDS, IDX, REPO, SRC

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
        return stdout.rstrip('\n').split('\t')

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

    def test_eighteen_fields(self):
        tp = self.write('d.jsonl', [line('m1', 1, 1, 'end_turn')])
        self.assertEqual(len(self.fields(self.run_tok(tp, 'c4').stdout)), len(FIELDS))

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
        ts = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
        tp = self.write('g.jsonl', [line('mold', 1000000, 0, 'end_turn', ts=ts(now - 86400 * 40)),
                                    line('mnew', 1000, 100, 'end_turn', ts=ts(now - 60))])
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

    def test_today_counts_headless_claude_not_codex(self):
        """今日按 app_type 统计：没渲染过状态栏的 Claude 会话（无头）也计入，Codex 不计。"""
        now = int(time.time())
        db = self.make_db(now)
        c = sqlite3.connect(db)
        c.execute("insert into proxy_request_logs values('h1', 'headless-sid', '_session', 'claude', 'session_log', "
                  "200, ?, 10, 1, 0, 0, '2.5')", (now - 120,))
        c.execute("insert into proxy_request_logs values('c1', 'codex-sid', 'codex-provider', 'codex', 'codex_session', "
                  "200, ?, 10, 1, 0, 0, '9')", (now - 120,))
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
        ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 60))
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
        c.execute("insert into proxy_request_logs values('a1','other','A','claude','proxy',200,?,0,0,0,0,'30')",
                  (now - 3600,))
        c.execute("insert into proxy_request_logs values('b1','sess','B','claude','proxy',200,?,0,0,0,0,'5')",
                  (now - 1800,))
        c.execute("insert into proxy_request_logs values('s1','direct','_session','claude','session_log',200,?,0,0,0,0,'7')",
                  (now - 900,))
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

    def test_limit_includes_rollup_excludes_tail(self):
        """日限额分子含 usage_daily_rollups；不含尚未入账的尾部（尾部将记为 _session）。"""
        now = int(time.time())
        db = self.provider_db(now, rollup=2.0)
        ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
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
        self.assertAlmostEqual(r['today_unattr'], 37.0, places=6)


# FIELDS 里各段在 bash 侧对应的变量名（顺序由 FIELDS 决定，见下一个用例）
BASH_NAMES = {'in': 't_in', 'out': 't_out', 'rate': 'p_rate', 'comp': 'comp',
              'q5lv': 'q5lv', 'q5out': 'q5out', 'q7lv': 'q7lv', 'q7out': 'q7out',
              'dlv': 'dlv', 'dout': 'dout', 'rcost': 'r_cost', 'runpriced': 'r_unpriced',
              'rtoday': 'r_today', 'rlimit': 'r_limit', 'hit': 't_hit',
              'splv': 'splv', 'spout': 'spout', 'msgs': 'msgs'}


class TokenProtocol(unittest.TestCase):
    """18 段 token 缓存的跨语言契约：生产者、bash 消费者与预览假数据必须同数同序。"""

    def test_preview_fake_tokens_match_protocol(self):
        """preview.sh 的假缓存必须同为 18 段、末段真的是消息数——曾停在 17 段，预览里看不到 m517。"""
        with open(os.path.join(REPO, 'tools', 'preview.sh')) as f:
            src = f.read()
        block = re.search(r'^SCENES=\((.*?)^\)', src, re.S | re.M).group(1)
        scenes = [l for l in block.splitlines() if '|' in l]
        self.assertTrue(scenes, '没找到 preview.sh 的场景表')
        for line in scenes:
            tok = line.split('|')[-1]
            self.assertEqual(tok.count('${T}') + 1, len(FIELDS), line.strip())
            last = tok.rsplit('${T}', 1)[-1].strip().strip('"')
            self.assertNotEqual(last, '-', f'末段应是消息数，不是占位：{line.strip()}')

    def test_bash_reads_fields_in_order(self):
        """bash 侧的 IFS=tab read 必须按 FIELDS 的顺序取变量，字段重排时这里先红。"""
        with open(os.path.join(SRC, 'statusline-command.sh')) as f:
            src = f.read()
        m = re.search(r"IFS=\$'\\t' read -r (t_in .*?) < \"\$\{SLT\}/claude-sl-tok-", src, re.S)
        self.assertIsNotNone(m, '没找到 bash 侧的 token 缓存 read 列表')
        got = re.sub(r'\\\s*\n', ' ', m.group(1)).split()
        self.assertEqual(got, [BASH_NAMES[f] for f in FIELDS], 'bash read 的字段顺序必须与 FIELDS 一致')


if __name__ == '__main__':
    unittest.main()
