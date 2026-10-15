# -*- coding: utf-8 -*-
"""
「顺序挂载 / 软件分时」可行性判定实验（行空板 M10）
====================================================

背景
----
本项目已实测：「语音识别(0x4F) + 语音合成(0x40)」同时挂在 4Pin I2C 上会失败
（扫描能看到两个地址 → 一执行 `tts.begin()` 就报通信失败 → 整条总线卡死）。

于是很自然想到一个办法：**顺序挂载** —— 两个模块都接着，但代码里保证
「语音识别在线时不去调用合成」，任一时刻只跟一个模块说话。

这个脚本就是来判定这个办法到底行不行的。它把问题拆成三个能单独下结论的实验：

  实验 A —— 物理共存，但完全不碰识别模块
      两个模块都插着，代码里**一次都不初始化/不寻址 ASR**，只做 TTS 的 begin + speak。
      ✅ 通过 → 说明「物理共存」本身不致命，只要不同时寻址就行 → 分时复用有希望
      ❌ 失败 → 说明**光是把两个模块插在一起就够了**，代码层面怎么错开都没用
                → 「顺序挂载」方案直接作废，必须走物理隔离（见文末方案）

  实验 B —— 识别模块一旦被激活，会不会毒化总线
      TTS 先初始化并试念一句（基线）→ 再初始化 ASR 并轮询几秒 → 停止轮询（不再读）
      → 再让 TTS 念一句。
      ✅ 通过 → 分时复用可行，直接进实验 C
      ❌ 失败 → ASR 只要被激活过，TTS 就再也用不了 → 分时复用作废

  实验 C —— 分时复用监听（如果 A/B 通过就直接可用）
      单线程交替：轮询 ASR 抓唤醒词 → 抓到就停止轮询 → 调 TTS 念「你好」→ 恢复轮询。

用法
----
    python3 test_bus_mutex.py              # 依次跑 A → B → C
    python3 test_bus_mutex.py --phase a    # 只跑实验 A（推荐先跑这个，结论最干净）
    python3 test_bus_mutex.py --phase b    # 只跑实验 B
    python3 test_bus_mutex.py --phase c    # 只跑实验 C（分时复用监听）

⚠️ 每个实验失败后总线可能已被拉死，**必须断电重启板子**再跑下一个实验，
   否则下一个实验一定也是失败，会得出错误结论。

如果三个实验全挂，请直接看文件末尾的「方案对比」——真正能解决问题的是让
TTS 走 UART（DFR0760 板上就有 I2C/UART 切换开关），配套驱动见
`tools/dfr0760_uart.py`。
"""

import sys
import time

# ------------------------------ 配置 ------------------------------

ASR_ADDR = 0x4F
TTS_ADDR = 0x40

WAKE_WORDS = (("xiao yi xiao yi", 1),)     # 唤醒词：拼音 + ID
REPLY_TEXT = "你好"
VOICE_MODE = "LOOP"
VOICE_INPUT = "MIC"
TTS_VERSION = "V2"
TTS_VOLUME = 5

BUS_LEVEL_CHECK = True        # 先用 GPIO 量总线空闲电平
POLL_SEC = 0.05               # 实验 B/C 的语音轮询间隔
POLL_SECONDS = 3.0            # 实验 B 轮询多久后停手
COOLDOWN_SEC = 3.0

OK = "[ OK ]"
BAD = "[FAIL]"
INFO = "[INFO]"

KNOWN_DEVICES = {
    0x32: "HUSKYLENS  (二哈识图)",
    0x40: "DFR0760    (语音合成 TTS)",
    0x4F: "DFR0715    (语音识别 ASR)",
}


def log(msg):
    print(msg)
    sys.stdout.flush()


def banner(title):
    log("\n" + "=" * 56)
    log(" " + title)
    log("=" * 56)


# ---------------------------- 公共工具 ----------------------------

def board_begin():
    from pinpong.board import Board
    Board("UNIHIKER").begin()
    time.sleep(1.0)


def bus_level():
    """量总线空闲电平。必须「先 Pin 再 I2C」，反序会触发 pinpong 的 io remap error。"""
    from pinpong.board import Pin
    scl = Pin(Pin.P19, Pin.IN)
    sda = Pin(Pin.P20, Pin.IN)
    time.sleep(0.2)
    s, d = [], []
    for _ in range(40):
        s.append(scl.value())
        d.append(sda.value())
        time.sleep(0.01)
    sh, dh = s.count(1), d.count(1)
    healthy = (sh == 40 and dh == 40)
    log("    总线电平：SCL 高 %d/40 | SDA 高 %d/40  -> %s" % (
        sh, dh, "健康" if healthy else "异常（SCL 被拉死 / 接反）"))
    return healthy


def scan_bus():
    from pinpong.board import I2C
    i2c = I2C(bus_num=0)
    found = []
    for a in range(0x08, 0x78):
        try:
            i2c.readfrom(a, 1)
            found.append(a)
        except Exception:
            try:
                i2c.writeto(a, b"\x00")
                found.append(a)
            except Exception:
                pass
    log("    扫描到：" + (", ".join("0x%02X %s" % (a, KNOWN_DEVICES.get(a, "?"))
                                 for a in found) if found else "无"))
    return found


