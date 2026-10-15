# -*- coding: utf-8 -*-
"""
行空板 M10 · 智能番茄时钟 —— 第 3 段：离座检测 + 分级提醒 + 超时暂停番茄钟

★ 最终版（LAST）—— 本项目**当前实际部署、上板运行**的就是这一个文件。
  命名约定：最终版文件名以 `_last` 结尾，一眼认出哪一版要用；中间版本不要用这个后缀。

    二哈识图（人脸识别）持续判断「在座 / 离座」：
        连续 60 秒没人脸  →  判离座，**番茄钟立即暂停**（回来自动继续）
        60s ~ 3min        →  L1 轻提醒（蓝）
        3min ~ 5min       →  L2 中提醒（琥珀）
        > 5min            →  L3 强提醒（品红，近全屏反色）
        人脸重新出现       →  绿色「欢迎回来」帧 1.5s，番茄钟恢复

完整说明（设计对照 / 分级矩阵 / 硬件与踩坑 / 用法）见：
    docs/m10_step3_away_last.md

运行环境：行空板 M10 板载 Python 3.7.3 + Pillow 9.0.1 + pinpong 0.6.x
"""

import os
import sys
import math
import time
import queue
import argparse
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ===================== 设计令牌（全部取自 DESIGN.md）=====================

C_BG = "#FFFFFF"          # 背景
C_ON = "#FFFFFF"          # 反白文字 / 叠在语义色带上的图形
C_TIME = "#00838F"        # 时间 · 专注青
C_DATE = "#546E7A"        # 次级文字
C_TIMER = "#00838F"       # 倒计时 · 专注青
C_TRACK = "#CFD8DC"       # 进度条底槽

C_FOCUS = "#00838F"       # 专注 · 青
C_L1 = "#1565C0"          # 轻提醒 · 蓝
C_L2 = "#A85E00"          # 中提醒 · 琥珀
C_L2T = "#9A5B00"         # 中提醒 · 白底上的文字（深一档）
C_L3 = "#C2185B"          # 强提醒 · 品红
C_RECOVER = "#2E7D32"     # 回座 · 绿

TIER_COLOR = {None: C_FOCUS, "l1": C_L1, "l2": C_L2, "l3": C_L3}

# 字号（DESIGN.md §3.2）
FS_CLOCK = 62             # 时钟主显
FS_TIMER = 54             # 倒计时主显
FS_AWAY_NUM = 28          # 离座数字（L2）
FS_AWAY_BIG = 62          # 离座数字（L3）
FS_ALERT = 24             # 一级告警
FS_BANNER = 18            # 提示条正文
FS_TIME_SMALL = 18        # 专注态左上角小时间
FS_DATE = 18              # 待机日期
FS_DATE_SMALL = 14        # 专注态小日期
FS_LABEL = 14             # 标签
FS_MICRO = 12             # 微字
FS_TIMER_MIN = 34         # 倒计时淡入起始字号

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

# 相机监督信号（右上常驻，锚点即机身中心）
CAM_CX, CAM_CY = 294, 16

# 提示条 / 人形 / 离座数字的位置（严格照 prototype_seat_detection.html）
BANNER_H = 28
L1_BANNER_CY = 120
L2_BANNER_CY = 46
L2_PERSON_CY = 104
L2_AWAY_CY = 150
L2_LABEL_CY = 176
L3_TOP_TEXT_CY = 20
L3_PERSON_CY = 80
L3_AWAY_CY = 134
L3_PAUSE_CY = 185

BAR_Y, BAR_H = 236, 4       # 底部进度条

TRANS_SEC = 0.9             # 布局过渡时长（秒）
ANIM_IDLE_SLEEP = 0.05      # 非动画期的轮询间隔
ANIM_SLEEP = 0.01           # 动画期的轮询间隔

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# ---- 离座检测参数 ----
AWAY_SEC = 60.0             # 连续多久没人脸 → 判离座（同时**暂停番茄钟**）
L2_SEC = 180.0              # 离座超过 3 分钟 → 中提醒
L3_SEC = 300.0              # 离座超过 5 分钟 → 强提醒
RECOVER_SEC = 1.5           # 回座绿帧时长
PRESENCE_POLL_SEC = 0.1     # 二哈轮询间隔
HUSKY_WARMUP_SEC = 5.5      # 切算法后官方建议等约 5s
HUSKY_MAX_ERRORS = 20       # 连续读失败这么多次就放弃（避免刷屏 / 拖慢主循环）
HUSKY_HEARTBEAT_SEC = 30   # 每这么久报一次「相机到底看没看到人」

# 二哈 I2C 协议包头：`55 AA 11 <datalen> <cmd> <data...> <checksum>`
#   对「请求 blocks」的响应，data[0] 就是**检测到几个框**（人脸识别里 = 几张脸）。
# ⚠️ pinpong 驱动是**逐字节** `readfrom_mem(0x32, x, 1)` 读的 —— 一个包要十几次 I2C 事务，
#    在行空板这种软件模拟 I2C 上又慢又容易错位。所以下面额外用**一次性读 6 字节**做对照，
#    两者读的是**同一个响应**（readfrom_mem 是随机访问，不消费），可直接比较谁对。
HL_ADDR = 0x32
HL_HEAD_LEN = 6
HL_PACKET_LEN = 16         # datalen=0x0A 时整包长度 = 5 + 10 + 1。一次读全，便于核对协议
# 「请求 blocks」命令帧：`55 AA 11 <len=0> <cmd=0x21> <checksum=0x31>`
# （校验和 = 前 5 字节之和 % 256 = 0x131 % 256 = 0x31，已按驱动 _calculateChecksum 核对）
HL_REQ_BLOCKS = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]

# 呼吸 / 脉冲周期（DESIGN.md §4.6：强档 1s 已是 ≤1Hz 上限）
PULSE_PERIOD = {None: 2.0, "l1": 4.0, "l2": 1.5, "l3": 1.0}
PULSE_MIN, PULSE_MAX = 0.6, 1.0

# ---- 语音 ----
# ⚠️ 关键词必须是**空格分隔的拼音音节**（库只是逐字符发 ASCII，真正解析音节的是固件）。
# ⚠️ 音节之间要**发音差异足够大**：`shi/xue/xi` 三个都是「舌面/舌尖 + i」，偏难认；
#    认不动时把动词整体换掉（如 `zan ting` → `ting yi xia`），不要只微调声母。
CMD_START, CMD_PAUSE, CMD_RESUME, CMD_STOP = 1, 2, 3, 4
WAKE_WORDS = (
    ("kai shi xue xi", CMD_START),     # 开始学习 → 开番茄钟
    ("zan ting xue xi", CMD_PAUSE),    # 暂停学习 → 冻结倒计时
    ("ji xu xue xi", CMD_RESUME),      # 继续学习 → 恢复倒计时
    ("ting zhi xue xi", CMD_STOP),     # 停止学习 → 结束番茄钟回待机
)
VOICE_MODE = "LOOP"
VOICE_INPUT = "MIC"
VOICE_POLL_SEC = 0.05
VOICE_DEBUG = True
VOICE_HEARTBEAT_SEC = 30
TTS_VERSION = "V2"
TTS_VOLUME = 5
ACK_START_TIMEOUT = 3.0
ACK_DONE_TIMEOUT = 30.0
START_REPLY = "好的，开始专注%d分钟"
FINISH_REPLY = "专注结束，休息一下吧"
PAUSE_REPLY = "已暂停，休息一下"
RESUME_REPLY = "继续专注"
STOP_REPLY = "已结束本次专注"
VOICE_L1 = "同学，你还在吗"
VOICE_L2 = "请回到座位"
VOICE_L3 = "计时已暂停，请回到座位"
VOICE_RECOVER = "欢迎回来"
# 回座但计时**因手动暂停未恢复**时用这句 —— 不能沿用「欢迎回来」，
# 否则用户会以为计时已经跑起来了（见 AwaySupervisor.update 的回座分支）。
VOICE_RECOVER_MANUAL = "欢迎回来，计时还暂停着，说继续学习就恢复"
L2_REPEAT_SEC = 8.0                    # 中提醒：8 秒后重复一次
L3_INTERVALS = (15, 20, 30, 45, 60)    # 强提醒：间隔递增（秒），到顶后保持
SHORT_AWAY_MIN_SEC = 2.0               # 短于这个的「离座」当作噪声，不计入统计

