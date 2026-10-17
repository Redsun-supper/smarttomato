# -*- coding: utf-8 -*-
"""诊断2：请求 blocks 后读 64 字节，拆出响应流里**所有**的包（0x29 + 可能的 0x2a）。"""
import time

from pinpong.board import Board
Board().begin()

from pinpong.libs.dfrobot_huskylens import Huskylens
hl = Huskylens(i2c_addr=0x32, bus_num=0)
i2c = hl.i2c

HL_ADDR = 0x32
REQ = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]

CMD_NAME = {0x29: "RETURN_INFO", 0x2a: "RETURN_BLOCK", 0x2b: "RETURN_ARROW",
            0x2e: "RETURN_OK", 0x2c: "KNOCK"}


def hexs(b):
    return " ".join("%02X" % x for x in list(b))


def split_packets(raw):
    """把一段字节里连续的 55 AA 包拆出来。"""
    pkts, i = [], 0
    while i + 5 <= len(raw):
        if raw[i] == 0x55 and raw[i + 1] == 0xAA:
            datalen = raw[i + 3]
            end = i + 5 + datalen + 1
            if end <= len(raw):
                pkts.append(raw[i:end])
                i = end
                continue
        i += 1
    return pkts


print("--- 切算法（等 5.5 秒）---")
hl.command_request_algorthim("ALGORITHM_OBJECT_CLASSIFICATION")
time.sleep(5.5)

print("--- 请求 blocks 后读 64 字节，连读 4 次 ---")
for k in range(4):
    i2c.writeto(HL_ADDR, REQ)
    time.sleep(0.1)
    raw = i2c.readfrom_mem(HL_ADDR, 0, 64)
    print("[%d] 全 64 字节: %s" % (k, hexs(raw)))
    for p in split_packets(raw):
        cmd = p[4]
        data = list(p[5:5 + p[3]])
        print("    包 cmd=0x%02X(%s) datalen=%d data=%s"
              % (cmd, CMD_NAME.get(cmd, "?"), p[3], hexs(data)))
    time.sleep(0.8)
