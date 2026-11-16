"""缓存目录隔离、权限与缓存内容安全。

默认缓存目录必须是每用户私有（0700），文件只允许本人读写（0600）；读取前还要确认
所有者是当前进程（跨用户预置的文件一律忽略）。token / theme 缓存来自磁盘，按不可信
数据对待：控制字符不得进入状态栏输出。
"""
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from _util import SCRIPT, TOKENS, Sandbox, strip, tok_line


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

    def test_theme_cache_key_cannot_run_commands(self):
        """printf -v 的目标名来自缓存：数组下标里的 $(…) 会被求值，键必须先按字符集校验。"""
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        marker = os.path.join(self.d, 'pwned')
        with open(os.path.join(slt, 'claude-sl-theme-term-other.cache'), 'w') as f:
            f.write('THEME=dracula/12/tint/v4\nP_NEU=40;40;40\nP_CTX=40;40;40\nT_NEU=230;230;230\n'
                    f'P_x[$(touch {marker})]=1;2;3\n')
        out = self.render('thk', {'CLAUDE_SL_TMP': slt})
        self.assertFalse(os.path.exists(marker), '色板缓存的键触发了命令执行')
        self.assertIn('42%', strip(out))

    def test_token_cache_number_cannot_run_commands(self):
        """命中率要进算术展开取整：$((…)) 会把变量内容当表达式求值，非数字必须先丢弃。"""
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        marker = os.path.join(self.d, 'pwned')
        with open(os.path.join(slt, 'claude-sl-tok-arith.cache'), 'w') as f:
            f.write(tok_line(**{'in': '30M', 'out': '290k', 'hit': f'a[$(touch {marker})].5'}))
        out = self.render('arith', {'CLAUDE_SL_TMP': slt})
        self.assertFalse(os.path.exists(marker), 'token 缓存的命中率字段触发了命令执行')
        self.assertNotIn('hit', out, '非数字的命中率不该显示')
        self.assertIn('↑30M', out, '其余合法字段不应被连带丢弃')


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
        # 合法但没有任何 transcript 的会话状态应当被清；损坏 / 读不出的保留（不误删断点）
        with open(files['claude-statusline-old.json'], 'w') as fh:
            json.dump({'v': 9, 'files': {}}, fh)
        corrupt = os.path.join(p, 'claude-statusline-corrupt.json')
        with open(corrupt, 'w') as fh:
            fh.write('not json\n')
        self.old(corrupt)
        for n in ('claude-sl-tok-old.cache', 'claude-sl-tok-old.cache.ts',
                  'claude-sl-skip-old', 'claude-sl-git-k.cache',
                  'claude-statusline-old.json'):
            self.old(files[n])
        # 不在清理名单里的东西即使很旧也不能删
        keep = {'claude-sl-theme-term-other.cache': 'THEME=dracula/12/tint/v4\n',
                'claude-sl-status.cache': '1\tnone\n',
                'claude-subagent-sl-%d.json' % os.getuid(): '{"t":{}}\n'}
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
        self.assertTrue(os.path.exists(corrupt), '读不出的会话状态被误删（无法确认 transcript 已消失）')

    def test_prune_covers_every_temp_file_name(self):
        """被杀进程遗留的临时文件靠 .claude-sl-* 谓词回收：每个生产者的临时名都得落在里面。"""
        src = os.path.dirname(SCRIPT)
        names = [TOKENS.TMP_PREFIX + 'abc123']
        for sh in glob.glob(os.path.join(src, '*.sh')):
            with open(sh, encoding='utf-8') as f:
                for tpl in re.findall(r'mktemp "[^"]*/([^"/]+)\.XXXXXX"', f.read()):
                    names.append(re.sub(r'\$\{?\w+\}?', 'x', tpl) + '.abc123')
        self.assertGreaterEqual(len(names), 4, '没扫到 mktemp 模板，正则该跟着改了')
        p = self.private()
        os.makedirs(p, mode=0o700)
        for n in names:
            with open(os.path.join(p, n), 'w') as fh:
                fh.write('x\n')
            self.old(os.path.join(p, n))
        self.refresh(self.env)
        left = [n for n in names if os.path.exists(os.path.join(p, n))]
        self.assertEqual(left, [], '这些临时文件名不在每日清理的谓词里')

    def test_prune_threshold_is_exactly_30_days(self):
        """30 天是精确阈值：用落在 30~31 天之间的文件，-mtime 的整天取整会漏掉它。"""
        p = self.private()
        os.makedirs(p, mode=0o700)
        # 30.5 天：find -mtime +30 不匹配（它按整天取整，要超过 31 天才算），-mmin 才命中
        between = os.path.join(p, 'claude-sl-tok-30d12h.cache')
        fresh = os.path.join(p, 'claude-sl-tok-29d.cache')
        for f, days in ((between, 30.5), (fresh, 29)):
            with open(f, 'w') as fh:
                fh.write('x\n')
            self.old(f, days=days)
        self.refresh(self.env)
        self.assertFalse(os.path.exists(between), '30.5 天前的缓存没有被清理（阈值不是精确 30 天）')
        self.assertTrue(os.path.exists(fresh), '29 天前的缓存被误删')

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

    def test_occupied_refresh_lock_is_cleared(self):
        """主栏刷新锁被普通文件占住时清掉重试；否则该会话后台永久退出。"""
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        lock = os.path.join(slt, 'claude-sl-lock-life')
        with open(lock, 'w') as f:
            f.write('x')
        r = self.refresh(dict(self.env, CLAUDE_SL_TMP=slt), sid='life')
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(lock), '被普通文件占住的刷新锁没有被清除')

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


