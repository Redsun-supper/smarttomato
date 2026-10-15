# -*- coding: utf-8 -*-
# M10 I2C bus quick check
# 用法：板子上执行  python3 /root/smarttomato/i2c_check.py
# 注意顺序：先 Pin 再 I2C —— 同一进程内反序会触发 pinpong 的 io remap error
import time

KNOWN = {
    0x32: "HUSKYLENS  (vision)",
    0x40: "DFR0760    (TTS)",
    0x4F: "DFR0715    (ASR)",
}


def main():
    from pinpong.board import Board
    Board("UNIHIKER").begin()
    time.sleep(1.0)

    # ---- 1. 总线空闲电平（判断线序 / 是否被从机拉住）----
    from pinpong.board import Pin
    scl = Pin(Pin.P19, Pin.IN)
    sda = Pin(Pin.P20, Pin.IN)
    time.sleep(0.2)
    s, d = [], []
    for _ in range(40):
        s.append(scl.value()); d.append(sda.value())
        time.sleep(0.01)
    sh, dh = s.count(1), d.count(1)

    print("=" * 50)
    print(" M10 I2C bus check  (P19=SCL, P20=SDA)")
    print("=" * 50)
    print("\n[1] Bus level")
    print("    SCL high %d/40 | SDA high %d/40" % (sh, dh))
    if sh == 40 and dh == 40:
        print("    OK   bus idle is healthy")
    elif sh == 0 and dh == 40:
        print("    FAIL SCL is stuck LOW, SDA is HIGH")
        print("         -> SDA/SCL swapped, or a module is holding SCL")
        print("         -> power off, unplug ALL modules, retry")
    elif sh == 0 and dh == 0:
        print("    FAIL both LOW -> no pull-up / wiring problem")
    else:
        print("    NOTE lines toggling, bus busy")

    # ---- 2. 全地址扫描 ----
    from pinpong.board import I2C
    i2c = I2C(bus_num=0)
    found = []
    for a in range(0x08, 0x78):
        try:
            i2c.readfrom(a, 1); found.append(a)
        except Exception:
            try:
                i2c.writeto(a, b'\x00'); found.append(a)
            except Exception:
                pass

    print("\n[2] Devices found on 4-pin I2C port")
    if not found:
        print("    none")
    for a in found:
        print("    0x%02X  %s" % (a, KNOWN.get(a, "unknown")))
    print("\n    (not found: " + ", ".join(
        "0x%02X %s" % (k, KNOWN[k].split()[0]) for k in sorted(KNOWN) if k not in found) + ")")
    print("\ndone.")


if __name__ == "__main__":
    main()
