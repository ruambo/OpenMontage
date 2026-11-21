#!/usr/bin/env bash
# 《8点前给我》4 镜入队（lane1 正式位 bf16+32B CPU-TE，steps=8）→ 收片 → 合成 18s
set -u
B=http://192.168.2.101:8189; T=$(grep -m1 H3_BRIDGE_TOKEN ~/projects/OpenMontage/.env | cut -d= -f2)
D=$HOME/projects/OpenMontage/projects/dr_eight_am
S1="extreme close-up of a smartphone on a dark office desk at night, screen suddenly lighting up with chat notification glow, empty rainy office bokeh behind, cold blue tones, suspenseful atmosphere, slow push-in, cinematic vertical composition, film grain"
S2="time-lapse style shot, young chinese male office worker typing frantically on laptop at cluttered desk, blue laptop glow on tired face, coffee cups stacking up, window behind shifting from night rain to dawn light, vertical cinematic, film grain"
S3="bright conference room, smug middle-aged chinese male manager in glasses pointing at large bright projector screen, presenting confidently, blurred colleagues clapping in background, warm morning light, vertical cinematic slow dolly, film grain"
S4="slow-motion shot from behind, young chinese male office worker walking away confidently down office corridor, holding phone low in hand, strong morning backlight silhouette, lens flare, calm triumphant mood, vertical cinematic, film grain"
post(){ curl -s -X POST -H "X-Auth: $T" -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$1\",\"width\":480,\"height\":832,\"length\":108,\"source\":\"openmontage-dr\"}" $B/jobs | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])'; }
j1=$(post "$S1"); j2=$(post "$S2"); j3=$(post "$S3"); j4=$(post "$S4")
echo "$(date '+%F %T') 已入队 $j1 $j2 $j3 $j4" > "$D/BATCH.log"
state(){ curl -s -H "X-Auth: $T" $B/jobs/$1 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state"))' 2>/dev/null; }
for pair in "$j1:1" "$j2:2" "$j3:3" "$j4:4"; do
  id=${pair%:*}; n=${pair#*:}
  while :; do s=$(state $id)
    echo "$(date '+%T') scene$n $s" >> "$D/BATCH.log"
    [ "$s" = done ] && break
    [ "$s" = failed ] && { echo "SCENE$n FAILED" >> "$D/BATCH.log"; exit 1; }
    sleep 30
  done
  for t in 1 2 3; do
    sleep 8
    curl -s -H "X-Auth: $T" -o "$D/scene$n.mp4.part" $B/jobs/$id/file
    DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$D/scene$n.mp4.part" 2>/dev/null | cut -d. -f1)
    if [ "${DUR:-0}" -ge 3 ]; then mv "$D/scene$n.mp4.part" "$D/scene$n.mp4"; break; fi
    echo "$(date '+%T') scene$n download corrupt(dur=${DUR:-x}) retry$t" >> "$D/BATCH.log"
  done
done
bash "$D/dr_compose.sh" final "$D/scene1.mp4" "$D/scene2.mp4" "$D/scene3.mp4" "$D/scene4.mp4" >> "$D/BATCH.log" 2>&1
echo "$(date '+%F %T') ALLDONE $D/dr_final_18s.mp4" >> "$D/BATCH.log"
