# -*- coding: utf-8 -*-
"""按键式学习 —— 用 M10 的 A/B 键控制二哈「物体分类」的学习。

背景：二哈模块上的按键在模块**本地**，I2C 协议读不到它的状态
      （协议里没有「读按键」指令；0x2C 那个 KNOCK 是心跳，不是按键，库里还被注释掉了）。
      所以拿 M10 自己的 A/B 键当开关 —— 设备 `/dev/input/event2`（名字 `keyboard_ab`）。

按键 code（2026-10-17 从设备位图 `KEY=1000050000000` 推出，bit 28/30/48）：
    A 键 = 30（KEY_A）   B 键 = 48（KEY_B）   Home = 28（KEY_ENTER）
    不确定就先跑 `--probe` 现场确认。

用法（在板子上单独跑，**跑前先 kill 主程序**，跑完 reboot 再拉起来）：

    # 手动模式（默认）：按 A 开始，每按 B 学一张，再按 A 停止
    python3 -u husky_learn_hold.py --id 1

    # 按住模式：按一下开始（立刻学第一张，之后每秒一张），再按一下停
    python3 -u husky_learn_hold.py --id 1 --mode hold --interval 1.0

    # 其它
    python3 -u husky_learn_hold.py --probe                 # 探按键 code
    python3 -u husky_learn_hold.py --id 1 --key-a 30 --key-b 48
    python3 -u husky_learn_hold.py --id 1 --auto-save 0    # 退出时自动存槽 0

⚠️ 手动模式下**每张都是你摆好姿势再按 B**，所以换 App / 换角度 / 换光线
   都能精确控制。官方建议每个类别 30 张以上。细节见 docs/m10_learn_phone.md。
"""
import argparse
import os
import select
import struct
import sys
import time

HL_ADDR = 0x32
HL_REQ_BLOCKS = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]
HL_READ_LEN = 64           # 一次读 64 字节 = RETURN_INFO(16B) + 最多 3 个 RETURN_BLOCK；
                           # 物体分类永远只有 1 个结果（共 32 字节），够用
ALGO = "ALGORITHM_OBJECT_CLASSIFICATION"
ALGO_WAIT = 5.5

# M10 板载 A/B 键
EVENT_DEV = "/dev/input/event2"     # 设备名 keyboard_ab
EV_KEY = 1
KEY_A = 30                          # KEY_A
KEY_B = 48                          # KEY_B
EVENT_FORMAT = "llHHi"              # timeval(16) + type(2) + code(2) + value(4) = 24 字节
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def u16(buf, i):
    return buf[i] | (buf[i + 1] << 8)


def parse_response(raw):
    """拆「请求 blocks」的响应流 → (框数, 已学ID数, [(id, cx, cy, w, h), ...])。

    ⚠️ 关键（2026-10-17 实测）：响应是**两个包** ——
      1) 0x29 RETURN_INFO：data[0..1]=框数、[2..3]=已学ID数、[4..5]=帧号、[6..9]=固定 0
      2) 每个框一个 0x2A RETURN_BLOCK：xCenter yCenter width height ID（各 2 字节小端）
    「物体分类不输出坐标」容易让人以为 ID 在 RETURN_INFO 里 —— **错了**，
    ID 在 RETURN_BLOCK 里（坐标恒为整屏 320×320 无意义，但 ID 是真的）。
    **只读 16 字节永远看不到 ID**，必须连后面的 RETURN_BLOCK 一起读。
    """
    if not raw or len(raw) < 16 or raw[0] != 0x55 or raw[1] != 0xAA:
        return None, None, []
    data = raw[5:5 + raw[3]]
    if len(data) < 6:
        return None, None, []
    n_box, n_learned = u16(data, 0), u16(data, 2)
    blocks, pos = [], 16          # 跳过 RETURN_INFO 固定 16 字节
    while pos + 5 <= len(raw) and len(blocks) < n_box:
        if raw[pos] != 0x55 or raw[pos + 1] != 0xAA:
            break
        dlen = raw[pos + 3]
        end = pos + 5 + dlen + 1
        if end > len(raw):
            break
        if raw[pos + 4] == 0x2A and dlen >= 10:
            d = raw[pos + 5:pos + 5 + dlen]
            blocks.append((u16(d, 8), u16(d, 0), u16(d, 2),
                           u16(d, 4), u16(d, 6)))
        pos = end
    return n_box, n_learned, blocks


def fmt(blocks):
    if not blocks:
        return "（无检测框）"
    return " / ".join("ID=%d 框(%d,%d %dx%d)" % b for b in blocks)


