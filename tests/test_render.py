"""主状态栏渲染测试：回归金样、宽度性质、健壮性、语义。

更新金样（有意改变了渲染结果时）：UPDATE_GOLDEN=1 python3 -m unittest tests.test_render
"""
import json
import os
import re
import subprocess
import unittest

from _util import STYLES, Sandbox, color_of, strip, width

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'golden', 'render.json')
WIDTHS = (220, 120, 80)
SAFE = 4            # SAFE_MARGIN：可用宽度 = COLUMNS − 4（docs/DECISIONS.md）


def scenarios(sb):
    """(名称, stdin, token 缓存字段, 额外环境变量, Claude 状态)。"""
    cjk = os.path.join(sb.repo, '课程资料', '量子力学作业')
    os.makedirs(cjk, exist_ok=True)
    tok = dict(**{'in': '30.1M'}, out='290k', rate='$4.9/h', comp='1', hit='97.4')
    return [
        ('常态', sb.stdin('s1', q5=20, q7=40, cache=3000), dict(tok, q5lv='ok', q7lv='ok'), {}, 'none'),
        ('提前用完', sb.stdin('s2', ctx=18, ctx_tok=175000, q5=58, q7=88, cache=500),
         dict(tok, q5lv='warn', q5out='2h00m', q7lv='high', q7out='16h21m'), {}, 'none'),
        ('告警全开', sb.stdin('s3', ctx=96, ctx_tok=960000, q5=82, q7=97, cache='cold'),
         dict(tok, q5lv='warn', q5out='36m', q7lv='bad', q7out='3h42m'), {}, 'major'),
        ('中转站', sb.stdin('s4', cache=3000),
         dict(tok, rcost='3.2100', runpriced='1', rtoday='23.4000', rlimit='50.0000', dlv='warn', dout='3h10m'),
         {'ANTHROPIC_BASE_URL': 'https://relay.example/v1'}, 'minor'),
        ('网关花费限额', sb.stdin('s5', spend={'used_percentage': 47, 'used_usd': 23.4, 'limit_usd': 50,
                                               'period': 'weekly'}),
         dict(tok, splv='warn', spout='1d2h'), {'ANTHROPIC_BASE_URL': 'https://gw.example'}, 'none'),
        ('长模型名与中文目录', sb.stdin('s6', q5=60, q7=82, cache=360, cwd=cjk,
                                       model='Claude Opus 5.5 (1M context)'),
         dict(tok, q5lv='warn', q5out='1h42m', q7lv='high', q7out='1d1h'), {}, 'none'),
    ]


def each_scenario(sb):
    """逐个场景写好 token 缓存与 Claude 状态，产出 (名称, stdin, 额外环境变量)。"""
    for name, d, tok, env, status in scenarios(sb):
        sb.write_tok(d['session_id'], **tok)
        sb.set_status(status)
        yield name, d, env


def render_all():
    out = {}
    for st in STYLES:
        with Sandbox(style=st) as sb:
            for name, d, env in each_scenario(sb):
                for c in WIDTHS:
                    out[f'{st}|{name}|{c}'] = sb.render(d, c, env=env)
    return out


class Golden(unittest.TestCase):
    def test_golden(self):
        got = render_all()
        if os.environ.get('UPDATE_GOLDEN'):
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, 'w') as f:
                json.dump(got, f, ensure_ascii=False, indent=0, sort_keys=True)
            self.skipTest(f'已更新金样 {len(got)} 组')
        with open(GOLDEN) as f:
            want = json.load(f)
        self.assertEqual(sorted(got), sorted(want), '场景 / 风格 / 宽度组合与金样不一致')
        bad = [k for k in want if got[k] != want[k]]
        msg = '\n'.join(f'{k}\n  金样 {strip(want[k])}\n  实际 {strip(got[k])}' for k in bad[:5])
        self.assertFalse(bad, f'{len(bad)}/{len(want)} 组与金样不同：\n{msg}')


