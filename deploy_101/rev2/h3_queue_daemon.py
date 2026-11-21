#!/usr/bin/env python3
"""H3 视频队列守护进程。

职责（常驻，systemd 用户服务 h3-queue.service）：
  1. 盯着 queue/*.json
  2. 有任务时，先等**对话模型(llama-server)空闲** —— AionUi 的 agent 就是它，
     必须等它把话说完（连续 IDLE_SECONDS 秒没有请求在处理）才能停机
  3. 让路：h3_video_mode.sh on（停 llama-server、起 CUDA ComfyUI）
  4. 逐条生成（提交 → 轮询 → 收文件）
  5. **无论成功失败，最后一定** h3_video_mode.sh off（把 llama-server 拉回来）
  6. 结果写入 last_job.json / queue/done/，并尽力发一条桌面通知

日志：h3/h3_queue_daemon.log
"""
import glob
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

H3 = "/home/ruanlinbo/harness/h3"
QUEUE = f"{H3}/queue"
DONE = f"{H3}/queue/done"
OUTDIR = f"{H3}/ComfyUI/output"
RESULTS = f"{H3}/videos_done"
LAST = f"{H3}/last_job.json"
HOLD = f"{H3}/.ui_hold"   # UI 保持模式标记（h3_ui_mode.sh 管理）
LOGFILE = f"{H3}/h3_queue_daemon.log"
TEMPLATE = f"{H3}/payload_portrait_girl_v1.json"
TEMPLATE_I2V = f"{H3}/payload_i2v_v1.json"   # 图生视频：节点 19=LoadImage → 节点 8 first_frame
TEMPLATE_ZIMAGE = f"{H3}/payload_zimage_t2i_v1.json"  # Z-Image-Turbo 文生图（官方工作流 API 化）
RESULTS_IMG = f"{H3}/images_done"

LLM_SLOTS = "http://127.0.0.1:11435/slots"
COMFY = os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188")  # [CEIL5] lane2 daemon→:8190
COMFY_SVC = os.environ.get("H3_COMFY_SVC", "h3-comfyui")  # [CEIL5] 自愈目标单元
LANE = os.environ.get("H3_LANE", "1")  # [CEIL5] "1"=V100 主车道(默认) "2"=PG500 快道(只吃 lane==fast)
# OM101 rev2 (2026-11-20)：视频是常态 —— ComfyUI 由 h3-comfyui.service 常驻看护，
# 对话脑在 141 flash，27B 仅按需（.h3-llm-mode 标记存在 = LLM 备选模式在岗，任务排队等待）。
REV2 = os.environ.get("H3_REV2", "") == "1"
LLM_MODE_FLAG = f"{H3}/.h3-llm-mode"
_last_repair = [0.0]

POLL = 5            # 队列轮询间隔（秒）
IDLE_SECONDS = 30   # 连续空闲多久才认为对话结束
JOB_TIMEOUT = 1800  # 单条任务超时

def _black_check(path):
    """[CEIL7] NaN 防线（shared-memory notes/2026-09-24-H3-NaN根因 缺陷B 标注未修复：
    DiT 内部 fp16 溢出→全黑片）。抽 3 帧 YAVG，多数≈0 判全黑。"""
    import re
    try:
        d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True)
        dur = float((d.stdout or "0").strip() or 0)
        bad = 0
        for t in (0.1, max(dur / 2, 0.2), max(dur - 0.3, 0.2)):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                                "-vf", "signalstats,metadata=print", "-f", "null", "-"],
                               capture_output=True, text=True)
            m = re.search(r"YAVG=([\d.]+)", r.stdout + r.stderr)
            if m and float(m.group(1)) < 2.0:
                bad += 1
        return bad >= 2
    except Exception:
        return False

def _lane_match(jf):
    """[CEIL5] 只认自己车道的任务；lane==fast 属 PG500 快道。坏文件留主道按旧行为处理。"""
    try:
        ln = json.load(open(jf, encoding="utf-8")).get("lane", "")
    except Exception:
        return LANE == "1"
    return (ln == "fast") if LANE == "2" else (ln != "fast")

