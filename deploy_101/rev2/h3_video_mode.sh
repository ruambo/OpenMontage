#!/usr/bin/env bash
# h3_video_mode.sh on [small|large] [--auto-fallback] [--brain=moe|ornith|none] | off | status | switch-brain moe|ornith
#
# 架构（2026-09-23 独立显卡方案）：
#   11435 = 智能路由(llama-resolver) → big(11437) → m2(11442) / vbrain-ornith(11444)
#   11437 = 大模型 qwen3.8-27B Q8（双卡，平时常驻）
#   11442 = 视频大脑 m2 越狱35B-A3B-MoE-Q5（PG500 独占）
#   11444 = 视频大脑 ornith Ornith-1.0-35B-A3B Q5_K_M（PG500 独占，与 m2 互斥）
#
#   on : 停大模型(11437)+停cosy-tts(P0a钉PG500,让路搭车) → 起大脑(11442/11444,PG500) → 预算校验(≥5300MiB：大脑已在岗后留给编码器的余量) → 起 CUDA ComfyUI(V100 独占)
#   off: 停 ComfyUI → 停大脑(11442/11444) → 恢复对话后端(默认strata@11447；27B启用才起11437)+拉回cosy-tts
#   status: 查看当前状态
#   switch-brain: 视频模式运行中在 moe/ornith 之间热切换（接受 20~60 秒空窗）
#
# 大脑映射：
#   moe    = llama-server-m2 (11442)              越狱35B-A3B-MoE-Q5，实测占 26641 MiB（缺省）
#   ornith = llama-server-vbrain-ornith (11444)   Ornith-1.0-35B-A3B Q5_K_M，实测占 25873 MiB（更宽松）
#
# 显存分布：
#   平时: V100 空 | PG500 大模型 ≈ 24G | AMD Pro VII 16G（视觉/显示；2026-10-10 换掉原 1660）
#   视频: V100 H3 独占 ≈ 30G | PG500 大脑(35B) ≈ 26G | AMD Pro VII 同上（不参与出片）
H3=/home/ruanlinbo/harness/h3
HOLD=$H3/.ui_hold

# ---- systemd 用户服务操作：systemctl 优先，libdbus 不可用时退回 busctl(sd-bus) ----
# （agent 沙箱里 systemctl --user 会报 "Failed to connect to bus"，但 busctl 可用）
svc_do() {   # svc_do start|stop|restart <unit>
  local act=$1 unit=$2 m
  case "$act" in
    start)   m=StartUnit ;;
    stop)    m=StopUnit ;;
    restart) m=RestartUnit ;;
    *) echo "svc_do: 未知动作 $act" >&2; return 2 ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && systemctl --user "$act" "$unit" 2>/dev/null; then
    return 0
  fi
  command -v busctl >/dev/null 2>&1 || return 1
  busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
      org.freedesktop.systemd1.Manager "$m" ss "$unit" replace >/dev/null 2>&1
}

svc_active() {  # 输出 active / inactive / failed / unknown
  local unit=$1 st p M=org.freedesktop.systemd1
  st=$(systemctl --user is-active "$unit" 2>/dev/null || true)
  case "$st" in
    active|inactive|activating|deactivating|failed|reloading) echo "$st"; return 0 ;;
  esac
  command -v busctl >/dev/null 2>&1 || { echo unknown; return 0; }
  p=$(busctl --user call $M /org/freedesktop/systemd1 $M.Manager GetUnit s "$unit" 2>/dev/null \
        | awk '{print $2}' | tr -d '"')
  [ -n "$p" ] || { echo unknown; return 0; }
  st=$(busctl --user get-property $M "$p" $M.Unit ActiveState 2>/dev/null \
        | sed -n 's/^s "\(.*\)"$/\1/p')
  echo "${st:-unknown}"
}

wait_unit_inactive() {  # $1=unit $2=最多秒：等进程真正退出，别让显存争抢
  local unit=$1 secs=${2:-120} i=0
  while [ "$i" -lt "$secs" ]; do
    [ "$(svc_active "$unit")" != "active" ] && return 0
    sleep 2; i=$((i+2))
  done
  return 1
}

gpu_idx_by_name() {   # 按卡名解析当前 nvidia-smi 索引（换卡/插卡后依然正确）
  nvidia-smi --query-gpu=index,name --format=csv,noheader 2>/dev/null \
    | awk -F', *' -v pat="$1" 'tolower($2) ~ tolower(pat) {print $1; exit}'
}

vram() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr '\n' '/' | sed 's|/$||'; }

