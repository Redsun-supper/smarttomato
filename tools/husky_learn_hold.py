# -*- coding: utf-8 -*-
"""二哈「物体分类」学手机 —— 常驻脚本（官方库写法 + M10 屏幕状态显示）。

官方接口（pinpong.libs.dfrobot_huskylens）：

    Board().begin()
    hl = Huskylens(i2c_addr=0x32, bus_num=0)

    hl.command_request_algorthim("ALGORITHM_OBJECT_CLASSIFICATION")  # 切算法，等约 5 秒
    hl.command_request_learn_once(1)                # 把镜头前这一刻学成 ID 1
    hl.command_request_blocks()                     # 请求识别结果
    hl.read_count("blocks")                         # 画面里检测到几个目标
    hl.read_learned_id_count()                      # 模块上已学几个 ID
    hl.command_request_save_model_to_SD_card(0)     # 存模型到 SD 卡槽 0
    hl.command_request_load_model_from_SD_card(0)   # 从 SD 卡槽 0 载入
    hl.command_request_forget()                     # 清空已学内容

二哈模块上的学习键在模块本地，I2C 读不到，所以用 M10 板载 A/B 键：
    /dev/input/event2（keyboard_ab），A = 30（KEY_A），B = 48（KEY_B）

M10 屏幕（240×320，unihiker.GUI + Pillow）实时显示，方便判断学得对不对：

    学手机 · ID 1
        学习中              ← 学习中青色 / 待命灰色，顶部有青色进度条
          12                ← 本次已学张数（大字）
        本次已学（张）
    ● 看到目标              ← 二哈检测到目标时亮绿，没检测到是灰色空心圆
    模块已学 ID：1
    A 开始/结束   B 学一张

用法（板上 /root/smarttomato 下）：
    python3 -u husky_learn_hold.py              # 常驻：按 A 启停，按 B 学一张
    python3 -u husky_learn_hold.py --check 10   # 连读 10 秒看识别结果
    python3 -u husky_learn_hold.py --save 0     # 存模型到 SD 卡槽 0
    python3 -u husky_learn_hold.py --load 0     # 从 SD 卡槽 0 载入
    python3 -u husky_learn_hold.py --forget     # 清空已学内容
    python3 -u husky_learn_hold.py --no-switch  # 模块已在物体分类时跳过切算法
    python3 -u husky_learn_hold.py --no-screen  # 不接管屏幕，只打日志

⚠️ 退出请用 Ctrl+C 或 systemctl stop：脚本走 os._exit()，避免 pinpong 退出时
   复位 gd32v 触发内核 panic（2026-10-17 实测）。
"""
import argparse
import os
import select
import signal
import struct
import sys
import time

from pinpong.board import Board
from pinpong.libs.dfrobot_huskylens import Huskylens

EVENT_DEV = "/dev/input/event2"
EV_KEY, KEY_A, KEY_B = 1, 30, 48
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

ALGO = "ALGORITHM_OBJECT_CLASSIFICATION"
ALGO_WAIT = 5
READ_SEC = 1.0                  # 每秒读一次识别结果（刷新屏幕用）

# 屏幕
SCR_W, SCR_H = 240, 320
C_BG, C_INK, C_SUB = "#FFFFFF", "#1F2933", "#5B6B73"
C_TEAL, C_GREEN, C_GREY = "#00838F", "#2E7D32", "#B0BEC5"

