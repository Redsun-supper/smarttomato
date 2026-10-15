# -*- coding: utf-8 -*-
"""
DFR0760 语音合成模块 —— UART 驱动（行空板 M10）
================================================

为什么需要这个
--------------
本项目已实测：「语音识别(0x4F) + 语音合成(0x40)」挂在同一条 4Pin I2C 上会失败
（一执行 `tts.begin()` 就报通信失败，随后整条总线被拉死，必须断电重启）。

但 **DFR0760 板上自带 I2C / UART 模式开关**（丝印 `D/T` 和 `C/R`，即
`I2C_SDA / UART_TX` 和 `I2C_SCL / UART_RX`）。把开关拨到 UART 方向，
再把模块接到板子的**串口**上，就让 TTS 彻底离开 I2C 总线：

    语音识别 DFR0715  →  4Pin I2C (0x4F)        ← 老样子
    语音合成 DFR0760  →  串口（见下）           ← 换成这条，零冲突

协议与 I2C 版**完全一致**（只是换了个传输层），波特率 **115200**。

⚠️ 重要：光拨开关是不够的
------------------------
DFR0760 的 4Pin 接口在两种模式下引脚定义不同，**必须把线也换到串口上**：

| 模块丝印 | I2C 模式 | UART 模式 |
|---|---|---|
| `D/T` | SDA | **TX** |
| `C/R` | SCL | **RX** |

只拨开关、线还插在 4Pin I2C 口上 → 模块的 TX 会去顶板子的 SDA，
**两条总线都通不了**（实测：I2C 扫描全空、UART 也收不到任何字节）。

两条接线路径
------------
【路径 1】板子硬串口 `/dev/ttySP0`（P0=RX / P3=TX，在**金手指**上）
    模块 D/T (TX) → 板子 P0 (RX)
    模块 C/R (RX) → 板子 P3 (TX)
    ⚠️ 必须用 `--pinpong`（默认自动开）：pinpong 的 `UART()` 会把 P0/P3
       复用成 UART 功能（unihi.py 里 `pinReuse(202, STATE_UART)`），
       只用 pyserial 直接打开 `/dev/ttySP0` 是收不到数据的。
    ⚠️ 首次调用 pinpong UART 会触发一次 GD32 协处理器固件校验/重刷，
       流程里有 "kill all process"，**会把当前脚本杀掉**。看到
       "start update gd32 firmware" 就等它刷完再跑一次，第二次就正常了。

【路径 2】USB-A 口 + USB-TTL 转串口（最省事，不用碰金手指）
    模块 D/T (TX) → 转串口模块 RX
    模块 C/R (RX) → 转串口模块 TX
    GND 共地；插上后板子会出现 `/dev/ttyUSB0`，用纯 pyserial 即可。

用法
----
    python3 dfr0760_uart.py --list                # 列出候选串口
    python3 dfr0760_uart.py                       # 自动找串口，念「你好」
    python3 dfr0760_uart.py --port /dev/ttySP0    # 硬串口（自动走 pinpong）
    python3 dfr0760_uart.py --port /dev/ttyUSB0   # USB-TTL
    python3 dfr0760_uart.py --text "你好，我是智能番茄时钟"

依赖：pyserial（pinpong 依赖它，板子上通常已有）
"""

import argparse
import glob
import os
import sys
import time

try:
    import serial
except ImportError:
    print("缺少 pyserial，请先执行：pip install pyserial")
    sys.exit(1)

# ============================ 协议常量 ============================

DEFAULT_BAUD = 115200          # DFR0760 UART 官方示例即 115200
DEFAULT_PORT = "/dev/ttySP0"   # M10 串口：P0=RX / P3=TX
DEFAULT_VOLUME = 5             # 音量 0~9

HEAD = 0xFD
CMD_SPEAK = 0x01
CMD_INQUIRY_STATUS = 0x21
ACK_START = 0x41               # 模块回 ASCII 'A'：开始合成
ACK_IDLE = 0x4F                # 模块回 ASCII 'O'：空闲/完成

PORT_PATTERNS = (
    "/dev/ttySP*",             # M10 串口
    "/dev/ttyUSB*",            # USB-TTL 转串口
    "/dev/ttyACM*",
    "/dev/ttyAMA*",
)


def log(msg):
    print(msg)
    sys.stdout.flush()


def list_ports():
    found = []
    for pat in PORT_PATTERNS:
        for p in sorted(glob.glob(pat)):
            if p not in found:
                found.append(p)
    return found


def autodetect_port():
    for p in list_ports():
        if "/ttyUSB" in p or "/ttyACM" in p:
            return p
    if os.path.exists(DEFAULT_PORT):
        return DEFAULT_PORT
    ports = list_ports()
    return ports[0] if ports else None