class Husky(object):
    """薄封装：发命令走官方库，读 blocks 走自己的快速读法。"""

    def __init__(self):
        # ⚠️ 必须先 Board().begin() 初始化板子：pinpong 的 gboard 默认是 None，
        #    不初始化的话 I2C() 构造里 `self.board.i2c[bus_num]` 直接抛
        #    `'NoneType' object has no attribute 'i2c'`（主程序也是这么初始化的）。
        from pinpong.board import Board
        Board().begin()
        from pinpong.libs.dfrobot_huskylens import Huskylens
        self.lib = Huskylens(i2c_addr=HL_ADDR, bus_num=0)
        self.i2c = self.lib.i2c

    def switch(self):
        try:
            self.lib.command_request_algorthim(ALGO)
        except Exception as e:
            print("  切算法失败（%s）—— 不退出，继续尝试读数" % e)
            return False
        time.sleep(ALGO_WAIT)
        self.read_blocks()      # 清一次缓冲，避免读到上一个算法留下的残留响应
        return True

    def _read_blocks_once(self):
        """读一次识别结果；包不合法时返回 (None, None, [])（模块可能在忙/训练中）。

        ⚠️ I2C 操作必须包 try：总线偶发失败时 pinpong 直接抛异常，不接住的话
        进程会带着半截 I2C 事务死掉 → 退出时 gd32v 复位 → **内核 panic**
        （2026-10-17 实测：没加这层保护时 daemon 版两次把板子搞崩）。
        """
        try:
            self.i2c.writeto(HL_ADDR, HL_REQ_BLOCKS)
            time.sleep(0.05)      # 给模块准备响应的时间，立即读偶尔会拿到上一次的残留
            raw = self.i2c.readfrom_mem(HL_ADDR, 0, HL_READ_LEN)
        except Exception:
            return None, None, []
        return parse_response(raw)

    def read_blocks(self, retries=4, delay=0.4):
        """读识别结果；包不合法时重试几次，仍失败就重发切算法指令自救。

        ⚠️ 实测（2026-10-17）：连续 learn 之后模块可能卡在不回响应的状态
        （read 持续返回 None），重发 command_request_algorthim + 等 5.5s 即恢复。
        """
        for _ in range(max(1, retries)):
            n, nlearned, blocks = self._read_blocks_once()
            if n is not None:
                return n, nlearned, blocks
            time.sleep(delay)
        print("  （连续 %d 次读包失败，重发切算法指令自救…）" % retries)
        try:
            self.lib.command_request_algorthim(ALGO)
        except Exception as e:
            print("  （自救的切算法也失败了：%s）" % e)
            return None, None, []
        time.sleep(ALGO_WAIT)
        return self._read_blocks_once()


def _open_keys():
    return os.open(EVENT_DEV, os.O_RDONLY)


def _poll_key(fd):
    """读一次按键事件；是「按下」就返回 code，否则返回 None。非阻塞。"""
    r, _, _ = select.select([fd], [], [], 0.05)
    if not r:
        return None
    data = os.read(fd, EVENT_SIZE)
    if len(data) < EVENT_SIZE:
        return None
    _, _, type_, code, value = struct.unpack(EVENT_FORMAT, data)
    if type_ == EV_KEY and value == 1:
        return code
    return None


def cmd_probe(sec):
    """只读按键、不碰 I2C —— 用来确认 A/B 键的 code。"""
    fd = _open_keys()
    print("探测 %.0f 秒：请按 A / B / Home 键，看下面打印的 code" % sec)
    print("（Ctrl+C 提前结束）\n")
    t_end = time.time() + sec
    try:
        while time.time() < t_end:
            code = _poll_key(fd)
            if code is not None:
                print("  按下 code=%d" % code)
    finally:
        os.close(fd)


def _finish(h, n, cid, auto_save):
    print("本次共学 %d 张到 ID %d" % (n, cid))
    if auto_save is not None:
        print("自动存到 SD 卡槽 %d …（卡必须 FAT32）" % auto_save)
        h.lib.command_request_save_model_to_SD_card(auto_save)
        time.sleep(3.0)
        print("已发送保存指令（无回执，拔卡插电脑确认）")
    _, nlearned, _ = h.read_blocks()
    print("当前模块上已学 ID 数：%s" % nlearned)


