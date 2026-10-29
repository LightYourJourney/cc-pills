"""Token 缓存新鲜度与跨窗口一致性。

后台刷新持续失败时，旧缓存不能无限期冒充当前值；当前 stdin 的额度百分比也不能被
上一窗口遗留的「用尽 / bad」覆盖。缓存旁的时间戳是生产者写的，缺失时按新鲜处理
（预览 / 测试假缓存与升级过渡）。
"""
import json
import os
import subprocess
import time
import unittest

from _util import SCRIPT, Sandbox, color_of, strip


def write_ts(sb, sid, age=0):
    with open(os.path.join(sb.tmp, f'claude-sl-tok-{sid}.cache.ts'), 'w') as f:
        f.write(f'{int(time.time()) - age}\n')


class Freshness(unittest.TestCase):
    def test_stale_cache_hides_derived_items(self):
        with Sandbox() as sb:
            sb.write_tok('st', **{'in': '172M'}, out='1M', rate='$99/h', comp='2',
                         hit='98.4', q5lv='bad', q5out='用尽', msgs='517')
            write_ts(sb, 'st', age=10 * 86400)
            o = strip(sb.render(sb.stdin('st', q5=3, q7=40, cache=3000)))
            for item in ('$99/h', 'hit ', 'm517', '用尽', 'out '):
                self.assertNotIn(item, o, f'陈旧缓存仍在展示 {item!r}')
            self.assertIn('ctx 42%', o)
            self.assertIn('5h 3%', o)

    def test_fresh_cache_still_shows_items(self):
        with Sandbox() as sb:
            sb.write_tok('fr', **{'in': '172M'}, out='1M', rate='$99/h', hit='98.4', msgs='517')
            write_ts(sb, 'fr', age=0)
            o = strip(sb.render(sb.stdin('fr', q5=20)))
            for item in ('$99/h', 'hit 98.4%', 'm517'):
                self.assertIn(item, o)

    def test_missing_ts_is_treated_as_fresh(self):
        """升级过渡 / 预览与测试的假缓存没有 .ts，不能因此整段消失。"""
        with Sandbox() as sb:
            sb.write_tok('no', **{'in': '172M'}, out='1M', rate='$99/h', hit='98.4', msgs='517')
            o = strip(sb.render(sb.stdin('no', q5=20)))
            self.assertIn('$99/h', o)
            self.assertIn('m517', o)

    def test_bad_level_cannot_override_fresh_quota(self):
        """缓存说「用尽」但 stdin 只有 3%：这个组合不可能属于当前窗口，必须丢弃。"""
        with Sandbox() as sb:
            sb.write_tok('bad', q5lv='bad', q5out='用尽', q7lv='bad', q7out='用尽')
            write_ts(sb, 'bad', age=0)
            out = sb.render(sb.stdin('bad', q5=3, q7=5))
            self.assertNotIn('用尽', strip(out))
            self.assertNotEqual(color_of(out, '3%'), sb.theme()['T_CRIT'], '3% 不应是强红')

    def test_refresh_writes_timestamp(self):
        """生产者必须在缓存旁写 .ts；否则新鲜度检查只能永远按「旧缓存」处理。"""
        with Sandbox(conf_extra='status_page = off') as sb:
            tp = os.path.join(sb.dir, 'rt.jsonl')
            now = int(time.time())
            line = json.dumps({'type': 'assistant',
                               'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
                               'message': {'id': 'm1', 'model': 'claude-opus-5-5',
                                           'stop_reason': 'end_turn',
                                           'usage': {'input_tokens': 10, 'output_tokens': 1,
                                                     'cache_creation_input_tokens': 0,
                                                     'cache_read_input_tokens': 0}}})
            with open(tp, 'w') as f:
                f.write(line + '\n')
            env = dict(sb.env, CLAUDE_SL_NO_REFRESH='1')
            subprocess.run(['bash', SCRIPT, '--refresh', sb.repo, tp, 'rt', 'sub'],
                           env=env, capture_output=True)
            cache = os.path.join(sb.tmp, 'claude-sl-tok-rt.cache')
            self.assertTrue(os.path.isfile(cache), '刷新没有写出 token 缓存')
            ts = cache + '.ts'
            self.assertTrue(os.path.isfile(ts), '刷新没有写出新鲜度时间戳')
            with open(ts) as f:
                self.assertLess(abs(now - int(f.read().strip())), 60)

    def test_soon_exhausted_stays_red(self):
        """bad 但 out 是真实倒计时（不是「用尽」）：设计上仍应变红，不能被护栏误伤。"""
        with Sandbox() as sb:
            sb.write_tok('soon', q5lv='bad', q5out='36m')
            write_ts(sb, 'soon', age=0)
            out = sb.render(sb.stdin('soon', q5=3))
            self.assertIn('out 36m', strip(out))
            self.assertEqual(color_of(out, '3%'), sb.theme()['T_CRIT'])


if __name__ == '__main__':
    unittest.main()
