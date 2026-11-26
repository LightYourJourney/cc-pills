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
                              text=True, env=e, timeout=60)

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

    def points_to(self, cmd, dest):
        r = subprocess.run(['bash', '-c', '. "$1"; points_to "$2" statusline-command.sh "$3"; echo $?',
                            '_', os.path.join(REPO, 'tools', 'lib', 'points_to.sh'), cmd, dest],
                           capture_output=True, text=True, env=self.env, timeout=30)
        return int(r.stdout)

    def test_home_shortcuts_must_name_the_installed_file(self):
        """$HOME/.claude/ 与 ~/.claude/ 后面必须紧跟脚本名：子目录里的同名脚本、前缀黏住的
        文件名、复合命令里的另一个脚本跑的都不是安装副本（克隆在 ~/.claude/cc-pills 时会误判）。"""
        home_dest = os.path.join(self.d, '.claude')
        for cmd in ('bash $HOME/.claude/cc-pills/src/statusline-command.sh',
                    'bash ~/.claude/backup/statusline-command.sh',
                    'bash $HOME/.claude/my-statusline-command.sh',
                    'bash $HOME/.claude/statusline-tokens.py; bash /opt/x/statusline-command.sh'):
            self.assertEqual(self.points_to(cmd, home_dest), 2, cmd)
        self.assertEqual(self.points_to('bash $HOME/.claude/statusline-command.sh', home_dest), 0)
        # 设了 CLAUDE_CONFIG_DIR=/别处 时，$HOME/.claude/… 跑的是默认目录里的另一份
        self.assertEqual(self.points_to('bash $HOME/.claude/statusline-command.sh', self.dest), 2)

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

    @unittest.skipIf(os.geteuid() == 0, 'root 不受目录权限限制')
    def test_copy_failure_exits_nonzero(self):
        """目标目录写不进去时不能报成功：否则用户以为装好了，状态栏却还是旧的。"""
        os.chmod(self.dest, 0o500)
        r = self.run_sh()
        os.chmod(self.dest, 0o700)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('安装失败', r.stderr)

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


    def test_uninstall_refuses_hand_edited_copy_without_force(self):
        """线上副本与仓库不同（被手改过）时卸载拒绝删它、返回 1，别的未改动副本照删；加 --force
        才删：用户对已安装脚本的改动不能被一条 --uninstall 悄悄带走。"""
        self.assertEqual(self.run_sh().returncode, 0)
        edited = os.path.join(self.dest, 'statusline-command.sh')
        with open(edited, 'a') as f:
            f.write('# 本地改动\n')
        r = self.run_sh('--uninstall')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('statusline-command.sh 与本仓库不同', r.stderr)
        self.assertIn('--force', r.stderr)
        with open(edited) as f:
            self.assertIn('# 本地改动', f.read(), '被手改的副本没有 --force 就被删了')
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'statusline-tokens.py')),
                         '未改动的副本应照常删除')
        r = self.run_sh('--uninstall', '--force')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(edited), '--force 没有删掉被手改的副本')

    def test_installer_refuses_bash_3_2(self):
        """安装器自己要在 bash ≥ 4.2 下跑（渲染脚本用到关联数组等）：macOS 自带的 /bin/bash 是 3.2，
        直接拒绝、提示换新版 bash，且一个副本都不装。/bin/bash 不是 3.x 的机器上无从构造，跳过。"""
        if not os.path.exists('/bin/bash'):
            self.skipTest('没有 /bin/bash')
        ver = subprocess.run(['/bin/bash', '-c', 'echo ${BASH_VERSINFO[0]}'],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if ver != '3':
            self.skipTest(f'/bin/bash 主版本是 {ver}，不是 3.x')
        r = subprocess.run(['/bin/bash', INSTALL, '--with-subagent'], capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('需要 bash', r.stderr)
        self.assertEqual(os.listdir(self.dest), [], '版本不够时不应安装任何副本')

    def test_uninstall_warns_when_command_only_names_the_script(self):
        """settings 命令只含脚本名、路径不是安装副本（points_to 返回 2）也算「仍指向」：卸载后
        同样提示，只有完全无关的命令（返回 1）才不吵。"""
        self.assertEqual(self.run_sh('--with-subagent').returncode, 0)
        s = {'statusLine': {'type': 'command', 'command': 'bash statusline-command.sh'},
             'subagentStatusLine': {'type': 'command',
                                    'command': 'bash /elsewhere/subagent-statusline.sh'}}
        r = self.run_sh('--uninstall', settings=s)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r'statusLine 仍指向已删除的 \S*/statusline-command\.sh')
        self.assertRegex(r.stderr, r'subagentStatusLine 仍指向已删除的 \S*/subagent-statusline\.sh')
        self.assertEqual(r.stderr.count('仍指向已删除'), 2, r.stderr)

    def test_check_flags_panel_command_pointing_elsewhere_when_panel_not_installed(self):
        """面板脚本没装、settings 的 subagentStatusLine 却含面板脚本名（只有文件名，或路径在别处）：
        --check 报「指向面板脚本，但文件未安装」并返回 1；配的是毫不相干的程序则不算失败。"""
        self.assertEqual(self.run_sh().returncode, 0)
        for cmd in ('bash subagent-statusline.sh', 'bash /elsewhere/subagent-statusline.sh'):
            s = self.cmds(sub=False)
            s['subagentStatusLine'] = {'type': 'command', 'command': cmd}
            r = self.run_sh('--check', settings=s)
            self.assertEqual(r.returncode, 1, f'{cmd}\n{r.stdout}')
            self.assertIn('指向面板脚本，但文件未安装', r.stdout, cmd)
        s['subagentStatusLine']['command'] = 'bash /usr/local/bin/other-panel.sh'
        r = self.run_sh('--check', settings=s)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('配置的是别的程序', r.stdout)

    def test_plain_reinstall_updates_installed_panel(self):
        """README：拉取后重跑 bash install.sh。已经装过的面板脚本必须一起更新，否则它悄悄
        停在旧版，直到 --check 才报不一致。"""
        self.assertEqual(self.run_sh('--with-subagent').returncode, 0)
        panel = os.path.join(self.dest, 'subagent-statusline.sh')
        with open(panel, 'a') as f:
            f.write('# 旧版\n')
        r = self.run_sh()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(panel) as f, open(os.path.join(REPO, 'src', 'subagent-statusline.sh')) as g:
            self.assertEqual(f.read(), g.read(), '重装没有更新已安装的面板脚本')
        self.assertEqual(self.run_sh('--check', settings=self.cmds()).returncode, 0)

    def test_fresh_install_without_flag_does_not_add_panel(self):
        self.assertEqual(self.run_sh().returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'subagent-statusline.sh')))

    @unittest.skipIf(os.geteuid() == 0, 'root 不受文件权限限制')
    def test_backup_failure_keeps_the_local_copy(self):
        """备份失败还照样覆盖，用户对已安装副本的改动就没了。"""
        self.assertEqual(self.run_sh().returncode, 0)
        dst = os.path.join(self.dest, 'statusline-command.sh')
        with open(dst, 'a') as f:
            f.write('# 本地改动\n')
        os.chmod(dst, 0o000)                    # 读不了 → 备份失败
        r = self.run_sh()
        os.chmod(dst, 0o644)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(dst) as f:
            self.assertIn('# 本地改动', f.read(), '备份失败后仍覆盖了本地改动')

    def test_invalid_settings_json_is_not_reported_as_unconfigured(self):
        """settings.json 解析失败时说它坏了，而不是引导用户再粘贴一个已经有的键。"""
        with open(os.path.join(self.dest, 'settings.json'), 'w') as f:
            f.write('{"statusLine": {"type": "command", "command": "bash x"},}\n')
        for args, rc in (((), 0), (('--check',), 1)):
            r = self.run_sh(*args)
            self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
            self.assertIn('不是合法 JSON', r.stdout, args)
            self.assertNotIn('尚未配置', r.stdout, args)
            self.assertNotIn('未指向', r.stdout, args)

    def test_check_rejects_config_dir_default_pointing_elsewhere(self):
        """${CLAUDE_CONFIG_DIR:-/别处} 在变量未设时跑的是别处的脚本，不能当成指向已安装副本。"""
        self.assertEqual(self.run_sh().returncode, 0)
        s = {'statusLine': {'type': 'command',
                            'command': 'bash "${CLAUDE_CONFIG_DIR:-/opt/stale}/statusline-command.sh"'}}
        r = self.run_sh('--check', settings=s)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('路径不是已安装副本', r.stdout)
        s['statusLine']['command'] = 'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh"'
        self.assertEqual(self.run_sh('--check', settings=s).returncode, 0, '建议写法被误判')

    def test_check_accepts_config_dir_with_trailing_slash(self):
        env = dict(self.env, CLAUDE_CONFIG_DIR=self.dest + '/')
        self.assertEqual(self.run_sh(env=env).returncode, 0)
        r = self.run_sh('--check', settings=self.cmds(sub=False), env=env)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == '__main__':
    unittest.main()
