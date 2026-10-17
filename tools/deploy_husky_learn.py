# -*- coding: utf-8 -*-
"""一键把「二哈学手机」常驻守护部署到 M10 并作为 systemd 服务跑起来。

做四件事：
  1. 上传 tools/husky_learn_hold.py → /root/smarttomato/
  2. 上传 tools/husky-learn.service → /etc/systemd/system/
  3. systemctl daemon-reload
  4. （默认）systemctl start husky-learn，并打印状态 + 日志尾部

⚠️ 部署前后请看 docs/m10_learn_phone.md 里的硬约束：
   - 主程序（m10_step3）占着 I2C 时不要起这个服务；
   - 本板 pinpong 进程退出 / 切算法都可能触发内核 panic，
     所以服务里**故意不配** Restart=always，挂了就整机重启。

用法：
    python deploy_husky_learn.py                 # 部署 + 启动
    python deploy_husky_learn.py --no-start      # 只部署不启动
    python deploy_husky_learn.py --enable        # 额外设成开机自启（慎用）
    python deploy_husky_learn.py --stop          # 停服务
    python deploy_husky_learn.py --status        # 只看状态
"""
import argparse
import os
import sys

import paramiko

HOST = "10.1.2.3"
USER = "root"
PASS = "dfrobot"
WORKDIR = "/root/smarttomato"
UNIT = "/etc/systemd/system/husky-learn.service"
SERVICE = "husky-learn"
HERE = os.path.dirname(os.path.abspath(__file__))


def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=15,
              banner_timeout=20, auth_timeout=20)
    return c


def run(c, cmd, timeout=60):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    if o.strip():
        print(o.rstrip())
    if e.strip():
        print("[stderr] " + e.rstrip())
    return rc


def put(c, local, remote):
    s = c.open_sftp()
    try:
        s.put(local, remote)
    finally:
        s.close()
    # ⚠️ 必须 sync + 校验字节数：本板随时可能 panic，ext4 延迟写会让文件
    #    在重启后变成 0 字节（实测：husky-learn.service 变成空文件后被 systemd
    #    当成 masked，怎么启都不生效）。
    _, out, _ = c.exec_command("sync; wc -c < %s" % remote)
    remote_size = int((out.read().decode().strip() or "0"))
    local_size = os.path.getsize(local)
    ok = remote_size == local_size
    print("↑ %s → %s（%d/%d 字节%s）"
          % (os.path.basename(local), remote, remote_size, local_size,
             "" if ok else " ✗ 不一致"))
    if not ok:
        raise SystemExit("写入校验失败：%s（远端 %d 字节，本地 %d 字节）"
                         % (remote, remote_size, local_size))


def status(c):
    run(c, "systemctl is-active %s; systemctl is-enabled %s 2>/dev/null; "
           "ps -eo pid,etime,args | grep '[h]usky_learn_hold'; "
           "echo '--- 状态文件 ---'; cat /dev/shm/husky_learn_status.json 2>/dev/null; "
           "echo; echo '--- 日志尾部 ---'; tail -12 %s/learn.log 2>/dev/null"
        % (SERVICE, SERVICE, WORKDIR))


def main():
    ap = argparse.ArgumentParser(description="部署/管理 husky-learn 常驻服务")
    ap.add_argument("--no-start", action="store_true", help="只部署，不启动")
    ap.add_argument("--enable", action="store_true", help="设为开机自启（慎用）")
    ap.add_argument("--stop", action="store_true", help="停止服务")
    ap.add_argument("--status", action="store_true", help="只看状态")
    args = ap.parse_args()

    c = conn()
    try:
        if args.status:
            status(c)
            return 0
        if args.stop:
            run(c, "systemctl stop %s; systemctl is-active %s" % (SERVICE, SERVICE))
            return 0

        put(c, os.path.join(HERE, "husky_learn_hold.py"),
            WORKDIR + "/husky_learn_hold.py")
        put(c, os.path.join(HERE, "husky-learn.service"), UNIT)
        run(c, "systemctl daemon-reload")
        if args.enable:
            run(c, "systemctl enable %s" % SERVICE)
        if not args.no_start:
            run(c, "systemctl restart %s" % SERVICE, timeout=90)
            print("\n--- 等待 12 秒看是否稳住（本板上起进程有 panic 风险）---")
            run(c, "sleep 12; systemctl is-active %s" % SERVICE)
        status(c)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
