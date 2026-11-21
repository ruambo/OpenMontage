#!/usr/bin/env python3
# rev2fix (2026-11-20 23:1x)：本机 ComfyUI fork 的 /system_stats 只返回 {system, devices}，
# 没有 comfyui_version —— daemon/看护/启动脚本三处健康判定全部改认 devices。
import io, shutil, os
H3 = "/home/ruanlinbo/harness/h3"
EDITS = [
    (f"{H3}/h3_queue_daemon.py",
     '    return isinstance(info, dict) and "comfyui_version" in info',
     '    return isinstance(info, dict) and ("devices" in info or "comfyui_version" in info)  # rev2fix: fork 只回 {system,devices}'),
    (f"{H3}/comfyui_resident.sh",
     'curl -s --max-time 3 http://127.0.0.1:8188/system_stats | grep -q comfyui_version || return 1',
     'curl -s --max-time 3 http://127.0.0.1:8188/system_stats | grep -qE \'"(devices|comfyui_version)"\' || return 1'),
    (f"{H3}/start_comfy_cuda.sh",
     "grep -q '\"comfyui_version\"'",
     "grep -qE '\"(devices|comfyui_version)\"'"),
]
rc = 0
for path, old, new in EDITS:
    s = open(path, encoding="utf-8").read()
    if "rev2fix" in s or "(devices|comfyui_version)" in s:
        print(f"= {path} 已修")
        continue
    n = s.count(old)
    if n != 1:
        print(f"❌ {path}: 锚点 {n} 次: {old[:60]!r}")
        rc = 1
        continue
    bak = path + ".bak-rev2fix"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    print(f"✓ {path}")
raise SystemExit(rc)