def cmd_manual(h, cid, key_a, key_b, auto_save):
    """按 A 开始/停止，每按 B 学一张。"""
    fd = _open_keys()
    print("切到「物体分类」…（等 %.1f 秒）" % ALGO_WAIT)
    h.switch()

    print("\n手动模式（A=开始/停止，B=学一张）：")
    print("  按 A 键 → 开始学习 ID %d" % cid)
    print("  按 B 键 → 学一张（先摆好姿势：换 App / 换角度 / 换光线）")
    print("  再按 A 键 → 停止\n")

    active = False
    n = 0
    try:
        while True:
            code = _poll_key(fd)
            if code is None:
                continue
            if code == key_a:
                active = not active
                if active:
                    print("\n▶ 学习模式已开启 —— 现在每按一次 B 键学一张")
                else:
                    print("\n■ 学习模式已关闭")
                    break
            elif code == key_b:
                if not active:
                    print("  （学习模式未开启，先按 A 键）")
                    continue
                try:
                    h.lib.command_request_learn_once(cid)
                except Exception as e:
                    print("  学习失败（%s）—— 总线可能抽了，下张再试" % e)
                    continue
                time.sleep(0.4)      # 给模块消化/训练的时间，否则快速连按会背积
                n += 1
                print("  [%d] 已学 → ID %d" % (n, cid))
            else:
                print("  （未识别的键 code=%d；--probe 查看，--key-a/--key-b 指定）" % code)
    except KeyboardInterrupt:
        print("\n（Ctrl+C）")
    finally:
        os.close(fd)
    _finish(h, n, cid, auto_save)


def cmd_hold(h, cid, interval, key, auto_save):
    """按一下开始（立刻学第一张，之后每 interval 秒一张），再按一下停。"""
    fd = _open_keys()
    print("切到「物体分类」…（等 %.1f 秒）" % ALGO_WAIT)
    h.switch()

    print("\n按住模式（任意键启停）：按一次键 = 开始，再按一次 = 停止")
    print("⚠️ 开始后每 %.1f 秒自动学一张，请**不断晃动手机 / 换角度 / 换 App**\n" % interval)

    learning = False
    n = 0
    next_learn = 0.0
    try:
        while True:
            code = _poll_key(fd)
            if code is not None and (key is None or code == key):
                learning = not learning
                print("\n%s（键 code=%d，累计已学 %d 张）"
                      % ("▶ 开始学习" if learning else "■ 停止", code, n))
                if learning:
                    next_learn = time.time()      # 立刻学第一张
                else:
                    break
            if learning and time.time() >= next_learn:
                try:
                    h.lib.command_request_learn_once(cid)
                except Exception as e:
                    print("  学习失败（%s）—— 总线可能抽了，下一张再试" % e)
                    next_learn = time.time() + interval
                    continue
                n += 1
                print("  [%d] 已学 → ID %d" % (n, cid))
                time.sleep(0.4)      # 给模块消化/训练的时间，否则指令会背积
                next_learn = time.time() + interval
    except KeyboardInterrupt:
        print("\n（Ctrl+C）")
    finally:
        os.close(fd)
    _finish(h, n, cid, auto_save)


def main():
    ap = argparse.ArgumentParser(description="用 M10 的 A/B 键控制二哈物体分类学习")
    ap.add_argument("--probe", type=float, nargs="?", const=8.0, metavar="秒",
                    help="只探测按键 code，不碰 I2C（默认 8 秒）")
    ap.add_argument("--id", type=int, metavar="ID", help="要学的类别 ID")
    ap.add_argument("--mode", choices=["manual", "hold"], default="manual",
                    help="manual=按A启停+按B学一张（默认）；hold=按一下连续学、再按一下停")
    ap.add_argument("--interval", type=float, default=1.0, metavar="秒",
                    help="hold 模式下每张之间的间隔（默认 1.0 秒）")
    ap.add_argument("--key", type=int, metavar="code",
                    help="hold 模式下限定某一个键（默认任意键触发）")
    ap.add_argument("--key-a", type=int, default=KEY_A, metavar="code",
                    help="A 键 code（默认 %d）" % KEY_A)
    ap.add_argument("--key-b", type=int, default=KEY_B, metavar="code",
                    help="B 键 code（默认 %d）" % KEY_B)
    ap.add_argument("--auto-save", type=int, metavar="槽",
                    help="退出时自动把模型存到 SD 卡槽位")
    args = ap.parse_args()

    if args.probe is not None:
        cmd_probe(args.probe)
        return 0
    if args.id is None:
        ap.print_help()
        return 2

    try:
        h = Husky()
    except Exception as e:
        print("二哈初始化失败：%s" % e)
        print("→ 先确认：主程序是不是还占着 I2C？二哈的 TF 卡插着没有（非 FAT32 会让 0x32 消失）？")
        return 1

    if args.mode == "manual":
        try:
            cmd_manual(h, args.id, args.key_a, args.key_b, args.auto_save)
        except Exception:
            # ⚠️ 兜底：任何意外都不能让进程「带着半截 I2C 事务」死掉 ——
            #    那种死法会在退出复位 gd32v 时引发内核 panic（2026-10-17 实测）。
            import traceback
            traceback.print_exc()
            return 1
    else:
        try:
            cmd_hold(h, args.id, args.interval, args.key, args.auto_save)
        except Exception:
            import traceback
            traceback.print_exc()
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
