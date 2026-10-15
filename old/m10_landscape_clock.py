# -*- coding: utf-8 -*-
"""
行空板 M10（UNIHIKER M10）横屏时钟
=================================

效果：把 M10 横过来（长边朝上），全屏显示一个横屏的大号数字时钟 + 日期。

原理：
    M10 的物理屏幕是 240(宽) x 320(高) 的竖屏。unihiker 库的 GUI 没有提供
    整屏旋转接口，所以这里采用 DFRobot 官方推荐的思路 —— 先在「横屏逻辑画布
    320 x 240」上正常排版，画完后用 PIL 把整张图旋转 90 度，再整屏贴到屏幕上。
    这样坐标就仍然按横屏直觉来写，不需要做任何坐标换算。

运行环境：
    Mind+ 内置 Python 3.11.9（已含 unihiker 0.0.27 + Pillow），或行空板 M10 板载
    Python 3.7.3 + Pillow 9.0.1。代码已对 Pillow < 10 做兼容，板上可直接运行。

放置位置：
    Mind+ → Python 积木模式 → 项目「新建项目」→ 该文件即项目下的 main.py
"""

import os
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from unihiker import GUI

# ============================ 可调参数 ============================

SCREEN_W, SCREEN_H = 240, 320          # M10 竖屏物理分辨率
LAND_W, LAND_H = SCREEN_H, SCREEN_W    # 横屏逻辑画布：320 x 240

# 逆时针旋转角度（在横屏逻辑画布基础上整屏旋转）：
#   90  = 横屏方向 A
#   270 = 在方向 A 的基础上再旋转 180°（等价于 -90，横屏方向 B）
ROTATE = 270

BG_COLOR = "#FFFFFF"                   # 背景色（白色）
TIME_COLOR = "#00838F"                 # 时间颜色（深青；深色主题原值 #00E5FF）
DATE_COLOR = "#546E7A"                 # 日期颜色（深灰蓝；深色主题原值 #7A8794）
TIME_SIZE = 62                         # 时间字号
DATE_SIZE = 18                         # 日期字号

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# ==================================================================


def find_font():
    """定位中文字体：优先使用 unihiker 库自带的汉仪旗黑，避免额外拷贝字体文件。"""
    candidates = []
    try:
        import unihiker as _unihiker
        candidates.append(os.path.join(os.path.dirname(_unihiker.__file__), "HYQiHei_50S.ttf"))
    except Exception:
        pass
    # 兜底：当前目录 / 系统常见字体（含中文，适配行空板 Debian 10 / Pillow 9.x）
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


def make_font(size):
    if FONT_PATH:
        return ImageFont.truetype(FONT_PATH, size)
    # 无字体文件兜底：Pillow < 10（如板载 9.0.1）的 load_default 不接受 size 参数，需兼容
    try:
        return ImageFont.load_default(size)      # Pillow >= 10
    except TypeError:
        return ImageFont.load_default()          # Pillow < 10 回退到默认位图字体


def draw_center(draw, cx, cy, text, font, fill):
    """以 (cx, cy) 为中心绘制文字。"""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (left + right) / 2.0, cy - (top + bottom) / 2.0),
              text, font=font, fill=fill)


def render(now):
    """在横屏逻辑坐标系（320 x 240）里画一帧，再旋转成 240 x 320 返回。"""
    img = Image.new("RGB", (LAND_W, LAND_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    time_text = now.strftime("%H:%M:%S")
    date_text = "%04d年%02d月%02d日  %s" % (
        now.year, now.month, now.day, WEEKDAYS[now.weekday()])

    draw_center(draw, LAND_W // 2, 95, time_text, make_font(TIME_SIZE), TIME_COLOR)
    draw_center(draw, LAND_W // 2, 172, date_text, make_font(DATE_SIZE), DATE_COLOR)

    # 关键一步：整图逆时针旋转 90 度，expand=True 让画布自动变成 240 x 320
    return img.rotate(ROTATE, expand=True)


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


def main():
    gui = create_gui()
    clock = gui.draw_image(x=0, y=0, image=render(datetime.now()))
    print("M10 横屏时钟已启动")

    try:
        while True:
            now = datetime.now()
            clock.config(image=render(now))
            # 对齐到下一个整秒，避免同秒内重复刷新
            time.sleep(max(0.2, 1.0 - now.microsecond / 1000000.0))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
