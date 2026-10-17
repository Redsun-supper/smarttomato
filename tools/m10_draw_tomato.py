# -*- coding: utf-8 -*-
"""行空板 M10 屏幕绘图 —— 动态番茄时钟（smarttomato 主题）。

用途：
  1. 屏幕健康自检（连续 panic 后验证显示链路完好）；
  2. 项目主题画面：番茄造型 + 表盘 + 实时指针 + 数字时间。

实现要点（沿用 m10_step3_away_last.py 的板上实测结论）：
  - 逻辑画布 320×240 横屏，`GUI().config(image=PIL Image)` 直接贴屏；
  - 静态部分（番茄/表盘/刻度/文字/日期）只渲染一次，每秒只重画指针
    和数字时间（config 贴屏 ~42ms，1fps 足够，CPU 占用极低）；
  - 纯 unihiker + Pillow，**不碰 I2C / pinpong** → 进程退出干净，
    不会触发 pinpong 的 config_close → gd32v 复位 → 内核 panic 链。

用法（板上）：
    python3 -u m10_draw_tomato.py                # 常驻动画，Ctrl+C 退出
    python3 -u m10_draw_tomato.py --seconds 60   # 60 秒后自动退出
    python3 -u m10_draw_tomato.py --preview out.png   # 本机渲染一帧到 PNG（不上板）
"""
import argparse
import math
import os
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# PIL 9（板上）与 PIL 10+（本机）的 Transpose 枚举位置不同
try:
    from PIL.Image import Transpose
    _ROT270 = Transpose.ROTATE_270
except ImportError:
    _ROT270 = Image.ROTATE_270

W, H = 320, 240                       # 横屏逻辑画布
C_BG = (255, 255, 255)
C_TOMATO = (211, 47, 47)              # 番茄红
C_TOMATO_D = (142, 0, 0)              # 番茄描边
C_HILITE = (239, 154, 154)            # 高光
C_HILITE2 = (255, 205, 210)           # 高光核心
C_LEAF = (46, 125, 50)                # 叶子深绿
C_LEAF_L = (67, 160, 71)              # 叶子亮绿
C_STEM = (93, 64, 55)                 # 蒂
C_FACE = (255, 255, 255)              # 表盘
C_FACE_E = (215, 204, 200)            # 表盘描边
C_TICK = (55, 71, 79)                 # 刻度
C_HAND = (38, 50, 56)                 # 时针/分针
C_SEC = (194, 24, 91)                 # 秒针（项目强提醒品红）
C_TEAL = (0, 131, 143)                # 主色深青
C_SUB = (91, 107, 115)                # 次要文字
C_INK = (31, 41, 51)                  # 主要文字

FONT_NUM = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_CN = [
    "HYQiHei_50S.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
]


def _find(cands):
    here = os.path.dirname(os.path.abspath(__file__))
    for p in cands:
        if os.path.exists(p):
            return p
        q = os.path.join(here, p)
        if os.path.exists(q):
            return q
    return None


def font(cands, size):
    p = _find(cands)
    if p:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------- 静态底图

def draw_tomato(d, cx, cy, r):
    """番茄本体：红圆 + 高光 + 描边，顶部留出放叶子的空间。"""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=C_TOMATO,
              outline=C_TOMATO_D, width=3)
    # 高光：左上两块椭圆（Pillow 无渐变，用叠椭圆模拟光泽）
    d.ellipse([cx - r * 0.62, cy - r * 0.55, cx - r * 0.18, cy - r * 0.05],
              fill=C_HILITE)
    d.ellipse([cx - r * 0.52, cy - r * 0.47, cx - r * 0.30, cy - r * 0.22],
              fill=C_HILITE2)
    # 底部暗弧：加一点体积感
    d.arc([cx - r * 0.9, cy - r * 0.9, cx + r * 0.9, cy + r * 0.9],
          start=30, end=150, fill=C_TOMATO_D, width=3)


def draw_leaves(d, cx, cy, r):
    """蒂 + 放射状叶子（五瓣）。"""
    # 蒂（小棕色圆角条）
    d.rounded_rectangle([cx - 4, cy - r - 12, cx + 4, cy - r + 2],
                        radius=3, fill=C_STEM)
    # 花萼底座
    d.ellipse([cx - 10, cy - r - 4, cx + 10, cy - r + 8], fill=C_LEAF)
    # 五片叶子
    tips = [(-1, -0.55), (-0.95, 0.1), (0.95, 0.1), (1, -0.55), (0, -1)]
    for fx, fy in tips:
        lx, ly = cx + fx * r * 0.72, cy - r - 6 + fy * r * 0.42
        # 叶子 = 两个三角形拼的梭形
        d.polygon([(cx, cy - r - 2),
                   (lx - 6 * fy, ly + 6 * fx),
                   (lx, ly),
                   (lx + 6 * fy, ly - 6 * fx)],
                  fill=C_LEAF_L if fx == 0 else C_LEAF)


def draw_face(d, cx, cy, r):
    """表盘：白圆 + 12 刻度（整点加粗加长）+ 4 个数字。"""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=C_FACE,
              outline=C_FACE_E, width=2)
    for i in range(12):
        a = math.radians(i * 30 - 90)
        major = (i % 3 == 0)
        r1, r2 = (r - 10, r - 2) if major else (r - 6, r - 2)
        x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
        x2, y2 = cx + r2 * math.cos(a), cy + r2 * math.sin(a)
        d.line([(x1, y1), (x2, y2)], fill=C_TICK, width=3 if major else 2)
    # 12/3/6/9 数字
    f = font(FONT_NUM, 13)
    pos = {0: (cx, cy - r + 14), 3: (cx + r - 15, cy), 6: (cx, cy + r - 20),
           9: (cx - r + 15, cy)}
    anchors = {0: "mm", 3: "mm", 6: "mm", 9: "mm"}
    for k, (tx, ty) in pos.items():
        d.text((tx, ty), str(k if k else 12), font=f, fill=C_TICK,
               anchor=anchors[k])


