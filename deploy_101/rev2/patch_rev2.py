#!/usr/bin/env python3
# OM101 rev2 (2026-11-20)：架构反转补丁 —— 视频是常态，27B 是备选，对话脑 = 141 flash。
# 锚点式幂等补丁：锚点缺失/不唯一 → 整体不落盘并报错；重复执行安全。
# 用法: python3 patch_rev2.py [--dry]
import sys, re

DRY = "--dry" in sys.argv
H3 = "/home/ruanlinbo"  # 101 上运行本脚本（补丁在 101 本机执行）

# (path, [(anchor, replacement, marker_or_None), ...])
PATCHES = []

MODE = f"{H3}/harness/h3/h3_video_mode.sh"
DAEMON = f"{H3}/harness/h3/h3_queue_daemon.py"
RESOLVER = f"{H3}/harness/h3/llama_resolver.py"

PATCHES.append((MODE, [
    # M1: on) —— 回视频常态：解除 LLM 模式标记 + 路由回自动
    ("    touch /home/ruanlinbo/harness/h3/.allow-video-mode   # 硬性门禁令牌：允许当前大脑启动",
     "    touch /home/ruanlinbo/harness/h3/.allow-video-mode   # 硬性门禁令牌：允许当前大脑启动\n"
     "    rm -f /home/ruanlinbo/harness/h3/.h3-llm-mode   # OM101rev2-A: 回视频常态 → 解除常驻看护暂停\n"
     "    curl -s -m 5 -X POST http://127.0.0.1:11435/admin/route -H 'Content-Type: application/json' -d '{\"mode\":\"auto\"}' >/dev/null 2>&1 || true",
     "OM101rev2-A"),
    # M2: on) —— brain=none 常态不再停 cosy-tts（PG500 不抢 V100 的卡，TTS 保持常驻）
    ('    if [ "$(svc_active cosy-tts)" = "active" ]; then',
     '    if [ "$(svc_active cosy-tts)" = "active" ] && [ "${BRAIN_SEL:-}" != none ]; then   # OM101rev2-B: brain=none 不停 TTS',
     "OM101rev2-B"),
    # M3: off) —— LLM 备选模式在岗：置标记 + 路由钉到 big（27B 起来后 resolver 自动选中）
    ('    echo "→ 停止 CUDA ComfyUI..."',
     '    echo "→ 停止 CUDA ComfyUI..."\n'
     "    touch /home/ruanlinbo/harness/h3/.h3-llm-mode   # OM101rev2-C: LLM 备选模式在岗 → 常驻看护待命\n"
     "    curl -s -m 5 -X POST http://127.0.0.1:11435/admin/route -H 'Content-Type: application/json' -d '{\"mode\":\"big\"}' >/dev/null 2>&1 || true",
     "OM101rev2-C"),
]))

PATCHES.append((DAEMON, [
    # P4: 常量区
    ('COMFY = "http://127.0.0.1:8188"',
     'COMFY = "http://127.0.0.1:8188"\n'
     "# OM101 rev2 (2026-11-20)：视频是常态 —— ComfyUI 由 h3-comfyui.service 常驻看护，\n"
     "# 对话脑在 141 flash，27B 仅按需（.h3-llm-mode 标记存在 = LLM 备选模式在岗，任务排队等待）。\n"
     'REV2 = os.environ.get("H3_REV2", "") == "1"\n'
     'LLM_MODE_FLAG = f"{H3}/.h3-llm-mode"\n'
     '_last_repair = [0.0]',
     "OM101 rev2 (2026-11-20)：视频是常态"),
    # P5: 健康检查 + 等待函数（插在 ui_hold_active 的 return 之后）
    ("    return os.path.exists(HOLD)",
     "    return os.path.exists(HOLD)\n\n\n"
     "def comfyui_alive():\n"
     '    """8188 在线且返回 comfyui_version（CUDA ComfyUI 特有字段）。"""\n'
     "    try:\n"
     '        info = http_get(f"{COMFY}/system_stats", timeout=3)\n'
     "    except Exception:\n"
     "        return False\n"
     '    return isinstance(info, dict) and "comfyui_version" in info\n\n\n'
     "def wait_comfyui_ready(max_wait=2400):\n"
     '    """rev2：等常驻 ComfyUI 在岗。不在 → 5 分钟冷却地 systemctl restart h3-comfyui 自愈；\n'
     '    LLM 备选模式在岗 → 排队等人回常态（不抢卡）。"""\n'
     "    t0 = time.time()\n"
     "    while time.time() - t0 < max_wait:\n"
     "        if comfyui_alive():\n"
     "            return True\n"
     "        if os.path.exists(LLM_MODE_FLAG):\n"
     '            log("ℹ️ LLM 备选模式在岗(.h3-llm-mode)：任务排队，等待 h3_video_mode.sh on 回视频常态")\n'
     "            time.sleep(30)\n"
     "            continue\n"
     "        if time.time() - _last_repair[0] > 300:\n"
     "            _last_repair[0] = time.time()\n"
     '            log("ℹ️ ComfyUI 不在岗 → 自愈: systemctl --user restart h3-comfyui")\n'
     "            try:\n"
     '                subprocess.run(["systemctl", "--user", "restart", "h3-comfyui"], timeout=30)\n'
     "            except Exception:\n"
     "                pass\n"
     "        time.sleep(10)\n"
     "    return False",
     "def wait_comfyui_ready"),
    # P6: 让路门 → rev2 直渲
    ('    ok = switch("on", te=batch_te)\n    if not ok:',
     '    if REV2:\n'
     '        log("▶ 常态视频模式：ComfyUI 常驻在岗则直渲，不让路/不回滚")\n'
     "        ok = wait_comfyui_ready()\n"
     "    else:\n"
     '        ok = switch("on", te=batch_te)\n'
     "    if not ok:",
     "常态视频模式：ComfyUI 常驻在岗则直渲"),
    # P7: finally 不再 off（rev2）
    ("    finally:\n        if ui_hold_active():",
     "    finally:\n"
     "        if REV2:\n"
     '            log("常态模式：ComfyUI 保持常驻，不 off、不回滚 LLM")\n'
     "        elif ui_hold_active():",
     "ComfyUI 保持常驻，不 off"),
    # P8: 主循环 LLM 空闲等待在 rev2 下跳过（对话脑在 141，本地无大脑可让）
    ("            if jobs:\n                wait_for_llm_idle()",
     "            if jobs:\n                if not REV2:\n                    wait_for_llm_idle()",
     "if not REV2:\n                    wait_for_llm_idle()"),
    # P9: 异常路径也不 off
    ('                    switch("off")  # 异常也保证 LLM 在',
     '                    if not REV2:\n'
     '                        switch("off")  # 异常也保证 LLM 在（rev2：常态即视频，无需回滚）',
     "rev2：常态即视频"),
]))

