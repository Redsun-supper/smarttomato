# -*- coding: utf-8 -*-
"""
行空板 M10 横屏时钟 + 离线语音唤醒 + 语音合成应答
================================================

    平时         → 全屏横屏数字时钟 + 日期
    识别到唤醒词 → 由语音合成模块念出「老头子」（默认不改变屏幕）

硬件
----
1) 语音识别：DFRobot Gravity I2C 语音识别模块 **DFR0715**（芯片 LD3320）
   - I2C 地址 **0x4F**
   - 关键词**在代码里用拼音定义**（不需要"学习"）：
     `begin(mode, mic)` → `add_command("xiao yi xiao yi", 1)` → `start()`
   - `read()` 返回关键词 ID，无结果返回 -1
   - 关键词建议 4~6 音节；**离麦克风 10cm 以上**

2) 语音合成：DFRobot Gravity 中英文语音合成模块 **DFR0760**（V2）
   - I2C 地址 **0x40**
   - 库：`from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C`
   - `begin(V2)` 初始化 → `speak("中文")` 合成播放（内部按 GB2312 编码后发送）
   - 注意：库里的 `begin(V2)` 握手失败时会调 `exit()` 抛 SystemExit（不是 Exception），
     所以初始化处用 `except BaseException` 兜底，避免整个程序被终止
   - 若换回旧款 DFR0213，把 TTS_VERSION 改成 "V1" 即可

3) 总线：pinpong 的 `I2C(bus=0)` 在行空板上自动映射到 `/dev/i2c-4`（外部 4Pin I2C 口）。
   两个模块可挂同一条 I2C 总线（地址不同：0x4F / 0x40）。

运行环境：行空板 M10 板载 Python 3.7.3 + pinpong 0.6.x。
"""

import os
import time
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from unihiker import GUI

# ============================ 可调参数 ============================

SCREEN_W, SCREEN_H = 240, 320          # M10 竖屏物理分辨率
LAND_W, LAND_H = SCREEN_H, SCREEN_W    # 横屏逻辑画布：320 x 240

# 逆时针旋转角度（横屏逻辑画布 → 整屏旋转）：
#   90  = 横屏方向 A
#   270 = 方向 A 再转 180°（横屏方向 B）
ROTATE = 270

BG_COLOR = "#FFFFFF"                   # 背景色（白色）
TIME_COLOR = "#00838F"                 # 时间颜色（深青）
DATE_COLOR = "#546E7A"                 # 日期颜色（深灰蓝）
TIME_SIZE = 62                         # 时间字号
DATE_SIZE = 18                         # 日期字号

REPLY_TEXT = "老头子"                   # 唤醒后要「念」/「显示」的文字
REPLY_COLOR = "#00838F"                # （屏幕模式下）文字颜色
REPLY_SIZE = 80                        # （屏幕模式下）文字字号，长文本会自动缩小

# 唤醒后的反馈方式：
#   "tts"    —— 只让语音合成模块念出 REPLY_TEXT（屏幕保持时钟）  ← 默认
#   "screen" —— 只在屏幕上显示 REPLY_TEXT
#   "both"   —— 两者都做
REPLY_MODE = "tts"

# ---- 语音识别模块 DFR0715 / LD3320 ----
VOICE_I2C_ADDR = 0x4F                  # DFR0715 固定 I2C 地址
WAKE_WORDS = (                         # 唤醒词：拼音 + 关键词 ID
    ("xiao yi xiao yi", 1),            # 「小易小易」
    ("lao tou zi", 2),                 # 「老头子」
)
WAKE_CMD_IDS = tuple(idx for _, idx in WAKE_WORDS)
VOICE_MODE = "LOOP"                    # 识别模式：LOOP / BUTTON / PASSWORD
VOICE_INPUT = "MIC"                    # 拾音方式：MIC（板载麦）/ MONO（LineIn）
WAKE_HOLD_SEC = 2.0                    # （屏幕模式下）保持显示的秒数
VOICE_POLL_SEC = 0.05                  # 语音轮询间隔（秒）
VOICE_DEBUG = True                     # True：打印识别到的每个关键词 ID，便于调试

# ---- 语音合成模块 DFR0213 / DFR0760 ----
TTS_I2C_ADDR = 0x40                    # 合成模块 I2C 地址
TTS_VERSION = "V2"                     # DFR0760(V2) 用 "V2"；DFR0213 用 "V1"
TTS_VOICE = 5                          # 音量 0~9

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# ==================================================================


# ------------------------------ 字体 ------------------------------

