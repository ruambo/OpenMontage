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