# ---- 调试 ----
DEMO_DELAY_SEC = 8.0
TRIGGER_FILE = "/tmp/step3_trigger"       # touch 一下 = 识别到「开始学习」
AWAY_FILE = "/tmp/step3_absent"           # touch 一下 = 强制判「离座」（二哈在线时也生效）
PRESENT_FILE = "/tmp/step3_present"       # touch 一下 = 强制判「在场」（用来验证回座绿帧）
TTS_MUTE_FILE = "/tmp/step3_no_tts"       # touch 一下 = 暂时静音（只停合成，识别与番茄钟照常）
PAUSE_FILE = "/tmp/step3_pause"           # touch 一下 = 等价识别到「暂停学习」
RESUME_FILE = "/tmp/step3_resume"         # touch 一下 = 等价识别到「继续学习」
STOP_FILE = "/tmp/step3_stop"             # touch 一下 = 等价识别到「停止学习」

# ---- 字体栈（按顺序回退；禁止 uming.ttc）----
FONT_CN = [
    "HYQiHei_50S.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
FONT_NUM = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
] + FONT_CN

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

# 行空板 M10 + Pillow 9.0.1 实测：draw.text 渲染一行 62px ≈ 21ms，paste 小图 ≈ 1ms。
# 所以文字一律先渲染成 RGBA 小图**缓存**，之后每帧只 paste。
#
# ⚠️ 缓存 key 里**不能放逐帧变化的量**（颜色 / 字号）—— 那会每帧都是新 key，
#    等于没缓存，还会把缓存撑爆。做法：
#      * 精灵一律用**最终颜色**渲染；淡入改用「贴图时按 alpha 合成」；
#      * 只缓存**端点**字号，中间靠 resize；
#      * 淘汰只丢最旧一批，**绝不用 clear()**。

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

    必须传查表、不要传 lambda —— 传 lambda 会变成「每像素一次 Python 调用」。
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
            top = min(top, t - asc)
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