class RefreshState(unittest.TestCase):
    """后台刷新的时间状态机：git 缓存的 TTL 与 HEAD 失效、残锁接管、色板失败退避、token 助手节流。

    时间戳都在缓存文件内容里（git 缓存第 4 段、.fail、skip 文件），锁目录年龄用 mtime——
    所以直接改文件就能构造「过去」，不需要 sleep，也不需要给脚本加时钟注入。
    """

    def setUp(self):
        self.sb = Sandbox(conf_extra='status_page = off').__enter__()
        self.addCleanup(self.sb.__exit__, None, None, None)
        self.now = int(time.time())

    def git_cache_path(self):
        names = [f for f in os.listdir(self.sb.tmp) if f.startswith('claude-sl-git-')]
        self.assertEqual(len(names), 1, names)
        return os.path.join(self.sb.tmp, names[0])

    def git_cache(self):
        with open(self.git_cache_path()) as f:
            return f.read().rstrip('\n').split('\t')

    def plant_git_cache(self, branch, age):
        """把 git 缓存改写成 <branch>、写入时刻 = age 秒前；.git 的 HEAD / index 推到更早。"""
        with open(self.git_cache_path(), 'w') as f:
            f.write(f'{self.sb.repo}\t{branch}\t-\t{self.now - age}\n')
        for name in ('HEAD', 'index'):
            p = os.path.join(self.sb.repo, '.git', name)
            if os.path.exists(p):
                os.utime(p, (self.now - 3600, self.now - 3600))

    def fake_python(self, body):
        """PATH 最前面放一个假 python3（留调用标记），返回 (环境变量, 标记文件)。"""
        b = os.path.join(self.sb.dir, 'fakebin')
        os.makedirs(b, exist_ok=True)
        marker = os.path.join(self.sb.dir, 'python-calls')
        with open(os.path.join(b, 'python3'), 'w') as f:
            f.write(f'#!/bin/sh\necho x >> "{marker}"\n{body}\n')
        os.chmod(os.path.join(b, 'python3'), 0o755)
        return dict(self.sb.env, PATH=b + os.pathsep + self.sb.env['PATH']), marker

    def calls(self, marker):
        if not os.path.exists(marker):
            return 0
        with open(marker) as f:
            return len(f.read().split())

    # ── git 缓存 ─────────────────────────────────────────────────────────
    def test_fresh_git_cache_is_reused(self):
        """GIT_TTL 内且 HEAD / index 没动：不重算（git status 是后台刷新里最贵的一项）。"""
        self.plant_git_cache('cached', age=0)
        self.sb.refresh(self.sb.repo)
        self.assertEqual(self.git_cache()[1], 'cached')

    def test_git_cache_expires_after_ttl(self):
        self.plant_git_cache('stale', age=60)
        self.sb.refresh(self.sb.repo)
        self.assertEqual(self.git_cache()[1], 'main', '超过 GIT_TTL 的 git 缓存没有重算')

    def test_head_change_invalidates_git_cache_within_ttl(self):
        """切分支后不等 TTL：HEAD 不比缓存旧就立即重算。"""
        self.plant_git_cache('main', age=0)
        subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/feature'], cwd=self.sb.repo, check=True)
        self.sb.refresh(self.sb.repo)
        self.assertEqual(self.git_cache()[1], 'feature', '切分支后 TTL 内没有重算')

    def test_render_reads_git_cache(self):
        """同步路径读的是缓存，不现算：缓存里写什么分支就显示什么。"""
        self.plant_git_cache('from-cache', age=0)
        self.assertIn('from-cache', strip(self.sb.render(self.sb.stdin())))

    # ── 刷新锁 ───────────────────────────────────────────────────────────
    def test_stale_refresh_lock_is_taken_over(self):
        """被杀的刷新留下空锁目录：超过 LOCK_STALE 后接管，否则该会话后台永久停摆。"""
        lock = os.path.join(self.sb.tmp, 'claude-sl-lock-lk')
        os.mkdir(lock)
        os.utime(lock, (self.now - 3600, self.now - 3600))
        self.plant_git_cache('stale', age=60)
        self.sb.refresh(self.sb.repo, sid='lk')
        self.assertEqual(self.git_cache()[1], 'main', '残锁没有被接管，刷新没跑')
        self.assertFalse(os.path.exists(lock), '接管后的锁没有释放')

    def test_live_refresh_lock_is_respected(self):
        lock = os.path.join(self.sb.tmp, 'claude-sl-lock-lk')
        os.mkdir(lock)
        self.plant_git_cache('stale', age=60)
        self.sb.refresh(self.sb.repo, sid='lk')
        self.assertEqual(self.git_cache()[1], 'stale', '锁还新鲜时不该并行再跑一轮刷新')
        self.assertTrue(os.path.isdir(lock), '别人的锁被删了')

    # ── 色板失败退避 ─────────────────────────────────────────────────────
    def break_theme(self, fail_age):
        tc = next(os.path.join(self.sb.tmp, f) for f in os.listdir(self.sb.tmp)
                  if f.startswith('claude-sl-theme-') and f.endswith('.cache'))
        with open(tc, 'w') as f:
            f.write('THEME=bogus\n')
        with open(tc + '.fail', 'w') as f:
            f.write(f'{self.now - fail_age}\n')

    def test_theme_failure_backs_off(self):
        """色板生成刚失败过：THEME_TTL 内不再每轮重跑 python。"""
        self.break_theme(fail_age=5)
        env, marker = self.fake_python('exit 1')
        self.sb.refresh(self.sb.repo, env=env)
        self.assertEqual(self.calls(marker), 0, '退避期内仍然重跑了色板生成')

    def test_theme_retries_after_backoff(self):
        self.break_theme(fail_age=3600)
        env, marker = self.fake_python('exit 1')
        self.sb.refresh(self.sb.repo, env=env)
        self.assertEqual(self.calls(marker), 1, '退避期过后没有重试色板生成')

    # ── token 助手节流 ───────────────────────────────────────────────────
    def test_token_helper_skipped_when_inputs_unchanged(self):
        """输入没变且距上次不足 TOK_MIN_GAP：跳过 python；transcript 变了就再跑。"""
        import sys
        tp = os.path.join(self.sb.dir, 'gap.jsonl')
        row = json.dumps({'type': 'assistant', 'timestamp': '2026-10-28T06:00:00Z',
                          'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
                                      'usage': {'input_tokens': 10, 'output_tokens': 1,
                                                'cache_creation_input_tokens': 0,
                                                'cache_read_input_tokens': 0}}})
        with open(tp, 'w') as f:
            f.write(row + '\n')
        env, marker = self.fake_python(f'exec "{sys.executable}" "$@"')

        def run():
            subprocess.run(['bash', SCRIPT, '--refresh', self.sb.repo, tp, 'gap', 'sub', '1.5'],
                           env=env, capture_output=True)
        run()
        self.assertEqual(self.calls(marker), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.sb.tmp, 'claude-sl-tok-gap.cache')))
        run()
        self.assertEqual(self.calls(marker), 1, '输入没变却又跑了一次 token 助手')
        with open(tp, 'a') as f:
            f.write(row.replace('"m1"', '"m2"') + '\n')
        run()
        self.assertEqual(self.calls(marker), 2, 'transcript 变了却没有重跑 token 助手')


if __name__ == '__main__':
    unittest.main()