class WidthProperty(unittest.TestCase):
    """超出预算（COLUMNS − 4）只允许发生在所有项都已降到底时（永不丢弃项本身放不下）。"""

    # 倒计时按真实时钟算，两次渲染之间跨过分钟边界就会改文本（8m → 7m）。
    # 只归一化「数字 + 时长单位」，百分比、token 量、美元都不受影响。
    COUNTDOWN = re.compile(r'\d+[dhms]')

    @classmethod
    def stable(cls, s):
        return cls.COUNTDOWN.sub('#', strip(s))

    def test_never_overflow_while_degradable(self):
        with Sandbox() as sb:
            for name, d, env in each_scenario(sb):
                for c in range(30, 221, 5):
                    o = sb.render(d, c, env=env)
                    if width(o) > c - SAFE:
                        # 现场渲染最小形态（C=12 必然全部降级），只比较倒计时以外的内容
                        minimal = sb.render(d, 12, env=env)
                        self.assertEqual(self.stable(o), self.stable(minimal),
                                         f'{name} C={c}：宽 {width(o)} 超预算 {c - SAFE}，但还能降级')

    def test_backfill_leaves_little_slack(self):
        """回填：有降级时剩余空间应小于任何一项可恢复内容（实测各场景 ≤ 7 列；无回填的旧版 100 列时余 16 列）。

        判据按宽度：现有 drop / shrink 步骤都严格减宽；将来加入等宽降级，这条要跟着改。
        """
        with Sandbox() as sb:
            for name, d, env in each_scenario(sb):
                full_w = width(sb.render(d, 400, env=env))
                for c in range(40, 221, 4):
                    o = sb.render(d, c, env=env)
                    # 用宽度而不是文本判断「是否降级」：倒计时跨分钟会改文本、通常不改宽度
                    if width(o) < full_w and width(o) <= c - SAFE:
                        self.assertLessEqual(c - SAFE - width(o), 10, f'{name} C={c}：降级后还空着 {c - SAFE - width(o)} 列')

    def test_backfill_restores_by_priority(self):
        """后降级的（优先级高的）先恢复：110 列时完整模型名回来，低优先级的额度间距仍是紧凑写法。"""
        with Sandbox() as sb:
            d = sb.stdin('bf', q5=60, q7=82, cache=360, model='Claude Opus 5.5 (1M context)')
            sb.write_tok('bf', **{'in': '30.1M'}, out='290k', rate='$4.9/h', comp='2', hit='97.4',
                         q5lv='warn', q5out='1h42m', q7lv='high', q7out='1d1h')
            self.assertNotIn('(1M context)', strip(sb.render(d, 100)))
            o = strip(sb.render(d, 110))
            self.assertIn('Claude Opus 5.5 (1M context)', o)
            self.assertIn('5h60%', o)


