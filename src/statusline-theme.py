#!/usr/bin/env python3
"""Claude Code 状态栏配色助手（由 statusline-command.sh 的 --refresh 后台模式调用）。

用法：statusline-theme.py <THEME> <缓存路径> [pill_chroma] [pill_style] [格式版本]
  THEME = auto | tokyonight | dracula | catppuccin | onedark | nord
  pill_style = tint | cool | jewel | glass | mono | pastel | tokyo-glass（见 STYLES）
  格式版本由 bash 侧 PALETTE_V 传入，原样写进首行——版本号只在 bash 一处维护

写入缓存：首行 THEME=<请求值>/<pill_chroma>/<pill_style>/v<格式版本>，次行 NAME=<实际主题>，
其余每行 KEY=R;G;B（W_ACC 例外，是字重 SGR）。bash 同步路径只读缓存、不做颜色计算：
  P_GIT / P_MODEL / P_CTX / P_Q5 / P_Q7 / P_USD / P_TAIL
                   各组胶囊底色，由胶囊风格 STYLES 生成（pill_style 可选）；模型、尾段胶囊各有
                   自己的键，具体颜色由风格决定
  P_NEU            告警胶囊（cache / claude）底色 = 基础表面色（pastel 为模型胶囊色）
  T_NEU            主值文字（终端前景色；bash 侧加粗）
  T_MODEL          模型名（tokyo-glass 为冷蓝；其余风格 = T_NEU）
  T_SEC            次值文字（中性灰，同组里的附属数字：ctx 量、重置倒计时、$/h 等）
  T_TER            第三级统计值：hit / io / api（tokyo-glass 低于次值；其余风格 = T_SEC）
  L_CTX / L_QUOTA / L_Q7 / L_USD
                   各组标签色（ctx / 5h / 7d / 费用）：主题强调色混入前景色降饱和，刻意
                   避开黄、红色相——那两种只留给告警；风格带 label='pill' 时改取各自
                   胶囊的色相，底与标签成对
  T_DIR / T_GIT    目录、分支的淡主题色点缀
  T_WARN / T_BAD   语义色：提醒（黄）/ 危险（红），只用在需要注意的那一项文字上
  T_CRIT           强危险：已用 ≥ crit_at 或很快用完（tokyo-glass 为明显红；其余风格 = T_BAD）
  T_MUTED / T_DIM  尾段标签弱化字 / 分隔符
  W_ACC            会话费用的字重 SGR：1 加粗 / 22 常规（tokyo-glass 降为二级、不加粗）

  1. 终端识别（THEME=auto）：在 iTerm 里（TERM_PROGRAM=iTerm.app 或 LC_TERMINAL=iTerm2，
     后者在 tmux 里也在）读当前 profile（ITERM_PROFILE 名，缺省取默认 profile）的 ANSI
     1–6 号色，与各主题标准终端色逐色 CIE76 ΔE 求和，取最小者；profile 分深浅色时按
     macOS 当前外观取对应一套。非 iTerm、或读 plist 出任何异常（缺键、损坏），回落
     tokyonight——异常不能让整个色板生成失败，否则状态栏一直无色。
  2. 基础表面色 = 主题官方表面色（SURFACE；Dracula 为 current-line #44475a，与 iTerm
     Dracula 预设的 Selection Color 相同），须与【profile 背景】和【主题标准背景】两者的
     CIE76 ΔE 都 ≥ MIN_PILL_DE，否则逐步向中性字色提亮（上限 40%）。这道门槛只约束
     基础表面色；各风格的组胶囊明度、彩度由风格定义，不逐组套这道门槛（逐组提亮会抹平
     风格的明暗节奏，见 docs/DECISIONS.md）。要对两种背景验收，是因为 iTerm 的透明度 /
     模糊只提亮默认背景、胶囊底色不透明（实测背景从 profile 的 #1e1f29 变成 #272a36）。
  3. 字色从目标色出发逐步混白或混墨（取与底色对比度更高的一端），直到与「最难达标的
     那块胶囊」的 WCAG 对比度达标：浅字对最亮的胶囊、深字对最暗的胶囊；尾段也在胶囊里，
     同样对胶囊校正。文字极性按胶囊实际明暗决定（不按终端底色：中灰底上胶囊可能比底色
     更暗，浅字压在亮胶囊上只有 3.76）。中灰底色上 7:1 数学上无解，此时退到 MIN_CONTRAST。

任何未预料的异常静默退出（不写缓存），bash 侧沿用旧色板或回落无色，并按 THEME_TTL 退避重试。
"""
import math
import os
import plistlib
import subprocess
import sys