def _camera_sprite(body, dot, factor):
    """相机监督信号：机身 14×12 r2 + 取景器 4×2 + 镜头镂空 r2.5 + 状态点 r1.5。

    镜头用**透明**表示镂空 —— 这样贴到白底上显白、贴到品红底上显品红，
    两种底色都不用改图。
    """
    key = ("cam", body, dot, round(factor, 2))

    def build():
        im = Image.new("RGBA", (14, 14), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rectangle([5, 0, 8, 1], fill=body)            # 取景器凸块
        d.rounded_rectangle([0, 2, 13, 13], radius=2, fill=body)
        d.ellipse([3, 5, 8, 10], fill=(0, 0, 0, 0))     # 镜头（镂空）
        d.ellipse([10, 10, 13, 13], fill=dot)           # 状态点
        if abs(factor - 1.0) >= 0.01:
            im = im.resize((max(1, int(round(14 * factor))),
                            max(1, int(round(14 * factor)))), _BILINEAR)
        return im

    return _sprite(key, build)


def _person_sprite(color):
    """人形符号：头 r6 + 身体 20×14 r4（DESIGN.md §4.4）。"""
    key = ("person", color)

    def build():
        im = Image.new("RGBA", (20, 28), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([4, 0, 16, 12], fill=color)
        d.rounded_rectangle([0, 14, 19, 27], radius=4, fill=color)
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


def paste_at(img, x, cy, sp, alpha=1.0):
    """左对齐贴图：左边缘落在 x，垂直居中在 cy。"""
    box = (int(round(x)), int(round(cy - sp.height / 2.0)))
    if alpha >= 0.999:
        img.paste(sp, box, sp)
    else:
        img.paste(sp, box, sp.getchannel("A").point(_alpha_lut(alpha)))


def _ref_sizes(size_lo, size_hi, n=4):
    """在 [lo, hi] 上等距取 n 档参考字号（含两端）。"""
    if size_hi <= size_lo:
        return (size_lo,)
    step = (size_hi - size_lo) / float(n)
    return tuple(int(round(size_lo + step * i)) for i in range(n + 1))


def _ref_size(size_lo, size_hi, target):
    """挑一个**参考字号**来渲染，之后靠 `_scale` 缩放到 target。

    ⚠️ 原来是硬二分（`target >= (lo+hi)/2` 取 hi 否则 lo）：target 跨过中点的
    那一帧，参考字号会从 lo 突然跳到 hi，缩放比同步突变 —— 实测过渡里会
    闪一下模糊。改成按区间**等距取若干档**、选离 target 最近的一档，
    缩放比最大不超过 1/(2N)，跳变幅度随之降到原来的 1/N。
    """
    if size_hi <= size_lo:
        return size_lo
    refs = _ref_sizes(size_lo, size_hi)
    best = refs[0]
    for r in refs[1:]:
        if abs(r - target) < abs(best - target):
            best = r
    return best


def _scale(sp, ref, target):
    if target == ref:
        return sp
    r = target / float(ref)
    return sp.resize((max(1, int(round(sp.width * r))),
                      max(1, int(round(sp.height * r)))), _BILINEAR)


def paste_text(img, cx, cy, text, path, size_lo, size_hi, target, fill, alpha=1.0):
    """按目标字号贴一行单字体文字：只缓存两端字号，中间靠缩放。"""
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
    a, b = hex_rgb(h1), hex_rgb(h2)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def mmss(sec):
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def done_ratio(remain, total):
    return 0.0 if total <= 0 else clamp(1.0 - remain / float(total))


def breathe(mono, period):
    """呼吸 / 脉冲系数：0.6 ↔ 1.0 正弦往复（DESIGN.md §4.6）。"""
    if period <= 0:
        return PULSE_MAX
    c = 0.5 + 0.5 * math.cos(2.0 * math.pi * (mono % period) / period)
    return PULSE_MIN + (PULSE_MAX - PULSE_MIN) * c


# --------------------------- 整屏旋转（快路径）---------------------------

try:                                   # Pillow >= 9.1
    from PIL.Image import Transpose
    _ROT90 = Transpose.ROTATE_90
    _ROT270 = Transpose.ROTATE_270
except Exception:                      # Pillow < 9.1（板载 9.0.1）
    _ROT90 = Image.ROTATE_90
    _ROT270 = Image.ROTATE_270


def rotate_out(img, angle=ROTATE):
    """横屏逻辑画布 → 物理竖屏（transpose 是纯内存搬运，比 rotate() 快得多）。"""
    return img.transpose(_ROT270 if angle == 270 else _ROT90)


# ------------------------------ 画面模型 ------------------------------

class View(object):
    """一帧要画什么。由 AwaySupervisor 每轮算好交给 Screen。"""

    __slots__ = ("p", "remain", "total", "tier", "away_sec", "pulse", "recover", "paused")

    def __init__(self, p=0.0, remain=0.0, total=0.0, tier=None,
                 away_sec=0.0, pulse=1.0, recover=False, paused=False):
        self.p = p
        self.remain = remain
        self.total = total
        self.tier = tier
        self.away_sec = away_sec
        self.pulse = pulse
        self.recover = recover
        self.paused = paused       # 用户手动暂停（「暂停学习」）→ 倒计时下方显示暂停指示


# ------------------------------ 渲染器 ------------------------------

class Screen(object):
    """把 View 画成一张图。复用同一张画布，每帧只清屏重绘。"""

    def __init__(self, l3_sec=L3_SEC):
        # ⚠️ `l3_sec` 走构造参数而**不是模块级全局** —— 早先用 `global _L3_MIN`，
        #    preview() 与 run() 会互相污染（连续调用 / 单测时拿到上一次的值）。
        self.l3_min = max(1, int(l3_sec // 60))

        self._img = Image.new("RGB", (LAND_W, LAND_H), C_BG)
        self._draw = ImageDraw.Draw(self._img)
        # 专注态是「左对齐」，但绘制统一用居中 —— 先把最终居中的 x 算出来
        w_time = text_width("00:00:00", num(FS_TIME_SMALL))
        self.time1_cx = PAD + w_time / 2.0
        w_date = measure_mixed(self._sample_date(), cn(FS_DATE_SMALL), num(FS_DATE_SMALL))
        self.date1_cx = PAD + w_date / 2.0
        self._preload()

    def _preload(self):
        for s in (FS_TIME_SMALL, FS_CLOCK, FS_TIMER_MIN, FS_TIMER, FS_AWAY_BIG, FS_AWAY_NUM):
            num(s)
        for s in (FS_DATE_SMALL, FS_DATE, FS_LABEL, FS_MICRO, FS_BANNER, FS_ALERT):
            cn(s)
        print("[step3] 字体就绪（中文 %s / 数字 %s）" % (FONT_CN_PATH, FONT_NUM_PATH))

    @staticmethod
    def _sample_date():
        return "0000年00月00日  星期零"

    # --- 对外 ---

    def render(self, now, v, rotate=True):
        if v.recover:
            self._recover()
        elif v.tier:
            self._away(v)
        else:
            self._normal(now, v)
        return rotate_out(self._img) if rotate else self._img

    # --- 待机 / 专注（含过渡）---

    def _normal(self, now, v):
        img, d, p = self._img, self._draw, v.p
        d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_BG)

        time_text = now.strftime("%H:%M:%S")
        date_text = "%04d年%02d月%02d日  %s" % (
            now.year, now.month, now.day, WEEKDAYS[now.weekday()])

        # ---- 先画倒计时（Z 序在底层）----
        # 日期要从 y=170 飞到 y=44，中途必然穿过画面中央；把倒计时画在下面，
        # 重叠的那几帧日期仍然清晰可读，看起来是「倒计时在背景里浮现」。
        pc = clamp((p - 0.30) / 0.70)
        if pc > 0.001:
            c_cy = lerp(TIMER_CY + 18, TIMER_CY, pc)
            paste_text(img, TIMER_CX, c_cy, mmss(v.remain), FONT_NUM_PATH,
                       FS_TIMER_MIN, FS_TIMER, lerp(FS_TIMER_MIN, FS_TIMER, pc),
                       C_TIMER, alpha=pc)
            if v.paused:
                # 手动暂停：倒计时下方的标签换成「已暂停」双竖条 + 文字。
                # 不复用离座那套彩色底纹 —— 一屏一事，暂停只是状态不是告警。
                bar_h, bar_w, gap = 11, 4, 6
                label_sp = text_sprite("已暂停", FONT_CN_PATH, FS_LABEL, C_TIMER)
                total_w = bar_w * 2 + gap + 6 + label_sp.width
                bx = int(TIMER_CX - total_w / 2.0)
                by = TIMER_LABEL_CY - bar_h // 2
                for k in range(2):
                    d.rectangle([bx + k * (bar_w + gap), by,
                                 bx + k * (bar_w + gap) + bar_w - 1, by + bar_h - 1],
                                fill=C_TIMER)
                paste_at(img, bx + bar_w * 2 + gap + 6, TIMER_LABEL_CY, label_sp, pc)
            else:
                paste_text(img, TIMER_CX, TIMER_LABEL_CY, "剩余时间", FONT_CN_PATH,
                           FS_LABEL, FS_LABEL, FS_LABEL, C_DATE, alpha=pc)

            d.rectangle([0, BAR_Y, LAND_W, BAR_Y + BAR_H], fill=lerp_color(C_BG, C_TRACK, pc))
            w = int(LAND_W * done_ratio(v.remain, v.total))
            if w > 0:
                d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=C_TIMER)

        # ---- 时间 / 日期：中心 + 字号一起插值 ----
        paste_text(img, lerp(TIME0_CX, self.time1_cx, p), lerp(TIME0_CY, TIME1_CY, p),
                   time_text, FONT_NUM_PATH, FS_TIME_SMALL, FS_CLOCK,
                   lerp(FS_CLOCK, FS_TIME_SMALL, p), C_TIME)

        paste_mixed(img, lerp(DATE0_CX, self.date1_cx, p), lerp(DATE0_CY, DATE1_CY, p),
                    date_text, FONT_CN_PATH, FONT_NUM_PATH,
                    FS_DATE_SMALL, FS_DATE, lerp(FS_DATE, FS_DATE_SMALL, p), C_DATE)

        # ---- 相机监督信号：待机时灰、专注时青（用 alpha 交叉淡入，不产生新 key）----
        self._camera(img, C_DATE, C_ON, 1.0, 1.0)
        if p > 0.001:
            self._camera(img, C_FOCUS, C_ON, 1.0, p)

    def _camera(self, img, body, dot, factor, alpha):
        paste_center(img, CAM_CX, CAM_CY, _camera_sprite(body, dot, factor), alpha)

    # --- 离座三档 ---

    def _away(self, v):
        img, d = self._img, self._draw
        tier, a = v.tier, v.pulse

        if tier == "l3":
            # 近全屏反色：背景品红，前景全部反白
            d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_L3)
            paste_at(img, SAFE_MARGIN, L3_TOP_TEXT_CY,
                     text_sprite("离座已超 %d 分钟" % self.l3_min, FONT_CN_PATH,
                                 FS_LABEL, C_ON), a)
            self._camera(img, C_ON, C_L3, 1.4, a)
            paste_center(img, 160, L3_PERSON_CY, _person_sprite(C_ON), a)
            # 离座大数字必须一起呼吸（原来漏传 a → 人形在闪、数字不动）
            paste_center(img, 160, L3_AWAY_CY,
                         text_sprite(mmss(v.away_sec), FONT_NUM_PATH, FS_AWAY_BIG, C_ON), a)
            # 「计时已暂停」双竖条 + 文字
            bar = lerp_color(C_L3, C_ON, a)
            d.rectangle([105, 176, 110, 193], fill=bar)
            d.rectangle([117, 176, 122, 193], fill=bar)
            paste_at(img, 135, L3_PAUSE_CY,
                     text_sprite("计时已暂停", FONT_CN_PATH, FS_BANNER, C_ON), a)
            # 进度冻结：底槽与背景融为一体，只剩白段
            w = int(LAND_W * done_ratio(v.remain, v.total))
            if w > 0:
                d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=C_ON)
            return

        # L1 / L2 都是白底
        col = TIER_COLOR[tier]
        d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_BG)
        self._camera(img, col, C_ON, 1.0, a)

        if tier == "l1":
            d.rectangle([0, L1_BANNER_CY - BANNER_H // 2, LAND_W - 1,
                         L1_BANNER_CY + BANNER_H // 2], fill=col)
            paste_center(img, 160, L1_BANNER_CY,
                         text_sprite("同学，你还在吗？", FONT_CN_PATH, FS_BANNER, C_ON))
        else:
            d.rectangle([0, L2_BANNER_CY - BANNER_H // 2, LAND_W - 1,
                         L2_BANNER_CY + BANNER_H // 2], fill=col)
            paste_center(img, 160, L2_BANNER_CY,
                         text_sprite("请回到座位", FONT_CN_PATH, FS_BANNER, C_ON))
            paste_center(img, 160, L2_PERSON_CY, _person_sprite(col), a)
            paste_center(img, 160, L2_AWAY_CY,
                         text_sprite(mmss(v.away_sec), FONT_NUM_PATH, FS_AWAY_NUM, C_L2T), a)
            paste_center(img, 160, L2_LABEL_CY,
                         text_sprite("离座时长", FONT_CN_PATH, FS_MICRO, C_DATE), a)

        # 进度冻结 + 整条变该档语义色
        d.rectangle([0, BAR_Y, LAND_W, BAR_Y + BAR_H], fill=C_TRACK)
        w = int(LAND_W * done_ratio(v.remain, v.total))
        if w > 0:
            d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=col)

    # --- 回座恢复 ---

    def _recover(self):
        img, d = self._img, self._draw
        d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_RECOVER)
        # 绿色对勾：两条粗线（线宽 8px ≥ 4px）+ 圆头
        d.line([(132, 122), (152, 144)], fill=C_ON, width=8)
        d.line([(152, 144), (192, 92)], fill=C_ON, width=8)
        for (px, py) in ((132, 122), (152, 144), (192, 92)):
            d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=C_ON)
        paste_center(img, 160, 180,
                     text_sprite("欢迎回来", FONT_CN_PATH, FS_ALERT, C_ON))


# ------------------------------ 番茄钟状态 ------------------------------

IDLE, IN, FOCUS, OUT = "idle", "in", "focus", "out"


class FocusApp(object):
    """番茄钟状态机：idle → in（过渡）→ focus（倒计时）→ out（淡回）→ idle。

    第 3 段新增 `pause()` / `resume()`：离座时**冻结**倒计时（`_end` 不再推进），
    回座时把冻结的剩余秒数重新挂到当前时刻，实现「离座不计时、回来自动继续」。
    """

    def __init__(self, focus_sec, trans_sec=TRANS_SEC, on_finish=None):
        self.focus_sec = float(focus_sec)
        self.trans_sec = float(trans_sec)
        self.on_finish = on_finish
        self._lock = threading.Lock()
        self._phase = IDLE
        self._t0 = 0.0
        self._end = 0.0
        self._frozen = 0.0
        self._paused = False
        self._paused_remain = 0.0
        # 用户手动暂停（「暂停学习」）与离座自动暂停**必须分开记** ——
        # 否则回座时 AwaySupervisor 的 resume() 会把用户的手动暂停一并解开。
        self._manual_pause = False

    # --- 外部控制 ---
    def start(self):
        """触发一次番茄钟（等价于识别到「开始学习」）。已在专注中则忽略。"""
        with self._lock:
            if self._phase in (IN, FOCUS):
                return False
            now = time.monotonic()
            self._phase = IN
            self._t0 = now
            self._end = now + self.focus_sec
            self._frozen = self.focus_sec
            self._paused = False
            self._paused_remain = 0.0
            self._manual_pause = False
            return True

    def stop(self):
        """立即淡回待机（「停止学习」/ B 键）。**幂等**：已在淡出 / 已待机时是空操作。"""
        with self._lock:
            if self._phase in (IDLE, OUT):
                return False
            self._frozen = self._paused_remain if self._paused else \
                max(0.0, self._end - time.monotonic())
            self._paused = False
            self._manual_pause = False
            self._phase = OUT
            self._t0 = time.monotonic()
            return True

    def pause(self, manual=False):
        """冻结倒计时（离座 or 「暂停学习」）。只在 focus 阶段有效。

        返回一个**字符串状态**而不是布尔 —— 调用方需要区分「这次真的暂停了」
        和「本来就在暂停中」，否则用户对着已暂停的钟说「暂停学习」，
        日志会打「未在倒计时中」，与实际不符。
            "paused"   本次成功暂停
            "already"  已经在暂停中（手动 or 离座），本次是空操作
            "idle"     不在专注阶段，暂停无效
        """
        with self._lock:
            if self._phase != FOCUS:
                return "idle"
            if self._paused:
                # 已在暂停：不重复冻结（_paused_remain 会被覆盖成更短的剩余），
                # 但**升级**标记 —— 离座期间用户又说了「暂停学习」，应转为手动暂停。
                if manual:
                    self._manual_pause = True
                return "already"
            self._paused_remain = max(0.0, self._end - time.monotonic())
            self._paused = True
            if manual:
                self._manual_pause = True
            return "paused"

    def resume(self, force=False):
        """恢复倒计时：把冻结的剩余秒数挂回当前时刻。幂等。

        ⚠️ 用户**手动**暂停时，离座监督的回座 `resume()` **不该**解开它 ——
        只有 `force=True`（用户说「继续学习」）才允许覆盖手动暂停。
        """
        with self._lock:
            if not self._paused:
                return False
            if self._manual_pause and not force:
                return False              # 手动暂停优先，自动恢复让路
            self._end = time.monotonic() + self._paused_remain
            self._paused = False
            self._manual_pause = False
            return True

    @property
    def phase(self):
        with self._lock:
            return self._phase

    @property
    def paused(self):
        with self._lock:
            return self._paused

    @property
    def manual_paused(self):
        with self._lock:
            return self._manual_pause

    # --- 主循环调用 ---
    def snapshot(self, with_phase=False):
        """返回 (p, remain, total)；`with_phase=True` 时追加当前 phase。

        带 phase 的版本**必须在同一个锁内**取这两个量，供主循环使用。
        p=0 待机，p=1 专注。
        """
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
                # 暂停时 _end 是过期的时间戳，必须先判暂停再判归零
                if self._paused:
                    p = 1.0
                elif self._end - now <= 0.0:
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

            if ph in (IN, FOCUS):
                remain = self._paused_remain if self._paused else max(0.0, self._end - now)
            else:
                remain = self._frozen

        if finished and self.on_finish is not None:
            try:
                self.on_finish()
            except Exception as e:
                print("[番茄钟] on_finish 回调出错：%s" % e)
        if with_phase:
            return p, remain, fs, ph
        return p, remain, fs

    def snapshot_phase(self):
        """一次取锁同时返回 (p, remain, total, phase)。

        ⚠️ 主循环**必须**用它，不能先 `snapshot()` 再单独读 `self.phase` ——
        两次取锁之间若有语音回调（trigger / cmd_stop）改了状态，
        这一帧的 `p` 和 `focusing` 就会对不上（例如 p=1.0 但 phase 已是 OUT）。
        """
        return self.snapshot(with_phase=True)


# ------------------------------ 在座检测 ------------------------------

# 所有 I2C 访问共用一把锁：ASR 轮询线程、TTS 播放线程、二哈轮询线程
# 都在同一条 I2C1 上，而 pinpong 的 I2C 对象**不是线程安全的** ——
# 两边同时发事务会互相打断（实测能打断 TTS 的合成）。
I2C_LOCK = threading.RLock()


class HuskyPresence(threading.Thread):
    """二哈识图人脸识别 → 有没有人。

    🔹 **不认人**：请求用的是 `COMMAND_REQUEST_BLOCKS`（0x21，请求**全部**检测框），
       不是 `..._LEARNED`(0x22) / `..._BY_ID`(0x23)。所以**任意一张脸都算在座** ——
       不需要事先在模块上「学习」某个人，也不关心是谁。只要检测框数 > 0 就判在场。

    轮询放在后台线程：驱动失败时会重试 6 次、每次 sleep 0.5s 才抛错，
    放在主循环里会把画面卡死。
    """

    def __init__(self, poll_sec=PRESENCE_POLL_SEC, warmup_sec=HUSKY_WARMUP_SEC,
                 cross_check=False):
        super(HuskyPresence, self).__init__()
        self.daemon = True
        self.poll_sec = poll_sec
        self.warmup_sec = warmup_sec
        self.cross_check = cross_check   # 额外跑一遍库的读法做对照（慢 15 倍，仅调试用）
        self.available = False
        self.failed = False           # 初始化就失败（区别于「还在预热」）
        self.note = "二哈识图 I2C 0x32 · 人脸识别"
        self._present = True          # 出错时按「在场」处理，宁可漏报不要误报
        self._stop = threading.Event()
        self._n_ok = 0
        self._n_face = 0
        self._n_err = 0           # **连续**读失败次数（读成功即清零）
        self._n_err_total = 0     # 累计读失败次数（只用于排查，不参与退出判断）
        self._n_badpkt = 0        # 包头非法的次数（既不算命中也不算错误）
        # 两种读法的对照统计（用来定位「模块没看到脸」还是「我们的读法丢了脸」）
        self._n_face_lib = 0      # 库的逐字节读命中次数
        self._n_face_fast = 0     # 一次性读包头命中次数
        self._n_diff = 0          # 两种读法结论不一致的次数
        self._t_lib = 0.0         # 库读法累计耗时
        self._t_fast = 0.0        # 直读累计耗时
        self._last_head = None    # 最近一次读到的整包（心跳时打出来，便于核对协议）
        self._was_face = None     # 上一次的在场状态（用来打翻转日志）
        self._dump_left = 5       # 前几次不一致时把原始字节打出来

    @property
    def present(self):
        return self._present

    # --- 两种读法 ---

    def _read_head_fast(self):
        """一次性把整个响应包读回来，取包头的计数字节。

        返回 (count, packet_bytes)；包不合法时 count 为 None。
        读的是**同一个响应**（驱动用随机访问的 readfrom_mem），所以不会互相干扰。

        ⚠️ 这里故意读**整个包**（16 字节）而不是只读 6 字节 —— 为的是把
        datalen / command / 每个 data 字节都 dump 出来，核对协议里
        「检测框数」到底在 data[0] 还是在别处（当前只信 data[0]，
        而模块一直回 0，必须能看清全貌才能判断是数据位置错了还是真的没人）。
        """
        pkt = self._hl.i2c.readfrom_mem(HL_ADDR, 0, HL_PACKET_LEN)
        if len(pkt) < HL_HEAD_LEN or pkt[0] != 0x55 or pkt[1] != 0xAA:
            return None, pkt
        return pkt[5], pkt

    @staticmethod
    def _hex(b, n=16):
        try:
            return " ".join("%02X" % x for x in list(b)[:n])
        except Exception:
            return repr(b)

    def _log_transition(self, face, n_fast, head):
        """在场/离座翻转时打一行 —— 用来看模块的检测是不是在抖。"""
        if face == self._was_face:
            return
        self._was_face = face
        print("[在座] %s：检测框数=%s 包=[%s]"
              % ("看到人脸" if face else "人脸消失", n_fast, self._hex(head)))

    def run(self):
        try:
            from pinpong.libs.dfrobot_huskylens import Huskylens
            with I2C_LOCK:
                self._hl = Huskylens(i2c_addr=0x32, bus_num=0)
                time.sleep(0.3)
                self._hl.command_request_algorthim("ALGORITHM_FACE_RECOGNITION")
            time.sleep(self.warmup_sec)      # 切算法后官方建议等约 5s 再读数
            with I2C_LOCK:
                self._hl.read_count("blocks")
        except Exception as e:
            print("[在座] 二哈不可用（%s）→ 退回触发文件模拟" % e)
            self.available = False
            self.failed = True
            return

        self.available = True
        print("[在座] 二哈就绪（人脸识别，I2C 0x32），轮询 %.0f Hz%s"
              % (1.0 / self.poll_sec, "，含库读法对照" if self.cross_check else ""))
        next_beat = time.time() + HUSKY_HEARTBEAT_SEC
        while not self._stop.is_set():
            try:
                with I2C_LOCK:
                    t0 = time.monotonic()
                    # ---- 主读法：自己发「请求 blocks」+ 一次性读包头 ----
                    # 只取包头的计数字节就够判「有没有脸」，实测 3.4ms；
                    # 库的 read_count 是逐字节 readfrom_mem，同一个响应要 51.3ms（慢 15 倍），
                    # 所以在 10Hz 轮询下必须用这个快路径。
                    self._hl.i2c.writeto(HL_ADDR, HL_REQ_BLOCKS)
                    n_fast, head = self._read_head_fast()
                    t1 = time.monotonic()
                    self._last_head = head

                    # ---- 可选对照：库的读法（默认关，调试时开）----
                    n_lib = None
                    if self.cross_check:
                        n_lib = self._hl.read_count("blocks")
                        t2 = time.monotonic()
                    else:
                        t2 = t1

                self._t_fast += t1 - t0
                self._n_ok += 1
                # ⚠️ 读成功就把「连续失败」清零 —— 否则 _n_err 实际是**累计**失败数，
                #    10Hz 轮询下跑一小时是 3.6 万次读，只要偶发错误累计到 20 次
                #    就永久停轮询（注释写的是「连续」，行为却是「累计」）。
                self._n_err = 0

                # ⚠️ n_fast is None = **包不合法**（包头不是 55 AA，多半是 I2C 错位读到了
                #    上一次响应的尾巴）。这**不是**「没人脸」，必须按在场兜底 ——
                #    否则一次错位就判离座，60 秒后误报暂停 + 语音骚扰。
                #    只有「包合法且计数为 0」才是真的没人。
                valid = n_fast is not None
                face = (n_fast or 0) > 0
                if face:
                    self._n_face_fast += 1

                if self.cross_check:
                    self._t_lib += t2 - t1
                    lib_face = (n_lib or 0) > 0
                    if lib_face:
                        self._n_face_lib += 1
                    if lib_face != face:
                        self._n_diff += 1
                        if self._dump_left > 0:
                            self._dump_left -= 1
                            print("[在座] ⚠️ 两种读法不一致：库=%s 直读=%s 包头=[%s]"
                                  % (n_lib, n_fast, self._hex(head)))
                    face = face or lib_face

                if not valid:
                    # 包不合法：既不翻转在场状态，也不计入检测率
                    self._n_badpkt += 1
                    if self._n_badpkt <= 3:
                        print("[在座] 包头非法（按在场兜底，不翻转状态）包=[%s]"
                              % self._hex(head))
                    self._present = True
                else:
                    # 看到脸 = 在场；包合法但计数为 0 = 确实没人
                    self._present = face
                    if face:
                        self._n_face += 1
                self._log_transition(self._present, n_fast, head)
            except Exception as e:
                self._n_err += 1
                self._n_err_total += 1
                self._present = True         # 读失败当作在场，避免误报离座
                if self._n_err <= 3:
                    print("[在座] 读取失败：%s" % e)
                if self._n_err >= HUSKY_MAX_ERRORS:
                    # ⚠️ 必须同时置 failed —— Presence 靠 available/failed 两个标志
                    #    决定「继续按在场兜底」还是「回落到触发文件源」。
                    #    只设 available=False 会让 Presence 一直走预热分支、永久返回在场，
                    #    离座检测静默失效且没有任何上层告警。
                    print("[在座] ⚠️ **连续**失败 %d 次（累计 %d）→ 停止轮询，"
                          "**离座检测已失效**（回落到触发文件源）"
                          % (self._n_err, self._n_err_total))
                    self.available = False
                    self.failed = True
                    return

            # 心跳：报「检测率」和单次读耗时 —— 一眼看出是模块没看到人还是读法有问题
            if HUSKY_HEARTBEAT_SEC and time.time() >= next_beat:
                n = max(1, self._n_ok)
                rate = 100.0 * self._n_face_fast / n
                extra = ""
                if self.cross_check:
                    extra = ("（库 %d，不一致 %d，库耗时 %.1fms）"
                             % (self._n_face_lib, self._n_diff, self._t_lib / n * 1000.0))
                print("[在座] 二哈心跳：读 %d 次 / 看到人脸 %d 次 = %.1f%%%s"
                      " / 单次读 %.1fms / 错误 %d（累计 %d）/ 非法包 %d"
                      " / 整包[%s]"
                      % (self._n_ok + self._n_err_total, self._n_face_fast, rate, extra,
                         self._t_fast / n * 1000.0, self._n_err, self._n_err_total,
                         self._n_badpkt, self._hex(self._last_head)))
                next_beat = time.time() + HUSKY_HEARTBEAT_SEC

            self._stop.wait(self.poll_sec)

    def stop(self):
        self._stop.set()


class FilePresence(object):
    """没有二哈时的替代：由触发文件驱动。

        touch /tmp/step3_absent   →  判离座（开始倒计时）
        rm    /tmp/step3_absent   →  判回座
    """

    def __init__(self, path=AWAY_FILE):
        self.path = path
        self.available = False
        self.note = "触发文件模拟（touch %s = 离座）" % path

    @property
    def present(self):
        return not os.path.exists(self.path)

    def stop(self):
        pass


class Presence(object):
    """在座来源的对外统一接口：优先二哈，拿不到就自动退回触发文件。

    刻意**不阻塞启动** —— 二哈初始化要等约 5.5s 预热，若在这里 join 会让
    开机后十几秒屏幕冻住。所以：初始化完成前一律按「在场」处理（不会误报离座），
    线程自己报就绪或失败，之后自动切换来源。
    """

    def __init__(self, use_husky=True, husky=None):
        self._file = FilePresence()
        self._husky = husky if use_husky else None
        self._announced = False
        self.note = self._husky.note if self._husky is not None else self._file.note

    @property
    def present(self):
        # 调试钩子（优先级最高，二哈在线时也生效）：
        #   存在 step3_present → 强制「在场」，用来验证回座绿帧；
        #   存在 step3_absent  → 强制「离座」，用来验证三档画面，不用真的人走开。
        if os.path.exists(PRESENT_FILE):
            return True
        if os.path.exists(AWAY_FILE):
            return False
        h = self._husky
        if h is not None and h.available:
            return h.present
        if h is not None and not h.failed:
            return True                      # 还在预热：按在场处理
        if not self._announced:
            self._announced = True
            self.note = self._file.note
            print("[在座] 当前来源：%s" % self.note)
        return self._file.present

    def stop(self):
        if self._husky is not None:
            self._husky.stop()


# ------------------------------ 离座监督 ------------------------------

class AwaySupervisor(object):
    """把「有没有人」变成「画哪一帧 + 要不要暂停番茄钟 + 要不要出声」。

    只管三档升级与回座，不碰 I2C（语音走 Speaker 的队列）。
    """

    def __init__(self, app, speaker=None, away_sec=AWAY_SEC, l2_sec=L2_SEC, l3_sec=L3_SEC):
        self.app = app
        self.speaker = speaker
        self.away_sec = float(away_sec)
        self.l2_sec = float(l2_sec)
        self.l3_sec = float(l3_sec)
        self._last_seen = time.monotonic()
        self._tier = None
        self._recover_until = 0.0
        self._voice_next = 0.0
        self._voice_n = 0
        self.n_away_events = 0         # 达到阈值、真正触发提醒的次数
        self.n_short_away = 0          # 未达阈值的短离座次数
        self.away_total = 0.0          # 累计离座秒数（**含**未达阈值的短离座）

    @property
    def tier(self):
        return self._tier

    def _say(self, text):
        # ⚠️ 必须包住异常：_say 是从主循环的 update() 里调的，
        #    一旦 Speaker.say() 抛出（队列问题 / 静音文件判断出错等）
        #    就会顺着 update() 一路把整个进程带崩 —— 那时屏幕和番茄钟全没了。
        #    语音只是提醒，任何情况下都不该成为致命路径。
        if self.speaker is None:
            return
        try:
            self.speaker.say(text)
        except Exception as e:
            print("[离座] 语音播报失败（忽略，不影响计时）：%s" % e)

    def _enter(self, tier, away, mono):
        self._tier = tier
        self._voice_n = 0
        if tier == "l1":
            print("[离座] %.0f 秒没人 → L1 轻提醒，**番茄钟已暂停**" % away)
            self._say(VOICE_L1)
            self._voice_next = mono + L2_REPEAT_SEC
        elif tier == "l2":
            print("[离座] 已 %.0f 秒（>%.0f 分）→ L2 中提醒" % (away, self.l2_sec / 60.0))
            self._say(VOICE_L2)
            self._voice_next = mono + L2_REPEAT_SEC
        elif tier == "l3":
            print("[离座] 已 %.0f 秒（>%.0f 分）→ L3 强提醒" % (away, self.l3_sec / 60.0))
            self._say(VOICE_L3)
            self._voice_next = mono + L3_INTERVALS[0]

    def update(self, present, mono, focusing, p, remain, total):
        """返回这一帧要画的 View。"""
        # 没在专注就不监督（一屏一事：待机时只有时钟）
        if not focusing:
            if self._tier is not None:
                self._tier = None
                self.app.resume()
            self._last_seen = mono
            self._recover_until = 0.0
            return View(p, remain, total, pulse=breathe(mono, PULSE_PERIOD[None]))

        if present:
            if self._tier is not None:
                # ⚠️ 必须在更新 `_last_seen` **之前**算 —— `_last_seen` 停在
                #    他最后被看到的那一刻，`mono - _last_seen` 才是完整的离座时长。
                #    （早先误用「进入 L1 的时刻」当起点，结果漏掉了阈值前那 60 秒。）
                gone = mono - self._last_seen
                self.away_total += gone
                self._tier = None
                # ⚠️ 先判 resume() 是否真的生效，再决定怎么"庆祝"。
                #    用户**手动**暂停（「暂停学习」）时 resume() 返回 False（被 _manual_pause
                #    拦住）—— 若照样出绿帧 + 播「欢迎回来」，就是在骗人：
                #    画面和语音都说恢复了，倒计时却还冻着。
                if self.app.resume():
                    self._recover_until = mono + RECOVER_SEC
                    print("[离座] 回座（本次离开 %.0f 秒，累计 %.0f 秒）→ 番茄钟恢复"
                          % (gone, self.away_total))
                    self._say(VOICE_RECOVER)
                else:
                    print("[离座] 回座（本次离开 %.0f 秒，累计 %.0f 秒）"
                          "→ 保持**手动暂停**，倒计时未恢复" % (gone, self.away_total))
                    self._say(VOICE_RECOVER_MANUAL)
            elif mono - self._last_seen >= SHORT_AWAY_MIN_SEC:
                # 离座没到阈值（还没进 _tier）就回来了 —— 原来是**直接丢弃**，
                # 导致 away_total 只统计 ≥ away_sec 的离座、长期偏低。
                # 这里把未达阈值的离座补记进统计（但不进事件数、不提醒）。
                gone = mono - self._last_seen
                self.away_total += gone
                self.n_short_away += 1
                if gone >= 5.0:
                    print("[离座] 短离座 %.0f 秒（未达阈值 %.0f 秒）→ 计入累计"
                          % (gone, self.away_sec))
            self._last_seen = mono
        else:
            away = mono - self._last_seen
            new = None
            if away >= self.l3_sec:
                new = "l3"
            elif away >= self.l2_sec:
                new = "l2"
            elif away >= self.away_sec:
                new = "l1"

            if new != self._tier:
                if new is None:
                    self._tier = None          # 还没到阈值，静默
                else:
                    # 首次判离座 → 暂停番茄钟（DESIGN.md 原表是 L3 才冻结，
                    # 用户 2026-10-14 明确要求「超过 1 分钟就停止番茄钟」，故提前到 L1）
                    if self._tier is None:
                        # ⚠️ 事件计数放在**判离座处**，不放在回座处 ——
                        #    放回座处的话，会话中途按 B 结束（走 focusing=False 分支）
                        #    就永远不会统计到这次离座，注释说的「触发次数」会偏低。
                        self.n_away_events += 1
                        r = self.app.pause()
                        if r != "paused":
                            # 只可能是 "already"（用户已手动暂停）—— 此时不能
                            # 覆盖成离座暂停，否则回座时会把用户的手动暂停解开。
                            print("[离座] 番茄钟已处于暂停（%s），保持手动暂停语义" % r)
                    self._enter(new, away, mono)

            if self._tier == "l3" and mono >= self._voice_next:
                self._say(VOICE_L3)
                i = min(self._voice_n, len(L3_INTERVALS) - 1)
                self._voice_next = mono + L3_INTERVALS[i]
                self._voice_n += 1
            elif self._tier == "l2" and mono >= self._voice_next:
                self._say(VOICE_L2)
                self._voice_next = mono + 1e9   # L2 只重复一次

        # 回座绿帧优先于一切
        if self._recover_until > mono:
            return View(p, remain, total, recover=True)

        # 手动暂停（「暂停学习」）也要在屏幕上看得出来
        man = self.app.manual_paused

        if self._tier:
            period = PULSE_PERIOD.get(self._tier, 2.0)
            return View(p, remain, total, tier=self._tier,
                        away_sec=mono - self._last_seen, pulse=breathe(mono, period))

        return View(p, remain, total, pulse=breathe(mono, PULSE_PERIOD[None]),
                    paused=man)


# ------------------------------ 语音 ------------------------------

class VoiceASR(object):
    """DFR0715（LD3320）语音识别。关键词在代码里用拼音下发，最后必须 start()。"""

    def __init__(self, words=WAKE_WORDS, mode=VOICE_MODE, mic=VOICE_INPUT):
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        with I2C_LOCK:
            self._asr = DFRobot_ASR()              # I2C 0x4F
            self._asr.begin(getattr(self._asr, mode), getattr(self._asr, mic))
            for text, idx in words:
                self._asr.add_command(text, idx)
            self._asr.start()

    def read(self):
        with I2C_LOCK:
            return self._asr.read()                # 无结果返回 -1


class Speaker(object):
    """DFR0760（V2）语音合成。后台线程 + 队列，绝不阻塞屏幕刷新与语音轮询。

    自己实现合成而不用库的 `speak()`：库的 `wait()` 是**没有超时的死循环**，
    模块一旦不应答工作线程就永久卡死（表现为「日志有『合成』但永远没声音、也不报错」）。
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
        """入队播报。**静音开关**：存在 `TTS_MUTE_FILE` 时只打日志、不出声 ——
        语音识别与番茄钟完全不受影响，删掉文件即可立刻恢复，不用重启进程。
        """
        if os.path.exists(TTS_MUTE_FILE):
            print("[语音] 已静音（%s 存在），跳过：%s" % (TTS_MUTE_FILE, text))
            return
        print("[语音] 待播：%s" % text)
        self._q.put(text)

    def _ack(self):
        try:
            with I2C_LOCK:                       # 短临界区：只包住一次读
                d = self._tts.read_ack(1)
            return d[0] if d else None
        except Exception as e:
            print("[语音] 读模块状态失败：%s" % e)
            return None

    def _speak_once(self, text):
        # ⚠️ 整条合成链路最坏 ACK_START_TIMEOUT(3s) + 0.1s + ACK_DONE_TIMEOUT(30s) ≈ 33s。
        #    若把 `with I2C_LOCK:` 套在整个 _speak_once 外面（改造前的写法），
        #    这 33 秒里 ASR 轮询与二哈轮询**全部被挡在锁外** ——
        #    表现为「一播报，在座检测就瞎了」。
        #    所以这里只把**每一次实际 I2C 事务**包成短临界区，等待的 sleep 不持锁。
        # 只编码一次：`errors="replace"` 保证不会抛异常（所以下面不需要 try），
        # 再用同一个 bytes 判一下有没有发生替换 —— 早先这里是编码两次、
        # 还套着一个永远不会触发的 try/except。
        raw = text.encode("gb2312", "replace")
        if raw.decode("gb2312", "replace") != text:
            print("[语音] ⚠️ 文本含 gb2312 之外的字符，已替换：%s" % text)
        data = list(raw)

        n = len(data) + 2
        with I2C_LOCK:
            self._tts.write_cmd([0xFD, (n >> 8) & 0xFF, n & 0xFF, 0x01, 0x00] + data)

        t0 = time.monotonic()
        started = False
        while time.monotonic() - t0 < ACK_START_TIMEOUT:
            if self._ack() == 0x41:
                started = True
                break
            time.sleep(0.02)
        if not started:
            return None

        # ⚠️⚠️ 这一步绝对不能省！库的 wait() 在 0x41 之后固定 sleep(0.1)。
        #      省掉它立刻发状态查询会**打断刚起步的合成** → 耗时 0.00s、完全没声音，
        #      但日志上一切正常、不报任何错（2026-10-14 对照实验坐实）。
        time.sleep(0.1)

        while time.monotonic() - t0 < ACK_DONE_TIMEOUT:
            with I2C_LOCK:                       # 短临界区：发查询 + 读应答
                self._tts.write_cmd([0xFD, 0x00, 0x01, 0x21])
                done = self._tts.read_ack(1)
            if done and done[0] == 0x4F:
                return time.monotonic() - t0
            time.sleep(0.02)
        return None

    def _speak(self, text):
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
                # ⚠️ 这里**不能**再套 `with I2C_LOCK` —— 锁已经下沉到 _speak_once /
                #    _ack 内部的每次 I2C 事务上。套在这里会把 33 秒全占住。
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
    """后台轮询语音模块。识别到关键词就按 ID 分发到对应回调（边沿触发）。

    四个命令各自一个回调：`on_start` / `on_pause` / `on_resume` / `on_stop`。
    传 `None` 的表示该命令不处理（仍会打日志，不抛错）。
    """

    def __init__(self, asr, on_start=None, on_pause=None, on_resume=None, on_stop=None,
                 ids=tuple(i for _, i in WAKE_WORDS),
                 poll_sec=VOICE_POLL_SEC, debug=VOICE_DEBUG,
                 heartbeat_sec=VOICE_HEARTBEAT_SEC):
        super(VoiceTrigger, self).__init__()
        self.daemon = True
        self.asr = asr
        # ID → (回调, 名字)，只装非 None 的（None 的仍会出现在 self.ids 里，只是不回调）
        self.handlers = {}
        for cid, cb, name in ((CMD_START, on_start, "开始学习"),
                              (CMD_PAUSE, on_pause, "暂停学习"),
                              (CMD_RESUME, on_resume, "继续学习"),
                              (CMD_STOP, on_stop, "停止学习")):
            if cb is not None:
                self.handlers[cid] = (cb, name)
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

            # 边沿触发：同一个 ID 连续出现只处理第一次（避免 LOOP 模式下重复触发）
            if cid != -1 and cid in self.ids and cid != prev:
                h = self.handlers.get(cid)
                if h is None:
                    print("[语音] 识别到「未接线的命令」ID=%s（被忽略）" % cid)
                else:
                    cb, name = h
                    try:
                        cb(cid)
                    except Exception as e:
                        print("[语音] 「%s」回调出错：%s" % (name, e))

            prev = cid

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


def init_board():
    try:
        from pinpong.board import Board
        Board().begin()
        return True
    except Exception as e:
        print("[初始化] pinpong 失败：%s" % e)
        return False


def init_voice(tts=True):
    """返回 (asr, speaker)。任一模块缺失都返回 None，程序退化为纯屏幕版继续跑。

    `tts=False`（`--no-tts`）时**完全不初始化合成模块**，但保留语音识别。
    """
    asr = None
    try:
        asr = VoiceASR()
        print("[语音] 识别模块就绪（I2C 0x4F），共 %d 条命令：%s"
              % (len(WAKE_WORDS), " / ".join("%s(→%d)" % (w, i) for w, i in WAKE_WORDS)))
    except Exception as e:
        # ⚠️ 这里只接 Exception（不是 BaseException）—— 识别模块初始化失败
        #    只会抛普通异常，用 BaseException 会把 Ctrl+C（KeyboardInterrupt）
        #    一并吞掉，启动阶段按 Ctrl+C 就中断不了。
        print("[语音] 未检测到识别模块（%s）" % e)

    speaker = None
    if not tts:
        print("[语音] 语音合成已关闭（--no-tts）")
        return asr, speaker
    try:
        speaker = Speaker()
        print("[语音] 合成模块就绪（I2C 0x40，%s）" % TTS_VERSION)
    except BaseException as e:
        # ⚠️ 这一处**必须**是 BaseException：DFR0760 的 begin(V2) 失败时库内部
        #    直接调 exit()，抛的是 SystemExit，而 SystemExit 继承自 BaseException、
        #    不是 Exception —— 收窄成 Exception 会让它穿透出去终止整个程序。
        #    （这是「该宽就宽」的一处，与上面 VoiceASR 的收窄不矛盾。）
        print("[语音] 未检测到合成模块（%s）" % e)

    return asr, speaker


def run(args):
    gui = create_gui()
    screen = Screen(l3_sec=args.l3_sec)
    box = {}

    def on_finish():
        if box.get("speaker") is not None:
            box["speaker"].say(FINISH_REPLY)
        print("[番茄钟] 时间到")

    app = FocusApp(args.focus_min * 60, on_finish=on_finish)
    total = app.focus_sec

    clock = gui.draw_image(x=0, y=0, image=screen.render(datetime.now(), View(total=total)))
    print("[step3] 已启动：专注 %d 分钟，过渡 %.2fs，旋转 %d°" % (
        args.focus_min, TRANS_SEC, ROTATE))
    print("[step3] 离座阈值 %.0fs → 暂停番茄钟；L2 %.0fs / L3 %.0fs" % (
        args.away_sec, args.l2_sec, args.l3_sec))
    print("[step3] 当前时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Board().begin() 只调一次（二哈与语音都要用）。
    # ⚠️ 必须检查返回值：pinpong 起不来时后面每个模块都会各自报一次「未检测到」，
    #    真正的原因（总线没起来）反而被埋在这几条下游报错里，很难排查。
    board_ok = True
    if not args.no_voice or not args.no_husky:
        board_ok = init_board()
        if not board_ok:
            print("[step3] ⚠️ pinpong Board 初始化失败 —— 语音与二哈都会退化。"
                  "先解决总线问题再排查模块（I2C 三条件见 docs/board-ops.md）")

    asr = speaker = None
    if not args.no_voice:
        if board_ok:
            asr, speaker = init_voice(tts=not args.no_tts)
        else:
            print("[语音] 跳过初始化（总线未就绪）")
    box["speaker"] = speaker
    if speaker is not None and os.path.exists(TTS_MUTE_FILE):
        print("[语音] ⚠️ 检测到 %s —— 当前处于**静音**状态（删除该文件即可恢复）"
              % TTS_MUTE_FILE)

    # ---- 在座检测 ----
    husky = None
    if not args.no_husky and board_ok:
        husky = HuskyPresence(cross_check=args.husky_lib_read)
        husky.start()                # 刻意不 join：预热 5.5s 期间不能让屏幕冻住
    elif not args.no_husky:
        print("[在座] 跳过二哈初始化（总线未就绪）→ 回落触发文件源")
    presence = Presence(use_husky=husky is not None, husky=husky)
    print("[在座] 当前来源：%s" % presence.note)

    sup = AwaySupervisor(app, speaker=speaker,
                         away_sec=args.away_sec, l2_sec=args.l2_sec, l3_sec=args.l3_sec)

    def say(text):
        if box.get("speaker") is not None:
            box["speaker"].say(text)

    def trigger(src):
        if app.start():
            print("[番茄钟] 触发（%s）→ 专注 %d 分钟" % (src, args.focus_min))
            say(START_REPLY % args.focus_min)
        else:
            print("[番茄钟] 已在专注中，忽略（%s）" % src)

    def cmd_pause(src):
        """「暂停学习」：只在倒计时阶段有效，否则给出提示。"""
        r = app.pause(manual=True)
        if r == "paused":
            print("[番茄钟] 手动暂停（%s）→ 倒计时冻结（回座不会自动解开）" % src)
            say(PAUSE_REPLY)
        elif r == "already":
            print("[番茄钟] 已经在暂停中（%s）→ 不重复暂停（已标记为手动）" % src)
            say(PAUSE_REPLY)
        else:
            print("[番茄钟] 未在专注中，暂停无效（%s）" % src)

    def cmd_resume(src):
        """「继续学习」：把冻结的剩余时间挂回当前时刻（能覆盖手动暂停）。"""
        if app.resume(force=True):
            print("[番茄钟] 继续（%s）→ 倒计时恢复" % src)
            say(RESUME_REPLY)
        else:
            print("[番茄钟] 未处于暂停，继续无效（%s）" % src)

    def cmd_stop(src):
        """「停止学习」：结束本次专注，回待机。"""
        if app.stop():
            print("[番茄钟] 停止（%s）→ 结束本次专注" % src)
            say(STOP_REPLY)
        else:
            print("[番茄钟] 已待机，停止无效（%s）" % src)

    trigger_thread = None
    if asr is not None:
        trigger_thread = VoiceTrigger(
            asr,
            on_start=lambda cid: trigger("语音「开始学习」"),
            on_pause=lambda cid: cmd_pause("语音「暂停学习」"),
            on_resume=lambda cid: cmd_resume("语音「继续学习」"),
            on_stop=lambda cid: cmd_stop("语音「停止学习」"),
        )
        trigger_thread.start()

    try:
        gui.on_a_click(lambda *a: trigger("A 键"))
        gui.on_b_click(lambda *a: cmd_stop("B 键"))
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
    # 调试钩子：文件 → 处理函数（touch 一下即触发一次，处理后自动删除）
    file_cmds = ((TRIGGER_FILE, lambda: trigger("触发文件")),
                 (PAUSE_FILE, lambda: cmd_pause("暂停文件")),
                 (RESUME_FILE, lambda: cmd_resume("继续文件")),
                 (STOP_FILE, lambda: cmd_stop("停止文件")))
    try:
        while True:
            mono = time.monotonic()
            now = datetime.now()

            for path, fn in file_cmds:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    fn()

            # 一次取锁拿全 (p, remain, total, phase) —— 见 FocusApp.snapshot_phase 的说明
            p, remain, _, phase_now = app.snapshot_phase()
            focusing = (phase_now == FOCUS)
            v = sup.update(presence.present, mono, focusing, p, remain, total)

            in_trans = 0.0 < p < 1.0 and v.tier is None and not v.recover

            if in_trans:
                if trans_t0 is None:
                    trans_t0, trans_frames = time.monotonic(), 0
                clock.config(image=screen.render(now, v))
                trans_frames += 1
                last_key = None
                continue

            if trans_t0 is not None:
                dt = time.monotonic() - trans_t0
                print("[step3] 过渡实测：%d 帧 / %.2fs = %.1f fps"
                      % (trans_frames, dt, (trans_frames / dt) if dt > 0 else 0.0))
                trans_t0 = None

            if v.tier or v.recover:
                # 动画期：每帧重绘（脉冲 ≤1Hz，节奏由 View.pulse 决定）
                clock.config(image=screen.render(now, v))
                last_key = None
                time.sleep(ANIM_SLEEP)
                continue

            key = ("clock", now.strftime("%H:%M:%S")) if p == 0.0 else ("focus", int(remain))
            if key != last_key:
                clock.config(image=screen.render(now, v))
                last_key = key
            time.sleep(ANIM_IDLE_SLEEP)
    except KeyboardInterrupt:
        print("\n[step3] 已退出")
    finally:
        if trigger_thread is not None:
            trigger_thread.stop()
        if presence is not None:
            presence.stop()


# ------------------------------ 电脑预览 ------------------------------

def preview(out_path, focus_min=25, away_sec=AWAY_SEC, l2_sec=L2_SEC, l3_sec=L3_SEC):
    """不连板子，把六种状态拼成一张对比图（2 行 × 3 列），对照 prototype 核对排版。"""
    screen = Screen(l3_sec=l3_sec)
    total = focus_min * 60.0
    now = datetime.now().replace(second=45)
    remain = total * 0.6        # 让进度条看得出「冻结在某处」

    frames = [
        ("① 待机", View(p=0.0, remain=total, total=total)),
        ("② 专注中", View(p=1.0, remain=remain, total=total)),
        ("③ L1 轻提醒", View(p=1.0, remain=remain, total=total,
                          tier="l1", away_sec=95, pulse=1.0)),
        ("④ L2 中提醒", View(p=1.0, remain=remain, total=total,
                          tier="l2", away_sec=212, pulse=1.0)),
        ("⑤ L3 强提醒", View(p=1.0, remain=remain, total=total,
                          tier="l3", away_sec=324, pulse=1.0)),
        ("⑥ 回座恢复", View(p=1.0, remain=remain, total=total, recover=True)),
    ]

    cols, rows = 3, 2
    pad, label_h = 10, 22
    sheet = Image.new("RGB", (cols * (LAND_W + pad) + pad,
                              rows * (LAND_H + pad + label_h) + pad), "#EEEEEE")
    sd = ImageDraw.Draw(sheet)
    f_lab = load_font(FONT_CN_PATH, 16)

    for i, (label, v) in enumerate(frames):
        frame = screen.render(now, v, rotate=False)
        x = pad + (i % cols) * (LAND_W + pad)
        y = pad + (i // cols) * (LAND_H + pad + label_h)
        sheet.paste(frame, (x, y + label_h))
        sd.rectangle([x, y + label_h, x + LAND_W - 1, y + label_h + LAND_H - 1],
                     outline="#BBBBBB")
        sd.text((x, y + 2), label, font=f_lab, fill="#333333")

    sheet.save(out_path)
    print("[step3] 六态预览已保存：%s（%d×%d）" % (out_path, sheet.width, sheet.height))
    print("[step3] 使用字体：中文=%s / 数字=%s" % (FONT_CN_PATH, FONT_NUM_PATH))


# ------------------------------ 入口 ------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="行空板 M10 智能番茄时钟 · 第 3 段：离座检测 + 分级提醒 + 超时暂停")
    ap.add_argument("--focus-min", type=float, default=25.0, help="专注时长（分钟），默认 25")
    ap.add_argument("--away-sec", type=float, default=AWAY_SEC,
                    help="连续多久没人脸判离座并暂停番茄钟，默认 %.0f 秒" % AWAY_SEC)
    ap.add_argument("--l2-sec", type=float, default=L2_SEC, help="中提醒阈值（秒）")
    ap.add_argument("--l3-sec", type=float, default=L3_SEC, help="强提醒阈值（秒）")
    ap.add_argument("--no-voice", action="store_true", help="不初始化语音模块（纯屏幕）")
    ap.add_argument("--no-tts", action="store_true",
                    help="只关语音合成，保留语音识别（也可运行时 touch %s）" % TTS_MUTE_FILE)
    ap.add_argument("--no-husky", action="store_true",
                    help="不用二哈，改由 %s 触发文件驱动离座" % AWAY_FILE)
    ap.add_argument("--husky-lib-read", action="store_true",
                    help="二哈额外跑一遍库的逐字节读法做对照（慢 15 倍，仅调试）")
    ap.add_argument("--demo", action="store_true",
                    help="启动 %.0f 秒后自动触发一次番茄钟" % DEMO_DELAY_SEC)
    ap.add_argument("--preview", metavar="PNG", help="不连板子，把六态拼成一张图")
    args = ap.parse_args(argv)

    if args.preview:
        preview(args.preview, args.focus_min, args.away_sec, args.l2_sec, args.l3_sec)
    else:
        run(args)


if __name__ == "__main__":
    main()
