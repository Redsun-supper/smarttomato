# -*- coding: utf-8 -*-
"""
语音唤醒 → 语音合成 联动测试（行空板 M10）
==========================================

    喊出「xiao yi xiao yi」（小易小易） →  语音合成念出「你好」

硬件
----
1) 语音识别：DFR0715（Gravity I2C 离线语音识别，芯片 LD3320）
   - I2C 地址 0x4F，pinpong 自带库 `pinpong.libs.dfrobot_asr.DFRobot_ASR`
   - 关键词用拼音在代码里下发，不需要"学习"：
         begin(LOOP, MIC) -> add_command("xiao yi xiao yi", 1) -> start()
   - read() 返回关键词 ID，无结果返回 -1

2) 语音合成：DFR0760（Gravity 中英文语音合成，I2C 版）
   - I2C 地址 0x40，pinpong 自带库 `pinpong.libs.dfrobot_speech_synthesis`
   - begin(V2) -> speak("你好")，speak() 内部会阻塞到播报结束
   - ⚠️ begin(V2) 握手失败时会连报 6 次 "synthesis iic communication faild"
      然后抛 ValueError（老版库会直接 exit()），所以这里用 except BaseException 兜底

⚠️ 重要：本项目已实测的硬件冲突
--------------------------------
    「语音识别(0x4F) + 语音合成(0x40)」两个模块**同时挂在 4Pin I2C 上会失败**：
    扫描阶段两者都能扫到，但只要一执行 TTS 的 begin() 就报通信失败，
    随后整条总线被拉死（SCL 恒低），必须断电重启才能恢复。
    已复现两次（2026-10-13 13:40 / 17:50），可定论。

    ✅ 单独接 TTS      —— 正常
    ✅ 单独接语音识别  —— 正常
    ❌ 两个一起接      —— TTS 通信失败 + 总线卡死

    所以本脚本**默认会把 TTS 作为第一步先测**，好让你在总线被拉死之前
    就拿到「TTS 本身没问题」的证据。若第 4 步（识别模块）之后崩溃，
    那就是上面这个已知冲突，不是代码问题。

用法（在行空板上执行）
----------------------
    python3 test_wake_tts.py              # 完整测试：扫总线 → 测 TTS → 测识别 → 等唤醒
    python3 test_wake_tts.py --scan       # 只扫总线并打印电平
    python3 test_wake_tts.py --tts-only   # 只测语音合成（推荐先用这个排除模块问题）
    python3 test_wake_tts.py --asr-only   # 只测语音识别（不初始化 TTS）
    python3 test_wake_tts.py --order asr-first   # 换初始化顺序（TTS 优先 / 识别优先）

Ctrl+C 退出。
"""

import sys
import time

# ============================ 可调参数 ============================

# ---- 唤醒词（拼音，不需要"学习"）----
# 单条 ≤10 汉字 / 79 字节拼音，建议 4~6 音节，喊话时离麦克风 10cm 以上
WAKE_WORDS = (
    ("xiao yi xiao yi", 1),        # 「小易小易」
)

# ---- 唤醒后要念的内容 ----
REPLY_TEXT = "你好"

# ---- 语音识别 DFR0715 / LD3320 ----
ASR_ADDR = 0x4F
VOICE_MODE = "LOOP"                # LOOP（循环识别）/ BUTTON（按键触发）/ PASSWORD（密码）
VOICE_INPUT = "MIC"                # MIC（板载麦克风）/ MONO（LineIn）

# ---- 语音合成 DFR0760 ----
TTS_ADDR = 0x40
TTS_VERSION = "V2"                 # DFR0760 = "V2"；老款 DFR0213 = "V1"
TTS_VOLUME = 5                     # 音量 0~9

# ---- 行为开关 ----
BUS_LEVEL_CHECK = True             # 是否先用 GPIO 量总线空闲电平（最有效的判据）
SPEAK_ON_START = True              # TTS 初始化成功后先念一次 REPLY_TEXT 做自证
SCAN_BUS = True                    # 是否全地址扫描一遍
POLL_SEC = 0.05                    # 语音轮询间隔
COOLDOWN_SEC = 3.0                 # 同一唤醒词的最短重复触发间隔（防止连续触发）
INIT_ORDER = "tts-first"           # "tts-first" / "asr-first"

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"

KNOWN_DEVICES = {
    0x32: "HUSKYLENS  (二哈识图)",
    0x40: "DFR0760    (语音合成 TTS)",
    0x4F: "DFR0715    (语音识别 ASR)",
}

