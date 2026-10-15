# -*- coding: utf-8 -*-
"""
行空板 M10 · 智能番茄时钟（在座监督版）
======================================

   待机          → 横屏时钟 + 日期（右上角常驻「相机监督信号」）
   按 A 键       → 启动番茄钟，进入「专注中」，同时开启在座监督
   离座 60s+     → 三档递进提醒（屏幕 + 语音），番茄钟自动暂停
   回座          → 绿色反馈帧 1.5 秒，番茄钟自动继续
   按 Home 键    → 结束番茄钟，回到待机
   语音唤醒      → 说出唤醒词，TTS 念出应答

三档提醒（屏幕层次按 DESIGN.md 的「6 通道升级矩阵」逐档放大）
----------------------------------------------------------
   L1  60s ~ 3min  蓝 #1565C0   满宽 28px 提示条 + 底部细条变蓝        单次轻提示
   L2  3min ~ 5min 琥珀 #A85E00  顶部 28px 色带 + 人形符号 + 离座时长   单次 + 一次重复
   L3  > 5min      品红 #C2185B  整屏反色 + 大号秒数 + 「计时已暂停」   循环重复
   回座            绿 #2E7D32    对勾 + 「欢迎回来」（1.5 秒）

硬件
----
- 视觉：二哈识图 1 代 HUSKYLENS（SEN0305），I2C 0x32，**人脸识别**算法
        只输出识别框 (x,y,w,h,ID)，不输出图像；本程序只关心「有没有人脸框」
- 识别：DFR0715（I2C 0x4F），关键词在代码里用拼音定义
- 合成：DFR0760（I2C 0x40，V2）
- 三者共用 M10 的 4Pin I2C —— M10 的两个 4Pin I2C 口是同一条 I2C1 总线的并联引出

屏幕约束（来自 DESIGN.md）
--------------------------
画布 320×240 横屏，Pillow 逐帧绘制 → 禁用渐变/阴影/模糊，线宽一律 ≥2px，
只使用板载字体（中文 HYQiHei_50S.ttf / 数字 DejaVuSans-Bold.ttf）。

运行环境：行空板 M10 板载 Python 3.7.3 + Pillow 9.0.1 + pinpong。
"""

import os
import time
import queue
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from unihiker import GUI

# ============================== 可调参数 ==============================

LAND_W, LAND_H = 320, 240              # 横屏逻辑画布
ROTATE = 270                           # 逆时针旋转角度：90 / 270 互为 180° 翻转

# ---- 番茄钟 ----
FOCUS_MINUTES = 25                     # 单次专注时长（分钟）

# ---- 在座监督 ----
ABSENT_L1_SEC = 60                     # 连续无脸 → 轻提醒
ABSENT_L2_SEC = 180                    # 3 分钟 → 中提醒
ABSENT_L3_SEC = 300                    # 5 分钟 → 强提醒
NO_FACE_CONFIRM = 5                    # 连续几次读不到脸才计入「无脸」（防单帧抖动）
POLL_SEC = 0.2                         # 二哈轮询间隔（秒）
FACE_ALGO_WAIT = 5.0                   # 切换算法后必须等待，等画面稳定

# ⚠️ 测试加速：把三档阈值缩短为 5s / 15s / 25s，方便现场快速验证。
#    正式使用请改为 False。
TEST_FAST = True

# ---- 行为开关 ----
SENSE_MODE = "auto"                    # "auto" 自动探测 / "huskylens" 强制真机 / "simulate" 强制模拟
WATCH_INTERVAL_SEC = 5.0               # "auto" 模式下每隔几秒探一次二哈（插上即自动接管）
DEBUG_KEY_B = True                     # B 键 = 切换「强制模拟离座」（调试 / 演示用）
RECOVER_FEEDBACK_SEC = 1.5             # 回座反馈帧持续秒数
TTS_L3_REPEAT_SEC = 30                 # 强提醒语音重复间隔（秒）
ANIM_FPS = 4                           # 脉冲动画帧率（仅 L2 / L3 / 专注态需要）
VERBOSE = True                         # 打印状态变化日志