def make_tts():
    """构造并初始化 TTS。失败返回 None（连同失败原因一起打印）。"""
    try:
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        tts = DFRobot_SpeechSynthesis_I2C()
        ver = tts.V1 if TTS_VERSION == "V1" else tts.V2
        tts.begin(ver)
        tts.set_voice(TTS_VOLUME)
        return tts
    except BaseException as e:   # 库失败时可能 exit() 抛 SystemExit
        log(BAD + " TTS 初始化失败：%r" % (e,))
        return None


def tts_say(tts, text=REPLY_TEXT):
    """让 TTS 念一句。返回 True/False。"""
    try:
        t0 = time.time()
        tts.speak(text)
        log(OK + " TTS 念出「%s」，耗时 %.1fs" % (text, time.time() - t0))
        return True
    except BaseException as e:
        log(BAD + " TTS 播报失败：%r" % (e,))
        return False


def make_asr():
    """构造并初始化 ASR（下发关键词 + start）。失败返回 None。"""
    try:
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        asr = DFRobot_ASR()
        asr.begin(getattr(asr, VOICE_MODE), getattr(asr, VOICE_INPUT))
        for text, idx in WAKE_WORDS:
            asr.add_command(text, idx)
        asr.start()
        log(OK + " ASR 初始化成功（关键词已下发并 start）")
        return asr
    except BaseException as e:
        log(BAD + " ASR 初始化失败：%r" % (e,))
        return None


def preflight(title):
    banner(title)
    board_begin()
    if BUS_LEVEL_CHECK:
        bus_level()
    return scan_bus()


# ---------------------------- 实验 A ----------------------------

def phase_a():
    """物理共存，但完全不碰 ASR —— 判定「物理共存」本身是否致命。"""
    found = preflight("实验 A：两个模块都接着，但完全不初始化 ASR")

    if TTS_ADDR not in found:
        log(BAD + " 没扫到 0x40（TTS）。请确认 TTS 的模式开关拨到了 I2C 方向、线插好了。")
        return None

    log("\n  >> 现在只跟 TTS 打交道（ASR 一次都不寻址）")
    tts = make_tts()
    if tts is None:
        log("\n" + BAD + " 实验 A 结论：**只接 TTS 就失败** —— 与 ASR 无关，先单独查 TTS。")
        return False

    if not tts_say(tts):
        log("\n" + BAD + " 实验 A 结论：**物理共存本身致命**。")
        log("     ASR 只是插着、代码里一次都没碰它，TTS 就已经用不了了。")
        log("     → 「顺序挂载 / 软件分时」方案作废，必须做物理隔离。")
        return False

    log("\n" + OK + " 实验 A 结论：**物理共存不致命** —— 只要不同时寻址，TTS 就能用。")
    log("     → 分时复用有希望，继续跑实验 B：python3 test_bus_mutex.py --phase b")
    return True


# ---------------------------- 实验 B ----------------------------

def phase_b():
    """ASR 被激活后，TTS 还能不能用 —— 判定「分时复用」是否可行。"""
    found = preflight("实验 B：TTS 基线 → 激活 ASR → 停止轮询 → 再测 TTS")

    for addr, name in ((TTS_ADDR, "TTS"), (ASR_ADDR, "ASR")):
        if addr not in found:
            log(BAD + " 没扫到 0x%02X（%s），本实验需要两个模块都在。" % (addr, name))
            return None

    log("\n  [B-1] TTS 基线：")
    tts = make_tts()
    if tts is None or not tts_say(tts):
        log("\n" + BAD + " 实验 B 中止：TTS 基线就不通，无法判断 ASR 的影响。")
        return False

    log("\n  [B-2] 激活 ASR（begin + add_command + start）：")
    asr = make_asr()
    if asr is None:
        log("\n" + BAD + " 实验 B 结论：ASR 初始化就失败 —— 这是模块/接线问题，不是分时问题。")
        return False

    log("\n  [B-3] 轮询 ASR %.0f 秒（模拟「语音识别在线」）：" % POLL_SECONDS)
    t0 = time.time()
    n = 0
    try:
        while time.time() - t0 < POLL_SECONDS:
            asr.read()
            n += 1
            time.sleep(POLL_SEC)
    except BaseException as e:
        log(BAD + " 轮询 ASR 时出错：%r" % (e,))
        log("\n" + BAD + " 实验 B 结论：**ASR 一被激活就把总线搞坏了**，TTS 无从谈起。")
        return False
    log("     完成 %d 次轮询，无异常" % n)

    log("\n  [B-4] 停止轮询 ASR，再让 TTS 念一句（这就是「顺序挂载」的核心动作）：")
    if not tts_say(tts):
        log("\n" + BAD + " 实验 B 结论：**ASR 只要被激活过，TTS 就再也用不了**。")
        log("     → 「语音识别在线时合成不调用」这种做法无效：等你需要合成时，")
        log("       识别模块已经被激活过了，总线已经坏了。")
        log("     → 分时复用作废，必须物理隔离。")
        return False

    log("\n" + OK + " 实验 B 结论：**分时复用可行！**")
    log("     ASR 轮询期间不碰 TTS、要用 TTS 时先停轮询，这套逻辑是通的。")
    log("     → 继续跑实验 C：python3 test_bus_mutex.py --phase c")
    return True