# ============================ 传输层 ============================

class SerialTransport(object):
    """纯 pyserial 传输。适合 USB-TTL 转串口（/dev/ttyUSB0）这类不需要复用的口。"""

    def __init__(self, port, baudrate, timeout=0.5):
        self.port = port
        self.baudrate = baudrate
        self.ser = serial.Serial(
            port=port, baudrate=baudrate, bytesize=8,
            parity="N", stopbits=1, timeout=timeout,
        )

    def reset_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def write(self, data):
        self.ser.write(bytearray(data))

    def read(self, n):
        b = self.ser.read(n)
        return b if b else b""

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class PinpongTransport(object):
    """走 pinpong 的 UART 类（`/dev/ttySP0`）。

    必须走这一条：pinpong 的 `UART()` 会把 P0/P3 复用成 UART 功能，
    只用 pyserial 直接打开 `/dev/ttySP0` 是收不到数据的。
    副作用见文件头说明（首次会触发 GD32 固件重刷并把脚本杀掉）。
    """

    def __init__(self, port, baudrate, timeout=0.5):
        from pinpong.board import Board, UART
        Board("UNIHIKER").begin()
        self.u = UART(None, port)
        self.u.init(baud_rate=baudrate)
        self.port = port
        self.baudrate = baudrate

    def reset_input(self):
        try:
            n = self.u.any()
            if n:
                self.u.read(n)
        except Exception:
            pass

    def write(self, data):
        self.u.write(bytearray(data))

    def read(self, n):
        try:
            d = self.u.read(n)
            return d if d else b""
        except Exception:
            return b""

    def close(self):
        try:
            self.u.deinit()
        except Exception:
            pass


# ============================ 驱动 ============================

class DFR0760_UART(object):
    """DFR0760（UART 模式）语音合成驱动。

    帧格式（与 I2C 版完全相同）：
        合成语音 : FD <len_hi> <len_lo> 01 00 <GB2312 文本字节...>
                   len = len(文本字节) + 2
        查状态   : FD 00 01 21
        设音量   : FD 00 06 01 00 '[' 'v' <0~9> ']'   （即文本 "[v5]"）
    模块用 ASCII 'A' 表示开始合成、'O' 表示空闲。
    """

    def __init__(self, port=DEFAULT_PORT, baudrate=DEFAULT_BAUD, timeout=0.5,
                 use_pinpong=None):
        # 默认策略：/dev/ttySP* 走 pinpong（需要复用引脚），其余走纯 pyserial
        if use_pinpong is None:
            use_pinpong = str(port).startswith("/dev/ttySP")
        cls = PinpongTransport if use_pinpong else SerialTransport
        self.transport_name = cls.__name__
        self.tp = cls(port, baudrate, timeout)
        self.port = port
        self.baudrate = baudrate
        self.tp.reset_input()

    # ---------- 底层 ----------

    def write_cmd(self, data):
        """写一帧。官方实现写完固定 sleep 0.1s 给模块处理时间。"""
        self.tp.write(data)
        time.sleep(0.1)

    def read_ack(self):
        """读 1 字节并翻译成 ACK 码。超时返回 None。

        模块回的是 ASCII：'A' -> 0x41（开始合成），'O' -> 0x4F（空闲/完成）。
        """
        b = self.tp.read(1)
        if not b:
            return None
        if b == b"A":
            return ACK_START
        if b == b"O":
            return ACK_IDLE
        return b[0]

    def any(self):
        """返回当前可读字节数（仅 pinpong 传输支持，pyserial 返回 0/未知）。"""
        try:
            return self.tp.u.any()
        except Exception:
            return 0

    # ---------- 高层 ----------

    def wait(self, timeout=10.0):
        """等待播报结束。官方实现是死循环，这里加了超时，避免模块异常时永久卡住。"""
        # 阶段一：等 'A'（开始合成）
        deadline = time.time() + timeout
        got_a = False
        while time.time() < deadline:
            if self.read_ack() == ACK_START:
                got_a = True
                break
            time.sleep(0.02)

        time.sleep(0.1)
        # 阶段二：轮询状态帧，等 'O'（合成完成）
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.write_cmd([HEAD, 0x00, 0x01, CMD_INQUIRY_STATUS])
            if self.read_ack() == ACK_IDLE:
                return True
            time.sleep(0.02)
        return got_a and False

    def speak(self, text, timeout=10.0):
        """合成并播放一段中文/英文/数字。返回 True/False。"""
        data = list(text.encode("gb2312"))
        length = len(data) + 2
        frame = [HEAD, (length >> 8) & 0xFF, length & 0xFF, CMD_SPEAK, 0x00] + data
        self.write_cmd(frame)
        return self.wait(timeout)

    def set_voice(self, voc):
        """设置音量 0~9。"""
        voc = max(0, min(9, int(voc)))
        frame = [HEAD, 0x00, 0x06, CMD_SPEAK, 0x00, ord("["), ord("v"), 48, ord("]")]
        frame[7] = 48 + voc
        self.write_cmd(frame)
        self.wait()

    def set_speed(self, speed):
        """设置语速 0~9。"""
        speed = max(0, min(9, int(speed)))
        frame = [HEAD, 0x00, 0x06, CMD_SPEAK, 0x00, ord("["), ord("s"), 48, ord("]")]
        frame[7] = 48 + speed
        self.write_cmd(frame)
        self.wait()

    def set_tone(self, tone):
        """设置音调 0~9。"""
        tone = max(0, min(9, int(tone)))
        frame = [HEAD, 0x00, 0x06, CMD_SPEAK, 0x00, ord("["), ord("t"), 48, ord("]")]
        frame[7] = 48 + tone
        self.write_cmd(frame)
        self.wait()

    def reset(self):
        """恢复默认设置（等价于念 "[d]"）。"""
        return self.speak("[d]")

    def close(self):
        self.tp.close()