# ---- 语音唤醒（沿用已验证的 DFR0715 + DFR0760）----
WAKE_WORDS = (
    ("xiao yi xiao yi", 1),            # 「小易小易」
    ("lao tou zi", 2),                 # 「老头子」
)
WAKE_REPLY = "老头子"                   # 唤醒后 TTS 念出的内容
WAKE_HOLD_SEC = 2.0                    # 唤醒应答画面保持秒数
VOICE_MODE = "LOOP"
VOICE_INPUT = "MIC"
VOICE_DEBUG = False

if TEST_FAST:
    ABSENT_L1_SEC, ABSENT_L2_SEC, ABSENT_L3_SEC = 5, 15, 25

# ===================== 设计令牌（来自 DESIGN.md）=====================

C_BG = "#FFFFFF"                       # 背景
C_TEXT2 = "#546E7A"                    # 次级文字（日期 / 标签）
C_TRACK = "#CFD8DC"                    # 进度条底槽
C_ON = "#FFFFFF"                       # 色块上的反白文字

T_FOCUS = "#00838F"                    # 专注 · 青（品牌基线）
T_L1 = "#1565C0"                       # 轻提醒 · 蓝
T_L2 = "#A85E00"                       # 中提醒 · 琥珀（色带 / 进度条）
T_L2_TEXT = "#9A5B00"                  # 中提醒 · 琥珀（**白底上的文字**，深一档）
T_L3 = "#C2185B"                       # 强提醒 · 品红
C_RECOVER = "#2E7D32"                  # 回座 · 绿

FS_CLOCK = 62                          # 待机时钟
FS_TIMER = 54                          # 专注倒计时
FS_AWAY_NUM = 28                       # 离座时长（L2）
FS_AWAY_BIG = 62                       # 离座时长（L3）
FS_ALERT = 24                          # 大提示
FS_BODY = 18                           # 提示条正文
FS_LABEL = 14                          # 状态标签
FS_MICRO = 12                          # 极小标注

BAR_Y, BAR_H = 236, 4                  # 底部进度条
CAM_CX, CAM_CY = 294, 16               # 相机监督信号锚点（常驻元素）

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# ====================================================================


# ------------------------------ 字体 ------------------------------

def _find_cn_font():
    cands = []
    try:
        import unihiker as _u
        cands.append(os.path.join(os.path.dirname(_u.__file__), "HYQiHei_50S.ttf"))
    except Exception:
        pass
    cands += [
        "HYQiHei_50S.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",              # 文泉驿正黑
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",     # Noto 思源黑体
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def _find_num_font():
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return p
    return None


CN_FONT = _find_cn_font()
NUM_FONT = _find_num_font()
_FONT_CACHE = {}


def font(path, size):
    key = (path, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    if path:
        f = ImageFont.truetype(path, size)
    if f is None:
        try:
            f = ImageFont.load_default(size)      # Pillow >= 10
        except TypeError:
            f = ImageFont.load_default()          # Pillow < 10
    _FONT_CACHE[key] = f
    return f


def cn(size):
    return font(CN_FONT, size)


def num(size):
    return font(NUM_FONT, size)


def draw_center(d, cx, cy, text, fnt, fill):
    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
    d.text((cx - (l + r) / 2.0, cy - (t + b) / 2.0), text, font=fnt, fill=fill)


def draw_left(d, x, cy, text, fnt, fill):
    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
    d.text((x - l, cy - (t + b) / 2.0), text, font=fnt, fill=fill)


def mmss(sec):
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def human_dur(sec):
    """把秒数说成人话：>=60s 用分钟，否则用秒。避免测试加速时出现「0 分钟」。"""
    sec = int(sec)
    if sec >= 60 and sec % 60 == 0:
        return "%d 分钟" % (sec // 60)
    if sec >= 60:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 秒" % sec


def rgba(hex_color, alpha):
    """hex -> RGBA 元组。ImageDraw 在 RGBA 图层上必须用元组才能带透明度。"""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(round(255 * alpha)))


# --------------------------- 相机监督信号 ---------------------------

def draw_cam(d, color, hollow=False, scale=1.0, hole=C_ON, alpha=None):
    """右上角常驻的「相机监督信号」。

    hollow=True 时只描边（待机态，降调）；实心态用于专注 / 各档提醒。
    几何完全按原型：凸块 4×2、机身 14×12 r2、镜头 r2.5、状态点 r1.5。
    alpha=None 表示直接画在 RGB 图上；给了 alpha 则画在 RGBA 叠加层上。
    """
    def col(c):
        return c if alpha is None else rgba(c, alpha)

    def tx(x):
        return CAM_CX + (x - CAM_CX) * scale

    def ty(y):
        return CAM_CY + (y - CAM_CY) * scale

    def box(x, y, w, h):
        return [tx(x), ty(y), tx(x + w), ty(y + h)]

    sw = max(2, int(round(2 * scale)))
    body = col(color)

    d.rectangle(box(292, 8, 4, 2), fill=body)                        # 取景器凸块

    if hollow:
        d.rounded_rectangle(box(287, 10, 14, 12), radius=int(2 * scale),
                            outline=body, width=sw)
        inner = body
    else:
        d.rounded_rectangle(box(287, 10, 14, 12), radius=int(2 * scale), fill=body)
        inner = col(hole)

    lx, ly, lr = tx(291), ty(16), 2.5 * scale                        # 镜头
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=inner)

    sx, sy, sr = tx(300), ty(21), 1.5 * scale                        # 状态点
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=inner)


