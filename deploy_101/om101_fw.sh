#!/usr/bin/env bash
# OM101 待办① 网络层版：nft 精准锁 8189（比开 ufw 稳：不碰 22/tailscale/11435 等其它服务）
set -e
if nft -a list chain ip filter INPUT 2>/dev/null | grep -q "om101-8189"; then
  echo "already applied"
else
  nft add rule ip filter INPUT iifname "lo" tcp dport 8189 counter accept comment "om101-8189"
  nft add rule ip filter INPUT iifname != "lo" ip saddr 192.168.2.141 tcp dport 8189 counter accept comment "om101-8189"
  nft add rule ip filter INPUT iifname != "lo" tcp dport 8189 counter drop comment "om101-8189"
  echo "rules added"
fi

cat > /etc/nftables-om101.nft <<'EOF'
table ip filter
chain INPUT {
  iifname "lo" tcp dport 8189 counter accept comment "om101-8189"
  iifname != "lo" ip saddr 192.168.2.141 tcp dport 8189 counter accept comment "om101-8189"
  iifname != "lo" tcp dport 8189 counter drop comment "om101-8189"
}
EOF

cat > /etc/systemd/system/om101-8189-guard.service <<'EOF'
[Unit]
Description=OM101 nft guard for 8189 bridge port
After=network-online.target nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-om101.nft
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable om101-8189-guard.service >/dev/null 2>&1
echo "persistence: $(systemctl is-enabled om101-8189-guard.service)"
nft list chain ip filter INPUT 2>/dev/null | grep om101
