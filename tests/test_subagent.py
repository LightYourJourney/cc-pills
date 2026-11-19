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
            a.wait()
            b.wait()
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
        self.spawn(st, 'a').wait()
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
        self.spawn(st, 'a').wait()
        self.assertFalse(os.path.exists(lock), '非空残锁没有被清除')
        with open(st) as f:
            self.assertIn('a', json.load(f))

    def test_live_stale_lock_is_not_stolen(self):
        """残锁里的 PID 还活着就不能抢：宁少一帧，也不与正在写的进程并发读改写。"""
        st = self.seeded()
        lock = self.hold_lock(st)
        self.spawn(st, 'a').wait()
        self.assertTrue(os.path.exists(lock), '持有者 PID 仍存活时锁被抢走了')
        with open(st) as f:
            self.assertNotIn('a', json.load(f), '没有拿到锁却写回了状态')
        self.assertEqual(self.reason(st), 'busy', '抢锁失败没有落 busy 标记')

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
                self.spawn(st, 'a', **kw).wait()
                self.assertEqual(self.reason(st), code)
                with open(st) as f:
                    self.assertNotIn('a', json.load(f), '失败的写回不该改动状态')
                self.assertFalse(os.path.exists(st + '.lock'), '失败后没有释放锁')

    def test_empty_stdin_is_not_a_write_failure(self):
        """手工运行（没有 stdin）没有可记的 task：不抢锁、不落失败标记。"""
        st = self.seeded()
        self.hold_lock(st)
        self.spawn(st, 'a', stdin='').wait()
        self.assertFalse(os.path.lexists(st + '.fail'), '空 stdin 被记成了写回失败')

    def test_successful_write_clears_fail_marker(self):
        """上一次失败的标记在成功写回后必须消失，否则诊断会一直报失败。"""
        st = self.seeded()
        with open(st + '.fail', 'w') as f:
            f.write(f'{int(time.time()) - 60} busy\n')
        self.spawn(st, 'a').wait()
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
                self.spawn(st, 'a').wait()
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
        self.spawn(st, 'a').wait()
        self.assertEqual(os.listdir(st + '.fail'), [])

    def test_lock_path_occupied_by_file_is_recovered(self):
        st = os.path.join(self.d, 's.json')
        lock = st + '.lock'
        with open(lock, 'w') as f:
            f.write('not a lock')
        self.spawn(st, 'a').wait()
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
        self.spawn(st, 'a').wait()
        self.assertFalse(os.path.lexists(lock), '被符号链接占住的锁路径没有被清除')
        self.assertTrue(os.path.isfile(os.path.join(victim, 'keep')),
                        '清锁跟随了符号链接，删掉了目标目录里的文件')
        with open(st) as f:
            self.assertIn('a', json.load(f))

    def test_state_file_is_owner_only(self):
        st = os.path.join(self.d, 's.json')
        self.spawn(st, 'a').wait()
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
                                 env=dict(self.env, CLAUDE_SUBAGENT_SL_STATE=st)).stdout
        self.assertIn('ctx 剩75%', out)

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
        self.assertLessEqual(len(p), 120)
        self.assertRegex(p, r'— opus-5-5 · high · tok 250k ↑[45]\.\dk/s · ctx 剩75% · 2m0[5-9]s$')
        q = rows['q']
        self.assertTrue(q.startswith('done  — '), q)
        self.assertIn('ctx 剩10%⚠', q)
        self.assertTrue(q.endswith(' [completed]'), q)
        self.assertNotRegex(q, r'\dm\d\ds', '非运行态不应显示时长')

    def test_default_state_path_is_per_user(self):
        tmp = os.path.join(self.d, 'T')
        os.makedirs(tmp, mode=0o700)
        # 空值等同上界未设置；TMPDIR 带尾斜杠也要归一成同一个私有根。
        self.spawn(os.path.join(tmp, 'unused.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp + os.sep}).wait()
        root = os.path.join(tmp, f'claude-sl-{os.getuid()}')
        want = os.path.join(root, f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), '默认状态路径没有按用户隔离')
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)

    def test_claude_sl_tmp_isolates_panel_state(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt)
        self.spawn(os.path.join(self.d, 'unused.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'CLAUDE_SL_TMP': slt}).wait()
        want = os.path.join(slt, f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), 'CLAUDE_SL_TMP 没有同时隔离面板状态')

    def test_explicit_state_file_wins(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt)
        st = os.path.join(self.d, 'own.json')
        self.spawn(st, 'a', env={'CLAUDE_SL_TMP': slt,
                                 'CLAUDE_SUBAGENT_SL_STATE': st}).wait()
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
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp}).wait()
        self.assertFalse(os.path.exists(
            os.path.join(target, f'claude-subagent-sl-{os.getuid()}.json')),
            '状态被写进了符号链接指向的目录')

    def test_cache_path_with_spaces_and_unicode(self):
        tmp = os.path.join(self.d, 'T 空 格')
        os.makedirs(tmp, mode=0o700)
        self.spawn(os.path.join(self.d, 'in.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp}).wait()
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
        self.assertEqual(f, {'cc-pills-facts': '2', 'state_explicit': '0',
                             'state': os.path.join(root, f'claude-subagent-sl-{os.getuid()}.json')})
        self.assertEqual(os.listdir(tmp), [], '自述模式建了缓存目录')
        slt = os.path.join(self.d, 'slt')
        self.assertEqual(self.panel_facts({'CLAUDE_SL_TMP': slt})['state'],
                         os.path.join(slt, f'claude-subagent-sl-{os.getuid()}.json'))
        f = self.panel_facts({'CLAUDE_SUBAGENT_SL_STATE': '/x/y.json', 'CLAUDE_SL_TMP': slt})
        self.assertEqual((f['state'], f['state_explicit']), ('/x/y.json', '1'))


if __name__ == '__main__':
    unittest.main()