MIN_PILL_DE = 12     # 胶囊底与 profile 背景、主题标准背景的最小 CIE76 ΔE
# 各主题官方表面色（胶囊底）
SURFACE = {'tokyonight': '#3b4261', 'dracula': '#44475a', 'catppuccin': '#45475a',
           'onedark': '#3e4452', 'nord': '#434c5e'}
ACCENT_MIX = 45      # 目录 / 分支点缀色中强调色的占比（%），其余为中性字色
PILL_CHROMA = 12     # tint 风格的淡染彩度（argv[3] 可覆盖；0 = 统一底色）
PILL_STYLE = 'tint'  # 胶囊风格（argv[4] 可覆盖），见 STYLES

# 固定色相（取自 Dracula 强调色，冷色 / 宝石 / 粉彩风格用；与终端主题无关）
HUES = {'indigo': '#6272a4', 'teal': '#50e3c2', 'purple': '#bd93f9', 'blue': '#6c8cff',
        'cyan': '#8be9fd', 'pink': '#ff79c6', 'green': '#50fa7b'}
GROUPS = ('git', 'model', 'ctx', 'q5', 'q7', 'usd', 'tail')
# 胶囊风格（先做了 A–F 六种，看完 iTerm 实渲染预览后全部保留为配置项）。
# 每组 (色相, 明度, 彩度)：色相 = 'key' 取主题色板（THEMES 键）、HUES 名取固定色相、
# None 取表面色自身的色相；明度 dark 风格为相对表面色明度的偏移（适配各主题），
# light 风格为绝对值。label='pill' 时标签色取自己胶囊的色相，否则取主题组色。
#   tint   A 同明度淡染：只换色相、彩度 = pill_chroma（第十版；暖色组偏棕，用户嫌土气）
#   cool   B 冷色系：蓝 / 靛 / 紫 / 青 / 蓝绿，Tokyo Night / lualine 气质
#   jewel  C 宝石色：更深更纯、避开棕 / 橄榄色相，GitHub / Linear 标签气质
#   glass  D 极简玻璃：纯中性灰两档明暗交替，颜色只在标签与告警上
#   mono   E 单色深浅：只用主题紫，靠明暗区分
#   pastel F 粉彩亮块：浅色马卡龙底 + 墨色字，Catppuccin / starship 气质
#   tokyo-glass  B / C / D 融合：Tokyo Night 冷色 Token + 近背景
#          的「玻璃」胶囊 + 三级信息层级，见 glass_tokens()
STYLES = {
    'tint':   dict(label='theme', groups={'git': ('git', 0, None), 'model': (None, 0, 0),
                   'ctx': ('ctx', 0, None), 'q5': ('q5', 0, None), 'q7': ('q7', 0, None),
                   'usd': ('usd', 0, None), 'tail': (None, 0, 0)}),
    'cool':   dict(label='pill', groups={'git': ('indigo', -0.6, 18), 'model': (None, 0, 0),
                   'ctx': ('teal', -0.6, 18), 'q5': ('purple', -0.6, 18), 'q7': ('blue', -0.6, 18),
                   'usd': ('cyan', -0.6, 18), 'tail': (None, 0, 0)}),
    'jewel':  dict(label='pill', groups={'git': ('blue', -4.6, 26), 'model': (None, -4.6, 4),
                   'ctx': ('teal', -4.6, 24), 'q5': ('purple', -4.6, 28), 'q7': ('pink', -4.6, 26),
                   'usd': ('cyan', -4.6, 22), 'tail': (None, -4.6, 4)}),
    'glass':  dict(label='theme', groups={'git': (None, 2.4, 1.5), 'model': (None, -3.6, 1.5),
                   'ctx': (None, 2.4, 1.5), 'q5': (None, -3.6, 1.5), 'q7': (None, 2.4, 1.5),
                   'usd': (None, -3.6, 1.5), 'tail': (None, 2.4, 1.5)}),
    'mono':   dict(label='theme', groups={'git': ('q5', 3.4, 14), 'model': ('q5', -4.6, 14),
                   'ctx': ('q5', 3.4, 14), 'q5': ('q5', -4.6, 14), 'q7': ('q5', 3.4, 14),
                   'usd': ('q5', -4.6, 14), 'tail': ('q5', -0.6, 14)}),
    'tokyo-glass': dict(tokens=True),
    'pastel': dict(label='pill', light=True, groups={'git': ('pink', 82, 26), 'model': (None, 86, 4),
                   'ctx': ('green', 84, 30), 'q5': ('purple', 80, 28), 'q7': ('blue', 82, 26),
                   'usd': ('cyan', 84, 24), 'tail': (None, 78, 4)}),
}
LABEL_MIX = 60       # 组标签色中强调色的占比（%）
LABEL_CHROMA = 0.75  # 标签彩度上限 = 告警色（黄 / 红中较低者）彩度 × 此值：标签再鲜艳也不压过告警
PRI_CONTRAST = 7.0   # 主值字与胶囊底的最低对比度：明显亮于次值（≥ 4.5），主次才分得开
SEC_MIX = 55         # 次值灰中前景色的占比（%），其余为胶囊底；不足 MIN_CONTRAST 时自动提亮
MIN_CONTRAST = 4.5   # WCAG AA
DIM_CONTRAST = 2.0   # 分隔符只需可辨

