# -*- coding: utf-8 -*-
"""
行空板 M10 屏幕抓取（/dev/fb0 → PNG）
=====================================

用途：程序在板子上跑着，**不用按键**就能确认屏幕到底画成了什么。

板子的 framebuffer 是 240×320 / 16bpp（RGB565），Pillow 不能直接读，
这里手工把 RGB565 展开成 RGB 再存成 PNG。

用法（在板子上跑）：
    python3 grab_screen.py                    # 存到 ./screen.png
    python3 grab_screen.py -o /root/x.png

把图取回电脑：
    # 板子
    python3 -m http.server 8123 --bind 10.1.2.3 --directory /root/smarttomato
    # 电脑（--noproxy 很关键，否则可能被本机代理拦掉）
    curl --noproxy '*' http://10.1.2.3:8123/screen.png -o screen.png

⚠️ 抓下来的是**物理竖屏**的帧缓冲（240×320）。如果程序是横屏程序，
   看图时把图逆时针转 90°（PIL 里是 `im.rotate(90, expand=True)`）
   才是它正常的横屏排版。

参考：本项目 DESIGN.md 的「两个省事的验证手法」之一。
"""

import argparse

from PIL import Image

FB_PATH = "/dev/fb0"
FB_W, FB_H = 240, 320          # M10 物理竖屏


def grab(fb_path=FB_PATH, size=(FB_W, FB_H), out="screen.png"):
    w, h = size
    with open(fb_path, "rb") as f:
        raw = f.read()

    stride = w * 2             # RGB565：每像素 2 字节
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        base = y * stride
        for x in range(w):
            i = base + x * 2
            v = raw[i] | (raw[i + 1] << 8)          # 小端：低字节在前
            px[x, y] = (((v >> 11) & 0x1F) << 3,    # R 5bit → 8bit
                        ((v >> 5) & 0x3F) << 2,     # G 6bit → 8bit
                        (v & 0x1F) << 3)            # B 5bit → 8bit
    img.save(out)
    return img


def main():
    ap = argparse.ArgumentParser(description="行空板 M10 抓屏（/dev/fb0 → PNG）")
    ap.add_argument("-o", "--out", default="screen.png", help="输出 PNG 路径")
    ap.add_argument("--fb", default=FB_PATH, help="framebuffer 设备，默认 /dev/fb0")
    ap.add_argument("-W", "--width", type=int, default=FB_W)
    ap.add_argument("-H", "--height", type=int, default=FB_H)
    args = ap.parse_args()

    img = grab(args.fb, (args.width, args.height), args.out)
    print("[grab] 已保存 %s（%d×%d，物理竖屏；横屏程序请逆时针转 90° 查看）"
          % (args.out, img.width, img.height))


if __name__ == "__main__":
    main()
