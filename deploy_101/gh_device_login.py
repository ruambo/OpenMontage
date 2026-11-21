#!/usr/bin/env python3
# 在 101 跑：pty 拉起 `gh auth login` 设备流程，把一次性码打到日志后自动按回车，
# 然后保持 gh 存活轮询（最长 20 分钟），直到用户在浏览器确认。
import pty, os, re, sys, time, select, signal

OUT = os.path.expanduser("~/gh_login.log")
log = open(OUT, "w", buffering=1)

def emit(s):
    log.write(s)

pid, fd = pty.fork()
if pid == 0:
    os.environ["GH_PATH_SELF"] = os.path.expanduser("~/bin/gh")
    os.execv(os.path.expanduser("~/bin/gh"),
             ["gh", "auth", "login", "--hostname", "github.com",
              "--git-protocol", "ssh", "--web"])
    os._exit(1)

code_sent = False
key_acked = False
t0 = time.time()
try:
    while time.time() - t0 < 1200:
        r, _, _ = select.select([fd], [], [], 1.0)
        if not r:
            if not code_sent and time.time() - t0 > 15:
                emit("!! 15 秒内没看到一次性码，原始输出可能异常\n")
            continue
        try:
            data = os.read(fd, 8192)
        except OSError:
            break
        if not data:
            break
        txt = data.decode("utf-8", "replace")
        emit(txt)
        if not key_acked and "Upload your SSH public key" in txt:
            key_acked = True   # 确认上传 ~/.ssh/id_ed25519.pub（高亮项）
            time.sleep(0.8)
            os.write(fd, b"\n")
            emit("\n>>> ACK-SSH-KEY <<<\n")
        if not code_sent:
            m = re.search(r"one-time code[:：]\s*([0-9A-Z]{4,8}(?:-[0-9A-Z]{4})?)", txt)
            if m:
                code_sent = True
                time.sleep(0.5)
                os.write(fd, b"\n")  # Press Enter → 尝试开浏览器(失败也无妨，继续轮询)
                emit(f"\n>>> CODE={m.group(1)} <<<\n")
    # 收尾读干
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                break
            d = os.read(fd, 8192)
            if not d:
                break
            emit(d.decode("utf-8", "replace"))
    except OSError:
        pass
finally:
    try:
        os.waitpid(pid, os.WNOHANG)
    except Exception:
        pass
    emit("\n>>> DONE <<<\n")