# ==================================================================


def log(msg):
    print(msg)
    sys.stdout.flush()


# --------------------------- 各步骤 ---------------------------

def step_board():
    """初始化 pinpong。I2C 依赖它，且板子 i2c-4 是 pinpong 动态创建的。"""
    log("[1/5] 初始化 pinpong ...")
    try:
        from pinpong.board import Board
        Board("UNIHIKER").begin()
        time.sleep(1.0)
        log(OK + " Board().begin() 成功")
        return True
    except Exception as e:
        log(BAD + " pinpong 初始化失败：%r" % (e,))
        return False


def step_bus_level():
    """用 GPIO 读总线空闲电平。

    这是排查接线最有效的判据（比 i2cdetect 信息量大）：
      SCL/SDA 都高          -> 总线空闲，健康
      SCL 恒低、SDA 高      -> 有从机把 SCL 拉死（或 SDA/SCL 接反）
      两线都低              -> 没有上拉 / 线没接好
    注意：必须「先 Pin 再 I2C」，同一进程内反序会触发 pinpong 的 io remap error。
    """
    log("\n[2/5] 总线空闲电平 (P19 = SCL, P20 = SDA)")
    try:
        from pinpong.board import Pin
        scl = Pin(Pin.P19, Pin.IN)
        sda = Pin(Pin.P20, Pin.IN)
        time.sleep(0.2)
        s, d = [], []
        for _ in range(40):
            s.append(scl.value())
            d.append(sda.value())
            time.sleep(0.01)
    except Exception as e:
        log(WARN + " 电平采样失败（跳过）：%r" % (e,))
        return

    sh, dh = s.count(1), d.count(1)
    log("    SCL 高 %d/40 | SDA 高 %d/40" % (sh, dh))
    if sh == 40 and dh == 40:
        log(OK + " 两线皆高，总线空闲正常")
    elif sh == 0 and dh == 40:
        log(BAD + " SCL 恒低、SDA 高 —— 总线被拉死")
        log("      -> 可能：SDA/SCL 接反 / 某模块 clock-stretching 不释放")
        log("      -> 处理：断电，拔掉所有模块，逐个接回排查")
    elif sh == 0 and dh == 0:
        log(BAD + " 两线皆低 —— 没有上拉或线没接好")
    else:
        log(WARN + " 电平在跳变，总线正忙")


def step_scan():
    """全地址扫描，看 0x40 / 0x4F 是否应答。"""
    log("\n[3/5] I2C 全地址扫描 (0x08 - 0x77)")
    try:
        from pinpong.board import I2C
        i2c = I2C(bus_num=0)
    except Exception as e:
        log(BAD + " 创建 I2C 对象失败：%r" % (e,))
        return []

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

    if not found:
        log(BAD + " 一个设备都没有应答")
    for a in found:
        log("    0x%02X  %s" % (a, KNOWN_DEVICES.get(a, "unknown")))
    return found


def init_tts():
    """初始化语音合成模块。失败返回 None。

    注意库里的坑：
      * begin(V2) 内部会 write_cmd + read_ack，失败连报 6 次后抛 ValueError
      * 某些版本握手失败时直接调 exit() 抛 SystemExit（不是 Exception）
      所以这里用 except BaseException 一并接住，避免整个脚本被终止。
    """
    log("\n[4/5] 初始化语音合成模块 (0x%02X, %s)" % (TTS_ADDR, TTS_VERSION))
    try:
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        tts = DFRobot_SpeechSynthesis_I2C()
        ver = tts.V1 if TTS_VERSION == "V1" else tts.V2
        tts.begin(ver)
        tts.set_voice(TTS_VOLUME)
        log(OK + " 语音合成模块就绪")
    except BaseException as e:
        log(BAD + " 语音合成初始化失败：%r" % (e,))
        log("      -> 单独只接 TTS 时正常吗？若正常，就是与识别模块的已知冲突")
        return None

    if SPEAK_ON_START:
        log("    试念一句「%s」..." % REPLY_TEXT)
        try:
            t0 = time.time()
            tts.speak(REPLY_TEXT)
            log(OK + " 播报完成，耗时 %.1fs" % (time.time() - t0))
        except BaseException as e:
            log(BAD + " 播报失败：%r" % (e,))
            log("      -> 若此刻总线被拉死，请断电重启后再单独测 TTS")
            return None
    return tts


