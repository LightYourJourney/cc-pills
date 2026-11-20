"""并行跑测试：把各模块的用例按轮转切成 N 片，每片一个 `python3 -m unittest` 进程。

每个渲染用例本来就在独立沙箱里（私有缓存根、临时 HOME），彼此不共享状态，可以并行。
对时序敏感的模块（tokens 的共享时间预算、subagent 的并发抢锁）在并行段之后串行跑，
免得被别的分片抢 CPU 而误报。用法见 run_tests.sh。
"""
import concurrent.futures
import os
import re
import subprocess
import sys
import unittest

PARALLEL = ['test_render', 'test_theme', 'test_cache', 'test_freshness', 'test_install', 'test_doctor']
SERIAL = ['test_tokens', 'test_subagent']


def test_ids(mods):
    out = []

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                out.append(t.id())
    walk(unittest.defaultTestLoader.loadTestsFromNames(mods))
    return out


def run(ids):
    r = subprocess.run([sys.executable, '-m', 'unittest', *ids], capture_output=True, text=True)
    return r.returncode, r.stderr + r.stdout


def tally(text):
    """unittest 输出末尾的「Ran N tests」与「(skipped=M)」→ (N, M)。"""
    ran = re.search(r'^Ran (\d+) test', text, re.M)
    skipped = re.search(r'skipped=(\d+)', text)
    return int(ran.group(1)) if ran else 0, int(skipped.group(1)) if skipped else 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    found = {f[:-3] for f in os.listdir(here) if f.startswith('test_') and f.endswith('.py')}
    missing = found - set(PARALLEL) - set(SERIAL)
    if missing:                                # 新加的模块必须归到某一段，不能被静默漏跑
        print(f'未归段的测试模块：{" ".join(sorted(missing))}（加进 _parallel.py 的 PARALLEL 或 SERIAL）')
        return 1
    if os.environ.get('CCPILLS_SERIAL') == '1':   # 全部串行 = 一片，走同一套汇总
        jobs = 1
    else:
        jobs = int(os.environ.get('CCPILLS_JOBS') or min(os.cpu_count() or 2, 8))
    ids = test_ids(PARALLEL)
    shards = [ids[i::jobs] for i in range(jobs) if ids[i::jobs]]
    print(f'并行段：{len(ids)} 项，{len(shards)} 片', flush=True)
    with concurrent.futures.ThreadPoolExecutor(len(shards)) as ex:
        results = list(ex.map(run, shards))
    print(f'串行段：{", ".join(SERIAL)}', flush=True)
    results.append(run(SERIAL))
    rc = total = skipped = 0
    for code, text in results:
        n, k = tally(text)
        total += n
        skipped += k
        if code:
            rc = 1
            print(text, flush=True)
    # 跳过数要打出来：成功时不打印 unittest 原文，CI 日志里只能靠这一行看出有没有用例被跳过
    print(f'共 {total} 项，跳过 {skipped} 项：{"OK" if rc == 0 else "FAILED"}', flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
