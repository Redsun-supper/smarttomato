# -*- coding: utf-8 -*-
"""
行空板 M10 · 智能番茄时钟 —— 第 1 段：显示时间与日期
======================================================

本段只做一件事：让 M10 的 2.8 寸屏横过来，安静地显示

        ┌──────────────────────────┐
        │        13:29:45          │   ← 62px 深青（#00838F）
        │   2026年10月13日 星期二   │   ← 18px 深灰蓝（#546E7A）
        └──────────────────────────┘

它是整个项目的「地基」，后续每一段都在这上面叠加：

    第 1 段  时钟（本文件）         时间 / 日期 / 横屏 / 整秒刷新
    第 2 段  番茄钟状态机           倒计时、专注 ↔ 休息循环
    第 3 段  二哈识图监督           离座 / 玩手机 → 出声提醒
    第 4 段  语音唤醒 + 拍照解题     DFR0715 / DFR0760 / USB 摄像头

────────────────────────────────────────────────────────────────────
横屏是怎么来的
    M10 的物理屏是 240(宽) × 320(高) 的竖屏，而 unihiker 的 GUI 没有整屏旋转接口。
    所以这里沿用 DFRobot 官方推荐思路：先在「横屏逻辑画布 320 × 240」上正常排版，
    画完整张图后逆时针旋转 90°，再整屏贴到屏幕上。
    好处是 —— 所有坐标都按横屏直觉书写，不需要做任何坐标换算。

为什么单独写一个 ClockRenderer 类
    板上 CPU 只有 1.2GHz，Pillow 如果每帧都重新 ImageFont.truetype() 加载 62px 字体，
    会有肉眼可见的卡顿。ClockRenderer 把「画布 / 画布句柄 / 各字号字体」全部缓存，
    每帧只重绘内容 —— 这套基础设施后面每一段都要复用。

运行环境
    · 行空板 M10 板载 Python 3.7.3 + Pillow 9.0.1（本文件已对 Pillow < 10 做兼容）
    · 或 Mind+ 内置 Python 3.11.9（unihiker 0.0.27）
    · 或任意装有 Pillow 的电脑（用 --preview 出图，无需连板子）

用法
    # 1) 在板子上跑：横屏时钟常驻
    python3 m10_step1_clock.py

    # 2) 不连板子，只把这一帧渲染成 PNG 看效果
    python3 m10_step1_clock.py --preview step1_preview.png
    python3 m10_step1_clock.py --preview step1_preview.png --at "2026-10-13 13:29:45"

    # 3) 横屏方向反了（90 与 270 互为 180° 翻转）
    python3 m10_step1_clock.py --rotate 90

放置位置
    Mind+ → Python 积木模式 → 项目「新建项目」→ 该文件即项目下的 main.py

⚠️ 板子没有 RTC、也没有外网，系统时间默认可能是错的（曾显示 2019-02-14），
   时间不准会让「时钟」看起来像坏了。上板前先对时：
       date -s '2026-10-13 20:56:00' && hwclock -w
"""

import os
import sys
import time
import argparse
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

# ===================== 设计令牌（全部取自 DESIGN.md）=====================

# ---- 色彩 ----
C_BG = "#FFFFFF"          # 背景
C_TIME = "#00838F"        # 时间 · 专注青（品牌基线，对白底 4.52:1）
C_DATE = "#546E7A"        # 日期 · 次级文字（对白底 5.40:1）

# ---- 字号 ----
FS_CLOCK = 62             # 时钟主显
FS_DATE = 18              # 正文（日期行）

# ---- 画布与布局 ----
LAND_W, LAND_H = 320, 240   # 横屏逻辑画布
SAFE_MARGIN = 12            # 屏幕四边安全边距
CONTENT_W = LAND_W - 2 * SAFE_MARGIN   # 内容可用宽 = 296px
ROTATE = 270                # 逆时针旋转角度：90 / 270 互为 180° 翻转
TIME_CY = 98                # 时间行中心 y
DATE_CY = 170               # 日期行中心 y

# ---- 字体栈（按顺序回退，取第一个可用者；禁止 uming.ttc）----
FONT_CN = [
    "HYQiHei_50S.ttf",                                # unihiker 自带汉仪旗黑（优先）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",   # 文泉驿正黑
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",                      # 开发机预览用（微软雅黑）
    "C:/Windows/Fonts/simhei.ttf",
]
FONT_NUM = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
] + FONT_CN   # 数字字体缺失时退回中文字体，保证一定能画出来

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# =======================================================================