gpu_free_mib() {  # $1=卡名模式：输出该卡当前 free MiB（读不到 → 空）
  local idx
  idx=$(nvidia-smi --query-gpu=index,name --format=csv,noheader 2>/dev/null \
    | awk -F', *' -v pat="$1" 'tolower($2) ~ tolower(pat) {print $1; exit}')
  [ -n "$idx" ] || return 1
  nvidia-smi -i "$idx" --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null
}

wait_gpu_free() {  # $1=卡序号(nvidia-smi index) $2=阈值MiB $3=最多秒
  local idx=$1 lim=$2 secs=${3:-60} i=0 used
  # 读不到显卡（agent 沙箱 / 无 /dev/nvidia*）时刻意不空转
  if ! nvidia-smi -L >/dev/null 2>&1; then
    sleep 8; return 0
  fi
  while [ "$i" -lt "$secs" ]; do
    used=$(nvidia-smi -i "$idx" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null)
    [ -n "$used" ] && [ "$used" -lt "$lim" ] && return 0
    sleep 2; i=$((i+2))
  done
  return 1
}

wait_http() {  # $1=url $2=最多秒
  local secs=${2:-240} i=0
  while [ $i -lt "$secs" ]; do
    curl -sf -o /dev/null --max-time 3 "$1" && return 0
    sleep 3; i=$((i+3))
  done
  return 1
}

# ---- 大脑映射与显存预算（2026-09-23 支持两选一）----
# 视频期对话大脑（都在 PG500，unit 用 Conflicts 双向互斥，只能有一个）：
#   moe    = m2 (11442)               越狱35B-A3B-MoE-Q5，实测占 26641 MiB
#   ornith = vbrain-ornith (11444)    Ornith-1.0-35B-A3B Q5_K_M，实测占 25873 MiB（更宽松）
BRAIN_SEL=${BRAIN_SEL:-moe}
# 2026-09-26：用户手动选定的大脑偏好（由 switch-brain 写入）。
# on 分支未显式给 --brain 时沿用它 —— 否则队列守护每次让路都会把选择复位成默认 moe。
VBRAIN_CHOICE=$H3/.vbrain-choice
BRAIN_UNIT() { if [ "$BRAIN_SEL" = ornith ]; then echo llama-server-vbrain-ornith; else echo llama-server-m2; fi; }
BRAIN_PORT() { if [ "$BRAIN_SEL" = ornith ]; then echo 11444; else echo 11442; fi; }
BRAIN_NAME() {
  if [ "$BRAIN_SEL" = ornith ]; then echo "Ornith-35B-A3B-Q5(25.9G)"
  else echo "35B MoE-Q5(26.7G)"; fi
}
BRAIN_BUDGET() { echo 5300; }   # 大脑在岗后，PG500 还需给 4B 编码器留出的余量（两个大脑同一门槛）
OTHER_TE()   { echo ""; }        # 无降级：35B 级是唯一选择

# 停掉视频大脑单元，遍历两个（先撤令牌再停）
stop_brains() {
  rm -f /home/ruanlinbo/harness/h3/.allow-video-mode   # 停大脑即撤令牌（off 与回滚都会走这里）
  local u
  # ★ 2026-09-30：VLM(llama-vlm) 也钉 PG500、与大模型显存互斥。它不属于"视频大脑"，
  # 但同样占着这张卡 —— 不停它，off 分支的 wait_gpu_free 永远等不到 PG500 腾空，
  # 会白等满 90 秒（实测这就是"点回平时像没反应"的真正原因）。所以这里一并停掉。
  for u in llama-vlm llama-server-vbrain-ornith llama-server-m2; do
    if [ "$(svc_active "$u")" = "active" ]; then
      echo "  → 停止 $u ..."
      svc_do stop "$u"
      wait_unit_inactive "$u" 120 && echo "    已退出" || echo "    ⚠️ 仍在退出中"
    fi
  done
}

