#!/usr/bin/env bash
# OM101 待办①②一键应用（本机执行；备份 *.bak-om101-todo）
set -euo pipefail
H3=/home/ruanlinbo/harness/h3
HERE="$(cd "$(dirname "$0")" && pwd)"
S="ssh -o BatchMode=yes 101"

echo "== 上传 =="
scp -q "$HERE/h3_bridge.py" "$HERE/prewarm_weights.sh" 101:/tmp/
$S "python3 -m py_compile /tmp/h3_bridge.py && bash -n /tmp/prewarm_weights.sh && chmod +x /tmp/prewarm_weights.sh"

echo "== ① 部署带白名单的 bridge 并重启 =="
$S "cp /tmp/h3_bridge.py $H3/h3_bridge.py && python3 -m py_compile $H3/h3_bridge.py && systemctl --user restart h3-bridge.service && sleep 1 && systemctl --user is-active h3-bridge.service"

echo "== ② 补丁：on 时后台预热权重 + 编译缓存钉到持久盘（幂等） =="
$S "cp -n $H3/h3_video_mode.sh $H3/h3_video_mode.sh.bak-om101-todo 2>/dev/null || true
cp -n $H3/start_comfy_cuda.sh $H3/start_comfy_cuda.sh.bak-om101-todo 2>/dev/null || true
cp /tmp/prewarm_weights.sh $H3/prewarm_weights.sh && chmod +x $H3/prewarm_weights.sh
python3 - <<'PY'
m='/home/ruanlinbo/harness/h3/h3_video_mode.sh'
s=open(m,encoding='utf-8').read()
if 'prewarm_weights' not in s:
    o='    TE_MODE=large; AUTOFB=0; BRAIN_EXPLICIT=0'
    assert o in s,'anchor mode'
    s=s.replace(o,'    bash /home/ruanlinbo/harness/h3/prewarm_weights.sh >/dev/null 2>&1 &  # OM101②: 让路同时预读权重进页缓存\n'+o,1)
    open(m,'w',encoding='utf-8').write(s); print('mode patched')
else: print('mode already')
c='/home/ruanlinbo/harness/h3/start_comfy_cuda.sh'
s=open(c,encoding='utf-8').read()
if 'TORCHINDUCTOR_CACHE_DIR' not in s:
    o='export HOME=\$H3/home'
    assert o in s,'anchor comfy'
    s=s.replace(o,o+'\n# OM101②：任何未来的 inductor/triton 编译缓存钉到持久盘（当前 --disable-triton-backend，纯保险）\nexport TORCHINDUCTOR_CACHE_DIR=\$H3/home/.cache/torchinductor\nexport TRITON_CACHE_DIR=\$H3/home/.cache/triton',1)
    open(c,'w',encoding='utf-8').write(s); print('comfy patched')
else: print('comfy already')
PY
bash -n $H3/h3_video_mode.sh && bash -n $H3/start_comfy_cuda.sh && echo SYNTAX_OK"

echo "== 验证 =="
TOKEN=$($S "cat ~/.h3_bridge_token")
echo -n "LAN(141→101)      : "; curl -s -o /dev/null -w "%{http_code}\n" -H "X-Auth: $TOKEN" http://192.168.2.101:8189/health
echo -n "tailscale 越权源  : "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 -H "X-Auth: $TOKEN" http://100.124.12.108:8189/health || true
echo -n "101 本机回环      : "; $S "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8189/health"
echo "== 预热脚本单跑（应 0 报错；缓存热时秒级） =="
$S "time bash $H3/prewarm_weights.sh; tail -2 /home/ruanlinbo/h3-logs/prewarm.log 2>/dev/null"
echo DONE
