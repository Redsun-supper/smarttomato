# -*- coding: utf-8 -*-
"""二哈 1 代「学手机」工具 —— 远程学习 / 模型存取 / 识别核查。

背景：二哈 1 代（SEN0305）一次只能跑**一个**算法，其中「物体分类」是唯一可自学的。
      学出来的类别用 **ID** 区分 —— 本项目只要**一个**「手机」ID。

🔴 学习样本必须覆盖**亮屏和灭屏**：只在黑屏时学，模型学到的是「黑色矩形」，
   亮屏时大概率认不出来。样本配比与验证清单见 docs/m10_learn_phone.md。

📌 官方规格（wiki 实测，2026-10-16）：
   - **每个类别官方推荐学 30 张以上**，"学的越多识别越精准"，没给硬上限。
   - 模块上「长按学习键不松开」可连续录入多张，松开自动训练。
     → 脚本用 `--burst` 等效实现（每次 `learn_once` = 学一张）。
   - **每个算法可存 5 个模型（槽 0~4）**，官方设计意图就是「多场景切换」：
     比如白天/晚上各存一个模型，用时切换，避免塞进同一个 ID 互相干扰。
   - 物体分类**不输出坐标**（框位置固定屏幕中央），xCenter/yCenter 无意义。

⚠️ 两件必读（详见 docs/m10_learn_phone.md）：
   1. 「物体分类」和主程序用的「人脸识别」**互斥**，切成物体分类后判在座就没了。
   2. I2C 单进程约束：跑本脚本前必须先 `kill` 掉主程序的**精确 PID**；
      学完要 `reboot`，等约 1 分钟，再重新拉起主程序。

用法（在板子上 /root/smarttomato 下单独跑，加 `-u` 看实时输出）：

    python3 -u husky_learn_phone.py --check 5           # 切「物体分类」并连读 5 秒
    python3 -u husky_learn_phone.py --learn 1           # 把镜头前这一刻学成类别 1
    python3 -u husky_learn_phone.py --burst 1 30        # 连续学 30 张到 ID 1（晃动手机！）
    python3 -u husky_learn_phone.py --name 1 手机        # 起名（模块屏幕显示用）
    python3 -u husky_learn_phone.py --save 0            # 模型存 SD 卡槽 0（卡须 FAT32）
    python3 -u husky_learn_phone.py --load 0            # 从 SD 卡槽 0 载入
    python3 -u husky_learn_phone.py --forget            # 清空全部已学内容

「亮屏 / 灭屏」都要学、且都归到同一个 ID —— 用 `--burst` 给每个条件学够 30 张。
所有命令默认先切到「物体分类」（约 5.5 秒），加 `--no-switch` 可跳过（省时间）。
"""
import argparse
import sys
import time

HL_ADDR = 0x32
HL_REQ_BLOCKS = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]
HL_READ_LEN = 64           # 一次读 64 字节 = RETURN_INFO(16B) + 最多 3 个 RETURN_BLOCK；
                           # 物体分类永远只有 1 个结果（共 32 字节），够用
ALGO = "ALGORITHM_OBJECT_CLASSIFICATION"
ALGO_WAIT = 5.5            # 切算法后官方建议等约 5s


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
    包不合法时框数返回 None（注意：这不代表没人，只代表这次读失败）。
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
            blocks.append((u16(d, 8),      # ID
                           u16(d, 0),      # xCenter
                           u16(d, 2),      # yCenter
                           u16(d, 4),      # width
                           u16(d, 6)))     # height
        pos = end
    return n_box, n_learned, blocks


def fmt(blocks):
    if not blocks:
        return "（无检测框）"
    return " / ".join("ID=%d 框(%d,%d %dx%d)" % b for b in blocks)


class Husky(object):
    """薄封装：发命令走官方库（它管协议细节），读 blocks 走自己的快速读法。

    库的 read_count 是逐字节 readfrom_mem，同一个响应要 51.3ms；自己「发请求 +
    一次读全包」只要 3.4ms（结论完全一致）。这里要连读，所以走快路径。
    ⚠️ 必须先 Board().begin() 初始化板子：pinpong 的 gboard 默认是 None，
    不初始化的话 I2C() 构造里 `self.board.i2c[bus_num]` 直接抛
    `'NoneType' object has no attribute 'i2c'`（主程序也是这么初始化的）。
    """

    def __init__(self):
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


