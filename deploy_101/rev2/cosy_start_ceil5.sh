#!/usr/bin/env bash
# [CEIL5] cosy 挪到 V100：PG500 32G 要整卡给 lane2 ComfyUI（DiT fp8 转换峰值装不下 cosy 3G）。
# TTS 只在成片期配音，与 V100 采样错峰概率极高；峰值实测 26.1G+2.9G<32G。
# 驱动不认 PG500 的 CUDA_VISIBLE_DEVICES UUID 稳定语义，统一动态索引。
# [CEIL7] systemd 单元钉卡一律 UUID（MEMORY.md 铁律，数字索引重启会翻转）
export CUDA_VISIBLE_DEVICES="GPU-e8aa3aad-d090-9fb8-15a6-7022d67a3133"
exec /home/ruanlinbo/harness/cosyvoice-venv/bin/python /home/ruanlinbo/harness/tts/cosy_server.py --port 11450
