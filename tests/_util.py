"""测试公共工具：在隔离目录里渲染真实脚本，结果确定、不碰本机任何线上缓存。

隔离与确定性（每个渲染一个独立沙箱）：
  - CLAUDE_SL_TMP → 沙箱目录：色板 / git / token / 状态 / 锁全部写在里面
  - CLAUDE_SL_NO_REFRESH=1：不派生后台刷新进程；色板与 git 缓存由一次同步 --refresh 预先生成
  - XDG_CONFIG_HOME → 沙箱配置：示例配置 + theme = dracula（主题固定，不读 iTerm 偏好）
  - 清掉 TERM_PROGRAM / LC_TERMINAL / ITERM_PROFILE / ANTHROPIC_BASE_URL，固定 truecolor 与 UTF-8 locale
  - Claude 状态页缓存与「上次拉取时刻」都预先写好：不会联网
  - 工作目录是沙箱里新建的 git 仓库 demo（分支 main）
  - 时间相关字段都取 now + 「整分钟再加 30 秒」的偏移，运行跨秒也不改变显示的分钟数
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, 'src')
SCRIPT = os.path.join(SRC, 'statusline-command.sh')
EXAMPLE_CONF = os.path.join(REPO, 'config', 'claude-statusline.conf.example')
ANSI = re.compile(r'\x1b\[[0-9;]*m')
STYLES = ('tint', 'cool', 'jewel', 'glass', 'mono', 'pastel', 'tokyo-glass')


def utf8_locale():
    out = subprocess.run(['locale', '-a'], capture_output=True, text=True).stdout.split()
    low = {x.lower().replace('-', ''): x for x in out}
    for want in ('c.utf8', 'en_us.utf8', 'zh_cn.utf8'):
        if want in low:
            return low[want]
    return 'C.UTF-8'


LOCALE = utf8_locale()


def strip(s):
    return ANSI.sub('', s)


def width(s):
    """显示宽度：东亚宽 / 全角字符两列，其余（含歧义宽度与私用区字形）一列——与脚本的宽度模型一致。"""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in strip(s))


def tok_line(**kw):
    """18 段 token 缓存（见 statusline-tokens.py 文档）。"""
    keys = ['in', 'out', 'rate', 'comp', 'q5lv', 'q5out', 'q7lv', 'q7out', 'dlv', 'dout',
            'rcost', 'runpriced', 'rtoday', 'rlimit', 'hit', 'splv', 'spout', 'msgs']
    return '\t'.join(str(kw.get(k, '-')) for k in keys) + '\n'


class Sandbox:
    """一个隔离的渲染环境。用法：with Sandbox(style='tint') as sb: sb.render(stdin, cols)"""

    def __init__(self, style='tint', conf_extra='', status='none'):
        self.style, self.conf_extra, self.status = style, conf_extra, status

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='ccpills-test-')
        self.tmp = os.path.join(self.dir, 'slt')
        self.cfg = os.path.join(self.dir, 'cfg')
        self.repo = os.path.join(self.dir, 'demo')
        for d in (self.tmp, self.cfg, self.repo):
            os.makedirs(d)
        with open(EXAMPLE_CONF) as f:
            conf = f.read()
        conf = '\n'.join(l for l in conf.split('\n')
                         if not re.match(r'\s*(pill_style|theme|status_page)\s*=', l))
        with open(os.path.join(self.cfg, 'claude-statusline.conf'), 'w') as f:
            f.write(conf + f'\ntheme = dracula\npill_style = {self.style}\n{self.conf_extra}\n')
        subprocess.run(['git', 'init', '-q', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'], cwd=self.repo, check=True)
        self.now = int(time.time())
        self.set_status(self.status)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('TERM_PROGRAM', 'LC_TERMINAL', 'ITERM_PROFILE', 'ANTHROPIC_BASE_URL',
                                 'CLAUDE_CONFIG_DIR')}
        self.env.update(CLAUDE_SL_TMP=self.tmp, CLAUDE_SL_NO_REFRESH='1', XDG_CONFIG_HOME=self.cfg,
                        COLORTERM='truecolor', TERM='xterm-256color', LC_ALL=LOCALE, LANG=LOCALE)
        self.refresh(self.repo)
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)

    def set_status(self, status):
        with open(os.path.join(self.tmp, 'claude-sl-status.cache'), 'w') as f:
            f.write(f'{self.now}\t{status}\n')
        with open(os.path.join(self.tmp, 'claude-sl-status.try'), 'w') as f:
            f.write(f'{self.now}\n')

    def refresh(self, cwd, sid='test-init', mode='sub', env=None):
        subprocess.run(['bash', SCRIPT, '--refresh', cwd, '-', sid, mode],
                       env=dict(self.env, **(env or {})), capture_output=True)

    def write_tok(self, sid, **kw):
        with open(os.path.join(self.tmp, f'claude-sl-tok-{sid}.cache'), 'w') as f:
            f.write(tok_line(**kw))

    def stdin(self, sid='t1', ctx=42, ctx_tok=419000, q5=None, q7=None, cache=None, cwd=None,
              model='Opus 5.5', effort='high', cost=17.54, api_ms=3120000, spend=None, **extra):
        """cache：None 不给 prompt_cache；'cold' 已过期；整数 = 剩余秒数（另加 30 s 余量）。"""
        n = self.now
        d = {'session_id': sid, 'transcript_path': '', 'cwd': cwd or self.repo,
             'model': {'display_name': model}, 'effort': {'level': effort},
             'cost': {'total_cost_usd': cost, 'total_api_duration_ms': api_ms},
             'context_window': {'total_input_tokens': ctx_tok, 'context_window_size': 1000000,
                                'used_percentage': ctx}}
        rl = {}
        if q5 is not None:
            rl['five_hour'] = {'used_percentage': q5, 'resets_at': n + 2 * 3600 + 13 * 60 + 30}
        if q7 is not None:
            rl['seven_day'] = {'used_percentage': q7, 'resets_at': n + 47 * 3600 + 30 * 60 + 30}
        if spend is not None:
            rl['spend_limit'] = dict({'resets_at': n + 8 * 3600 + 30}, **spend)
        if rl:
            d['rate_limits'] = rl
        if cache is not None:
            exp = n - 60 if cache == 'cold' else n + cache + 30   # +30 s：跨秒运行也不改变显示的分钟数
            d['prompt_cache'] = {'warm': cache != 'cold', 'caching_observed': True, 'ttl': '1h',
                                 'expires_at': exp, 'recache_tokens_if_cold': ctx_tok}
        d.update(extra)
        return d

    def render(self, stdin, cols=220, env=None, raw=False):
        data = stdin if isinstance(stdin, str) else json.dumps(stdin)
        r = subprocess.run(['bash', SCRIPT], input=data, capture_output=True, text=True,
                           env=dict(self.env, COLUMNS=str(cols), **(env or {})))
        return r if raw else r.stdout

    def theme(self):
        name = next(f for f in os.listdir(self.tmp) if f.startswith('claude-sl-theme-') and f.endswith('.cache'))
        with open(os.path.join(self.tmp, name)) as f:
            return dict(l.rstrip('\n').split('=', 1) for l in f if '=' in l)


def color_of(out, text):
    """渲染结果里包含 text 的那一段的前景色 "R;G;B"（truecolor）。"""
    cur = None
    for m in re.finditer(r'\x1b\[([0-9;]*)m|([^\x1b]+)', out):
        if m.group(1) is not None:
            mm = re.search(r'38;2;(\d+;\d+;\d+)', m.group(1))
            if mm:
                cur = mm.group(1)
        elif text in m.group(2):
            return cur
    return None
