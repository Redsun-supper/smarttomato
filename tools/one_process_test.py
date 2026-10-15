# -*- coding: utf-8 -*-
"""
单进程全链路测试（行空板 M10）
================================

⚠️⚠️ 为什么必须"一个进程跑完"（2026-10-13 实测确认）

    行空板上 pinpong 的 I2C **只在开机后的第一个进程里能用**。
    第一个进程退出后，GD32 会把 **SCL 留在低电平**，之后任何进程做 I2C
    都会报 `PermissionError(1, 'Operation not permitted')`，必须断电重启。

    实测证据（同一个脚本 `i2c_check.py`，同一次开机）：
        第 1 个进程 -> SCL 40/40 健康，扫到 0x40  ✅
        第 2 个进程 -> SCL 0/40 恒低，全空        ❌

    → 本项目的主程序 `m10_focus_clock.py` 是**单个常驻进程**，所以不受影响。
    → 但**调试时千万不要反复跑脚本**，那会制造出"总线卡死/模块冲突"的假象。

用法
----
    1. 断电重启板子（拔 Type-C，等 3 秒再插）
    2. 等 2 分钟
    3. python3 one_process_test.py

本脚本一个进程内依次做：
    [1] 量总线电平
    [2] 全地址扫描
    [3] TTS 基线：begin + 念「你好」        ← 先证明合成能用
    [4] ASR：初始化 + 轮询 2 秒             ← 再把识别加上
    [5] TTS 复测：再念一次「你好」          ← 关键！验证"识别在线时合成还能不能用"
    [6] ASR 再读一次                        ← 验证 TTS 播报后识别还活着

第 [5] 步就是本项目最核心的问题：**两个模块到底能不能共存**。
"""

import sys
import time

TTS_ADDR = 0x40
ASR_ADDR = 0x4F
TTS_VERSION = "V2"
TTS_VOLUME = 5
WAKE_WORDS = (("xiao yi xiao yi", 1),)
REPLY_TEXT = "你好"


def log(msg):
    print(msg)
    sys.stdout.flush()


def step(n, title):
    log("\n" + "-" * 52)
    log("[%d] %s" % (n, title))
    log("-" * 52)


def main():
    log("=" * 52)
    log(" 单进程全链路测试  (TTS 0x40 / ASR 0x4F)")
    log("=" * 52)

    from pinpong.board import Board, Pin, I2C
    Board("UNIHIKER").begin()
    time.sleep(1.0)

    # ---- [1] 总线电平 ----
    step(1, "总线电平 (P19=SCL, P20=SDA)")
    scl = Pin(Pin.P19, Pin.IN)
    sda = Pin(Pin.P20, Pin.IN)
    time.sleep(0.2)
    s = d = 0
    for _ in range(40):
        s += scl.value()
        d += sda.value()
        time.sleep(0.01)
    log("    SCL 高 %d/40 | SDA 高 %d/40" % (s, d))
    if s != 40 or d != 40:
        log("    ⚠️ 总线不健康。请断电重启后再跑（第 2 个进程必然这样）")

    # ---- [2] 全地址扫描 ----
    step(2, "I2C 全地址扫描")
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
    names = {0x32: "HUSKYLENS", 0x40: "DFR0760 TTS", 0x4F: "DFR0715 ASR"}
    if found:
        for a in found:
            log("    0x%02X  %s" % (a, names.get(a, "?")))
    else:
        log("    一个设备都没有")
    has_tts = TTS_ADDR in found
    has_asr = ASR_ADDR in found
    log("    TTS(0x40) %s | ASR(0x4F) %s" % (
        "在 ✅" if has_tts else "不在 ❌", "在 ✅" if has_asr else "不在 ❌"))

    # ---- [3] TTS 基线 ----
    step(3, "TTS 基线：begin + 念「%s」" % REPLY_TEXT)
    tts = None
    try:
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        tts = DFRobot_SpeechSynthesis_I2C()
        tts.begin(tts.V1 if TTS_VERSION == "V1" else tts.V2)
        tts.set_voice(TTS_VOLUME)
        t0 = time.time()
        tts.speak(REPLY_TEXT)
        log("    ✅ 播报成功，耗时 %.1fs" % (time.time() - t0))
    except BaseException as e:
        log("    ❌ 失败：%r" % (e,))
        log("    → 合成模块单独就不通，先查它自己（开关/接线/供电）")
        return

    # ---- [4] ASR 初始化 + 轮询 ----
    step(4, "ASR：初始化 + 轮询 2 秒")
    asr = None
    try:
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        asr = DFRobot_ASR()
        asr.begin(asr.LOOP, asr.MIC)
        for text, idx in WAKE_WORDS:
            asr.add_command(text, idx)
        asr.start()
        log("    ✅ 初始化成功（关键词已下发 + start）")
        t0 = time.time()
        n = 0
        while time.time() - t0 < 2.0:
            asr.read()
            n += 1
            time.sleep(0.05)
        log("    轮询 %d 次，无异常" % n)
    except BaseException as e:
        log("    ❌ 失败：%r" % (e,))
        log("    → 识别模块不通。但它已经不影响下面的 TTS 复测")
        asr = None

    # ---- [5] TTS 复测（核心！）----
    step(5, "TTS 复测：识别模块在线时，合成还能不能用？")
    try:
        t0 = time.time()
        tts.speak(REPLY_TEXT)
        log("    ✅✅ 播报成功，耗时 %.1fs" % (time.time() - t0))
        log("")
        log("    >>> 结论：两个模块**可以共存**！")
        log("    >>> 之前记录的「ASR+TTS 冲突」很可能是多进程调试造成的假象。")
    except BaseException as e:
        log("    ❌ 失败：%r" % (e,))
        log("")
        log("    >>> 结论：ASR 一上线，TTS 就用不了 —— 「冲突」是真的。")
        log("    >>> 那就只能走 UART 方案（tools/dfr0760_uart.py）。")

    # ---- [6] ASR 再读 ----
    step(6, "ASR 再读一次（TTS 播报后识别还活着吗）")
    if asr is None:
        log("    (ASR 未初始化，跳过)")
    else:
        try:
            vals = []
            for _ in range(6):
                vals.append(asr.read())
                time.sleep(0.2)
            log("    ✅ 仍可读取：%s" % vals)
        except BaseException as e:
            log("    ❌ 失败：%r" % (e,))

    log("\n" + "=" * 52)
    log(" done.  现在可以直接断电重启了（本轮已用完这个进程）")
    log("=" * 52)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已退出。")
