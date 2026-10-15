# 硬件与驱动速查（smarttomato）

> `MEMORY.md` 只留结论与硬约束；**模块规格、驱动 API、协议细节都在这里**。
> 涉及语音 / 识图 / 总线的排查，先读这一份。

---

## 0. 总览：三个 I2C 设备共用一条 I2C1

| 设备 | 地址 | 通信 | 供电 |
|---|---|---|---|
| 语音识别 DFR0715（LD3320） | **0x4F**（固定，**无开关**） | **仅 I2C** | 3.3~5V / 25~40mA |
| 语音合成 DFR0760（V2） | **0x40**（I2C 模式） | I2C / UART（**板上有 `D/T`-`C/R` 模式开关**） | 3.3~5V / 160mA |
| 二哈识图 1 代 SEN0305 | **0x32** | I2C / UART，默认 Auto | 3.3–5.0V / 320mA@3.3V |

合计约 400mA，4Pin 口带得动。**三者同时在线、同时通信已实测正常**（2026-10-13 / 10-14）。

⚠️ DFR0760 若开关在 UART 档却插在 I2C 口上，模块 TX（推挽）会顶板子 SDA → 必然冲突；
**改开关后必须重新插拔杜邦线**（官方 FAQ）。
⚠️ UART 备选路径已具备（`tools/dfr0760_uart.py`），协议帧与 I2C 完全一致、115200 8N1，暂不需要。

---

## 1. 语音识别 DFR0715（LD3320）

- 板载驻极体麦 + 3.5mm 麦口 + 3.5mm 音频输入。
- ⚠️ **没有模式 / 拨码开关**（早期"拨码到 I2C"的说法是针对 SEN0539，不适用本模块）。
- 板载灯：**红色电源 LED** + RGB 模式灯（蓝=循环 / 绿=按键 / 白=指令）→ 红电源灯不亮 = 没供电。
- 协议是**裸读写** `writeto(addr,[cmd])` → `readfrom(addr,n)`，
  **不走"寄存器地址+数据"**（`readfrom_mem` 探它一律 NACK）。

### 驱动（`pinpong.libs.dfrobot_asr.DFRobot_ASR`）

```python
asr = DFRobot_ASR()                       # I2C 0x4F
asr.begin(asr.LOOP, asr.MIC)              # 模式 LOOP/BUTTON/PASSWORD；拾音 MIC/MONO
asr.add_command("kai shi xue xi", 1)      # ⚠️ 拼音必须空格分音节
asr.start()                               # 「设置完成 开始识别」——少了它不会识别
asr.read()                                # 返回关键词 ID；无结果返回 -1
```

- `read()` 实现：`read_reg(ASR_LOOP,1)` 返回一个字节，`0xff` → `-1`，否则是关键词 ID。
  所以「读得到但一直 -1」= **模块在线但没匹配上**。
- 最多 50 条候选，单条 ≤10 汉字 / 79 字节拼音；建议 4~6 音节；**离麦克风 ≥10cm**。
- 常量 `LOOP/MIC/...`；另有 `ASR_SET_IIC_ADDR = 0xAB`（**可改 I2C 地址**）。
- 模块不在时库重试 6 次后抛 `ValueError`。
- 已排除 SEN0539（DF2301Q，0x64，出厂唤醒词「小智你好」）—— 不是这款。

### 🔴🔴 最大的坑：拼音必须用空格分成音节

`add_command()` 只是把字符串**逐字符**发 ASCII 码，真正解析音节的是**模块固件**。
官方例子全是 `"kai deng"` / `"shang hai"` / `"bei jing"`。

写成 `"kaishixuexi"`（不分音节）→ 固件解析不出音节 → **一次都识别不到**，
而且 `read()` 一直返回 -1、**不报任何错**，极易误判成硬件坏了。
改成分音节的 `"kai shi xue xi"` 后立刻正常（实测日志 `[语音] 识别到 ID = 1`）。

**排查口诀：先看拼音有没有空格，再看发音。**
（另外 `kai shi xue xi` 里 `shi/xue/xi` 三个音节都是「舌面/舌尖 + i」，本身也偏难认；
认不动时换音节差异大的词，如 `kai shi zhuan zhu` / `zhuan zhu`。）

---

## 2. 语音合成 DFR0760（V2）—— I2C / UART 双模

- 模式开关：`D/T` = I2C_SDA / **UART_TX**，`C/R` = I2C_SCL / **UART_RX**（GND/VCC 不变）。
- 自带喇叭，42×32mm。

### I2C 模式（地址 0x40）

```python
from pinpong.libs.dfrobot_speech_synthesis import DFRobot_SpeechSynthesis_I2C
tts = DFRobot_SpeechSynthesis_I2C()
tts.begin(tts.V2)          # ⚠️ 结尾会自己 set_voice(1)，把音量压到 1
tts.set_voice(5)           # 所以初始化后必须再设一次音量
tts.speak("中文")          # 内部 GB2312 编码；阻塞到播报结束
```

握手失败连报 6 次 → `ValueError`；某些版本直接 `exit()` 抛 `SystemExit`，
**初始化处必须 `except BaseException` 兜底**。

