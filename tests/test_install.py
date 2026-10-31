"""install.sh：statusLine 与 subagentStatusLine 的安装 / 检查 / 卸载覆盖。

两个键都要检查：只装脚本不配键、键指向不存在的脚本、卸载后 settings 仍指向
已删除脚本，都必须被指出来（卸载不自动改用户 settings）。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, 'install.sh')


class Install(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='ccpills-inst-')
        self.dest = os.path.join(self.d, 'claude')
        self.cfg = os.path.join(self.d, 'cfg')
        os.makedirs(self.dest)
        os.makedirs(self.cfg)
        self.env = dict(os.environ, HOME=self.d, CLAUDE_CONFIG_DIR=self.dest,
                        XDG_CONFIG_HOME=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_sh(self, *args, settings=None, env=None):
        e = env or self.env
        if settings is not None:
            dest = e.get('CLAUDE_CONFIG_DIR', self.dest)
            with open(os.path.join(dest, 'settings.json'), 'w') as f:
                json.dump(settings, f)
        return subprocess.run(['bash', INSTALL, *args], capture_output=True,
                              text=True, env=e)

    def cmds(self, sub=True):
        out = {'statusLine': {'type': 'command',
                              'command': f'bash {self.dest}/statusline-command.sh'}}
        if sub:
            out['subagentStatusLine'] = {'type': 'command',
                                         'command': f'bash {self.dest}/subagent-statusline.sh'}
        return out

    def test_install_reports_both_keys_and_check_passes(self):
        r = self.run_sh('--with-subagent')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('statusLine', r.stdout)
        self.assertIn('subagentStatusLine', r.stdout, '装面板脚本后必须提示第二个键')
        for f in ('statusline-command.sh', 'statusline-tokens.py', 'statusline-theme.py',
                  'subagent-statusline.sh'):
            self.assertTrue(os.path.isfile(os.path.join(self.dest, f)), f)
        r = self.run_sh('--check', settings=self.cmds())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('subagentStatusLine', r.stdout)

    def test_check_flags_dangling_subagent_key(self):
        self.run_sh('--with-subagent')
        s = self.cmds()
        s['subagentStatusLine']['command'] = 'bash /nonexistent/subagent-statusline.sh'
        r = self.run_sh('--check', settings=s)
        self.assertEqual(r.returncode, 1)
        self.assertIn('subagentStatusLine', r.stdout)

    def test_check_flags_missing_key_when_panel_installed(self):
        self.run_sh('--with-subagent')
        r = self.run_sh('--check', settings=self.cmds(sub=False))
        self.assertEqual(r.returncode, 1)
        self.assertIn('subagentStatusLine', r.stdout)

    def test_uninstall_warns_but_keeps_settings(self):
        self.run_sh('--with-subagent')
        s = self.cmds()
        self.run_sh('--check', settings=s)
        with open(os.path.join(self.dest, 'settings.json')) as f:
            before = f.read()
        r = self.run_sh('--uninstall')
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in ('statusline-command.sh', 'subagent-statusline.sh'):
            self.assertFalse(os.path.exists(os.path.join(self.dest, f)))
        self.assertIn('仍指向已删除', r.stderr, '卸载后要提示悬空的 settings 键')
        with open(os.path.join(self.dest, 'settings.json')) as f:
            self.assertEqual(f.read(), before, '卸载不应改写用户 settings')

    def test_uninstall_warns_for_official_command_form(self):
        """README 建议的 ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/... 写法也要被识别。"""
        self.run_sh('--with-subagent')
        s = {'statusLine': {'type': 'command',
                            'command': 'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh"'},
             'subagentStatusLine': {'type': 'command',
                                    'command': 'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/subagent-statusline.sh"'}}
        r = self.run_sh('--check', settings=s)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_sh('--uninstall')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('仍指向已删除', r.stderr)

    def test_help_includes_full_header(self):
        r = self.run_sh('--help')
        self.assertEqual(r.returncode, 0)
        for s in ('--with-subagent', '--with-conf', '--check', '--uninstall', '覆盖前把旧副本'):
            self.assertIn(s, r.stdout)
        self.assertNotIn('set -u', r.stdout)

    def test_check_accepts_home_shortcuts(self):
        """$HOME/.claude/... 与 ~/.claude/... 在默认 DEST 下是等价写法，不能误判。"""
        home_dest = os.path.join(self.d, '.claude')
        os.makedirs(home_dest, exist_ok=True)
        env = dict(self.env, HOME=self.d, CLAUDE_CONFIG_DIR=home_dest)
        self.assertEqual(self.run_sh('--with-subagent', env=env).returncode, 0)
        s = {'statusLine': {'type': 'command',
                            'command': 'bash "$HOME/.claude/statusline-command.sh"'},
             'subagentStatusLine': {'type': 'command',
                                    'command': 'bash "~/.claude/subagent-statusline.sh"'}}
        r = self.run_sh('--check', settings=s, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_with_conf_does_not_freeze_default_steps(self):
        r = self.run_sh('--with-conf')
        self.assertEqual(r.returncode, 0, r.stderr)
        conf = os.path.join(self.cfg, 'claude-statusline.conf')
        self.assertTrue(os.path.isfile(conf))
        with open(conf) as f:
            text = f.read()
        active = [l for l in text.splitlines() if l.strip().startswith('step')]
        self.assertEqual(active, [], '安装的配置不应带 active step（否则冻结默认序列）')
        self.assertIn('# step = drop    api', text)

    def test_python_floor_matches_doctor(self):
        """install.sh 的 python 下限必须跟 doctor 的 PY_MIN 同源，改一处不能漏另一处。"""
        with open(os.path.join(REPO, 'tools', 'doctor.sh'), encoding='utf-8') as f:
            m = re.search(r'^PY_MIN=(\d+)\.(\d+)', f.read(), re.M)
        self.assertIsNotNone(m, 'doctor 里没有 PY_MIN')
        major, minor = m.group(1), m.group(2)
        with open(INSTALL, encoding='utf-8') as f:
            src = f.read()
        self.assertIn(f'sys.version_info >= ({major}, {minor})', src,
                      'install.sh 的 python 版本比较与 PY_MIN 不一致')
        self.assertIn(f'≥ {major}.{minor}', src, 'install.sh 的提示文案与 PY_MIN 不一致')

    def test_low_python_is_refused(self):
        """安装时就该拦下 <3.9：装完才发现 token / 色板静默失效是最差的失败方式。"""
        b = os.path.join(self.d, 'bin')
        os.makedirs(b, exist_ok=True)
        with open(os.path.join(b, 'python3'), 'w') as f:
            # -c 版本探针按 <3.9 失败，-V 报出具体版本供提示使用
            f.write('#!/bin/sh\ncase "$1" in -c) exit 1;; -V) echo "Python 3.7.9";; esac\nexit 0\n')
        os.chmod(os.path.join(b, 'python3'), 0o755)
        r = self.run_sh(env=dict(self.env, PATH=b + os.pathsep + self.env['PATH']))
        self.assertEqual(r.returncode, 1)
        self.assertIn('3.9', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'statusline-command.sh')),
                         '依赖不满足时不应安装任何副本')


if __name__ == '__main__':
    unittest.main()