class Robustness(unittest.TestCase):
    def test_escape_injection_in_dir_names(self):
        with Sandbox() as sb:
            for name in ('a\\cb', 'e\\033[41mRED', 'esc\x1b[42mX'):
                d = os.path.join(sb.repo, name)
                os.makedirs(d)
                o = sb.render(sb.stdin(cwd=d))
                self.assertIn('ctx 42%', strip(o), f'{name!r}：整行被截断')
                self.assertNotIn('\x1b[41m', o)
                self.assertNotIn('\x1b[42m', o)

    def test_wrong_field_types_only_affect_that_item(self):
        with Sandbox() as sb:
            for k, v in (('effort', 'high'), ('prompt_cache', True), ('rate_limits', [1]),
                         ('model', 'Opus'), ('cost', 'x'), ('workspace', 3)):
                d = sb.stdin()
                d[k] = v
                self.assertIn('ctx 42%', strip(sb.render(d)), f'.{k}={v!r} 拖垮了整行')
            for bad in ('[1,2]', 'not json', '', '{}'):
                r = sb.render(bad, raw=True)
                self.assertEqual(r.returncode, 0, f'stdin={bad!r} 退出码非 0')
                self.assertEqual(r.stderr, '', f'stdin={bad!r} 有 stderr 输出')

    def test_empty_string_fields_do_not_shift(self):
        with Sandbox() as sb:
            d = sb.stdin(q5=58)
            d['model']['display_name'] = ''
            d['effort']['level'] = ''
            o = strip(sb.render(d))
            self.assertIn('ctx 42%', o)
            self.assertIn('5h 58%', o)

    def test_config_leading_zero(self):
        with Sandbox(conf_extra='safe_margin = 08') as sb:
            r = sb.render(sb.stdin(), 60, raw=True)
            self.assertEqual(r.stderr, '')
            self.assertLessEqual(width(r.stdout), 52)

    def test_ascii_caps_have_background(self):
        with Sandbox(conf_extra='glyphs = ascii') as sb:
            o = sb.render(sb.stdin())
            self.assertRegex(o, r'^\x1b\[48;2;[\d;]+m \x1b\[48;')
            self.assertNotIn('', o)

    def test_no_color_and_256_color(self):
        with Sandbox() as sb:
            o = sb.render(sb.stdin(), env={'TERM': 'dumb', 'COLORTERM': ''})
            self.assertNotIn('\x1b', o)
            o = sb.render(sb.stdin(), env={'COLORTERM': ''})
            self.assertTrue(re.search(r'[34]8;5;\d+m', o))
            self.assertNotIn('8;2;', o)
            self.assertNotRegex(o, r'\x1b\[[0-9;]*;m', '出现了缺颜色值的非法 SGR')

    def test_long_paths_and_key_collisions(self):
        with Sandbox() as sb:
            lp = os.path.join(sb.repo, 'L' * 120, 'M' * 120, 'N' * 60)
            os.makedirs(lp)
            sb.refresh(lp)
            self.assertTrue(any(f.startswith('claude-sl-git-h') for f in os.listdir(sb.tmp)),
                            '超长路径的 git 缓存没写进去')
            for p in ('x/y_z', 'x_y/z'):
                os.makedirs(os.path.join(sb.dir, p))
            subprocess.run(['git', 'init', '-q', '.'], cwd=os.path.join(sb.dir, 'x/y_z'), check=True)
            subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/feat-collide'],
                           cwd=os.path.join(sb.dir, 'x/y_z'), check=True)
            sb.refresh(os.path.join(sb.dir, 'x/y_z'))
            sb.refresh(os.path.join(sb.dir, 'x_y/z'))
            self.assertNotIn('feat-collide', strip(sb.render(sb.stdin(cwd=os.path.join(sb.dir, 'x_y/z')))))

    def test_cjk_branch_truncated_by_display_width(self):
        with Sandbox() as sb:
            subprocess.run(['git', 'symbolic-ref', 'HEAD', 'refs/heads/功能/超长中文分支名称测试显示宽度截断'],
                           cwd=sb.repo, check=True)
            sb.refresh(sb.repo)
            o = strip(sb.render(sb.stdin(), 80))
            br = o.split(' ')[1].split('')[0].split(' ')[0]
            self.assertTrue(br.endswith('…'))
            self.assertLessEqual(width(br), 21)


