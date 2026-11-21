#!/usr/bin/env bash
# 《8点前给我》合成：4 镜归一化→镜1聊天窗/镜2时间码/镜3PPT卡片/镜4立案式卡片→拼接→字幕→低频铺底
# 用法: dr_compose.sh final s1.mp4 s2.mp4 s3.mp4 s4.mp4
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
FONT="Noto Sans CJK SC"

sc720() {
  ffmpeg -y -v error -t 4.5 -i "$1" -vf "scale=720:1280:flags=lanczos,fps=24,setsar=1" \
    -c:v libx264 -preset fast -crf 19 -c:a aac -b:a 128k "$2"
}

drone() {
  [ -f drone.m4a ] || ffmpeg -y -v error \
    -f lavfi -i "sine=frequency=52:duration=18.2" -f lavfi -i "sine=frequency=104:duration=18.2" \
    -filter_complex "[0:a]volume='0.16*(0.55+0.45*sin(2*PI*t*0.9))':eval=frame[a0];[1:a]volume=0.05[a1];[a0][a1]amix=inputs=2:duration=longest,afade=t=in:d=1.5,afade=t=out:st=16.8:d=1.4" \
    -c:a aac -b:a 128k drone.m4a
}

[ $# -ge 5 ] || { echo "用法: dr_compose.sh final s1 s2 s3 s4"; exit 1; }

# 镜1：老板深夜消息（白泡+时间戳）
sc720 "$2" c1raw.mp4
ffmpeg -y -v error -i c1raw.mp4 -vf "\
drawbox=x=140:y=360:w=470:h=118:color=0xffffff@0.97:t=fill:enable='gte(t,0.7)',\
drawtext=font='$FONT':text='重写一版  明早8点要':fontsize=36:fontcolor=0x111111:x=165:y=398:enable='gte(t,0.7)',\
drawtext=font='$FONT':text='23:59':fontsize=24:fontcolor=0x999999:x=180:y=330:enable='gte(t,0.7)'" \
  -c:a copy c1.mp4

# 镜2：熬夜时间码跳变
sc720 "$3" c2raw.mp4
ffmpeg -y -v error -i c2raw.mp4 -vf "\
drawtext=font='$FONT':text='23\\:59':fontsize=44:fontcolor=0xff5252:x=40:y=60:enable='lt(t,1.5)',\
drawtext=font='$FONT':text='03\\:12':fontsize=44:fontcolor=0xff5252:x=40:y=60:enable='between(t,1.5,3)',\
drawtext=font='$FONT':text='06\\:40':fontsize=44:fontcolor=0xff5252:x=40:y=60:enable='gte(t,3)'" \
  -c:a copy c2.mp4

# 镜3：台上PPT卡片（署名被抢）
sc720 "$4" c3raw.mp4
ffmpeg -y -v error -i c3raw.mp4 -vf "\
drawbox=x=90:y=300:w=540:h=330:color=0xf5f5f5@0.97:t=fill:enable='gte(t,0.8)',\
drawbox=x=90:y=300:w=540:h=76:color=0xB71C1C@0.95:t=fill:enable='gte(t,0.8)',\
drawtext=font='$FONT':text='年度最佳提案':fontsize=42:fontcolor=white:x=(w-text_w)/2:y=316:enable='gte(t,0.8)',\
drawtext=font='$FONT':text='主讲人：王建国 总监':fontsize=40:fontcolor=0x111111:x=(w-text_w)/2:y=470:enable='gte(t,1.0)',\
drawtext=font='$FONT':text='起草：已阅（未署名）':fontsize=28:fontcolor=0x888888:x=(w-text_w)/2:y=552:enable='gte(t,1.2)'" \
  -c:a copy c3.mp4

# 镜4：反转卡片（压暗弹卡）
sc720 "$5" c4raw.mp4
ffmpeg -y -v error -i c4raw.mp4 -vf "\
drawbox=x=0:y=0:w=720:h=1280:color=black@0.72:t=fill:enable='gte(t,1.6)',\
drawbox=x=60:y=430:w=600:h=420:color=0x1e1e1e@0.98:t=fill:enable='gte(t,1.6)',\
drawtext=font='$FONT':text='邮件已发送 · 昨晚 23\\:58':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=510:enable='gte(t,1.6)',\
drawtext=font='$FONT':text='收件人：全体高管':fontsize=33:fontcolor=0xcccccc:x=(w-text_w)/2:y=618:enable='gte(t,1.8)',\
drawtext=font='$FONT':text='附件：提案原稿＋会议录音.mp3':fontsize=31:fontcolor=0xff5252:x=(w-text_w)/2:y=712:enable='gte(t,2.0)'" \
  -c:a copy c4.mp4

printf "file 'c1.mp4'\nfile 'c2.mp4'\nfile 'c3.mp4'\nfile 'c4.mp4'\n" > list.txt
ffmpeg -y -v error -f concat -safe 0 -i list.txt -c copy joined.mp4
drone
ffmpeg -y -v error -i joined.mp4 -i drone.m4a -filter_complex \
  "[0:v]ass=dr.ass:fontsdir=/usr/share/fonts[v];\
   [0:a]volume=0.95,apad=whole_dur=18,atrim=0:18[amb];[1:a]volume=1.0[dn];[amb][dn]amix=inputs=2:duration=first:dropout_transition=0[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -preset medium -crf 18 -c:a aac -b:a 160k dr_final_18s.mp4
echo "成片: $DIR/dr_final_18s.mp4"