# 各主题：标准背景、标准终端 ANSI 1–6（红绿黄蓝品青，用于识别），以及角色强调色
THEMES = {
    'tokyonight': dict(q7='#7aa2f7', bg='#1a1b26', ansi=['#f7768e', '#9ece6a', '#e0af68', '#7aa2f7', '#bb9af7', '#7dcfff'],
                       dir='#7aa2f7', git='#ff9e64', model='#a9b1d6', ctx='#9ece6a', q5='#bb9af7',
                       usd='#7dcfff', warn='#e0af68', bad='#f7768e', muted='#787c99', dim='#4a5072'),
    'dracula':    dict(q7='#ff79c6', bg='#282a36', ansi=['#ff5555', '#50fa7b', '#f1fa8c', '#bd93f9', '#ff79c6', '#8be9fd'],
                       dir='#ff79c6', git='#ffb86c', model='#c6c8d6', ctx='#50fa7b', q5='#bd93f9',
                       usd='#8be9fd', warn='#f1fa8c', bad='#ff5555', muted='#8a92bf', dim='#55597a'),
    'catppuccin': dict(q7='#f5c2e7', bg='#1e1e2e', ansi=['#f38ba8', '#a6e3a1', '#f9e2af', '#89b4fa', '#f5c2e7', '#94e2d5'],
                       dir='#89b4fa', git='#fab387', model='#a6adc8', ctx='#a6e3a1', q5='#cba6f7',
                       usd='#89dceb', warn='#f9e2af', bad='#f38ba8', muted='#7f849c', dim='#45475a'),
    'onedark':    dict(q7='#61afef', bg='#282c34', ansi=['#e06c75', '#98c379', '#e5c07b', '#61afef', '#c678dd', '#56b6c2'],
                       dir='#61afef', git='#d19a66', model='#abb2bf', ctx='#98c379', q5='#c678dd',
                       usd='#56b6c2', warn='#e5c07b', bad='#e06c75', muted='#848b98', dim='#4b5263'),
    'nord':       dict(q7='#81a1c1', bg='#2e3440', ansi=['#bf616a', '#a3be8c', '#ebcb8b', '#81a1c1', '#b48ead', '#88c0d0'],
                       dir='#81a1c1', git='#d08770', model='#d8dee9', ctx='#a3be8c', q5='#b48ead',
                       usd='#88c0d0', warn='#ebcb8b', bad='#bf616a', muted='#8c96ab', dim='#4c566a'),
}