### UART 模式（115200 8N1，协议帧与 I2C 完全一致）

- 合成帧 `FD <len_hi> <len_lo> 01 00 <GB2312 字节>`，`len = len(字节) + 2`
- 查状态 `FD 00 01 21`；设音量 `FD 00 06 01 00 "[vN]"`
- 模块回 ASCII：`'A'`(0x41) = 开始合成 / `'O'`(0x4F) = 空闲完成
- 本机实现 `tools/dfr0760_uart.py`（已逐字节核对官方库）

### 🔴🔴 自己实现合成时，`0x41` 之后必须 `sleep(0.1)`

库的 `wait()` 里有这一步。省掉它、一检测到 0x41 就立刻发 `[0xFD,0x00,0x01,0x21]`，
会**打断刚起步的合成** → 模块马上回空闲，表现是「耗时 0.00s」且**完全没声音**，
但**日志一切正常、不报任何错**（最难查的一类）。

对照实验：`guard=False` 首轮 0.00s，`guard=True` 1.63s。
→ `Speaker._speak_once()` 已修正，并加了「耗时 <0.3s 就重试一次」兜底。

### 其它注意

- ⚠️ 库的 `wait()` 是**没有超时的死循环**；自己重写时务必全程带超时，
  否则模块不响应会把工作线程**永久卡死**（表现为「日志有『合成：xxx』却永远没声音」）。
- 排查手法：**同一个进程里把「我们的实现」和「库的实现」背靠背对比**，
  一步就能把范围从「硬件 / 共存 / 环境」缩到「我们的代码」。
- 自检脚本按顺序用：`tools/tts_probe.py` → `tts_probe2.py` → `tts_probe3.py` → `tts_probe4.py`。
- 板子硬串口 `/dev/ttySP0`（P0=RX / P3=TX，在**金手指**上）；也可用 USB-A + USB-TTL → `/dev/ttyUSB0`。
- ⚠️ 只用 pyserial 开 `/dev/ttySP0` **收不到数据**，必须让 pinpong 的 `UART()` 先把 P0/P3 复用为 UART；
  ⚠️ 调 `UART()` 会触发 GD32 固件校验/重刷并 `kill all process`，第一次跑"没反应"，刷完再跑第二次。
- ⚠️ 早期"DFR0213 是 UART 版"**未经证实**；确定的是 **DFR0760 双模 + 有开关**。

---

## 3. 二哈识图 1 代（SEN0305）

✅ **2026-10-14 实测确认已接入可用**：日志 `[在座] 二哈就绪（人脸识别，I2C 0x32）`，
且**真的连续 60 秒没检测到人脸**并触发离座提醒 —— 模块真在工作。

### 驱动（`pinpong.libs.dfrobot_huskylens.Huskylens`，I2C 0x32）

```python
from pinpong.libs.dfrobot_huskylens import Huskylens, Parameter
hl = Huskylens(i2c_addr=0x32, bus_num=0)
hl.command_request_algorthim("ALGORITHM_FACE_RECOGNITION")
time.sleep(5.5)                 # 切算法后官方建议等约 5s 再读数
n = hl.read_count("blocks")     # 「检测到几个框」= 几张脸；>0 即判在座
```

其他常用：`is_appear(id,"blocks")` / `read_block_center_parameter_direct(Parameter.yCenter,"blocks")` /
`command_request_learn_once(id)` / `command_request_customnames(id,name)` /
`command_request_save/load_model_from_SD_card(idx)` / `command_request_photo()`。
`Parameter`: xCenter=0, yCenter=1, width=2, height=3, id=4。**无 `command_request_knock`**。

### 🔴 驱动的失败重试会阻塞

`_write_to_huskyLens` 失败时重试 **6 次、每次 `sleep(0.5)`** 再抛 `ValueError`（共约 3 秒）。
→ **轮询必须放后台线程**，放主循环会卡死画面。
初始化时看到几行 `huskylens iic communication faild, please wait` 是**正常**的。
读失败时按「**在场**」处理（宁可漏报离座，不要误报）。

### 🔴 库的读法慢 15 倍 —— 高频轮询必须自己读

2026-10-14 实测（同一个响应，两种读法背靠背对比）：

| 读法 | 单次耗时 |
|---|---|
| 库 `read_count("blocks")`（内部**逐字节** `readfrom_mem(0x32, x, 1)`） | **51.3 ms** |
| 自己「发 0x21 请求 + 一次性 `readfrom_mem(0x32, 0, 6)` 读包头」 | **3.4 ms** |

两者**结论完全一致**（说明库的读法没丢数据，只是慢）。做法：

```python
HL_REQ_BLOCKS = [0x55, 0xAA, 0x11, 0x00, 0x21, 0x31]   # 请求 blocks，校验和已核对
hl.i2c.writeto(0x32, HL_REQ_BLOCKS)                     # 自己发请求
head = hl.i2c.readfrom_mem(0x32, 0, 6)                  # 一次性读包头
count = head[5]                                         # 检测框数 = 人脸数
```

