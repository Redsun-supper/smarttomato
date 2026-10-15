# 板子运维与调试手法（smarttomato）

> `MEMORY.md` 只留结论与硬约束；**板子层面的操作细节、验证手法都在这里**。

---

## 1. 连接与环境

- **行空板 M10**：SSH `10.1.2.3`，用户 `root`，密码 `dfrobot`。
  本机在板子网段 = **10.1.2.101**。
- ⚠️ **普通 `ssh` 命令行在本机用不了**（`BatchMode` 报 `Permission denied`，且本机**没有 sshpass**）。
  → **走 Mind+ 的 SSH 通道**（带密码就能连）。
- ⚠️ **Mind+ 一关，`mcp__mindplus__*` 全部不可用**（mind-link 是 Mind+ 的子进程），
  那时只能走普通 SSH（也就意味着连不上）。
- **板子没有 RTC、也没有外网** → 系统时间默认是错的（曾显示 2019-02-14），
  **时间不准会让时钟功能看起来"坏了"**。对时：`date -s '2026-10-14 21:00:00' && hwclock -w`。
- ⚠️ **`/root` 下的脚本重启后会丢失**，需重新部署。

---

## 2. 🔴🔴 最重要：I2C 调试三条件

pinpong 的 I2C 在行空板上极其脆弱。2026-10-13 那天所有「总线卡死 / 模块冲突」的误判
都源于没遵守下面三条：

1. **重启后等约 1 分钟**再起进程（等 `rc.local` 的 `anaysis.py` 跑完、GD32 就绪）。
   ⚠️ 2026-10-13 用户纠正：**不用等 4~5 分钟**（那条是拔 Type-C 冷启动 + 反复试错得出的，偏保守）。
   起进程后看日志 `[语音] 识别模块就绪` / `[在座] 二哈就绪` 就能立刻确认 I2C 通没通，
   不用盲等。
2. **同一次开机只跑一个碰 I2C 的进程**。
   实测（同一个 `i2c_check.py`、同一次开机）：第 1 个进程 → SCL 40/40 健康、扫到 0x40 ✅；
   第 2 个进程 → SCL 0/40 恒低、全空 ❌，报 `PermissionError(1)`，**只能整机重启**
   （重跑 `anaysis.py` 救不回来，已验证）。
   → 所以**改代码后要整机 reboot**，再让新程序当第一个进程。
3. **所有要测的步骤写进同一个脚本、一个进程跑完** → 一个重启只换一个数据点。

**其它要点**

- **不要依赖 `i2cdetect`**：裸 `i2cdetect` 会全空而 pinpong 能扫到 0x40
  （4Pin 口的 I2C1 由 pinpong/GD32 动态配置，没有 pinpong 参与时总线没被正确驱动）。
  **一律以 pinpong 的扫描为准。**
- `PermissionError` ≠ 总线卡死，也可能是没按 pinpong 要求的顺序初始化
  （**先 `Pin(P19/P20, IN)` 再 `I2C(bus_num=0)`**）。
- `pkill -f <pattern>` 会**匹配到自己的 shell 把自己杀掉** → 用 `fuser -k <port>/tcp` 或 `kill <pid>`。
- 总线电平排查手法：`Pin(P19,IN)` / `Pin(P20,IN)` 采样 40 次。
  两线皆高 = 健康；SCL 恒低 + SDA 高 = 被从机拉住。
  ⚠️「两线都高」**不能**证明模块接上了（4Pin 口有板载上拉）；
  ⚠️ 而「第 2 个进程」测出的"SCL 恒低"**是假象**，别据此下结论。

---

## 3. `/dev/i2c-4` 是怎么来的

- 由 `/etc/rc.local` 末尾的 `pinpong/base/gd32/anaysis.py`（**官方拼写错误**）开机创建；
  i2c-4 是 pinpong 动态创建的 `pinpong_i2c1_adapter`，**必须先 `Board().begin()`**。
