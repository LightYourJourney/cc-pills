"""statusline-theme.py 配色测试：对比度矩阵、胶囊与背景可辨、主题识别与确定性。

矩阵用「配套的前景与底色」（深底配浅字、浅底配深字）的合成 iTerm profile，覆盖
白 / 浅灰 / 中灰 / 深灰 / 近黑五种底 × 七种胶囊风格；色相用 Tokyo Night ANSI，
主题自动识别应选中 tokyonight。
"""
import math
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest

from _util import SRC, STYLES, THEME as theme

PATH = os.path.join(SRC, 'statusline-theme.py')
THEMES = tuple(theme.THEMES)
ANSI = ('#f7768e', '#9ece6a', '#e0af68', '#7aa2f7', '#bb9af7', '#7dcfff')
# (背景, 前景)：真实终端里浅底配深字、深底配浅字
PROFILES = (('#ffffff', '#1e1e1e'), ('#f5f5f5', '#2b2b2b'), ('#c8c8c8', '#202020'),
            ('#808080', '#111111'), ('#3a3a3a', '#d0d0d0'), ('#0b0b12', '#e6e6e6'),
            ('#1e1f29', '#e6e6e6'))
TRIPLE = re.compile(r'^\d+;\d+;\d+$')
MIN_TEXT = 4.5      # 二级 / 三级文字（除 T_DIM）的最低对比度
MIN_DIM = 2.0       # T_DIM（分隔符）只求可辨
MIN_PILL_DE = 12    # 基础表面色与终端背景的最小可辨距离（docs/DECISIONS.md 的硬下限）


def style_surface(st):
    """该风格的 P_NEU 是否由风格自己定义：'light'（pastel）/ 'tokens'（tokyo-glass）/ None。"""
    s = theme.STYLES[st]
    return 'light' if s.get('light') else 'tokens' if s.get('tokens') else None


def rgb(s):
    return theme.rgb(s)


def level(s):
    return [int(x) for x in s.split(';')]


def make_home(bg, fg):
    home = tempfile.mkdtemp(prefix='ccpills-theme-')
    os.makedirs(os.path.join(home, 'Library', 'Preferences'))

    def comp(h):
        v = rgb(h)
        return {'Red Component': v[0] / 255, 'Green Component': v[1] / 255, 'Blue Component': v[2] / 255}
    prof = {'Background Color': comp(bg), 'Foreground Color': comp(fg)}
    for i, a in enumerate(ANSI, 1):
        prof[f'Ansi {i} Color'] = comp(a)
    with open(os.path.join(home, 'Library', 'Preferences', 'com.googlecode.iterm2.plist'), 'wb') as f:
        plistlib.dump({'New Bookmarks': [dict(Name='P', Guid='G0', **prof)],
                       'Default Bookmark Guid': 'G0'}, f)
    return home


def palette(home, style, theme_name='auto', ver='4'):
    out = os.path.join(home, f'{theme_name}-{style}.cache')
    env = dict(os.environ, HOME=home, TERM_PROGRAM='iTerm.app', ITERM_PROFILE='P')
    subprocess.run([sys.executable, PATH, theme_name, out, '12', style, ver],
                   env=env, capture_output=True, check=False)
    with open(out) as f:
        lines = f.read().split('\n')
    kv = {}
    for l in lines:
        k, _, v = l.partition('=')
        if TRIPLE.match(v.strip()):
            kv[k] = v.strip()
    return kv, lines


def comp(h):
    v = rgb(h)
    return {'Red Component': v[0] / 255, 'Green Component': v[1] / 255, 'Blue Component': v[2] / 255}


def iterm_home(*profiles, guid='G0'):
    """只放给定 iTerm profile 的 HOME（make_home 固定用东京夜 ANSI，识别不出别的主题）。"""
    home = tempfile.mkdtemp(prefix='ccpills-theme-')
    os.makedirs(os.path.join(home, 'Library', 'Preferences'))
    with open(os.path.join(home, 'Library', 'Preferences', 'com.googlecode.iterm2.plist'), 'wb') as f:
        plistlib.dump({'New Bookmarks': list(profiles), 'Default Bookmark Guid': guid}, f)
    return home


