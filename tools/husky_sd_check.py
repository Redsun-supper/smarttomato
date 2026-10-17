# -*- coding: utf-8 -*-
"""验证二哈模块（I2C 0x32）的 TF 卡能否写入。

库里 command_request_save_model_to_SD_card() 只发命令、不读回执，光看代码
判断不了卡行不行，所以这里自己发原始命令并读回响应包。

流程：
  1. 读一次原始包，确认模块在线
  2. 请求 blocks（0x21），确认能正常应答
  3. 发「拍照存 SD 卡」（0x30）
  4. 发「保存模型到 SD 卡 · 槽位 1」（0x32）
  5. 打印所有原始 hex

判读：跑完把 TF 卡拔下来插到电脑上，看根目录有没有新出现的 .bmp / 模型文件。
      有   -> 卡完全可用；
      没有 -> 多半是文件系统不对（二哈要求 FAT32）。
"""
import time

ADDR = 0x32


def csum(body):
    return sum(body) & 0xFF


def hexs(b):
    return " ".join("%02X" % x for x in b)


def read_raw(i2c, n=16):
    try:
        return bytes(i2c.readfrom_mem(ADDR, 0, n))
    except Exception as e:
        return ("ERR(%s)" % e).encode()


def send(i2c, body):
    cmd = body + [csum(body)]
    i2c.writeto(ADDR, cmd)
    return cmd


def main():
    from pinpong.board import Board
    Board("UNIHIKER").begin()
    time.sleep(1.0)

    from pinpong.libs.dfrobot_huskylens import Huskylens
    hl = Huskylens(i2c_addr=ADDR, bus_num=0)
    i2c = hl.i2c
    time.sleep(0.5)

    print("=" * 52)
    print(" 二哈 TF 卡写入验证")
    print("=" * 52)

    print("\n[1] 上电后原始包 :", hexs(read_raw(i2c, 16)))

    print("[2] 发请求 blocks :", hexs(send(i2c, [0x55, 0xAA, 0x11, 0x00, 0x21])))
    time.sleep(0.3)
    print("    响应         :", hexs(read_raw(i2c, 16)))

    print("\n[3] 发拍照存卡   :", hexs(send(i2c, [0x55, 0xAA, 0x11, 0x00, 0x30])))
    time.sleep(2.0)
    print("    响应         :", hexs(read_raw(i2c, 16)))

    print("\n[4] 发存模型槽1  :",
          hexs(send(i2c, [0x55, 0xAA, 0x11, 0x02, 0x32, 0x01, 0x00])))
    time.sleep(2.0)
    print("    响应         :", hexs(read_raw(i2c, 16)))

    print("\n完成。请把 TF 卡拔出插到电脑上，看根目录有没有新文件。")


if __name__ == "__main__":
    main()