- **P0~P25 全部由 GD32VF103 扩展**（I2C1 也是软件模拟）。
  `anaysis.py` 读 `.io_config.txt` → `rmmod` + `insmod` `pinpong_linux_module.ko` → `pinConfig()`。
  **`/opt/.io_config.txt` 优先于包内默认。**
- 验证 I2C1 是否启用：`cat /sys/module/pinpong_linux_module/parameters/dev_iic1`
  → `0x0C000000` = bit26/27 置位。
- `rmmod pinpong_linux_module` 会失败（引用计数恒为 2），
  **重跑 anaysis.py 救不回卡死的总线**。

---

## 4. 开机自启

- 配置在 `/opt/unihiker/pyboardUI/config.cfg`（**单行 JSON**）。**改前先备份。**
- 六个用户级服务在 `/lib/systemd/user/`；`PyboardUI.service` 带 `Restart=always`，
  要停必须 `systemctl --user stop <name>`（先 `export XDG_RUNTIME_DIR=/run/user/0`）。
- ⚠️ 新板 `/root/main/` 原有「Smart Pet Care」项目且设了自启
  （**2026-10-13 已关闭**，备份 `config.cfg.bak`）。它占 P21/P23/P4/P1/P9，
  **做本项目前先 `ps -eo pid,args | grep main.py` 确认没在跑**。
- 📌 当前项目**没有配开机自启**，靠 `setsid nohup` 常驻；板子重启后要手动拉起。

---

## 5. 三个省事的验证手法（值得复用）

### 5.1 抓真实屏幕（不用按键就能确认画对了）

`/dev/fb0` 是 240×320 / 16bpp。Python 读出 RGB565 → RGB 存 PNG：

```bash
cd /root/smarttomato && python3 grab.py          # 工具：tools/grab_screen.py
python3 -m http.server 8123 --bind 10.1.2.3      # 板子上起个静态服务
# 本机：
curl --noproxy '*' -s http://10.1.2.3:8123/screen.png -o shot.png
```

⚠️ 本机 curl **必须加 `--noproxy '*'`**（否则走代理连不上 10.1.2.3）。
⚠️ 收尾记得 `fuser -k 8123/tcp`。

### 5.2 推文件给板子

本机起 HTTP 让板子 `wget` 会被 **Windows 防火墙挡掉**（实测连不通）。
→ **反着来**：板子起一个只收 POST 的小服务，电脑推过去。

```bash
# 板子（脚本在板上：/root/smarttomato/recv.py）
setsid nohup python3 recv.py /root/smarttomato/xxx.py.new 8125 >/dev/null 2>&1 < /dev/null &
# 本机
curl --noproxy '*' -s -X POST --data-binary @xxx.py http://10.1.2.3:8125/upload
```

工具：`tools/board_upload.py`。**传完必须 sha256 校验**（两边 `sha256sum` 对齐再 `mv`）。
收尾 `fuser -k 8125/tcp`。

### 5.3 用板子真实字体渲染（不连板子出图）

```bash
python3 m10_step3_away_last.py --preview out.png     # 六态拼图
```

`Screen` 只依赖 Pillow，**不需要创建 GUI**，所以可以在板上或本机直接跑。
本机缺 DejaVu / 汉仪旗黑，会回退到系统字体（预览的字宽略有差异）——
**真正的字宽差异只有这样上板抓屏才能发现**。

---

## 6. 常驻与部署模板

```bash
# 1) 整机重启（改了代码就必须重启，见 §2）
sync; systemctl reboot --no-block        # ⚠️ 用 `(sleep 1; reboot) &` 会被 mind-link
                                         #    连会话一起清掉、根本不生效

# 2) 等约 1 分钟
# 3) 传文件 + sha256 校验 + mv
# 4) 常驻启动（PPID 应为 1，脱离 SSH 会话）
cd /root/smarttomato && setsid nohup python3 -u <脚本> > run.log 2>&1 < /dev/null &
sleep 25 && ps -eo pid,ppid,etime,args | grep '[m]10_'
```

- `PPID 1` = 已脱离 SSH 会话 —— 关掉 Mind+、断开连接都不会停。
- 板上目录统一 `/root/smarttomato/`。
