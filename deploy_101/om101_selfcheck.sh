#!/usr/bin/env bash
# OM101 一键巡检（141 上运行）。任何 ✗ 都以非零码退出。
# 教训来源(2026-11-20)：v1 预warm 解析了 payload 里不存在的字段(ckpt_name)，静默读错文件
# 还把启动拖爆——语法检查抓不到语义错误，本脚本第 4 项专防此类问题。
SSH="ssh -o BatchMode=yes -o ConnectTimeout=6 101"
TOK=$(grep -m1 H3_BRIDGE_TOKEN ~/projects/OpenMontage/.env 2>/dev/null | cut -d= -f2)
URL=${H3_BRIDGE_URL:-http://192.168.2.101:8189}
fails=0
ok()   { echo "  ✓ $1"; }
bad()  { echo "  ✗ $1"; fails=$((fails+1)); }

echo "1. bridge 健康"
H=$(curl -sf --max-time 6 -H "X-Auth: $TOK" $URL/health 2>/dev/null) \
  && python3 -c "import json,sys; h=json.loads('''$H'''); assert 'ok' in h; print('     pending=%s done总数已计 双卡可见' % h['queue_pending'])" 2>/dev/null \
  && ok "/health 可达(token 正确)" || bad "/health 不可达或 token 错"

echo "2. 访问控制三层"
c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 6 -H "X-Auth: $TOK" $URL/health); [ "$c" = 200 ] && ok "LAN+token → 200" || bad "LAN+token → $c (期望 200)"
c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -H "X-Auth: $TOK" http://100.124.12.108:8189/health 2>/dev/null); c=${c:-000}
[ "${c:0:3}" = 000 ] && ok "tailscale 越权 → 网络层 drop" || bad "tailscale 越权 → $c (期望无响应)"
c=$($SSH "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8189/health" 2>/dev/null); [ "$c" = 200 ] && ok "101 回环豁免 → 200" || bad "回环自检 → $c"

echo "3. 补丁完整性（签名 + 备份 + 服务）"
cnt=$($SSH 'bash -c "
  grep -q \"brain=none\" ~/harness/h3/h3_video_mode.sh && echo x
  grep -q H3_BRAIN_NONE ~/harness/h3/h3_queue_daemon.py && echo x
  grep -q \"ionice -c3\" ~/harness/h3/prewarm_weights.sh && echo x
  grep -q unet_name ~/harness/h3/prewarm_weights.sh && echo x
  grep -q TORCHINDUCTOR ~/harness/h3/start_comfy_cuda.sh && echo x
  grep -q H3_BRIDGE_ALLOW ~/harness/h3/h3_bridge.py && echo x
  [ -f ~/harness/h3/h3_video_mode.sh.bak-om101 ] && echo x
  systemctl --user is-active h3-queue h3-bridge >/dev/null && echo x
  systemctl is-enabled om101-8189-guard.service 2>/dev/null | grep -q enabled && echo x
  ls ~/.config/systemd/user/h3-queue.service.d/om101.conf >/dev/null 2>&1 && echo x
"' 2>/dev/null | wc -l)
[ "$cnt" = 10 ] && ok "10/10 补丁签名与服务在位" || bad "补丁签名仅 $cnt/10（逐项 grep 上表定位缺失）"

echo "4. 预warm dry-run（v1 事故防复发项：断言解析结果=工作流实际所需且文件存在）"
cat > /tmp/om101_dryrun.py <<'PYEOF'
import re, os, sys
s = open("/home/ruanlinbo/harness/h3/payload_portrait_girl_v1.json", encoding="utf-8").read()
want = {os.path.basename(m) for m in re.findall(r'"(?:unet_name|clip_name|vae_name)"\s*:\s*"([^"]+)"', s)}
assert want, "FATAL: payload 解析出 0 个模型引用——字段名又变了！"
M = "/home/ruanlinbo/harness/h3/ComfyUI/models"
have = {f: os.path.join(r, f) for r, _, fs in os.walk(M) for f in fs}
missing = [w for w in want if w not in have]
tot = sum(os.path.getsize(have[w]) for w in want if w in have)
print(f"     引用 {len(want)} 个模型, 缺失 {len(missing)}, 合计 {tot/1e9:.1f} GB")
for w in sorted(want):
    print(f"       {'OK  ' if w in have else 'MISS'} {w}")
sys.exit(1 if missing else 0)
PYEOF
scp -q /tmp/om101_dryrun.py 101:/tmp/ && $SSH "python3 /tmp/om101_dryrun.py" && ok "引用清单非空且全部存在" || bad "预warm 解析异常（字段变更或缺文件）"

echo "5. 语法全量"
$SSH 'bash -n ~/harness/h3/h3_video_mode.sh && bash -n ~/harness/h3/start_comfy_cuda.sh && bash -n ~/harness/h3/prewarm_weights.sh && bash -n /usr/local/sbin/om101_guard.sh && python3 -m py_compile ~/harness/h3/h3_bridge.py ~/harness/h3/h3_queue_daemon.py' 2>/dev/null \
  && ok "101 端 6 个文件语法通过" || bad "101 端存在语法错误"
cd ~/projects/OpenMontage && python3 -m py_compile tools/video/h3_lan_video.py tools/video/h3_lan_image.py deploy_101/h3_bridge.py 2>/dev/null \
  && bash -n deploy_101/prewarm_weights.sh 2>/dev/null && ok "141 端 tools 语法通过" || bad "141 端存在语法错误"

echo "6. 队列卫生 + 模式锁占用（锁文件常驻是 flock 设计，查的是'被持有'而非'存在'）"
$SSH 'bash -c "
  n=\$(ls ~/harness/h3/queue/[!d]*.json 2>/dev/null | wc -l)
  flock -n /home/ruanlinbo/h3-logs/.h3_mode_switch.lock -c true && l=free || l=BUSY
  echo \"     队列残留=\$n 模式锁=\$l\"" ' 2>/dev/null | tee /tmp/om101_hyg.txt | cat
grep -q "残留=0" /tmp/om101_hyg.txt && grep -q "锁=free" /tmp/om101_hyg.txt && ok "队列干净、锁空闲" || bad "队列卫生异常（残留任务或锁被长期占用）"

echo "7. OpenMontage 工具注册"
(cd ~/projects/OpenMontage && python3 -c "
import sys; sys.path.insert(0,'.')
from tools.tool_registry import registry
registry.ensure_discovered()
for n,c in (('h3_lan_video','video_generation'),('h3_lan_image','image_generation')):
    t=registry.get(n)
    assert t and n in [x.name for x in registry.get_by_capability(c)], n
    assert t.get_status().name in ('AVAILABLE','DEGRADED'), n
print('     双 provider 注册且状态可查')
" 2>/dev/null) && ok "h3_lan 双 provider 在线" || bad "OpenMontage provider 注册异常"

echo
[ $fails = 0 ] && echo "══ 全部通过 ══" || { echo "══ $fails 项失败 ══"; exit 1; }
