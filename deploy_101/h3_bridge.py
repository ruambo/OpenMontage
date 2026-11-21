#!/usr/bin/env python3
"""H3 LAN Bridge — 把 101 的 H3 视频队列暴露成 HTTP API（供 141 / OpenMontage 调用）。

设计原则：**不碰** h3_queue_daemon.py 的任何让路逻辑（等待 LLM 空闲 → h3_video_mode.sh on
→ 渲染 → off 复原）。本服务只做三件事：
  1. 校验并落盘 queue/<job_id>.json（daemon 自然接手）
  2. 汇总任务状态（pending / done / failed + 产物路径）
  3. 回传产物 mp4/png、接收 i2v 首帧图

端点（全部 JSON，除 /file 返回二进制）:
  GET  /health            模式与余量概览（comfyui 在否、队列深度、两卡显存）
  POST /jobs              提交任务 → 202 {"job_id": ...}
  GET  /jobs/<id>         状态: pending | done | failed | unknown
  GET  /jobs/<id>/file    下载主产物 (?kind=preview 取预览, zimage 自动给 png)
  POST /upload            {"name": "x.png", "b64": "..."} → 存 ComfyUI/input
鉴权: 设置 H3_BRIDGE_TOKEN 后，请求需带 X-Auth 头（本机回环豁免）。

env: H3_BRIDGE_PORT=8189  H3_BRIDGE_TOKEN=***  H3_HOME=/home/ruanlinbo/harness/h3
"""
import base64
import glob
import json
import os
import re
import secrets
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

H3 = os.environ.get("H3_HOME", "/home/ruanlinbo/harness/h3")
QUEUE = f"{H3}/queue"
DONE = f"{QUEUE}/done"
INPUTS = f"{H3}/ComfyUI/input"
OUTDIR = f"{H3}/ComfyUI/output"
TEMPLATE = f"{H3}/payload_portrait_girl_v1.json"
MODE_TOKEN = "/home/ruanlinbo/h3-logs/.allow-video-mode"
PORT = int(os.environ.get("H3_BRIDGE_PORT", "8189"))
TOKEN = os.environ.get("H3_BRIDGE_TOKEN", "")
# OM101 待办①：sudo 需密码无法用 ufw → 代码级来源白名单（前缀匹配）。
# 默认放行回环 + 141 编排脑；要加机器改 env H3_BRIDGE_ALLOW="127.,192.168.2.141,192.168.2.50"
ALLOW_PREFIXES = tuple(
    p.strip() for p in os.environ.get("H3_BRIDGE_ALLOW", "127.,192.168.2.141,::1").split(",") if p.strip()
)
JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}

# daemon 无条件读取这些 job 字段（见 h3_queue_daemon.py build_payload），必须有默认值
try:
    with open(TEMPLATE, encoding="utf-8") as f:
        _tpl = json.load(f)["prompt"]
    DEFAULT_LORA = _tpl.get("18", {}).get("inputs", {}).get("lora_name", "")
except Exception:
    DEFAULT_LORA = ""


def _default_lora() -> str:
    """模板可能被 daemon/人改过——每次热读一次，取不到才用启动缓存。"""
    try:
        with open(TEMPLATE, encoding="utf-8") as f:
            return json.load(f)["prompt"]["18"]["inputs"]["lora_name"]
    except Exception:
        return DEFAULT_LORA


def nvidia_free():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout
        return [ln.strip() for ln in out.strip().splitlines()]
    except Exception:
        return []