# ---------------------------- 实验 C ----------------------------

def phase_c():
    """分时复用监听：轮询 ASR 抓唤醒词，抓到就停轮询、调 TTS、再恢复。"""
    preflight("实验 C：分时复用监听（喊「小易小易」→ 念「你好」）")

    log("\n  初始化 TTS（常驻，但只在唤醒时使用）：")
    tts = make_tts()
    if tts is None:
        log(BAD + " TTS 初始化失败，实验 C 无法继续。")
        return

    log("\n  初始化 ASR：")
    asr = make_asr()
    if asr is None:
        log(BAD + " ASR 初始化失败，实验 C 无法继续。")
        return

    wake_ids = tuple(i for _, i in WAKE_WORDS)
    log("\n" + "=" * 56)
    log(" 分时复用已启动：平时只轮询识别模块，唤醒瞬间才唤醒合成模块")
    log(" 请喊：「小易小易」   Ctrl+C 退出")
    log("=" * 56 + "\n")

    prev_id = None
    last_fire = 0.0
    while True:
        try:
            cmd_id = asr.read()          # ← 分时：此刻只跟 ASR 说话
        except BaseException as e:
            log(BAD + " 读取识别结果失败：%r" % (e,))
            log("     -> 总线可能已卡死，需断电重启")
            return

        if cmd_id != -1 and cmd_id != prev_id:
            log("    识别到 ID = %d" % cmd_id)

        if (cmd_id in wake_ids
                and (cmd_id != prev_id or (time.time() - last_fire) >= COOLDOWN_SEC)):
            last_fire = time.time()
            log("\n>>> 唤醒！停止轮询识别模块，交给合成模块 ...")
            # 分时：这里先"让出"识别模块，再调 TTS
            time.sleep(0.2)
            if not tts_say(tts):
                log(BAD + " 播报失败 —— 说明分时复用在这个硬件上仍然不行。")
                log("     -> 请改用 UART 方案：tools/dfr0760_uart.py")
                return
            log("    恢复轮询识别模块 ...\n")

        prev_id = cmd_id
        time.sleep(POLL_SEC)


# ---------------------------- 结论汇总 ----------------------------

def print_verdict(a=None, b=None):
    banner("方案对比（A/B 都失败时看这里）")
    log("""
  ❌ 软件分时（顺序挂载）：把两个模块都接在同一条 I2C 上，靠代码错开寻址。
     从 A/B 的实验结果看，这条路大概率不通 —— 因为 ASR 必须**持续轮询**才能
     抓到唤醒词，而"要用 TTS"这件事本身就发生在 ASR 刚被轮询之后。

  ✅ 方案一（推荐，零硬件改动成本最低）：TTS 改走 UART
     DFR0760 板上自带 I2C/UART 模式开关，拨到 UART 后：
        TTS  →  P0(RX) / P3(TX)  硬串口 /dev/ttySP0，或 USB-A + USB-TTL 转串口
        识别 →  继续留在 4Pin I2C（0x4F）
     两条总线完全独立 → 不存在任何冲突。
     协议帧与 I2C 完全一致，波特率 115200。
     配套驱动已写好：tools/dfr0760_uart.py

  ✅ 方案二：用带独立供电的 I2C 扩展板
     如果冲突根因是供电不足，把模块 VCC 走外部电源即可。改动最小，但要买板子。

  ✅ 方案三：物理分时（GPIO 控制供电）
     用一个 GPIO 驱动 MOS 管/继电器，任一时刻只给一个模块供电。
     本质上是"顺序挂载"的正确形态 —— 但那是物理切换，不是代码切换。

  ✅ 方案四：换非 I2C 的语音识别
     板载硅麦克风 + 云端/本地识别；或换 UART 版识别模块，把识别挪出 I2C 总线。
""")


def main():
    args = [a.lower() for a in sys.argv[1:]]
    phase = None
    for i, a in enumerate(args):
        if a == "--phase" and i + 1 < len(args):
            phase = args[i + 1]
        elif a.startswith("--phase="):
            phase = a.split("=", 1)[1]

    if phase == "a":
        phase_a()
        return
    if phase == "b":
        phase_b()
        return
    if phase == "c":
        phase_c()
        return

    # 默认：A → B → C
    a = phase_a()
    if a is not True:
        print_verdict(a)
        log("\n提示：请断电重启板子后，再单独跑 --phase b 交叉验证。")
        return
    b = phase_b()
    if b is not True:
        print_verdict(a, b)
        log("\n提示：请断电重启板子后，再单独跑 --phase c。")
        return
    phase_c()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已退出。")
