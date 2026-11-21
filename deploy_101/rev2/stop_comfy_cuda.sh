#!/usr/bin/env bash
# 只停 CUDA ComfyUI（不动对话大脑 / 大模型）
# 2026-09-20 dsh 修补：读不到 nvidia-smi 的环境（agent 沙箱）不再空转 40 秒。
H3=/home/ruanlinbo/harness/h3
[ -f $H3/comfy_cuda.pid ] && kill "$(cat $H3/comfy_cuda.pid)" 2>/dev/null
# 注意：本 pkill 在脚本文件里执行，脚本自身命令行不含该模式，不会误杀调用者
pkill -f "venv-cuda/bin/python.*ComfyUI/main.py" 2>/dev/null
# [CEIL7] lane2(PG500 :8190) 同样要停——否则 27B/VLM 接管时 PG500 被顶死（11-06 显存互斥教训同族）
[ -f $H3/comfy_pg.pid ] && kill "$(cat $H3/comfy_pg.pid)" 2>/dev/null
pkill -f "main.py --listen 127.0.0.1:8190" 2>/dev/null
if nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits >/dev/null 2>&1; then
  for i in $(seq 1 20); do
    tot=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | awk '{s+=$1} END{print s+0}')
    [ "$tot" -lt 15000 ] && break
    sleep 2
  done
  echo "ComfyUI 已停（当前总显存 ${tot:-?} MiB）"
else
  sleep 5
  echo "ComfyUI 已停（本环境读不到 nvidia-smi，跳过显存等待）"
fi