def find_font():
    """定位中文字体：优先 unihiker 自带的汉仪旗黑，避免额外拷贝字体文件。"""
    candidates = []
    try:
        import unihiker as _unihiker
        candidates.append(os.path.join(os.path.dirname(_unihiker.__file__), "HYQiHei_50S.ttf"))
    except Exception:
        pass
    candidates.append("HYQiHei_50S.ttf")
    candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    candidates.append("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")            # 文泉驿正黑
    candidates.append("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")   # Noto 思源黑体
    candidates.append("/usr/share/fonts/truetype/arphic/uming.ttc")               # 文鼎明体
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


FONT_PATH = find_font()
_FONT_CACHE = {}


def make_font(size):
    """带缓存的字体构造（避免每帧重复加载字体文件）。"""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    if FONT_PATH:
        font = ImageFont.truetype(FONT_PATH, size)
    if font is None:
        # 无字体文件兜底：Pillow < 10（如板载 9.0.1）的 load_default 不接受 size 参数
        try:
            font = ImageFont.load_default(size)   # Pillow >= 10
        except TypeError:
            font = ImageFont.load_default()       # Pillow < 10 回退到默认位图字体
    _FONT_CACHE[size] = font
    return font


def draw_center(draw, cx, cy, text, font, fill):
    """以 (cx, cy) 为中心绘制文字。"""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (left + right) / 2.0, cy - (top + bottom) / 2.0),
              text, font=font, fill=fill)


# --------------------------- 画面渲染 ---------------------------

