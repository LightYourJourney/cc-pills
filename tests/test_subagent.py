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

    def spawn(self, state, ident, env=None):
        p = os.path.join(self.d, f'in-{ident}.json')
        with open(p, 'w') as f:
            f.write(task(ident))
        e = dict(self.env, CLAUDE_SUBAGENT_SL_STATE=state)
        e.update(env or {})
        with open(p) as f:
            return subprocess.Popen(['bash', SCRIPT], stdin=f,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=e)

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
        st = os.path.join(self.d, 's.json')
        with open(st, 'w') as f:
            json.dump({'seed': {'t': time.time(), 'k': 1}}, f)
        lock = st + '.lock'
        os.makedirs(lock)
        with open(os.path.join(lock, 'owner'), 'w') as f:
            f.write(f'{os.getpid()}.1')
        old = time.time() - 30
        os.utime(lock, (old, old))
        self.spawn(st, 'a').wait()
        self.assertTrue(os.path.exists(lock), '持有者 PID 仍存活时锁被抢走了')
        with open(st) as f:
            self.assertNotIn('a', json.load(f), '没有拿到锁却写回了状态')

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


if __name__ == '__main__':
    unittest.main()
