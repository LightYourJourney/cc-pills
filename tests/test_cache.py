"""缓存目录隔离、权限与缓存内容安全。

默认缓存目录必须是每用户私有（0700），文件只允许本人读写（0600）；读取前还要确认
所有者是当前进程（跨用户预置的文件一律忽略）。token / theme 缓存来自磁盘，按不可信
数据对待：控制字符不得进入状态栏输出。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from _util import SCRIPT, tok_line


class CacheBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='ccpills-cache-')
        self.tmpdir = os.path.join(self.d, 'T')      # 模拟 TMPDIR 私有根
        os.makedirs(self.tmpdir, mode=0o700)
        self.cfg = os.path.join(self.d, 'cfg')
        os.makedirs(self.cfg)
        with open(os.path.join(self.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('theme = dracula\nstatus_page = off\n')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CLAUDE_SL_TMP', 'CLAUDE_SL_NO_REFRESH', 'TERM_PROGRAM',
                                 'LC_TERMINAL', 'ITERM_PROFILE', 'ANTHROPIC_BASE_URL',
                                 'CLAUDE_CONFIG_DIR')}
        self.env.update(TMPDIR=self.tmpdir, XDG_CONFIG_HOME=self.cfg, HOME=self.d,
                        COLORTERM='truecolor', TERM='xterm-256color')

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def private(self):
        return os.path.join(self.tmpdir, f'claude-sl-{os.getuid()}')

    def render(self, sid='cachetest', env=None, cols=200):
        data = json.dumps({
            'session_id': sid, 'cwd': self.d, 'model': {'display_name': 'Opus 5.5'},
            'context_window': {'used_percentage': 42, 'total_input_tokens': 419000,
                               'context_window_size': 1000000},
            'cost': {'total_cost_usd': 17.54, 'total_api_duration_ms': 1000}})
        e = dict(self.env, COLUMNS=str(cols), CLAUDE_SL_NO_REFRESH='1', **(env or {}))
        return subprocess.run(['bash', SCRIPT], input=data, capture_output=True,
                              text=True, env=e).stdout

    def refresh(self, env):
        return subprocess.run(['bash', SCRIPT, '--refresh', self.d, '-', 'cachetest', 'sub'],
                              capture_output=True, env=env)


class DefaultDir(CacheBase):
    def test_default_dir_is_per_user_private(self):
        """没有 CLAUDE_SL_TMP 时只读 $TMPDIR/claude-sl-<uid>，不读 /tmp 的旧位置。"""
        p = self.private()
        os.makedirs(p, mode=0o700)
        with open(os.path.join(p, 'claude-sl-tok-cachetest.cache'), 'w') as f:
            f.write(tok_line(**{'in': '777M'}, msgs='999'))
        out = self.render()
        self.assertIn('↑777M', out, '默认缓存目录不是每用户私有目录')
        self.assertIn('m999', out)

    def test_refresh_creates_private_dir(self):
        self.refresh(self.env)
        p = self.private()
        self.assertTrue(os.path.isdir(p), '后台刷新没有创建私有缓存目录')
        st = os.stat(p)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        self.assertEqual(st.st_uid, os.getuid())

    def test_legacy_tmp_files_are_migrated(self):
        """旧版写在共享 /tmp 的本会话文件要迁入私有目录（唯一名字，跑完清理）。"""
        sid = f'ccpills-mig-{os.getpid()}-{time.time_ns()}'
        names = (f'claude-sl-tok-{sid}.cache', f'claude-sl-skip-{sid}',
                 f'claude-statusline-{sid}.json')
        paths = [os.path.join('/tmp', n) for n in names]

        def cleanup():
            for p in paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.addCleanup(cleanup)
        with open(paths[0], 'w') as f:
            f.write(tok_line(**{'in': '30M'}, msgs='1'))
        with open(paths[1], 'w') as f:
            f.write('sig\t1\n')
        with open(paths[2], 'w') as f:
            f.write('{"v":9}')
        r = subprocess.run(['bash', SCRIPT, '--refresh', self.d, '-', sid, 'sub'],
                           env=self.env, capture_output=True)
        self.assertEqual(r.returncode, 0)
        p = self.private()
        for n in names:
            self.assertTrue(os.path.isfile(os.path.join(p, n)), f'{n} 未迁移')
            self.assertFalse(os.path.exists(os.path.join('/tmp', n)), f'/tmp/{n} 仍在')
        # 重复迁移要幂等：第二次刷新既不能把文件搬回去，也不能覆盖已迁移的内容
        def read(n):
            with open(os.path.join(p, n)) as f:
                return f.read()
        before = {n: read(n) for n in names}
        r = subprocess.run(['bash', SCRIPT, '--refresh', self.d, '-', sid, 'sub'],
                           env=self.env, capture_output=True)
        self.assertEqual(r.returncode, 0)
        for n in names:
            self.assertTrue(os.path.isfile(os.path.join(p, n)), f'重复刷新后 {n} 消失')
            self.assertFalse(os.path.exists(os.path.join('/tmp', n)), f'重复刷新把 {n} 搬回了 /tmp')
        self.assertEqual(read(names[1]), before[names[1]], '重复迁移覆盖了已迁移的跳过标记')


class Modes(CacheBase):
    def test_cache_files_are_owner_only(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        r = self.refresh(dict(self.env, CLAUDE_SL_TMP=slt))
        self.assertEqual(r.returncode, 0)
        files = [f for f in os.listdir(slt) if os.path.isfile(os.path.join(slt, f))]
        self.assertTrue(files, '刷新没有写出任何缓存')
        for f in files:
            mode = stat.S_IMODE(os.stat(os.path.join(slt, f)).st_mode)
            self.assertEqual(mode, 0o600, f'{f} 的模式是 {oct(mode)}，应为 0600')


class ContentSafety(CacheBase):
    def test_token_cache_cannot_inject_escapes(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        with open(os.path.join(slt, 'claude-sl-tok-inj.cache'), 'w') as f:
            f.write(tok_line(**{'in': '30M\x1b]52;c;SGVsbG8=\x07', 'out': '290k',
                                'rate': '$4.9/h', 'hit': '97.4', 'msgs': '1'}))
        out = self.render('inj', {'CLAUDE_SL_TMP': slt})
        self.assertNotIn('\x1b]52;', out, '缓存内容里的 OSC 序列进入了状态栏')
        self.assertIn('↑30M', out, '合法内容不应被整段丢弃')

    def test_cache_owned_by_other_user_is_ignored(self):
        """用 root 拥有的 /etc/passwd 做符号链接目标，验证 -O 跨用户拒读。"""
        if not os.path.exists('/etc/passwd'):
            self.skipTest('/etc/passwd 不存在')
        if os.stat('/etc/passwd').st_uid == os.getuid():
            self.skipTest('以 root 运行，无法构造跨用户场景')
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        os.symlink('/etc/passwd', os.path.join(slt, 'claude-sl-tok-own.cache'))
        out = self.render('own', {'CLAUDE_SL_TMP': slt})
        self.assertIn('ctx 42%', out)
        self.assertNotIn('↑', out, '别人拥有的同名缓存被当成 token 缓存读取了')

    def test_theme_cache_rejects_non_numeric_values(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        with open(os.path.join(slt, 'claude-sl-theme-term-other.cache'), 'w') as f:
            f.write('THEME=dracula/12/tint/v4\n'
                    'P_NEU=40;40;40\nP_CTX=x\x1b]0;pwn\x07\nT_NEU=230;230;230\n')
        out = self.render('th', {'CLAUDE_SL_TMP': slt})
        self.assertNotIn('\x1b]0;', out, '色板缓存里的非数值被拼进了 SGR')


class Lifecycle(CacheBase):
    """缓存生命周期：默认根里 30 天没动的缓存由后台每天清一次，显式根不清理。"""

    def old(self, path, days=40):
        t = time.time() - days * 86400
        os.utime(path, (t, t))

    def refresh(self, env, sid='life'):
        return subprocess.run(['bash', SCRIPT, '--refresh', self.d, '-', sid, 'sub'],
                              capture_output=True, env=env)

    def test_prune_keeps_session_state_while_transcript_exists(self):
        """会话状态是重扫断点：transcript 还在就不能删，哪怕状态文件很旧。"""
        p = self.private()
        os.makedirs(p, mode=0o700)
        tp = os.path.join(self.d, 'live session.jsonl')
        with open(tp, 'w') as f:
            f.write('{}\n')
        state = os.path.join(p, 'claude-statusline-live.json')
        with open(state, 'w') as f:
            json.dump({'v': 9, 'files': {tp: {'off': 3}}}, f)
        self.old(state, days=400)
        dead = os.path.join(p, 'claude-statusline-dead.json')
        with open(dead, 'w') as f:
            json.dump({'v': 9, 'files': {os.path.join(self.d, 'gone.jsonl'): {'off': 3}}}, f)
        self.old(dead, days=400)
        self.refresh(self.env)
        self.assertTrue(os.path.exists(state), 'transcript 仍在的会话状态被清理了')
        self.assertFalse(os.path.exists(dead), 'transcript 已不存在的死会话状态没有被清理')

    def test_prune_keeps_recent_dead_session_state(self):
        p = self.private()
        os.makedirs(p, mode=0o700)
        dead = os.path.join(p, 'claude-statusline-recent.json')
        with open(dead, 'w') as f:
            json.dump({'v': 9, 'files': {os.path.join(self.d, 'gone.jsonl'): {'off': 3}}}, f)
        self.refresh(self.env)
        self.assertTrue(os.path.exists(dead), '未超过 30 天的会话状态不应被清理')

    def test_prune_removes_only_stale_files_in_default_root(self):
        p = self.private()
        os.makedirs(p, mode=0o700)
        files = {n: os.path.join(p, n) for n in (
            'claude-sl-tok-old.cache', 'claude-sl-tok-old.cache.ts',
            'claude-sl-skip-old', 'claude-sl-git-k.cache',
            'claude-statusline-old.json',
            'claude-sl-tok-live.cache', 'claude-sl-tok-live.cache.ts')}
        for n, f in files.items():
            with open(f, 'w') as fh:
                fh.write('x\n')
        for n in ('claude-sl-tok-old.cache', 'claude-sl-tok-old.cache.ts',
                  'claude-sl-skip-old', 'claude-sl-git-k.cache',
                  'claude-statusline-old.json'):
            self.old(files[n])
        # 不在清理名单里的东西即使很旧也不能删
        keep = {'claude-sl-theme-term-other.cache': 'THEME=dracula/12/tint/v4\n',
                'claude-sl-status.cache': '1\tnone\n'}
        for n, body in keep.items():
            with open(os.path.join(p, n), 'w') as fh:
                fh.write(body)
            self.old(os.path.join(p, n))
        self.refresh(self.env)
        for n in ('claude-sl-tok-old.cache', 'claude-sl-tok-old.cache.ts',
                  'claude-sl-skip-old', 'claude-sl-git-k.cache',
                  'claude-statusline-old.json'):
            self.assertFalse(os.path.exists(files[n]), f'{n} 已超 30 天但没有被清理')
        for n in ('claude-sl-tok-live.cache', 'claude-sl-tok-live.cache.ts') + tuple(keep):
            self.assertTrue(os.path.exists(os.path.join(p, n)), f'{n} 不该被清理')

    def test_prune_skips_symlinks_and_subdirectories(self):
        p = self.private()
        os.makedirs(p, mode=0o700)
        victim = os.path.join(self.d, 'victim.txt')
        with open(victim, 'w') as f:
            f.write('keep me')
        self.old(victim)
        link = os.path.join(p, 'claude-sl-tok-link.cache')
        os.symlink(victim, link)
        self.old(link)
        sub = os.path.join(p, 'claude-sl-tok-sub.cache')
        os.makedirs(sub)
        self.old(sub)
        inside = os.path.join(sub, 'nested')
        with open(inside, 'w') as f:
            f.write('x')
        self.old(inside)
        self.refresh(self.env)
        self.assertTrue(os.path.exists(victim), '清理跟随符号链接删掉了缓存目录之外的文件')
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isfile(inside), '清理递归进了子目录')

    def test_prune_runs_at_most_once_a_day(self):
        p = self.private()
        os.makedirs(p, mode=0o700)
        f = os.path.join(p, 'claude-sl-tok-old.cache')
        with open(f, 'w') as fh:
            fh.write('x\n')
        self.old(f)
        self.refresh(self.env)
        self.assertFalse(os.path.exists(f))
        stamp = os.path.join(p, 'claude-sl-prune.stamp')
        self.assertTrue(os.path.isfile(stamp), '清理后没有留下时间戳')
        # 同一天内新建的过期文件不触发第二次清理
        again = os.path.join(p, 'claude-sl-tok-old2.cache')
        with open(again, 'w') as fh:
            fh.write('x\n')
        self.old(again)
        self.refresh(self.env)
        self.assertTrue(os.path.exists(again), '同一天内又跑了一次清理')

    def test_explicit_cache_dir_is_never_pruned(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        f = os.path.join(slt, 'claude-sl-tok-old.cache')
        with open(f, 'w') as fh:
            fh.write('x\n')
        self.old(f)
        self.refresh(dict(self.env, CLAUDE_SL_TMP=slt))
        self.assertTrue(os.path.exists(f), '显式 CLAUDE_SL_TMP 的目录被清理了')
        self.assertFalse(os.path.exists(os.path.join(slt, 'claude-sl-prune.stamp')))

    def test_deleted_cache_dir_is_recreated(self):
        p = self.private()
        os.makedirs(p, mode=0o700)
        self.refresh(self.env)
        shutil.rmtree(p)
        self.refresh(self.env)
        self.assertTrue(os.path.isdir(p), '缓存目录被删后没有自动重建')
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700)

    def test_read_only_cache_dir_degrades_quietly(self):
        """无写权限的缓存根：渲染照常出（读得到的缓存仍生效），刷新静默退出，不报错。"""
        p = self.private()
        os.makedirs(p, mode=0o700)
        with open(os.path.join(p, 'claude-sl-tok-ro.cache'), 'w') as f:
            f.write(tok_line(**{'in': '555M'}, msgs='77'))
        with open(os.path.join(p, 'claude-sl-tok-ro.cache.ts'), 'w') as f:
            f.write(f'{int(time.time())}\n')
        os.chmod(p, 0o500)
        try:
            out = self.render('ro')
            self.assertIn('↑555M', out, '只读根里已缓存的项应当照常生效')
            r = self.refresh(self.env)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stderr, b'', '只读缓存根不应向状态栏输出错误')
        finally:
            os.chmod(p, 0o700)


class Hardening(CacheBase):
    def test_symlinked_cache_root_is_not_used(self):
        target = os.path.join(self.d, 'target')
        os.makedirs(target)
        os.symlink(target, self.private())
        out = self.render()
        self.assertNotIn('↑', out)
        self.assertEqual(os.listdir(target), [], '把缓存写进了符号链接指向的目录')

    def test_symlinked_token_cache_is_ignored(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        real = os.path.join(self.d, 'tok')
        with open(real, 'w') as f:
            f.write(tok_line(**{'in': '777M'}, msgs='999'))
        self.old_ts(slt, 'sym')
        os.symlink(real, os.path.join(slt, 'claude-sl-tok-sym.cache'))
        out = self.render('sym', {'CLAUDE_SL_TMP': slt})
        self.assertNotIn('↑777M', out, '符号链接被当作 token 缓存读取了')

    def old_ts(self, slt, sid, age=0):
        with open(os.path.join(slt, f'claude-sl-tok-{sid}.cache.ts'), 'w') as f:
            f.write(f'{int(time.time()) - age}\n')

    def test_tmpdir_trailing_slash_and_unicode(self):
        t = os.path.join(self.d, 'T M P 空')
        os.makedirs(t, mode=0o700)
        env = dict(self.env, TMPDIR=t + os.sep)
        subprocess.run(['bash', SCRIPT, '--refresh', self.d, '-', 'x', 'sub'],
                       capture_output=True, env=env)
        want = os.path.join(t, f'claude-sl-{os.getuid()}')
        self.assertTrue(os.path.isdir(want), f'含空格 / 中文的 TMPDIR 未归一（{want}）')

    def test_unset_tmpdir_falls_back_to_tmp(self):
        """TMPDIR 未设置时用 /tmp 下的私有目录——只核对渲染器与 token 助手同源，不写真实缓存。"""
        with open(SCRIPT, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('tmpdir=${TMPDIR:-/tmp}', src)
        with open(os.path.join(os.path.dirname(SCRIPT), 'statusline-tokens.py'),
                  encoding='utf-8') as f:
            toks = f.read()
        self.assertIn("os.environ.get('TMPDIR') or '/tmp'", toks)
        self.assertIn("f'claude-sl-{os.getuid()}'", toks)


if __name__ == '__main__':
    unittest.main()