def draw_person(d, color, cy, alpha=None):
    """人形/在座符号：头 r6 + 身体 20×14 r4（原型坐标）。"""
    c = color if alpha is None else rgba(color, alpha)
    d.ellipse([160 - 6, cy - 6, 160 + 6, cy + 6], fill=c)
    d.rounded_rectangle([150, cy + 8, 170, cy + 22], radius=4, fill=c)


def pulse_alpha(period, t, lo=0.30, hi=1.0):
    """三角波脉冲透明度，用来表现各档不同的动画节奏。"""
    ph = (t % period) / float(period)
    k = 2 * ph if ph < 0.5 else 2 * (1 - ph)
    return lo + (hi - lo) * k


def overlay(base, fn, alpha):
    """把带透明度的图元叠到 base 上，再合成回 RGB。"""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer), alpha)
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def canvas():
    img = Image.new("RGB", (LAND_W, LAND_H), C_BG)
    return img, ImageDraw.Draw(img)


def bar(d, done_ratio, color):
    """底部进度条：底槽 + 已完成段。"""
    d.rectangle([0, BAR_Y, LAND_W, BAR_Y + BAR_H], fill=C_TRACK)
    w = int(LAND_W * max(0.0, min(1.0, done_ratio)))
    if w > 0:
        d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=color)


def done_ratio(remain, total):
    return 0.0 if total <= 0 else (1.0 - remain / float(total))


def finish(img):
    return img.rotate(ROTATE, expand=True)


# ------------------------------ 六种画面 ------------------------------

def render_clock(now):
    """① 待机时钟：大号时间 + 日期 + 静止的相机信号（降调灰蓝）。"""
    img, d = canvas()
    draw_center(d, 160, 95, now.strftime("%H:%M:%S"), num(FS_CLOCK), T_FOCUS)
    draw_center(d, 160, 172, "%04d年%02d月%02d日  %s" % (
        now.year, now.month, now.day, WEEKDAYS[now.weekday()]), cn(FS_BODY), C_TEXT2)
    draw_cam(d, C_TEXT2, hollow=True)
    return finish(img)


def render_focus(remain, total, t):
    """② 专注中：倒计时 + 剩余进度 + 呼吸的相机信号。"""
    img, _ = canvas()
    img = overlay(img, lambda ld, a: draw_cam(ld, T_FOCUS, alpha=a),
                  pulse_alpha(2.0, t, 0.45, 1.0))
    d = ImageDraw.Draw(img)

    draw_left(d, 12, 20, "专注中", cn(FS_LABEL), C_TEXT2)
    draw_center(d, 160, 118, mmss(remain), num(FS_TIMER), T_FOCUS)
    draw_center(d, 160, 156, "剩余时间", cn(FS_LABEL), C_TEXT2)
    bar(d, done_ratio(remain, total), T_FOCUS)
    return finish(img)


