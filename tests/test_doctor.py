"""tools/doctor.sh：故障场景 → PASS / WARN / FAIL / SKIP / UNKNOWN。

每个用例都在临时 HOME / CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME / TMPDIR 里跑：不碰线上安装、
不碰用户缓存、不读真实 CC Switch 库、不联网。依赖缺失与版本过低用临时 PATH 里的假可执行
文件模拟。先建一份「健康」安装，再逐项破坏——诊断工具最常见的毛病是误报，所以健康态
必须零 FAIL 也是用例之一。
"""
import glob
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

from _util import REPO, THEME, TOKENS, tok_line

DOCTOR = os.path.join(REPO, 'tools', 'doctor.sh')
DOCTOR_LIB = os.path.join(REPO, 'tools', 'doctor')
INSTALL = os.path.join(REPO, 'install.sh')
SRC = os.path.join(REPO, 'src')

def doctor_sources():
    """入口 + 四组检查模块：契约测试要覆盖全部源码。"""
    return [DOCTOR] + sorted(glob.glob(os.path.join(DOCTOR_LIB, '*.sh')))
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
STATUS_STALE = int(read_const('STATUS_STALE'))
LOCK_STALE = int(read_const('LOCK_STALE'))
CACHE_MAX_AGE = int(read_const('CACHE_MAX_AGE'))
PANEL_FAIL_STALE = int(read_const('PANEL_FAIL_STALE', DOCTOR))
FACTS_V = read_const('FACTS_V', DOCTOR)
# 面板写回失败的原因码 → doctor 文案里的关键词（集合由契约测试与两边源码对齐）
PANEL_FAIL_TEXT = {'busy': '抢锁超时', 'merge': '状态合并失败', 'tmp': '临时文件创建失败',
                   'write': '写入失败', 'mv': '替换失败'}


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

    def set_installed(self, body, name='statusline-command.sh'):
        """换掉已安装副本。body 是新内容，或「原文 → 新内容」的函数。"""
        p = os.path.join(self.dest, name)
        if callable(body):
            with open(p) as f:
                body = body(f.read())
        os.remove(p)
        with open(p, 'w') as f:
            f.write(body)

    def render_stub(self, body):
        """已安装副本换成「能正常自述（--facts），渲染却是 body」的脚本：冒烟只跑答得出自述的
        副本（答不出的不执行），所以占位 / 透传这类坏渲染要挂在真实的自述分支后面才测得到。"""
        def stub(src):
            i = src.index('if [ "${1:-}" = --facts ]')
            end = src.index('\nfi\n', i) + 4
            return src[:end] + re.sub(r'\A#!.*\n', '', body)
        self.set_installed(stub)

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
                f.write(tok_line(hit='98.0', msgs='12'))
            ts = os.path.join(self.cache, 'claude-sl-tok-doctor.cache.ts')
            with open(ts, 'w') as f:
                f.write(f'{int(time.time() - tok_age)}\n')
            for q in (p, ts):
                os.chmod(q, 0o600)
        return self.cache

    def make_ccswitch(self, rows=(), columns='full'):
        """建 CC Switch 库；rows=(id, app_type, is_current, [limit, [meta]])。

        columns: full（含 meta）/ no_meta（旧库缺 meta 列）/ minimal（只到 is_current）/
        None（不建 providers 表）。代理日志表只被 COUNT(*)，不需要具体列。
        """
        db = os.path.join(self.home, '.cc-switch', 'cc-switch.db')
        os.makedirs(os.path.dirname(db), exist_ok=True)
        if os.path.exists(db):
            os.unlink(db)
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE proxy_request_logs (x)')
        if columns:
            cols = {
                'full': 'id TEXT, app_type TEXT, is_current INTEGER, limit_daily_usd REAL, meta TEXT',
                'no_meta': 'id TEXT, app_type TEXT, is_current INTEGER, limit_daily_usd REAL',
                'minimal': 'id TEXT, app_type TEXT, is_current INTEGER',
            }[columns]
            con.execute(f'CREATE TABLE providers ({cols})')
            for row in rows:
                con.execute('INSERT INTO providers VALUES (' + ','.join('?' * len(row)) + ')', row)
        con.commit()
        con.close()
        return db

    def healthy(self, relay=False, tok_age=0):
        """健康态：装好副本、settings 两个键、私有缓存目录、新鲜色板与 token 缓存。"""
        self.install()
        line = f'THEME=auto/12/tint/v{PALETTE_V}'
        self.make_cache(theme_line=line, tok_age=tok_age)
        env = {}
        if relay:
            env['ANTHROPIC_BASE_URL'] = 'https://relay.example/v1'
        return env

    def panel_path(self, suffix):
        """面板状态文件的伴生路径：suffix 为 .lock / .fail。"""
        return os.path.join(self.cache, f'claude-subagent-sl-{os.getuid()}.json{suffix}')

    def make_panel_fail(self, reason='busy', age=0, line=None):
        """写面板的写回失败标记（单行 "epoch 原因码"）：age 把时间戳拨回去，line 直接给整行。"""
        p = self.panel_path('.fail')
        with open(p, 'w') as f:
            f.write(line if line is not None else f'{int(time.time()) - age} {reason}\n')
        return p

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
        self.sb.healthy()                     # 缓存根存在，确保走的是版本分支而不是「根不可用」
        b = self.sb.minimal_path()
        self.sb.fake(b, 'python3', '#!/bin/sh\necho "Python 3.7.9"\n')
        r = self.sb.run(env={'PATH': b})
        self.assertEqual(self.sb.status(r, '环境/python3'), 'FAIL')
        self.assertIn('3.9', self.sb.detail(r, '环境/python3'))
        self.assertEqual(self.sb.status(r, '缓存/后台刷新'), 'SKIP')
        self.assertIn('3.7.9', self.sb.detail(r, '缓存/后台刷新'))

    def test_low_bash_is_reported(self):
        """PATH 里是 3.2 时必须报出来。本机若另装了 ≥4.2 的 bash，脚本的换壳守卫能接管，
        此时正确结论是 WARN；没有替代才是 FAIL——两种都算「查到了」，不能一律 FAIL。"""
        self.sb.install()
        b = self.sb.minimal_path()
        self.sb.fake(b, 'bash', '#!/bin/sh\n'
                                'case "$1" in -c) echo "3.2";; *) echo "GNU bash, version 3.2.57(1)-release";; esac\n')
        r = self.sb.run(env={'PATH': b})
        alt = False
        for p in ('/opt/homebrew/bin/bash', '/usr/local/bin/bash', '/usr/bin/bash'):
            if os.access(p, os.X_OK):
                v = subprocess.run([p, '-c', 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
                                   capture_output=True, text=True).stdout.strip()
                try:
                    major, minor = (int(x) for x in v.split('.'))
                except ValueError:
                    continue
                if (major, minor) >= (4, 2):
                    alt = True
                    break
        self.assertEqual(self.sb.status(r, '环境/bash'), 'WARN' if alt else 'FAIL', r.stdout)
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

    def test_unreadable_settings_reports_permission_not_syntax(self):
        """chmod 000 是读不到，不是 JSON 写错；原因与修复建议必须对得上。"""
        self.sb.install()
        sj = os.path.join(self.sb.dest, 'settings.json')
        os.chmod(sj, 0)
        self.addCleanup(os.chmod, sj, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/settings.json'), 'FAIL')
        d = self.sb.detail(r, '安装/settings.json')
        self.assertIn('不可读', d)
        self.assertIn('chmod', r.stdout)
        self.assertNotIn('JSON', d)

    def test_statusline_pointing_at_missing_file_is_fail(self):
        self.sb.install()
        self.sb.write_settings({'statusLine': {'type': 'command',
                                               'command': 'bash /nonexistent/statusline-command.sh'}})
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/statusLine'), 'FAIL')

    def test_bare_filename_is_not_accepted(self):
        """只有文件名、路径不明：install.sh --check 也不认，两边判断必须一致。"""
        self.sb.install(subagent=False, settings=False)
        self.assertEqual(self._points_to_verdict('statusline-command.sh'), 'FAIL')

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

    def test_cache_root_symlink_is_fail(self):
        """渲染不跟随符号链接：根是链接时整轮放弃写缓存，必须当故障报出来。"""
        self.sb.install()
        target = os.path.join(self.sb.d, 'target')
        os.makedirs(target)
        os.symlink(target, self.sb.cache)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/根'), 'FAIL')
        self.assertIn('符号链接', self.sb.detail(r, '缓存/根'))

    def test_symlinked_token_cache_warns(self):
        self.sb.healthy()
        cache = os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache')
        real = os.path.join(self.sb.d, 'real-tok')
        shutil.move(cache, real)
        os.symlink(real, cache)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('符号链接', self.sb.detail(r, '缓存/token'))

    def test_panel_state_lives_in_cache_root(self):
        """面板状态与主栏共用缓存根：路径必须与 subagent-statusline.sh 的推导一致。"""
        self.sb.healthy()
        st = os.path.join(self.sb.cache, f'claude-subagent-sl-{os.getuid()}.json')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板状态'), 'SKIP')   # 还没渲染过
        with open(st, 'w') as f:
            f.write('{}')
        os.chmod(st, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板状态'), 'PASS', r.stdout)

    def test_panel_state_loose_mode_warns(self):
        self.sb.healthy()
        st = os.path.join(self.sb.cache, f'claude-subagent-sl-{os.getuid()}.json')
        with open(st, 'w') as f:
            f.write('{}')
        os.chmod(st, 0o644)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板状态'), 'WARN')
        self.assertIn('chmod 600', r.stdout)

    def test_panel_state_symlink_warns(self):
        self.sb.healthy()
        st = os.path.join(self.sb.cache, f'claude-subagent-sl-{os.getuid()}.json')
        real = os.path.join(self.sb.d, 'panel.json')
        with open(real, 'w') as f:
            f.write('{}')
        os.symlink(real, st)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板状态'), 'WARN')
        self.assertIn('符号链接', self.sb.detail(r, '缓存/面板状态'))

    def test_panel_state_skipped_when_explicit(self):
        self.sb.healthy()
        r = self.sb.run(env={'CLAUDE_SUBAGENT_SL_STATE': os.path.join(self.sb.d, 'own.json')})
        self.assertEqual(self.sb.status(r, '缓存/面板状态'), 'SKIP')
        self.assertIn('CLAUDE_SUBAGENT_SL_STATE', self.sb.detail(r, '缓存/面板状态'))

    def test_panel_lock_occupied_by_file_warns(self):
        """结构问题（锁路径被非目录占住）当场判，与写回是否失败无关；面板每轮先清掉再抢锁、能自愈，
        所以是 WARN 而不是 FAIL（与主栏刷新锁同一条，11-22 用户裁定）。"""
        self.sb.healthy()
        with open(self.sb.panel_path('.lock'), 'w') as f:
            f.write('x')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'WARN')
        self.assertIn('普通文件或符号链接', self.sb.detail(r, '缓存/面板锁'))

    def test_panel_lock_no_marker_passes(self):
        self.sb.healthy()
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'PASS')
        self.assertIn('无写回失败记录', self.sb.detail(r, '缓存/面板锁'))

    def test_panel_lock_fresh_marker_decodes_every_reason(self):
        """新标记：每个原因码都有自己的文案，不认识的码报「未知原因」；说明会自动清除。"""
        self.sb.healthy()
        for code, text in dict(PANEL_FAIL_TEXT, bogus='未知原因').items():
            with self.subTest(code=code):
                self.sb.make_panel_fail(code)
                r = self.sb.run()
                self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'WARN')
                d = self.sb.detail(r, '缓存/面板锁')
                self.assertIn(f'原因：{text}', d)
                self.assertIn('下次成功写回会自动清除', d)

    def test_panel_lock_stale_marker_means_stopped(self):
        """标记超过 PANEL_FAIL_STALE = 此后没有写回尝试：文案换成「可能已停跑」。"""
        self.sb.healthy()
        for age, stopped in ((PANEL_FAIL_STALE - 60, False), (PANEL_FAIL_STALE + 60, True)):
            with self.subTest(age=age):
                self.sb.make_panel_fail('write', age=age)
                r = self.sb.run()
                self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'WARN')
                self.assertEqual('可能已停跑' in self.sb.detail(r, '缓存/面板锁'), stopped)

    def test_panel_lock_marker_not_regular_file_warns(self):
        """标记是符号链接或目录：不读内容，报出来并给清理命令。"""
        for kind in ('symlink', 'dir'):
            with self.subTest(kind=kind):
                self.sb.healthy()
                p = self.sb.panel_path('.fail')
                if kind == 'symlink':
                    os.symlink('/nonexistent-marker', p)
                else:
                    os.makedirs(p)
                r = self.sb.run()
                (os.unlink if kind == 'symlink' else os.rmdir)(p)
                self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'WARN')
                self.assertIn('不是普通文件', self.sb.detail(r, '缓存/面板锁'))

    def test_panel_lock_odd_marker_does_not_abort(self):
        """标记读不出（权限 000）、时间戳带前导 0（当八进制会算术报错）或写在未来：
        doctor 必须跑完整轮、这一行照常出现、不漏 stderr，年龄不为负。"""
        cases = {'unreadable': None, 'leading_zero': '08 busy\n',
                 'future': f'{int(time.time()) + 5000} busy\n'}
        for name, line in cases.items():
            with self.subTest(case=name):
                self.sb.healthy()
                p = self.sb.make_panel_fail('busy', line=line)
                if line is None:
                    os.chmod(p, 0o000)
                r = self.sb.run()
                os.chmod(p, 0o600)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('汇总', r.stdout, '诊断提前中断')
                self.assertEqual(r.stderr, '', f'不该有 stderr 泄漏：{r.stderr!r}')
                self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'WARN')
                self.assertRegex(self.sb.detail(r, '缓存/面板锁'), r'[（ ]\d+s 前',
                                 '年龄应是非负整数')

    def test_panel_lock_skipped_with_explicit_state(self):
        self.sb.healthy()
        r = self.sb.run(env={'CLAUDE_SUBAGENT_SL_STATE': os.path.join(self.sb.d, 'own.json')})
        self.assertEqual(self.sb.status(r, '缓存/面板锁'), 'SKIP')

    def test_explicit_tmp_missing_fails(self):
        """显式 CLAUDE_SL_TMP 不存在时渲染不会创建，不能报「首次刷新会创建」。"""
        self.sb.install()
        r = self.sb.run(env={'CLAUDE_SL_TMP': os.path.join(self.sb.d, 'missing-slt')})
        self.assertEqual(self.sb.status(r, '缓存/根'), 'FAIL')
        self.assertIn('CLAUDE_SL_TMP', self.sb.detail(r, '缓存/根'))

    def test_loose_cache_dir_mode_warns(self):
        self.sb.healthy()
        os.chmod(self.sb.cache, 0o755)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/权限'), 'WARN')
        self.assertIn('chmod 700', r.stdout)

    def test_read_only_cache_root_is_fail(self):
        """缓存目录不可写 → 后台刷新永远落不了盘，必须 FAIL（渲染本身仍能读旧缓存）。"""
        self.sb.healthy()
        os.chmod(self.sb.cache, 0o500)
        try:
            r = self.sb.run()
            self.assertEqual(self.sb.status(r, '缓存/后台刷新'), 'FAIL')
            self.assertIn('不可写', self.sb.detail(r, '缓存/后台刷新'))
        finally:
            os.chmod(self.sb.cache, 0o700)

    def test_stale_token_cache_warns(self):
        env = self.sb.healthy(tok_age=TOK_STALE + 60)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')

    def session_state(self, transcript_age=None, body=None):
        """给 doctor 会话写一份会话状态：记一个 transcript_age 秒前写过的 transcript，或原样写 body。"""
        if body is None:
            tp = os.path.join(self.sb.home, 'proj', 'doctor.jsonl')
            os.makedirs(os.path.dirname(tp), exist_ok=True)
            with open(tp, 'w') as f:
                f.write('{}\n')
            t = time.time() - transcript_age
            os.utime(tp, (t, t))
            body = json.dumps({'v': 9, 'files': {tp: {'off': 3, 'fp': 0}}})
        with open(os.path.join(self.sb.cache, 'claude-statusline-doctor.json'), 'w') as f:
            f.write(body)

    def old_cache_without_ts(self):
        """从旧版本迁移来、还没回填时间戳的 token 缓存（mtime 已超过 TOK_STALE）。"""
        self.sb.install()
        self.sb.make_cache(tok_age=0)
        tok = os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache')
        os.unlink(tok + '.ts')
        old = time.time() - TOK_STALE - 600
        os.utime(tok, (old, old))

    def test_stale_cache_of_idle_session_passes(self):
        """会话已结束（transcript 也超过 TOK_STALE 没写过）时缓存陈旧是常态，不是故障：不报 WARN，
        只在 PASS 里点一句（此前每次 doctor 都因已结束的会话报 WARN）。"""
        env = self.sb.healthy(tok_age=TOK_STALE + 60)
        self.session_state(transcript_age=TOK_STALE + 50)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'PASS')
        self.assertIn('闲置', self.sb.detail(r, '缓存/token'))

    def test_stale_cache_of_active_session_warns(self):
        """transcript 近 TOK_STALE 内还在写、缓存却超过 TOK_STALE：后台没跟上，照报 WARN。"""
        env = self.sb.healthy(tok_age=TOK_STALE + 60)
        self.session_state(transcript_age=30)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('TOK_STALE', self.sb.detail(r, '缓存/token'))

    def test_stale_cache_with_unreadable_session_state_warns(self):
        """会话状态读不出（损坏）、files 不是对象或一条路径都没记：判断不了是否闲置，照报 WARN，
        文案不能断言「transcript 有写入」。"""
        for body in ('not json\n', '{"v": 9, "files": {}}', '{"v": 9, "files": ["/x.jsonl"]}'):
            with self.subTest(body=body):
                env = self.sb.healthy(tok_age=TOK_STALE + 60)
                self.session_state(body=body)
                r = self.sb.run(env=env)
                self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
                self.assertIn('无法判断', self.sb.detail(r, '缓存/token'))

    def stale_cache(self, sid, transcript_age):
        """一份超过 TOK_STALE 的 token 缓存，带 tp= 行（1.0.7 起生产者写），指向 transcript_age 秒前写过的
        主 transcript；返回该 transcript 路径。"""
        tp = os.path.join(self.sb.home, 'proj', f'{sid}.jsonl')
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        with open(tp, 'w') as f:
            f.write('{}\n')
        t = time.time() - transcript_age
        os.utime(tp, (t, t))
        p = os.path.join(self.sb.cache, f'claude-sl-tok-{sid}.cache')
        with open(p, 'w') as f:
            f.write(tok_line(hit='98.0', msgs='12', tp=tp))
        with open(p + '.ts', 'w') as f:
            f.write(f'{int(time.time() - TOK_STALE - 60)}\n')
        return tp

    def test_tp_line_decides_activity(self):
        """缓存里有 tp= 时按它判活跃，不再去读会话状态（状态里说活跃也以 tp= 为准）。"""
        env = self.sb.healthy(tok_age=None)
        self.session_state(transcript_age=30)                    # 会话状态里的 transcript 刚写过……
        os.rename(os.path.join(self.sb.home, 'proj', 'doctor.jsonl'),
                  os.path.join(self.sb.home, 'proj', 'sub.jsonl'))
        self.session_state(body=json.dumps({'v': 9, 'files': {
            os.path.join(self.sb.home, 'proj', 'sub.jsonl'): {'off': 3}}}))
        self.stale_cache('doctor', transcript_age=TOK_STALE + 50)  # ……tp= 指的主 transcript 早已不写
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'PASS')
        self.assertIn('闲置', self.sb.detail(r, '缓存/token'))
        self.stale_cache('doctor', transcript_age=30)
        self.assertEqual(self.sb.status(self.sb.run(env=env), '缓存/token'), 'WARN')

    def test_several_stale_sessions_are_judged_separately(self):
        """多个陈旧缓存的 transcript 一次 stat 取完，结论仍逐会话对应：会话 a 无 tp=、回落会话状态里的两个
        旧文件（闲置），会话 b 的 tp= 刚写过（活跃）→ 只报 1 个。两会话路径数不同，取错下标就会报错。"""
        env = self.sb.healthy(tok_age=None)
        old = [self.stale_cache(n, transcript_age=TOK_STALE + 50) for n in ('a1', 'a2')]
        for n in ('a1', 'a2'):
            os.unlink(os.path.join(self.sb.cache, f'claude-sl-tok-{n}.cache'))
            os.unlink(os.path.join(self.sb.cache, f'claude-sl-tok-{n}.cache.ts'))
        p = os.path.join(self.sb.cache, 'claude-sl-tok-a.cache')
        with open(p, 'w') as f:
            f.write(tok_line(hit='98.0'))
        with open(p + '.ts', 'w') as f:
            f.write(f'{int(time.time() - TOK_STALE - 60)}\n')
        with open(os.path.join(self.sb.cache, 'claude-statusline-a.json'), 'w') as f:
            json.dump({'v': 9, 'files': {q: {'off': 3} for q in old}}, f)
        self.stale_cache('b', transcript_age=30)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('有 1 个缓存', self.sb.detail(r, '缓存/token'))

    def test_deleted_transcript_means_idle(self):
        """tp= 指的 transcript 已删：会话已结束，陈旧不报 WARN。"""
        env = self.sb.healthy(tok_age=None)
        os.unlink(self.stale_cache('gone', transcript_age=30))
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'PASS')
        self.assertIn('闲置', self.sb.detail(r, '缓存/token'))

    def test_idle_session_without_ts_passes(self):
        """无 .ts 的旧缓存同理：会话已闲置时回填后隐藏派生项也无所谓，不报 WARN。"""
        self.old_cache_without_ts()
        self.session_state(transcript_age=TOK_STALE + 600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/token'), 'PASS')

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

    def test_old_cache_without_ts_reports_round_then_refresh(self):
        """无 .ts 的旧缓存本轮仍沿用，但刷新按 mtime 回填 .ts 后下一轮会隐藏派生项。"""
        self.old_cache_without_ts()
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        d = self.sb.detail(r, '缓存/token')
        self.assertIn('沿用', d)
        self.assertIn('回填', d)
        self.assertIn('隐藏', d)

    def test_fresh_status_cache_passes(self):
        self.sb.healthy()
        p = os.path.join(self.sb.cache, 'claude-sl-status.cache')
        with open(p, 'w') as f:
            f.write(f'{int(time.time())}\tminor\n')
        os.chmod(p, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'PASS')

    def test_stale_status_cache_warns(self):
        self.sb.healthy()
        p = os.path.join(self.sb.cache, 'claude-sl-status.cache')
        with open(p, 'w') as f:
            f.write(f'{int(time.time()) - STATUS_STALE - 60}\tminor\n')
        os.chmod(p, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'WARN')
        self.assertIn('隐藏', self.sb.detail(r, '缓存/状态页'))

    def test_corrupt_status_cache_warns(self):
        self.sb.healthy()
        p = os.path.join(self.sb.cache, 'claude-sl-status.cache')
        with open(p, 'w') as f:
            f.write('garbage\n')
        os.chmod(p, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'WARN')
        self.assertIn('损坏', self.sb.detail(r, '缓存/状态页'))

    def test_status_page_off_skips_cache_check(self):
        self.sb.healthy()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('status_page = off\n')
        p = os.path.join(self.sb.cache, 'claude-sl-status.cache')
        with open(p, 'w') as f:
            f.write(f'{int(time.time()) - STATUS_STALE - 60}\tminor\n')
        os.chmod(p, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'SKIP')

    def test_status_cache_directory_warns(self):
        self.sb.healthy()
        os.makedirs(os.path.join(self.sb.cache, 'claude-sl-status.cache'))
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'WARN')
        self.assertIn('普通文件', self.sb.detail(r, '缓存/状态页'))

    def test_status_cache_without_indicator_warns(self):
        self.sb.healthy()
        p = os.path.join(self.sb.cache, 'claude-sl-status.cache')
        with open(p, 'w') as f:
            f.write(f'{int(time.time())}\t\n')
        os.chmod(p, 0o600)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/状态页'), 'WARN')
        self.assertIn('indicator', self.sb.detail(r, '缓存/状态页'))

    def test_token_cache_in_another_format_is_flagged(self):
        """首行不是当前格式版本的缓存（旧版留下的、被截断的）渲染侧整份不读，不能只看时间戳。"""
        env = self.sb.healthy()
        for body in ('-\t-\t-\n', 'TOK=999\nhit=98.0\n', ''):
            with open(os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache'), 'w') as f:
                f.write(body)
            r = self.sb.run(env=env)
            self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN', repr(body))
            self.assertIn('格式', self.sb.detail(r, '缓存/token'))

    def test_corrupt_ts_is_reported_as_stale(self):
        """渲染侧把非数字 .ts 判为陈旧；doctor 按 .ts 的 mtime 回退会漏报，必须同样判陈旧。"""
        env = self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache.ts'), 'w') as f:
            f.write('garbage\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('时间戳', self.sb.detail(r, '缓存/token'))

    def test_dangling_ts_symlink_is_reported_as_stale(self):
        """渲染侧用 -e || -L 认 .ts：悬空链接也判陈旧；doctor 只看 -e 会漏报成 PASS。"""
        env = self.sb.healthy()
        ts = os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache.ts')
        os.unlink(ts)
        os.symlink('/nonexistent-ccpills-target', ts)
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

    def test_config_path_directory_warns(self):
        """配置路径是目录时重定向会失败、配置被忽略；不能因为 -r 通过就报 PASS。"""
        self.sb.healthy()
        conf = os.path.join(self.sb.cfg, 'claude-statusline.conf')
        if os.path.exists(conf):
            os.unlink(conf)
        os.makedirs(conf)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/配置文件'), 'WARN')
        self.assertIn('普通文件', self.sb.detail(r, '安装/配置文件'))

    def test_facts_come_from_installed_copy_first(self):
        """诊断回答的是「装着的那一版会怎么做」：已安装副本能自述（--facts）就以它为准，
        答不出（旧版、被换成别的脚本）才问仓库里的源文件。"""
        self.sb.healthy()

        def nord(src):
            self.assertEqual(src.count('\nTHEME=auto '), 1)
            return src.replace('\nTHEME=auto ', '\nTHEME=nord ')
        self.sb.set_installed(nord)
        self.assertIn('不能上色', self.sb.detail(self.sb.run(), '缓存/色板'), '没按已安装副本的 theme=nord 判读')
        self.sb.set_installed('#!/bin/sh\necho hello\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'PASS', '已安装副本答不出时应回落到仓库源文件')
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'SKIP', '答不出自述的副本不执行，冒烟也不跑')

    def test_old_installed_copy_is_not_executed(self):
        """只读承诺：不认识 --facts 的旧副本会把它当普通参数、照常跑一遍（建缓存目录）。
        所以脚本里没有自述标记就不执行它，直接问仓库源文件，并在首行说明装的是旧版。冒烟也
        不跑它：这个文件名不专属于 cc-pills，也不能指望它认 CLAUDE_SL_TMP 这类隔离变量。"""
        self.sb.install()
        ran = os.path.join(self.sb.dest, 'ran')
        for name in ('statusline-command.sh', 'subagent-statusline.sh'):
            self.sb.set_installed('#!/usr/bin/env bash\nmkdir -p "$TMPDIR/claude-sl-$(id -u)"\n'
                                  f'touch "{ran}"\necho hello\n', name)
        r = self.sb.run()
        self.assertFalse(os.path.exists(self.sb.cache), '诊断执行了答不出 --facts 的旧副本')
        self.assertFalse(os.path.exists(ran), '诊断执行了答不出 --facts 的旧副本')
        self.assertEqual(self.sb.status(r, '缓存/根'), 'SKIP')
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'SKIP')
        self.assertIn('重装', self.sb.detail(r, '数据/stdin'))
        first = r.stdout.splitlines()[0]
        self.assertIn('旧版', first)
        self.assertIn('install.sh', first)

    def test_facts_of_another_version_are_not_used(self):
        """自述格式版本不认识（比诊断工具新或旧的副本）：它的键不能按这一版的含义读，问仓库源文件。"""
        self.sb.healthy()

        def other_version(src):
            self.assertEqual(src.count('cc-pills-facts=' + FACTS_V), 1)
            return src.replace('cc-pills-facts=' + FACTS_V, 'cc-pills-facts=999').replace('\nTHEME=auto ', '\nTHEME=nord ')
        self.sb.set_installed(other_version)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'PASS', '用了别的自述版本报出的 theme=nord')
        self.assertIn('仓库源文件', r.stdout.splitlines()[0])

    def test_old_copy_caches_are_not_judged_by_the_new_format(self):
        """装着旧版时磁盘上的 token 缓存是旧格式、旧渲染脚本照常读它：不能按仓库这一版的格式
        报「渲染侧整份不读」。"""
        env = self.sb.healthy()
        self.sb.set_installed('#!/usr/bin/env bash\necho hello\n')
        with open(os.path.join(self.sb.cache, 'claude-sl-tok-doctor.cache'), 'w') as f:
            f.write('118M\t736k\t$4.9/h\t1\n')
        r = self.sb.run(env=env)
        self.assertNotIn('格式', self.sb.detail(r, '缓存/token'))
        self.assertEqual(self.sb.status(r, '缓存/token'), 'PASS')

    def test_old_copy_palette_version_is_not_judged(self):
        """装着旧副本时色板缓存是它按自己的格式版本写的：只比主题 / 彩度 / 风格，不比版本。"""
        self.sb.install()
        self.sb.set_installed('#!/usr/bin/env bash\necho hello\n')
        self.sb.make_cache(theme_line=f'THEME=auto/12/tint/v{int(PALETTE_V) - 1}')
        self.assertEqual(self.sb.status(self.sb.run(), '缓存/色板'), 'PASS')

    def test_leftover_old_format_caches_do_not_hide_a_stale_one(self):
        """升级后已结束会话的旧格式缓存会留到被清理为止；其间活跃会话的陈旧告警不能被它盖住。"""
        env = self.sb.healthy(tok_age=TOK_STALE + 60)
        ended = os.path.join(self.sb.cache, 'claude-sl-tok-ended.cache')
        with open(ended, 'w') as f:
            f.write('118M\t736k\t-\t1\n')
        with open(ended + '.ts', 'w') as f:
            f.write(f'{int(time.time())}\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '缓存/token'), 'WARN')
        self.assertIn('TOK_STALE', self.sb.detail(r, '缓存/token'))

    def test_reports_installed_version(self):
        """提 issue 时要能说清装的是哪一版：首行给出已安装副本的版本；与仓库不同时两个都给。"""
        self.sb.install()
        first = self.sb.run().stdout.splitlines()[0]
        self.assertRegex(first, r'^cc-pills \d+\.\d+\.\d+ ')
        self.assertNotIn('仓库', first)
        self.sb.set_installed(lambda src: re.sub(r'(?m)^VERSION=\S+', 'VERSION=0.0.7', src, count=1))
        first = self.sb.run().stdout.splitlines()[0]
        self.assertIn('cc-pills 0.0.7 ', first)
        self.assertRegex(first, r'仓库 \d+\.\d+\.\d+')

    def test_config_last_line_without_newline_is_checked(self):
        """末行没有换行符也要解析：渲染侧会读它，诊断不能漏看这一行。"""
        self.sb.install()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('theme = dracula\nno_such_key = 1')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '安装/配置文件'), 'WARN')
        self.assertIn('no_such_key', self.sb.detail(r, '安装/配置文件'))

    def test_invalid_p_ctx_is_not_colourable(self):
        """渲染侧只接受 [0-9;] 的 P_CTX，非法值等于没有：此时整行无色，不能报 PASS。"""
        self.sb.install()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('theme = dracula\n')
        os.makedirs(self.sb.cache, exist_ok=True)
        os.chmod(self.sb.cache, 0o700)
        p = os.path.join(self.sb.cache, 'claude-sl-theme-term-other.cache')
        with open(p, 'w') as f:
            f.write(f'THEME=dracula/12/tint/v{PALETTE_V}\nNAME=dracula\nP_CTX=garbage\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'WARN')
        self.assertIn('无色', self.sb.detail(r, '缓存/色板'))

    def test_leftover_lock_passes(self):
        """超过 LOCK_STALE 的空锁不需要处理：活跃会话下一轮刷新就抢掉，已结束会话的由每日清理在 30 天后
        删掉——报 PASS 并说明，而不是一条永远不必处理的 WARN。"""
        self.sb.healthy()
        lock = os.path.join(self.sb.cache, 'claude-sl-lock-doctor')
        os.makedirs(lock)
        old = time.time() - LOCK_STALE - 60
        os.utime(lock, (old, old))
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'PASS')
        d = self.sb.detail(r, '缓存/锁')
        self.assertIn('残锁', d)
        self.assertIn('下次刷新时会清除', d)
        self.assertIn(f'{CACHE_MAX_AGE} 天', d)
        d = self.sb.detail(self.sb.run(env={'CLAUDE_SL_TMP': self.sb.cache}), '缓存/锁')
        self.assertNotIn(f'{CACHE_MAX_AGE} 天', d, '显式 CLAUDE_SL_TMP 不清理，不能说多少天后会删')
        self.assertIn('CLAUDE_SL_TMP', d)
        # 天数取自已安装副本的自述，不在诊断侧另抄；不报这一项的是不清理锁的旧版：不能说会删
        self.sb.set_installed(lambda src: src.replace('CACHE_MAX_AGE=30 ', 'CACHE_MAX_AGE=7 ', 1))
        self.assertIn('7 天', self.sb.detail(self.sb.run(), '缓存/锁'))
        self.sb.set_installed(lambda src: src.replace('"cache_max_age=$CACHE_MAX_AGE" ', '', 1))
        d = self.sb.detail(self.sb.run(), '缓存/锁')
        self.assertNotIn(' 天后', d)
        self.assertIn('rmdir', d)

    def test_backoff_does_not_claim_no_colour_when_theme_still_matches(self):
        """渲染侧只要求缓存的主题部分与当前 THEME 相同、且有 P_CTX 就上色；chroma / 风格 /
        版本不符只会触发后台重算。此时 .fail 存在也不能说「整行无色」。"""
        self.sb.install()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('theme = dracula\n')
        # 主题相同、格式版本旧：渲染侧照常上色
        self.sb.make_cache(theme_line=f'THEME=dracula/12/tint/v{PALETTE_V}old')
        with open(os.path.join(self.sb.cache, 'claude-sl-theme-term-other.cache.fail'), 'w') as f:
            f.write(f'{int(time.time())}\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/退避'), 'WARN')
        self.assertNotIn('整行无色', self.sb.detail(r, '缓存/退避'))

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
             'env': {'ANTHROPIC_BASE_URL': 'https://relay.example?token=SUPERSECRET123',
                     'ANTHROPIC_AUTH_TOKEN': 'tok_live_should_not_leak'}}
        self.sb.write_settings(s)
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        d = self.sb.detail(r, '数据/CCSwitch库')
        self.assertIn('settings.json', d)
        self.assertIn('ANTHROPIC_BASE_URL=relay.example', d)
        self.assertNotIn('SUPERSECRET123', r.stdout)
        self.assertNotIn('tok_live_should_not_leak', r.stdout)

    def test_relay_from_settings_env_beats_shell_official(self):
        """Claude Code 把 settings 的 env 注入状态栏进程；shell 里的旧官方地址不能盖掉它。"""
        self.sb.healthy()
        s = {'statusLine': {'type': 'command',
                            'command': f'bash {self.sb.dest}/statusline-command.sh'},
             'env': {'ANTHROPIC_BASE_URL': 'https://relay.example/v1'}}
        self.sb.write_settings(s)
        r = self.sb.run(env={'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'})
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        d = self.sb.detail(r, '数据/CCSwitch库')
        self.assertIn('relay.example', d)
        self.assertIn('settings.json', d)

    def test_db_is_not_opened_in_subscription_mode(self):
        """订阅模式渲染侧从不打开 CC Switch 库（tokens.py 约束 7），诊断也不该打开。"""
        self.sb.healthy()
        d = os.path.dirname(self.sb.make_ccswitch(columns=None))
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
        db = self.sb.make_ccswitch([('p1', 'claude', 1, 50, '{"limitDailyUsd": 50}')])
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
        self.sb.make_ccswitch(columns=None)
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('providers', self.sb.detail(r, '数据/CCSwitch库'))

    def test_db_without_limit_column_warns(self):
        """providers 缺 limit_daily_usd 列时渲染侧返回 (None, None)、隐藏日限额。"""
        env = self.sb.healthy(relay=True)
        self.sb.make_ccswitch([('p1', 'claude', 1)], columns='minimal')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('日限额', self.sb.detail(r, '数据/CCSwitch库'))

    def test_db_without_limit_is_skip(self):
        """供应商唯一但没有日限额：日限额项本来就不显示，属正常配置，不该报 WARN。"""
        env = self.sb.healthy(relay=True)
        self.sb.make_ccswitch([('p1', 'claude', 1, None, None)])
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'SKIP')
        self.assertIn('正常', self.sb.detail(r, '数据/CCSwitch库'))

    def test_db_with_invalid_limit_warns(self):
        """限额为 0 / 非数字时渲染侧按「未设置」处理，诊断不能说「口径可确定」。"""
        env = self.sb.healthy(relay=True)
        self.sb.make_ccswitch([('p1', 'claude', 1, 0, None)])
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/CCSwitch库'), 'WARN')
        self.assertIn('日限额', self.sb.detail(r, '数据/CCSwitch库'))

    def test_limit_state_matches_tokens_current_provider(self):
        """doctor 经 --provider-state 调用的探针与 TOKENS.current_provider 同源（limit_state）；
        这里锁的是 doctor 把探针结果映射成状态的那一层，以 current_provider 为 oracle。

        唯一必须锁死的语义是「PASS ⇔ 上游认为限额可用」；未设置与非法值都隐藏，
        至于报 SKIP 还是 WARN 是诊断自身的分级，不在这里约束。
        """
        env = self.sb.healthy(relay=True)
        cases = [
            ('full', '{"limitDailyUsd": 50}', 99),      # meta 压过旧列
            ('full', '{"limitDailyUsd": "abc"}', 50),   # 非法值不回落旧列
            ('full', '{}', 50),                         # 空对象回落旧列
            ('full', None, 50),                         # 没有 meta 回落旧列
            ('full', '[1]', 50),                        # meta 不是对象
            ('full', '{"limitDailyUsd": null}', 50),    # 明确写 null
            ('no_meta', None, 50),                      # 旧库没有 meta 列
        ]
        for columns, meta, col in cases:
            with self.subTest(meta=meta, col=col):
                row = ('p1', 'claude', 1, col) + ((meta,) if columns == 'full' else ())
                db = self.sb.make_ccswitch([row], columns=columns)
                ro = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
                try:
                    _, lim = TOKENS.current_provider(ro)
                finally:
                    ro.close()
                r = self.sb.run(env=env)
                st = self.sb.status(r, '数据/CCSwitch库')
                if lim is not None:
                    self.assertEqual(st, 'PASS', r.stdout)
                else:
                    self.assertNotEqual(st, 'PASS', r.stdout)

    def test_relay_without_unique_provider_warns(self):
        env = self.sb.healthy(relay=True)
        self.sb.make_ccswitch([])
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

    def test_smoke_detects_stub_that_only_echoes_pwd(self):
        """两份夹具必须在同目录里跑：否则只 printf "$PWD" 的脚本也会因 cwd 不同而「通过」。"""
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\nprintf "%s" "$PWD"\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('相同', self.sb.detail(r, '数据/stdin'))

    def test_smoke_detects_stub_that_ignores_stdin(self):
        """只会 echo 的脚本也能「非空」，必须靠两份夹具结果不同才判通过。"""
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\necho hello\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('相同', self.sb.detail(r, '数据/stdin'))

    def test_smoke_detects_passthrough_stub(self):
        """cat 会把两份不同夹具原样输出，差分成立但主渲染没执行——必须拒绝 stdin 原文。"""
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\ncat\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('原文', self.sb.detail(r, '数据/stdin'))

    def test_smoke_detects_single_field_extractor(self):
        """只提取一个字段的占位脚本也会因两份夹具不同而通过差分，所以要求 ctxin 加任一可辨识字段的渲染形态。"""
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\njq -r .model.display_name\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('渲染形态', self.sb.detail(r, '数据/stdin'))

    def test_smoke_detects_multi_field_extractor(self):
        """提取 model+ctx% 两个字段的脚本仍不是渲染器：没有 ctxin 的渲染形态（123k）就判 FAIL。"""
        self.sb.install()
        self.sb.render_stub("#!/usr/bin/env bash\n"
                    "jq -r '\"\\(.model.display_name) \\(.context_window.used_percentage)%\"'\n")
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')
        self.assertIn('渲染形态', self.sb.detail(r, '数据/stdin'))

    def test_smoke_is_independent_of_custom_steps(self):
        """用户改过降级序列（删掉 ctx / 模型 / 目录）不该让冒烟误报——判据不看具体项。"""
        env = self.sb.healthy()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('step = drop ctx\nstep = drop model\nstep = drop cost\n')
        r = self.sb.run(env=env)
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'PASS')

    def test_smoke_tolerates_truncated_directory(self):
        """truncate_length=1 时目录只剩末段，不能因为匹配不到完整路径就判 FAIL。"""
        self.sb.healthy()
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('truncate_length = 1\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'PASS')

    def test_smoke_render_fails_on_silent_script(self):
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\nexit 0\n')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '数据/stdin'), 'FAIL')

    def test_smoke_render_detects_guard_fallback(self):
        """bash 版本守卫的最简行「Opus 5.5 · 37%」不能算冒烟通过——那说明主渲染没跑起来。"""
        self.sb.install()
        self.sb.render_stub('#!/usr/bin/env bash\nprintf "Opus 5.5 · 37%%"\n')
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
                       'x-api-key: possesscret789; Authorization: Basic dXNlcjpwYXNz; '
                       'bearer BearerTOKENvalue123456 bash /wrong/statusline-command.sh'}})
        r = self.sb.run()
        self.assertNotIn('user:pw', r.stdout)
        self.assertNotIn('possesscret789', r.stdout)
        self.assertNotIn('dXNlcjpwYXNz', r.stdout)
        self.assertNotIn('plain123456', r.stdout)
        self.assertNotIn('tok_live_abcdef', r.stdout)
        self.assertNotIn('SECRETabcdef123456', r.stdout)
        self.assertNotIn('BearerTOKENvalue123456', r.stdout)
        self.assertIn('ANTHROPIC_API_KEY=***', r.stdout)
        self.assertIn('AUTH_TOKEN=***', r.stdout)
        self.assertIn('sk-***', r.stdout)
        self.assertIn('/wrong/statusline-command.sh', r.stdout, '路径必须留下，否则无法修')

    def test_quoted_and_query_credentials_are_scrubbed(self):
        """DECISIONS 承诺「值可带引号」；URL query 里的 token 也不能漏。"""
        self.sb.install()
        self.sb.write_settings({'statusLine': {
            'type': 'command',
            'command': 'Bearer "tok_secret"; api_key="low secret"; x-api-key: "key secret"; '
                       'Authorization: Basic "auth secret"; '
                       'curl "https://relay.example/x?token=SECRET987&key=SECRET888&page=2"; '
                       'bash /wrong/statusline-command.sh'}})
        r = self.sb.run()
        for s in ('tok_secret', 'low secret', 'key secret', 'auth secret',
                  'SECRET987', 'SECRET888'):
            self.assertNotIn(s, r.stdout, f'{s!r} 泄露到诊断输出')
        self.assertIn('&page=', r.stdout, 'query 参数结构必须保留，只抹值')
        self.assertIn('/wrong/statusline-command.sh', r.stdout, '路径必须留下，否则无法修')

    def test_no_network(self):
        self.sb.install()
        marker = os.path.join(self.sb.d, 'curl-was-called')
        b = self.sb.minimal_path(extra={'curl': f'#!/bin/sh\ntouch "{marker}"\nexit 7\n'})
        self.sb.run(env={'PATH': b})
        self.assertFalse(os.path.exists(marker), 'doctor 默认不得发起任何网络请求')

    # ── 8. 契约：常量与判据不得与渲染脚本漂移 ───────────────────────────
    def _points_to_verdict(self, cmd):
        """写 settings → 跑 doctor 与 install --check，断言两者一致，返回 doctor 结论。"""
        self.sb.write_settings({'statusLine': {'type': 'command', 'command': cmd}})
        d = self.sb.run()
        chk = subprocess.run([BASE_BASH, INSTALL, '--check'], capture_output=True,
                             text=True, env=self.sb.env)
        got = self.sb.status(d, '安装/statusLine')
        self.assertEqual(chk.returncode == 0, got == 'PASS',
                         f'写法 {cmd!r}：install --check rc={chk.returncode}，doctor={got}\n{chk.stdout}')
        return got

    def test_bak_only_reference_is_not_a_hit(self):
        """`.bak` 指向的是另一个文件；与真引用混在一条命令里时，也要认出真引用。"""
        self.sb.install(subagent=False, settings=False)   # 只装主脚本：--check 就不会因面板键未配而红
        d = self.sb.dest
        for cmd, want in ((f'bash {d}/statusline-command.sh.bak', 'FAIL'),
                          (f'bash {d}/statusline-command.sh.bak && bash /elsewhere/statusline-command.sh', 'FAIL'),
                          (f'cp {d}/statusline-command.sh {d}/statusline-command.sh.bak; '
                           f'bash {d}/statusline-command.sh', 'PASS')):
            self.assertEqual(self._points_to_verdict(cmd), want, cmd)

    def test_config_dir_forms_count_as_pointing(self):
        """运行时展开到已安装目录的几种写法都算指向（默认值只认默认目录）。"""
        self.sb.install(subagent=False, settings=False)
        home = self.sb.env['HOME']
        forms = ['bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-command.sh"',
                 'bash "${CLAUDE_CONFIG_DIR}/statusline-command.sh"',
                 'bash "${CLAUDE_CONFIG_DIR:-~/.claude}/statusline-command.sh"',
                 'bash "${CLAUDE_CONFIG_DIR:-' + home + '/.claude}/statusline-command.sh"']
        # 一种走 doctor 与 install --check 全程；其余直接调两者共用的 points_to，省掉 6 个子进程
        self.assertEqual(self._points_to_verdict(forms[0]), 'PASS', forms[0])
        r = subprocess.run([BASE_BASH, '-c', '. "$1"; shift; d=$1; shift; for c; do '
                            'points_to "$c" statusline-command.sh "$d" || echo "$?: $c"; done',
                            '_', os.path.join(REPO, 'tools', 'lib', 'points_to.sh'), self.sb.dest, *forms[1:]],
                           capture_output=True, text=True, env=self.sb.env, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_panel_fail_codes_match_doctor(self):
        """面板写出的原因码与 doctor 解码的集合必须相等（也等于测试用的文案表）。"""
        with open(os.path.join(SRC, 'subagent-statusline.sh'), encoding='utf-8') as f:
            written = set(re.findall(r'\bmark_fail ([a-z]+)\b', f.read()))
        with open(os.path.join(DOCTOR_LIB, 'cache.sh'), encoding='utf-8') as f:
            decoded = set(re.findall(r'^\s+([a-z]+)\)\s+why=', f.read(), re.M))
        self.assertEqual(written, decoded)
        self.assertEqual(written, set(PANEL_FAIL_TEXT))

    def test_theme_cache_key_matches_renderer(self):
        """让真实渲染脚本写一次色板缓存，doctor 必须找到同一个文件名（不是测试自己编的名字）。"""
        self.sb.install()
        os.makedirs(self.sb.cache, exist_ok=True)
        os.chmod(self.sb.cache, 0o700)
        # 沙箱里跑 --refresh 也不能联网：关掉状态页拉取
        with open(os.path.join(self.sb.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write('status_page = off\n')
        subprocess.run([BASE_BASH, os.path.join(SRC, 'statusline-command.sh'), '--refresh',
                        self.sb.d, '-', 'doctor', 'sub'],
                       env=dict(self.sb.env, CLAUDE_SL_TMP=self.sb.cache, TERM_PROGRAM='wezterm'),
                       capture_output=True, timeout=60)
        self.assertIn('claude-sl-theme-term-wezterm.cache', os.listdir(self.sb.cache))
        r = self.sb.run(env={'TERM_PROGRAM': 'wezterm'})
        self.assertEqual(self.sb.status(r, '缓存/色板'), 'PASS')

    def test_no_bare_var_before_multibyte(self):
        """`$var` 紧跟多字节字符时，bash 会把首字节吞进变量名（渲染脚本约束 6 同一条坑）。
        在 zh_CN.UTF-8 + bash 3.2 上会直接 unbound variable，所以一律写 ${var}。
        install.sh 同样会被 macOS 自带的 3.2 直接运行（它负责报「需要 bash ≥ 4.2」）。"""
        bad = []
        for p in doctor_sources() + [INSTALL]:
            with open(p, encoding='utf-8') as f:
                lines = f.read().splitlines()
            bad += [f'{os.path.basename(p)}:{i}: {l.strip()}' for i, l in enumerate(lines, 1)
                    if not l.lstrip().startswith('#')
                    and re.search(r'\$[A-Za-z_0-9][A-Za-z0-9_]*[^\x00-\x7f]', l)]
        self.assertEqual(bad, [], '这些行要改成 ${var} 写法')

    def test_no_empty_printf_v(self):
        """bash 4.2 的 `printf -v var ''` 不清空变量（5.x 才赋值空串）；必须给 '%s' 格式。

        实测 4.2.0：`v=-; printf -v v ''` 之后 v 仍是 `-`，占位符不会被清掉，状态栏
        会多出「5h -%」「out -」「hit -%」。"""
        bad = []
        for base in (SRC, os.path.join(REPO, 'tools'), os.path.join(REPO, 'tools', 'doctor')):
            for p in sorted(glob.glob(os.path.join(base, '*.sh'))):
                with open(p, encoding='utf-8') as f:
                    lines = f.read().splitlines()
                bad += [f'{os.path.basename(p)}:{i}: {l.strip()}' for i, l in enumerate(lines, 1)
                        if not l.lstrip().startswith('#')
                        and re.search(r"printf -v \S+ (''|\"\")(\s|$)", l)]
        self.assertEqual(bad, [], "printf -v 的格式不能是空串（bash 4.2 不赋值）")

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

    def test_missing_module_exits_1(self):
        """模块缺失不是参数错误：exit 1，并说明缺哪个文件。"""
        tmp = tempfile.mkdtemp(prefix='ccpills-doctor-')
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(DOCTOR, os.path.join(tmp, 'doctor.sh'))
        r = subprocess.run([BASE_BASH, os.path.join(tmp, 'doctor.sh')],
                           capture_output=True, text=True, env=self.sb.env, timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('缺少诊断模块', r.stderr)

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
            # 同一命令里既有 .bak 又有真引用：不能因为先命中 .bak 就判未指向
            f'cp {dest}/statusline-command.sh {dest}/statusline-command.sh.bak; '
            f'bash {dest}/statusline-command.sh',
            '',
        ]
        for cmd in forms:
            self._points_to_verdict(cmd)

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

    def test_lock_path_occupied_by_file_warns(self):
        """普通文件占住锁路径：刷新会先清掉它再抢锁、下一轮自愈，状态栏照常工作——WARN 而不是 FAIL。"""
        self.sb.healthy()
        with open(os.path.join(self.sb.cache, 'claude-sl-lock-doctor'), 'w') as f:
            f.write('x')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'WARN')
        self.assertIn('刷新会先清掉它再抢锁', self.sb.detail(r, '缓存/锁'))

    def test_lock_path_symlink_warns(self):
        """符号链接占住锁路径：同上，刷新先清掉再抢锁。"""
        self.sb.healthy()
        os.symlink('/nonexistent-lock-target',
                   os.path.join(self.sb.cache, 'claude-sl-lock-doctor'))
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'WARN')
        self.assertIn('刷新会先清掉它再抢锁', self.sb.detail(r, '缓存/锁'))

    def test_nonempty_lock_dir_fails(self):
        """渲染侧只用 rmdir，非空锁目录永远删不掉：不能报「下次会自动清除」。"""
        self.sb.healthy()
        d = os.path.join(self.sb.cache, 'claude-sl-lock-doctor')
        os.makedirs(d)
        with open(os.path.join(d, 'junk'), 'w') as f:
            f.write('x')
        r = self.sb.run()
        self.assertEqual(self.sb.status(r, '缓存/锁'), 'FAIL')
        self.assertIn('非空', self.sb.detail(r, '缓存/锁'))

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
        repo = os.path.join(self.sb.d, 'gitrepo')         # 自建仓库：源码树本身不一定是 git 检出（zip / tarball）
        os.makedirs(repo)
        subprocess.run(['git', 'init', '-q', '.'], cwd=repo, check=True)
        self.assertEqual(self.sb.status(self.sb.run('--dir', repo), '缓存/git'), 'PASS')

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