# ============================ 自检 ============================

def main():
    ap = argparse.ArgumentParser(description="DFR0760 UART 语音合成自检")
    ap.add_argument("--port", default=None, help="串口设备，默认自动识别")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="波特率，默认 115200")
    ap.add_argument("--text", default="你好", help="要合成的内容")
    ap.add_argument("--volume", type=int, default=DEFAULT_VOLUME, help="音量 0~9")
    ap.add_argument("--list", action="store_true", help="只列出候选串口")
    ap.add_argument("--pinpong", dest="pinpong", action="store_true", default=None,
                    help="强制用 pinpong UART 初始化（复用 P0/P3）")
    ap.add_argument("--no-pinpong", dest="pinpong", action="store_false",
                    help="强制用纯 pyserial")
    args = ap.parse_args()

    log("=" * 52)
    log(" DFR0760 UART 语音合成自检")
    log("=" * 52)

    ports = list_ports()
    log("\n[1] 候选串口")
    if ports:
        for p in ports:
            log("    %s" % p)
    else:
        log("    没找到任何串口设备")

    if args.list:
        return

    port = args.port or autodetect_port()
    if port is None:
        log("\n[FAIL] 没有可用串口。请检查：")
        log("       * 模块开关是否拨到了 UART 方向")
        log("       * 线是否也换到了串口上（D/T->RX，C/R->TX）")
        log("       * 或改用 USB-TTL 转串口接 USB-A 口")
        return
    log("\n[2] 使用串口 %s @ %d 8N1" % (port, args.baud))

    try:
        tts = DFR0760_UART(port=port, baudrate=args.baud, use_pinpong=args.pinpong)
    except Exception as e:
        log("[FAIL] 打开串口失败：%r" % (e,))
        log("       * 串口被占用？用 fuser %s 看谁在用" % port)
        log("       * 权限不足？试试 sudo")
        return
    log("[ OK ] 串口已打开（传输层：%s）" % tts.transport_name)

    log("\n[3] 先探一下模块是否在线（状态查询帧）")
    tts.tp.reset_input()
    tts.write_cmd([0xAA])
    tts.write_cmd([HEAD, 0x00, 0x01, CMD_INQUIRY_STATUS])
    time.sleep(0.3)
    n = tts.any()
    log("    可读字节数 = %d" % n)
    if n:
        log("[ OK ] 模块有应答，链路正常")
    else:
        log("[WARN] 模块没有任何应答。可能：")
        log("       * 开关没拨到 UART，或线还插在 4Pin I2C 口上")
        log("       * TX/RX 接反（D/T 是模块 TX，要接板子 RX）")
        log("       * 波特率不对，试试 --baud 9600")

    log("\n[4] 设音量 = %d" % args.volume)
    try:
        tts.set_voice(args.volume)
    except Exception as e:
        log("[FAIL] 设置音量失败：%r" % (e,))

    log("\n[5] 合成「%s」" % args.text)
    t0 = time.time()
    ok = tts.speak(args.text)
    if ok:
        log("[ OK ] 播报完成，耗时 %.1fs" % (time.time() - t0))
    else:
        log("[FAIL] 等不到模块应答（超时）。")

    tts.close()
    log("\ndone.")


if __name__ == "__main__":
    main()
