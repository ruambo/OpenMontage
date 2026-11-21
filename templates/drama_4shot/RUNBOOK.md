# 4镜18s竖屏短剧流水线（101视频工厂）
触发词：短剧 / 短视频 / 出片。给一句话剧情就开工；不给就按"钩子→苦熬→冲突→反转"四镜自编。
## 流程
1. `cp -r templates/drama_4shot projects/<剧名>` → 改 `dr_batch.sh` 里 S1-S4 四条英文 prompt、`dr_compose.sh` 压字、`dr.ass` 四句字幕（时长表别动）
2. 开工单：`cd projects/<剧名> && nohup bash dr_batch.sh > BATCH.out 2>&1 &`（bridge :8189 提交，token 在仓库 .env H3_BRIDGE_TOKEN）
3. 完成标志：BATCH.log 尾行 ALLDONE → `dr_final_18s.mp4`；中途自检：每镜 YAVG 非黑 + tblend 差值有运动
4. **人眼终审（铁律7）**：用户说"花"就回炉，判什么是什么
## 红线（全来自 101 共享记忆/复盘）
- 禁 fp16（CEIL8 人眼全马赛克）；32B TE 必须 CPU（铁律4）；成片只走 lane1，lane2=4B草稿位
- 动 101 前先读 `ssh 101 cat ~/harness/shared-memory/MEMORY.md`；钉卡只认 UUID（值在 OM101.md）
## 节奏与体检
- 13-15.6min/镜（8步，音画联合固定~5min/镜开销）；4镜+合成 ≈1h
- 体检：`ssh 101 'bash ~/om101_ceil5_check.sh; bash ~/om101_rev2_check.sh'`（24✓+21✓）；101 重启自启（Linger=yes，5×h3+cosy enabled）
- 深度文档：deploy_101/rev2/OM101.md；验收样板片：projects/dr_eight_am/dr_final_18s.mp4《8点前给我》

## 图生视频（first_frame 路径，2026-11-21 人眼验收"像且不花"）
- 触发词：图生视频 / 用这张图出片 / I2V
- 做法：图片放 `ssh 101 /home/ruanlinbo/harness/h3/ComfyUI/input/` → 任务 json 加 `"first_frame": "<文件名>"`（其余同 T2V：width/height/length/steps/seed/lora），lane1 正式位自动走 I2V 模板
- **首帧会被拉伸到画布**：素材比例必须=画布比例（9:16 图配 480×832），否则变形
- prompt 写法 = 运动延续描述（"keeps walking, camera pushes in..."），不是重新描述画面
- 实测：480×832×27f@4步 379.6s（首跑）；成片规格按 T2V 口径加 steps/length
- 未验分支勿擅自用：last_frame 首尾帧 / AddGuide 中间帧 / ReferenceToVideo 参考图 / Director——要用时先同 seed 测试片过人眼
