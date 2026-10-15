# -*- coding: utf-8 -*-
"""
最小化测试 —— 验证「全地址扫描本身会把总线搞坏」的假设（行空板 M10）
====================================================================

背景
----
2026-10-13 20:28 单进程测试结果：
    [1] 电平    SCL 40/40 健康
    [2] 全地址扫描  找到 0x40 (TTS)          ← 能扫到
    [3] TTS begin   连报 6 次失败 → ValueError  ← 但驱动一写就失败

而历史记录里 13:37 那次「只接 TTS = begin + speak 完全正常」。

两者差别：**20:28 那次在 TTS 之前先做了一次全地址扫描**（112 个地址 × 2 次操作）。
假设：**大量 I2C 操作会把这条（GD32 模拟的）总线弄坏**，
所以扫描能扫到 0x40（它在 0x4F 之前，赶在坏掉之前），但之后 TTS 就用不了了。

本实验
------
一个进程内，**故意不做任何扫描**，直接测 TTS；然后再做一次全地址扫描，
最后再测一次 TTS。如果「扫描前正常、扫描后失败」，假设即成立。

    [1] 直接测 TTS（begin + 念「你好」）      ← 不做扫描
    [2] 再念一次（确认稳定）
    [3] 全地址扫描                            ← 唯一的"重负载"操作
    [4] 再测 TTS                              ← 若这里失败 = 扫描把总线搞坏了
    [5] 测 ASR（0x4F）

用法：断电重启 -> 等 2 分钟 -> python3 minimal_test.py
"""

import sys
import time

TTS_VERSION = "V2"
TTS_VOLUME = 5
REPLY_TEXT = "你好"


def log(m):
    print(m)
    sys.stdout.flush()


def step(n, title):
    log("\n" + "-" * 52)
    log("[%d] %s" % (n, title))
    log("-" * 52)


def main():
    log("=" * 52)
    log(" 最小化测试：扫描会不会把总线搞坏？")
    log("=" * 52)

    from pinpong.board import Board, I2C
    Board("UNIHIKER").begin()
    time.sleep(1.0)

    # ---------- 先建 TTS 对象并初始化（不做任何扫描）----------
    tts = None

    def tts_test(tag):
        if tts is None:
            log("    (TTS 未初始化，跳过)")
            return False
        try:
            t0 = time.time()
            tts.speak(REPLY_TEXT)
            log("    ✅ %s：播报成功，耗时 %.1fs" % (tag, time.time() - t0))
            return True
        except BaseException as e:
            log("    ❌ %s：失败 %r" % (tag, e))
            return False

    step(1, "直接初始化 TTS 并念「%s」（不做任何扫描）" % REPLY_TEXT)
    try:
        from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
        tts = DFRobot_SpeechSynthesis_I2C()
        tts.begin(tts.V1 if TTS_VERSION == "V1" else tts.V2)
        tts.set_voice(TTS_VOLUME)
        log("    ✅ begin 成功")
        tts_test("首次播报")
    except BaseException as e:
        log("    ❌ TTS 初始化失败：%r" % (e,))
        log("    → 连「不扫描直接测」都不通，说明问题不在扫描")
        return

    step(2, "再念一次（确认稳定）")
    tts_test("第二次播报")

    step(3, "全地址扫描（唯一的重负载操作）")
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

    step(4, "扫描之后再测 TTS（关键！）")
    ok_after = tts_test("扫描后播报")
    if ok_after:
        log("")
        log("    >>> 结论：扫描**没有**搞坏总线，假设不成立。")
        log("    >>> 那 20:28 那次失败是别的原因。")
    else:
        log("")
        log("    >>> 结论：**全地址扫描把总线搞坏了**！")
        log("    >>> 以后调试一律不要做全地址扫描，直接测目标模块。")

    step(5, "测 ASR（0x4F）")
    try:
        from pinpong.libs.dfrobot_asr import DFRobot_ASR
        asr = DFRobot_ASR()
        asr.begin(asr.LOOP, asr.MIC)
        asr.add_command("xiao yi xiao yi", 1)
        asr.start()
        log("    ✅ ASR 初始化成功")
        vals = []
        for _ in range(6):
            vals.append(asr.read())
            time.sleep(0.2)
        log("    读取：%s" % vals)
    except BaseException as e:
        log("    ❌ ASR 失败：%r" % (e,))

    log("\n" + "=" * 52)
    log(" done.")
    log("=" * 52)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已退出。")
