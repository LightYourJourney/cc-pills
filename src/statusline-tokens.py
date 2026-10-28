#!/usr/bin/env python3
"""Claude Code 状态栏 token 段助手（由 statusline-command.sh 的 --refresh 后台模式调用）。

从 transcript 增量聚合两个 stdin JSON 给不了的指标：
  1. session 累计 input/output tokens（transcript 求和，含本文件内的 sidechain 条目）
  2. 计费口径烧速 tokens/min（in×1 + cacheW×1.25 + cacheR×0.1 + out×5，
     与 /token-stats 同权重；原始口径 98% 是 cache_read，烧速会虚高到没意义）

输出单行、tab 分隔的两段纯文本："io <in>/<out>\t<rate>/min"（bash 侧上色并以
· 连接；烧速数据不足时只有第一段）；数据不完整或任何异常时输出空 → 整段隐藏。

⚠️ 估算值声明：这是客户端 transcript 聚合的估算，不是账单权威值
（见 docs/notes/客户端自报成本是估算不是账单.md）。

工程约束（v2.1.235 二进制实测结论，修改时必须保留）：
  1. 本脚本跑在后台刷新进程里，不阻塞渲染，但仍可能被 abort 波及杀死——
     单次调用最多解析 MAX_CHUNK 字节（断点在状态文件），状态写入必须原子
     （tmp + os.replace），被杀只丢一片进度、绝不污染状态。
  2. 追平文件尾之前不输出累计段：宁缺勿假，不显示爬升中的半截数字。
  3. 绝不向 stdout 输出错误信息；任何异常静默吞掉。
  4. 同一 API 响应会以多行重复出现（每内容块一行），且**后写的行会修正 usage**
     （实测：首行可为全 0 占位，末行才有真实值；重复最大间距 164 个 usage 行）。
     去重用「可修正 pending 映射」：id 首见计入总量并挂入 pending（FIFO 上限
     1024 = 实测最大间距 ×6），重见时按差值修正总量；挤出 pending 的贡献冻结。
"""
import json
import os
import sys
import time

MAX_CHUNK = 32 * 1024 * 1024  # 单次调用最多解析的字节数（后台运行，可放大分片）
WINDOW = 60                   # 烧速窗口（秒）
SAMPLE_KEEP = 90              # 采样保留时长（秒）
PEND_KEEP = 1024              # 可修正 pending 映射的 FIFO 上限（见约束 4）

EMPTY = {'offset': 0, 'in': 0, 'out': 0, 'cw': 0, 'cr': 0, 'pend': [], 'samples': []}
KEYS = ('in', 'out', 'cw', 'cr')


def fmt(n):
    if n >= 1_000_000:
        v = n / 1_000_000
        return f'{int(v)}M' if v == int(v) else f'{v:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}k'
    return str(int(n))


def main():
    if len(sys.argv) < 3:
        return
    path, sid = sys.argv[1], sys.argv[2]
    if not os.path.isfile(path):
        return
    state_path = os.path.join('/tmp', f'claude-statusline-{os.path.basename(sid)}.json')
    st = dict(EMPTY)
    try:
        with open(state_path, encoding='utf-8') as f:
            st.update(json.load(f))
    except Exception:
        pass
    if os.path.getsize(path) < st['offset']:   # 文件被截断/重建 → 重扫
        st = dict(EMPTY)

    with open(path, 'rb') as f:
        f.seek(st['offset'])
        raw = f.read(MAX_CHUNK)
    caught_up = len(raw) < MAX_CHUNK           # 读不满一片 = 已到当时的文件尾
    nl = raw.rfind(b'\n')                      # 末尾不完整行留给下次
    raw = raw[:nl + 1] if nl >= 0 else b''
    st['offset'] += len(raw)

    pend = dict((k, tuple(v)) for k, v in st['pend'])   # id -> 当前计入总量的四元组
    order = [k for k, _ in st['pend']]                  # FIFO 顺序
    for line in raw.split(b'\n'):
        if b'"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get('message')
        if not isinstance(msg, dict):
            continue
        u = msg.get('usage')
        if not isinstance(u, dict):
            continue
        vals = (u.get('input_tokens') or 0, u.get('output_tokens') or 0,
                u.get('cache_creation_input_tokens') or 0,
                u.get('cache_read_input_tokens') or 0)
        mid = msg.get('id')
        if mid and mid in pend:                         # 重见：按差值修正总量
            old = pend[mid]
            for k, nv, ov in zip(KEYS, vals, old):
                st[k] += nv - ov
            pend[mid] = vals
            continue
        for k, v in zip(KEYS, vals):                    # 首见（或无 id）：直接累加
            st[k] += v
        if mid:
            pend[mid] = vals
            order.append(mid)
            while len(order) > PEND_KEEP:               # 挤出最老的，贡献冻结
                pend.pop(order.pop(0), None)
    st['pend'] = [[k, list(pend[k])] for k in order if k in pend]

    now = time.time()
    billed = st['in'] + st['cw'] * 1.25 + st['cr'] * 0.1 + st['out'] * 5
    samples = [s for s in st['samples'] if now - s[0] <= SAMPLE_KEEP]
    if caught_up:
        samples.append([now, billed])
    st['samples'] = samples
    try:
        tmp = state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        os.replace(tmp, state_path)
    except Exception:
        pass

    if not caught_up:
        return                                 # 未追平不出数（约束 2）
    parts = [f"io {fmt(st['in'])}/{fmt(st['out'])}"]
    aged = [s for s in samples if now - s[0] >= 10]   # 不足 10s 数据不算烧速
    if aged:
        base = min(aged, key=lambda s: abs((now - s[0]) - WINDOW))
        dt = now - base[0]
        rate = (billed - base[1]) / dt * 60
        if rate >= 0:
            parts.append(f'{fmt(rate)}/min')
    print('\t'.join(parts))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass                                   # 约束 3：状态栏绝不见错误
