"""Subagent 状态行：共享状态的并发写、残锁恢复与文件权限。

状态文件被本机所有会话共享。两个实例同时渲染时必须合并条目而不是后写覆盖先写；
进程被杀留下的锁要在超时后清除；状态文件只允许本人读写。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from _util import run_facts

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'src', 'subagent-statusline.sh')


NARROW = {0x2026, 0x2191, 0x2193, 0x21BA, 0x21BB, 0x21E1, 0x21E3}


def w(s):
    """终端显示宽度：与脚本的 dw 同一模型（≤ 0x7FF 的码点占 1 列，3/4 字节字符占 2 列，
    已知按 1 列显示的 3 字节符号除外）。"""
    return sum(1 if ord(c) <= 0x7FF or ord(c) in NARROW else 2 for c in s)


def task(i):
    return json.dumps({'columns': 120, 'tasks': [{
        'id': i, 'name': 'agent', 'status': 'running', 'description': 'work',
        'label': 'work', 'startTime': 0, 'model': 'm',
        'tokenCount': 1000, 'contextWindowSize': 1000000}]})


class SubagentState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='ccpills-sub-')
        self.env = dict(os.environ)
        self.env.pop('CLAUDE_SUBAGENT_SL_STATE', None)
        self.env.pop('CLAUDE_SL_TMP', None)      # 默认路径用例不应被外层隔离变量干扰

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def spawn(self, state, ident, env=None, stdin=None):
        p = os.path.join(self.d, f'in-{ident}.json')
        with open(p, 'w') as f:
            f.write(task(ident) if stdin is None else stdin)
        e = dict(self.env, CLAUDE_SUBAGENT_SL_STATE=state)
        e.update(env or {})
        with open(p) as f:
            return subprocess.Popen(['bash', SCRIPT], stdin=f,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=e)

    def seeded(self):
        """建一份已有条目的状态文件，返回路径。"""
        st = os.path.join(self.d, 's.json')
        with open(st, 'w') as f:
            json.dump({'seed': {'t': time.time(), 'k': 1}}, f)
        return st

    def hold_lock(self, st):
        """超龄但持有者（本测试进程）仍存活的锁：抢锁方只能放弃，落 busy。"""
        lock = st + '.lock'
        os.makedirs(lock)
        with open(os.path.join(lock, 'owner'), 'w') as f:
            f.write(f'{os.getpid()}.1')
        old = time.time() - 30
        os.utime(lock, (old, old))
        return lock

    def reason(self, st):
        with open(st + '.fail') as f:
            return f.read().split()[1]

    def shim(self, name, body):
        """在一个新的临时 bin 里放同名假命令（body 里用 $REAL 调真命令），返回带它的 PATH。"""
        b = tempfile.mkdtemp(dir=self.d, prefix='bin-')
        p = os.path.join(b, name)
        with open(p, 'w') as f:
            f.write(f'#!/bin/sh\nREAL={shutil.which(name)}\n{body}\n')
        os.chmod(p, 0o755)
        return {'PATH': b + os.pathsep + os.environ.get('PATH', '')}

    @unittest.skipIf(os.environ.get('CCPILLS_SKIP_CONCURRENCY') == '1',
                     'Linux CI runner 上两个独立进程能在 2µs 内同时 mkdir 同一条路径成功'
                     '（跨进程目录可见性异常，见 docs/DECISIONS.md），锁语义无法在此验证')
    def test_concurrent_writes_do_not_lose_entries(self):
        lost = []
        for n in range(30):
            st = os.path.join(self.d, f's{n}.json')
            with open(st, 'w') as f:
                json.dump({'seed': {'t': time.time(), 'k': 1}}, f)
            a = self.spawn(st, 'a')
            b = self.spawn(st, 'b')
            a.wait(timeout=60)
            b.wait(timeout=60)
            with open(st) as f:
                d = json.load(f)
            if not ('a' in d and 'b' in d and 'seed' in d):
                # 失败现场：留下了哪个条目、锁是否残留
                lost.append({'round': n, 'keys': sorted(d),
                             'lock': os.path.exists(st + '.lock')})
        self.assertEqual(lost, [], f'{len(lost)}/30 次并发写入丢失了另一实例的条目：{lost}')

    def test_stale_lock_is_recovered(self):
        st = os.path.join(self.d, 's.json')
        with open(st, 'w') as f:
            json.dump({'seed': {'t': time.time(), 'k': 1}}, f)
        lock = st + '.lock'
        os.makedirs(lock)
        old = time.time() - 10
        os.utime(lock, (old, old))
        self.spawn(st, 'a').wait(timeout=60)
        self.assertFalse(os.path.exists(lock), '超过 5s 的残锁没有被清除')
        with open(st) as f:
            d = json.load(f)
        self.assertIn('a', d)
        self.assertIn('seed', d)

    def test_non_empty_stale_lock_is_recovered(self):
        """锁目录里被塞了文件时 rmdir 永远失败：超时后必须连内容一起清掉。"""
        st = os.path.join(self.d, 's.json')
        lock = st + '.lock'
        os.makedirs(lock)
        with open(os.path.join(lock, 'junk'), 'w') as f:
            f.write('x')
        old = time.time() - 10
        os.utime(lock, (old, old))
        self.spawn(st, 'a').wait(timeout=60)
        self.assertFalse(os.path.exists(lock), '非空残锁没有被清除')
        with open(st) as f:
            self.assertIn('a', json.load(f))

    def test_live_stale_lock_is_not_stolen(self):
        """残锁里的 PID 还活着就不能抢：宁少一帧，也不与正在写的进程并发读改写。"""
        st = self.seeded()
        lock = self.hold_lock(st)
        self.spawn(st, 'a').wait(timeout=60)
        self.assertTrue(os.path.exists(lock), '持有者 PID 仍存活时锁被抢走了')
        with open(st) as f:
            self.assertNotIn('a', json.load(f), '没有拿到锁却写回了状态')
        self.assertEqual(self.reason(st), 'busy', '抢锁失败没有落 busy 标记')

    def test_fresh_lock_without_owner_is_not_stolen(self):
        """刚建（不足 5 秒）、还没来得及写 owner 的锁属于另一个正在起步的实例：不能当残锁回收，
        否则两个实例会同时读改写。抢锁方放弃本次写回、记 busy，锁目录原样留着。"""
        st = self.seeded()
        lock = st + '.lock'
        os.makedirs(lock)
        self.spawn(st, 'a').wait(timeout=60)
        self.assertTrue(os.path.isdir(lock), '不足 5 秒的锁被当残锁回收了')
        self.assertEqual(os.listdir(lock), [], '别人的锁里被写了东西')
        with open(st) as f:
            self.assertNotIn('a', json.load(f), '没有拿到锁却写回了状态')
        self.assertEqual(self.reason(st), 'busy')

    def test_release_only_removes_own_lock(self):
        """释放锁前核对 owner：写回期间锁被别人接管（owner 变了）时，退出不能把别人的锁删掉。
        用假 jq 在合并那一步把 owner 改写成别人的 token。"""
        st = self.seeded()
        lock = st + '.lock'
        env = self.shim('jq', 'case "$*" in *with_entries*) printf %s intruder.1 > "' + lock +
                        '/owner" ;; esac; exec "$REAL" "$@"')
        self.spawn(st, 'a', env=env).wait(timeout=60)
        with open(st) as f:
            self.assertIn('a', json.load(f), '对照失效：写回本身应当成功')
        self.assertTrue(os.path.isdir(lock), '释放锁删掉了别人的锁')
        with open(os.path.join(lock, 'owner')) as f:
            self.assertEqual(f.read(), 'intruder.1')

    def test_write_back_prunes_entries_older_than_600s(self):
        """写回时剪掉 600 秒前的条目：本会话已不再出现的 subagent 不能永远留在共享状态里；
        没到 600 秒的照留。"""
        st = os.path.join(self.d, 's.json')
        now = time.time()
        with open(st, 'w') as f:
            json.dump({'old': {'t': now - 700, 'k': 1}, 'recent': {'t': now - 500, 'k': 1}}, f)
        self.spawn(st, 'a').wait(timeout=60)
        with open(st) as f:
            d = json.load(f)
        self.assertEqual(sorted(d), ['a', 'recent'], '700 秒前的条目没有被剪掉（或 500 秒前的被误剪）')

    def test_entry_is_not_refreshed_within_2s(self):
        """同一条目 2 秒内再次写回不改 t / k：事件驱动的密集重跑不能冲掉速率基线；满 2 秒才更新。"""
        st = os.path.join(self.d, 's.json')
        now = time.time()
        with open(st, 'w') as f:
            json.dump({'a': {'t': now, 'k': 7}, 'b': {'t': now - 3, 'k': 7}}, f)
        self.spawn(st, 'a').wait(timeout=60)
        self.spawn(st, 'b').wait(timeout=60)
        with open(st) as f:
            d = json.load(f)
        self.assertEqual(d['a']['k'], 7, '2 秒内的再次写回改了速率基线的 k')
        self.assertAlmostEqual(d['a']['t'], now, places=3)
        self.assertEqual(d['b']['k'], 1000, '满 2 秒的条目没有更新')
        self.assertGreater(d['b']['t'], now - 1)

    def test_write_back_keeps_running_pending_and_empty_status_only(self):
        """写回只收运行中的行：running、pending、空串、缺 status（按 running）；已结束的不记
        速率基线。"""
        st = os.path.join(self.d, 's.json')
        tasks = {'columns': 120, 'tasks': [
            {'id': 'run', 'status': 'running'}, {'id': 'pend', 'status': 'pending'},
            {'id': 'empty', 'status': ''}, {'id': 'none'},
            {'id': 'done', 'status': 'completed'}, {'id': 'bad', 'status': 'failed'}]}
        self.panel(tasks, st)
        with open(st) as f:
            self.assertEqual(sorted(json.load(f)), ['empty', 'none', 'pend', 'run'])

    def test_write_failures_record_their_reason(self):
        """合并 / 建临时文件 / 写入 / 替换失败各落自己的原因码（假命令制造失败点）。"""
        cases = {
            'merge': dict(stdin='not json'),
            'tmp': dict(env=self.shim(
                'mktemp', 'case "$1" in *.claude-sl-panel.*) exit 1 ;; esac; exec "$REAL" "$@"')),
            # 临时文件「建成」了却是个目录：重定向写入必然失败（root 也一样）
            'write': dict(env=self.shim(
                'mktemp', 'case "$1" in *.claude-sl-panel.*) mkdir "${1%.XXXXXX}.d" && '
                          'echo "${1%.XXXXXX}.d"; exit ;; esac; exec "$REAL" "$@"')),
            'mv': dict(env=self.shim(
                'mv', 'for a; do :; done; case "$a" in *s.json) exit 1 ;; esac; exec "$REAL" "$@"')),
        }
        for code, kw in cases.items():
            with self.subTest(code=code):
                st = self.seeded()
                if os.path.lexists(st + '.fail'):
                    os.unlink(st + '.fail')
                self.spawn(st, 'a', **kw).wait(timeout=60)
                self.assertEqual(self.reason(st), code)
                with open(st) as f:
                    self.assertNotIn('a', json.load(f), '失败的写回不该改动状态')
                self.assertFalse(os.path.exists(st + '.lock'), '失败后没有释放锁')

    def test_empty_stdin_is_not_a_write_failure(self):
        """手工运行（没有 stdin）没有可记的 task：不抢锁、不落失败标记。"""
        st = self.seeded()
        self.hold_lock(st)
        self.spawn(st, 'a', stdin='').wait(timeout=60)
        self.assertFalse(os.path.lexists(st + '.fail'), '空 stdin 被记成了写回失败')

    def test_successful_write_clears_fail_marker(self):
        """上一次失败的标记在成功写回后必须消失，否则诊断会一直报失败。"""
        st = self.seeded()
        with open(st + '.fail', 'w') as f:
            f.write(f'{int(time.time()) - 60} busy\n')
        self.spawn(st, 'a').wait(timeout=60)
        self.assertFalse(os.path.lexists(st + '.fail'), '成功写回后失败标记没有被清除')

    def test_fail_marker_symlink_is_not_followed(self):
        """预置的符号链接标记：换成真标记，链接目标（文件或目录）一个字节都不能动。"""
        for kind in ('file', 'dir'):
            with self.subTest(target=kind):
                st = self.seeded()
                victim = os.path.join(self.d, f'victim-{kind}')
                if kind == 'file':
                    with open(victim, 'w') as f:
                        f.write('keep me')
                else:
                    os.makedirs(victim)
                os.symlink(victim, st + '.fail')
                lock = self.hold_lock(st)
                self.spawn(st, 'a').wait(timeout=60)
                shutil.rmtree(lock)
                if kind == 'file':
                    with open(victim) as f:
                        self.assertEqual(f.read(), 'keep me', '写标记改掉了链接目标')
                else:
                    self.assertEqual(os.listdir(victim), [], '标记被写进了链接指向的目录')
                self.assertFalse(os.path.islink(st + '.fail'), '符号链接标记没有被换掉')
                self.assertEqual(self.reason(st), 'busy')
                os.unlink(st + '.fail')

    def test_fail_marker_directory_is_left_alone(self):
        """标记路径是个目录：不是我们的东西，不往里塞文件（诊断会报它）。"""
        st = self.seeded()
        os.makedirs(st + '.fail')
        self.hold_lock(st)
        self.spawn(st, 'a').wait(timeout=60)
        self.assertEqual(os.listdir(st + '.fail'), [])

    def test_lock_path_occupied_by_file_is_recovered(self):
        st = os.path.join(self.d, 's.json')
        lock = st + '.lock'
        with open(lock, 'w') as f:
            f.write('not a lock')
        self.spawn(st, 'a').wait(timeout=60)
        self.assertFalse(os.path.exists(lock), '被普通文件占住的锁路径没有被清除')
        with open(st) as f:
            self.assertIn('a', json.load(f))

    def test_lock_path_occupied_by_symlink_is_recovered(self):
        st = os.path.join(self.d, 's.json')
        lock = st + '.lock'
        victim = os.path.join(self.d, 'victim')
        os.makedirs(victim)
        with open(os.path.join(victim, 'keep'), 'w') as f:
            f.write('x')
        os.symlink(victim, lock)
        self.spawn(st, 'a').wait(timeout=60)
        self.assertFalse(os.path.lexists(lock), '被符号链接占住的锁路径没有被清除')
        self.assertTrue(os.path.isfile(os.path.join(victim, 'keep')),
                        '清锁跟随了符号链接，删掉了目标目录里的文件')
        with open(st) as f:
            self.assertIn('a', json.load(f))

    def test_state_file_is_owner_only(self):
        st = os.path.join(self.d, 's.json')
        self.spawn(st, 'a').wait(timeout=60)
        mode = stat.S_IMODE(os.stat(st).st_mode)
        self.assertEqual(mode, 0o600, f'状态文件模式是 {oct(mode)}，应为 0600')

    def test_ctx_label_is_remaining(self):
        """主栏 ctx 是已用；面板数值是剩余——必须显式标「剩」，否则同名字会误读。"""
        p = os.path.join(self.d, 'in-ctx.json')
        with open(p, 'w') as f:
            json.dump({'columns': 120, 'tasks': [{
                'id': 'c', 'status': 'running', 'description': 'd', 'label': 'd',
                'model': 'm', 'tokenCount': 250000, 'contextWindowSize': 1000000}]}, f)
        st = os.path.join(self.d, 'ctx.json')
        with open(p) as f:
            out = subprocess.run(['bash', SCRIPT], stdin=f, capture_output=True, text=True,
                                 env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st), timeout=60).stdout
        self.assertIn('ctx 剩75%', out)

    def panel(self, tasks, st):
        """跑一次面板脚本（状态文件 st），返回输出的各行（已解析）。"""
        r = subprocess.run(['bash', SCRIPT], input=json.dumps(tasks), capture_output=True, text=True,
                           env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st), timeout=60)
        return [json.loads(l) for l in r.stdout.splitlines()]

    def test_malformed_task_only_drops_that_row(self):
        """一个 task 形状不对（id 不是字符串、tokenCount 是字符串、description / model 是数字、task
        本身不是对象）只让那一行回到默认渲染，前后的行照常输出、状态照常写回。此前 $prev[数字]
        或任何一处类型错误都让整个 jq 中止：坏行之后全部丢失，写回记成 merge 失败。id 不是字符串
        的那行不写状态。"""
        st = os.path.join(self.d, 'bad.json')
        ok = lambda i: {'id': i, 'status': 'running', 'description': f'd{i}', 'model': 'm',
                        'tokenCount': 1000, 'contextWindowSize': 1000000}
        tasks = {'columns': 120, 'tasks': [
            ok('a'),
            ok(7),
            dict(ok('b'), tokenCount='2000'),
            dict(ok('c'), description=42),
            dict(ok('d'), model=5),
            5,
            ok('e')]}
        self.assertEqual([row['id'] for row in self.panel(tasks, st)], ['a', 'e'])
        with open(st) as f:
            self.assertEqual(sorted(json.load(f)), ['a', 'b', 'c', 'd', 'e'])
        self.assertFalse(os.path.exists(st + '.fail'), '写回被记成失败')

    def test_multi_value_state_is_treated_as_empty(self):
        """状态文件里有多个 JSON 值（「{} {}」）能骗过只看最后一个输出的 type 校验，随后
        --argjson 拒收多值：此前每轮零输出、记 merge，状态永不修复。stdin 违约给了两个对象时，
        多行的合并结果也不能落盘（下一轮就是这种坏状态）。"""
        st = os.path.join(self.d, 'mv.json')
        with open(st, 'w') as f:
            f.write('{} {}')
        tasks = {'columns': 120, 'tasks': [{'id': 'a', 'status': 'running', 'description': 'work'}]}
        self.assertEqual([row['id'] for row in self.panel(tasks, st)], ['a'])
        with open(st) as f:
            self.assertEqual(sorted(json.load(f)), ['a'])
        self.assertFalse(os.path.exists(st + '.fail'))
        two = json.dumps(tasks) + json.dumps({'tasks': [{'id': 'b', 'status': 'running'}]})
        subprocess.run(['bash', SCRIPT], input=two, capture_output=True, text=True,
                       env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st), timeout=60)
        with open(st) as f:
            json.load(f)                                  # 仍是单个 JSON 值

    def test_non_numeric_columns_falls_back_to_default_width(self):
        """columns 不是数字时按默认宽度 120 渲染，而不是一行都不出。"""
        tasks = {'columns': '100', 'tasks': [{'id': 'a', 'status': 'running', 'description': 'work'}]}
        rows = self.panel(tasks, os.path.join(self.d, 'c.json'))
        self.assertEqual([row['content'] for row in rows], ['work'])

    def test_panel_line_fields(self):
        """面板行：名字、按宽度截断的描述、模型 / effort / tok 与增速 / ctx 剩余 / 时长，非运行态
        带 [状态] 且不计时长，剩余 < 20% 带 ⚠。"""
        now = time.time()
        st = os.path.join(self.d, 'f.json')
        with open(st, 'w') as f:
            json.dump({'p': {'t': now - 10, 'k': 200000}}, f)
        tasks = {'columns': 120, 'tasks': [
            {'id': 'p', 'name': 'reviewer', 'status': 'running', 'description': '审查' * 100,
             'model': 'claude-opus-5-5[1m]', 'effort': 'high', 'startTime': (now - 125) * 1000,
             'tokenCount': 250000, 'contextWindowSize': 1000000},
            {'id': 'q', 'name': 'agent', 'status': 'completed', 'description': 'done',
             'model': 'm', 'startTime': (now - 125) * 1000, 'tokenCount': 900000,
             'contextWindowSize': 1000000}]}
        out = subprocess.run(['bash', SCRIPT], input=json.dumps(tasks), capture_output=True, text=True,
                             env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st), timeout=60).stdout
        rows = {r['id']: r['content'] for r in map(json.loads, out.splitlines())}
        p = rows['p']
        self.assertTrue(p.startswith('reviewer 审查'), p)
        self.assertIn('…', p, '超宽描述没有截断')
        self.assertLessEqual(w(p), 120, '行宽按码点算会超宽')
        self.assertRegex(p, r'— opus-5-5 · high · tok 250k ↑[45]\.\dk/s · ctx 剩75% · 2m0[5-9]s$')
        q = rows['q']
        self.assertTrue(q.startswith('done  — '), q)
        self.assertIn('ctx 剩10%⚠', q)
        self.assertTrue(q.endswith(' [completed]'), q)
        self.assertNotRegex(q, r'\dm\d\ds', '非运行态不应显示时长')

    def test_cjk_description_fits_the_row(self):
        """中文描述按显示宽度截断。码点数只有实际列数的一半，按码点分预算会让整行超宽，
        面板于是把行尾的 tok / ctx 剩余 / 时长裁掉（80 列实测超 29 列）。"""
        st = os.path.join(self.d, 'w.json')
        for cols in (60, 80, 120):
            tasks = {'columns': cols, 'tasks': [
                {'id': 'c', 'name': 'reviewer', 'status': 'running',
                 'description': '审查这个模块的并发写与残锁恢复逻辑' * 10,
                 'model': 'claude-opus-5-5', 'effort': 'high',
                 'startTime': (time.time() - 125) * 1000,
                 'tokenCount': 250000, 'contextWindowSize': 1000000}]}
            out = subprocess.run(['bash', SCRIPT], input=json.dumps(tasks), capture_output=True,
                                 text=True, env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st),
                                 timeout=60).stdout
            row = json.loads(out.splitlines()[0])['content']
            self.assertLessEqual(w(row), cols, f'{cols} 列时超宽：{row!r}')
            self.assertIn('ctx 剩75%', row, '行尾的 ctx 段不见了')

    def test_combining_marks_do_not_double_count(self):
        """2 字节的拉丁重音字符（é、ü）在终端里占 1 列，不能按宽字符算成 2。"""
        st = os.path.join(self.d, 'e.json')
        tasks = {'columns': 100, 'tasks': [
            {'id': 'e', 'status': 'running', 'description': 'café Zürich ' * 20,
             'model': 'm', 'tokenCount': 1000, 'contextWindowSize': 1000000}]}
        out = subprocess.run(['bash', SCRIPT], input=json.dumps(tasks), capture_output=True,
                             text=True, env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st),
                             timeout=60).stdout
        row = json.loads(out.splitlines()[0])['content']
        self.assertLessEqual(w(row), 100)
        self.assertGreaterEqual(w(row), 94, '把 2 字节字符按宽字符算，行没填满就提前截断了')

    def test_default_state_path_is_per_user(self):
        tmp = os.path.join(self.d, 'T')
        os.makedirs(tmp, mode=0o700)
        # 空值等同上界未设置；TMPDIR 带尾斜杠也要归一成同一个私有根。
        self.spawn(os.path.join(tmp, 'unused.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp + os.sep}).wait(timeout=60)
        root = os.path.join(tmp, f'claude-sl-{os.getuid()}')
        want = os.path.join(root, f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), '默认状态路径没有按用户隔离')
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)

    def test_claude_sl_tmp_isolates_panel_state(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt)
        self.spawn(os.path.join(self.d, 'unused.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'CLAUDE_SL_TMP': slt}).wait(timeout=60)
        want = os.path.join(slt, f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), 'CLAUDE_SL_TMP 没有同时隔离面板状态')

    def test_explicit_state_file_wins(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt)
        st = os.path.join(self.d, 'own.json')
        self.spawn(st, 'a', env={'CLAUDE_SL_TMP': slt,
                                 'CLAUDE_SUBAGENT_SL_STATE': st}).wait(timeout=60)
        self.assertTrue(os.path.isfile(st))
        self.assertFalse(os.path.exists(
            os.path.join(slt, f'claude-subagent-sl-{os.getuid()}.json')),
            '显式 CLAUDE_SUBAGENT_SL_STATE 应压过 CLAUDE_SL_TMP')

    def test_symlink_cache_root_is_not_used(self):
        tmp = os.path.join(self.d, 'T')
        target = os.path.join(self.d, 'target')
        os.makedirs(tmp, mode=0o700)
        os.makedirs(target)
        os.symlink(target, os.path.join(tmp, f'claude-sl-{os.getuid()}'))
        self.spawn(os.path.join(self.d, 'in.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp}).wait(timeout=60)
        self.assertFalse(os.path.exists(
            os.path.join(target, f'claude-subagent-sl-{os.getuid()}.json')),
            '状态被写进了符号链接指向的目录')

    def test_cache_path_with_spaces_and_unicode(self):
        tmp = os.path.join(self.d, 'T 空 格')
        os.makedirs(tmp, mode=0o700)
        self.spawn(os.path.join(self.d, 'in.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp}).wait(timeout=60)
        want = os.path.join(tmp, f'claude-sl-{os.getuid()}',
                            f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), '含空格 / 中文的缓存路径不可用')

    def panel_facts(self, env):
        return run_facts(SCRIPT, dict(self.env, **env))[0]

    def test_facts_report_state_path_without_touching_disk(self):
        """--facts：面板自述状态文件路径给诊断工具；只读，不建缓存目录。"""
        tmp = os.path.join(self.d, 'T')
        os.makedirs(tmp)
        f = self.panel_facts({'TMPDIR': tmp + os.sep})
        root = os.path.join(tmp, f'claude-sl-{os.getuid()}')
        self.assertEqual(f, {'cc-pills-facts': '3', 'state_explicit': '0',
                             'state': os.path.join(root, f'claude-subagent-sl-{os.getuid()}.json')})
        self.assertEqual(os.listdir(tmp), [], '自述模式建了缓存目录')
        slt = os.path.join(self.d, 'slt')
        self.assertEqual(self.panel_facts({'CLAUDE_SL_TMP': slt})['state'],
                         os.path.join(slt, f'claude-subagent-sl-{os.getuid()}.json'))
        f = self.panel_facts({'CLAUDE_SUBAGENT_SL_STATE': '/x/y.json', 'CLAUDE_SL_TMP': slt})
        self.assertEqual((f['state'], f['state_explicit']), ('/x/y.json', '1'))


if __name__ == '__main__':
    unittest.main()
