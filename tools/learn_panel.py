# -*- coding: utf-8 -*-
"""学习控制台 —— 本机网页面板，远程管理 M10 上常驻的二哈学习脚本。

背景：学习脚本（husky_learn_hold.py）用 nohup 常驻在板子上，靠 M10 板载
A/B 键操作。本面板负责「脚本之外的杂事」：看状态、拉起脚本、reboot 清场、
查询/清空已学模型 —— 全都不用记 SSH 命令。

⚠️ 硬约束（2026-10-17 实测，见 memory）：
   1. pinpong 进程「一退出就 panic」——正常退出也崩。所以：
      - 学完**不要**按 A 停止，直接点「重启板子」清场；
      - 「查询已学 / 清空」这类一次性脚本跑完必崩板子 → 属预期，约 1 分钟恢复。
   2. 常驻启动只能用 nohup 方式（board_ssh.py daemon 的 setsid launcher 有 SIGHUP
      竞态，learn.log 会一片空白）。
   3. 查询/清空执行前，板子上**不能有** husky_learn_hold.py 在跑（I2C 单进程）。

用法：
    双击「启动学习控制台.bat」，或在 tools/ 下跑：
    C:/Users/student/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe learn_panel.py
    然后浏览器打开 http://127.0.0.1:8790
"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paramiko

HOST = "10.1.2.3"
USER = "root"
PASS = "dfrobot"
PORT = 8790
WORKDIR = "/root/smarttomato"


def ssh_run(cmd, timeout=60):
    """在板子上执行命令，返回 (stdout, stderr, rc)。失败抛异常。"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=15,
              banner_timeout=20, auth_timeout=20)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        return o, e, rc
    finally:
        c.close()


def board_reachable():
    """只探 TCP 22，不建 SSH 会话（板子 panic 重启期间快速判定）。"""
    import socket
    try:
        s = socket.create_connection((HOST, 22), timeout=4)
        s.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 状态查询

STATUS_CMD = (
    "uptime; echo '===PS==='; "
    "ps aux | grep husky_learn_hold | grep -v grep; "
    "echo '===I2C==='; i2cdetect -y 4 2>/dev/null | grep -oE '\\b32\\b' | head -1; "
    "echo '===LOG==='; tail -25 {wd}/learn.log 2>/dev/null; "
    "echo '===DONE==='"
).format(wd=WORKDIR)


def get_status():
    """采集板子状态 → dict。任何一步失败都不致命，字段置空。"""
    st = {
        "online": False, "uptime_raw": "", "up_minutes": None,
        "proc": None, "husky_online": None, "log": "", "error": "",
        "time": time.strftime("%H:%M:%S"),
    }
    if not board_reachable():
        st["error"] = "板子 ping 不通（可能正在 panic 重启，约 1 分钟恢复）"
        return st
    try:
        out, _, _ = ssh_run(STATUS_CMD, timeout=40)
    except Exception as e:
        st["error"] = "SSH 查询失败：%s" % e
        return st
    st["online"] = True

    # uptime：`08:58:43 up 6 min, 1 user, load average: ...`
    m = re.search(r"up\s+([^,]+),", out)
    if m:
        st["uptime_raw"] = m.group(1).strip()
        m2 = re.search(r"(\d+)\s*min", st["uptime_raw"])
        if m2:
            st["up_minutes"] = int(m2.group(1))
        elif "hour" in st["uptime_raw"]:
            st["up_minutes"] = 999      # 以小时计，稳定

    # 学习脚本进程：root 1206 0.7 3.7 26652 16404 ? S 08:55 0:01 /usr/bin/python3 -u ...
    m = re.search(r"^root\s+(\d+)\s+.*?/usr/bin/python3 -u husky_learn_hold.*$",
                  out, re.MULTILINE)
    if m:
        st["proc"] = {"pid": m.group(1)}

    # 二哈 0x32 在线
    i2c_part = out.split("===I2C===")[1].split("===LOG===")[0] if "===I2C===" in out else ""
    st["husky_online"] = bool(re.search(r"\b32\b", i2c_part))

    log_part = out.split("===LOG===")[1].split("===DONE===")[0] if "===LOG===" in out else ""
    st["log"] = log_part.strip()
    return st