def render_l1(absent, remain, total):
    """③ 离座 · 轻提醒：满宽 28px 蓝提示条（最轻的一档，静态）。"""
    img, d = canvas()
    draw_cam(d, T_L1)
    d.rectangle([0, 106, LAND_W, 134], fill=T_L1)
    draw_center(d, 160, 120, "同学，你还在吗？", cn(FS_BODY), C_ON)
    bar(d, done_ratio(remain, total), T_L1)
    return finish(img)


def render_l2(absent, remain, total, t):
    """④ 离座 · 中提醒：顶部琥珀色带 + 人形符号 + 离座时长。"""
    img, _ = canvas()
    img = overlay(img, lambda ld, a: draw_cam(ld, T_L2, alpha=a), 1.0)
    img = overlay(img, lambda ld, a: draw_person(ld, T_L2, 96, alpha=a),
                  pulse_alpha(1.5, t, 0.30, 1.0))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 32, LAND_W, 60], fill=T_L2)
    draw_center(d, 160, 46, "请回到座位", cn(FS_BODY), C_ON)
    draw_center(d, 160, 150, mmss(absent), num(FS_AWAY_NUM), T_L2_TEXT)
    draw_center(d, 160, 176, "离座时长", cn(FS_MICRO), C_TEXT2)
    bar(d, done_ratio(remain, total), T_L2)
    return finish(img)


def render_l3(absent, remain, total, t):
    """⑤ 离座 · 强提醒：整屏反色（品红）+ 大号秒数 + 计时已暂停。"""
    img = Image.new("RGB", (LAND_W, LAND_H), T_L3)
    img = overlay(img, lambda ld, a: draw_cam(
        ld, C_ON, scale=1.4, hole=T_L3, alpha=a), pulse_alpha(1.0, t, 0.40, 1.0))
    img = overlay(img, lambda ld, a: draw_person(ld, C_ON, 72, alpha=a),
                  pulse_alpha(1.0, t, 0.40, 1.0))

    def pause_mark(ld, a):
        ld.rectangle([105, 176, 111, 194], fill=rgba(C_ON, a))
        ld.rectangle([117, 176, 123, 194], fill=rgba(C_ON, a))
        draw_left(ld, 135, 185, "计时已暂停", cn(FS_BODY), rgba(C_ON, a))

    img = overlay(img, pause_mark, pulse_alpha(1.0, t, 0.40, 1.0))
    d = ImageDraw.Draw(img)

    draw_left(d, 12, 20, "离座已超 %s" % human_dur(ABSENT_L3_SEC), cn(FS_LABEL), C_ON)
    draw_center(d, 160, 134, mmss(absent), num(FS_AWAY_BIG), C_ON)

    # 底部进度冻结：底槽与背景同色，只留白色冻结段
    w = int(LAND_W * done_ratio(remain, total))
    if w > 0:
        d.rectangle([0, BAR_Y, w, BAR_Y + BAR_H], fill=C_ON)
    return finish(img)


def render_recover():
    """⑥ 回座恢复：绿色反馈帧（对勾 + 欢迎回来）。"""
    img = Image.new("RGB", (LAND_W, LAND_H), C_RECOVER)
    d = ImageDraw.Draw(img)
    pts = [(132, 122), (152, 144), (192, 92)]
    d.line(pts, fill=C_ON, width=8, joint="curve")
    for (px, py) in pts:                       # 手工补圆头（Pillow 的 line 无圆帽）
        d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=C_ON)
    draw_center(d, 160, 180, "欢迎回来", cn(FS_ALERT), C_ON)
    return finish(img)


def render_wake(text=WAKE_REPLY):
    """语音唤醒应答帧（沿用原有行为）。"""
    img, d = canvas()
    size = FS_CLOCK
    fnt = cn(size)
    while size > 24:
        l, t, r, b = d.textbbox((0, 0), text, font=fnt)
        if r - l <= LAND_W - 24:
            break
        size -= 4
        fnt = cn(size)
    draw_center(d, 160, 120, text, fnt, T_FOCUS)
    return finish(img)


# ------------------------------ 二哈识图 ------------------------------

