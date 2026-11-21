#!/usr/bin/env bash
# OM101 rev2 (2026-11-20)：ComfyUI 常驻看护 —— "视频是常态"。
#  职责：保证 8188 上始终有一个 venv-cuda ComfyUI（V100）。
#  · 8188 健康且是 venv-cuda → 仅巡检（start_comfy_cuda.sh 本身幂等，绝无双实例）
#  · 不健康 → 调 start_comfy_cuda.sh 拉起（41.7G 权重冷读需几分钟，期间任务在队列等待）
#  · .h3-llm-mode 存在 = LLM 备选模式在岗（用户 h3_video_mode.sh off 拉起 27B）→ 待命不抢卡
#  · llama-server.service 在岗（手工起的 27B 但忘了置标记）→ 同样避让
set -u
H3=/home/ruanlinbo/harness/h3
FLAG="$H3/.h3-llm-mode"
log(){ echo "[resident $(date '+%F %T')] $*"; }

comfy_alive() {
  curl -s --max-time 3 http://127.0.0.1:8188/system_stats | grep -q comfyui_version || return 1
  local pid
  pid=$(ss -tlnp 2>/dev/null | grep ':8188 ' | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$pid" ] && tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -q venv-cuda
}

log "看护启动（20s 巡检；标记 $FLAG 存在时待命；回视频常态: h3_video_mode.sh on）"
while :; do
  if [ -f "$FLAG" ]; then
    sleep 20; continue
  fi
  if systemctl --user is-active --quiet llama-server; then
    sleep 20; continue
  fi
  if ! comfy_alive; then
    log "ComfyUI 不在岗（或非 CUDA 实例）→ 拉起"
    bash "$H3/start_comfy_cuda.sh" || log "start_comfy_cuda.sh 返回非零，稍后重试"
    sleep 30
    continue
  fi
  sleep 20
done
