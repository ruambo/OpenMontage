#!/usr/bin/env bash
# OM101 网络层守卫 v2：幂等地把 8189 白名单规则插到 ts-input jump 之前
# （tailscale 的 ts-input 链会 accept 短路，规则必须排在它前面才拦得住 tailscale 源）
set -e
# 清掉本方案旧规则（按 comment 标记找 handle，倒序删）
nft -a list chain ip filter INPUT 2>/dev/null | grep om101-8189 | grep -oE "handle [0-9]+" | awk '{print $2}' | sort -rn | while read -r h; do
  nft delete rule ip filter INPUT handle "$h" 2>/dev/null || true
done
# 反序 insert（最后 insert 的排最前）
nft insert rule ip filter INPUT iifname "tailscale0" tcp dport 8189 counter drop comment "om101-8189-tailscale"
nft insert rule ip filter INPUT iifname != "lo" tcp dport 8189 counter drop comment "om101-8189"
nft insert rule ip filter INPUT iifname != "lo" ip saddr 192.168.2.141 tcp dport 8189 counter accept comment "om101-8189"
nft insert rule ip filter INPUT iifname "lo" tcp dport 8189 counter accept comment "om101-8189"
nft list chain ip filter INPUT | grep om101-8189
