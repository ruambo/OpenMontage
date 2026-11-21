#!/usr/bin/env bash
# 在 141 上执行：scp rev2 套件 → 101:~/om101-rev2 → 远端跑 apply_rev2.sh
set -euo pipefail
cd "$(dirname "$0")"
ssh -o BatchMode=yes 101 'mkdir -p ~/om101-rev2'
scp -q -o BatchMode=yes patch_rev2.py comfyui_resident.sh h3-comfyui.service apply_rev2.sh 101:om101-rev2/
ssh -o BatchMode=yes 101 'bash ~/om101-rev2/apply_rev2.sh'