class Semantics(unittest.TestCase):
    def test_quota_colored_by_current_value(self):
        with Sandbox() as sb:
            th = sb.theme()
            self.assertEqual(color_of(sb.render(sb.stdin(q5=20, q7=82)), '82%'), th['T_BAD'],
                             '没有 token 缓存时 7d 82% 也应是红色')
            sb.write_tok('t1', q5lv='high', q7lv='high')
            self.assertEqual(color_of(sb.render(sb.stdin(q5=3, q7=40)), '3%'), th['T_NEU'],
                             '窗口刚重置、旧缓存为 high 时 5h 3% 不应变红')

    def test_exhausted(self):
        with Sandbox() as sb:
            o = strip(sb.render(sb.stdin(q5=100, q7=60)))
            self.assertIn('用尽', o)
            self.assertIn('↺', o)

    def test_spend_limit(self):
        base = {'ANTHROPIC_BASE_URL': 'https://gw.example'}
        with Sandbox() as sb:
            o = strip(sb.render(sb.stdin(spend={'used_percentage': 47, 'used_usd': 23.4, 'limit_usd': 50,
                                                'period': 'daily'}), env=base))
            self.assertIn('今日 $23.40/$50.00', o)
            o = sb.render(sb.stdin(spend={'used_percentage': 85, 'period': 'weekly'}), env=base)
            self.assertIn('本周 85%', strip(o))
            self.assertEqual(color_of(o, '85%'), sb.theme()['T_BAD'])
            o = strip(sb.render(sb.stdin(spend={'used_percentage': 104, 'used_usd': 52, 'limit_usd': 50,
                                                'period': 'monthly'}), env=base))
            self.assertIn('用尽', o)

    def test_mode_detection(self):
        with Sandbox() as sb:
            o = strip(sb.render(sb.stdin()))
            self.assertIn('$17.54', o)
            self.assertNotIn('$17.54*', o, '无额度、无 BASE_URL 应按订阅口径')
            o = strip(sb.render(sb.stdin(), env={'ANTHROPIC_BASE_URL': 'https://relay.example'}))
            self.assertIn('$17.54*', o)
            o = strip(sb.render(sb.stdin(), env={'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}))
            self.assertNotIn('$17.54*', o)

    def test_cache_cold_only_when_expired(self):
        with Sandbox() as sb:
            d = sb.stdin(cache=3000)
            d['prompt_cache']['warm'] = False
            self.assertNotIn('cold', strip(sb.render(d)))
            self.assertIn('cache cold', strip(sb.render(sb.stdin(cache='cold'))))

    def test_message_count_item(self):
        with Sandbox() as sb:
            d = sb.stdin('mc', q5=20)
            sb.write_tok('mc', **{'in': '172M'}, out='1M', rate='$8.4/h', hit='98.4', msgs='517')
            o = strip(sb.render(d, 220))
            self.assertIn('m517', o)
            tail = o.split(o[0])[-1]        # 左端帽取自渲染结果本身（与脚本的 PL_LCAP 同源）
            self.assertIn('m517', tail, '消息数应在尾段胶囊里')

    def test_third_party_model_abbreviation(self):
        """无空格的模型名：紧凑级取首个 "-" 段并保留方括号（deepseek[1M]·XH）。"""
        with Sandbox() as sb:
            tok = dict(**{'in': '172M'}, out='1M', rate='$8.4/h', hit='98.4', msgs='517')
            d = sb.stdin('mm', model='deepseek-flash[1M]', effort='xhigh')
            sb.write_tok('mm', **tok)
            self.assertIn('deepseek-flash[1M] xhigh', strip(sb.render(d, 220)))
            self.assertIn('deepseek[1M]·XH', strip(sb.render(d, 55)))
            d = sb.stdin('mg', model='gpt-6.1-sol', effort='high')
            sb.write_tok('mg', **tok)
            self.assertIn('gpt·H', strip(sb.render(d, 50)))

    def test_status_page_off(self):
        with Sandbox(conf_extra='status_page = off', status='major') as sb:
            self.assertNotIn('major', strip(sb.render(sb.stdin())))
            os.remove(os.path.join(sb.tmp, 'claude-sl-status.try'))
            sb.refresh(sb.repo)
            self.assertFalse(os.path.exists(os.path.join(sb.tmp, 'claude-sl-status.try')),
                             'status_page = off 时不应发起状态页请求')


if __name__ == '__main__':
    unittest.main()