# ---------------------------------------------------------------- 操作

def action_start():
    """nohup 拉起学习脚本（唯一验证过可靠的常驻方式）。"""
    out, _, _ = ssh_run(
        "ps aux | grep husky_learn_hold | grep -v grep | wc -l", timeout=20)
    if out.strip() != "0":
        return {"ok": False, "msg": "学习脚本已在运行，无需重复启动。"}
    ssh_run(
        "cd {wd} && nohup python3 -u husky_learn_hold.py --id 1 "
        "> learn.log 2>&1 < /dev/null & echo LAUNCHED".format(wd=WORKDIR),
        timeout=25)
    time.sleep(2)
    out, _, _ = ssh_run("ps aux | grep husky_learn_hold | grep -v grep", timeout=20)
    pid = ""
    m = re.search(r"root\s+(\d+)", out)
    if m:
        pid = "（PID %s）" % m.group(1)
    return {"ok": True, "msg": "学习脚本已启动%s，日志 learn.log。\n"
            "现在走到板子前：按 A 开始 → 按 B 学一张（重复 30+ 张）。\n"
            "⚠️ 学完别按 A 停止，直接回本面板点「重启板子」清场。" % pid}


def action_reboot():
    """reboot 清场（停脚本的唯一干净方式）。"""
    ssh_run("(sleep 2; reboot) >/dev/null 2>&1 & echo REBOOTING", timeout=15)
    return {"ok": True, "msg": "板子将在 2 秒后重启，约 1 分钟恢复。\n"
            "重启后常驻脚本不再运行，需要再学习就点「启动学习脚本」。"}


def _proc_running():
    out, _, _ = ssh_run(
        "ps aux | grep husky_learn_hold | grep -v grep | wc -l", timeout=20)
    return out.strip() != "0"


def action_check():
    """查询当前已学 ID 数与识别效果（--check 8）。

    ⚠️ 跑完进程退出 → 板子必 panic 重启（pinpong 已知 bug），属预期。
    """
    if _proc_running():
        return {"ok": False, "msg": "学习脚本还在跑（I2C 单进程约束）。\n"
                "请先点「重启板子」清场，等约 1 分钟恢复后再查。"}
    out, _, _ = ssh_run(
        "cd {wd} && timeout 30 python3 -u husky_learn_phone.py --check 8 2>&1".format(
            wd=WORKDIR), timeout=45)
    learned = None
    m = re.findall(r"已学=(\d+)", out)
    if m:
        learned = m[-1]
    tail = "\n".join(out.strip().splitlines()[-8:])
    msg = tail + "\n\n⚠️ 查询进程已退出 —— 按已知 bug，板子会在几十秒内自动重启（约 1 分钟恢复），属预期。"
    if learned is not None:
        msg = "当前已学 ID 数：%s\n\n" % learned + msg
    return {"ok": True, "msg": msg}


def action_forget():
    """清空模块上全部已学内容。⚠️ 同样跑完必崩板子，属预期。"""
    if _proc_running():
        return {"ok": False, "msg": "学习脚本还在跑（I2C 单进程约束）。\n"
                "请先点「重启板子」清场，等约 1 分钟恢复后再清空。"}
    out, _, _ = ssh_run(
        "cd {wd} && timeout 30 python3 -u husky_learn_phone.py --forget 2>&1".format(
            wd=WORKDIR), timeout=45)
    tail = "\n".join(out.strip().splitlines()[-6:])
    msg = tail + "\n\n⚠️ 清空进程已退出 —— 板子会在几十秒内自动重启（约 1 分钟恢复），属预期。"
    return {"ok": True, "msg": msg}


ACTIONS = {
    "start": action_start,
    "reboot": action_reboot,
    "check": action_check,
    "forget": action_forget,
}

