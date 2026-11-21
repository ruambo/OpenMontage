#!/usr/bin/env bash
# OM101 rev2 部署（在 101 上执行）：视频转常态、27B 转按需、对话脑指 141 flash。
# 安全护栏：队列非空 / ComfyUI 正在出图 / 模式锁被占 → 拒绝执行。
# 回滚：*.bak-om101rev2 原样 cp 回去 + 单元反向操作。
set -euo pipefail
H3=/home/ruanlinbo/harness/h3
SRC=$HOME/om101-rev2          # scp 落地点
TS=$(date +%F-%H%M)
fail(){ echo "❌ $*"; exit 1; }

echo "== 0) 护栏检查 =="
Q=$(find "$H3/queue" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)   # 空目录/无文件都必须平稳给 0（pipefail 下 ls 通配符会炸）
[ "$Q" -eq 0 ] || fail "队列还有 $Q 个任务，等跑完再来"
RUN=$(curl -s -m 5 http://127.0.0.1:8188/queue 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('queue_running',[]))+len(d.get('queue_pending',[])))" 2>/dev/null) || RUN=0   # ComfyUI 本就停着（off 窗口）视为空闲
[ "${RUN:-0}" = "0" ] || fail "ComfyUI 还有 $RUN 个渲染中/排队中 prompt"
flock -n "$H3/.h3_mode_switch.lock" true || fail "模式锁被占用（有切换在途）"
echo "   队列空闲 ✓  渲染空闲 ✓  模式锁空闲 ✓"

echo "== 1) 备份 =="
for f in h3_video_mode.sh h3_queue_daemon.py llama_resolver.py; do
  [ -f "$H3/$f.bak-om101rev2" ] || cp -a "$H3/$f" "$H3/$f.bak-om101rev2"
done
echo "   *.bak-om101rev2 ✓（已存在则不覆盖）"

echo "== 2) 打补丁 =="
python3 "$SRC/patch_rev2.py" || fail "补丁锚点失败"
bash -n "$H3/h3_video_mode.sh" || fail "h3_video_mode.sh 语法"
python3 -m py_compile "$H3/h3_queue_daemon.py" "$H3/llama_resolver.py" || fail "python 语法"
grep -q 'OM101rev2-A' "$H3/h3_video_mode.sh" && grep -q 'REV2 = ' "$H3/h3_queue_daemon.py" && grep -q 'MODEL_DEFAULT = "qwen3.8-flash-next"' "$H3/llama_resolver.py" || fail "补丁签名缺失"
echo "   12 处补丁全部落盘且签名可查 ✓"

echo "== 3) 先停 h3-queue（ComfyUI 现在挂在它的 cgroup 里，会被带走；看护单元马上会拉回来）=="
systemctl --user stop h3-queue.service

echo "== 4) 常驻看护单元 =="
install -m 755 "$SRC/comfyui_resident.sh" "$H3/comfyui_resident.sh"
install -Dm 644 "$SRC/h3-comfyui.service" "$HOME/.config/systemd/user/h3-comfyui.service"
systemctl --user daemon-reload
systemctl --user enable --now h3-comfyui.service

echo "== 5) daemon 开 rev2 =="
DROP=$HOME/.config/systemd/user/h3-queue.service.d/om101.conf
mkdir -p "$(dirname "$DROP")"; touch "$DROP"
grep -q 'H3_REV2=1' "$DROP" || echo 'Environment=H3_REV2=1' >> "$DROP"
systemctl --user daemon-reload
systemctl --user start h3-queue.service

echo "== 6) 27B 转按需 / TTS 归常态 / 视频令牌复位 =="
touch "$H3/.allow-video-mode"   # rev2 起视频=常态，令牌常驻
rm -f "$H3/.h3-llm-mode"        # 部署即回到视频常态
systemctl --user disable llama-server.service 2>/dev/null || true
if systemctl --user is-active --quiet llama-server; then
  echo "   llama-server 在岗（v1 daemon 收尾拉起的）→ 停掉，回到视频常态（看护会自动补位 ComfyUI）"
  systemctl --user stop llama-server.service
fi
systemctl --user start cosy-tts.service || echo "   ⚠ cosy-tts 启动失败（不影响视频常态）"

echo "== 7) 路由表生效（会瞬断 11435 上的在途请求，几秒自愈）=="
systemctl --user restart llama-resolver.service

echo "== 8) 验证 =="
sleep 6
B=$(systemctl --user is-active h3-comfyui h3-queue llama-resolver h3-bridge cosy-tts 2>&1 | paste -sd' ')
EN=$(systemctl --user is-enabled h3-comfyui llama-server 2>&1 | paste -sd' ')
HEALTH=$(curl -s -m 5 http://127.0.0.1:11435/health | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('service','?'),d.get('model','?'))" 2>/dev/null || echo "?")
COMFY=$(curl -s -m 5 http://127.0.0.1:8188/system_stats | grep -c comfyui_version || true)
echo "   services(h3-comfyui h3-queue resolver bridge cosy-tts): $B"
echo "   enabled(h3-comfyui llama-server): $EN"
echo "   11435 当前后端: $HEALTH"
echo "   8188 comfyui_version 命中: $COMFY（0=还在加载权重，几分钟后复查 curl :8188/system_stats）"
echo "✅ rev2 部署完成。日常：出视频=投队列即可；要 27B=h3_video_mode.sh off；回常态=h3_video_mode.sh on"