包头格式：`55 AA 11 <datalen> <cmd> <data...> <checksum>`；
请求 0x21 的响应命令码是 **0x29**，`data[0..1]`（16 位小端）就是检测框数。

**实测抓到的完整响应包**（`readfrom_mem(0x32, 0, 16)` 一次读全）：

```
看到人脸：55 AA 11 0A 29 | 01 00 01 00 BF 24 00 00 00 00 | 28
人脸消失：55 AA 11 0A 29 | 00 00 01 00 28 26 00 00 00 00 | 92
                  └len=10─┘ └data[0..1]=检测框数┘        └checksum┘
                              └data[2..3]=已学习ID数┘
                                     └data[4..5]每次都在变（帧号/时间戳）┘
```

- **检测框数确实在 `data[0]`**（小端 16 位）：看到脸时 = `01 00`，没脸时 = `00 00`。
- `data[2..3] = 01 00` = 模块上**已有 1 个学习过的 ID**。
- `data[4..5]` 每次读都在变（0x1424 → 0x0F40 → 0x24BF…），可用它确认「拿到的是新响应」而不是旧包。
- **校验和可验算**：除最后一个字节外全部求和 mod 256。
  例：`55+AA+11+0A+29+01+00+01+00+BF+24+00+00+00+00 = 0x0D0`，`0xD0 mod ...` → 实测 `0x28` ✓。

> ⚠️ `i2c.readfrom(0x32, n)`（不带寄存器地址）读的是当前指针位置，容易错位，别用。
> 👉 排「读不到脸」的问题时，把整包打出来对着上面这张表核对：**包格式合法 + 计数字节是 0，
> 就是模块真的没检测到**，别再往读法上查。

### 只判「有人 / 没人」—— 用对命令

- `COMMAND_REQUEST_BLOCKS`（**0x21**）= 请求**全部**检测框 → **任意人脸都算数，不需要「学习」**。
- `..._LEARNED`（0x22）/ `..._BY_ID`（0x23）= 只要学过的 → **不要用**。

### 规格与算法

- K210 / OV2640 / 2.0 寸 IPS 320×240 / UART 或 I2C，默认 Auto。固件建议 ≥ **V0.5.1a**。
- **7 种算法**：人脸识别、物体追踪、物体识别、巡线、颜色识别、标签识别、**物体分类**。
  帧率：追踪/人脸/物体识别 15Hz；颜色/巡线/标签 30Hz。
- **物体分类可学习**（长按学习键、多角度多学几次，模型可存/取 SD 卡）→ 学「手机」可行。
- 物体识别 20 类固定，**有「人」无「手机」**；人脸识别**只输出框 + ID，无头部角度**。
- 拍照只存**模块自己的 SD 卡**（320×240 `.bmp`，需 FAT32），**协议无取图指令**。
- 只有一个 4Pin 口；Protocol Type 在 General Settings 里选，改完 Save & Return；
  **扫不到时优先手动锁成 I2C 再重新上电**（Auto 检测会失败）。

---

## 4. 二哈识图 2 代（SEN0638，升级目标）

K230 双核 1.6GHz / 6 TOPS / 1GB LPDDR4 / 8GB eMMC / 2.4 寸 640×480 IPS 触摸 / GC2093 200W / 3.3–5V。
内置模型覆盖：人体检测·人脸识别→在座；人体关键点·姿态识别→坐姿；人脸朝向·注视方向→朝向；
自学习分类 + 自训练→手机；内置相机→取图。
官方库 `DFRobot_HuskylensV2`，**板载未安装，接入前先 pip install**。
FAQ：功能缺失先更新镜像；切模型延时约 5s；花屏多因静电接地不良。

---

## 5. 行空板 M10 本体

- RK3308 四核 1.2GHz / 512MB DDR3 / 16GB eMMC / Debian 10 / Wi-Fi 2.4G + BT4.0 /
  GD32VF103C8T6 协处理器 / 2.8 寸 240×320 触摸彩屏。供电 Type-C 5V/2A。
- 板载元件：电容硅麦克风、无源蜂鸣器、光线传感器、六轴传感器、Home/A/B 按键。
- **接口全在背面**：Type-C×1、**USB-A×1（本项目已释放）**、microSD×1、
  **4Pin I2C×2**（P19=SCL / P20=SDA）、3Pin I/O×4（P21~P24）、金手指 19 路（含 1×I2C、1×UART）。
- **两个 4Pin I2C 口是同一条 I2C1 的并联引出**，插哪个都一样；代码统一 `I2C(bus=0)`
  （板上映射到 `/dev/i2c-4`）。板载触摸屏 ns2009@0x48 走另一条总线（i2c-2），不共用。
- ⚠️ 模块插到 **3Pin I/O 口（P21~P24）扫不到** —— 只有 4Pin I2C 口通到 i2c-4。
- 板载 **Python 3.7.3 + Pillow 9.0.1**（低于 README 写的 3.8 / ≥10）；`unihiker`、`pinpong` 已装；
  `DFRobot_HuskylensV2` 未装。代码已对 Pillow<10 兼容（`except TypeError`）。
