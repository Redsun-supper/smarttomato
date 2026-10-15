# -*- coding: utf-8 -*-
"""
DFR0760 语音合成单独自检（不初始化 ASR，排除共存干扰）
=====================================================

用途：程序里「日志有『待播：xxx』但没声音」时，用这个把合成单独拎出来测。

用法（在板上，**作为本次开机第一个碰 I2C 的进程**跑）：
    timeout 60 python3 tts_probe.py

要点：
  * `begin(V2)` 结尾库会自己 `set_voice(1)`，所以必须再 `set_voice(5)` 补一刀。
  * 每次 speak 前后都打一下模块状态（0x4F=空闲 / 0x41=正在合成），
    就能看出模块到底是「压根没开始」还是「开始了但没声音」。
  * 用 `timeout 60` 兜底：库里的 `wait()` 是没有超时的死循环，卡住会被杀掉。
"""

import time


def status(t):
    """读模块状态：0x4F=空闲，0x41=正在合成。失败返回 None。"""
    try:
        t.write_cmd([0xFD, 0x00, 0x01, 0x21])
        d = t.read_ack(1)
        return d[0] if d else None
    except Exception as e:
        print("[probe] 读状态失败：%r" % e)
        return None


def main():
    from pinpong.board import Board
    from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C

    Board().begin()
    t = DFRobot_SpeechSynthesis_I2C()

    print("[probe] begin(V2) ...")
    t.begin(t.V2)
    st = status(t)
    print("[probe] begin 完成，状态 = %s" % (hex(st) if st is not None else "无应答"))

    t.set_voice(5)
    st = status(t)
    print("[probe] set_voice(5) 完成，状态 = %s" % (hex(st) if st is not None else "无应答"))

    tests = ["你好", "开始专注", "好的，开始专注25分钟"]
    for s in tests:
        before = status(t)
        t0 = time.monotonic()
        try:
            t.speak(s)
            dt = time.monotonic() - t0
            after = status(t)
            print("[probe] speak(%-12s) 耗时 %5.2fs  前=%s 后=%s"
                  % (s, dt,
                     hex(before) if before is not None else "无",
                     hex(after) if after is not None else "无"))
        except BaseException as e:
            print("[probe] speak(%s) 异常：%r" % (s, e))
        time.sleep(0.5)

    print("[probe] 全部完成")


if __name__ == "__main__":
    main()
