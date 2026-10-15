# -*- coding: utf-8 -*-
"""
对照实验：0x41 之后要不要 sleep(0.1)？
=====================================

已定位：主程序没声音是因为**我们自己写的 _speak()** 有问题
（同一个进程里，我们的 `_speak` 秒回 0.0s，而库自带的 `speak()` 正常 1.62s）。

差异点：库的 `wait()` 在检测到 0x41「开始合成」之后会固定 `time.sleep(0.1)`，
再开始轮询状态；我们的实现没有这一步，**立刻就把状态查询帧发了出去**，
怀疑这样会把刚起步的合成打断，模块于是马上回到空闲（表现为「播完 0.0s」且没声音）。

本脚本交替跑「无 guard / 有 guard」各两轮，看耗时就能判定。

用法（板上，本次开机第一个碰 I2C 的进程）：
    timeout 90 python3 tts_probe4.py
"""

import time

TEXT = "你好"


def ack(t):
    try:
        d = t.read_ack(1)
        return d[0] if d else None
    except Exception:
        return None


def speak_variant(t, text, guard):
    """guard=True 时复刻库的行为：0x41 之后 sleep(0.1) 再开始轮询状态。"""
    data = list(text.encode("gb2312"))
    n = len(data) + 2
    t.write_cmd([0xFD, (n >> 8) & 0xFF, n & 0xFF, 0x01, 0x00] + data)

    t0 = time.monotonic()
    started = False
    while time.monotonic() - t0 < 3.0:
        if ack(t) == 0x41:
            started = True
            break
        time.sleep(0.02)
    if not started:
        return None
    if guard:
        time.sleep(0.1)                      # ← 待验证的关键一步
    while time.monotonic() - t0 < 30.0:
        t.write_cmd([0xFD, 0x00, 0x01, 0x21])
        if ack(t) == 0x4F:
            return time.monotonic() - t0
        time.sleep(0.02)
    return None


def main():
    from pinpong.board import Board
    from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C

    Board().begin()
    t = DFRobot_SpeechSynthesis_I2C()
    t.begin(t.V2)
    t.set_voice(9)
    print("[p] 就绪，开始对照（音量 9，注意听）")

    for i, guard in enumerate([False, True, False, True]):
        t0 = time.monotonic()
        r = speak_variant(t, TEXT, guard)
        print("[p] 第%d轮 guard=%-5s -> %s   (实测 %.2fs)"
              % (i + 1, guard,
                 ("%.2fs" % r) if r is not None else "超时",
                 time.monotonic() - t0))
        time.sleep(1.0)

    t0 = time.monotonic()
    t.speak(TEXT)
    print("[p] 对照：库自带 speak() -> %.2fs" % (time.monotonic() - t0))
    print("[p] 完成")


if __name__ == "__main__":
    main()
