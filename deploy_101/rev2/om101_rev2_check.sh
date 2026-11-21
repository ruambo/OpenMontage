#!/usr/bin/env bash
# OM101 rev2 自检（在 101 执行；只读，不改任何东西）。全 ✓ 才算架构就位。
H3=/home/ruanlinbo/harness/h3
pass=0; fail=0
chk(){ local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "  ✓ $name"; pass=$((pass+1));
  else echo "  ❌ $name"; fail=$((fail+1)); fi; }
not(){ ! "$@" >/dev/null 2>&1; }

echo "── rev2 架构自检 ──"
# 1. 服务面
chk "h3-comfyui 常驻看护 enabled"   systemctl --user is-enabled h3-comfyui
chk "h3-comfyui 看护 running"       systemctl --user is-active h3-comfyui
chk "h3-queue daemon running"       systemctl --user is-active h3-queue
chk "llama-resolver running"        systemctl --user is-active llama-resolver
chk "llama-server(27B) 不常驻"       not systemctl --user is-enabled llama-server
chk "cosy-tts 常态在岗"              systemctl --user is-active cosy-tts
# 2. 语义：看护逻辑三要素都在（标记避让 / 幂等拉起 / CUDA 判定）
chk "看护脚本避 .h3-llm-mode"        grep -q .h3-llm-mode $H3/comfyui_resident.sh
chk "看护走 start_comfy_cuda 幂等"   grep -q start_comfy_cuda.sh $H3/comfyui_resident.sh
chk "看护只认 venv-cuda"             grep -q venv-cuda $H3/comfyui_resident.sh
# 3. daemon rev2：直渲、不 off、能自愈
chk "daemon REV2 常量"               grep -q 'REV2 = os.environ' $H3/h3_queue_daemon.py
chk "daemon 不再盲目 off(rev2)"       grep -q '常态模式：ComfyUI 保持常驻，不 off' $H3/h3_queue_daemon.py
chk "daemon 自愈 restart h3-comfyui"  grep -q 'restart.*h3-comfyui' $H3/h3_queue_daemon.py
chk "daemon drop-in H3_REV2=1"        grep -q H3_REV2=1 ~/.config/systemd/user/h3-queue.service.d/om101.conf
# 4. 模式脚本：标记双向 + TTS 不再被 brain=none 赶走
chk "on 解除 .h3-llm-mode"           grep -q 'rm -f /home/ruanlinbo/harness/h3/.h3-llm-mode' $H3/h3_video_mode.sh
chk "off 置 .h3-llm-mode"            grep -q 'touch /home/ruanlinbo/harness/h3/.h3-llm-mode' $H3/h3_video_mode.sh
chk "off 钉路由 big"                 grep -q 'mode.\{0,2\}:.\{0,2\}big' $H3/h3_video_mode.sh
chk "brain=none 不停 cosy-tts"       grep -q 'BRAIN_SEL:-}" != none \]; then   # OM101rev2-B' $H3/h3_video_mode.sh
# 5. 路由表：flash 在 big 之前（语义顺序，不只是存在）
if python3 - "$H3/llama_resolver.py" <<'EOF'
import sys
src=open(sys.argv[1],encoding='utf-8').read()
i_f=src.find('("flash",'); i_b=src.find('("big",')
assert 0 <= i_f < i_b, "flash 不在 big 之前"
assert src.count('("flash",')==1, "flash 出现多次（表尾没删净）"
assert 'MODEL_DEFAULT = "qwen3.8-flash-next"' in src, "MODEL_DEFAULT 未切 flash"
EOF
then echo "  ✓ resolver: flash 优先 / 无重复 / 默认别名=flash-next"; pass=$((pass+1))
else echo "  ❌ resolver 路由表语义"; fail=$((fail+1)); fi
# 6. 运行时行为（非静态签名）
chk "11435 当前后端 = 141 strata"     bash -c "curl -s -m5 http://127.0.0.1:11435/health | grep -q '\"service\": *\"strata\"'"
chk "8188 CUDA ComfyUI 在岗"          bash -c "curl -s -m5 http://127.0.0.1:8188/system_stats | grep -q comfyui_version"
# 7. 常态下不该有 27B 占卡
if systemctl --user is-active --quiet llama-server; then
  echo "  ⚠ llama-server 在岗（= LLM 备选模式期间，属正常，用完 h3_video_mode.sh on 回常态）"
else
  echo "  ✓ 27B 不在岗（常态）"; pass=$((pass+1)); fi
echo "── 结果: $pass ✓ / $fail ❌ ──"
[ $fail -eq 0 ]