class HuskylensSensor(object):
    """二哈识图 1 代（HUSKYLENS）人脸识别最小封装。

    只关心「画面里有没有人脸框」。人脸识别算法输出 Block(x,y,w,h,ID)，
    本类读 count 即可；不做任何图像处理（二哈也不输出图像）。
    """

    ALGO = "ALGORITHM_FACE_RECOGNITION"

    def __init__(self):
        from pinpong.libs.dfrobot_huskylens import Huskylens
        self.hl = Huskylens()                       # I2C 0x32，bus 0 → /dev/i2c-4
        self.hl.command_request_algorthim(self.ALGO)
        time.sleep(FACE_ALGO_WAIT)                  # 切算法后必须等待

    def face_count(self):
        """返回人脸框数量；读取失败返回 -1。"""
        try:
            n = self.hl.read_count("blocks")
            return int(n) if n is not None else -1
        except Exception:
            return -1


HUSKYLENS_I2C_ADDR = 0x32


def probe_huskylens(addr=HUSKYLENS_I2C_ADDR):
    """快速探活：只要 0x32 地址有应答就算在线。

    只发一个字节、不做完整握手，因此很快；用于「插上自动识别」。
    二哈收到不完整的帧会直接忽略，不会影响它的正常工作。
    """
    try:
        from pinpong.board import I2C
        i2c = I2C(0)                      # 板上 → /dev/i2c-4
        i2c.writeto(addr, [0x55])
        return True
    except Exception:
        return False


# --------------------------- 在座状态机 ---------------------------

PRESENT, L1, L2, L3 = "present", "l1", "l2", "l3"
STATE_CN = {PRESENT: "在座", L1: "离座·轻", L2: "离座·中", L3: "离座·强"}


class PresenceMonitor(threading.Thread):
    """后台轮询二哈，维护「在座 / 离座三档」状态。

    设计要点：
      * 轮询独立线程，不阻塞屏幕刷新；
      * 连续 NO_FACE_CONFIRM 次读不到脸才计入「无脸」，抑制单帧抖动；
      * 一旦读到脸立即复位（回座要快，误报要少）；
      * 模拟模式下由 B 键切换在座 / 离座。
    """

    def __init__(self, sensor=None, debug=False):
        super(PresenceMonitor, self).__init__()
        self.daemon = True
        self.sensor = sensor
        self.debug = debug
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_seen = time.time()
        self._no_face_run = 0
        self._state = PRESENT
        self._forced_absent = False                 # B 键强制模拟离座
        self._sim_present = True                    # 模拟模式下的「在座」

    # --- 外部控制 ---
    def force_toggle(self):
        with self._lock:
            self._forced_absent = not self._forced_absent
            return self._forced_absent

    def attach(self, sensor):
        """运行期接入真机传感器（由 SensorWatcher 调用），无需重启程序。"""
        with self._lock:
            self.sensor = sensor
            self._last_seen = time.time()
            self._no_face_run = 0
            self._state = PRESENT

    def stop(self):
        self._stop.set()

    # --- 状态查询 ---
    def snapshot(self):
        """返回 (state, absent_sec)。"""
        with self._lock:
            if self._state == PRESENT:
                return PRESENT, 0.0
            return self._state, time.time() - self._last_seen

    def source(self):
        return "真机" if self.sensor is not None else "模拟"

    # --- 主循环 ---
    def run(self):
        while not self._stop.is_set():
            present = self._read_present()
            now = time.time()

            if present:
                with self._lock:
                    self._last_seen = now
                    self._no_face_run = 0
                    new_state = PRESENT
            else:
                with self._lock:
                    self._no_face_run += 1
                    confirmed = self._no_face_run >= NO_FACE_CONFIRM
                    absent = now - self._last_seen
                    if not confirmed:
                        new_state = self._state          # 尚未确认，保持原状态
                    elif absent >= ABSENT_L3_SEC:
                        new_state = L3
                    elif absent >= ABSENT_L2_SEC:
                        new_state = L2
                    elif absent >= ABSENT_L1_SEC:
                        new_state = L1
                    else:
                        new_state = PRESENT

            with self._lock:
                if new_state != self._state:
                    if self.debug:
                        print("[在座] %s -> %s" % (self._state, new_state))
                    self._state = new_state

            self._stop.wait(POLL_SEC)

    def _read_present(self):
        with self._lock:
            forced = self._forced_absent
            sim = self._sim_present

        if forced:
            return False
        if self.sensor is None:                         # 模拟模式
            return sim
        n = self.sensor.face_count()
        if n < 0:                                       # 读取失败，保守当作在座
            return True
        return n > 0