# ------------------------------ 字体 ------------------------------

def _resolve_font(paths):
    """按顺序找到第一个真实存在的字体文件；unihiker 自带的旗黑单独找一遍。"""
    for name in paths:
        if name == "HYQiHei_50S.ttf":
            try:
                import unihiker as _u
                cand = os.path.join(os.path.dirname(_u.__file__), name)
                if os.path.exists(cand):
                    return cand
            except Exception:
                pass
            cand = os.path.join(HERE, name)
            if os.path.exists(cand):
                return cand
            continue
        if os.path.exists(name):
            return name
    return None


FONT_CN_PATH = _resolve_font(FONT_CN)
FONT_NUM_PATH = _resolve_font(FONT_NUM)


def load_font(path, size):
    """加载字体；找不到字体文件时退回 Pillow 默认字体（并兼容 Pillow < 10）。"""
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)   # Pillow >= 10
    except TypeError:
        return ImageFont.load_default()       # Pillow < 10（板载 9.0.1）


# ------------------------------ 排版辅助 ------------------------------

def split_runs(text):
    """把字符串切成「数字段 / 中文段」交替的若干段，用于分段取字体。

    例："2026年10月13日 星期二" → ["2026"(数字), "年"(中文), "10"(数字), ...]
    数字与拉丁字符走 DejaVu，中文与空格走汉仪旗黑 —— 即 DESIGN.md 3.1 的「中英混排分段取字体」。
    """
    runs, buf, buf_is_num = [], "", None
    for ch in text:
        is_num = ord(ch) < 128 and not ch.isspace()
        if buf and is_num != buf_is_num:
            runs.append((buf, buf_is_num))
            buf = ""
        buf += ch
        buf_is_num = is_num
    if buf:
        runs.append((buf, buf_is_num))
    return runs


def draw_center_mixed(draw, cx, cy, text, font_cn, font_num, fill):
    """以 (cx, cy) 为中心绘制「中英混排」文字：数字走 font_num，其余走 font_cn。

    Pillow 没有 CSS 那样的整段字体回退，所以自己量宽、逐段贴上去。
    """
    runs = split_runs(text)
    boxes = []
    top, bottom = 0, 0
    for s, is_num in runs:
        f = font_num if is_num else font_cn
        l, t, r, b = draw.textbbox((0, 0), s, font=f)
        boxes.append((s, f, l, t, r))
        top = min(top, t)
        bottom = max(bottom, b)

    total = sum(r - l for (_s, _f, l, _t, r) in boxes)
    x = cx - total / 2.0
    y0 = cy - (top + bottom) / 2.0
    for s, f, l, t, r in boxes:
        draw.text((x - l, y0 - t), s, font=f, fill=fill)
        x += r - l


def _text_width(text, font):
    """量一段文字的宽度（优先 getlength，退化到 textlength，兼容 Pillow 8/9/10+）。"""
    try:
        return font.getlength(text)
    except Exception:
        img = Image.new("RGB", (1, 1))
        return ImageDraw.Draw(img).textlength(text, font=font)


def fit_size(text, path, max_size, max_width):
    """在不超过 max_width 的前提下，取 ≤ max_size 的最大字号。

    为什么需要它：DESIGN.md 把时钟定为 62px（按 DejaVuSans-Bold 计），但中文字体
    （汉仪旗黑 / 微软雅黑）的数字更窄。同一段 "HH:MM:SS" 在不同字体下宽度能差 20px，
    62px 的 DejaVu 粗体实测约 302px > 296px 内容宽 —— 会顶到屏幕边缘。
    这里先按线性比例估一个字号，再逐 px 收敛，保证任何字体下都不溢出。
    """
    w = _text_width(text, load_font(path, max_size))
    if w <= max_width or w <= 0:
        return max_size
    size = max(12, int(max_size * max_width / w))
    while size > 12 and _text_width(text, load_font(path, size)) > max_width:
        size -= 1
    return size


# ------------------------------ 渲染器 ------------------------------