def job_state(job_id: str):
    """按队列目录 + 产物文件推断状态（不依赖 daemon 内部结果格式）。"""
    if not JOB_ID_RE.match(job_id):
        return {"error": "bad id"}
    qfile = glob.glob(f"{QUEUE}/{job_id}*.json")
    dfile = glob.glob(f"{DONE}/{job_id}*.json")
    src = None
    meta = {}
    if dfile:
        src = sorted(dfile)[-1]
    elif qfile:
        src = sorted(qfile)[-1]
    if src:
        try:
            meta = json.load(open(src, encoding="utf-8"))
        except Exception:
            meta = {}
    prefix = meta.get("filename_prefix", f"om-{job_id}")
    is_img = bool(meta.get("zimage"))
    ext = "png" if is_img else "mp4"
    finals = [f for f in glob.glob(f"{OUTDIR}/{prefix}*.{ext}") if "_pre" not in os.path.basename(f)]
    previews = [f for f in glob.glob(f"{OUTDIR}/{prefix}_pre*.{ext}")]
    finals.sort(key=os.path.getmtime)
    previews.sort(key=os.path.getmtime)
    if finals or previews:
        primary = (finals or previews)[-1]
        return {"state": "done", "file": primary,
                "preview": previews[-1] if previews else None,
                "te": meta.get("te"), "prompt": meta.get("prompt", "")[:200]}
    if dfile:  # 已出队但没产物 → daemon 判失败（OOM 兜底也用了会生成文件，故无文件=失败）
        return {"state": "failed", "hint": "done 无产物；查 h3_queue_daemon.log / videos_done"}
    if qfile:
        return {"state": "pending", "created_at": meta.get("created_at")}
    return {"state": "unknown"}


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # 降噪
        pass

    def _send(self, code, obj):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allow(self):
        ip = self.client_address[0]
        if not ip.startswith(ALLOW_PREFIXES):
            self._send(403, {"error": "ip not allowed", "ip": ip})
            return False
        if TOKEN and not ip.startswith("127.") and ip != "::1":
            if self.headers.get("X-Auth", "") != TOKEN:
                self._send(401, {"error": "bad token"})
                return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 40 * 1024 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._allow():
            return
        if u.path == "/health":
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2)
                comfy = True
            except Exception:
                comfy = False
            pend = len(glob.glob(f"{QUEUE}/[!d]*.json"))
            return self._send(200, {
                "ok": True,
                "comfyui_up": comfy,
                "video_mode": os.path.exists(MODE_TOKEN),
                "queue_pending": pend,
                "queue_done": len(glob.glob(f"{DONE}/*.json")),
                "gpus": nvidia_free(),
                "default_lora_ok": bool(_default_lora()),
            })
        m = re.match(r"^/jobs/([^/]+)/?$", u.path)
        if m:
            return self._send(200, job_state(m.group(1)))
        m = re.match(r"^/jobs/([^/]+)/file$", u.path)
        if m:
            st = job_state(m.group(1))
            kind = parse_qs(u.query).get("kind", [""])[0]
            path = st.get("preview") if kind == "preview" else st.get("file")
            if st.get("state") != "done" or not path:
                return self._send(409, {"error": "not ready"})
            data = open(path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4" if path.endswith(".mp4") else "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        return self._send(404, {"error": "no route"})

    def do_POST(self):
        if not self._allow():
            return
        u = urlparse(self.path)
        try:
            body = self._body()
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})
        if u.path == "/upload":
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(body.get("name", "om_img.png"))))
            if os.path.splitext(name)[1].lower() not in IMG_EXT:
                name += ".png"
            try:
                raw = base64.b64decode(body.get("b64", ""))
            except Exception:
                return self._send(400, {"error": "bad b64"})
            if not raw:
                return self._send(400, {"error": "empty b64"})
            dest = f"{INPUTS}/{int(time.time())}-{name}"
            open(dest, "wb").write(raw)
            return self._send(200, {"first_frame": os.path.basename(dest)})
        if u.path == "/jobs":
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                return self._send(400, {"error": "prompt required"})
            job_id = f"om-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
            length = int(body.get("length") or 0)
            if not length:  # 允许按秒提交：480p@5.2s≈39f
                secs = float(body.get("seconds", 5.2))
                fps = int(body.get("fps", 24))
                length = max(9, round(secs * fps / 4) * 4 + 3)  # H3/DiT 帧数步进
            job = {
                "job_id": job_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "created_ts": int(time.time()),
                "prompt": prompt,
                "width": int(body.get("width", 480)),
                "height": int(body.get("height", 832)),
                "length": length,
                "fps": int(body.get("fps", 24)),
                "seed": int(body.get("seed", secrets.randbelow(2**31))),
                "steps": int(body.get("steps", 8)),
                "lora": body.get("lora") or _default_lora(),
                "filename_prefix": job_id,
                "lane": str(body.get("lane", "")),  # [CEIL5] "fast"=PG500 快道(强制 te=small)，缺省主道
                "te": body.get("te", "large"),
                "spectrum": bool(body.get("spectrum", False)),
            }
            if body.get("zimage"):
                job["zimage"] = True
            if body.get("first_frame"):
                job["first_frame"] = re.sub(r"[^A-Za-z0-9._-]", "", str(body["first_frame"]))
            if body.get("source") == "openmontage":
                job["source"] = "openmontage"
            if not job["lora"] and not job.get("zimage"):
                return self._send(500, {"error": "template lora 读取失败"})
            os.makedirs(QUEUE, exist_ok=True)
            with open(f"{QUEUE}/{job_id}.json", "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=1)
            return self._send(202, {"job_id": job_id, "length": length, "queued": True})
        return self._send(404, {"error": "no route"})


if __name__ == "__main__":
    print(f"[h3-bridge] :{PORT} token={'on' if TOKEN else 'off'} H3={H3}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