# --------------------------- 插上自动识别 ---------------------------

class SensorWatcher(threading.Thread):
    """SENSE_MODE == "auto" 时后台定期探测二哈，插上就自动接管，不用重启程序。

    只在还没有传感器时工作；一旦接管成功线程即退出。
    """

    def __init__(self, monitor, interval=WATCH_INTERVAL_SEC, debug=True):
        super(SensorWatcher, self).__init__()
        self.daemon = True
        self.monitor = monitor
        self.interval = interval
        self.debug = debug
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            if self.monitor.sensor is not None:
                return
            if probe_huskylens():
                try:
                    s = HuskylensSensor()
                    self.monitor.attach(s)
                    print("[视觉] 检测到二哈识图，已自动切换到真机模式（人脸识别）")
                    return
                except BaseException as e:
                    if self.debug:
                        print("[视觉] 探到 0x32 但初始化失败：%s" % e)
            self._stop.wait(self.interval)


# ------------------------------ 番茄钟 ------------------------------

class FocusTimer(object):
    """最小番茄钟：专注倒计时 + 暂停 / 继续。"""

    def __init__(self, minutes):
        self.total = int(minutes * 60)
        self.remain = float(self.total)
        self.running = False
        self._last = None

    def start(self):
        if self.remain <= 0:
            self.remain = float(self.total)
        self.running = True
        self._last = time.time()

    def pause(self):
        self._tick()
        self.running = False

    def resume(self):
        if self.remain > 0 and not self.running:
            self.running = True
            self._last = time.time()

    def reset(self):
        self.running = False
        self.remain = float(self.total)
        self._last = None

    def _tick(self):
        if self.running and self._last is not None:
            now = time.time()
            self.remain = max(0.0, self.remain - (now - self._last))
            self._last = now
            if self.remain <= 0:
                self.running = False

    def snapshot(self):
        self._tick()
        return self.remain, self.total, self.running


# ------------------------------ 语音 ------------------------------

class VoiceASR(object):
    """DFR0715（LD3320）语音识别，关键词在代码里用拼音定义。"""

    def __init__(self, words=WAKE_WORDS, mode=VOICE_MODE, mic=VOICE_INPUT):
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        self._asr = DFRobot_ASR()                  # I2C 0x4F
        self._asr.begin(getattr(self._asr, mode), getattr(self._asr, mic))
        for text, idx in words:
            self._asr.add_command(text, idx)
        self._asr.start()                          # 「设置完成 开始识别」

    def read(self):
        return self._asr.read()                    # 无结果返回 -1


class Speaker(object):
    """DFR0760（V2）语音合成，异步队列，绝不阻塞主循环与轮询线程。"""

    def __init__(self):
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        self._tts = DFRobot_SpeechSynthesis_I2C()  # I2C 0x40
        self._tts.begin(self._tts.V2)
        self._tts.set_voice(5)
        self._q = queue.Queue()
        t = threading.Thread(target=self._worker)
        t.daemon = True
        t.start()

    def say(self, text):
        self._q.put(text)

    def _worker(self):
        while True:
            text = self._q.get()
            try:
                self._tts.speak(text)
            except Exception as e:
                print("[语音] 合成失败：%s" % e)


# ------------------------------ 启动 ------------------------------

def create_gui():
    """unihiker 0.0.27 的 GUI 首次初始化偶发 RuntimeError，重试一次。"""
    try:
        return GUI()
    except RuntimeError:
        time.sleep(0.5)
        return GUI()


def init_pinpong():
    try:
        from pinpong.board import Board
        Board().begin()
        return True
    except Exception as e:
        print("[初始化] pinpong 失败：%s" % e)
        return False


