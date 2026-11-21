#!/usr/bin/env python3
"""在 101 上给 h3_video_mode.sh 打 --brain=none / H3_BRAIN 补丁（幂等，锚点校验）。"""
import sys

P = "/home/ruanlinbo/harness/h3/h3_video_mode.sh"
s = open(P, encoding="utf-8").read()

if "--brain=none" in s:
    print("already patched")
    sys.exit(0)

subs = [
    # 1. 参数解析接受 --brain=none
    ('--brain=moe|--brain=ornith) BRAIN_SEL=${a#--brain=}; BRAIN_EXPLICIT=1 ;;',
     '--brain=moe|--brain=ornith|--brain=none) BRAIN_SEL=${a#--brain=}; BRAIN_EXPLICIT=1 ;;'),
    # 2. 合法性校验放行 none
    ('case "$BRAIN_SEL" in moe|ornith) ;; *) echo "❌ --brain 只能是 moe 或 ornith（收到 $BRAIN_SEL）"; exit 1 ;; esac',
     'case "$BRAIN_SEL" in moe|ornith|none) ;; *) echo "❌ --brain 只能是 moe/ornith/none（收到 $BRAIN_SEL）"; exit 1 ;; esac'),
    # 3. H3_BRAIN 环境变量优先于面板偏好文件
    ('    if [ "$BRAIN_EXPLICIT" = 0 ] && [ -f "$VBRAIN_CHOICE" ]; then',
     '    if [ "$BRAIN_EXPLICIT" = 0 ] && [ -n "${H3_BRAIN:-}" ]; then\n'
     '      BRAIN_SEL="$H3_BRAIN"; echo "  （沿用 H3_BRAIN 环境变量：$BRAIN_SEL）"\n'
     '    elif [ "$BRAIN_EXPLICIT" = 0 ] && [ -f "$VBRAIN_CHOICE" ]; then'),
    # 4. none → 不装本地大脑、跳过预算（对话脑在 141）
    ('start_video_brain() {\n  if brain_ready && pg500_budget_ok; then',
     'start_video_brain() {\n'
     '  # OM101: brain=none → 视频期对话脑用远端 141（strata@:8080 / ornith@:11441），不装本地大脑\n'
     '  if [ "${BRAIN_SEL:-}" = none ]; then\n'
     '    echo "  📡 大脑=none：跳过本地大脑与预算校验（PG500 全额留给 TE/编码器）"\n'
     '    return 0\n'
     '  fi\n'
     '  if brain_ready && pg500_budget_ok; then'),
]
for old, new in subs:
    if old not in s:
        sys.exit(f"ANCHOR MISSING: {old[:60]}...")
    s = s.replace(old, new, 1)

s = s.replace("[--brain=moe|ornith]", "[--brain=moe|ornith|none]")
open(P, "w", encoding="utf-8").write(s)
print("h3_video_mode.sh patched (4 anchors)")
