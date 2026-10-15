# -*- coding: utf-8 -*-
"""
DFR0760 合成「分段排查」探针：到底是哪一步把 TTS 搞哑的
=========================================================

背景：单独跑 TTS 完全正常（`speak("你好")` 1.6s、目标句 3.6s），
但一放进主程序（同时有 ASR）就没声音、且模块秒回 0x41→0x4F（等于没合成）。

本脚本按顺序做四组对照，把变量一个个加上去：

    ① 只有 TTS
    ② 加一个 ASR 对象（不轮询）
    ③ 再起一个 ASR 轮询线程（每 50ms 读一次）
    ④ 停掉轮询线程，再测一次

哪一组开始「耗时骤降 / 模块秒回」= 元凶就是它。

用法（在板上，**作为本次开机第一个碰 I2C 的进程**跑）：
    timeout 120 python3 tts_probe2.py
"""

import threading
import time

# 音量拉到最大，方便用耳朵确认到底有没有出声
VOLUME = 9


def timed(label, fn):
    t0 = time.monotonic()
    try:
        fn()
        dt = time.monotonic() - t0
        print("[t] %-36s %5.2fs %s" % (label, dt, "" if dt > 0.5 else "  <== 太快了，没合成！"))
        return dt
    except BaseException as e:
        print("[t] %-36s 异常 %r" % (label, e))
        return -1


def main():
    from pinpong.board import Board
    from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C

    Board().begin()

    t = DFRobot_SpeechSynthesis_I2C()
    t.begin(t.V2)
    t.set_voice(VOLUME)
    print("[t] TTS 就绪，音量 %d" % VOLUME)

    # ---------- ① 只有 TTS ----------
    timed("① 只有 TTS", lambda: t.speak("你好"))

    # ---------- ② 加一个 ASR 对象（先不轮询）----------
    from pinpong.libs.dfrobot_asr import DFRobot_ASR
    a = DFRobot_ASR()
    a.begin(a.LOOP, a.MIC)
    a.add_command("kai shi xue xi", 1)
    a.start()
    print("[t] ASR 已建好（还没轮询）")
    time.sleep(0.5)
    timed("② 建了 ASR、但没轮询", lambda: t.speak("你好"))

    # ---------- ③ 起一个 ASR 轮询线程 ----------
    stop = threading.Event()

    def poll():
        n = 0
        while not stop.is_set():
            try:
                a.read()
                n += 1
            except Exception:
                pass
            stop.wait(0.05)

    th = threading.Thread(target=poll)
    th.daemon = True
    th.start()
    time.sleep(0.5)
    timed("③ ASR 线程在轮询（50ms 一次）", lambda: t.speak("你好"))

    # ---------- ④ 停掉轮询，再测 ----------
    stop.set()
    time.sleep(0.5)
    timed("④ 停掉 ASR 轮询之后", lambda: t.speak("你好"))

    print("[t] 完成")


if __name__ == "__main__":
    main()