def main():
    ap = argparse.ArgumentParser(description="二哈 1 代学手机工具（物体分类）")
    ap.add_argument("--check", type=float, metavar="秒",
                    help="切到物体分类并连读若干秒，看识别成什么 ID")
    ap.add_argument("--learn", type=int, metavar="ID",
                    help="把镜头前这一刻学成指定 ID（单张）")
    ap.add_argument("--burst", nargs=2, type=int, metavar=("ID", "张数"),
                    help="连续学多张到同一个 ID（等效模块上长按学习键）")
    ap.add_argument("--interval", type=float, default=0.8, metavar="秒",
                    help="--burst 时每张之间的间隔（默认 0.8s，留时间给你换角度）")
    ap.add_argument("--name", nargs=2, metavar=("ID", "名称"),
                    help="给类别起名，显示在模块屏幕上")
    ap.add_argument("--save", type=int, metavar="槽",
                    help="模型存到 SD 卡槽位（卡必须 FAT32）")
    ap.add_argument("--load", type=int, metavar="槽",
                    help="从 SD 卡槽位载入模型")
    ap.add_argument("--forget", action="store_true", help="清空全部已学内容")
    ap.add_argument("--no-switch", action="store_true",
                    help="不切算法（省 5.5 秒；模块已是物体分类时用）")
    args = ap.parse_args()

    if not any([args.check is not None, args.learn is not None, args.burst,
                args.name, args.save is not None, args.load is not None,
                args.forget]):
        ap.print_help()
        return 2

    try:
        h = Husky()
    except Exception as e:
        print("二哈初始化失败：%s" % e)
        print("→ 先确认：主程序是不是还占着 I2C？二哈的 TF 卡插着没有（非 FAT32 会让 0x32 直接消失）？")
        return 1

    # ⚠️ 兜底：任何意外都不能让进程「带着半截 I2C 事务」死掉 ——
    #    那种死法会在退出复位 gd32v 时引发内核 panic（2026-10-17 实测）。
    try:
        if not args.no_switch:
            print("切到「物体分类」…（等 %.1f 秒）" % ALGO_WAIT)
            h.switch()

        if args.check:
            print("\n连读 %.1f 秒：把目标放镜头前，看打印出的 ID（变化才打一行）\n" % args.check)
            t_end = time.time() + args.check
            prev = None
            while time.time() < t_end:
                n, nlearned, blocks = h.read_blocks()
                cur = (n, nlearned, tuple(blocks))
                if cur != prev:
                    print("  框数=%-4s 已学=%-3s %s" % (n, nlearned, fmt(blocks)))
                    prev = cur
                time.sleep(0.2)

        if args.learn is not None:
            cid = args.learn
            print("把镜头前这一刻学成类别 %d …" % cid)
            print("⚠️ 先把目标摆好：换角度、换 App、换环境光，请**反复执行本命令**多学几次")
            try:
                h.lib.command_request_learn_once(cid)
            except Exception as e:
                print("  学习失败（%s）—— 总线可能抽了，换一次再试" % e)
                return 1
            time.sleep(1.0)
            n, nlearned, blocks = h.read_blocks()
            print("→ 已学 ID 数：%s" % nlearned)
            print("→ 当前识别：%s" % fmt(blocks))
            print("⚠️ 刚学完读到的可能是旧结果 —— 请再跑一次 --check 看真实效果")

        if args.burst:
            cid, n = args.burst[0], args.burst[1]
            print("连续学 %d 张到 ID %d，每 %.1f 秒一张（共约 %.0f 秒）"
                  % (n, cid, args.interval, n * args.interval))
            print("⚠️ 这期间请**不断晃动手机 / 换角度 / 换 App** ——")
            print("   静止不动学 30 张 = 1 张，白费。官方建议每类 30 张以上。\n")
            t0 = time.time()
            for i in range(n):
                try:
                    h.lib.command_request_learn_once(cid)
                except Exception as e:
                    print("  [%d/%d] 学习失败（%s）—— 总线可能抽了，跳这张继续" % (i + 1, n, e))
                    continue
                print("  [%d/%d] 已学（%.0fs）" % (i + 1, n, time.time() - t0))
                time.sleep(0.4)          # 给模块消化/训练的时间，否则指令会背积
                if i < n - 1:
                    time.sleep(args.interval)
            time.sleep(1.0)
            _, nlearned, blocks = h.read_blocks()
            print("\n→ 已学 ID 数：%s" % nlearned)
            print("→ 当前识别：%s" % fmt(blocks))
            print("⚠️ 刚学完读到的可能是旧结果 —— 请再跑一次 --check 看真实效果")

        if args.name:
            cid, nm = int(args.name[0]), args.name[1]
            try:
                h.lib.command_request_customnames(cid, nm)
            except Exception as e:
                print("  起名失败（%s）" % e)
                return 1
            time.sleep(0.5)
            print("已把 ID %d 命名为「%s」" % (cid, nm))

        if args.save is not None:
            print("模型存到 SD 卡槽 %d …" % args.save)
            print("⚠️ 卡必须是 FAT32 —— 插上非 FAT32 的卡，模块会卡在启动流程，0x32 直接消失")
            try:
                h.lib.command_request_save_model_to_SD_card(args.save)
            except Exception as e:
                print("  保存失败（%s）" % e)
                return 1
            time.sleep(3.0)
            print("已发送保存指令（无回执，需拔卡插电脑确认根目录有无新文件）")

        if args.load is not None:
            try:
                h.lib.command_request_load_model_from_SD_card(args.load)
            except Exception as e:
                print("  载入失败（%s）" % e)
                return 1
            time.sleep(3.0)
            print("已从 SD 卡槽 %d 载入（无回执，跑 --check 验证）" % args.load)

        if args.forget:
            try:
                h.lib.command_request_forget()
            except Exception as e:
                print("  清空失败（%s）" % e)
                return 1
            time.sleep(1.0)
            _, nlearned, _ = h.read_blocks()
            print("已清空；当前已学 ID 数：%s" % nlearned)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
