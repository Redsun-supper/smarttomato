# -*- coding: utf-8 -*-
"""
对照探针：直接跑主程序的 Speaker / VoiceASR，定位「我们的 _speak 有问题」还是「环境有问题」
=========================================================================================

前面已经排除：
    * TTS 单独跑 → 正常（1.6s）
    * 建了 ASR 再跑 → 正常
    * ASR 线程 50ms 轮询时跑 → 正常

所以嫌疑集中在**我们自己的代码路径**（`m10_step2_focus.Speaker._speak`，
以及「先建 ASR 再建 TTS」这个顺序）。

本脚本直接 import 主程序模块，做三件事：
    ① 调主程序的 init_voice()（ASR 先、TTS 后，和主程序完全一致）
    ② 用**主程序的** Speaker.say() 播三句（走我们自己的 _speak）
    ③ 再用**库自带的** speak() 播一句作对照

用法（板上，本次开机第一个碰 I2C 的进程）：
    timeout 120 python3 tts_probe3.py
"""

import sys
import time

sys.path.insert(0, "/root/smarttomato")


def main():
    import m10_step2_focus as m

    print("[p] 调主程序的 init_voice()（ASR 先 / TTS 后）...")
    asr, spk = m.init_voice(25)
    print("[p] init 完成：asr=%s  speaker=%s" % (asr is not None, spk is not None))
    if spk is None:
        print("[p] 合成模块没起来，后面没法测")
        return

    print("[p] ---- ① 走主程序自己的 Speaker.say()（我们实现的 _speak）----")
    for s in ["你好", "开始专注", "好的，开始专注25分钟"]:
        spk.say(s)
        time.sleep(7)

    print("[p] ---- ② 对照：直接调库自带的 speak() ----")
    t0 = time.monotonic()
    try:
        spk._tts.speak("你好")
        print("[p] 库 speak(你好) 耗时 %.2fs" % (time.monotonic() - t0))
    except BaseException as e:
        print("[p] 库 speak 异常：%r" % e)

    print("[p] 完成")


if __name__ == "__main__":
    main()