PATCHES.append((RESOLVER, [
    # R1: 表尾原 flash 兜底项移除（先删再插，避免重复行）
    ('    ("flash",   "192.168.2.141", 8080, 262144),  # strata Qwen3.8-Flash-Next @141（262K MoE）\n',
     '    # OM101 rev2 (2026-11-20)：flash 元组已提到 BACKENDS 表首（对话脑常态 = 141 flash）\n',
     "flash 元组已提到 BACKENDS 表首"),
    # R2: flash 提到首位（对话脑常态；big 只在 off 钉 big 或 flash 死掉时被选中）
    ('BACKENDS = [\n    ("big",     "127.0.0.1", 11437, 131072),  # 平时模式：27B Q8 双卡',
     'BACKENDS = [\n'
     "    # OM101 rev2 (2026-11-20)：对话脑常态 = 141 qwen3.8-flash(:8080)。101 的 27B 降级为按需备选\n"
     "    # （h3_video_mode.sh off 才在岗；off 经 /admin/route 把路由钉 big，on 回 auto）。\n"
     '    ("flash",   "192.168.2.141", 8080, 262144),  # strata Qwen3.8-Flash-Next @141（对话脑常态）\n'
     '    ("big",     "127.0.0.1", 11437, 131072),  # 平时模式：27B Q8 双卡',
     "101 的 27B 降级为按需备选"),
    # R3: 默认模型别名 → flash（目录里 qwen3.8-flash-next 已映射后端 flash）
    ('MODEL_DEFAULT = "qwen3.8-27b-q8"          # 兼容既有配置：自动（大模型优先；不在时按健康度落到 m1/m2/视频后端）',
     'MODEL_DEFAULT = "qwen3.8-flash-next"          # OM101 rev2：对话脑常态 = 141 flash（27B 仅 off 备选模式在岗）',
     "OM101 rev2：对话脑常态 = 141 flash（27B 仅 off 备选模式在岗）"),
]))


def apply():
    rc = 0
    for path, patches in PATCHES:
        src = open(path, encoding="utf-8").read()
        orig = src
        for anchor, repl, marker in patches:
            if marker and marker in src:
                print(f"  = {path}: 已打过（{marker}）")
                continue
            n = src.count(anchor)
            if n != 1:
                print(f"  ❌ {path}: 锚点出现 {n} 次（需恰好 1）：{anchor[:60]!r}")
                rc = 1
                continue
            src = src.replace(anchor, repl, 1)
            print(f"  ✓ {path}: +{marker or anchor[:40]!r}")
        if src == orig:
            continue
        if rc:
            print(f"  ⛔ {path}: 存在失败锚点，整个文件不落盘")
            return 1
        if DRY:
            print(f"  (dry-run) 本应写入 {path}")
            continue
        open(path, "w", encoding="utf-8").write(src)
    return rc


if __name__ == "__main__":
    sys.exit(apply())