# 回滚：停大脑 → 拉大模型 → 等 health（回到平时模式）
rollback() {
  echo "  ↩ 回滚到平时模式（停大脑 → 拉大模型）..."
  stop_brains
  # ★ 2026-10-19：平时默认后端 = strata 常驻（IQ3_S @11447）；27B 已改为手动启动。
  #   优先拉 strata（enabled 才算默认），失败再回退 27B，两者都失败则如实报错。
  if [ "$(systemctl --user is-enabled strata-iq3s-local 2>/dev/null)" = "enabled" ]; then
    systemctl --user start strata-iq3s-local 2>/dev/null || true
    if wait_http "http://127.0.0.1:11447/health" 900; then
      echo "  ✅ 已恢复：strata IQ3_S 常驻（11435 自动切回，11447 就绪）"
    else
      echo "  ⚠️ strata 未就绪，回退手动大模型 27B ..."
      svc_do start llama-server
      wait_http "http://127.0.0.1:11437/health" 300 && echo "  ✅ 27B 已恢复" || echo "  ❌ 27B 也未就绪，看 /home/ruanlinbo/llama-server.log"
    fi
  else
    svc_do start llama-server
    if wait_http "http://127.0.0.1:11437/health" 300; then
      echo "  ✅ 已回滚：大模型恢复（11435 自动切回大模型）"
    else
      echo "  ❌ 回滚后大模型未就绪，看 /home/ruanlinbo/llama-server.log"
    fi
  fi
  restore_tts
}

# ★ 2026-10-08：恢复平时模式后把 cosy-tts 拉回（on 分支让路时停掉的，best-effort 不阻塞主流程）。
# 背景：P0a 裁定(10-08 15:28)把 TTS 用 UUID 钉到 PG500（常驻 ~3.1G，来福音色提速）；
# 但视频期 35B 大脑同样落 PG500（26.6G）→ free=2766MiB < 5300MiB 预算 → 让路连环失败（10-08 19:21 起）。
# 现 on 分支让路时停 TTS；off/rollback 调本函数拉回。
restore_tts() {
  [ "$(svc_active cosy-tts)" = "active" ] && return 0
  echo "  → 拉回 cosy-tts（让路搭车停掉的，P0a 钉 PG500）..."
  if svc_do start cosy-tts && wait_http "http://127.0.0.1:11450/health" 120; then
    echo "    ✅ TTS 在线"
  else
    echo "    ⚠️ TTS 未拉起（不影响大模型，面板可手动重启）"
  fi
}

# 起大脑并等 health 就绪；失败返回 1
brain_ready() {
  local u port
  u=$(BRAIN_UNIT); port=$(BRAIN_PORT)
  echo "→ 起视频期大脑: $BRAIN_SEL → $u（$port，$(BRAIN_NAME)）..."
  svc_do start "$u"
  if wait_http "http://127.0.0.1:$port/health" 300; then
    echo "  ✅ 大脑已就绪（11435 大模型不在时自动路由到此）"
    return 0
  fi
  echo "  ❌ 大脑启动超时（>300s），看 journalctl --user -u $u"
  return 1
}

# 预算校验：PG500 当前 free 是否 ≥ 阈值；不足则硬失败(rollback+exit 1)
pg500_budget_ok() {
  local free need
  free=$(gpu_free_mib "PG500")
  need=$(BRAIN_BUDGET)
  if [ -z "$free" ]; then
    echo "  ❌ 读不到 PG500 显存，预算校验失败"
    return 1
  fi
  if [ "$free" -ge "$need" ]; then
    echo "  ✅ PG500 余量够装编码器: free=${free}MiB ≥ ${need}MiB（${BRAIN_SEL} 大脑已在岗）"
    return 0
  fi
  echo "  ❌ PG500 余量不足: free=${free}MiB < ${need}MiB（${BRAIN_SEL} 大脑已在岗，装不下 4B 编码器）"
  return 1
}

# 视频期对话大脑：只起选定大脑，不降级。返回 0=已起, 1=失败
start_video_brain() {
  # OM101: brain=none → 视频期对话脑用远端 141（strata@:8080 / ornith@:11441），不装本地大脑
  if [ "${BRAIN_SEL:-}" = none ]; then
    echo "  📡 大脑=none：跳过本地大脑与预算校验（PG500 全额留给 TE/编码器）"
    return 0
  fi
  if brain_ready && pg500_budget_ok; then
    echo "  📡 本次视频期对话大脑: $BRAIN_SEL → $(BRAIN_UNIT)"
    return 0
  fi
  echo "  ❌ $BRAIN_SEL 大脑不可用（启动失败 或 预算不足）"
  return 1
}

CMD=${1:-}
if [ -z "$CMD" ]; then
  echo "用法: $0 on [small|large] [--auto-fallback] [--brain=moe|ornith|none] | off | status | switch-brain moe|ornith" >&2
  echo "      te 缺省 = large（与 h3_gen.py 默认一致）" >&2
  exit 1
fi

