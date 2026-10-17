# -*- coding: utf-8 -*-
"""二哈「物体分类」一次性工具 —— 自定义协议版（快读 / 起名 / 存载模型 / 核查）。

为什么这个文件用自定义协议，而 husky_learn_hold.py 用官方库：
  1. 官方库 `_read_response()` 是 `while True` 逐字节找 0x55，**没有超时** ——
     模块不回包时进程永久卡死（看着像死机），而且一次响应要 51.3ms。
     这里「发请求 + 一次读 64 字节」只要 3.4ms，结论完全一致，还能连读。
  2. 官方库 `command_request_customnames()` 用 `ord(char)` 逐字符取码，
     中文（如「手机」）会得到 >255 的值 → 直接抛 ValueError。这里按 UTF-8 字节发。

协议帧（DFRobot HuskyLens I2C 协议）：
    55 AA 11 <数据长度> <命令> <数据...> <校验和>，校验和 = 前面所有字节和 % 256
    0x2D 切算法 / 0x36 学一张 / 0x2F 起名 / 0x32 存模型 / 0x33 载模型 / 0x37 清空
    0x21 请求检测框 → 响应 0x29 RETURN_INFO(16B) + 每个框一个 0x2A RETURN_BLOCK(16B)

用法（板上 /root/smarttomato 下，加 -u 看实时输出）：
    python3 -u husky_learn_phone.py --check 5        # 切物体分类并连读 5 秒
    python3 -u husky_learn_phone.py --learn 1        # 学一张到 ID 1
    python3 -u husky_learn_phone.py --burst 1 30     # 连学 30 张到 ID 1（期间动起来！）
    python3 -u husky_learn_phone.py --name 1 手机     # 起名（模块屏幕显示用，支持中文）
    python3 -u husky_learn_phone.py --save 0         # 存 SD 卡槽 0（卡须 FAT32）
    python3 -u husky_learn_phone.py --load 0         # 从 SD 卡槽 0 载入
    python3 -u husky_learn_phone.py --forget         # 清空全部已学内容

⚠️ 跑前先停掉占 I2C 的进程（主程序 / husky-learn 服务），同一时刻只能有一个。
⚠️ 退出走 os._exit()，避免 pinpong 退出时复位 gd32v 触发内核 panic。
"""
import argparse
import os
import sys
import time

HL_ADDR = 0x32
HL_READ_LEN = 64            # RETURN_INFO(16B) + 最多 3 个 RETURN_BLOCK(16B)
ALGO_OBJ_CLASSIFICATION = 0x06
ALGO_WAIT = 5


def hard_exit(code=0):
    """退出走 os._exit：跳过解释器清理，避免 pinpong 复位 gd32v 导致内核 panic。"""
    sys.stdout.flush()
    os._exit(code)


def frame(cmd, data=None):
    """拼一帧：55 AA 11 <len> <cmd> <data...> <checksum>。"""
    data = list(data or [])
    body = [0x55, 0xAA, 0x11, len(data), cmd] + data
    body.append(sum(body) % 256)
    return body


def u16(buf, i):
    return buf[i] | (buf[i + 1] << 8)


def parse_response(raw):
    """拆「请求 blocks」的响应 → (框数, 已学ID数, [(id, cx, cy, w, h), ...])。

    响应是两段：0x29 RETURN_INFO（data[0..1]=框数、[2..3]=已学ID数、[4..5]=帧号）
    和每个框一个 0x2A RETURN_BLOCK（xCenter yCenter width height ID）。
    物体分类不输出坐标（框固定在屏幕中央），但 ID 在 RETURN_BLOCK 里是真的。
    包不合法时框数返回 None（只代表这次读失败）。
    """
    if not raw or len(raw) < 16 or raw[0] != 0x55 or raw[1] != 0xAA:
        return None, None, []
    data = raw[5:5 + raw[3]]
    if len(data) < 6:
        return None, None, []
    n_box, n_learned = u16(data, 0), u16(data, 2)
    blocks, pos = [], 16
    while pos + 5 <= len(raw) and len(blocks) < n_box:
        if raw[pos] != 0x55 or raw[pos + 1] != 0xAA:
            break
        dlen = raw[pos + 3]
        end = pos + 5 + dlen + 1
        if end > len(raw):
            break
        if raw[pos + 4] == 0x2A and dlen >= 10:
            d = raw[pos + 5:pos + 5 + dlen]
            blocks.append((u16(d, 8), u16(d, 0), u16(d, 2), u16(d, 4), u16(d, 6)))
        pos = end
    return n_box, n_learned, blocks


