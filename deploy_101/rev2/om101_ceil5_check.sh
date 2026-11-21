#!/usr/bin/env bash
# [CEIL5+6+7] 双车道 + fp16 + CPU-TE(铁律4) + 真UUID钉卡 + NaN护栏 语义自检（幂等只读）
H3=/home/ruanlinbo/harness/h3
V100_UUID=GPU-e8aa3aad-d090-9fb8-15a6-7022d67a3133
PG_UUID=GPU-9522238e-f1e2-6930-da67-f1aad8cb3e24
ok=0; bad=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "  ✓ $1"; ok=$((ok+1)); else echo "  ❌ $1"; bad=$((bad+1)); fi; }
chk "lane1 :8188 双卡可见(V100+PG500)" "curl -s -m3 http://127.0.0.1:8188/system_stats | grep -q PG500"
chk "lane2 :8190 在岗" "curl -s -m3 http://127.0.0.1:8190/system_stats | grep -q devices"
chk "lane1 生产值=bf16(CEIL8人眼终审)" "grep -q ':-${H3_DIT_DTYPE:-bf16}' $H3/start_comfy_cuda.sh"
chk "lane1 TE→cpu(铁律4: 32B TE必须CPU)" "grep -q 'H3_TE_DEVICE:-cpu' $H3/start_comfy_cuda.sh"
chk "lane1 --cuda-device all(不钉卡)" "grep -q -- '--cuda-device all' $H3/start_comfy_cuda.sh"
chk "lane1 脚本钉真UUID双卡对(CEIL7)" "grep -q $V100_UUID $H3/start_comfy_cuda.sh && grep -q $PG_UUID $H3/start_comfy_cuda.sh"
chk "lane2 脚本锁 PG500 真UUID单卡(CEIL7)" "grep -q $PG_UUID $H3/start_comfy_pg.sh"
chk "lane2 DiT=bf16(CEIL9)" "grep -q ^export.H3_DIT_DTYPE=bf16 $H3/start_comfy_pg.sh"
chk "lane2 端口 8190" "grep -q -- '--port 8190' $H3/start_comfy_pg.sh"
chk "stop 脚本连 lane2 一起收(CEIL7)" "grep -q '8190' $H3/stop_comfy_cuda.sh"
chk "daemon 有车道过滤" "grep -q '_lane_match' $H3/h3_queue_daemon.py"
chk "daemon 快道强制 te=small" "grep -q 'def _te_of' $H3/h3_queue_daemon.py"
chk "daemon NaN黑帧护栏(CEIL7)" "grep -q 'def _black_check' $H3/h3_queue_daemon.py && grep -q 'black-suspect-nan' $H3/h3_queue_daemon.py"
chk "daemon 自愈单元参数化" "grep -q 'restart\", COMFY_SVC' $H3/h3_queue_daemon.py"
chk "h3-comfyui2 enabled" "systemctl --user is-enabled h3-comfyui2"
chk "h3-comfyui2 active" "systemctl --user is-active h3-comfyui2"
chk "h3-queue2 enabled" "systemctl --user is-enabled h3-queue2"
chk "h3-queue2 active" "systemctl --user is-active h3-queue2"
chk "lane2 daemon 环境=LANE2/8190" "systemctl --user show h3-queue2 -p Environment --value | grep -q 'H3_LANE=2'"
chk "cosy 改道 drop-in 生效" "systemctl --user show cosy-tts -p ExecStart --value | grep -q cosy_start_ceil5"
CPID=$(systemctl --user show cosy-tts -p MainPID --value)
chk "cosy 实际落 V100(按PID查卡,CEIL7)" "nvidia-smi --query-compute-apps=pid,gpu_name --format=csv,noheader | grep -w $CPID | grep -q V100"
chk "PG500 无 cosy 占卡(lane2 独享)" "! nvidia-smi --query-compute-apps=gpu_name,process_name --format=csv,noheader | grep cosy | grep -q PG500"
chk "TTS 11450 端口在岗" "ss -tln | grep -q 11450"
LPID=$(pgrep -f 'main.py --cuda-device all --disable-auto-launch --port 8188' | head -1)
chk "lane1 运行态 env: 真UUID对+TE=cpu" "tr '\0' '\n' < /proc/$LPID/environ | grep -q '^CUDA_VISIBLE_DEVICES=$V100_UUID,$PG_UUID' && tr '\0' '\n' < /proc/$LPID/environ | grep -q '^H3_TE_DEVICE=cpu'"
echo "── CEIL5/6/7 结果: $ok ✓ / $bad ❌ ──"
[ $bad -eq 0 ]
