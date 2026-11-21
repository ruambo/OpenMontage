#!/usr/bin/env python3
# [CEIL5] fp16 转正 + 双车道：PG500 独立 ComfyUI(:8190) + 车道过滤的第二 daemon。
# 幂等：每步独立判重；备份 .bak-ceil5。锚点不唯一即整体失败退出（宁可不动，不可错动）。
import os, shutil, pathlib, py_compile

H3 = "/home/ruanlinbo/harness/h3"
rd = lambda p: pathlib.Path(p).read_text()
wr = lambda p, s: pathlib.Path(p).write_text(s)

# ---------- 1) lane1 生产值 bf16→fp16 ----------
P1 = f"{H3}/start_comfy_cuda.sh"
s = rd(P1)
if "[CEIL5]" not in s:
    shutil.copy2(P1, P1 + ".bak-ceil5")
    old = "export H3_DIT_DTYPE=bf16   # 生产值：bf16（实测 fp16+缩放会显著改变输出，见 shared-memory 笔记）"
    new = ("export H3_DIT_DTYPE=fp16   # [CEIL5] 生产值改 fp16：480×832×111f 实测 921s→230s（4.0×）；\n"
           "#   TextFP32 节点 ×1/8+clamp 兜住 3e5 激活不 NaN（2026-11-21 实测同 seed 无黑屏）。同 seed 输出风格会变，\n"
           "#   QC 对比视频见 141:~/projects/OpenMontage/projects/rev2_speed_test/qc/。回退：改回 bf16 并重启 h3-comfyui")
    assert s.count(old) == 1, f"fp16 anchor count={s.count(old)}"
    s = s.replace(old, new)
    wr(P1, s)
    print("1) lane1 fp16 转正 OK")
else:
    print("1) fp16 已转正，skip")

# ---------- 2) start_comfy_pg.sh（由 lane1 派生） ----------
P2 = f"{H3}/start_comfy_pg.sh"
if not os.path.exists(P2):
    g = rd(P1).replace("8188", "8190")
    g = g.replace("comfy_cuda.log", "comfy_pg.log").replace("comfy_cuda.pid", "comfy_pg.pid")
    g = g.replace('export CUDA_VISIBLE_DEVICES="${_cv_v},${_cv_p}"', 'export CUDA_VISIBLE_DEVICES="${_cv_p}"')
    g = g.replace('[ -n "$_cv_v" ] && [ -n "$_cv_p" ] || { echo "[FATAL] 双卡枚举失败 v=$_cv_v p=$_cv_p" >&2; exit 1; }',
                  '[ -n "$_cv_p" ] || { echo "[FATAL] PG500 枚举失败 p=$_cv_p" >&2; exit 1; }')
    g = g.replace('export H3_TE_DEVICE=${H3_TE_DEVICE:-cuda:1}  # [CEIL] 32B int4 TE→PG500：编码从几分钟→几十秒',
                  'export H3_TE_DEVICE=${H3_TE_DEVICE:-cuda:0}  # [CEIL5] lane2 单卡 PG500：TE 放本卡，须配 te=small')
    g = g.replace("# MiniMax H3 ComfyUI — NVIDIA CUDA (Tesla V100 ×2) 启动脚本",
                  "# MiniMax H3 ComfyUI — [CEIL5] lane2: PG500 专属实例 :8190（由 start_comfy_cuda.sh 派生）\n"
                  "# 只应吃 lane==fast 任务（daemon2 已强制 te=small：32B int4 与 DiT 在 32G 内挤不下）")
    assert g.count("8190") >= 5 and "[CEIL5]" in g, "pg derive failed"
    wr(P2, g)
    os.chmod(P2, 0o755)
    print("2) start_comfy_pg.sh OK")
else:
    print("2) start_comfy_pg.sh 已存在，skip")

# ---------- 3) comfy_pg_resident.sh ----------
P3 = f"{H3}/comfy_pg_resident.sh"
if not os.path.exists(P3):
    wr(P3, """#!/usr/bin/env bash
# [CEIL5] lane2 看护：保证 8190 上始终有 PG500 版 ComfyUI。避让逻辑同主车道
#（27B 备选模式的 dual-GPU 也要 PG500，故 LLM 模式标记/llama-server 在岗时同样待命）。
set -u
H3=/home/ruanlinbo/harness/h3
FLAG="$H3/.h3-llm-mode"
log(){ echo "[resident-pg $(date '+%F %T')] $*"; }
pg_alive() {
  curl -s --max-time 3 http://127.0.0.1:8190/system_stats | grep -qE '"(devices|comfyui_version)"' || return 1
  local pid
  pid=$(ss -tlnp 2>/dev/null | grep ':8190 ' | grep -oP 'pid=\\K[0-9]+' | head -1)
  [ -n "$pid" ] && tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -q venv-cuda
}
log "lane2 看护启动（20s 巡检）"
while :; do
  if [ -f "$FLAG" ] || systemctl --user is-active --quiet llama-server; then
    sleep 20; continue
  fi
  if ! pg_alive; then
    log "PG500 ComfyUI 不在岗 → 拉起"
    bash "$H3/start_comfy_pg.sh" || log "start_comfy_pg.sh 非零，稍后重试"
    sleep 30
    continue
  fi
  sleep 20
done
""")
    os.chmod(P3, 0o755)
    print("3) comfy_pg_resident.sh OK")
