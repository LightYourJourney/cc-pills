"""tools/doctor.sh：故障场景 → PASS / WARN / FAIL / SKIP / UNKNOWN。

每个用例都在临时 HOME / CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME / TMPDIR 里跑：不碰线上安装、
不碰用户缓存、不读真实 CC Switch 库、不联网。依赖缺失与版本过低用临时 PATH 里的假可执行
文件模拟。先建一份「健康」安装，再逐项破坏——诊断工具最常见的毛病是误报，所以健康态
必须零 FAIL 也是用例之一。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

from _util import REPO, THEME, TOKENS

DOCTOR = os.path.join(REPO, 'tools', 'doctor.sh')
INSTALL = os.path.join(REPO, 'install.sh')
SRC = os.path.join(REPO, 'src')
# 测试自身必须用真 bash 启动 doctor：PATH 会被换成假的，不能靠 PATH 解析
BASE_BASH = os.path.realpath(shutil.which('bash') or '/bin/bash')
BASE_PATH = os.environ.get('PATH', '/usr/bin:/bin')
STATUS = re.compile(r'^\[(PASS|WARN|FAIL|SKIP|UNKNOWN)\] (\S+) +(.*)$', re.M)


def read_const(name, path=os.path.join(SRC, 'statusline-command.sh')):
    """从渲染脚本读常量：doctor 里的同一数值由契约测试守护，不在测试里重复硬编码。"""
    with open(path) as f:
        m = re.search(rf'^{name}=(\d+)\s', f.read(), re.M)
    assert m, f'{path} 里没有 {name}'
    return m.group(1)


PALETTE_V = read_const('PALETTE_V')
THEME_TTL = int(read_const('THEME_TTL'))
TOK_STALE = int(read_const('TOK_STALE'))
LOCK_STALE = int(read_const('LOCK_STALE'))


class DoctorSandbox:
    """临时 HOME + 临时配置目录 + 临时 TMPDIR。"""

    def __init__(self):
        self.d = tempfile.mkdtemp(prefix='ccpills-doctor-')
        self.home = os.path.join(self.d, 'home')
        self.dest = os.path.join(self.home, '.claude')
        self.cfg = os.path.join(self.d, 'xdg')
        self.tmp = os.path.join(self.d, 'tmp')
        for p in (self.home, self.dest, self.cfg, self.tmp):
            os.makedirs(p, exist_ok=True)
        self.cache = os.path.join(self.tmp, f'claude-sl-{os.getuid()}')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('TERM_PROGRAM', 'LC_TERMINAL', 'ITERM_PROFILE', 'CLAUDE_CONFIG_DIR',
                                 'CLAUDE_SL_TMP', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY')}
        self.env.update(HOME=self.home, TMPDIR=self.tmp, XDG_CONFIG_HOME=self.cfg,
                        PATH=BASE_PATH, COLORTERM='truecolor', TERM='xterm-256color')

    def close(self):
        shutil.rmtree(self.d, ignore_errors=True)

    # ── 运行 ────────────────────────────────────────────────────────────
    def run(self, *args, env=None):
        return subprocess.run([BASE_BASH, DOCTOR, *args], capture_output=True, text=True,
                              env=dict(self.env, **(env or {})), timeout=120)

    @staticmethod
    def checks(r):
        return {m.group(2): (m.group(1), m.group(3)) for m in STATUS.finditer(r.stdout)}

    def status(self, r, name):
        c = self.checks(r)
        assert name in c, f'输出里没有 {name}：\n{r.stdout}'
        return c[name][0]

    def detail(self, r, name):
        return self.checks(r)[name][1]

    # ── 造健康安装 ──────────────────────────────────────────────────────
    def install(self, subagent=True, settings=True):
        args = ['--with-subagent'] if subagent else []
        r = subprocess.run([BASE_BASH, INSTALL, *args], capture_output=True, text=True, env=self.env)
        assert r.returncode == 0, r.stderr
        if settings:
            s = {'statusLine': {'type': 'command',
                                'command': f'bash {self.dest}/statusline-command.sh'}}
            if subagent:
                s['subagentStatusLine'] = {'type': 'command',
                                           'command': f'bash {self.dest}/subagent-statusline.sh'}
            self.write_settings(s)

    def write_settings(self, obj):
        p = os.path.join(self.dest, 'settings.json')
        with open(p, 'w') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def make_cache(self, mode=0o700, tok_age=0, theme_line=None, theme_age=0):
        os.makedirs(self.cache, exist_ok=True)
        os.chmod(self.cache, mode)
        if theme_line is not None:
            p = os.path.join(self.cache, 'claude-sl-theme-term-other.cache')
            with open(p, 'w') as f:
                f.write(theme_line + '\nNAME=dracula\nP_CTX=98;114;164\n')
            os.utime(p, (time.time() - theme_age, time.time() - theme_age))
            os.chmod(p, 0o600)
        if tok_age is not None:
            p = os.path.join(self.cache, 'claude-sl-tok-doctor.cache')
            with open(p, 'w') as f:
                f.write('-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t98.0\t-\t-\t12\n')
            ts = os.path.join(self.cache, 'claude-sl-tok-doctor.cache.ts')
            with open(ts, 'w') as f:
                f.write(f'{int(time.time() - tok_age)}\n')
            for q in (p, ts):
                os.chmod(q, 0o600)
        return self.cache

    def healthy(self, relay=False, tok_age=0):
        """健康态：装好副本、settings 两个键、私有缓存目录、新鲜色板与 token 缓存。"""
        self.install()
        line = f'THEME=auto/12/tint/v{PALETTE_V}'
        self.make_cache(theme_line=line, tok_age=tok_age)
        env = {}
        if relay:
            env['ANTHROPIC_BASE_URL'] = 'https://relay.example/v1'
        return env

    def minimal_path(self, extra=None):
        """只含常用工具的临时 bin（默认不含 jq / curl），用来模拟缺依赖。"""
        b = os.path.join(self.d, 'bin')
        os.makedirs(b, exist_ok=True)
        for n in ('bash', 'sh', 'python3', 'git', 'uname', 'sw_vers', 'stat', 'mktemp',
                  'rm', 'mv', 'mkdir', 'rmdir', 'chmod', 'sed', 'grep', 'tr', 'cut', 'head',
                  'tail', 'sort', 'date', 'id', 'basename', 'dirname', 'cat', 'find',
                  'readlink', 'sleep', 'awk', 'env', 'wc', 'printf', 'test'):
            p = shutil.which(n)
            if p and not os.path.exists(os.path.join(b, n)):
                os.symlink(p, os.path.join(b, n))
        for name, body in (extra or {}).items():
            self.fake(b, name, body)
        return b

    @staticmethod
    def fake(b, name, body):
        """在临时 bin 里造一个假可执行文件。

        先 unlink 再写：minimal_path 会把真实工具**软链**进来，直接 open(p,'w') 会写穿
        符号链接、覆盖掉本机的真程序（开发本测试时真的踩过一次）。再加一道解析路径的
        守卫，让同样的错误不可能再发生。
        """
        p = os.path.join(b, name)
        b_real = os.path.realpath(b)
        if os.path.exists(p) or os.path.islink(p):
            os.unlink(p)
        assert os.path.realpath(os.path.dirname(p)) == b_real, f'拒绝写到临时 bin 之外：{p}'
        with open(p, 'w') as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p


class Doctor(unittest.TestCase):
    def setUp(self):
        self.sb = DoctorSandbox()
        self.addCleanup(self.sb.close)

    # ── 健康态：诊断工具首先不能误报 ────────────────────────────────────
    def test_healthy_install_has_no_fail(self):
        env = self.sb.healthy()
        r = self.sb.run(env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        fails = [k for k, (s, _) in self.sb.checks(r).items() if s == 'FAIL']
        self.assertEqual(fails, [], r.stdout)
        self.assertEqual(self.sb.status(r, '安装/statusLine'), 'PASS')
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'PASS')

    def test_healthy_run_writes_nothing(self):
        """默认只读：除临时目录里的冒烟沙箱（跑完即删）外不落任何文件。"""
        env = self.sb.healthy()

        def snap():
            out = set()
            for root in (self.sb.home, self.sb.cfg, self.sb.tmp):
                for dp, dn, fn in os.walk(root):
                    for f in fn:
                        p = os.path.join(dp, f)
                        with open(p, 'rb') as fh:
                            out.add((p, hashlib.sha256(fh.read()).hexdigest()))
                    out.update(os.path.join(dp, d) + '/' for d in dn)
            return out

        before = snap()
        self.sb.run(env=env)
        self.assertEqual(snap(), before)

    # ── 1. 依赖缺失 / 版本过低 ──────────────────────────────────────────
    def test_missing_jq_is_fail(self):
        self.sb.install()
        r = self.sb.run(env={'PATH': self.sb.minimal_path()})
        self.assertEqual(self.sb.status(r, '环境/jq'), 'FAIL')
        self.assertEqual(r.returncode, 1)

    def test_low_python_is_fail(self):
        self.sb.install()
        b = self.sb.minimal_path()
        self.sb.fake(b, 'python3', '#!/bin/sh\necho "Python 3.7.9"\n')
        r = self.sb.run(env={'PATH': b})
        self.assertEqual(self.sb.status(r, '环境/python3'), 'FAIL')
        self.assertIn('3.9', self.sb.detail(r, '环境/python3'))
        self.assertEqual(self.sb.status(r, '缓存/后台刷新'), 'SKIP')

    def test_low_bash_is_reported(self):
        """PATH 里是 3.2 时必须报出来。本机若另装了 ≥5 的 bash，脚本的换壳守卫能接管，
        此时正确结论是 WARN；没有替代才是 FAIL——两种都算「查到了」，所以这里只断言不 PASS。
        （确定性覆盖 FAIL 分支的是 test_low_python_is_fail。）"""
        self.sb.install()
        b = self.sb.minimal_path()
        self.sb.fake(b, 'bash', '#!/bin/sh\n'
                                'case "$1" in -c) echo "3.2";; *) echo "GNU bash, version 3.2.57(1)-release";; esac\n')
        r = self.sb.run(env={'PATH': b})
        self.assertIn(self.sb.status(r, '环境/bash'), ('WARN', 'FAIL'))
        self.assertIn('4.2', r.stdout)

    # ── 2. settings.json：缺失 / 损坏 / 路径错误 ────────────────────────
    def test_missing_settings_is_fail(self):
        self.sb.install(settings=False)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/settings.json'), 'FAIL')
        self.assertEqual(self.sb.status(r, '安装/statusLine'), 'FAIL')
        self.assertIn('statusLine', r.stdout)

    def test_corrupt_settings_is_fail(self):
        self.sb.install(settings=False)
        self.sb.write_settings('{ not json')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/settings.json'), 'FAIL')
        self.assertIn('解析', self.sb.detail(r, '安装/settings.json'))

    def test_statusline_pointing_at_missing_file_is_fail(self):
        self.sb.install()
        self.sb.write_settings({'statusLine': {'type': 'command',
                                               'command': 'bash /nonexistent/statusline-command.sh'}})
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/statusLine'), 'FAIL')

    def test_bare_filename_is_not_accepted(self):
        """只有文件名、路径不明：install.sh --check 也不认，两边判断必须一致。"""
        self.sb.install()
        self.sb.write_settings({'statusLine': {'type': 'command', 'command': 'statusline-command.sh'}})
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/statusLine'), 'FAIL')
        chk = subprocess.run([BASE_BASH, INSTALL, '--check'], capture_output=True,
                             text=True, env=self.sb.env)
        self.assertEqual(chk.returncode, 1)

    def test_subagent_key_missing_warns_when_installed(self):
        self.sb.install(subagent=True, settings=False)
        self.sb.write_settings({'statusLine': {'type': 'command',
                                               'command': f'bash {self.sb.dest}/statusline-command.sh'}})
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/subagentStatusLine'), 'WARN')

    # ── 3. 安装副本不一致 ───────────────────────────────────────────────
    def test_installed_copy_drift_warns(self):
        self.sb.healthy()
        with open(os.path.join(self.sb.dest, 'statusline-tokens.py'), 'a') as f:
            f.write('# 本地改动\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/副本-statusline-tokens.py'), 'WARN')
        self.assertIn('install.sh', r.stdout)

    # ── 4. 缓存：缺失 / 过期 / 权限 ─────────────────────────────────────
    def test_missing_cache_root_is_skip_not_fail(self):
        self.sb.install()
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/根'), 'SKIP')
        self.assertNotEqual(self.sb.status(r, '缓存/色板'), 'FAIL')

    def test_cache_root_not_a_directory_is_fail(self):
        self.sb.install()
        with open(self.sb.cache, 'w') as f:
            f.write('x')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/根'), 'FAIL')

    def test_loose_cache_dir_mode_warns(self):
        self.sb.healthy()
        os.chmod(self.sb.cache, 0o755)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/权限'), 'WARN')
        self.assertIn('chmod 700', r.stdout)

    def test_stale_token_cache_warns(self):
        env = self.sb.healthy(tok_age=TOK_STALE + 60)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')

    def test_theme_header_mismatch_warns(self):
        self.sb.install()
        self.sb.make_cache(theme_line=f'THEME=auto/12/tint/v{PALETTE_V}old')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'WARN')

    def test_old_theme_cache_is_still_usable(self):
        """渲染侧读色板不看 mtime：年龄只影响后台何时重算，不算故障（没有活跃会话时必然偏旧）。"""
        self.sb.install()
        self.sb.make_cache(theme_line=f'THEME=auto/12/tint/v{PALETTE_V}', theme_age=THEME_TTL + 60)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'PASS')
        self.assertIn('按需', self.sb.detail(r, '缓存/色板'))

    def test_truncated_token_cache_is_flagged(self):
        """字段数不对的缓存会让派生项错位/消失，不能只看时间戳。"""
        env = self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache'), 'w') as f:
            f.write('-\t-\t-\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('18', self.sb.detail(r, '缓存/token'))

    def test_corrupt_ts_is_reported_as_stale(self):
        """渲染侧把非数字 .ts 判为陈旧；doctor 按 .ts 的 mtime 回退会漏报，必须同样判陈旧。"""
        env = self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache.ts'), 'w') as f:
            f.write('garbage\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('时间戳', self.sb.detail(r, '缓存/token'))

    def test_unreadable_config_warns_not_fails(self):
        """渲染脚本对不可读配置只是静默用默认值，状态栏照常工作——不能判 FAIL。"""
        self.sb.install()
        conf = os.path.join(self.sb.cfg, 'claude-statusline.conf')
        with open(conf, 'w') as f:
            f.write('theme = dracula\n')
        os.chmod(conf, 0)
        self.addCleanup(os.chmod, conf, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/配置文件'), 'WARN')
        self.assertIn('默认', self.sb.detail(r, '安装/配置文件'))

    def test_leftover_lock_warns(self):
        self.sb.healthy()
        lock = os.path.join(self.sb.cache, 'claude-sl-lock-doctor')
        os.makedirs(lock)
        old = time.time() - LOCK_STALE - 60
        os.utime(lock, (old, old))
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'WARN')
        self.assertIn('自动', self.sb.detail(r, '缓存/锁'))

    def test_theme_backoff_file_is_reported(self):
        self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-theme-term-other.cache.fail'), 'w') as f:
            f.write(f'{int(time.time())}\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/退避'), 'WARN')

    # ── 5. 数据来源：CC Switch 库 ───────────────────────────────────────
    def test_relay_from_settings_env_warns(self):
        """CC Switch 的标准写法是把 BASE_URL 写进 settings.json 的 env；只查 shell 变量会得出
        相反结论（SKIP 而不是「中转站但没库」）。"""
        self.sb.healthy()
        s = {'statusLine': {'type': 'command',
                            'command': f'bash {self.sb.dest}/statusline-command.sh'},
             'subagentStatusLine': {'type': 'command',
                                    'command': f'bash {self.sb.dest}/subagent-statusline.sh'},
             'env': {'ANTHROPIC_BASE_URL': 'https://relay.example/v1',
                     'ANTHROPIC_AUTH_TOKEN': 'tok_live_should_not_leak'}}
        self.sb.write_settings(s)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('settings.json', self.sb.detail(r, '数据/CCSwitch库'))
        self.assertNotIn('tok_live_should_not_leak', r.stdout)

    def test_db_is_not_opened_in_subscription_mode(self):
        """订阅模式渲染侧从不打开 CC Switch 库（tokens.py 约束 7），诊断也不该打开。"""
        self.sb.healthy()
        d = os.path.join(self.sb.home, '.cc-switch')
        os.makedirs(d)
        db = os.path.join(d, 'cc-switch.db')
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE proxy_request_logs (x)')
        con.commit()
        con.close()
        before = sorted(os.listdir(d))
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'SKIP')
        self.assertIn('不会打开', self.sb.detail(r, '数据/CCSwitch库'))
        self.assertEqual(sorted(os.listdir(d)), before)

    def test_relay_without_db_warns(self):
        env = self.sb.healthy(relay=True)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')

    def test_unreadable_db_warns(self):
        env = self.sb.healthy(relay=True)
        db = os.path.join(self.sb.home, '.cc-switch', 'cc-switch.db')
        os.makedirs(os.path.dirname(db))
        with open(db, 'w') as f:
            f.write('这不是数据库' * 100)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('$x*', self.sb.detail(r, '数据/CCSwitch库'))

    def test_db_is_opened_read_only(self):
        """真实库只允许只读：跑完之后字节完全不变。"""
        env = self.sb.healthy(relay=True)
        db = os.path.join(self.sb.home, '.cc-switch', 'cc-switch.db')
        os.makedirs(os.path.dirname(db))
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE proxy_request_logs (session_id TEXT, provider_id TEXT, '
                    'app_type TEXT, created_at INTEGER, total_cost_usd TEXT)')
        con.execute('CREATE TABLE providers (id TEXT, app_type TEXT, is_current INTEGER, '
                    'limit_daily_usd REAL, meta TEXT)')
        con.execute("INSERT INTO providers VALUES ('p1', 'claude', 1, 50, '{\"limitDailyUsd\": 50}')")
        con.commit()
        con.close()
        os.chmod(db, 0o600)
        with open(db, 'rb') as f:
            before = hashlib.sha256(f.read()).hexdigest()
        mtime = os.stat(db).st_mtime
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'PASS')
        with open(db, 'rb') as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)
        self.assertEqual(os.stat(db).st_mtime, mtime)

    def test_db_without_providers_table_warns(self):
        """旧库没有 providers 表：渲染侧容忍（只是隐藏日限额），诊断不能判 FAIL。"""
        env = self.sb.healthy(relay=True)
        db = os.path.join(self.sb.home, '.cc-switch', 'cc-switch.db')
        os.makedirs(os.path.dirname(db))
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE proxy_request_logs (session_id TEXT)')
        con.commit()
        con.close()
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('providers', self.sb.detail(r, '数据/CCSwitch库'))

    def test_relay_without_unique_provider_warns(self):
        env = self.sb.healthy(relay=True)
        db = os.path.join(self.sb.home, '.cc-switch', 'cc-switch.db')
        os.makedirs(os.path.dirname(db))
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE proxy_request_logs (session_id TEXT, provider_id TEXT, '
                    'app_type TEXT, created_at INTEGER, total_cost_usd TEXT)')
        con.execute('CREATE TABLE providers (id TEXT, app_type TEXT, is_current INTEGER, '
                    'limit_daily_usd REAL, meta TEXT)')
        con.commit()
        con.close()
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('供应商', self.sb.detail(r, '数据/CCSwitch库'))

    def test_billing_mode_is_unknown_without_stdin(self):
        self.sb.healthy()
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/计费模式'), 'UNKNOWN')

    # ── 6. 冒烟：用夹具真跑一次已安装脚本 ───────────────────────────────
    def test_smoke_render_parses_fixture(self):
        env = self.sb.healthy()
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'PASS')
        self.assertIn('不同', self.sb.detail(r, '数据/stdin'))

    def test_smoke_detects_stub_that_ignores_stdin(self):
        """只会 echo 的脚本也能「非空」，必须靠两份夹具结果不同才判通过。"""
        self.sb.install()
        with open(os.path.join(self.sb.dest, 'statusline-command.sh'), 'w') as f:
            f.write('#!/usr/bin/env bash\necho hello\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('相同', self.sb.detail(r, '数据/stdin'))

    def test_smoke_is_independent_of_custom_steps(self):
        """用户改过降级序列（删掉 ctx / 模型 / 目录）不该让冒烟误报——判据不看具体项。"""
        env = self.sb.healthy()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('step = drop ctx\nstep = drop model\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'PASS')

    def test_smoke_render_fails_on_silent_script(self):
        self.sb.install()
        with open(os.path.join(self.sb.dest, 'statusline-command.sh'), 'w') as f:
            f.write('#!/usr/bin/env bash\nexit 0\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')

    def test_smoke_render_detects_guard_fallback(self):
        """bash 版本守卫的最简行「Opus 5.5 · 37%」不能算冒烟通过——那说明主渲染没跑起来。"""
        self.sb.install()
        with open(os.path.join(self.sb.dest, 'statusline-command.sh'), 'w') as f:
            f.write('#!/usr/bin/env bash\nprintf "Opus 5.5 · 37%%"\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('最简行', self.sb.detail(r, '数据/stdin'))

    # ── 7. 敏感信息过滤 / 不联网 ────────────────────────────────────────
    def test_command_is_scrubbed(self):
        """命令要能显示（否则没法照着修），但凭据形态必须被抹掉。"""
        self.sb.install()
        self.sb.write_settings({'statusLine': {
            'type': 'command',
            'command': 'ANTHROPIC_API_KEY=plain123456; ANTHROPIC_AUTH_TOKEN=tok_live_abcdef; '
                       'echo sk-ant-api03-SECRETabcdef123456; '
                       'curl https://user:pw@relay.example/x; '
                       'bearer BearerTOKENvalue123456 bash /wrong/statusline-command.sh'}})
        r = self.sb.run()
        self.assertNotIn('user:pw', r.stdout)
        self.assertNotIn('plain123456', r.stdout)
        self.assertNotIn('tok_live_abcdef', r.stdout)
        self.assertNotIn('SECRETabcdef123456', r.stdout)
        self.assertNotIn('BearerTOKENvalue123456', r.stdout)
        self.assertIn('ANTHROPIC_API_KEY=***', r.stdout)
        self.assertIn('AUTH_TOKEN=***', r.stdout)
        self.assertIn('sk-***', r.stdout)
        self.assertIn('/wrong/statusline-command.sh', r.stdout, '路径必须留下，否则无法修')

    def test_no_network(self):
        self.sb.install()
        marker = os.path.join(self.sb.d, 'curl-was-called')
        b = self.sb.minimal_path(extra={'curl': f'#!/bin/sh\ntouch "{marker}"\nexit 7\n'})
        self.sb.run(env={'PATH': b})
        self.assertFalse(os.path.exists(marker), 'doctor 默认不得发起任何网络请求')

    # ── 8. 契约：常量与判据不得与渲染脚本漂移 ───────────────────────────
    def test_thresholds_match_renderer(self):
        with open(DOCTOR) as f:
            src = f.read()
        for name, want in (('THEME_TTL', THEME_TTL), ('TOK_STALE', TOK_STALE),
                           ('LOCK_STALE', LOCK_STALE), ('PALETTE_V', PALETTE_V)):
            m = re.search(rf'^{name}=(\d+)', src, re.M)
            self.assertIsNotNone(m, f'doctor 里没有 {name}')
            self.assertEqual(m.group(1), str(want), f'{name} 与 statusline-command.sh 不一致')

    def test_token_field_count_matches_tokens_module(self):
        with open(DOCTOR, encoding='utf-8') as f:
            m = re.search(r'^TOK_FIELDS=(\d+)', f.read(), re.M)
        self.assertIsNotNone(m, 'doctor 里没有 TOK_FIELDS')
        self.assertEqual(int(m.group(1)), len(TOKENS.FIELDS))

    def test_theme_and_style_lists_match_theme_module(self):
        """doctor 的白名单一旦落后于 statusline-theme.py，合法配置会被报成「值不合法」。"""
        with open(DOCTOR, encoding='utf-8') as f:
            src = f.read()
        m = re.search(r'theme\) case "\$v" in ([\w|]+)\)', src)
        self.assertIsNotNone(m, '没找到 theme 白名单')
        self.assertEqual(set(m.group(1).split('|')), {'auto'} | set(THEME.THEMES))
        m = re.search(r'pill_style\) case "\$v" in ([\w|-]+)\)', src)
        self.assertIsNotNone(m, '没找到 pill_style 白名单')
        self.assertEqual(set(m.group(1).split('|')), set(THEME.STYLES))

    def test_official_url_list_matches_renderer(self):
        """中转站判定用的「官方地址」白名单必须与渲染脚本逐字一致。"""
        pat = re.compile(r"''\|((?:https://[^\s|)]+)(?:\|https://[^\s|)]+)*)\)")
        with open(DOCTOR, encoding='utf-8') as f:
            doc = pat.search(f.read())
        with open(os.path.join(SRC, 'statusline-command.sh'), encoding='utf-8') as f:
            src = pat.search(f.read())
        self.assertIsNotNone(doc, 'doctor 里没找到官方地址白名单')
        self.assertIsNotNone(src, '渲染脚本里没找到官方地址白名单')
        self.assertEqual(doc.group(1), src.group(1))

    def test_bash_min_matches_renderer_guard(self):
        with open(os.path.join(SRC, 'statusline-command.sh'), encoding='utf-8') as f:
            src = f.read()
        major = re.search(r'BASH_VERSINFO\[0\]:-0\}" -lt (\d+)', src)
        minor = re.search(r'BASH_VERSINFO\[1\]\}" -lt (\d+)', src)
        self.assertIsNotNone(major)
        self.assertIsNotNone(minor)
        with open(DOCTOR, encoding='utf-8') as f:
            self.assertIn(f'BASH_MIN={major.group(1)}.{minor.group(1)}', f.read())

    def test_theme_cache_key_matches_renderer(self):
        """让真实渲染脚本写一次色板缓存，doctor 必须找到同一个文件名（不是测试自己编的名字）。"""
        self.sb.install()
        os.makedirs(self.sb.cache, exist_ok=True)
        os.chmod(self.sb.cache, 0o700)
        subprocess.run([BASE_BASH, os.path.join(SRC, 'statusline-command.sh'), '--refresh',
                        self.sb.d, '-', 'doctor', 'sub'],
                       env=dict(self.sb.env, CLAUDE_SL_TMP=self.sb.cache, TERM_PROGRAM='wezterm'),
                       capture_output=True, timeout=60)
        self.assertIn('claude-sl-theme-term-wezterm.cache', os.listdir(self.sb.cache))
        r = self.sb.run(env={'TERM_PROGRAM': 'wezterm'})
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'PASS')

    def test_config_keys_match_parser(self):
        with open(DOCTOR) as f:
            doc = re.search(r'^CONF_KEYS="([^"]*)"', f.read(), re.M)
        self.assertIsNotNone(doc, 'doctor 里没有 CONF_KEYS')
        keys = set(doc.group(1).split())
        with open(os.path.join(SRC, 'statusline-command.sh')) as f:
            src = f.read()
        block = src[src.index('SL_CONF='):src.index('done < "$SL_CONF"')]
        arms = set()
        for line in block.splitlines():
            m = re.match(r'^ {6}([a-z_]+(?:\|[a-z_]+)*)\)', line)
            if m:
                arms.update(m.group(1).split('|'))
        self.assertEqual(keys, arms, 'CONF_KEYS 必须与配置解析器的 case 分支一致')

    def test_no_bare_var_before_multibyte(self):
        """`$var` 紧跟多字节字符时，bash 会把首字节吞进变量名（渲染脚本约束 6 同一条坑）。
        在 zh_CN.UTF-8 + bash 3.2 上会直接 unbound variable，所以一律写 ${var}。"""
        with open(DOCTOR, encoding='utf-8') as f:
            lines = f.read().splitlines()
        bad = [f'{i}: {l.strip()}' for i, l in enumerate(lines, 1)
               if not l.lstrip().startswith('#')
               and re.search(r'\$[A-Za-z_0-9][A-Za-z0-9_]*[^\x00-\x7f]', l)]
        self.assertEqual(bad, [], '这些行要改成 ${var} 写法')

    def test_runs_under_system_bash(self):
        """macOS 自带 /bin/bash 是 3.2，症状最重的环境里诊断必须还能跑。"""
        sysbash = '/bin/bash'
        if not os.path.exists(sysbash):
            self.skipTest('没有 /bin/bash')
        v = subprocess.run([sysbash, '-c', 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
                           capture_output=True, text=True).stdout.strip()
        if not v.startswith('3.'):
            self.skipTest(f'/bin/bash 是 {v}，不是 3.2')
        sb = DoctorSandbox()
        self.addCleanup(sb.close)
        r = subprocess.run([sysbash, DOCTOR], capture_output=True, text=True, env=sb.env)
        self.assertNotIn('unbound variable', r.stderr)
        self.assertIn('汇总', r.stdout)

    def test_points_to_agrees_with_install_check(self):
        """doctor 与 install.sh --check 对同一组命令写法必须给出同一结论。

        两处各有一份 points_to（install.sh 不能 source），判据一旦漂移，用户会得到
        「安装器说没问题、诊断说没指向」这种自相矛盾的结果。
        """
        self.sb.install(subagent=False, settings=False)
        dest = self.sb.dest
        forms = [
            f'bash {dest}/statusline-command.sh',
            'bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh"',
            'bash "$HOME/.claude/statusline-command.sh"',
            'bash "~/.claude/statusline-command.sh"',
            'bash /elsewhere/statusline-command.sh',
            'statusline-command.sh',
            f'bash {dest}/statusline-command.sh.bak',
            '',
        ]
        for cmd in forms:
            self.sb.write_settings({'statusLine': {'type': 'command', 'command': cmd}})
            d = self.sb.run()
            chk = subprocess.run([BASE_BASH, INSTALL, '--check'], capture_output=True,
                                 text=True, env=self.sb.env)
            self.assertEqual(chk.returncode == 0, self.sb.status(d, '安装/statusLine') == 'PASS',
                             f'写法 {cmd!r}：install --check rc={chk.returncode}，'
                             f'doctor={self.sb.status(d, "安装/statusLine")}\n{chk.stdout}')

    def test_invalid_step_is_flagged(self):
        """渲染器会静默忽略写错的 step；配置检查必须指出来（合法项名从渲染脚本的 ITEM_IDS 取）。"""
        self.sb.install()
        conf = os.path.join(self.sb.cfg, 'claude-statusline.conf')
        with open(conf, 'w') as f:
            f.write('step = drop cot\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/配置文件'), 'WARN')
        self.assertIn('cot', self.sb.detail(r, '安装/配置文件'))
        with open(conf, 'w') as f:
            f.write('step = drop api\nstep = shrink model\n')
        self.assertEqual(self.sb.status(self.sb.run(), '安装/配置文件'), 'PASS')

    def test_lock_path_occupied_by_file_fails(self):
        """普通文件占住锁路径时 mkdir 永远失败，后台刷新每轮直接退出，不会自愈。"""
        self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-lock-doctor'), 'w') as f:
            f.write('x')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'FAIL')
        self.assertIn('mkdir', self.sb.detail(r, '缓存/锁'))

    def test_credentials_in_base_url_are_stripped(self):
        self.sb.healthy(relay=True)
        r = self.sb.run(env={'ANTHROPIC_BASE_URL': 'https://user:pass@relay.example/v1'})
        self.assertNotIn('user:pass', r.stdout)
        self.assertIn('relay.example', r.stdout)

    def test_flags_have_behaviour(self):
        """--transcript / --dir 不能只出现在 help 里。"""
        self.sb.install()
        r = self.sb.run('--transcript', os.path.join(self.sb.d, 'nope.jsonl'))
        self.assertEqual(self.sb.status(r, '数据/transcript'), 'FAIL')
        p = os.path.join(self.sb.d, 's.jsonl')
        with open(p, 'w') as f:
            f.write('{"x":1}\n')
        self.assertEqual(self.sb.status(self.sb.run('--transcript', p), '数据/transcript'), 'PASS')
        r = self.sb.run('--dir', os.path.join(self.sb.d, 'missing'))
        self.assertEqual(self.sb.status(r, '缓存/git'), 'WARN')
        self.assertEqual(self.sb.status(self.sb.run('--dir', REPO), '缓存/git'), 'PASS')

    def test_render_path_never_calls_doctor(self):
        for name in ('statusline-command.sh', 'subagent-statusline.sh'):
            with open(os.path.join(SRC, name)) as f:
                self.assertNotIn('doctor', f.read(), f'{name} 不得引用 doctor')

    def test_help(self):
        r = self.sb.run('--help')
        self.assertEqual(r.returncode, 0)
        for s in ('--dir', '--transcript', '--doctor'):
            self.assertIn(s, r.stdout)

    def test_unknown_arg_exits_2(self):
        self.assertEqual(self.sb.run('--nope').returncode, 2)


class InstallAlias(unittest.TestCase):
    """install.sh --doctor 是别名：同一实现，不重复逻辑。"""

    def test_install_doctor_delegates(self):
        sb = DoctorSandbox()
        self.addCleanup(sb.close)
        r = subprocess.run([BASE_BASH, INSTALL, '--doctor', '--help'], capture_output=True,
                           text=True, env=sb.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn('--transcript', r.stdout)


if __name__ == '__main__':
    unittest.main()