FONT_CN = [
    "/usr/share/fonts/truetype/HYQH/HYQiHei_50S.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def hard_exit(code=0):
    """退出走 os._exit：跳过解释器清理，避免 pinpong 复位 gd32v 导致内核 panic。"""
    sys.stdout.flush()
    os._exit(code)


class Panel(object):
    """M10 屏幕状态面板：只在内容变化时重画，不空转刷屏。"""

    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont
        self._Font = ImageFont
        here = os.path.dirname(os.path.abspath(__file__))
        self._path = next((p for p in ([here + "/HYQiHei_50S.ttf"] + FONT_CN)
                           if os.path.exists(p)), None)
        self._cache = {}
        self.pil = Image.new("RGB", (SCR_W, SCR_H), C_BG)
        self.d = ImageDraw.Draw(self.pil)
        self.item = None
        self._last = None
        try:
            from unihiker import GUI
            self.item = GUI().draw_image(x=0, y=0, image=self.pil)
            print("[屏幕] 已接管 240×320 显示")
        except Exception as e:
            print("[屏幕] 初始化失败（%s），只在日志里输出" % e)

    def _font(self, size):
        if size not in self._cache:
            self._cache[size] = (self._Font.truetype(self._path, size)
                                 if self._path else self._Font.load_default())
        return self._cache[size]

    def _text(self, cx, cy, s, size, fill):
        f = self._font(size)
        x0, y0, x1, y1 = self.d.textbbox((0, 0), s, font=f)
        self.d.text((cx - (x1 - x0) / 2.0 - x0, cy - (y1 - y0) / 2.0 - y0),
                    s, font=f, fill=fill)

    def show(self, cid, learning, count, boxes, learned):
        key = (cid, learning, count, boxes > 0, learned)
        if key == self._last:
            return
        self._last = key
        d = self.d
        d.rectangle([0, 0, SCR_W - 1, SCR_H - 1], fill=C_BG)
        if learning:                                  # 顶部青色条 = 正在学
            d.rectangle([0, 0, SCR_W - 1, 6], fill=C_TEAL)

        self._text(SCR_W / 2.0, 26, "学手机 · ID %d" % cid, 16, C_SUB)
        self._text(SCR_W / 2.0, 80, "学习中" if learning else "待命", 34,
                   C_TEAL if learning else C_GREY)
        self._text(SCR_W / 2.0, 150, "%d" % count, 72, C_INK)
        self._text(SCR_W / 2.0, 202, "本次已学（张）", 14, C_SUB)

        if boxes > 0:
            self._text(SCR_W / 2.0, 240, "● 看到目标", 18, C_GREEN)
        else:
            self._text(SCR_W / 2.0, 240, "○ 没看到目标", 18, C_GREY)

        self._text(SCR_W / 2.0, 270, "模块上报已学：%d" % learned, 14, C_SUB)
        self._text(SCR_W / 2.0, 300, "A 开始/结束    B 学一张", 12, C_SUB)

        if self.item is not None:
            try:
                self.item.config(image=self.pil)
            except Exception:
                pass


def open_husky(switch=True):
    Board().begin()
    hl = Huskylens(i2c_addr=0x32, bus_num=0)
    if switch:
        print("切到「物体分类」，等 %d 秒…" % ALGO_WAIT)
        hl.command_request_algorthim(ALGO)
        time.sleep(ALGO_WAIT)
    return hl


def read(hl):
    """读一次识别结果 → (检测框数, 已学 ID 数)。read_count 内部已发请求。"""
    return hl.read_count("blocks"), hl.read_learned_id_count()


def poll_key(fd):
    """非阻塞读一个按键事件，是「按下」就返回 code。"""
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return None
    data = os.read(fd, EVENT_SIZE)
    if len(data) < EVENT_SIZE:
        return None
    _, _, type_, code, value = struct.unpack(EVENT_FORMAT, data)
    return code if (type_ == EV_KEY and value == 1) else None


def main():
    ap = argparse.ArgumentParser(description="二哈物体分类学手机（常驻）")
    ap.add_argument("--id", type=int, default=1, help="要学的类别 ID（默认 1）")
    ap.add_argument("--check", type=float, metavar="秒", help="连读若干秒看识别结果")
    ap.add_argument("--save", type=int, metavar="槽", help="模型存到 SD 卡槽（0~4）")
    ap.add_argument("--load", type=int, metavar="槽", help="从 SD 卡槽载入模型")
    ap.add_argument("--forget", action="store_true", help="清空已学内容")
    ap.add_argument("--no-switch", action="store_true", help="不切算法")
    ap.add_argument("--no-screen", action="store_true", help="不接管屏幕")
    args = ap.parse_args()

    hl = open_husky(switch=not args.no_switch)

    if args.save is not None:
        hl.command_request_save_model_to_SD_card(args.save)
        time.sleep(3)
        print("已发送保存指令到槽 %d（无回执，拔卡插电脑确认）" % args.save)
        hard_exit(0)

    if args.load is not None:
        hl.command_request_load_model_from_SD_card(args.load)
        time.sleep(3)
        print("已从槽 %d 载入（无回执，用 --check 验证）" % args.load)
        hard_exit(0)

    if args.forget:
        hl.command_request_forget()
        time.sleep(1)
        print("已清空；当前已学 ID 数：%d" % hl.read_learned_id_count())
        hard_exit(0)

    panel = None if args.no_screen else Panel()

    if args.check:
        print("连读 %.0f 秒（Ctrl+C 结束）" % args.check)
        t_end = time.time() + args.check
        while time.time() < t_end:
            n, learned = read(hl)
            print("  框数=%d  已学ID=%d" % (n, learned))
            if panel:
                panel.show(args.id, False, 0, n, learned)
            time.sleep(1)
        hard_exit(0)

    # ---- 常驻：按 A 启停，按 B 学一张 ----
    fd = os.open(EVENT_DEV, os.O_RDONLY)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: hard_exit(0))

    print("常驻就绪：按 A 开始 / 结束，按 B 学一张到 ID %d" % args.id)
    learning = False
    count = 0
    boxes, learned = 0, 0
    t_read = 0.0
    last_state = None
    while True:
        code = poll_key(fd)
        if code == KEY_A:
            learning = not learning
            print("▶ 学习模式开启" if learning else "■ 学习模式关闭，本次 %d 张" % count)
        elif code == KEY_B:
            if not learning:
                print("  先按 A 键开启学习模式")
            else:
                hl.command_request_learn_once(args.id)
                time.sleep(0.4)      # 给模块消化的时间，避免连按积压
                count += 1
                print("  [%d] 已学 → ID %d" % (count, args.id))

        now = time.time()
        if now - t_read >= READ_SEC:
            t_read = now
            boxes, learned_now = read(hl)
            # 模块在后台重训时「已学 ID 数」会瞬间回 0，显示上取本次会话的最大值，
            # 免得屏幕在 1 / 0 之间跳（只有 --forget 才会真的清掉，那是另一个进程）。
            learned = max(learned, learned_now)
            if (learning, boxes > 0) != last_state:
                last_state = (learning, boxes > 0)
                print("· %s｜框数=%d｜已学ID=%d｜本次 %d 张"
                      % ("学习中" if learning else "待命", boxes, learned, count))

        if panel:
            panel.show(args.id, learning, count, boxes, learned)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("出错：%s" % e)
        hard_exit(1)