class Husky(object):
    """薄封装：命令自己拼帧发，结果一次读 64 字节（3.4ms），失败只重试有限次。"""

    def __init__(self):
        from pinpong.board import Board
        Board().begin()
        from pinpong.board import I2C
        self.i2c = I2C(bus_num=0)      # UNIHIKER 上映射到 /dev/i2c-4

    def send(self, cmd, retries=3, gap=0.2):
        for _ in range(retries):
            try:
                self.i2c.writeto(HL_ADDR, cmd)
                return True
            except Exception:
                time.sleep(gap)
        print("  ✗ I2C 发送失败（检查模块连线 / 是否有别的进程占着 I2C）")
        return False

    def read_blocks(self, retries=3, delay=0.15):
        """读一次识别结果；失败返回 (None, None, [])。"""
        for _ in range(retries):
            try:
                self.i2c.writeto(HL_ADDR, [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31])
                time.sleep(0.05)
                raw = self.i2c.readfrom_mem(HL_ADDR, 0, HL_READ_LEN)
            except Exception:
                raw = None
            if raw:
                n, learned, blocks = parse_response(raw)
                if n is not None:
                    return n, learned, blocks
            time.sleep(delay)
        return None, None, []

    def switch(self):
        if not self.send(frame(0x2D, [ALGO_OBJ_CLASSIFICATION, 0x00])):
            return False
        time.sleep(ALGO_WAIT)
        self.read_blocks()             # 清一次缓冲，避免读到上一个算法的残留
        return True

    def learn_once(self, cid):
        return self.send(frame(0x36, [cid & 0xFF, (cid >> 8) & 0xFF]))

    def save_model(self, slot):
        return self.send(frame(0x32, [slot & 0xFF, (slot >> 8) & 0xFF]))

    def load_model(self, slot):
        return self.send(frame(0x33, [slot & 0xFF, (slot >> 8) & 0xFF]))

    def forget(self):
        return self.send(frame(0x37))

    def set_name(self, cid, name):
        """起名：数据 = [ID, 名称字节数+1, 名称(UTF-8)..., 0x00]。"""
        nb = bytearray(name.encode("utf-8"))
        return self.send(frame(0x2F, [cid & 0xFF, len(nb) + 1] + list(nb) + [0x00]))


def main():
    ap = argparse.ArgumentParser(description="二哈物体分类一次性工具（自定义协议）")
    ap.add_argument("--check", type=float, metavar="秒", help="连读若干秒看识别结果")
    ap.add_argument("--learn", type=int, metavar="ID", help="学一张到指定 ID")
    ap.add_argument("--burst", nargs=2, type=int, metavar=("ID", "张数"),
                    help="连学多张到同一个 ID")
    ap.add_argument("--interval", type=float, default=1.0, metavar="秒",
                    help="--burst 每张的间隔（默认 1.0 秒）")
    ap.add_argument("--name", nargs=2, metavar=("ID", "名称"), help="给类别起名")
    ap.add_argument("--save", type=int, metavar="槽", help="模型存到 SD 卡槽")
    ap.add_argument("--load", type=int, metavar="槽", help="从 SD 卡槽载入模型")
    ap.add_argument("--forget", action="store_true", help="清空已学内容")
    ap.add_argument("--no-switch", action="store_true", help="不切算法")
    args = ap.parse_args()

    if not any([args.check, args.learn, args.burst, args.name,
                args.save is not None, args.load is not None, args.forget]):
        ap.print_help()
        hard_exit(2)

    try:
        h = Husky()
    except Exception as e:
        print("二哈初始化失败：%s" % e)
        print("→ 确认：主程序是不是还占着 I2C？二哈 TF 卡是不是非 FAT32？")
        hard_exit(1)

    if not args.no_switch:
        print("切到「物体分类」，等 %d 秒…" % ALGO_WAIT)
        h.switch()

    if args.check:
        print("连读 %.0f 秒（Ctrl+C 结束）" % args.check)
        t_end = time.time() + args.check
        prev = None
        while time.time() < t_end:
            n, learned, blocks = h.read_blocks()
            cur = (n, learned, tuple(blocks))
            if cur != prev:
                print("  框数=%s  已学ID=%s  %s" % (n, learned, blocks or "（无检测框）"))
                prev = cur
            time.sleep(0.2)

    if args.learn:
        h.learn_once(args.learn)
        time.sleep(1)
        _, learned, _ = h.read_blocks()
        print("已学一张 → ID %d；当前已学 ID 数：%s" % (args.learn, learned))

    if args.burst:
        cid, n = args.burst
        print("连学 %d 张到 ID %d，每 %.1f 秒一张" % (n, cid, args.interval))
        print("⚠️ 期间不断换 App / 换角度 / 换光线，静止学 30 张 = 1 张")
        for i in range(n):
            h.learn_once(cid)
            time.sleep(0.4 + args.interval)
            print("  [%d/%d] 已学" % (i + 1, n))
        _, learned, _ = h.read_blocks()
        print("当前已学 ID 数：%s" % learned)

    if args.name:
        cid, name = int(args.name[0]), args.name[1]
        h.set_name(cid, name)
        time.sleep(0.5)
        print("已把 ID %d 命名为「%s」" % (cid, name))

    if args.save is not None:
        h.save_model(args.save)
        time.sleep(3)
        print("已发送保存指令到槽 %d（无回执，拔卡插电脑确认）" % args.save)

    if args.load is not None:
        h.load_model(args.load)
        time.sleep(3)
        print("已从槽 %d 载入（无回执，用 --check 验证）" % args.load)

    if args.forget:
        h.forget()
        time.sleep(1)
        _, learned, _ = h.read_blocks()
        print("已清空；当前已学 ID 数：%s" % learned)

    hard_exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("出错：%s" % e)
        hard_exit(1)
