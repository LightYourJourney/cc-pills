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

    def test_concurrent_writes_do_not_lose_entries(self):
        lost = 0
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
            lost += not ('a' in d and 'b' in d and 'seed' in d)
        self.assertEqual(lost, 0, f'{lost}/30 次并发写入丢失了另一实例的条目')

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

    def test_state_file_is_owner_only(self):
        st = os.path.join(self.d, 's.json')
        self.spawn(st, 'a').wait()
        mode = stat.S_IMODE(os.stat(st).st_mode)
        self.assertEqual(mode, 0o600, f'状态文件模式是 {oct(mode)}，应为 0600')

    def test_default_state_path_is_per_user(self):
        tmp = os.path.join(self.d, 'T')
        os.makedirs(tmp, mode=0o700)
        self.spawn(os.path.join(tmp, 'unused.json'), 'a',
                   env={'CLAUDE_SUBAGENT_SL_STATE': '', 'TMPDIR': tmp}).wait()
        # 空值等同上界未设置：脚本应回落到 ${TMPDIR}/claude-subagent-sl-<uid>.json
        want = os.path.join(tmp, f'claude-subagent-sl-{os.getuid()}.json')
        self.assertTrue(os.path.isfile(want), '默认状态路径没有按用户隔离')


if __name__ == '__main__':
    unittest.main()
