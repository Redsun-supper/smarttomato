# -*- coding: utf-8 -*-
# HUSKYLENS connection self-check for UNIHIKER M10
# 用法：把本文件拷到板子（或直接跑 /root/check_husky.py）
#       python3 check_husky.py
#
# 依次检查三件事：
#   1. I2C 总线空闲电平（P19=SCL / P20=SDA），判断线序是否接反
#   2. 全地址扫描，看有没有设备应答
#   3. 用 pinpong 自带的 Huskylens 驱动握手一次，读一帧人脸框数量
import time

OK = "[ OK ]"
BAD = "[FAIL]"


def main():
    from pinpong.board import Board, I2C, Pin
    Board("UNIHIKER").begin()
    time.sleep(1.0)

    print("=" * 48)
    print(" HUSKYLENS self-check on UNIHIKER M10")
    print("=" * 48)

    # ---- 1. bus idle level -------------------------------------------
    scl = Pin(Pin.P19, Pin.IN)
    sda = Pin(Pin.P20, Pin.IN)
    time.sleep(0.2)
    s, d = [], []
    for _ in range(40):
        s.append(scl.value())
        d.append(sda.value())
        time.sleep(0.01)
    scl_hi, sda_hi = s.count(1), d.count(1)
    print("\n[1] Bus idle level  (P19 = SCL, P20 = SDA)")
    print("    SCL high %d/40 | SDA high %d/40" % (scl_hi, sda_hi))
    if scl_hi == 40 and sda_hi == 40:
        print(OK + " both lines high, bus idle is healthy")
    elif scl_hi == 0 and sda_hi == 40:
        print(BAD + " SCL stuck LOW while SDA is HIGH")
        print("      -> typical of SDA/SCL swapped, or slave holding SCL")
        print("      -> try swapping the blue and green wires")
    elif scl_hi == 0 and sda_hi == 0:
        print(BAD + " both lines LOW -> no pull-up, wire not connected?")
    else:
        print("    note: lines toggling, bus may be busy")

    # ---- 2. scan ------------------------------------------------------
    i2c = I2C(bus_num=0)
    found = []
    for addr in range(0x08, 0x78):
        try:
            i2c.readfrom(addr, 1)
            found.append(addr)
        except Exception:
            try:
                i2c.writeto(addr, b'\x00')
                found.append(addr)
            except Exception:
                pass
    print("\n[2] I2C scan  (0x08 - 0x77, pinpong bus_num=0)")
    if found:
        print(OK + " responded: " + ", ".join("0x%02X" % a for a in found))
    else:
        print(BAD + " no device responded at all")
    print("     0x32 = HUSKYLENS, 0x4F = voice ASR, 0x40 = TTS")

    # ---- 3. huskylens handshake --------------------------------------
    print("\n[3] HUSKYLENS driver handshake (addr 0x32)")
    try:
        from pinpong.libs.dfrobot_huskylens import Huskylens
        hl = Huskylens(i2c_addr=0x32, bus_num=0)
        time.sleep(0.3)
        hl.command_request_algorthim("ALGORITHM_FACE_RECOGNITION")
        time.sleep(5.5)          # 切算法后官方建议等约 5s
        n = hl.read_count("blocks", 0)
        print(OK + " connected, face blocks = %s" % n)
    except Exception as e:
        print(BAD + " not connected: %r" % (e,))
        print("      -> set Protocol Type = I2C in the HuskyLens menu,")
        print("         save, then power-cycle the module")

    print("\ndone.")


if __name__ == "__main__":
    main()
