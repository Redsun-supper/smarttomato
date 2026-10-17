# -*- coding: utf-8 -*-
"""诊断：物体分类模式下二哈回的原始字节（临时脚本，排查读包失败用）。"""
import time

from pinpong.board import Board
Board().begin()

from pinpong.libs.dfrobot_huskylens import Huskylens
hl = Huskylens(i2c_addr=0x32, bus_num=0)
i2c = hl.i2c

HL_ADDR = 0x32
REQ = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]


def hexs(b, n=16):
    return " ".join("%02X" % x for x in list(b)[:n])


print("--- 重新切一次物体分类（等 5.5 秒）---")
hl.command_request_algorthim("ALGORITHM_OBJECT_CLASSIFICATION")
time.sleep(5.5)

print("--- 发 0x21 后读 16 字节，连读 5 次 ---")
for i in range(5):
    i2c.writeto(HL_ADDR, REQ)
    time.sleep(0.05)
    raw = i2c.readfrom_mem(HL_ADDR, 0, 16)
    print("[%d] %s" % (i, hexs(raw)))
    time.sleep(1.0)

print("--- 库读法 read_count('blocks') 对照 ---")
try:
    r = hl.read_count("blocks")
    print("返回类型 %s：%r" % (type(r).__name__, r))
except Exception as e:
    print("库读法异常：%s" % e)

print("--- 再发一次请求读 ---")
i2c.writeto(HL_ADDR, REQ)
time.sleep(0.05)
print("raw: %s" % hexs(i2c.readfrom_mem(HL_ADDR, 0, 16)))
