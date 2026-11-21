#!/usr/bin/env bash
# [CEIL5] 双车道 + fp16 + cosy改道 语义自检（幂等只读，可反复跑）
H3=/home/ruanlinbo/harness/h3
ok=0; bad=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "  ✓ $1"; ok=$((ok+1)); else echo "  ❌ $1"; bad=$((bad+1)); fi; }
chk "lane1 :8188 双卡可见(V100+PG500)" "curl -s -m3 http://127.0.0.1:8188/system_stats | grep -q PG500"
chk "lane2 :8190 在岗" "curl -s -m3 http://127.0.0.1:8190/system_stats | grep -q devices"
chk "lane1 生产值=fp16" "grep -q 'export H3_DIT_DTYPE=fp16' $H3/start_comfy_cuda.sh"
chk "lane1 TE→cuda:1(PG500)" "grep -q 'H3_TE_DEVICE:-cuda:1' $H3/start_comfy_cuda.sh"
chk "lane1 --cuda-device all(不钉卡)" "grep -q -- '--cuda-device all' $H3/start_comfy_cuda.sh"
chk "lane2 脚本锁 PG500 单卡" "grep -q 'CUDA_VISIBLE_DEVICES=.\${_cv_p}' $H3/start_comfy_pg.sh"
chk "lane2 端口 8190" "grep -q -- '--port 8190' $H3/start_comfy_pg.sh"
chk "daemon 有车道过滤" "grep -q '_lane_match' $H3/h3_queue_daemon.py"
chk "daemon 快道强制 te=small" "grep -q 'def _te_of' $H3/h3_queue_daemon.py"
chk "daemon 自愈单元参数化" "grep -q 'restart\", COMFY_SVC' $H3/h3_queue_daemon.py"
chk "h3-comfyui2 enabled" "systemctl --user is-enabled h3-comfyui2"
chk "h3-comfyui2 active" "systemctl --user is-active h3-comfyui2"
chk "h3-queue2 enabled" "systemctl --user is-enabled h3-queue2"
chk "h3-queue2 active" "systemctl --user is-active h3-queue2"
chk "lane2 daemon 环境=LANE2/8190" "systemctl --user show h3-queue2 -p Environment --value | grep -q 'H3_LANE=2'"
chk "cosy 改道 drop-in 生效" "systemctl --user show cosy-tts -p ExecStart --value | grep -q cosy_start_ceil5"
CPID=$(systemctl --user show cosy-tts -p MainPID --value)
TTS_CVD=$(tr '\0' '\n' < /proc/$CPID/environ 2>/dev/null | sed -n 's/^CUDA_VISIBLE_DEVICES=//p')
TTS_GPU=$(nvidia-smi --query-gpu=index,name --format=csv,noheader | awk -F, -v i="$TTS_CVD" '$1==i' )
chk "cosy 实际落 V100(CVD 动态索引)" "echo '$TTS_GPU' | grep -q V100"
chk "PG500 无 cosy 占卡(lane2 独享)" "! nvidia-smi --query-compute-apps=gpu_name,used_memory --format=csv,noheader | grep PG500 | grep -q cosy"
chk "TTS 11450 端口在岗" "ss -tln | grep -q 11450"
echo "── CEIL5 结果: $ok ✓ / $bad ❌ ──"
[ $bad -eq 0 ]