def render_clock(now):
    """在横屏逻辑坐标系（320 x 240）里画一帧时钟，再旋转成 240 x 320 返回。"""
    img = Image.new("RGB", (LAND_W, LAND_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    time_text = now.strftime("%H:%M:%S")
    date_text = "%04d年%02d月%02d日  %s" % (
        now.year, now.month, now.day, WEEKDAYS[now.weekday()])

    draw_center(draw, LAND_W // 2, 95, time_text, make_font(TIME_SIZE), TIME_COLOR)
    draw_center(draw, LAND_W // 2, 172, date_text, make_font(DATE_SIZE), DATE_COLOR)

    return img.rotate(ROTATE, expand=True)


def render_reply(text=REPLY_TEXT):
    """应答画面：全屏居中显示文字。字号自适应，长文本自动缩小以免溢出。"""
    img = Image.new("RGB", (LAND_W, LAND_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    margin = 24
    size = REPLY_SIZE
    font = make_font(size)
    while size > 24:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= LAND_W - margin:
            break
        size -= 4
        font = make_font(size)

    draw_center(draw, LAND_W // 2, LAND_H // 2, text, font, REPLY_COLOR)
    return img.rotate(ROTATE, expand=True)


# --------------------------- 语音驱动 ---------------------------

class VoiceASR(object):
    """DFR0715（LD3320）语音识别模块的薄封装，基于 pinpong 自带库。

    关键词在构造时用拼音下发（add_command），最后必须调用 start()
    （对应 Mind+ 里的「设置完成 开始识别」），否则不会开始识别。
    """

    def __init__(self, words=WAKE_WORDS, mode=VOICE_MODE, mic=VOICE_INPUT):
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        self._asr = DFRobot_ASR()          # 默认地址 0x4F、总线 0（板上 → /dev/i2c-4）
        self._asr.begin(getattr(self._asr, mode), getattr(self._asr, mic))
        for text, idx in words:
            self._asr.add_command(text, idx)
        self._asr.start()                  # 「设置完成 开始识别」

    def read(self):
        """返回识别到的关键词 ID；无结果返回 -1。"""
        return self._asr.read()


class TTSSpeaker(object):
    """DFR0213（V1）/ DFR0760（V2）语音合成模块的薄封装，基于 pinpong 自带库。

    speak() 内部会阻塞等待模块播报完成（库里的 wait()），因此本类只在
    语音轮询线程里被调用，不影响屏幕刷新。
    """

    def __init__(self, version=TTS_VERSION, volume=TTS_VOICE):
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        self._tts = DFRobot_SpeechSynthesis_I2C()   # 默认地址 0x40
        ver = self._tts.V1 if version == "V1" else self._tts.V2
        self._tts.begin(ver)
        self._tts.set_voice(volume)

    def say(self, text):
        """合成并播放一段中文/英文。"""
        self._tts.speak(text)


class VoiceMonitor(threading.Thread):
    """后台轮询语音模块；识别到唤醒词后触发反馈（念出 / 显示）。

    设计要点：
      * 轮询放在独立线程，保证时钟刷新不被 I2C 读操作和语音播报拖慢；
      * 屏幕状态只写一个带锁的时间戳，GUI 仍由主线程驱动（unihiker 的 Tk 要求）；
      * 采用「边沿触发」（ID 变化到唤醒词才响应），避免模块持续返回同一个值时
        被反复触发。
    """

    def __init__(self, asr, speaker=None, wake_ids=WAKE_CMD_IDS, hold_sec=WAKE_HOLD_SEC,
                 poll_sec=VOICE_POLL_SEC, debug=False, show_screen=False):
        super(VoiceMonitor, self).__init__()
        self.daemon = True
        self.asr = asr
        self.speaker = speaker
        self.wake_ids = tuple(wake_ids)
        self.hold_sec = hold_sec
        self.poll_sec = poll_sec
        self.debug = debug
        self.show_screen = show_screen
        self._lock = threading.Lock()
        self._until = 0.0
        self._stop = threading.Event()

    def run(self):
        prev_id = None
        while not self._stop.is_set():
            try:
                cmd_id = self.asr.read()
            except Exception as e:
                if self.debug:
                    print("[语音] 读取失败：%s" % e)
                time.sleep(1.0)
                continue

            if self.debug and cmd_id != -1 and cmd_id != prev_id:
                print("[语音] 识别到 ID = %d" % cmd_id)

            if cmd_id != -1 and cmd_id in self.wake_ids and cmd_id != prev_id:
                print("[语音] 唤醒！ID=%d → %s" % (cmd_id, REPLY_TEXT))
                if self.show_screen:
                    with self._lock:
                        self._until = time.time() + self.hold_sec
                if self.speaker is not None:
                    try:
                        self.speaker.say(REPLY_TEXT)
                    except Exception as e:
                        print("[语音] 合成失败：%s" % e)

            prev_id = cmd_id
            self._stop.wait(self.poll_sec)

    def is_active(self):
        """当前是否处于应答显示窗口内。"""
        with self._lock:
            return time.time() < self._until

    def stop(self):
        self._stop.set()


# ----------------------------- 启动 -----------------------------

def create_gui():
    """创建 GUI 对象。

    容错说明：unihiker 0.0.27 的 GUI.__init__ 里，Tk 主循环是在子线程启动的，
    而 check_alive() 由主线程立即调用。首次运行时若正在安装字体，Tk 线程还没来得及
    进入 mainloop()，主线程就会抛 RuntimeError: main thread is not in main loop。
    这里捕获该异常并重试一次（此时字体已装好，必然成功）。
    """
    try:
        return GUI()
    except RuntimeError:
        time.sleep(0.5)
        return GUI()


def init_pinpong():
    """初始化 pinpong（I2C 依赖它）。返回 True/False。"""
    try:
        from pinpong.board import Board
        Board().begin()
        return True
    except Exception as e:
        print("[语音] pinpong 初始化失败：%s" % e)
        return False


def init_voice_module():
    """初始化语音识别模块；失败返回 None，此时程序退化为纯时钟继续运行。"""
    try:
        asr = VoiceASR()
        print("[语音] 识别模块就绪（I2C 0x%02X），唤醒词：%s" % (
            VOICE_I2C_ADDR, " / ".join(w for w, _ in WAKE_WORDS)))
        return asr
    except Exception as e:
        print("[语音] 未检测到语音识别模块（%s）。" % e)
        return None


def init_tts_module():
    """初始化语音合成模块；失败返回 None（唤醒时只打印提示）。"""
    try:
        spk = TTSSpeaker()
        print("[语音] 合成模块就绪（I2C 0x%02X，%s）" % (TTS_I2C_ADDR, TTS_VERSION))
        return spk
    except BaseException as e:   # 库里的 begin(V2) 失败时会调 exit() 抛 SystemExit，须一并接住
        print("[语音] 未检测到语音合成模块（%s）。" % e)
        return None


def main():
    # pinpong 先于 GUI 初始化（官方示例顺序）
    board_ok = init_pinpong()

    gui = create_gui()
    reply_img = render_reply()
    clock = gui.draw_image(x=0, y=0, image=render_clock(datetime.now()))
    print("M10 横屏时钟已启动（反馈方式：%s）" % REPLY_MODE)

    show_screen = REPLY_MODE in ("screen", "both")
    asr = init_voice_module() if board_ok else None
    speaker = init_tts_module() if (board_ok and REPLY_MODE in ("tts", "both")) else None

    monitor = None
    if asr is not None:
        monitor = VoiceMonitor(asr, speaker=speaker, debug=VOICE_DEBUG,
                               show_screen=show_screen)
        monitor.start()

    last_key = None
    try:
        while True:
            now = datetime.now()
            # 先判断该显示哪一帧（开销很小），只有内容真正变化时才渲染 + 刷新，
            # 避免每秒把时钟重绘 10 次带来的无谓 CPU 占用
            if monitor is not None and monitor.is_active():
                key = "reply"
            else:
                key = now.strftime("%H:%M:%S")

            if key != last_key:
                img = reply_img if key == "reply" else render_clock(now)
                clock.config(image=img)
                last_key = key

            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if monitor is not None:
            monitor.stop()


if __name__ == "__main__":
    main()
