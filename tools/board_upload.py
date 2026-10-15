# -*- coding: utf-8 -*-
"""
行空板 M10 —— 从电脑把文件「推」到板子（不用打字/不用 scp）
=============================================================

背景（2026-10-13 实测）：
    * 本机没有 sshpass，普通 `ssh/scp` 命令行没法带密码；
      但 Mind+ 的 SSH 通道（mcp__mindplus__ssh_connect）带密码就能连。
    * 「本机起 HTTP 服务让板子 wget」**走不通** —— Windows 防火墙挡入站
      （本机 curl 自己是 200，板子却连不上）。
    * 大文件用 base64 手抄进工具调用既慢又容易错。

所以反过来：**在板子上起一个只收 POST 的小服务，电脑 curl 推过去。**

用法
----
1) 在板子上启动接收端（本文件会被放到板子上）：

       python3 board_upload.py /root/smarttomato/target.py 8125

   （用 setsid nohup 后台起，见下方 BOARD_CMD）

2) 在电脑上推文件：

       curl --noproxy '*' -X POST --data-binary "@本地文件" http://10.1.2.3:8125/upload

3) 在板子上核对 sha256 后再启用：

       sha256sum /root/smarttomato/target.py

⚠️ 用完记得关掉端口：`fuser -k 8125/tcp`
⚠️ `--noproxy '*'` 很关键，否则可能被本机代理拦掉。

板子上的启动命令（可直接抄）
---------------------------
    cd /root/smarttomato && (setsid nohup python3 board_upload.py \
        /root/smarttomato/x.py.new 8125 >/dev/null 2>&1 < /dev/null &) ; sleep 2; \
        netstat -tln | grep 8125
"""

import sys

from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit("用法: python3 board_upload.py <输出路径> <端口>")
    out_path, port = sys.argv[1], int(sys.argv[2])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            with open(out_path, "wb") as f:
                f.write(data)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK %d" % len(data))
            print("[recv] %d bytes -> %s" % (len(data), out_path))

        def log_message(self, *args):
            pass

    print("[recv] 监听 0.0.0.0:%d，收到 POST 就写入 %s" % (port, out_path))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
