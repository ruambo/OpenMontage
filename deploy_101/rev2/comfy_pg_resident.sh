#!/usr/bin/env bash
# [CEIL5] lane2 看护：保证 8190 上始终有 PG500 版 ComfyUI。避让逻辑同主车道
#（27B 备选模式的 dual-GPU 也要 PG500，故 LLM 模式标记/llama-server 在岗时同样待命）。
set -u
H3=/home/ruanlinbo/harness/h3
FLAG="$H3/.h3-llm-mode"
log(){ echo "[resident-pg $(date '+%F %T')] $*"; }
pg_alive() {
  curl -s --max-time 3 http://127.0.0.1:8190/system_stats | grep -qE '"(devices|comfyui_version)"' || return 1
  local pid
  pid=$(ss -tlnp 2>/dev/null | grep ':8190 ' | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$pid" ] && tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -q venv-cuda
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
