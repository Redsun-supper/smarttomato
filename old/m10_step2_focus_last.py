# -*- coding: utf-8 -*-
"""
行空板 M10 · 智能番茄时钟 —— 第 2 段：语音开番茄钟 + 丝滑布局过渡

★ 最终版（LAST）—— 本项目**当前实际部署、上板运行**的就是这一个文件。
  命名约定：最终版文件名以 `_last` 结尾，一眼认出哪一版要用；中间版本不要用这个后缀。

    说「开始学习」→ 语音回答「好的，开始专注25分钟」→ 进入 25 分钟番茄钟，
    时间/日期在过渡中同步缩小、飞向左上角，倒计时在中央浮现。

完整说明（设计思路 / 精灵缓存 / 硬件与踩坑 / 用法 / 按键 / 远程调试钩子）见：
    docs/m10_step2_focus_last.md

运行环境：行空板 M10 板载 Python 3.7.3 + Pillow 9.0.1 + pinpong 0.6.x
"""

import os
import sys
import time
import queue
import argparse
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ===================== 设计令牌（全部取自 DESIGN.md）=====================

C_BG = "#FFFFFF"          # 背景
C_TIME = "#00838F"        # 时间 · 专注青
C_DATE = "#546E7A"        # 日期 · 次级文字
C_TIMER = "#00838F"       # 倒计时 · 专注青（与时间同色，一屏一主色）
C_TRACK = "#CFD8DC"       # 进度条底槽

FS_CLOCK = 62             # 待机时钟主显
FS_TIMER = 54             # 专注倒计时主显
FS_TIME_SMALL = 18        # 专注态左上角的小时间
FS_DATE = 18              # 待机日期
FS_DATE_SMALL = 14        # 专注态的小日期
FS_LABEL = 14             # 标签
FS_TIMER_MIN = 34         # 倒计时淡入时的起始字号（放大到 FS_TIMER）

LAND_W, LAND_H = 320, 240   # 横屏逻辑画布
SAFE_MARGIN = 12
CONTENT_W = LAND_W - 2 * SAFE_MARGIN
ROTATE = 270                # 逆时针旋转角度：90 / 270 互为 180° 翻转
PAD = 14                    # 专注态左上角文字的左内边距

# 待机态：两行都居中
TIME0_CX, TIME0_CY = 160, 98
DATE0_CX, DATE0_CY = 160, 170

# 专注态：两行都左对齐贴 PAD，上下叠放
TIME1_CY = 22
DATE1_CY = 44

# 专注态：倒计时居中
TIMER_CX, TIMER_CY = 160, 118
TIMER_LABEL_CY = 156
BAR_Y, BAR_H = 236, 4

TRANS_SEC = 0.9           # 过渡时长（秒）
ANIM_IDLE_SLEEP = 0.05    # 非过渡期的轮询间隔

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# ---- 字体栈（按顺序回退；禁止 uming.ttc）----
FONT_CN = [
    "HYQiHei_50S.ttf",                                # unihiker 自带汉仪旗黑
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",                      # 开发机预览用
    "C:/Windows/Fonts/simhei.ttf",
]
FONT_NUM = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
] + FONT_CN

# ---- 语音 ----
# ⚠️ 关键词必须是**空格分隔的拼音音节**，这是最容易踩的坑：
#     库的 add_command() 只是把字符串**逐字符**发 ASCII 码（见 pinpong/libs/dfrobot_asr.py），
#     真正解析音节的是模块固件。官方例子都是 `add_command("kai deng", 1)`、`("shang hai", 4)`。
#     写成 "kaishixuexi"（不分音节）固件解析不出来，**会一次都识别不到**（实测踩过）。
WAKE_WORDS = (("kai shi xue xi", 1),)   # 「开始学习」，拼音写在代码里，无需在模块上学习
VOICE_MODE = "LOOP"
VOICE_INPUT = "MIC"
VOICE_POLL_SEC = 0.05
VOICE_DEBUG = True
VOICE_HEARTBEAT_SEC = 30   # 一直没识别到时，每隔这么久打一行心跳，用来区分「线程死了」和「关键词没被识别」
TTS_VERSION = "V2"
TTS_VOLUME = 5
# ⚠️ 库的 begin(V2) 结尾会自己调 set_voice(1)（把音量压到 1），所以初始化后必须再设一次。
ACK_START_TIMEOUT = 3.0    # 等模块「开始合成」(0x41) 的最长等待（秒）
ACK_DONE_TIMEOUT = 30.0    # 等模块「空闲 / 播完」(0x4F) 的最长等待（秒）
START_REPLY = "好的，开始专注%d分钟"   # 触发后的语音回答
FINISH_REPLY = "专注结束，休息一下吧"    # 倒计时归零后的回答（归零会自动淡回待机时钟）

