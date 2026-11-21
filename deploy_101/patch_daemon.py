#!/usr/bin/env python3
"""在 101 上给 h3_queue_daemon.py 打 H3_BRAIN_NONE 开关补丁（幂等）。
守护自动让路时：H3_BRAIN_NONE=1 → h3_video_mode.sh on 附带 --brain=none。"""
import sys

P = "/home/ruanlinbo/harness/h3/h3_queue_daemon.py"
s = open(P, encoding="utf-8").read()

if "H3_BRAIN_NONE" in s:
    print("already patched")
    sys.exit(0)

old = '        args.append(te or "large")\n        args.append("--auto-fallback")'
if old not in s:
    sys.exit("ANCHOR MISSING")
new = old + (
    "\n        if os.environ.get(\"H3_BRAIN_NONE\") == \"1\":\n"
    "            args.append(\"--brain=none\")  # OM101: 对话脑=141，不装本地大脑"
)
open(P, "w", encoding="utf-8").write(s.replace(old, new, 1))
print("h3_queue_daemon.py patched")
