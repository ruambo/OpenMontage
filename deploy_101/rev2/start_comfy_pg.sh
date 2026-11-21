#!/usr/bin/env bash
# MiniMax H3 ComfyUI — [CEIL5] lane2: PG500 专属实例 :8190（由 start_comfy_cuda.sh 派生）
# 只应吃 lane==fast 任务（daemon2 已强制 te=small：32B int4 与 DiT 在 32G 内挤不下）
# 与 ROCm 版 start_comfy.sh 并存，互不干扰（同一份 ComfyUI + 同一批模型）
#
# 2026-09-20 dsh 修补：幂等化。
#   原版"8190 已被占用 → exit 1"会让「UI 保持模式下有任务入队」必然失败
#   （守护进程 switch("on") 收到非 0 → 判定让路失败 → 任务一直重试）。
#   现在：8190 上跑的就是 ComfyUI 本体 → 直接复用，返回 0。
set -eu
H3=/home/ruanlinbo/harness/h3

# 2026-09-21 加固：复用前必须确认 8190 上是 venv-cuda 实例。
#   教训：comfy-h3.service（ROCm 时代单元，用 venv=ROCm torch 2.5.1+rocm6.1，
#   只认得到 AMD 卡）开机自启抢占 8190，本脚本盲目复用 → 视频任务跑在 ROCm 后端上，
#   采样即报 "HIP error: invalid device function"（2026-09-21 00:50/00:54 两任务失败根因）。
#   注意：ROCm 实例的 /system_stats 同样返回 comfyui_version，无法靠接口区分，只能查进程。
if curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8190/system_stats; then
  if curl -s --max-time 5 http://127.0.0.1:8190/system_stats | grep -qE '"(devices|comfyui_version)"'; then
    pid=$(ss -tlnp 2>/dev/null | grep ':8190 ' | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "${pid:-}" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "venv-cuda"; then
      echo "✅ 8190 已有 CUDA ComfyUI 在运行（pid $pid），直接复用（不重启）"
      exit 0
    fi
    echo "⚠️  8190 被非 venv-cuda 的 ComfyUI 占用（pid ${pid:-?}: $(tr '\0' ' ' < /proc/${pid:-0}/cmdline 2>/dev/null | cut -c1-100)）→ 终止后按 CUDA 配置重启"
    [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
    for i in $(seq 1 15); do
      ss -tln 2>/dev/null | grep -q ':8190 ' || break
      sleep 1
    done
  else
    echo "⚠️  8190 被非 ComfyUI 进程占用，拒绝启动"
    exit 1
  fi
fi

export HOME=$H3/home
# OM101②：任何未来的 inductor/triton 编译缓存钉到持久盘（当前 --disable-triton-backend，纯保险）
export TORCHINDUCTOR_CACHE_DIR=$H3/home/.cache/torchinductor
export TRITON_CACHE_DIR=$H3/home/.cache/triton
# [CEIL7] lane2 钉死 PG500（真 UUID，索引重启会翻转，见 MEMORY.md）
export CUDA_VISIBLE_DEVICES="GPU-9522238e-f1e2-6930-da67-f1aad8cb3e24"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
# 编码器放 V100 上（cuda:0），消除 P2P 通信开销
# 2026-09-24 dsh：H3 全线 NaN 根因修复（详见 shared-memory/notes/2026-09-24-H3-NaN根因-fp16动态范围.md）
#   ① 文本条件投影 condition_proj 在 fp16 下溢出（幅值 1.5e4 → 输出 7.2e4 > 65504）
#      → custom_nodes/minimax_h3_textproj_fp32.py 导入即生效，无需改工作流
#   ② 该 checkpoint 的 DiT 激活量级达 3e5，fp16 装不下 → 计算 dtype 必须用 bf16
#      （H3_TEXTFP32 / H3_DIT_DTYPE 由该 custom node 读取）
export H3_TEXTFP32=1
export H3_DIT_DTYPE=bf16   # [CEIL9 试验] fp16草稿带马赛克(CEIL8人眼撤回)，V100实测bf16 DiT 23.5G，PG500 32G能否吃下待本试验；若OOM退回fp16并在描述标注草稿花屏；
#   TextFP32 节点 ×1/8+clamp 兜住 3e5 激活不 NaN（2026-11-21 实测同 seed 无黑屏）。同 seed 输出风格会变，
#   QC 对比视频见 141:~/projects/OpenMontage/projects/rev2_speed_test/qc/。回退：改回 bf16 并重启 h3-comfyui
# 残差流缩放补丁已证伪（MLP/attention 输入过 RMSNorm 尺度不变，溢出不随流缩放消失）→ 显式关闭
export H3_RESID_SCALE=1
# 32B 编码器放 CPU：V100 已被 DiT(20G)+VAE(5.5G) 占满，放卡上会 OOM
export H3_TE_DEVICE=${H3_TE_DEVICE:-cuda:0}  # [CEIL5] lane2 单卡 PG500：TE 放本卡，须配 te=small

cd $H3/ComfyUI
nohup $H3/venv-cuda/bin/python main.py \
  --cuda-device all \
  --disable-auto-launch \
  --port 8190 \
  --listen 127.0.0.1 \
  --fast-disk --disable-pinned-memory \
  --disable-triton-backend \
  --lowvram --disable-dynamic-vram \
  --disable-cuda-malloc \
  > $H3/comfy_pg.log 2>&1 &
echo $! > $H3/comfy_pg.pid
echo "🚀 CUDA ComfyUI 启动中 (PID $(cat $H3/comfy_pg.pid))，日志: $H3/comfy_pg.log"

for i in $(seq 1 40); do
  if curl -s -o /dev/null --max-time 3 http://127.0.0.1:8190/system_stats; then
    echo "✅ 启动成功 → http://127.0.0.1:8190"
    exit 0
  fi
  sleep 3
done
echo "❌ 启动超时，看日志: tail -40 $H3/comfy_pg.log"
exit 1
