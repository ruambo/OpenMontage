#!/usr/bin/env bash
# OM101 一键应用到 101（本机执行，经 ssh 生效；备份 *.bak-om101 可回滚）
set -euo pipefail
H3=/home/ruanlinbo/harness/h3
HERE="$(cd "$(dirname "$0")" && pwd)"
SSHOPTS="-o BatchMode=yes"

echo "== 0. 备份 =="
ssh $SSHOPTS 101 "for f in h3_video_mode.sh h3_queue_daemon.py; do [ -f $H3/\$f.bak-om101 ] || cp $H3/\$f $H3/\$f.bak-om101; done"

echo "== 1. 上传补丁与 bridge，远端校验语法 =="
scp -q "$HERE/patch_mode.py" "$HERE/patch_daemon.py" "$HERE/h3_bridge.py" 101:/tmp/
ssh $SSHOPTS 101 "python3 /tmp/patch_mode.py && python3 /tmp/patch_daemon.py \
  && cp /tmp/h3_bridge.py $H3/h3_bridge.py \
  && bash -n $H3/h3_video_mode.sh && python3 -m py_compile $H3/h3_queue_daemon.py $H3/h3_bridge.py && echo SYNTAX_OK"

echo "== 2. 令牌 + h3-bridge 服务 =="
TOKEN=$(ssh $SSHOPTS 101 "cat ~/.h3_bridge_token 2>/dev/null || { tr -dc a-f0-9 </dev/urandom 2>/dev/null | head -c 32 || openssl rand -hex 16; } | tee ~/.h3_bridge_token >/dev/null && cat ~/.h3_bridge_token")
[ -n "$TOKEN" ] || { echo "TOKEN GEN FAILED"; exit 1; }
ssh $SSHOPTS 101 "mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/h3-bridge.service <<EOF
[Unit]
Description=OM101 H3 queue HTTP bridge (:8189)
[Service]
ExecStart=/usr/bin/python3 $H3/h3_bridge.py
Environment=H3_HOME=$H3
Environment=H3_BRIDGE_PORT=8189
Environment=H3_BRIDGE_TOKEN=$TOKEN
Restart=on-failure
RestartSec=4
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now h3-bridge.service && sleep 2 && systemctl --user is-active h3-bridge.service"

echo "== 3. h3-queue 让路默认 --brain=none（drop-in，可删） =="
ssh $SSHOPTS 101 "mkdir -p ~/.config/systemd/user/h3-queue.service.d && printf '[Service]\nEnvironment=H3_BRAIN_NONE=1\n' > ~/.config/systemd/user/h3-queue.service.d/om101.conf && systemctl --user daemon-reload && systemctl --user restart h3-queue.service && sleep 1 && systemctl --user is-active h3-queue.service"

echo "== 4. health =="
sleep 1
curl -s -H "X-Auth: $TOKEN" http://192.168.2.101:8189/health; echo
echo "TOKEN=$TOKEN"