class ClockRenderer(object):
    """把「某个时刻」渲染成一张可直接贴屏的 PIL 图像。

    画布、画布句柄、各字号字体只创建一次并复用 —— 这是板上不卡的关键。
    """

    def __init__(self, rotate=ROTATE):
        self.rotate = rotate
        self._img = Image.new("RGB", (LAND_W, LAND_H), C_BG)
        self._draw = ImageDraw.Draw(self._img)
        # 时钟字号自动收缩到内容宽以内（DESIGN.md 的 62px 视为上限）
        self.clock_size = fit_size("00:00:00", FONT_NUM_PATH, FS_CLOCK, CONTENT_W)
        self._f_clock = load_font(FONT_NUM_PATH, self.clock_size)
        self._f_date_cn = load_font(FONT_CN_PATH, FS_DATE)
        self._f_date_num = load_font(FONT_NUM_PATH, FS_DATE)

    # ---- 横屏逻辑画布（320 × 240），未旋转 ----
    def render_landscape(self, now):
        d = self._draw
        d.rectangle([0, 0, LAND_W - 1, LAND_H - 1], fill=C_BG)   # 清屏（复用同一张画布）

        time_text = now.strftime("%H:%M:%S")
        date_text = "%04d年%02d月%02d日  %s" % (
            now.year, now.month, now.day, WEEKDAYS[now.weekday()])

        draw_center_mixed(d, LAND_W // 2, TIME_CY, time_text,
                          self._f_clock, self._f_clock, C_TIME)
        draw_center_mixed(d, LAND_W // 2, DATE_CY, date_text,
                          self._f_date_cn, self._f_date_num, C_DATE)
        return self._img

    # ---- 实际贴屏用（240 × 320），已按 self.rotate 旋转 ----
    def render(self, now):
        return self.render_landscape(now).rotate(self.rotate, expand=True)


# ------------------------------ 上板运行 ------------------------------

def create_gui():
    """创建 unihiker GUI。

    容错：unihiker 0.0.27 的 GUI.__init__ 里 Tk 主循环在子线程启动，而 check_alive()
    由主线程立即调用。首次运行若正好在装字体，Tk 线程还没进 mainloop()，主线程会抛
    RuntimeError: main thread is not in main loop。这里捕获并重试一次（此时字体已装好）。
    """
    from unihiker import GUI
    try:
        return GUI()
    except RuntimeError:
        time.sleep(0.5)
        return GUI()


def run_on_board(rotate=ROTATE):
    gui = create_gui()
    renderer = ClockRenderer(rotate)

    clock = gui.draw_image(x=0, y=0, image=renderer.render(datetime.now()))
    print("[step1] 时钟已启动，旋转 %d°，Ctrl+C 退出" % rotate)
    print("[step1] 当前时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        while True:
            clock.config(image=renderer.render(datetime.now()))
            # 睡到下一个整秒，避免同一秒内重复刷新
            time.sleep(max(0.05, 1.0 - datetime.now().microsecond / 1000000.0))
    except KeyboardInterrupt:
        print("\n[step1] 已退出")


# ------------------------------ 电脑预览 ------------------------------

def parse_at(text):
    """解析 --at；支持 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS'（后者用今天的日期）。"""
    if not text:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M:%S":
            today = datetime.now()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt
    raise SystemExit("--at 格式应为 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM:SS'")


def render_preview(path, when, rotate=ROTATE):
    """不连板子，渲染一帧存成 PNG。存的是旋转前的横屏逻辑画布（320×240），便于阅读。"""
    renderer = ClockRenderer(rotate)
    img = renderer.render_landscape(when)
    img.save(path)
    print("[step1] 预览已保存：%s（%d×%d，旋转前）" % (path, img.width, img.height))
    print("[step1] 时钟字号：%dpx（上限 %dpx）" % (renderer.clock_size, FS_CLOCK))
    print("[step1] 使用字体：中文=%s / 数字=%s" % (FONT_CN_PATH, FONT_NUM_PATH))


# ------------------------------ 入口 ------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="行空板 M10 智能番茄时钟 · 第 1 段：显示时间与日期")
    parser.add_argument("--preview", metavar="PNG",
                        help="不连板子，渲染一帧存成 PNG")
    parser.add_argument("--at", metavar="TIME",
                        help="配合 --preview 固定显示的时间，如 '2026-10-13 13:29:45'")
    parser.add_argument("--rotate", type=int, choices=(90, 270), default=ROTATE,
                        help="横屏方向，默认 270（与 90 互为 180°）")
    args = parser.parse_args(argv)

    if args.preview:
        render_preview(args.preview, parse_at(args.at), args.rotate)
    else:
        run_on_board(args.rotate)


if __name__ == "__main__":
    main()
