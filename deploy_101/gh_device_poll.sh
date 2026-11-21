#!/usr/bin/env bash
# 轮询 GitHub 设备授权（脚本文件由外部生成，device/user code 走环境变量）
: "${DEV_CODE:?}"; : "${USER_CODE:?}"
END=$(( $(date +%s) + 840 ))
while [ "$(date +%s)" -lt "$END" ]; do
  R=$(curl -4 -s -m 10 -d "client_id=178c6fc778ccc68e1d6a&device_code=${DEV_CODE}&grant_type=urn:ietf:params:oauth:grant-type:device_code" https://github.com/login/oauth/access_token)
  if echo "$R" | grep -q "access_token="; then
    echo "$R" | tr '&' '\n' | sed -n 's/^access_token=//p' > "$HOME/.gh_token"
    chmod 600 "$HOME/.gh_token"
    echo "TOKEN-OK"; exit 0
  fi
  if echo "$R" | grep -qE "expired_token|access_denied"; then echo "DENIED: $R"; exit 1; fi
  sleep 5
done
echo TIMEOUT; exit 1