def _te_of(job):
    """[CEIL5] 快道强制小编码器：PG500 32G 装不下 32B int4(14.2G)+DiT(20G)+VAE。"""
    if LANE == "2":
        return "small"
    return job.get("te") or "large"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def http_post(url, payload, timeout=60):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 带上响应体（ComfyUI 校验错误详情）
        try:
            body = e.read().decode("utf-8", "replace")[:1200]
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code}: {body}") from e


def llm_is_idle():
    """返回 True/False；对话后端不在时返回 None（表示无需等待）。router /slots 会代理到当前活跃后端（strata/27B）。"""
    try:
        slots = http_get(LLM_SLOTS, timeout=5)
    except Exception:
        return None
    if not isinstance(slots, list) or not slots:
        return None
    return all(not s.get("is_processing") for s in slots)


def ui_hold_active():
    """UI 保持模式：人正在用 ComfyUI 网页（h3_ui_mode.sh on 创建 HOLD 文件）。

    存在该标记时任务跑完**不要**自动 h3_video_mode.sh off —— 否则浏览器里的
    ComfyUI 立刻变成 Reconnecting。用完 UI 由 h3_ui_mode.sh off 释放。
    """
    return os.path.exists(HOLD)


def comfyui_alive():
    """8188 在线且返回 comfyui_version（CUDA ComfyUI 特有字段）。"""
    try:
        info = http_get(f"{COMFY}/system_stats", timeout=3)
    except Exception:
        return False
    return isinstance(info, dict) and ("devices" in info or "comfyui_version" in info)  # rev2fix: fork 只回 {system,devices}