def build_base():
    """静态底图：番茄 + 表盘 + 右侧文字栏。"""
    img = Image.new("RGB", (W, H), C_BG)
    d = ImageDraw.Draw(img)
    cx, cy, r = 102, 122, 78

    draw_tomato(d, cx, cy, r)
    draw_leaves(d, cx, cy, r)
    draw_face(d, cx, cy, 60)

    # 右侧文字栏
    x0 = 196
    f_name = font(FONT_NUM, 23)
    d.text((x0, 46), "smart", font=f_name, fill=C_TEAL, anchor="lm")
    d.text((x0, 74), "tomato", font=f_name, fill=C_TEAL, anchor="lm")
    d.line([(x0, 90), (x0 + 100, 90)], fill=C_TEAL, width=2)

    # 日期（静态，当天不变）
    now = datetime.now()
    f_date = font(FONT_CN, 14)
    f_week = font(FONT_CN, 12)
    d.text((x0, 186), "%d月%d日" % (now.month, now.day),
           font=f_date, fill=C_SUB, anchor="lm")
    d.text((x0, 206), "星期" + "一二三四五六日"[now.weekday()],
           font=f_week, fill=C_SUB, anchor="lm")
    # 角落小字
    f_tag = font(FONT_CN, 12)
    d.text((x0, 108), "智能番茄时钟", font=f_tag, fill=C_SUB, anchor="lm")
    return img, (cx, cy)


# ---------------------------------------------------------------- 每帧

def draw_frame(base, center):
    """在底图上画指针 + 数字时间，返回新图。"""
    img = base.copy()
    d = ImageDraw.Draw(img)
    cx, cy = center
    now = datetime.now()
    h, m, s = now.hour, now.minute, now.second

    # 指针角度：12 点方向 = -90°
    def tip(angle_deg, length):
        a = math.radians(angle_deg - 90)
        return cx + length * math.cos(a), cy + length * math.sin(a)

    a_h = (h % 12 + m / 60.0) * 30
    a_m = (m + s / 60.0) * 6
    a_s = s * 6

    # 时针（粗短）
    d.line([(cx, cy), tip(a_h, 26)], fill=C_HAND, width=6)
    # 分针（细长）
    d.line([(cx, cy), tip(a_m, 40)], fill=C_HAND, width=4)
    # 秒针（细 + 配重尾巴）
    tx, ty = tip(a_s, 48)
    bx, by = tip(a_s + 180, 10)
    d.line([(bx, by), (tx, ty)], fill=C_SEC, width=2)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=C_SEC)

    # 右侧数字时间
    x0 = 196
    f_time = font(FONT_NUM, 30)
    f_sec = font(FONT_NUM, 16)
    d.text((x0, 138), "%02d:%02d" % (h, m), font=f_time, fill=C_INK,
           anchor="lm")
    sw = d.textlength(":%02d" % s, font=f_sec)
    d.text((x0 + d.textlength("%02d:%02d" % (h, m), font=f_time) + 6, 142),
           ":%02d" % s, font=f_sec, fill=C_SEC, anchor="lm")
    return img


# ---------------------------------------------------------------- 入口

def preview(path):
    base, center = build_base()
    img = draw_frame(base, center)
    img.save(path)
    print("已保存预览帧：%s（%dx%d）" % (path, img.width, img.height))


def run(seconds=None):
    from unihiker import GUI

    # unihiker 0.0.27 首次初始化偶发 RuntimeError（Tk 线程竞态），重试一次
    gui = None
    for _ in range(3):
        try:
            gui = GUI()
            break
        except Exception:
            time.sleep(1)
    if gui is None:
        print("GUI 初始化失败")
        return 1

    base, center = build_base()
    # 先用 draw_image 在 (0,0) 铺上首帧，拿到图像控件；
    # 之后每帧用控件的 config(image=) 更新（unihiker 的 GUI 对象本身没有 config）
    clock = gui.draw_image(x=0, y=0,
                           image=draw_frame(base, center).transpose(_ROT270))
    print("番茄时钟已上屏（320x240）。Ctrl+C 退出；%s"
          % ("%d 秒后自动退出" % seconds if seconds else "常驻运行"))
    t_end = time.time() + seconds if seconds else None
    try:
        while True:
            # 横屏逻辑画布 → 物理竖屏（与 m10_step3_away_last.py 的 rotate_out 一致）
            clock.config(image=draw_frame(base, center).transpose(_ROT270))
            if t_end and time.time() >= t_end:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n（已退出）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="行空板屏幕绘图：动态番茄时钟")
    ap.add_argument("--preview", metavar="PNG",
                    help="只在本机渲染一帧存为 PNG，不上板")
    ap.add_argument("--seconds", type=float, metavar="秒",
                    help="运行 N 秒后自动退出（默认常驻）")
    args = ap.parse_args()

    if args.preview:
        preview(args.preview)
        return 0
    return run(args.seconds)


if __name__ == "__main__":
    sys.exit(main())
