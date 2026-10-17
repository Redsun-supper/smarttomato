# -*- coding: utf-8 -*-
"""通过 paramiko 直连板子（绕开 Mind+ 的 mind-link 通道）。

背景：Mind+ 一关，mcp__mindplus__ssh_* 全部失效；本机又没有 sshpass。
paramiko 能直接带密码连，是本机唯一可用的非交互 SSH 路径。

用法：
    python board_ssh.py exec "ls /root/smarttomato"
    python board_ssh.py put  <本地文件> <板上路径>
    python board_ssh.py get  <板上路径> <本地文件>
    python board_ssh.py daemon "<命令>" [日志名，默认 step3.log]
"""
import sys
import paramiko

HOST = "10.1.2.3"
USER = "root"
PASS = "dfrobot"


def _cli():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=15,
              banner_timeout=20, auth_timeout=20)
    return c


def do_exec(cmd):
    c = _cli()
    try:
        _, out, err = c.exec_command(cmd, timeout=120)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        sys.stdout.write(o)
        if e:
            sys.stdout.write("\n[STDERR]\n" + e)
        return rc
    finally:
        c.close()


def do_daemon(cmd, log="step3.log"):
    """在板子上用 `at`/自建 double-fork 启动**完全脱离**本 SSH 通道的常驻进程。

    ⚠️ 直接 `cmd &` 会让 paramiko 的 exec 通道一直等后台进程的 stdout 关闭，
    导致本机这边挂住。这里改走一个 shell 脚本 + `setsid`，并**主动关掉
    三个标准流**，让 exec 立刻返回。
    """
    c = _cli()
    try:
        launcher = "/tmp/_start_daemon.sh"
        body = "#!/bin/sh\ncd /root/smarttomato || exit 1\n" \
               "exec setsid %s > %s 2>&1 < /dev/null\n" % (cmd, log)
        sftp = c.open_sftp()
        with sftp.open(launcher, "w") as f:
            f.write(body)
        sftp.chmod(launcher, 0o755)
        sftp.close()
        # 关键：把 launcher 自己也 setsid 掉，且不做任何等待
        _, out, err = c.exec_command("setsid %s >/dev/null 2>&1 < /dev/null & " % launcher)
        out.channel.recv_exit_status()
        print("[daemon] 已启动：%s" % cmd)
        return 0
    finally:
        c.close()


def do_put(local, remote):
    """优先 SFTP；板子若没开 SFTP 子系统（实测报 No such file），退回 base64 走 exec。"""
    c = _cli()
    try:
        try:
            s = c.open_sftp()
            s.put(local, remote)
            st = s.stat(remote)
            print("put OK (sftp): %s -> %s (%d bytes)" % (local, remote, st.st_size))
            s.close()
            return
        except Exception as e:
            print("[put] SFTP 不可用（%s），改用 base64 通道" % e)

        import base64
        with open(local, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        # 分块写入，避免单条命令过长
        chunk = 60000
        _, _, _ = c.exec_command("rm -f %s.b64" % remote)
        for i in range(0, len(b64), chunk):
            part = b64[i:i + chunk]
            _, out, _ = c.exec_command("printf '%%s' '%s' >> %s.b64" % (part, remote))
            out.channel.recv_exit_status()
        cmd = "base64 -d %s.b64 > %s && rm -f %s.b64 && wc -c %s" % (
            remote, remote, remote, remote)
        _, out, err = c.exec_command(cmd)
        o = out.read().decode("utf-8", "replace")
        e2 = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        print("put OK (base64): %s -> %s\n%s%s" % (local, remote, o, e2))
        if rc != 0:
            raise SystemExit(rc)
    finally:
        c.close()


def do_get(remote, local):
    c = _cli()
    try:
        s = c.open_sftp()
        s.get(remote, local)
        print("get OK: %s -> %s" % (remote, local))
        s.close()
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    op = sys.argv[1]
    if op == "exec":
        raise SystemExit(do_exec(sys.argv[2]))
    elif op == "daemon":
        do_daemon(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "step3.log")
    elif op == "put":
        do_put(sys.argv[2], sys.argv[3])
    elif op == "get":
        do_get(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        raise SystemExit(2)