def run_theme(home, profile='P'):
    """在 home 里跑一次 THEME=auto 的色板生成；没写出缓存时返回 None（异常被吞掉）。"""
    out = os.path.join(home, 'auto-tint.cache')
    env = dict(os.environ, HOME=home, TERM_PROGRAM='iTerm.app')
    if profile is not None:
        env['ITERM_PROFILE'] = profile
    subprocess.run([sys.executable, PATH, 'auto', out, '12', 'tint', '4'],
                   env=env, capture_output=True, check=False)
    if not os.path.exists(out):
        return None
    with open(out) as f:
        return f.read().split('\n')


class ContrastMatrix(unittest.TestCase):
    def test_every_text_readable_on_every_pill(self):
        bad = []
        for bg, fg in PROFILES:
            home = make_home(bg, fg)
            for st in STYLES:
                kv, _ = palette(home, st)
                pills = {k: level(v) for k, v in kv.items() if k.startswith('P_')}
                self.assertIn('T_DIM', kv, f'{bg}/{st}: 色板缺少 T_DIM，多半是生成中途异常被静默吞掉')
                for k, v in kv.items():
                    if k[:2] not in ('T_', 'L_'):
                        continue
                    text = level(v)
                    worst = min(theme.contrast(text, p) for p in pills.values())
                    floor = MIN_DIM if k == 'T_DIM' else MIN_TEXT
                    if worst < floor:
                        bad.append(f'{bg} {st} {k} on {min(pills, key=lambda p: theme.contrast(text, pills[p]))} = {worst:.2f} < {floor}')
        self.assertFalse(bad, f'{len(bad)} 处文字对比不足：\n' + '\n'.join(bad[:10]))

    def test_base_pill_distinguishable(self):
        """DECISIONS：MIN_PILL_DE = 12 只约束基础表面色；pastel 的 P_NEU 是模型组胶囊，由风格定义。

        tokyo-glass 的胶囊由 TG_MIN_DE 约束（贴近背景的玻璃质感），同样要覆盖。
        """
        self.assertEqual(theme.MIN_PILL_DE, MIN_PILL_DE, 'DECISIONS 规定 12，常量被改低时这条要一起改')
        bad = []
        for bg, fg in PROFILES:
            home = make_home(bg, fg)
            for st in STYLES:
                own = style_surface(st)
                if own == 'light':
                    continue          # P_NEU 是模型组胶囊，由风格定义
                kv, lines = palette(home, st)
                name = next(l.split('=', 1)[1] for l in lines if l.startswith('NAME='))
                bgs = [rgb(bg)]
                if own != 'tokens' or not theme.is_light(rgb(bg)):
                    bgs.append(rgb(theme.THEMES[name]['bg']))   # 主题标准背景也要可辨
                base = level(kv['P_NEU'])
                de = min(math.dist(theme.lab(base), theme.lab(b)) for b in bgs)
                floor = theme.TG_MIN_DE if own == 'tokens' else MIN_PILL_DE
                if de < floor:
                    bad.append(f'{bg} {st}: 基础胶囊 ΔE {de:.1f} < {floor}')
        self.assertFalse(bad, '\n'.join(bad))

    def test_light_terminal_pills_do_not_collapse(self):
        """浅底曾把胶囊全截成 #ffffff（ΔE 与背景为 0）——每块胶囊至少三种不同颜色。"""
        home = make_home('#ffffff', '#1e1e1e')
        for st in STYLES:
            kv, _ = palette(home, st)
            colors = {kv[k] for k in kv if k.startswith('P_')}
            self.assertGreaterEqual(len(colors), 3, f'{st}: 白底上胶囊只剩 {len(colors)} 种颜色')

    def test_explicit_themes(self):
        home = make_home('#1e1f29', '#e6e6e6')
        for t in THEMES:
            for st in ('tint', 'tokyo-glass'):
                kv, lines = palette(home, st, theme_name=t)
                self.assertEqual(lines[0], f'THEME={t}/12/{st}/v4')
                self.assertIn(f'NAME={t}', lines)
                self.assertGreaterEqual(min(theme.contrast(level(v), level(kv['P_NEU']))
                                            for k, v in kv.items() if k.startswith('T_')), MIN_DIM)

    def test_auto_picks_theme_by_ansi(self):
        home = make_home('#1e1f29', '#e6e6e6')
        _, lines = palette(home, 'tint')
        self.assertIn('NAME=tokyonight', lines)

    def test_deterministic(self):
        home = make_home('#3a3a3a', '#d0d0d0')
        a, _ = palette(home, 'tokyo-glass')
        b, _ = palette(home, 'tokyo-glass')
        self.assertEqual(a, b)


