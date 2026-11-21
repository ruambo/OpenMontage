#!/usr/bin/env bash
# OM101 预warm v2（v1 教训：字段名要匹配 unet_name/clip_name/vae_name；且必须 ionice -c3，
# 否则与 ComfyUI 启动抢 68MB/s 的盘，反把启动拖爆超时）。
# 用途：把当前工作流所需权重预读进页缓存，让下一次 ComfyUI 冷启动走内存。
set -u
H3=/home/ruanlinbo/harness/h3
M="$H3/ComfyUI/models"
LOG=/home/ruanlinbo/h3-logs/prewarm.log

python3 - "$M" <<'PY' > /tmp/om101_prewarm_files.txt
import re, os, sys
M = sys.argv[1]
want = set()
try:
    s = open('/home/ruanlinbo/harness/h3/payload_portrait_girl_v1.json', encoding='utf-8').read()
    for m in re.findall(r'"(?:unet_name|clip_name|vae_name)"\s*:\s*"([^"]+)"', s):
        want.add(os.path.basename(m))
except Exception:
    pass
have = {}
for root, _, files in os.walk(M):
    for f in files:
        if f.endswith(('.safetensors', '.pth', '.ckpt')):
            have.setdefault(f, os.path.join(root, f))
sel = [have[w] for w in want if w in have]
print('\n'.join(sel))
PY

# ionice -c3 = IO 空闲级：ComfyUI 自己读盘时自动让路，纯捡空闲带宽
[ -s /tmp/om101_prewarm_files.txt ] && ionice -c3 nice -n19 xargs -r -P 2 -I{} \
  sh -c 'n=$(basename "{}"); s=$(date +%s); cat "{}" >/dev/null 2>&1; e=$(date +%s); m=$(du -m "{}" 2>/dev/null | cut -f1); echo "$(date "+%F %T") prewarm $n $((e-s))s ${m}M"' \
  < /tmp/om101_prewarm_files.txt >> "$LOG" 2>&1