# ---- 2026-11-06 并发锁（双触发事故修复）----
# 面板按钮 / 队列守护 / 终端可能同时触发本脚本，第二个实例会和第一个抢 systemctl，
# 21:39 那次就是面板点"出视频"时守护正在切换中 → 两套动作交错 → ComfyUI 被连坐杀死。
# 用共享锁（与 h3_multi_mode.sh 同一把）：拿不到锁立即退出，绝不硬闯。
# fd 9 继承判断：父脚本已持锁时（如 on 里内嵌调 h3_multi_mode.sh off），子进程直接复用。
H3_MODE_LOCK=/home/ruanlinbo/h3-logs/.h3_mode_switch.lock
case "$CMD" in
  status) ;;
  *)
    if [ -e /proc/self/fd/9 ] && [ "$(readlink /proc/self/fd/9 2>/dev/null)" = "$H3_MODE_LOCK" ]; then
      : # fd 9 已由持锁父进程继承
    else
      exec 9>>"$H3_MODE_LOCK"
      if ! flock -n 9; then
        echo "❌ 拒绝：另一个模式切换正在进行中（锁 $H3_MODE_LOCK）。"
        echo "   守护进程/面板已在处理，请等待完成（约 4 分钟），不要重复触发。"
        echo "   查看进度: python3 $H3/h3_status.py  或  h3_ctl.sh"
        exit 75
      fi
    fi
    ;;
esac