DEMO_DELAY_SEC = 8.0      # --demo：启动后多久自动触发
TRIGGER_FILE = "/tmp/step2_trigger"   # 调试用：程序运行期间 `touch` 一下就等于识别到「开始学习」

# =======================================================================


# ------------------------------ 字体 ------------------------------

def _find(paths):
    for name in paths:
        if name == "HYQiHei_50S.ttf":
            try:
                import unihiker as _u
                p = os.path.join(os.path.dirname(_u.__file__), name)
                if os.path.exists(p):
                    return p
            except Exception:
                pass
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            if os.path.exists(p):
                return p
            continue
        if os.path.exists(name):
            return name
    return None


FONT_CN_PATH = _find(FONT_CN)
FONT_NUM_PATH = _find(FONT_NUM)
_FONT_CACHE = {}


def load_font(path, size):
    key = (path, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    if path:
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            f = None
    if f is None:
        try:
            f = ImageFont.load_default(size)      # Pillow >= 10
        except TypeError:
            f = ImageFont.load_default()          # Pillow < 10（板载 9.0.1）
    _FONT_CACHE[key] = f
    return f


def cn(size):
    return load_font(FONT_CN_PATH, size)


def num(size):
    return load_font(FONT_NUM_PATH, size)


# ------------------------------ 排版 ------------------------------

def split_runs(text):
    """切成「数字段 / 中文段」交替的若干段，用于分段取字体（DESIGN.md 3.1）。"""
    runs, buf, is_num = [], "", None
    for ch in text:
        n = ord(ch) < 128 and not ch.isspace()
        if buf and n != is_num:
            runs.append((buf, is_num))
            buf = ""
        buf += ch
        is_num = n
    if buf:
        runs.append((buf, is_num))
    return runs


def text_width(text, font):
    try:
        return font.getlength(text)
    except Exception:
        img = Image.new("RGB", (1, 1))
        return ImageDraw.Draw(img).textlength(text, font=font)


def measure_mixed(text, f_cn, f_num):
    return sum(text_width(s, f_num if n else f_cn) for s, n in split_runs(text))


# --------------------------- 文字精灵（性能关键）---------------------------

# 行空板 M10 + Pillow 9.0.1 实测（这是本文件最重要的一组数字）：
#     ImageDraw.text   渲染一行 62px 时间      ≈ 21 ms
#     ImageDraw.textbbox 量一行 62px 文字      ≈  7 ms
#     把渲染好的小图 paste 回画布              ≈  1 ms
# 也就是说「每帧重新 draw.text」是过渡卡顿的根源（实测整帧 136ms ≈ 7fps）。
# 对策：把文字渲染成 RGBA 小图**缓存**起来，之后每帧只 paste。
#
# ⚠️ 缓存 key 里**不能放逐帧变化的量**。第一版踩了两个坑：
#     ① 把「白→青」淡入插值出来的**颜色**写进了 key；
#     ② 过渡中**字号**本来就在逐帧变，也被写进了 key。
#    结果过渡期间几乎每帧都是新 key、命中率接近 0 —— 等于白缓存，还把缓存撑爆；
#    而当时的淘汰策略是 `clear()` **整个清空**，连日期这种本该常驻的条目
#    也一起没了，越算越慢。
# 现在：
#     * 精灵一律用**最终颜色**渲染；淡入改成「贴图时按 alpha 合成」——
#       白底 + 青字按 alpha 叠加，数学上就等于「白→青」插值，但不产生新 key；
#     * 字号只缓存**两个端点**（它们本来就要用于静止态），中间字号用 resize
#       缩放得到 —— resize ≈2ms，重新 draw.text 要 8~20ms，而且缩放是连续的，
#       不用再量化到 2px 台阶，动画反而更顺；
#     * 淘汰改成**只丢最旧的一批**，绝不整个清空。

_SPRITE_CACHE = {}
_SPRITE_MAX = 192
_ALPHA_LUTS = {}

try:                                     # Pillow >= 9.1
    from PIL.Image import Resampling
    _BILINEAR = Resampling.BILINEAR
except Exception:                        # 板载 Pillow 9.0.1
    _BILINEAR = Image.BILINEAR


def _alpha_lut(alpha):
    """按 1/16 量化的 256 项查表，给 Image.point 用。

    必须传查表、不要传 lambda —— 传 lambda 会变成「每像素一次 Python 调用」，
    在板上能把 1ms 的操作拖成几十 ms。
    """
    q = int(round(max(0.0, min(1.0, alpha)) * 16))
    lut = _ALPHA_LUTS.get(q)
    if lut is None:
        lut = [int(round(v * q / 16.0)) for v in range(256)]
        _ALPHA_LUTS[q] = lut
    return lut


def _sprite(key, build):
    sp = _SPRITE_CACHE.get(key)
    if sp is not None:
        return sp
    if len(_SPRITE_CACHE) >= _SPRITE_MAX:
        # 只丢最旧的四分之一（dict 在 3.7+ 保序）。
        # **不要用 clear()** —— 那会把日期/标签这些本该常驻的条目也一起清掉。
        for k in list(_SPRITE_CACHE.keys())[:_SPRITE_MAX // 4]:
            del _SPRITE_CACHE[k]
    sp = build()
    _SPRITE_CACHE[key] = sp
    return sp


def text_sprite(text, path, size, fill):
    """单字体文字 → 缓存的 RGBA 小图（尺寸正好包住字形）。

    `fill` 只允许传**固定不变**的颜色；淡入请用 paste_center(..., alpha=...)，
    不要靠每帧换颜色 —— 那会让缓存每帧都失效。
    """
    key = (text, path, size, fill)

    def build():
        f = load_font(path, size)
        l, t, r, b = f.getbbox(text)
        im = Image.new("RGBA", (max(1, r - l), max(1, b - t)), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((-l, -t), text, font=f, fill=fill)
        return im

    return _sprite(key, build)


def mixed_sprite(text, cn_path, num_path, size, fill):
    """中英混排文字 → 缓存的 RGBA 小图：数字走 num_path，中文与空格走 cn_path。

    各段按**基线**对齐（用 font.getmetrics() 的 ascent 把每段统一下移），
    否则 DejaVu 的数字和汉仪旗黑的汉字会高低不齐。
    """
    key = (text, cn_path, num_path, size, fill)

    def build():
        runs = split_runs(text)
        widths, top, bottom = [], 0, 0
        for s, is_num in runs:
            f = load_font(num_path if is_num else cn_path, size)
            l, t, r, b = f.getbbox(s)
            asc = f.getmetrics()[0]
            widths.append(r - l)
            top = min(top, t - asc)          # 以基线为 0，往上为负
            bottom = max(bottom, b - asc)
        im = Image.new("RGBA", (max(1, sum(widths)), max(1, bottom - top)), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        x = 0
        for (s, is_num), w in zip(runs, widths):
            f = load_font(num_path if is_num else cn_path, size)
            d.text((x, -top - f.getmetrics()[0]), s, font=f, fill=fill)
            x += w
        return im

    return _sprite(key, build)


def paste_center(img, cx, cy, sp, alpha=1.0):
    """把精灵按中心贴到画布上。

    alpha < 1 时按透明度合成 —— 白底上「青字 + alpha」在数学上就等于
    「白→青」的颜色插值，但**不会产生新的缓存 key**，这正是淡入的正确做法。
    """
    box = (int(round(cx - sp.width / 2.0)), int(round(cy - sp.height / 2.0)))
    if alpha >= 0.999:
        img.paste(sp, box, sp)
    else:
        img.paste(sp, box, sp.getchannel("A").point(_alpha_lut(alpha)))


def _ref_size(size_lo, size_hi, target):
    """在两个端点字号里挑「缩放比更接近 1」的那个，保证两端都精确命中。"""
    return size_hi if target >= (size_lo + size_hi) * 0.5 else size_lo


def _scale(sp, ref, target):
    """把精灵缩放到目标字号对应的像素尺寸。"""
    if target == ref:
        return sp
    r = target / float(ref)
    return sp.resize((max(1, int(round(sp.width * r))),
                      max(1, int(round(sp.height * r)))), _BILINEAR)


def paste_text(img, cx, cy, text, path, size_lo, size_hi, target, fill, alpha=1.0):
    """按目标字号贴一行单字体文字：只缓存两端字号，中间靠缩放。

    过渡期间字号逐帧都在变，若按字号建 key 会**每帧都未命中**，等于没缓存。
    """
    ref = _ref_size(size_lo, size_hi, target)
    paste_center(img, cx, cy, _scale(text_sprite(text, path, ref, fill), ref, target), alpha)


def paste_mixed(img, cx, cy, text, cn_path, num_path, size_lo, size_hi, target, fill,
                alpha=1.0):
    """按目标字号贴一行中英混排文字（同上）。"""
    ref = _ref_size(size_lo, size_hi, target)
    sp = mixed_sprite(text, cn_path, num_path, ref, fill)
    paste_center(img, cx, cy, _scale(sp, ref, target), alpha)


# ------------------------------ 数值 ------------------------------

def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def lerp(a, b, t):
    return a + (b - a) * t


def ease_in_out(t):
    """easeInOutCubic：起步与收尾柔和，中段快 —— 动画「丝滑」的关键。"""
    t = clamp(t)
    if t < 0.5:
        return 4.0 * t * t * t
    u = -2.0 * t + 2.0
    return 1.0 - (u * u * u) / 2.0


def hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def lerp_color(h1, h2, t):
    """两个 HEX 之间插值。用来做「从背景白浮现」的淡入，省掉 RGBA 合成。"""
    a, b = hex_rgb(h1), hex_rgb(h2)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def mmss(sec):
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def done_ratio(remain, total):
    return 0.0 if total <= 0 else clamp(1.0 - remain / float(total))


# --------------------------- 整屏旋转（快路径）---------------------------

try:                                   # Pillow >= 9.1
    from PIL.Image import Transpose
    _ROT90 = Transpose.ROTATE_90
    _ROT270 = Transpose.ROTATE_270
except Exception:                      # Pillow < 9.1（板载 9.0.1）
    _ROT90 = Image.ROTATE_90
    _ROT270 = Image.ROTATE_270


def rotate_out(img, angle=ROTATE):
    """横屏逻辑画布 → 物理竖屏。

    rotate(270) 就是 transpose(ROTATE_270)，但 transpose 是纯内存搬运，
    比通用仿射变换的 rotate() 快得多 —— 过渡动画每帧都要做，这里省下的时间
    直接变成帧率。
    """
    return img.transpose(_ROT270 if angle == 270 else _ROT90)


# ------------------------------ 渲染器 ------------------------------

class Screen(object):
    """一个参数 p 画出所有帧。

        p = 0.0  待机时钟
        p = 1.0  专注倒计时
        0 < p < 1  过渡帧（时间/日期缩小 + 飞向左上，倒计时浮现）
    """

    def __init__(self):
        self._img = Image.new("RGB", (LAND_W, LAND_H), C_BG)
        self._draw = ImageDraw.Draw(self._img)
        # 专注态是「左对齐」，但绘制统一用居中 —— 所以先把最终居中的 x 算出来
        w_time = text_width("00:00:00", num(FS_TIME_SMALL))
        self.time1_cx = PAD + w_time / 2.0
        w_date = measure_mixed(self._sample_date(), cn(FS_DATE_SMALL), num(FS_DATE_SMALL))
        self.date1_cx = PAD + w_date / 2.0
        self._preload()

    def _preload(self):
        """预热真正会用到的字号（只有几个端点）。

        中间字号现在靠 resize 缩放得到，所以不必再预热整条字号阶梯 ——
        启动也从「加载 37 个字号」缩到「加载 7 个」。
        """
        for s in (FS_TIME_SMALL, FS_CLOCK, FS_TIMER_MIN, FS_TIMER):
            num(s)
        for s in (FS_DATE_SMALL, FS_DATE, FS_LABEL):
            cn(s)
        print("[step2] 字体就绪（中文 %s / 数字 %s）" % (FONT_CN_PATH, FONT_NUM_PATH))

    @staticmethod
    def _sample_date():
        return "0000年00月00日  星期零"

    def render(self, now, p, remain, total, rotate=True):
        img, d = self._img, self._draw
        d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_BG)     # 清屏（复用同一张画布）

        time_text = now.strftime("%H:%M:%S")
        date_text = "%04d年%02d月%02d日  %s" % (
            now.year, now.month, now.day, WEEKDAYS[now.weekday()])

        # ---- 先画倒计时（Z 序在底层）----
        # 日期要从 y=170 一路飞到 y=44，中途必然穿过倒计时所在的画面中央。
        # 把倒计时画在**下面**、时钟画在上面，重叠的那几帧里日期仍然清晰可读，
        # 看起来是「倒计时在背景里浮现」，而不是两行字糊在一起。
        pc = clamp((p - 0.30) / 0.70)      # 比时钟晚一点起飞，避免一开始就抢镜
        if pc > 0.001:
            c_cy = lerp(TIMER_CY + 18, TIMER_CY, pc)
            # 淡入用 alpha，**不要换颜色** —— 换颜色会让精灵缓存每帧都失效
            paste_text(img, TIMER_CX, c_cy, mmss(remain), FONT_NUM_PATH,
                       FS_TIMER_MIN, FS_TIMER, lerp(FS_TIMER_MIN, FS_TIMER, pc),
                       C_TIMER, alpha=pc)
            paste_text(img, TIMER_CX, TIMER_LABEL_CY, "剩余时间", FONT_CN_PATH,
                       FS_LABEL, FS_LABEL, FS_LABEL, C_DATE, alpha=pc)

            # 底部进度条：底槽同样从背景白浮现（矩形不走精灵缓存，无所谓）
            d.rectangle([0, BAR_Y, LAND_W, BAR_Y + BAR_H], fill=lerp_color(C_BG, C_TRACK, pc))
            w = int(LAND_W * done_ratio(remain, total))
            if w > 0:
                d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=C_TIMER)

        # ---- 时间：中心 + 字号一起插值（字号连续，不再量化到 2px 台阶）----
        paste_text(img, lerp(TIME0_CX, self.time1_cx, p), lerp(TIME0_CY, TIME1_CY, p),
                   time_text, FONT_NUM_PATH, FS_TIME_SMALL, FS_CLOCK,
                   lerp(FS_CLOCK, FS_TIME_SMALL, p), C_TIME)

        # ---- 日期：同上 ----
        paste_mixed(img, lerp(DATE0_CX, self.date1_cx, p), lerp(DATE0_CY, DATE1_CY, p),
                    date_text, FONT_CN_PATH, FONT_NUM_PATH,
                    FS_DATE_SMALL, FS_DATE, lerp(FS_DATE, FS_DATE_SMALL, p), C_DATE)

        return rotate_out(img) if rotate else img


# ------------------------------ 番茄钟状态 ------------------------------

IDLE, IN, FOCUS, OUT = "idle", "in", "focus", "out"


class FocusApp(object):
    """番茄钟状态机：idle → in（过渡）→ focus（倒计时）→ out（淡回）→ idle。

    对外只暴露 `snapshot()`，返回 (p, remain, total)，主循环据此画帧。
    归零时通过 on_finish 回调通知（回调在锁外触发）。
    """

    def __init__(self, focus_sec, trans_sec=TRANS_SEC, on_finish=None):
        self.focus_sec = float(focus_sec)
        self.trans_sec = float(trans_sec)
        self.on_finish = on_finish
        self._lock = threading.Lock()
        self._phase = IDLE
        self._t0 = 0.0
        self._end = 0.0
        self._frozen = 0.0          # OUT 阶段要显示 / 淡出的剩余秒数

    # --- 外部控制 ---
    def start(self):
        """触发一次番茄钟（等价于识别到「开始学习」）。已在专注中则忽略。"""
        with self._lock:
            if self._phase in (IN, FOCUS):
                return False
            now = time.monotonic()
            self._phase = IN
            self._t0 = now
            self._end = now + self.focus_sec      # 计时从过渡开始就起算
            self._frozen = self.focus_sec
            return True

    def stop(self):
        """立即淡回待机（B 键）。**幂等**：已在淡出 / 已待机时是空操作。

        ⚠️ 原来只判了 IDLE，于是在 OUT 阶段再按一次会重置 `_t0` ——
        等于把淡出动画从头再播一遍，连按几次就永远淡不回待机。
        同时冻结当前剩余秒数，避免倒计时突然跳到 00:00。
        """
        with self._lock:
            if self._phase in (IDLE, OUT):
                return False
            self._frozen = max(0.0, self._end - time.monotonic())
            self._phase = OUT
            self._t0 = time.monotonic()
            return True

    @property
    def phase(self):
        with self._lock:
            return self._phase

    # --- 主循环调用 ---
    def snapshot(self):
        """返回 (p, remain, total)。p=0 待机，p=1 专注。"""
        now = time.monotonic()
        finished = False
        p = 0.0
        with self._lock:
            ph, ts, fs = self._phase, self.trans_sec, self.focus_sec

            if ph == IN:
                r = (now - self._t0) / ts
                if r >= 1.0:
                    self._phase, ph = FOCUS, FOCUS
                else:
                    p = ease_in_out(r)
            if ph == FOCUS:
                if self._end - now <= 0.0:
                    self._phase, ph = OUT, OUT
                    self._t0 = now
                    self._frozen = 0.0
                    finished = True
                else:
                    p = 1.0
            if ph == OUT:
                r = (now - self._t0) / ts
                if r >= 1.0:
                    self._phase, ph = IDLE, IDLE
                    p = 0.0
                else:
                    p = 1.0 - ease_in_out(r)
            if ph == IDLE:
                p = 0.0

            remain = max(0.0, self._end - now) if ph in (IN, FOCUS) else self._frozen

        if finished and self.on_finish is not None:
            try:
                self.on_finish()
            except Exception as e:
                print("[番茄钟] on_finish 回调出错：%s" % e)
        return p, remain, fs


# ------------------------------ 语音 ------------------------------

# 两条语音链路（ASR 轮询线程 / TTS 播放线程）共用同一条 I2C 总线，
# 而 pinpong 的 I2C 对象**不是线程安全的** —— 两边同时发事务会互相打断，
# 所以所有 I2C 访问都串行化。
I2C_LOCK = threading.RLock()


class VoiceASR(object):
    """DFR0715（LD3320）语音识别。关键词在代码里用拼音下发，最后必须 start()。"""

    def __init__(self, words=WAKE_WORDS, mode=VOICE_MODE, mic=VOICE_INPUT):
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        # 初始化本身也是一串 I2C 事务，同样要和 TTS 互斥 —— 否则
        # 「ASR 还在下发关键词、TTS 已经在发合成帧」两边会互相打断。
        with I2C_LOCK:
            self._asr = DFRobot_ASR()              # I2C 0x4F
            self._asr.begin(getattr(self._asr, mode), getattr(self._asr, mic))
            for text, idx in words:
                self._asr.add_command(text, idx)
            self._asr.start()                      # 「设置完成 开始识别」

    def read(self):
        with I2C_LOCK:
            return self._asr.read()                # 无结果返回 -1


class Speaker(object):
    """DFR0760（V2）语音合成。后台线程 + 队列，绝不阻塞屏幕刷新与语音轮询。

    ⚠️ 为什么不用库自带的 `speak()`：它内部调 `wait()`，而 `wait()` 是
    **没有超时的死循环**（先等 0x41「开始合成」，再等 0x4F「空闲」）。
    模块一旦不应答（喇叭没接、供电不足、模式开关不对、被别的线程打断），
    工作线程就**永久卡死**，表现是「日志里有『合成：xxx』却永远没声音、
    也不报任何错」—— 极难排查。所以这里自己发帧 + 全程带超时轮询，
    并把每一步都打进日志。
    """

    def __init__(self, version=TTS_VERSION, volume=TTS_VOLUME):
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        self._tts = DFRobot_SpeechSynthesis_I2C()  # I2C 0x40
        with I2C_LOCK:
            self._tts.begin(self._tts.V1 if version == "V1" else self._tts.V2)
            self._tts.set_voice(volume)            # begin() 会把音量设成 1，必须补一刀
        print("[语音] 合成音量已设为 %d" % volume)
        self._q = queue.Queue()
        t = threading.Thread(target=self._worker)
        t.daemon = True
        t.start()

    def say(self, text):
        print("[语音] 待播：%s" % text)
        self._q.put(text)

    # --- 自己实现的合成（全程带超时）---

    def _ack(self):
        """读模块回的一个字节；失败返回 None。"""
        try:
            d = self._tts.read_ack(1)
            return d[0] if d else None
        except Exception as e:
            print("[语音] 读模块状态失败：%s" % e)
            return None

    def _speak_once(self, text):
        """发一帧并等播完。返回耗时（秒）；没起来 / 超时返回 None。"""
        data = list(text.encode("gb2312"))
        n = len(data) + 2
        self._tts.write_cmd([0xFD, (n >> 8) & 0xFF, n & 0xFF, 0x01, 0x00] + data)

        t0 = time.monotonic()
        # ① 等「开始合成」0x41
        started = False
        while time.monotonic() - t0 < ACK_START_TIMEOUT:
            if self._ack() == 0x41:
                started = True
                break
            time.sleep(0.02)
        if not started:
            return None

        # ⚠️⚠️ 这一步绝对不能省！库的 wait() 在 0x41 之后固定 sleep(0.1)。
        #      省掉它、立刻把状态查询帧发出去，会**打断刚起步的合成** ——
        #      模块马上回到空闲，表现为「耗时 0.00s」而且完全没声音。
        #      2026-10-14 用对照实验坐实：guard=False 首轮 0.00s，guard=True 1.63s。
        time.sleep(0.1)

        # ② 轮询状态，等「空闲」0x4F = 播完
        while time.monotonic() - t0 < ACK_DONE_TIMEOUT:
            self._tts.write_cmd([0xFD, 0x00, 0x01, 0x21])
            if self._ack() == 0x4F:
                return time.monotonic() - t0
            time.sleep(0.02)
        return None

    def _speak(self, text):
        """带一次重试：耗时短得可疑（< 0.3s）说明这次压根没合成出来，再试一遍。"""
        for attempt in (1, 2):
            dt = self._speak_once(text)
            if dt is not None and dt >= 0.3:
                return dt
            print("[语音] 第 %d 次没合成出来（%.2fs），重试"
                  % (attempt, dt if dt is not None else -1.0))
            time.sleep(0.2)
        return None

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                with I2C_LOCK:                     # 与 ASR 轮询互斥
                    dt = self._speak(text)
            except Exception as e:
                print("[语音] 合成失败：%s" % e)
                continue
            if dt is None:
                print("[语音] ⚠️ 合成超时：模块没应答"
                      "（检查喇叭接线 / 供电 / 板上 I2C-UART 模式开关）")
            else:
                print("[语音] 播完（%.1fs）：%s" % (dt, text))


class VoiceTrigger(threading.Thread):
    """后台轮询语音模块。识别到关键词就回调 on_wake（边沿触发，避免重复响应）。"""

    def __init__(self, asr, on_wake, ids=tuple(i for _, i in WAKE_WORDS),
                 poll_sec=VOICE_POLL_SEC, debug=VOICE_DEBUG,
                 heartbeat_sec=VOICE_HEARTBEAT_SEC):
        super(VoiceTrigger, self).__init__()
        self.daemon = True
        self.asr = asr
        self.on_wake = on_wake
        self.ids = tuple(ids)
        self.poll_sec = poll_sec
        self.debug = debug
        self.heartbeat_sec = heartbeat_sec
        self._stop = threading.Event()

    def run(self):
        prev = None
        n_read = n_hit = n_err = 0
        next_beat = time.time() + (self.heartbeat_sec or 0)
        while not self._stop.is_set():
            try:
                cid = self.asr.read()
            except Exception as e:
                n_err += 1
                if self.debug:
                    print("[语音] 读取失败：%s" % e)
                time.sleep(1.0)
                continue

            n_read += 1
            if cid != -1:
                n_hit += 1
                if self.debug and cid != prev:
                    print("[语音] 识别到 ID = %s" % cid)

            if cid != -1 and cid in self.ids and cid != prev:
                try:
                    self.on_wake(cid)
                except Exception as e:
                    print("[语音] 回调出错：%s" % e)

            prev = cid

            # 一直没命中就定期报一次「我还活着」——用来区分
            # 「轮询线程死了」和「模块在跑、但关键词没被识别」这两种情况
            if self.heartbeat_sec and n_hit == 0 and time.time() >= next_beat:
                print("[语音] 轮询心跳：已读 %d 次 / 命中 0 / 错误 %d"
                      "（能读 = 模块在线；读得到却没命中 = 关键词没被识别）"
                      % (n_read, n_err))
                next_beat = time.time() + self.heartbeat_sec

            self._stop.wait(self.poll_sec)

    def stop(self):
        self._stop.set()


# ------------------------------ 启动 ------------------------------

def create_gui():
    """unihiker 0.0.27 首次初始化偶发 RuntimeError（Tk 线程竞态），重试一次。"""
    from unihiker import GUI
    try:
        return GUI()
    except RuntimeError:
        time.sleep(0.5)
        return GUI()


def init_voice(focus_min):
    """返回 (asr, speaker)。任一模块缺失都返回 None，程序退化为纯屏幕版继续跑。"""
    try:
        from pinpong.board import Board
        Board().begin()
    except Exception as e:
        print("[初始化] pinpong 失败：%s" % e)
        return None, None

    asr = None
    try:
        asr = VoiceASR()
        print("[语音] 识别模块就绪（I2C 0x4F），关键词：%s" % " / ".join(w for w, _ in WAKE_WORDS))
    except BaseException as e:
        print("[语音] 未检测到识别模块（%s）" % e)

    speaker = None
    try:
        speaker = Speaker()
        print("[语音] 合成模块就绪（I2C 0x40，%s）" % TTS_VERSION)
    except BaseException as e:      # begin(V2) 失败会 exit() 抛 SystemExit，须一并接住
        print("[语音] 未检测到合成模块（%s）" % e)

    return asr, speaker


def run(args):
    gui = create_gui()
    screen = Screen()
    speech_box = {}

    def on_finish():
        if speech_box.get("speaker") is not None:
            speech_box["speaker"].say(FINISH_REPLY)
        print("[番茄钟] 时间到")

    app = FocusApp(args.focus_min * 60, on_finish=on_finish)
    total = app.focus_sec

    clock = gui.draw_image(x=0, y=0, image=screen.render(datetime.now(), 0.0, total, total))
    print("[step2] 已启动：专注 %d 分钟，过渡 %.2fs，旋转 %d°" % (
        args.focus_min, TRANS_SEC, ROTATE))
    print("[step2] 当前时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    asr = speaker = None
    if not args.no_voice:
        asr, speaker = init_voice(args.focus_min)
    speech_box["speaker"] = speaker

    def trigger(src):
        if app.start():
            print("[番茄钟] 触发（%s）→ 专注 %d 分钟" % (src, args.focus_min))
            if speech_box.get("speaker") is not None:
                speech_box["speaker"].say(START_REPLY % args.focus_min)
        else:
            print("[番茄钟] 已在专注中，忽略（%s）" % src)

    trigger_thread = None
    if asr is not None:
        trigger_thread = VoiceTrigger(asr, lambda cid: trigger("语音"))
        trigger_thread.start()

    # ---- 按键：A = 手动触发（演示/调试），B = 结束 ----
    try:
        gui.on_a_click(lambda *a: trigger("A 键"))
        gui.on_b_click(lambda *a: (app.stop(), print("[番茄钟] 手动结束")))
    except Exception as e:
        print("[按键] 注册失败（不影响语音）：%s" % e)

    if args.demo:
        def _demo():
            time.sleep(DEMO_DELAY_SEC)
            trigger("--demo 自动")
        t = threading.Thread(target=_demo)
        t.daemon = True
        t.start()

    # ---- 主循环 ----
    last_key = None
    trans_t0 = None
    trans_frames = 0
    try:
        while True:
            now = datetime.now()

            # 调试钩子：远程 `touch /tmp/step2_trigger` 即可触发一次，不用重启进程
            # （板上 I2C 很脆，重启进程可能让语音模块失效，见文件头注释）
            if os.path.exists(TRIGGER_FILE):
                try:
                    os.remove(TRIGGER_FILE)
                except Exception:
                    pass
                trigger("触发文件")

            p, remain, _ = app.snapshot()
            in_trans = 0.0 < p < 1.0

            if in_trans:
                # 过渡期：每帧都重绘，追求最高帧率
                if trans_t0 is None:
                    trans_t0, trans_frames = time.monotonic(), 0
                clock.config(image=screen.render(now, p, remain, total))
                trans_frames += 1
                last_key = None
                continue

            if trans_t0 is not None:       # 过渡刚结束，报告实测帧率（含贴屏开销）
                dt = time.monotonic() - trans_t0
                print("[step2] 过渡实测：%d 帧 / %.2fs = %.1f fps"
                      % (trans_frames, dt, (trans_frames / dt) if dt > 0 else 0.0))
                trans_t0 = None

            # 静止期：只在内容真的变化时重绘
            key = ("clock", now.strftime("%H:%M:%S")) if p == 0.0 else ("focus", int(remain))
            if key != last_key:
                clock.config(image=screen.render(now, p, remain, total))
                last_key = key
            time.sleep(ANIM_IDLE_SLEEP)
    except KeyboardInterrupt:
        print("\n[step2] 已退出")
    finally:
        if trigger_thread is not None:
            trigger_thread.stop()


# ------------------------------ 电脑预览 ------------------------------

def preview(out_path, focus_min=25):
    """不连板子，把过渡的关键帧拼成一张对比图（2 行 × 4 列），方便看排版。"""
    screen = Screen()
    total = focus_min * 60.0
    now = datetime.now().replace(second=45)
    ps = [0.0, 0.15, 0.30, 0.45, 0.60, 0.75, 0.90, 1.0]

    cols, rows = 4, 2
    pad, label_h = 8, 20
    sheet = Image.new("RGB", (cols * (LAND_W + pad) + pad,
                              rows * (LAND_H + pad + label_h) + pad), "#EEEEEE")
    sd = ImageDraw.Draw(sheet)
    f_lab = load_font(FONT_NUM_PATH, 14)

    for i, p in enumerate(ps):
        remain = total - 1.0                # 过渡刚发生，倒计时还停在 24:59
        frame = screen.render(now, p, remain, total, rotate=False)
        x = pad + (i % cols) * (LAND_W + pad)
        y = pad + (i // cols) * (LAND_H + pad + label_h)
        sheet.paste(frame, (x, y + label_h))
        sd.rectangle([x, y + label_h, x + LAND_W - 1, y + label_h + LAND_H - 1],
                     outline="#BBBBBB")
        sd.text((x, y + 2), "p = %.2f" % p, font=f_lab, fill="#333333")

    sheet.save(out_path)
    print("[step2] 过渡关键帧已保存：%s（%d×%d）" % (out_path, sheet.width, sheet.height))
    print("[step2] 使用字体：中文=%s / 数字=%s" % (FONT_CN_PATH, FONT_NUM_PATH))


# ------------------------------ 入口 ------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="行空板 M10 智能番茄时钟 · 第 2 段：语音开番茄钟 + 丝滑布局过渡")
    ap.add_argument("--focus-min", type=float, default=25.0, help="专注时长（分钟），默认 25")
    ap.add_argument("--no-voice", action="store_true", help="不初始化语音模块（纯屏幕）")
    ap.add_argument("--demo", action="store_true",
                    help="启动 %.0f 秒后自动触发一次（不用说话也能看效果）" % DEMO_DELAY_SEC)
    ap.add_argument("--preview", metavar="PNG", help="不连板子，把过渡关键帧拼成一张图")
    args = ap.parse_args(argv)

    if args.preview:
        preview(args.preview, args.focus_min)
    else:
        run(args)


if __name__ == "__main__":
    main()