class AutoDetect(unittest.TestCase):
    """THEME=auto 的终端识别：ITERM_PROFILE 选 profile、深浅色各取一套、plist 损坏回落 tokyonight。"""

    def ansi_profile(self, theme_name, name='P', **extra):
        """ANSI 1–6 用某主题的标准色，识别结果应当是该主题。"""
        prof = {'Name': name, 'Guid': 'G-' + name,
                'Background Color': comp('#101010'), 'Foreground Color': comp('#e0e0e0')}
        for i, a in enumerate(theme.THEMES[theme_name]['ansi'], 1):
            prof[f'Ansi {i} Color'] = comp(a)
        prof.update(extra)
        return prof

    def test_plist_failure_falls_back_to_tokyonight(self):
        """文档 1：读 plist 出任何异常（缺键、损坏）都回落 tokyonight——异常不能让整个色板
        生成失败，否则状态栏一直无色。这里放一个缺 ANSI 键的 profile：取值时抛 KeyError。"""
        home = iterm_home({'Name': 'P', 'Guid': 'G0',
                           'Background Color': comp('#101010'), 'Foreground Color': comp('#e0e0e0')})
        lines = run_theme(home)
        self.assertIsNotNone(lines, 'plist 损坏时没写出色板（异常逃出了回落分支）')
        self.assertEqual(lines[0], 'THEME=auto/12/tint/v4')
        self.assertIn('NAME=tokyonight', lines)
        self.assertIn('P_NEU', dict(l.split('=', 1) for l in lines if '=' in l))

    def test_profile_matched_by_name_not_just_default(self):
        """ITERM_PROFILE 指定的 profile 要按名字选中：删掉按名查找那行就只剩默认 profile，
        dracula 的终端被当成默认的东京夜（主题识别退回默认而不是用户实际的 profile）。"""
        home = iterm_home(self.ansi_profile('dracula', 'Dracula'), self.ansi_profile('nord', 'Default'),
                          guid='G-Default')
        self.assertIn('NAME=dracula', run_theme(home, profile='Dracula'),
                      'ITERM_PROFILE=Dracula 没被选中，用了默认 profile')
        # ITERM_PROFILE 对不上任何 profile 时取默认 profile（这里是 nord，与回落主题 tokyonight 不同，
        # 才能区分「读了默认 profile」与「回落」）。不用「不设」：外层终端的 ITERM_PROFILE 会漏进来
        self.assertIn('NAME=nord', run_theme(home, profile='NoSuchProfile'), '名字对不上时应当取默认 profile')

    def test_separate_light_and_dark_colors(self):
        """profile 分深浅色时按 macOS 当前外观取带 suffix 的那套：suffix 写成空串就永远读
        共用的那套，浅色模式上会拿到深色（或反之）的 ANSI，识别出错误的主题。"""
        light = self.ansi_profile('nord', 'Split', **{'Use Separate Colors for Light and Dark Mode': True})
        for i in range(1, 7):                         # 共用的一套是 dracula 色
            light[f'Ansi {i} Color'] = comp(theme.THEMES['dracula']['ansi'][i - 1])
            light[f'Ansi {i} Color (Light)'] = comp(theme.THEMES['nord']['ansi'][i - 1])
            light[f'Ansi {i} Color (Dark)'] = comp(theme.THEMES['catppuccin']['ansi'][i - 1])
        home = iterm_home(light)
        # 测试机上没有 AppleInterfaceStyle 时按浅色（defaults 读出空串 ≠ 'Dark'）
        for want_dark, want_theme in ((False, 'nord'), (True, 'catppuccin')):
            fake = os.path.join(home, 'fakebin')
            os.makedirs(fake, exist_ok=True)
            with open(os.path.join(fake, 'defaults'), 'w') as f:
                f.write('#!/bin/sh\necho ' + ('Dark' if want_dark else 'Light') + '\n')
            os.chmod(os.path.join(fake, 'defaults'), 0o755)
            out = os.path.join(home, 'auto-tint.cache')
            subprocess.run([sys.executable, PATH, 'auto', out, '12', 'tint', '4'],
                           env=dict(os.environ, HOME=home, TERM_PROGRAM='iTerm.app', ITERM_PROFILE='Split',
                                    PATH=fake + os.pathsep + os.environ['PATH']),
                           capture_output=True, check=False)
            with open(out) as f:
                lines = f.read().split('\n')
            self.assertIn(f'NAME={want_theme}', lines, ('深色' if want_dark else '浅色') + '模式取错了那一套 ANSI')


if __name__ == '__main__':
    unittest.main()