def init_asr():
    """初始化语音识别模块。失败返回 None。

    关键词必须在 start() 之前用 add_command() 下发，
    start() 相当于 Mind+ 里的「设置完成 开始识别」。
    """
    log("\n[5/5] 初始化语音识别模块 (0x%02X)" % ASR_ADDR)
    try:
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        asr = DFRobot_ASR()
        asr.begin(getattr(asr, VOICE_MODE), getattr(asr, VOICE_INPUT))
        for text, idx in WAKE_WORDS:
            asr.add_command(text, idx)
        asr.start()
        log(OK + " 语音识别模块就绪，唤醒词：%s" % " / ".join(w for w, _ in WAKE_WORDS))
        return asr
    except BaseException as e:
        log(BAD + " 语音识别初始化失败：%r" % (e,))
        log("      -> 库会重试 6 次并打印 'Asr iic communication faild'")
        log("      -> 若此时总线被拉死，就是「识别 + 合成」的已知冲突")
        return None


def wait_for_wake(asr, tts):
    """轮询识别结果；识别到唤醒词就念 REPLY_TEXT。Ctrl+C 退出。"""
    wake_ids = tuple(idx for _, idx in WAKE_WORDS)
    log("\n" + "=" * 52)
    log(" 开始监听，请对着麦克风喊：「小易小易」")
    log(" 离麦克风 10cm 以上，咬字清晰，每次间隔 3 秒以上")
    log(" Ctrl+C 退出")
    log("=" * 52 + "\n")

    prev_id = None
    last_fire = 0.0
    count = 0

    while True:
        try:
            cmd_id = asr.read()
        except BaseException as e:
            log(BAD + " 读取识别结果失败：%r" % (e,))
            log("      -> 总线可能已被拉死，需断电重启")
            return

        if cmd_id != -1:
            if cmd_id != prev_id:
                log("   识别到 ID = %d" % cmd_id)
            # 触发条件：识别到唤醒词，且「是新的识别事件」或「距上次触发超过冷却时间」
            # （LD3320 可能把同一个 ID 保持一段时间，所以不能只看 ID 变化）
            is_new = (cmd_id != prev_id)
            cooled = (time.time() - last_fire) >= COOLDOWN_SEC
            if cmd_id in wake_ids and (is_new or cooled):
                count += 1
                last_fire = time.time()
                log("\n>>> 第 %d 次唤醒（ID=%d）→ 念「%s」" % (count, cmd_id, REPLY_TEXT))
                if tts is not None:
                    try:
                        t0 = time.time()
                        tts.speak(REPLY_TEXT)
                        log("    播报完成，耗时 %.1fs\n" % (time.time() - t0))
                    except BaseException as e:
                        log(BAD + " 播报失败：%r" % (e,))
                        log("      -> 若总线被拉死，就是「识别 + 合成」的已知冲突\n")
                        return
                else:
                    log("    (未接语音合成模块，仅打印)\n")

        prev_id = cmd_id
        time.sleep(POLL_SEC)


# ----------------------------- 主流程 -----------------------------

def main():
    args = sys.argv[1:]
    scan_only = "--scan" in args
    tts_only = "--tts-only" in args
    asr_only = "--asr-only" in args
    global INIT_ORDER
    if "--order" in args:
        try:
            INIT_ORDER = args[args.index("--order") + 1]
        except IndexError:
            pass

    log("=" * 52)
    log(" 唤醒 → 语音合成 联动测试 (UNIHIKER M10)")
    log(" 唤醒词：「xiao yi xiao yi」  应答：「%s」" % REPLY_TEXT)
    log("=" * 52)

    if not step_board():
        return

    if BUS_LEVEL_CHECK:
        step_bus_level()
    if SCAN_BUS:
        found = step_scan()
        if found and TTS_ADDR not in found and not asr_only:
            log(WARN + " 没扫到 0x40（语音合成），先检查它的 I2C 模式开关和接线")

    if scan_only:
        log("\ndone. (--scan 模式，仅扫描)")
        return

    # ---- 按指定顺序初始化两个模块 ----
    tts, asr = None, None
    if INIT_ORDER == "asr-first":
        if not tts_only:
            asr = init_asr()
        if not asr_only:
            tts = init_tts()
    else:
        if not asr_only:
            tts = init_tts()
        if not tts_only:
            asr = init_asr()

    if tts_only:
        log("\ndone. (--tts-only 模式，仅测语音合成)")
        return
    if asr is None:
        log("\n识别模块不可用，无法继续监听。")
        return

    try:
        wait_for_wake(asr, tts)
    except KeyboardInterrupt:
        log("\n已退出。")


if __name__ == "__main__":
    main()
