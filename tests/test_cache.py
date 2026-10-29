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

    def test_theme_cache_rejects_non_numeric_values(self):
        slt = os.path.join(self.d, 'slt')
        os.makedirs(slt, mode=0o700)
        with open(os.path.join(slt, 'claude-sl-theme-term-other.cache'), 'w') as f:
            f.write('THEME=dracula/12/tint/v4\n'
                    'P_NEU=40;40;40\nP_CTX=x\x1b]0;pwn\x07\nT_NEU=230;230;230\n')
        out = self.render('th', {'CLAUDE_SL_TMP': slt})
        self.assertNotIn('\x1b]0;', out, '色板缓存里的非数值被拼进了 SGR')


if __name__ == '__main__':
    unittest.main()