def init_sensor():
    """按 SENSE_MODE 决定用真机还是模拟。失败一律回落到模拟，不崩。"""
    if SENSE_MODE == "simulate":
        print("[视觉] 强制模拟模式（B 键切换在座 / 离座）")
        return None
    try:
        s = HuskylensSensor()
        print("[视觉] 二哈识图 1 代就绪（I2C 0x32，人脸识别）")
        return s
    except BaseException as e:
        if SENSE_MODE == "huskylens":
            print("[视觉] 二哈识图初始化失败：%s" % e)
        else:
            print("[视觉] 未检测到二哈识图（%s）" % e)
            print("[视觉] → 自动进入模拟模式：按 B 键可切换「在座 / 离座」")
        return None


def init_asr():
    try:
        asr = VoiceASR()
        print("[语音] 识别模块就绪（I2C 0x4F），唤醒词：%s" % " / ".join(w for w, _ in WAKE_WORDS))
        return asr
    except BaseException as e:
        print("[语音] 未检测到识别模块（%s）" % e)
        return None


def init_tts():
    try:
        spk = Speaker()
        print("[语音] 合成模块就绪（I2C 0x40，V2）")
        return spk
    except BaseException as e:          # begin(V2) 失败会调 exit() 抛 SystemExit，须一并接住
        print("[语音] 未检测到合成模块（%s）" % e)
        return None