# ---------------------------------------------------------------- HTTP 层

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>学习控制台 · smarttomato</title>
<style>
  :root{
    --teal:#00838F; --teal-d:#00696F; --ink:#1F2933; --sub:#5B6B73;
    --line:#E3E8EA; --bg:#F4F7F8; --card:#FFFFFF;
    --ok:#2E7D32; --warn:#A85E00; --bad:#C2185B;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);
       font-family:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;
       font-size:14px;line-height:1.6;padding:24px 16px 48px}
  .wrap{max-width:920px;margin:0 auto}
  header{display:flex;align-items:center;justify-content:space-between;
         margin-bottom:18px;flex-wrap:wrap;gap:8px}
  h1{font-size:20px;font-weight:600;color:var(--teal-d)}
  .conn{font-size:13px;color:var(--sub);display:flex;align-items:center;gap:6px}
  .dot{width:10px;height:10px;border-radius:50%;display:inline-block;
       background:#B0BEC5}
  .dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.bad{background:var(--bad)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
        gap:12px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:14px 16px}
  .card .t{font-size:12px;color:var(--sub);margin-bottom:6px}
  .card .v{font-size:16px;font-weight:600}
  .card .s{font-size:12px;color:var(--sub);margin-top:2px}
  .v.ok{color:var(--ok)} .v.warn{color:var(--warn)} .v.bad{color:var(--bad)}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
  button{border:none;border-radius:10px;padding:11px 22px;font-size:14px;
         font-weight:600;cursor:pointer;font-family:inherit;transition:opacity .15s}
  button:disabled{opacity:.45;cursor:not-allowed}
  .b-main{background:var(--teal);color:#fff} .b-main:hover:not(:disabled){background:var(--teal-d)}
  .b-ghost{background:#fff;color:var(--teal-d);border:1.5px solid var(--teal)}
  .b-danger{background:var(--bad);color:#fff}
  .sect{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:16px 18px;margin-bottom:16px}
  .sect h2{font-size:15px;font-weight:600;color:var(--teal-d);margin-bottom:10px}
  ol.steps{padding-left:20px} ol.steps li{margin:6px 0}
  .warnbox{background:#FFF6EC;border:1px solid #F0D9B5;border-radius:8px;
           padding:10px 12px;font-size:13px;color:var(--warn);margin-top:8px}
  pre.log{background:#10212B;color:#C8D6DF;border-radius:8px;padding:12px;
          font-family:Consolas,"Courier New",monospace;font-size:12px;
          max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-all}
  details{margin-top:4px} summary{cursor:pointer;font-weight:600;color:var(--teal-d);
          padding:6px 0}
  .adv{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
  #msg{margin-top:14px;border-radius:10px;padding:12px 14px;display:none;
       white-space:pre-wrap;font-size:13px}
  #msg.ok{background:#EBF5EC;color:var(--ok);border:1px solid #CBE2CC;display:block}
  #msg.err{background:#FBECEF;color:var(--bad);border:1px solid #F2CDD6;display:block}
  label.chk{font-size:12px;color:var(--sub);display:flex;align-items:center;gap:5px}
  .overlay{position:fixed;inset:0;background:rgba(16,33,43,.45);display:none;
           align-items:center;justify-content:center;z-index:9}
  .overlay.show{display:flex}
  .modal{background:#fff;border-radius:14px;padding:22px;max-width:420px;width:90%}
  .modal h3{font-size:16px;margin-bottom:10px;color:var(--bad)}
  .modal p{font-size:13px;color:var(--ink);margin-bottom:16px;white-space:pre-wrap}
  .modal .row{display:flex;gap:10px;justify-content:flex-end}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>学习控制台 <span style="font-size:13px;color:var(--sub);font-weight:400">smarttomato · M10 二哈学习脚本</span></h1>
    <div class="conn"><span class="dot" id="cDot"></span><span id="cText">连接中…</span>
      <label class="chk" style="margin-left:12px"><input type="checkbox" id="auto" checked> 自动刷新</label>
    </div>
  </header>

  <div class="grid">
    <div class="card"><div class="t">板子</div><div class="v" id="kBoard">—</div><div class="s" id="kBoardSub">—</div></div>
    <div class="card"><div class="t">学习脚本</div><div class="v" id="kProc">—</div><div class="s" id="kProcSub">—</div></div>
    <div class="card"><div class="t">二哈模块 0x32</div><div class="v" id="kHusky">—</div><div class="s" id="kHuskySub">—</div></div>
    <div class="card"><div class="t">已学 ID 数</div><div class="v" id="kLearned">—</div><div class="s">高级操作里可查询</div></div>
  </div>

  <div class="btns">
    <button class="b-ghost" id="btnRefresh">刷新状态</button>
    <button class="b-main" id="btnStart">启动学习脚本</button>
    <button class="b-danger" id="btnReboot">重启板子（清场）</button>
  </div>

  <div class="sect">
    <h2>使用步骤</h2>
    <ol class="steps">
      <li>确认上面「学习脚本」显示 <b>运行中</b>（没运行就点「启动学习脚本」）。</li>
      <li>走到板子前，<b>按 A 键</b> 开始学习模式（学的 ID 是 1）。</li>
      <li>把手机摆好（换 App / 换角度 / 换光线），<b>按 B 键</b> 学一张；再换姿势按 B…… 建议 <b>30 张以上</b>，亮屏灭屏都要学。</li>
      <li>学完回到本面板，<b>点「重启板子」</b> 清场。</li>
    </ol>
    <div class="warnbox">⚠️ 学完<b>不要</b>在板子上按 A 停止 —— pinpong 进程一退出就会让板子内核 panic 重启（已知 bug，实测）。用「重启板子」按钮清场最干净。</div>
  </div>

  <div class="sect">
    <h2>运行日志 <span style="font-size:12px;color:var(--sub);font-weight:400">/root/smarttomato/learn.log（尾部）</span></h2>
    <pre class="log" id="logBox">—</pre>
  </div>

  <div class="sect">
    <details>
      <summary>高级操作（会重启板子，属预期）</summary>
      <div style="font-size:13px;color:var(--sub);margin-top:6px">
        「查询 / 清空」是一次性脚本，跑完退出必触发 panic 重启（已知 bug）——约 1 分钟自动恢复，别慌。执行前板子上不能有学习脚本在跑。
      </div>
      <div class="adv">
        <button class="b-ghost" id="btnCheck">查询已学 ID 与识别效果</button>
        <button class="b-danger" id="btnForget">清空全部已学内容</button>
      </div>
    </details>
  </div>

  <div id="msg"></div>
</div>

<div class="overlay" id="ov">
  <div class="modal">
    <h3 id="ovTitle">确认</h3>
    <p id="ovText"></p>
    <div class="row">
      <button class="b-ghost" id="ovNo">取消</button>
      <button class="b-danger" id="ovYes">确认执行</button>
    </div>
  </div>
</div>

<script>
var $ = function(id){ return document.getElementById(id); };
var autoTimer = null;

function showMsg(text, ok){
  var m = $("msg");
  m.textContent = text;
  m.className = ok ? "ok" : "err";
  m.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function render(st){
  var dot = $("cDot"), ct = $("cText");
  if(st.online){
    dot.className = "dot ok"; ct.textContent = "已连接 · " + st.time;
  }else{
    dot.className = "dot bad"; ct.textContent = "离线";
  }
  var kb = $("kBoard"), kbs = $("kBoardSub");
  if(!st.online){
    kb.textContent = "离线"; kb.className = "v bad";
    kbs.textContent = st.error || "可能正在 panic 重启，约 1 分钟恢复";
  }else if(st.up_minutes !== null && st.up_minutes < 2){
    kb.textContent = "运行中"; kb.className = "v warn";
    kbs.textContent = "uptime " + st.uptime_raw + "（刚重启过，可能刚 panic 过，等 1 分钟再看）";
  }else{
    kb.textContent = "运行中"; kb.className = "v ok";
    kbs.textContent = "uptime " + st.uptime_raw;
  }
  var kp = $("kProc"), kps = $("kProcSub");
  if(st.proc){
    kp.textContent = "运行中"; kp.className = "v ok";
    kps.textContent = "PID " + st.proc.pid + " · husky_learn_hold.py --id 1";
    $("btnStart").disabled = true;
  }else{
    kp.textContent = "未运行"; kp.className = "v warn";
    kps.textContent = st.online ? "点「启动学习脚本」拉起" : "—";
    $("btnStart").disabled = false;
  }
  var kh = $("kHusky"), khs = $("kHuskySub");
  if(st.husky_online === null){ kh.textContent = "—"; kh.className = "v"; khs.textContent = "—"; }
  else if(st.husky_online){ kh.textContent = "在线"; kh.className = "v ok"; khs.textContent = "I2C 0x32"; }
  else{ kh.textContent = "不在线"; kh.className = "v bad"; khs.textContent = "查 TF 卡（非 FAT32 会让 0x32 消失）"; }
  $("logBox").textContent = st.log || "（日志为空）";
}

function refresh(){
  fetch("/api/status").then(function(r){return r.json();}).then(render).catch(function(e){
    showMsg("查询失败：" + e, false);
  });
}

function post(action, btn){
  btn.disabled = true;
  var old = btn.textContent; btn.textContent = "执行中…";
  fetch("/api/" + action, {method:"POST"}).then(function(r){return r.json();})
    .then(function(j){ showMsg(j.msg, j.ok); refresh(); })
    .catch(function(e){ showMsg("请求失败：" + e, false); })
    .finally(function(){ btn.disabled = false; btn.textContent = old; });
}

function confirm(title, text, action, btn){
  $("ovTitle").textContent = title;
  $("ovText").textContent = text;
  $("ov").classList.add("show");
  $("ovYes").onclick = function(){
    $("ov").classList.remove("show");
    post(action, btn);
  };
}
$("ovNo").onclick = function(){ $("ov").classList.remove("show"); };

$("btnRefresh").onclick = refresh;
$("btnStart").onclick = function(){
  confirm("启动学习脚本？", "将以 nohup 方式在板子上常驻运行 husky_learn_hold.py --id 1（日志 learn.log）。\n若已在运行则不会重复启动。", "start", this);
};
$("btnReboot").onclick = function(){
  confirm("重启板子？", "板子将在 2 秒后重启（约 1 分钟恢复）。\n这是停掉学习脚本的唯一干净方式 —— 进程直接退出会触发内核 panic。\n重启后常驻脚本不再运行，需要再学习就重新点「启动学习脚本」。", "reboot", this);
};
$("btnCheck").onclick = function(){
  confirm("查询已学 ID？", "会跑一次性 --check 脚本（约 15 秒）。\n⚠️ 跑完进程退出，板子会在几十秒内自动 panic 重启（已知 bug），约 1 分钟恢复，属预期。\n若学习脚本还在跑请先取消，去点「重启板子」。", "check", this);
};
$("btnForget").onclick = function(){
  confirm("清空全部已学内容？", "会跑一次性 --forget 脚本，清掉模块上所有已学 ID（当前疑似有 6 个）。\n⚠️ 跑完进程退出，板子会在几十秒内自动 panic 重启，约 1 分钟恢复，属预期。", "forget", this);
};

$("auto").onchange = function(){
  if(this.checked){ autoTimer = setInterval(refresh, 5000); }
  else{ clearInterval(autoTimer); }
};
refresh();
autoTimer = setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass    # 安静模式

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(PAGE)
        elif self.path == "/api/status":
            try:
                self._send_json(get_status())
            except Exception as e:
                self._send_json({"online": False, "error": str(e)})
        else:
            self._send_json({"ok": False, "msg": "unknown path"}, 404)

    def do_POST(self):
        action = self.path.rsplit("/", 1)[-1]
        fn = ACTIONS.get(action)
        if not fn:
            self._send_json({"ok": False, "msg": "unknown action"}, 404)
            return
        try:
            self._send_json(fn())
        except Exception as e:
            self._send_json({"ok": False, "msg": "执行出错：%s" % e})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("学习控制台已启动：http://127.0.0.1:%d" % PORT)
    print("（关闭本窗口即停止面板；板子上的脚本不受影响）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