def wait_comfyui_ready(max_wait=2400):
    """rev2：等常驻 ComfyUI 在岗。不在 → 5 分钟冷却地 systemctl restart h3-comfyui 自愈；
    LLM 备选模式在岗 → 排队等人回常态（不抢卡）。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if comfyui_alive():
            return True
        if os.path.exists(LLM_MODE_FLAG):
            log("ℹ️ LLM 备选模式在岗(.h3-llm-mode)：任务排队，等待 h3_video_mode.sh on 回视频常态")
            time.sleep(30)
            continue
        if time.time() - _last_repair[0] > 300:
            _last_repair[0] = time.time()
            log("ℹ️ ComfyUI 不在岗 → 自愈: systemctl --user restart h3-comfyui")
            try:
                subprocess.run(["systemctl", "--user", "restart", COMFY_SVC], timeout=30)  # [CEIL5]
            except Exception:
                pass
        time.sleep(10)
    return False


def wait_for_llm_idle():
    need = max(1, IDLE_SECONDS // POLL)
    run = 0
    while run < need:
        idle = llm_is_idle()
        if idle is None:
            log("对话后端未在运行，跳过空闲等待")
            return
        if idle:
            run += 1
        else:
            if run:
                log("检测到新的对话请求，空闲计时重置")
            run = 0
        time.sleep(POLL)
    log(f"对话模型已空闲 {IDLE_SECONDS}s → 准备让路")


def switch(mode, te=None):
    """调 h3_video_mode.sh。te 仅 on 时透传: 实际执行 h3_video_mode.sh on <te> --auto-fallback
    （2026-09-26 更新：大脑选择已由 h3_video_mode.sh 负责 —— 现役是 m2@11442 / vornith@11444，
     旧的 27B@11438 / 14B@11439 已退役；--auto-fallback 仍允许显存不足时阶梯降级）。"""
    args = ["bash", f"{H3}/h3_video_mode.sh", mode]
    if mode == "on":
        args.append(te or "large")
        args.append("--auto-fallback")
        if os.environ.get("H3_BRAIN_NONE") == "1":
            args.append("--brain=none")  # OM101: 对话脑=141，不装本地大脑
    r = subprocess.run(args, capture_output=True, text=True, timeout=900, env=dict(os.environ))
    log(f"h3_video_mode.sh {' '.join(args[2:])} → rc={r.returncode} {(r.stdout or '').strip()[-200:]}")
    return r.returncode == 0


def build_director_payload(job):
    """Director 工作流: 一采(steps步) + 二采(BasicScheduler beta 3步 低噪声 0.2, 同分辨率精修)。
    模板: payload_director_refine.json (MiniMaxH3Director + MiniMaxH3DirectorRefine)。
    节点 22=Director, 18=LoRA, 4=编码器, 24/26=精修/一采 两个 SaveVideo。"""
    with open(f"{H3}/payload_director_refine.json", encoding="utf-8") as f:
        d = json.load(f)
    p = d["prompt"]
    if _te_of(job) == "large":
        p["4"] = {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
                             "type": "minimax"}}
    else:
        p["4"] = {"class_type": "ClipProjLoader",
                  "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                             "type": "krea2",
                             "projection": "mmh3-4b-ClipProj-v3.1.safetensors",
                             "device": "cuda:0", "mode": "dynamic"}}
    p["22"]["inputs"].update({
        "global_prompt": job["prompt"],
        "width": job["width"],
        "height": job["height"],
        "total_frames": job["length"],
        "seed": job["seed"],
        "steps": job["steps"],
    })
    p["18"]["inputs"]["lora_name"] = job["lora"]
    p["24"]["inputs"]["filename_prefix"] = job["filename_prefix"]
    p["26"]["inputs"]["filename_prefix"] = job["filename_prefix"] + "_pre"
    d["client_id"] = f"h3-queue-{job['job_id']}"
    return d


def build_zimage_payload(job):
    """Z-Image-Turbo 文生图（通义 6B 蒸馏模型，官方推荐 9 步 euler/simple/cfg1/shift3）。

    模板 payload_zimage_t2i_v1.json 由 ComfyUI 官方示例工作流逐节点转 API 格式而来。
    模型：diffusion_models/z_image_turbo_bf16.safetensors（bf16，V100 上 weight_dtype=default→fp16 运行）
    编码器：text_encoders/qwen_3_4b.safetensors（type=lumina2）；VAE：vae/ae.safetensors（与 FLUX 同一文件）。"""
    with open(TEMPLATE_ZIMAGE, encoding="utf-8") as f:
        d = json.load(f)
    p = d["prompt"]
    p["4"]["inputs"]["text"] = job["prompt"]
    p["6"]["inputs"]["width"] = job["width"]
    p["6"]["inputs"]["height"] = job["height"]
    p["7"]["inputs"]["seed"] = job["seed"]
    p["7"]["inputs"]["steps"] = job.get("steps", 9)
    p["10"]["inputs"]["filename_prefix"] = job["filename_prefix"]
    d["client_id"] = f"h3-queue-{job['job_id']}"
    return d


def build_payload(job):
    if job.get("zimage"):
        return build_zimage_payload(job)
    if job.get("director"):
        return build_director_payload(job)
    # 图生视频：任务带 first_frame 时走 I2V 模板。
    # 节点行为（nodes_minimax_h3.py 源码）：first_frame 被"拉伸"到画布当几何锚
    # （素材比例应与画布一致，如 9:16 配 768x1344，避免变形）；last_frame 保比例裁切。
    is_i2v = bool(job.get("first_frame"))
    if is_i2v:
        in_dir = os.path.join(H3, "ComfyUI", "input")
        if not os.path.isfile(os.path.join(in_dir, job["first_frame"])):
            raise RuntimeError(f"首帧图片不在 ComfyUI/input: {job['first_frame']}")
    with open(TEMPLATE_I2V if is_i2v else TEMPLATE, encoding="utf-8") as f:
        d = json.load(f)
    p = d["prompt"]
    if is_i2v:
        p["19"]["inputs"]["image"] = job["first_frame"]
    # 文本编码器二选一：
    #   large(默认) = 32B int4，占 14.3GB（2026-09-20 画质实验验证的最佳配置；对话由常驻小脑兜底）
    #   small      = 4B fp8 + ClipProj 投影，只占 4.9GB，快 ~35%，画质略降
    if _te_of(job) == "large":
        p["4"] = {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
                             "type": "minimax"}}
    else:
        p["4"] = {"class_type": "ClipProjLoader",
                  "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                             "type": "krea2",
                             "projection": "mmh3-4b-ClipProj-v3.1.safetensors",
                             "device": "cuda:0", "mode": "dynamic"}}
    p["8"]["inputs"].update({
        "prompt": job["prompt"],
        "width": job["width"],
        "height": job["height"],
        "length": job["length"],
    })
    p["9"]["inputs"]["noise_seed"] = job["seed"]
    p["11"]["inputs"]["steps"] = job["steps"]
    p["18"]["inputs"]["lora_name"] = job["lora"]
    p["17"]["inputs"]["filename_prefix"] = job["filename_prefix"]
    # Spectrum 免训练加速（可选，2026-09-28）：插在 SigmaShift(30) 与 BasicGuider(10) 之间。
    # 频谱预测未来步 hidden state，跳过 transformer block。euler 4步约省 1 步。
    # job 字段 "spectrum": true | {"blend_weight":0.5,"degree":1,...}
    spec = job.get("spectrum")
    if spec:
        if isinstance(spec, bool):
            spec = {"enabled": spec}
        p["31"] = {
            "class_type": "SpectrumApplyMiniMaxH3",
            "inputs": {
                "model": ["30", 0],
                "enabled": bool(spec.get("enabled", True)),
                "blend_weight": float(spec.get("blend_weight", 0.50)),
                "degree": int(spec.get("degree", 1)),
                "ridge_lambda": float(spec.get("ridge_lambda", 0.10)),
                "window_size": float(spec.get("window_size", 2.0)),
                "flex_window": float(spec.get("flex_window", 0.75)),
                "warmup_steps": int(spec.get("warmup_steps", 1)),
                "tail_actual_steps": int(spec.get("tail_actual_steps", 1)),
                "max_history": int(spec.get("max_history", 8)),
                "debug": bool(spec.get("debug", False)),
                "history_storage": spec.get("history_storage", "system_ram"),
            },
        }
        p["10"]["inputs"]["model"] = ["31", 0]
    d["client_id"] = f"h3-queue-{job['job_id']}"
    return d


def find_outputs(prefix):
    """找出该前缀的全部 mp4，返回 (主产物, 全部, 预览)。

    Director 工作流产出两个文件：`{prefix}_pre_*.mp4`（一采预览）与
    `{prefix}_*.mp4`（二采精修成片）。**预览往往后写**，所以不能简单取 mtime 最新，
    否则会把预览当成任务结果（2026-09-20 踩过此坑）。
    """
    files = glob.glob(f"{OUTDIR}/{prefix}*.mp4") or glob.glob(f"{OUTDIR}/*{prefix}*.mp4")
    if not files:
        return None, [], None
    files.sort(key=os.path.getmtime)
    previews = [f for f in files if "_pre" in os.path.basename(f)]
    finals = [f for f in files if "_pre" not in os.path.basename(f)]
    primary = finals[-1] if finals else previews[-1]
    return primary, files, (previews[-1] if previews else None)


def find_output(prefix):
    """兼容旧调用：只返回主产物。"""
    return find_outputs(prefix)[0]


def find_output_img(prefix):
    """文生图任务（Z-Image）：找该前缀的全部 png，返回 (主产物, 全部)。"""
    files = glob.glob(f"{OUTDIR}/{prefix}*.png")
    if not files:
        return None, []
    files.sort(key=os.path.getmtime)
    return files[-1], files


def _is_oom_error(e):
    s = str(e).lower()
    return "out of memory" in s or "cuda oom" in s


def _oom_fallback(job):
    """OOM 自动降档（2026-09-21 修正）：
    - 720p OOM → 480p **同帧数**（像素 -56%，帧数不变，激活显存严格更小）
    - 480p OOM → 480p@25f（最后手段）

    旧逻辑降到 480p@124f 是错的：帧数×3 后激活显存反而比 720p@39f 更大，
    实测 I2V 720p@39f 与 480p@124f 双双 OOM（2026-09-21 07:46/07:47），
    而同一 ComfyUI 会话里 T2V 720p@39f 成功——说明是帧数问题不是分辨率问题。"""
    if job.get("oom_retried", 0) >= 2:
        return None
    if (job["width"], job["height"]) in ((480, 832), (832, 480)):
        if job.get("oom_retried") == 1:
            fb = dict(job)
            fb["length"] = 25
            fb["oom_retried"] = 2
            return fb
        return None  # 已是 480p@25f，不再降级
    fw, fh = (480, 832) if job["height"] > job["width"] else (832, 480)
    fb = dict(job)
    fb["width"], fb["height"] = fw, fh
    fb["oom_retried"] = 1
    return fb


def run_job(job):
    t0 = time.time()
    res = {"job_id": job["job_id"], "prompt": job["prompt"], "steps": job["steps"],
           "resolution": f"{job['width']}x{job['height']}", "seed": job["seed"],
           "filename_prefix": job["filename_prefix"], "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        cur = job
        while True:
            try:
                r = http_post(f"{COMFY}/prompt", build_payload(cur))
                if "prompt_id" not in r:
                    raise RuntimeError(f"提交被拒绝: {json.dumps(r, ensure_ascii=False)[:300]}")
                pid = r["prompt_id"]
                res["prompt_id"] = pid
                log(f"  已提交 ComfyUI：{pid}")

                misses = 0
                while time.time() - t0 < JOB_TIMEOUT:
                    time.sleep(5)
                    try:
                        hist = http_get(f"{COMFY}/history/{pid}", timeout=15)
                        misses = 0
                    except Exception:
                        hist = {}
                        # 2026-09-21 修复：ComfyUI 在任务进行中被杀掉（例如面板点了"回平时"）时，
                        # 旧逻辑会一直等到 JOB_TIMEOUT(30 分钟) 才报错；改为连续 60 秒取不到就快速失败。
                        misses += 1
                        if misses >= 12:
                            raise RuntimeError(
                                "ComfyUI 已不可达（连续 60s 无法读取 /history）——"
                                "视频模式可能被中断（例如面板点了「回平时」或服务被重启）")
                    if pid in hist:
                        st = hist[pid].get("status", {})
                        res["comfy_status"] = st.get("status_str")
                        errs = [m for m in st.get("messages", []) if m and m[0] in ("execution_error", "execution_interrupted")]
                        if errs:
                            raise RuntimeError(f"ComfyUI 执行错误: {str(errs)[:300]}")
                        break
                else:
                    raise TimeoutError(f"任务超过 {JOB_TIMEOUT}s 未完成")
                break  # 成功
            except (RuntimeError, TimeoutError) as e:
                fb = _oom_fallback(cur) if _is_oom_error(e) else None
                if fb is None:
                    raise
                log(f"  ⚠️ OOM → 自动降档 480p 重试（原 {cur['width']}x{cur['height']}@{cur['length']}f）: {str(e)[:150]}")
                cur = fb
                res["oom_fallback"] = f"{cur['width']}x{cur['height']}@{cur['length']}f"
        if cur is not job:
            res["resolution"] = f"{cur['width']}x{cur['height']}"

        if job.get("zimage"):
            # 文生图：SaveImage 同步写 PNG，短等即可
            deadline = time.time() + 60
            primary, all_out = None, []
            while time.time() < deadline:
                primary, all_out = find_output_img(job["filename_prefix"])
                if primary:
                    break
                time.sleep(2)
            if not primary:
                raise RuntimeError("任务报成功但没找到输出 png")
            os.makedirs(RESULTS_IMG, exist_ok=True)
            copied = {}
            for f in all_out:
                d = f"{RESULTS_IMG}/{os.path.basename(f)}"
                shutil.copy2(f, d)
                copied[f] = d
            res.update({
                "status": "success",
                "output_path": copied[primary],
                "comfy_output": primary,
                "outputs": [copied[f] for f in all_out],
            })
            log(f"  ✅ 完成：{copied[primary]}")
        else:
            # history 报完成后 SaveVideo 的 ffmpeg 可能还在收尾（尤其 Director 二采成片常晚 1-2s），
            # 等"成片"（非 _pre）出现再走，最多 90s，避免先杀 ComfyUI 把文件丢了
            deadline = time.time() + 90
            while time.time() < deadline:
                primary, all_out, preview = find_outputs(job["filename_prefix"])
                if primary and "_pre" not in os.path.basename(primary):
                    break
                time.sleep(2)
            primary, all_out, preview = find_outputs(job["filename_prefix"])
            if not primary:
                raise RuntimeError("任务报成功但没找到输出 mp4")
            os.makedirs(RESULTS, exist_ok=True)
            copied = {}
            for f in all_out:
                d = f"{RESULTS}/{os.path.basename(f)}"
                shutil.copy2(f, d)
                copied[f] = d
            dst = copied[primary]
            if _black_check(dst):  # [CEIL7] NaN 全黑防线
                os.rename(dst, dst.replace(".mp4", ".black-suspect-nan.mp4"))
                raise RuntimeError("成片全黑：疑似 DiT fp16 溢出 NaN（shared-memory 2026-09-24 缺陷B），换 seed 重试或临时改 bf16")
            res.update({
                "status": "success",
                "video_path": dst,                      # 主产物 = 成片（非 _pre 预览）
                "comfy_output": primary,
                "outputs": [copied[f] for f in all_out],
                "preview_path": copied.get(preview),     # Director 一采预览（若有）
            })
            log(f"  ✅ 完成：{dst}"
                + (f"（另有预览 {os.path.basename(copied[preview])}）" if preview else ""))
    except Exception as e:  # noqa: BLE001
        res.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
        log(f"  ❌ 失败：{res['error']}")
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def notify(res):
    try:
        if res.get("status") == "success":
            out = res.get("video_path") or res.get("output_path")
            body = f"{os.path.basename(out)}  ({res['elapsed_s']}s)"
            title = "Z-Image 文生图完成" if "output_path" in res else "H3 视频生成完成"
        else:
            body = str(res.get("error"))[:120]
            title = "H3 视频生成失败"
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        uid = os.getuid()
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        subprocess.run(["notify-send", "-u", "normal", "-t", "0", title, body],
                       env=env, timeout=10, capture_output=True)
    except Exception:
        pass


def write_last(res):
    try:
        with open(LAST, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log(f"写 last_job.json 失败: {e}")


def archive(jobfile, res):
    try:
        os.makedirs(DONE, exist_ok=True)
        base = os.path.basename(jobfile)
        job = json.load(open(jobfile, encoding="utf-8"))
        job["result"] = res
        with open(f"{DONE}/{base}", "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        os.remove(jobfile)
    except Exception as e:  # noqa: BLE001
        log(f"归档失败: {e}")


def process_queue(jobs):
    tes = []
    for jf in jobs:
        try:
            tes.append(json.load(open(jf, encoding="utf-8")).get("te") or "large")
        except Exception:
            tes.append("large")
    # 按本批 te 调 h3_video_mode.sh 起视频期大脑（现役：m2@11442 / vornith@11444）。
    # 2026-09-26：常驻小脑(1660) 与 m3 桥接均已退役 —— 切换期间的"11435 不断"保障已不存在，
    # 停大模型到大脑就绪之间约 1~2 分钟没有后端（用户明确接受的代价）。
    from collections import Counter
    batch_te = Counter(tes).most_common(1)[0][0] if tes else "large"
    if sorted(set(tes)) != [batch_te]:
        # 同票或混合批时 most_common 取首个，显式保证同票取 large
        c = Counter(tes)
        if c["large"] >= c["small"]:
            batch_te = "large"
    log(f"本次文本编码器: {sorted(set(tes))} → 视频期大脑按 te={batch_te} 选择（对话有大脑/小脑兜底，不会断）")
    if REV2:
        log("▶ 常态视频模式：ComfyUI 常驻在岗则直渲，不让路/不回滚")
        ok = wait_comfyui_ready()
    else:
        ok = switch("on", te=batch_te)
    if not ok:
        log("⚠️ 让路失败（ComfyUI 未起来），任务保留在队列里稍后重试")
        return
    try:
        for jf in jobs:
            job = json.load(open(jf, encoding="utf-8"))
            log(f"开始生成 {job['job_id']}：{job['width']}x{job['height']} {job['steps']}步 「{job['prompt'][:60]}」")
            res = run_job(job)
            write_last(res)
            archive(jf, res)
            notify(res)
    finally:
        if REV2:
            log("常态模式：ComfyUI 保持常驻，不 off、不回滚 LLM")
        elif ui_hold_active():
            # 人在用 ComfyUI 网页：保留实例，否则前端立刻变 Reconnecting
            log("🖥️  UI 保持模式生效 → 保留 ComfyUI，不恢复大模型")
            log(f"   用完请执行: bash {H3}/h3_ui_mode.sh off")
        else:
            # 关键：无论如何都把对话模型拉回来
            log("全部任务结束 → 恢复对话后端（默认 strata）")
            # 【2026-11-07】off 可能瞬时失败（锁竞争 rc=75 / systemctl 抖动），必须重试，
            # 否则系统永久停在视频模式（11-06 23:46 事故：ComfyUI 继承了模式锁 fd，
            # 守护的 off 被自己的锁拒绝且无重试 → 视频跑完切不回来）。
            for _attempt in range(1, 6):
                if switch("off"):
                    break
                log(f"⚠️ 恢复对话后端第 {_attempt}/5 次失败，30s 后重试...")
                time.sleep(30)
            else:
                log("🚨🚨 恢复对话后端 5 次全部失败！系统仍停在视频模式，"
                    f"请人工执行: bash {H3}/h3_video_mode.sh off")


def main():
    os.makedirs(QUEUE, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    log(f"H3 队列守护进程启动 LANE={LANE} @ {COMFY}（轮询 {POLL}s，空闲判定 {IDLE_SECONDS}s，超时 {JOB_TIMEOUT}s）")  # [CEIL5]
    if ui_hold_active():
        log("⚠️  UI 保持模式已开启（h3/.ui_hold）：任务结束后不会自动停 ComfyUI")
    while True:
        try:
            jobs = [jf for jf in sorted(glob.glob(f"{QUEUE}/*.json")) if _lane_match(jf)]  # [CEIL5]
            if jobs:
                if not REV2:
                    wait_for_llm_idle()
                # 等空闲期间可能有新任务进来，重新取一次
                jobs = [jf for jf in sorted(glob.glob(f"{QUEUE}/*.json")) if _lane_match(jf)]  # [CEIL5]
                if jobs:
                    process_queue(jobs)
        except Exception as e:  # noqa: BLE001
            log(f"主循环异常: {type(e).__name__}: {e}")
            if ui_hold_active():
                log("UI 保持模式生效 → 异常路径也不停 ComfyUI")
            else:
                try:
                    if not REV2:
                        switch("off")  # 异常也保证 LLM 在（rev2：常态即视频，无需回滚）
                except Exception:
                    pass
        time.sleep(POLL)


if __name__ == "__main__":
    main()