def main():
    board_ok = init_pinpong()

    gui = create_gui()
    wake_img = render_wake()
    screen = gui.draw_image(x=0, y=0, image=render_clock(datetime.now()))
    print("M10 智能番茄时钟已启动")

    sensor = init_sensor() if board_ok else None
    monitor = PresenceMonitor(sensor=sensor, debug=VERBOSE)
    monitor.start()

    # 二哈没接上时，后台继续探测；插上就自动切换，不用重启程序
    watcher = None
    if sensor is None and SENSE_MODE == "auto":
        watcher = SensorWatcher(monitor, debug=VERBOSE)
        watcher.start()

    speaker = init_tts()
    asr = init_asr() if board_ok else None

    timer = FocusTimer(FOCUS_MINUTES)
    lock = threading.Lock()
    app = {"mode": "idle", "wake_until": 0.0, "recover_until": 0.0,
           "was_away": False, "last_away_state": None, "last_l3_say": 0.0}

    print("[配置] 专注 %d 分钟｜离座阈值 %ds / %ds / %ds%s" % (
        FOCUS_MINUTES, ABSENT_L1_SEC, ABSENT_L2_SEC, ABSENT_L3_SEC,
        "（测试加速模式，正式使用请把 TEST_FAST 改为 False）" if TEST_FAST else ""))
    print("[操作] A=开始/暂停番茄钟｜B=强制模拟离座（当前 %s）｜Home=结束回待机"
          % monitor.source())

    # ---- 语音轮询线程 ----
    wake_ids = tuple(i for _, i in WAKE_WORDS)

    def voice_loop():
        prev = None
        while True:
            if asr is None:
                time.sleep(1.0)
                continue
            try:
                cid = asr.read()
            except Exception as e:
                if VOICE_DEBUG:
                    print("[语音] 读取失败：%s" % e)
                time.sleep(1.0)
                continue
            if VOICE_DEBUG and cid != -1 and cid != prev:
                print("[语音] 识别到 ID = %d" % cid)
            if cid != -1 and cid in wake_ids and cid != prev:
                print("[语音] 唤醒！ID=%d -> %s" % (cid, WAKE_REPLY))
                with lock:
                    app["wake_until"] = time.time() + WAKE_HOLD_SEC
                if speaker is not None:
                    speaker.say(WAKE_REPLY)
            prev = cid
            time.sleep(0.05)

    if asr is not None:
        t = threading.Thread(target=voice_loop)
        t.daemon = True
        t.start()

    # ---- 按键 ----
    def on_a(*args):
        with lock:
            if app["mode"] == "idle":
                timer.start()
                app["mode"] = "focus"
                print("[番茄钟] 开始，%d 分钟" % FOCUS_MINUTES)
            elif timer.running:
                timer.pause()
                print("[番茄钟] 暂停")
            else:
                timer.resume()
                print("[番茄钟] 继续")

    def on_b(*args):
        if not DEBUG_KEY_B:
            return
        forced = monitor.force_toggle()
        print("[调试] 强制模拟离座 = %s" % ("开" if forced else "关"))

    def on_home(*args):
        with lock:
            timer.reset()
            app["mode"] = "idle"
            app["recover_until"] = 0.0
            app["was_away"] = False
        print("[番茄钟] 结束，回到待机")

    for hook, cb in ((gui.on_a_click, on_a), (gui.on_b_click, on_b)):
        try:
            hook(cb)
        except Exception as e:
            print("[按键] 注册失败：%s" % e)
    try:
        # on_key_click(event, onclick) 需要显式给出 Tk 事件名；Home 键的事件名
        # 在闭源驱动里，这里把两种常见写法都绑上，哪个生效都不影响另一个。
        for ev in ("<Home>", "Home"):
            gui.on_key_click(ev, on_home)
        print("[按键] Home 键已绑定（<Home> / Home）")
    except Exception as e:
        print("[按键] Home 键注册失败（不影响其他功能）：%s" % e)
        print("[按键] 番茄钟启动后需等它跑完，或重启程序回到待机")

    # ---- 主循环 ----
    last_key = None
    try:
        while True:
            now_ts = time.time()
            now = datetime.now()
            state, absent = monitor.snapshot()
            remain, total, running = timer.snapshot()

            with lock:
                mode = app["mode"]
                wake_until = app["wake_until"]
                recover_until = app["recover_until"]

            # 回座反馈窗口到期后复位
            if recover_until and now_ts >= recover_until:
                with lock:
                    app["recover_until"] = 0.0
                recover_until = 0.0

            is_away = (state != PRESENT)

            # ---- 回座：给 1.5s 绿色反馈，番茄钟自动继续 ----
            if app["was_away"] and not is_away and mode == "focus":
                with lock:
                    app["recover_until"] = now_ts + RECOVER_FEEDBACK_SEC
                    recover_until = app["recover_until"]
                timer.resume()
                if speaker is not None:
                    speaker.say("欢迎回来")
                print("[在座] 已回座，番茄钟继续")
            app["was_away"] = is_away

            # ---- 离座：番茄钟自动暂停 ----
            if is_away and mode == "focus" and running:
                timer.pause()
                print("[在座] 判定离座（%s），番茄钟已暂停" % STATE_CN.get(state, state))

            # ---- 三档语音提示 ----
            if is_away:
                if state != app["last_away_state"]:
                    if speaker is not None:
                        if state == L1:
                            speaker.say("同学，你还在吗？")
                        elif state == L2:
                            speaker.say("请回到座位")
                        elif state == L3:
                            speaker.say("离座已超过 %s，请马上回到座位" % human_dur(ABSENT_L3_SEC))
                            app["last_l3_say"] = now_ts
                    app["last_away_state"] = state
                elif state == L3 and now_ts - app["last_l3_say"] >= TTS_L3_REPEAT_SEC:
                    if speaker is not None:
                        speaker.say("请马上回到座位")
                    app["last_l3_say"] = now_ts
            else:
                app["last_away_state"] = None

            # ---- 选帧 ----
            if now_ts < recover_until:
                key = "recover"
            elif mode != "focus":
                key = "wake" if now_ts < wake_until else now.strftime("%H:%M:%S")
            elif state == L3:
                key = "l3:%d" % int(absent)
            elif state == L2:
                key = "l2:%d" % int(absent)
            elif state == L1:
                key = "l1:%d" % int(absent)
            else:
                key = "focus:%d" % int(remain)

            # 需要脉冲动画的帧，把动画步进也算进 key
            if key.startswith("l2") or key.startswith("l3") or key.startswith("focus"):
                key += ":%d" % int(now_ts * ANIM_FPS)

            if key != last_key:
                if key == "recover":
                    img = render_recover()
                elif key.startswith("l3"):
                    img = render_l3(absent, remain, total, now_ts)
                elif key.startswith("l2"):
                    img = render_l2(absent, remain, total, now_ts)
                elif key.startswith("l1"):
                    img = render_l1(absent, remain, total)
                elif key == "wake":
                    img = wake_img
                elif key.startswith("focus"):
                    img = render_focus(remain, total, now_ts)
                else:
                    img = render_clock(now)
                screen.config(image=img)
                last_key = key

            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        if watcher is not None:
            watcher.stop()
        monitor.stop()


if __name__ == "__main__":
    main()