else:
    print("3) comfy_pg_resident.sh 已存在，skip")

# ---------- 4) h3-comfyui2.service ----------
P4 = pathlib.Path("/home/ruanlinbo/.config/systemd/user/h3-comfyui2.service")
if not P4.exists():
    P4.write_text("""# [CEIL5] lane2：PG500 专属 ComfyUI(:8190) 看护单元
[Unit]
Description=OM101 lane2 PG500 ComfyUI resident supervisor

[Service]
ExecStart=/bin/bash /home/ruanlinbo/harness/h3/comfy_pg_resident.sh
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
""")
    print("4) h3-comfyui2.service OK")
else:
    print("4) h3-comfyui2.service 已存在，skip")

# ---------- 5) daemon 双车道 ----------
P5 = f"{H3}/h3_queue_daemon.py"
s = rd(P5)
if "[CEIL5]" in s:
    print("5) daemon 已打双车道补丁，skip")
else:
    shutil.copy2(P5, P5 + ".bak-ceil5")
    o = 'COMFY = "http://127.0.0.1:8188"'
    n = ('COMFY = os.environ.get("H3_COMFY_URL", "http://127.0.0.1:8188")  # [CEIL5] lane2 daemon→:8190\n'
         'COMFY_SVC = os.environ.get("H3_COMFY_SVC", "h3-comfyui")  # [CEIL5] 自愈目标单元\n'
         'LANE = os.environ.get("H3_LANE", "1")  # [CEIL5] "1"=V100 主车道(默认) "2"=PG500 快道(只吃 lane==fast)')
    assert s.count(o) == 1, f"D1 count={s.count(o)}"
    s = s.replace(o, n)

    o = 'JOB_TIMEOUT = 1800  # 单条任务超时'
    n = ('JOB_TIMEOUT = 1800  # 单条任务超时\n\n'
         'def _lane_match(jf):\n'
         '    """[CEIL5] 只认自己车道的任务；lane==fast 属 PG500 快道。坏文件留主道按旧行为处理。"""\n'
         '    try:\n'
         '        ln = json.load(open(jf, encoding="utf-8")).get("lane", "")\n'
         '    except Exception:\n'
         '        return LANE == "1"\n'
         '    return (ln == "fast") if LANE == "2" else (ln != "fast")\n\n'
         'def _te_of(job):\n'
         '    """[CEIL5] 快道强制小编码器：PG500 32G 装不下 32B int4(14.2G)+DiT(20G)+VAE。"""\n'
         '    if LANE == "2":\n'
         '        return "small"\n'
         '    return job.get("te") or "large"')
    assert s.count(o) == 1, f"D2 count={s.count(o)}"
    s = s.replace(o, n)

    o = 'jobs = sorted(glob.glob(f"{QUEUE}/*.json"))'
    n = 'jobs = [jf for jf in sorted(glob.glob(f"{QUEUE}/*.json")) if _lane_match(jf)]  # [CEIL5]'
    assert s.count(o) == 2, f"D3 count={s.count(o)}"
    s = s.replace(o, n)

    o = 'subprocess.run(["systemctl", "--user", "restart", "h3-comfyui"], timeout=30)'
    n = 'subprocess.run(["systemctl", "--user", "restart", COMFY_SVC], timeout=30)  # [CEIL5]'
    assert s.count(o) == 1, f"D4 count={s.count(o)}"
    s = s.replace(o, n)

    o = '(job.get("te") or "large") == "large"'
    n = '_te_of(job) == "large"'
    assert s.count(o) == 2, f"D5 count={s.count(o)}"
    s = s.replace(o, n)

    o = 'log(f"H3 队列守护进程启动（轮询 {POLL}s，空闲判定 {IDLE_SECONDS}s，超时 {JOB_TIMEOUT}s）")'
    n = 'log(f"H3 队列守护进程启动 LANE={LANE} @ {COMFY}（轮询 {POLL}s，空闲判定 {IDLE_SECONDS}s，超时 {JOB_TIMEOUT}s）")  # [CEIL5]'
    assert s.count(o) == 1, f"D6 count={s.count(o)}"
    s = s.replace(o, n)
    wr(P5, s)
    py_compile.compile(P5, doraise=True)
    print("5) daemon 双车道补丁 OK（py_compile 通过）")

# ---------- 6) h3-queue2.service ----------
P6 = pathlib.Path("/home/ruanlinbo/.config/systemd/user/h3-queue2.service")
if not P6.exists():
    P6.write_text("""# [CEIL5] lane2 队列守护：只吃 lane==fast 任务 → PG500 :8190，强制 te=small
[Unit]
Description=OM101 lane2 (PG500 fast lane) queue daemon

[Service]
ExecStart=/usr/bin/python3 /home/ruanlinbo/harness/h3/h3_queue_daemon.py
Restart=always
RestartSec=10
Environment=H3_BRAIN_NONE=1
Environment=H3_REV2=1
Environment=H3_LANE=2
Environment=H3_COMFY_URL=http://127.0.0.1:8190
Environment=H3_COMFY_SVC=h3-comfyui2

[Install]
WantedBy=default.target
""")
    print("6) h3-queue2.service OK")
else:
    print("6) h3-queue2.service 已存在，skip")
print("ALL OK")