def rgb(h):
    h = h.lstrip('#')
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(c):
    r, g, b = map(lin, c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted([lum(a), lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def lab(c):
    r, g, b = map(lin, c)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def mix(a, b, p):
    return [round(x * p / 100 + y * (100 - p) / 100) for x, y in zip(a, b)]


INK = [20, 20, 28]                  # 浅色胶囊（pastel）上的墨色


WHITE = [255, 255, 255]


def is_light(c):
    """浅底 = 墨色字比白字对比度更高（与 legible 的方向判据同源；此前用固定亮度阈值 0.18，
    与两端对比度相等的交点约 0.196 不一致，灰 118 上会选墨色只得 4.03 而白字可达 4.54）。"""
    return contrast(INK, c) > contrast(WHITE, c)


def legible(fg, bg, target):
    """fg 逐步混向对比度更高的一端（白或墨）直到与 bg 对比度达标（色相保持，只改明暗）。
    达不到 target 时退到 MIN_CONTRAST（中灰底上 7:1 无解，见文档 3）；仍达不到则取端点。
    首选端由「底色偏浅还是偏深」决定；若它连 MIN_CONTRAST 都达不到，换另一端再试一次
    ——中灰底上胶囊被抬到偏亮、首选端却仍是白，墨色那端才达标（对比度矩阵测试发现）。"""
    ends = [INK, WHITE] if is_light(bg) else [WHITE, INK]
    for end in ends:
        for goal in (target, MIN_CONTRAST):
            for p in range(0, 101, 4):
                c = mix(end, fg, p)
                if contrast(c, bg) >= goal:
                    return c
        if contrast(end, bg) < MIN_CONTRAST:
            continue                              # 这一端连下限都够不到，换另一端
        return end
    return max(ends, key=lambda e: contrast(e, bg))


def lab2rgb(L, a, b):
    """CIELAB（D65）→ sRGB，越界截断。"""
    fy = (L + 16) / 116
    g = lambda t: t ** 3 if t ** 3 > 0.008856 else (t - 16 / 116) / 7.787
    x, y, z = g(fy + a / 500) * 0.95047, g(fy), g(fy - b / 200) * 1.08883
    enc = lambda c: 255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)
    return [max(0, min(255, round(enc(v)))) for v in (3.2406 * x - 1.5372 * y - 0.4986 * z,
                                                      -0.9689 * x + 1.8758 * y + 0.0415 * z,
                                                      0.0557 * x - 0.2040 * y + 1.0570 * z)]


def lch(L, c, accent):
    """明度 L + accent 的色相 + 彩度 c。"""
    _, a, b = lab(accent)
    h = math.atan2(b, a)
    return lab2rgb(L, c * math.cos(h), c * math.sin(h))


def chroma(c):
    """CIELAB 彩度（鲜艳程度）。"""
    _, a, b = lab(c)
    return math.hypot(a, b)


def iterm_profile():
    """返回当前 iTerm profile 的 (背景色, ANSI 1–6, 前景色)；不可得时返回 None。"""
    path = os.path.expanduser('~/Library/Preferences/com.googlecode.iterm2.plist')
    in_iterm = os.environ.get('TERM_PROGRAM') == 'iTerm.app' or os.environ.get('LC_TERMINAL') == 'iTerm2'
    if not in_iterm or not os.path.isfile(path):
        return None
    with open(path, 'rb') as f:
        d = plistlib.load(f)
    books = d.get('New Bookmarks') or []
    name = os.environ.get('ITERM_PROFILE')
    prof = next((b for b in books if name and b.get('Name') == name), None)
    prof = prof or next((b for b in books if b.get('Guid') == d.get('Default Bookmark Guid')), None)
    if not prof:
        return None
    suffix = ''
    if prof.get('Use Separate Colors for Light and Dark Mode'):
        dark = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                              capture_output=True, text=True).stdout.strip() == 'Dark'
        suffix = ' (Dark)' if dark else ' (Light)'

    def col(key):
        v = prof.get(key + suffix) or prof.get(key)
        return [round(v[k] * 255) for k in ('Red Component', 'Green Component', 'Blue Component')]

    return col('Background Color'), [col(f'Ansi {i} Color') for i in range(1, 7)], col('Foreground Color')


# ── tokyo-glass Token（Tokyo Night 调色板为色相与色温来源，明暗相对终端背景计算）──
TG = dict(fg='#c0caf5', fg_muted='#8992b3', fg_faint='#737aa2', slate='#565f89',
          blue='#7aa2f7', cyan='#7dcfff', purple='#bb9af7', amber='#e0af68', red='#f7768e')
# 胶囊：(相对背景的明度增量, 彩度, 色相)。一级（模型 / ctx / 额度）略亮且带一点冷色相，
# 二级（项目 / 费用）、三级（统计）更贴近背景、几乎无色——「玻璃」靠贴近背景的轻微色差，
# 不靠大面积填色；告警胶囊（cache / claude）同二级，语义只由文字表达。
TG_PILLS = {'model': (10, 9, 'blue'), 'ctx': (10, 8, 'cyan'), 'q5': (10, 9, 'purple'),
            'q7': (8, 8, 'purple'), 'git': (6, 5, 'slate'), 'usd': (6, 5, 'slate'),
            'tail': (5, 4, 'slate'), 'neu': (6, 5, 'slate')}
TG_MIN_DE = 6        # 胶囊与终端背景（profile 背景与主题标准背景两者）的最小 ΔE：贴近但可辨
# 文字对比度分级：一级 / 二级 / 三级
TG_C1, TG_C2, TG_C3 = 7.0, 5.5, 4.5
TG_CM = 5.5          # 模型名：一级信息，但用色相而非亮度突出


def glass_tokens(bgs, put):
    """bgs = 可能的终端背景（profile 背景、主题标准背景；iTerm 透明度会把前者提亮成后者）。"""
    # 浅色终端背景：胶囊改往暗处偏、文字对最暗的胶囊校正（此前一律往亮处偏，白底上
    # 胶囊全被截成 #ffffff、与背景 ΔE 为 0——对比度矩阵测试发现）
    # 深浅只看 profile 背景（bgs[0]）：内置五套主题的标准背景全是深色，浅色终端下它不代表
    # 实际背景，不参与验收（否则浅底被误判成深底）
    lt = is_light(bgs[0])
    if lt:
        bgs = bgs[:1]
    ref_bg = (min if lt else max)(bgs, key=lum)    # 暗底以更亮的为基准，胶囊不至于融进实际背景
    Lb = lab(ref_bg)[0]
    sign = -1 if lt else 1
    pills = {}
    for g, (dl, c, h) in TG_PILLS.items():
        p = lch(Lb + sign * dl, c, rgb(TG[h]))
        while min(math.dist(lab(p), lab(b)) for b in bgs) < TG_MIN_DE and dl < 20:
            dl += 1
            p = lch(Lb + sign * dl, c, rgb(TG[h]))
        pills[g] = p
    put('P_NEU', pills['neu'])
    for g in GROUPS:
        put('P_' + g.upper(), pills[g])
    # 文字极性按胶囊实际明暗定，而非按终端底色：中灰底（#808080 一类）上胶囊可能被推向
    # 比底色更暗的一侧，此时浅字对最亮胶囊只有 3.76，必须改浅字为深字
    light_text = min(contrast(WHITE, p) for p in pills.values()) >= \
        min(contrast(INK, p) for p in pills.values())
    ref = (max if light_text else min)(pills.values(), key=lum)   # 浅字对最亮的胶囊、深字对最暗的最难达标
    fg, muted = (rgb(TG['fg']), rgb(TG['fg_muted'])) if light_text else (INK, rgb('#4c5374'))
    warn = legible(rgb(TG['amber']), ref, MIN_CONTRAST)
    soft_red = legible(mix(rgb(TG['red']), muted, 70), ref, MIN_CONTRAST)
    crit = legible(rgb(TG['red']), ref, MIN_CONTRAST)              # 保住饱和度，靠纯度 + 加粗区分于柔和红
    cap = LABEL_CHROMA * min(chroma(warn), chroma(soft_red))

    def label(h, target):
        share, c = LABEL_MIX, None
        while share > 10:
            c = legible(mix(rgb(TG[h]), muted, share), ref, target)
            if chroma(c) <= cap:
                break
            share -= 5
        return c

    put('T_NEU', legible(fg, ref, TG_C1))                         # 一级主值
    put('T_MODEL', legible(mix(rgb(TG['blue']), fg, 80), ref, TG_CM))   # 冷蓝：一级信息靠色相突出，对比度取二级下限
    put('T_SEC', legible(muted, ref, TG_C2))                      # 二级
    put('T_TER', legible(rgb(TG['fg_faint']), ref, TG_C3))       # 三级
    put('T_DIR', legible(mix(rgb(TG['blue']), muted, 30), ref, TG_C2))
    put('T_GIT', legible(mix(rgb(TG['purple']), muted, 30), ref, TG_C2))
    put('L_CTX', label('cyan', TG_C2))
    put('L_QUOTA', label('purple', TG_C2))
    put('L_Q7', label('purple', TG_C2))
    put('L_USD', legible(mix(rgb(TG['fg']), muted, 50), ref, TG_C2))   # 费用：二级，不着色
    put('T_WARN', warn)
    put('T_BAD', soft_red)
    put('T_CRIT', crit)
    put('T_MUTED', legible(rgb(TG['slate']), ref, TG_C3))
    put('T_DIM', legible(rgb(TG['slate']), ref, DIM_CONTRAST))
    lines_extra = ['W_ACC=22']
    return lines_extra


def main():
    want, out = sys.argv[1], sys.argv[2]
    pc = int(sys.argv[3]) if len(sys.argv) > 3 else PILL_CHROMA
    ps = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] in STYLES else PILL_STYLE
    ver = sys.argv[5] if len(sys.argv) > 5 else '0'
    try:
        term = iterm_profile() if want == 'auto' else None
    except Exception:                             # plist 缺键 / 损坏 → 回落 tokyonight（文档 1）
        term = None
    if want in THEMES:
        name = want
    elif term:
        name = min(THEMES, key=lambda t: sum(math.dist(lab(rgb(a)), lab(b))
                                             for a, b in zip(THEMES[t]['ansi'], term[1])))
    else:
        name = 'tokyonight'
    t = THEMES[name]
    bg = term[0] if term else rgb(t['bg'])

    style = STYLES[ps]
    light = style.get('light', False)
    lines = [f'THEME={want}/{pc}/{ps}/v{ver}', f'NAME={name}']
    if style.get('tokens'):
        put = lambda k, c: lines.append(f'{k}=' + ';'.join(map(str, c)))
        lines += glass_tokens([bg, rgb(t['bg'])], put)
        return write(out, lines)
    neu = term[2] if term else rgb(t['model'])        # 常态字 = 终端前景色
    pill, base = rgb(SURFACE[name]), rgb(SURFACE[name])
    lift = 0
    while min(math.dist(lab(pill), lab(bg)), math.dist(lab(pill), lab(rgb(t['bg'])))) < MIN_PILL_DE \
            and lift < 40:
        lift += 3
        pill = mix(neu, base, lift)
    put = lambda k, c: lines.append(f'{k}=' + ';'.join(map(str, c)))
    surf_L = lab(pill)[0]

    def hue_src(h):
        return pill if h is None else rgb(HUES[h]) if h in HUES else rgb(t[h])

    pills = {}
    for g in GROUPS:
        h, dl, c = style['groups'][g]
        c = pc if c is None else c
        L = dl if light else surf_L + dl
        pills[g] = pill if (c == 0 and not light and dl == 0) else lch(L, c, hue_src(h))
    put('P_NEU', pills['model'] if light else pill)
    for g in GROUPS:
        put('P_' + g.upper(), pills[g])

    # 文字对「最难达标」的那块胶囊校正：暗底浅字对最亮的、浅底墨字对最暗的，保证每块都达标
    ref = (min if light else max)(list(pills.values()) + [pill if not light else pills['model']], key=lum)
    ink = INK if light else neu
    tone = (lambda c, p: mix(c, INK, 100 - p)) if light else (lambda c, p: mix(c, neu, p))
    put('T_NEU', legible(ink, ref, PRI_CONTRAST))
    put('T_DIR', legible(tone(rgb(t['dir']), 35 if light else ACCENT_MIX), ref, MIN_CONTRAST))
    put('T_GIT', legible(tone(rgb(t['git']), 35 if light else ACCENT_MIX), ref, MIN_CONTRAST))
    put('T_SEC', legible(mix(ink, ref, SEC_MIX), ref, MIN_CONTRAST))
    if light:
        warn, bad = legible(rgb('#8a6d00'), ref, MIN_CONTRAST), legible(rgb('#c0262d'), ref, MIN_CONTRAST)
    else:
        warn, bad = legible(rgb(t['warn']), ref, MIN_CONTRAST), legible(rgb(t['bad']), ref, MIN_CONTRAST)
    cap = LABEL_CHROMA * min(chroma(warn), chroma(bad))
    for key, g in (('L_CTX', 'ctx'), ('L_QUOTA', 'q5'), ('L_Q7', 'q7'), ('L_USD', 'usd')):
        src = hue_src(style['groups'][g][0]) if style['label'] == 'pill' else rgb(t['q5' if g == 'q7' else g])
        share = 35 if light else LABEL_MIX
        lab_c = legible(tone(src, share), ref, MIN_CONTRAST)
        while chroma(lab_c) > cap and share > 10:
            share -= 5
            lab_c = legible(tone(src, share), ref, MIN_CONTRAST)
        put(key, lab_c)
    put('T_WARN', warn)
    put('T_BAD', bad)
    put('T_MUTED', legible(rgb(t['muted']) if not light else mix(rgb(t['muted']), INK, 40), ref, MIN_CONTRAST))
    put('T_DIM', legible(rgb(t['dim']), ref, DIM_CONTRAST))
    kv = dict(x.split('=', 1) for x in lines)
    lines += [f"T_MODEL={kv['T_NEU']}", f"T_TER={kv['T_SEC']}", f"T_CRIT={kv['T_BAD']}", 'W_ACC=1']
    write(out, lines)


def write(out, lines):
    tmp = f'{out}.{os.getpid()}'
    with open(tmp, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, out)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