case "$CMD" in
  on)
    bash /home/ruanlinbo/harness/h3/prewarm_weights.sh >/dev/null 2>&1 &  # OM101②: 让路同时预读权重进页缓存
    TE_MODE=large; AUTOFB=0; BRAIN_EXPLICIT=0
    shift
    for a in "$@"; do
      case "$a" in
        small|large) TE_MODE=$a ;;
        --auto-fallback) AUTOFB=1 ;;
        --brain=moe|--brain=ornith|--brain=none) BRAIN_SEL=${a#--brain=}; BRAIN_EXPLICIT=1 ;;
        *) echo "未知参数: $a"; echo "用法: $0 on [small|large] [--auto-fallback] [--brain=moe|ornith|none]"; exit 1 ;;
      esac
    done
    # 2026-09-26：未显式指定 --brain 时，沿用用户上次手动选定的大脑。
    # 背景：队列守护每次让路都调 `on <te> --auto-fallback`（不带 --brain），
    # 旧行为会把 BRAIN_SEL 复位成默认 moe → 面板上点「● Ornith」会被下次让路覆盖。
    if [ "$BRAIN_EXPLICIT" = 0 ] && [ -n "${H3_BRAIN:-}" ]; then
      BRAIN_SEL="$H3_BRAIN"; echo "  （沿用 H3_BRAIN 环境变量：$BRAIN_SEL）"
    elif [ "$BRAIN_EXPLICIT" = 0 ] && [ -f "$VBRAIN_CHOICE" ]; then
      _c=$(tr -d '[:space:]' < "$VBRAIN_CHOICE" 2>/dev/null)
      case "$_c" in moe|ornith) BRAIN_SEL="$_c"; echo "  （沿用上次手动选择的大脑：$BRAIN_SEL）" ;; esac
    fi
    case "$BRAIN_SEL" in moe|ornith|none) ;; *) echo "❌ --brain 只能是 moe/ornith/none（收到 $BRAIN_SEL）"; exit 1 ;; esac
    # 2026-10-14：Ornith-1.0-35B 整机迁到 141 机常驻（192.168.2.141:11441），本机
    # llama-server-vbrain-ornith(11444) 已 mask。视频模式是队列守护驱动的，不能让过期的
    # 大脑偏好把出片流程卡住 → 自动改用本机 m2，并把偏好写回，避免每次让路重复提示。
    if [ "$BRAIN_SEL" = ornith ]; then
      echo "⚠️ Ornith 视频大脑(11444)已退役：模型已迁到 141 常驻，本机不再占 PG500。"
      echo "   本机视频期大脑自动改用 m2（11442 越狱35B-A3B-MoE-Q5）。"
      echo "   想用 Ornith 当对话大脑：模型名选「视频大脑-ornith」，11435 会路由到 141。"
      BRAIN_SEL=moe; echo moe > "$VBRAIN_CHOICE"
    fi
    # 多模型模式(m1/m2)会占住 V100/PG500，必须先退出，否则下面是静默 OOM。
    # 【2026-11-06 修复】只认 .allow-multi-mode 令牌，不再按 m1/m2 服务是否 active 判断：
    # m2 本来就是视频模式的"对话大脑"（11442 常驻在岗），按服务状态判断会在守护切换
    # 进行中重入时把视频大脑误判成多模型模式 → 误跑 multi-off → 拆大脑、拉大模型回 V100
    # → 与 ComfyUI 抢显存（21:39 双触发事故的直接根因）。
    # 注意：必须放在 touch 视频令牌之前 —— multi off 里新增了"视频令牌存在则拒绝"保护，
    # 先 touch 会让内嵌的 multi off 被自己的保护挡掉。
    if [ -f /home/ruanlinbo/harness/h3/.allow-multi-mode ]; then
      echo "  → 检测到多模型模式令牌在线，先退出（h3_multi_mode.sh off）..."
      timeout 240 bash "$H3/h3_multi_mode.sh" off || { echo "  ❌ 退出多模型模式失败/超时"; exit 1; }
    fi
    touch /home/ruanlinbo/harness/h3/.allow-video-mode   # 硬性门禁令牌：允许当前大脑启动
    echo "→ 视频模式: te=$TE_MODE｜对话大脑=$(BRAIN_NAME) @ $(BRAIN_UNIT)/$(BRAIN_PORT)"
    # 【2026-09-26 变更】m3 桥接已按用户要求退役 → 本段移除。
    # 【2026-10-10】用户进一步要求「直接弃用 m3」→ 单元文件已删除，桥接不再有任何恢复路径。
    # 视频模式要先停大模型才能腾 V100，这段（停大模型 → m2 就绪）11435 没有后端（503）。
    echo "→ 停止大模型（⚠️ m3 桥接已退役：到 m2 就绪为止 11435 无后端）..."
    svc_do stop llama-server
    wait_unit_inactive llama-server 150 && echo "  大模型进程已退出" || echo "  ⚠️ 大模型仍在退出中"
    # ★ 2026-10-19：本机默认后端已改为 strata 常驻（Qwen3.8-Flash-Next IQ3_S @11447），
    #   它同时占住 PG500+V100 两张 32G 卡（30.6/32.2 GiB）→ 视频期必须一并让路，
    #   否则 ComfyUI 抢不到 PG500（wait_gpu_free 会白等到超时）。
    if [ "$(svc_active strata-iq3s-local)" = "active" ]; then
      echo "  → 停止 strata-iq3s-local（IQ3_S 常驻，让出两张 32G 卡）..."
      systemctl --user stop strata-iq3s-local 2>/dev/null || true
      wait_unit_inactive strata-iq3s-local 150 && echo "    strata 已退出" || echo "    ⚠️ strata 仍在退出中"
    fi
    # ★ 2026-10-08：TTS（P0a 钉 PG500，常驻 ~3.1G）必须一起让路 ——
    # 否则 35B 大脑在岗后 PG500 free≈2766MiB < 5300MiB → 让路连环失败（10-08 19:21 起反复）。
    # 视频期大模型也停着，TTS 无调用方；off/rollback 会自动拉回（restore_tts）。
    if [ "$(svc_active cosy-tts)" = "active" ]; then
      echo "  → 停止 cosy-tts（P0a 钉 PG500，让位 35B 大脑+编码器预算）..."
      svc_do stop cosy-tts
      wait_unit_inactive cosy-tts 90 && echo "    已退出" || echo "    ⚠️ 仍在退出中"
    fi
    # 2026-09-21 修复：按卡名解析索引 —— 插入 GTX 1660 后 V100 的索引从 1 变成 2，
    # 旧写法的 wait_gpu_free 1 实际查的是 1660（永远空闲）→ 该检查形同虚设
    # 2026-10-10：GTX 1660 已换为 AMD Radeon Pro VII → nvidia-smi 只剩 PG500(0)/V100(1)，
    # 故兜底默认值由 2 改为 1（仅在按名字查不到时才会用到）。
    V100_IDX=$(gpu_idx_by_name "V100-PCIE"); V100_IDX=${V100_IDX:-1}
    if ! wait_gpu_free "$V100_IDX" 22000 90; then
      echo "  ❌ V100 显存未腾出（需 ≥22000MiB 才能装 H3），疑似被别的模型占用"
      rollback; exit 1
    fi
    echo "  ✅ V100-PCIE(index $V100_IDX) 已清空（free ≥22000MiB）"
    if ! start_video_brain; then
      # 2026-09-26：--auto-fallback 时允许大脑降级。否则一旦"偏好大脑装不下"，
      # 每次让路都会失败 → 队列陷入无限重试（正是刚修好的那类死锁）。
      # 注意：这里只降级本次执行，偏好文件不改 —— 下次仍会先试用户选的那个。
      if [ "$AUTOFB" = 1 ] && [ "$BRAIN_SEL" != moe ]; then
        echo "  ⚠️ $BRAIN_SEL 大脑起不来/预算不足 → --auto-fallback 降级为 moe"
        BRAIN_SEL=moe
        if start_video_brain; then
          echo "  ✅ 本次已降级为 moe 大脑（偏好仍是 $( [ -f "$VBRAIN_CHOICE" ] && cat "$VBRAIN_CHOICE" || echo 未设置 )，下次仍先试它）"
        else
          rollback; exit 1
        fi
      else
        rollback; exit 1
      fi
    fi
    # 2026-09-26：m3 桥接已退役，此处不再有"收掉桥接"步骤
    echo "→ 启动 CUDA ComfyUI（DiT+VAE+4B编码器 全在 V100 cuda:0，T4 已验证同卡显存；32B 大编码器走 CPU）..."
    # 【2026-11-07 修复】9>&-：绝不能把模式锁 fd 9 泄漏给 ComfyUI 子树 —— 否则 ComfyUI
    # 存活期间一直持锁，任务结束后守护的 off 永远 rc=75 被拒，视频模式回不滚
    # （11-06 23:46 事故：三条视频跑完，自动回滚被自己的锁挡住）。
    bash $H3/start_comfy_cuda.sh 9>&-
    ;;
  switch-brain)
    shift
    NEW=${1:-}
    case "$NEW" in moe|ornith) ;; *) echo "用法: $0 switch-brain moe|ornith"; exit 1 ;; esac
    if [ "$NEW" = ornith ]; then
      echo "✗ 本机 Ornith 视频大脑（llama-server-vbrain-ornith / 11444）已于 2026-10-14 退役："
      echo "  Ornith-1.0-35B-A3B Q5_K_M 整机迁到 141 机常驻（192.168.2.141:11441，32GB/Vulkan），不再占 PG500。"
      echo "  想要 Ornith 大脑 → 模型名选「视频大脑-ornith」，11435 直接路由到 141；本机视频期大脑只剩 m2(11442)。"
      exit 3
    fi
    if [ ! -f /home/ruanlinbo/harness/h3/.allow-video-mode ]; then
      echo "❌ 当前不在视频模式（没有 .allow-video-mode 令牌）"
      echo "   要进视频模式: $0 on [small|large] [--auto-fallback] --brain=$NEW"
      exit 1
    fi
    # 2026-09-26：切换前先看 ComfyUI 有没有任务在跑。在跑时编码器占着 PG500：
    # ① 停掉当前大脑 → 这段窗口完全没有对话后端（实测出现过 18 秒「生效后端: 无」）
    # ② PG500 free 达不到 27500 → 新大脑必然起不来
    # 所以此时直接拒绝，让用户等这一批出完再切。
    _q=$(curl -s -m 4 http://127.0.0.1:8188/queue 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); print(len(d.get('queue_running',[]))+len(d.get('queue_pending',[])))
except Exception:
    print('')" 2>/dev/null)
    if [ -n "$_q" ] && [ "$_q" -gt 0 ] 2>/dev/null; then
      echo "⏸ 现在有 $_q 个出片任务在跑（ComfyUI 编码器占着 PG500）—— 暂停切换大脑。"
      echo "   在跑期间切会：① 停掉当前大脑 → 这段没有对话后端 ② PG500 free 达不到 27500 → 新大脑起不来"
      echo "   请等这一批出完（ComfyUI 队列清空）后再点；或先「■ 回平时」→ 再「▶ 出视频」并指定大脑。"
      exit 4
    fi
    # 判断当前在岗的是哪个
    CUR=""
    [ "$(svc_active llama-server-vbrain-ornith)" = active ] && CUR=ornith
    [ "$(svc_active llama-server-m2)" = active ] && CUR=${CUR:-moe}
    if [ "$CUR" = "$NEW" ]; then
      echo "✅ 视频期大脑已经是 $NEW（$(BRAIN_UNIT)），无需切换"
      echo "$NEW" > "$VBRAIN_CHOICE"      # 无需切换也把偏好记下来
      exit 0
    fi
    echo "→ 视频期大脑切换: ${CUR:-无} → $NEW"
    echo "⚠️ PG500 放不下两个 35B，必须先停后起 —— 期间 20~60 秒没有对话后端（已确认接受）"
    for u in llama-server-vbrain-ornith llama-server-m2; do
      if [ "$(svc_active "$u")" = active ]; then
        echo "  → 停 $u ..."
        svc_do stop "$u"
        wait_unit_inactive "$u" 120 && echo "    已退出" || echo "    ⚠️ 仍在退出中"
      fi
    done
    # 等 PG500 真正腾出来（两个 35B 都约 26G，留足余量再起）
    PG500_IDX=$(gpu_idx_by_name "PG500"); PG500_IDX=${PG500_IDX:-0}
    FREE=""
    for i in $(seq 1 40); do
      FREE=$(nvidia-smi -i "$PG500_IDX" --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
      [ -n "$FREE" ] && [ "$FREE" -ge 27500 ] && break
      sleep 2
    done
    echo "  PG500 free=${FREE:-?}MiB（需要 ≥27500 才能装下 35B + 编码器余量）"
    BRAIN_SEL=$NEW
    if brain_ready && pg500_budget_ok; then
      echo "$NEW" > "$VBRAIN_CHOICE"      # ★ 记住手动选择：以后自动让路也用它
      echo "✅ 已切到 $(BRAIN_NAME)（$(BRAIN_UNIT) / 端口 $(BRAIN_PORT)）"
      echo "   已记住该选择（$VBRAIN_CHOICE）—— 后续队列自动让路也沿用，不会被复位"
      exit 0
    fi
    # 2026-09-26：失败必须把原大脑恢复回来 —— 旧实现只 exit 1，会把视频期对话后端整个留空
    echo "❌ 切换到 $NEW 失败：新大脑未就绪或预算不足"
    echo "   排查: journalctl --user -u $(BRAIN_UNIT) -n 50"
    if [ -n "$CUR" ]; then
      echo "↩ 恢复原大脑 $CUR ..."
      BRAIN_SEL=$CUR
      if brain_ready; then echo "  ✅ 原大脑 $CUR 已恢复（11435 稍后自动切回）"
      else echo "  ⚠️ 原大脑 $CUR 也未恢复，请人工检查 journalctl --user -u $(BRAIN_UNIT)"; fi
    fi
    exit 1
    ;;
  off)
    if [ $# -gt 1 ]; then
      echo "off 不接受参数"; echo "用法: $0 on [small|large] [--auto-fallback] [--brain=moe|ornith|none] | off | status | switch-brain moe|ornith"; exit 1
    fi
    echo "→ 停止 CUDA ComfyUI..."
    # 【2026-09-26 变更】m3 桥接已退役：不再先起 m3，直接停 m2 → 拉大模型
    # （大模型加载 1~2 分钟期间 11435 没有后端，会 503 —— 用户明确接受的代价）
    rm -f "$HOLD"     # 关视频模式 = UI 保持模式一并失效
    # ★ 2026-09-30：**先停 VLM**，再停 ComfyUI。
    # 为什么顺序重要：stop_comfy_cuda.sh 里有个"等 3 卡总显存 < 15000 MiB，最多 40 秒"的循环，
    # 而 VLM 钉着 PG500 约 20 GB → 总显存 23000+ → 那个循环必然白等满 40 秒。
    # 先把 VLM 停掉，这个等待立刻通过（实测从 40s 白等降到 0s）。
    if [ "$(svc_active llama-vlm)" = "active" ]; then
      echo "  → 停止 llama-vlm（它与大模型显存互斥，先腾 PG500）..."
      svc_do stop llama-vlm
      wait_unit_inactive llama-vlm 60 && echo "    已退出" || echo "    ⚠️ 仍在退出中"
    fi
    bash $H3/stop_comfy_cuda.sh
    # 停掉占着 PG500 的视频大脑(m2/vornith)，等显存真正腾出
    stop_brains
    PG500_IDX=$(gpu_idx_by_name "PG500"); PG500_IDX=${PG500_IDX:-0}
    wait_gpu_free "$PG500_IDX" 13500 90 && echo "  PG500(index $PG500_IDX) 已腾出（大模型需 ~24GB）" || echo "  ⚠️ PG500 仍偏满，大模型可能加载失败"
    # ★ 2026-10-19：默认后端 = strata 常驻（IQ3_S @11447）；27B 已改为手动启动。
    #   优先拉 strata（enabled 才算默认），失败再回退 27B——与 rollback() 同一逻辑。
    if [ "$(systemctl --user is-enabled strata-iq3s-local 2>/dev/null)" = "enabled" ]; then
      echo "→ 恢复默认后端 strata（双卡）..."
      systemctl --user start strata-iq3s-local 2>/dev/null || true
      if wait_http "http://127.0.0.1:11447/health" 900; then
        echo "  ✅ strata 已恢复（11435 自动切回，11447 就绪）"
      else
        echo "  ⚠️ strata 未就绪，回退手动大模型 27B ..."
        svc_do start llama-server
        wait_http "http://127.0.0.1:11437/health" 300 && echo "  ✅ 27B 已恢复" || echo "  ❌ 27B 也未就绪，看 /home/ruanlinbo/llama-server.log"
      fi
    else
      echo "→ 拉起大模型（双卡）..."
      svc_do start llama-server
      if wait_http "http://127.0.0.1:11437/health" 300; then
        echo "  ✅ 大模型已恢复（11435 自动切回大模型）"
      else
        echo "  ⚠️ 大模型启动超时，看 /home/ruanlinbo/llama-server.log"
      fi
    fi
    restore_tts
    # 铁律"绝不停小脑"的管线级兜底——恢复阶段确认常驻小脑在线
    # 2026-09-23：常驻小脑已退役 → 视频/恢复期间的对话兜底改由 m2(11442) 承担，
    # 这里只做提示，不再拉起任何"小脑"服务（单元带退役围栏，起了也会被跳过）。
    if ! curl -sf -m 5 "http://127.0.0.1:11437/health" >/dev/null && ! curl -sf -m 5 "http://127.0.0.1:11447/health" >/dev/null; then
      echo "  ⚠️ 对话后端不在线——稍后重试或看 HOLO-MONITOR"
    fi
    # 2026-09-26：m3 桥接已退役，此处不再有"收掉桥接"步骤
    echo -n "  显存: "; _v=$(vram); echo "${_v:-?} MiB"
    ;;
  status)
    if [ $# -gt 1 ]; then
      echo "status 不接受参数"; echo "用法: $0 on [small|large] [--auto-fallback] [--brain=moe|ornith|none] | off | status | switch-brain moe|ornith"; exit 1
    fi
    echo -n "大模型(11437,双卡128K):   "; svc_active llama-server
    echo -n "strata(11447,默认常驻):   "; svc_active strata-iq3s-local
    echo -n "m2兜底(11442,35B-MoE):    "; svc_active llama-server-m2
    echo -n "m2大脑(11442,PG500):      "; svc_active llama-server-m2
    echo -n "11435 路由:               "; svc_active llama-resolver
    echo -n "11435 实际后端:           "; journalctl --user -u llama-resolver -n 30 --no-pager 2>/dev/null | grep -a "生效后端" | tail -1 | sed 's/.*生效后端: \([a-z]*\).*/\1/' || echo "?"
    echo -n "ComfyUI(8188):            "; (curl -s -o /dev/null --max-time 3 http://127.0.0.1:8188/system_stats && echo running) || echo stopped
    echo -n "UI 保持模式:              "; [ -f "$HOLD" ] && echo "开启（自 $(cat "$HOLD")）" || echo "关闭"
    echo -n "11435 可用:               "; (curl -sf -o /dev/null --max-time 3 http://127.0.0.1:11435/health && echo yes) || echo no
    _u=$(gpu_free_mib "PG500"); _t=$(gpu_free_mib "V100-PCIE")
    echo "PG500  free=${_u:-?}MiB   V100 free=${_t:-?}MiB"
    echo -n "  预算提示: 大脑+编码器 需 free≥5300MiB"; [ -n "$_u" ] && { [ "$_u" -ge 5300 ] && echo " ✔" || echo " ✘"; }
    echo -n "大脑偏好(手动选择):       "; [ -f "$VBRAIN_CHOICE" ] && cat "$VBRAIN_CHOICE" || echo "（未设置 → 默认 moe）"
    echo -n "视频期大脑: "
    if [ "$(svc_active llama-server-vbrain-ornith)" = active ]; then
      echo "Ornith（llama-server-vbrain-ornith / 11444）"
    elif [ "$(svc_active llama-server-m2)" = active ]; then
      echo "越狱35B MoE-Q5（llama-server-m2 / 11442）"
    else
      echo "（无 —— 不在视频模式或大脑未起）"
    fi
    ;;
  *)
    echo "用法: $0 on [small|large] [--auto-fallback] [--brain=moe|ornith|none] | off | status | switch-brain moe|ornith"
    echo "      te 缺省 = large（与 h3_gen.py 默认一致）"
    echo "      --brain 缺省 moe（与历史行为一致）；视频模式运行中可用 switch-brain 来回切"
    exit 1
    ;;
esac
