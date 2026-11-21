#!/usr/bin/env python3
"""11435 智能路由：按后端健康度转发 + 上下文安全网，消除 LLM 切换期间的 503 空窗。

为什么需要它：
  1) 视频模式下大模型必须让出显存，模型加载要 40-60 秒。AionUi 遇 503 不是快速失败
     而是退避重试（实测卡到 7 分钟），所以这里放一个常驻小脑兜底，客户端**永远看不到 503**。
  2) 小上下文后端装不下 AionUi 那种 5 万 token 的长对话，直接转发会得到
     400 exceed_context_size_error。因此转发前按需**修剪历史**：保留 system +
     能放下的**最大**后缀，**只在 role=="user" 的边界切**，保证 tool 配对不被拆散。

2026-10-06 P1-A v2 上下文安全网（本次改造）：
  * 计数不再靠字符系数猜 —— 直接问**即将转发的那一个后端**要精确值：
      POST /apply-template {"messages":..., "tools":...} → prompt
      POST /tokenize {"content": prompt}                → len(tokens)
    任一环节失败/超时(2.5s)/非 200/缺字段 → 退回字符估算器
    （estimate_tokens_fallback）并 60s 防刷屏 warning。
  * 修剪触发：精确值 > int(max_ctx * TRIM_TRIGGER=0.92)；
    修剪后**复算精确值**，仍 > max_ctx → 回 413（不转发）。SAFETY=0.80 不变。
  * trim_messages 修好：旧版 `reversed(starts)` 一触发就只留**最后一条 user**
    （466 条 → 1 条）。现在对用户切点做**二分**找"能放下的最大后缀"
    （235 个 user 候选 ≤ 8 次计数），检测到不单调则 ±2 线性确认。
  * 新增 GET /admin/clients：按 peername + /proc/net/tcp{,6} inode → pid →
    comm/cmdline 归属客户端，供面板显示"是谁在用"。
"""
import asyncio
import json
import logging
import os
import socket
import struct
import time
from collections import OrderedDict

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 11435
H3 = "/home/ruanlinbo/harness/h3"      # 供 /admin/video 调 h3_video_mode.sh

BACKENDS = [
    ("big",     "127.0.0.1", 11437, 131072),  # 平时模式：27B Q8 双卡
    # ★ 2026-10-19 13:0x：本机 strata 常驻换成 **UD-IQ4_XS**（unsloth 93.68GB 三片，sm_70 CUDA，:11448）。
    #   用户裁定：UD-IQ4_XS 常驻、IQ3_S(11447) 退下。质量档：UD-IQ4_XS 介于 IQ3_S 与 UD-Q4_K_XL 之间，prefill 更快
    #   （30K 1665 t/s / 80K 1936 t/s），decode 与并发略低于 IQ3_S（69.4 vs 80.3 / 83.8 vs 99.7），属体积换质量。
    #   27B 已改为【手动启动】（user 单元 disabled）→ 平时只有本服务在线，故排在 big 之后、其它之前。
    #   与 27B / IQ3_S 显存互斥：strata-iq4xs-local.service 与二者互相 Conflicts，不会同时健康，优先级天然安全。
    #   上下文 262144（模型原生 262K，KV int8 流式 + --kv-resident 16384：K/V 主体在 pinned RAM，VRAM 只留 20480 cells/层）。
    ("strata-local", "127.0.0.1", 11448, 262144),  # 本机 strata UD-IQ4_XS 常驻（默认后端；2026-10-19 13:2x 抬到模型原生 262K）
    # ★ 2026-10-14：Ornith-1.0-35B-A3B Q5_K_M 整机迁到 141 机常驻 ——
    #   192.168.2.141:11441，卡 = PCI 46:00.0（32GB Vega20/Duo，ROCm 7.x 跑不动、Vulkan 正常），
    #   单元 = 141 上的 user unit ornith-35b（开机自启 + Restart=always）。
    #   本机 llama-server-m1(11441) / llama-server-vbrain-ornith(11444) 已 mask 退役，
    #   V100-PCIE 与 PG500 都不再为 Ornith 让路；模型文件本机副本已删除。
    #   vornith 与 m1 指向同一个远端端点：目录里的「视频大脑-ornith」这个名字照旧可用。
    ("m1",      "192.168.2.141", 11441, 131072),   # Ornith-1.0-35B-A3B Q5_K_M @141 常驻（Vulkan/32G）
    ("vornith", "192.168.2.141", 11441, 131072),   # 同名远端端点（本机不再占 PG500）
    # brain(11438 旧 27B 大脑) / brain14(11439 旧 14B 大脑) 已于 2026-09-26 一并移除：
    # 两者都是退役单元（带退役围栏，起不来），留在 BACKENDS 里只会有两个坏处：
    #   ① /admin/route 仍接受 {"mode":"brain"} → 把路由强制指向一个死后端（全客户端 503）
    #   ② 健康检查白跑两个端口
    # 视频期对话大脑现在是 m2(11442) 与 vornith(11444)，见下方 BACKENDS。
    ("m2",      "127.0.0.1", 11442, 131072),  # 多模型模式：35B-A3B MoE Q5_K @PG500 全GPU；视频模式下亦可当大脑
    # (mini, 11440) 已于 2026-09-23 按用户要求退役（常驻小脑不再使用）
    # m3（14B @ GTX1660, 11443）已于 2026-09-26 按用户要求退役 —— 后端与名册条目一并移除
    #
    # ★ 2026-09-30 新增：VLM（Qwen3-VL-30B-A3B-Instruct）当【最后兜底】的对话后端。
    # 为什么需要它：VLM 模式与大模型显存互斥（VLM 钉 PG500，大模型要 V100+PG500 双卡），
    # 所以 VLM 一开，big/m1/m2/vornith **全都起不来** → 11435 对任何请求都 503，
    # hermes / QQ 机器人 / Open WebUI 全断。而这个模型本身就是 Instruct、能纯文本对话，
    # 拿来兜底总好过没有后端。
    # 放在列表**最后** = 优先级最低：只有其它后端都不健康时才会被选中，不影响日常路由。
    # 注意它上下文只有 16K（其余后端是 128K），长对话会被 router 按 SAFETY 比例裁剪。
    # ★ 2026-10-15 新增：141 机 strata Qwen3.8-Flash-Next（262K MoE，/health /props 都支持）。
    #   起因：07:11 事故——11437 的 175K KV 是 2 槽共享，两个 agent 会话（115K+59K）把它填满
    #   → 新请求分配不到 KV → llama.cpp batch 1960→32 全失败 → 会话"挂死"（服务端并没死）。
    #   单请求安全网防不了"多会话共同填满 KV"，长上下文会话改走 flash（见 BIG_LONG_TOKENS 导流）。
    ("flash",   "192.168.2.141", 8080, 262144),  # strata Qwen3.8-Flash-Next @141（262K MoE）
    ("vlm",     "127.0.0.1", 11445, 16384),   # VLM 模式：Qwen3-VL-30B-A3B-Instruct @PG500（多模态，兜底对话）
]
# 注（2026-10-14 更新）：m1 与 vornith 现在指向同一个 141 端点。保留两条名目，
# 只是让「ornith-1.0-35b」和「视频大脑-ornith」两个目录名各自解析到同一个常驻后端。

# ============ 对外可选模型目录（2026-09-22） ============
# agent（AionUi / OpenClaw / Hermes / Open WebUI）在模型下拉框里看到的名字 → 指定后端。
# 后端 llama.cpp 不校验 model 字段，所以直接透传即可，无需改写请求体。
MODEL_DEFAULT = "qwen3.8-27b-q8"          # 兼容既有配置：自动（大模型优先；不在时按健康度落到 m1/m2/视频后端）
MODEL_CATALOG = [
    # (对外模型名, 目标后端, 展示说明)
    ("qwen3.8-27b-q8",                None,   "自动路由：大模型优先(V100+PG500 双卡 128K)，不在时按健康度落到多模型/视频后端"),
    # 2026-09-26：以下 6 个指向 brain/brain14 的死名字已删除（后端已退役）：
    #   qwen3.8-27b-q8-brain / damanao / 视频大脑 / 视频大脑轻 / qwen3.8-27b-q8-brain14 / damanao14
    # 现役视频期大脑是「视频大脑-ornith」(vornith/11444) 与「视频大脑-越狱」(m2/11442)，见下。
    ("ornith-1.0-35b",                "m1",     "Ornith-1.0-35B-A3B Q5_K_M @141机 32G(Vulkan) 128K（混合注意力 MoE，常驻）"),
    ("ornith",                        "m1",     "Ornith（简写别名，@141 常驻）"),
    ("qwen3.8-27b-q8-v100",           "m1",     "旧名，现指向 141 上的 Ornith-1.0-35B-A3B Q5_K_M"),
    ("越狱35B-A3B-MoE-Q5",            "m2",   "越狱35B-A3B MoE Q5_K @PG500 全GPU 128K（无审查·最快）"),
    ("xiaonao-full",                  "m2",   "越狱35B MoE Q5（旧代号 xiaonao-full，保留兼容）"),
    ("小脑全速",                      "m2",     "多模型2（中文别名）"),
    ("视频大脑-ornith",               "vornith","视频期对话大脑(可选)：Ornith-1.0-35B-A3B Q5_K_M @141机常驻 128K（本机不占卡）"),
    ("视频大脑-越狱",                 "m2",     "视频期对话大脑(可选)：越狱35B-A3B-MoE-Q5 @PG500 128K"),
    # (qwen3-14b → m3) 已于 2026-09-26 随 m3 退役移除
    ("qwen3vl-30b",                   "vlm",    "VLM模式：Qwen3-VL-30B-A3B-Instruct @PG500 多模态 16K（可看图，也当纯文本兜底）"),
    ("VLM视觉模型",                    "vlm",    "VLM模式（中文别名）：Qwen3-VL-30B-A3B-Instruct，可传图"),
    ("qwen3.8-flash-next",            "flash",  "Qwen3.8-Flash-Next @141机 strata 262K（MoE，长上下文 agent 会话首选）"),
    ("qwen3.8-flash-next-local",      "strata-local", "本机 strata UD-IQ4_XS（sm_70 CUDA，93.7GB）262K（MoE，默认后端；旧名沿用）"),
    ("strata-local",                  "strata-local", "本机 strata UD-IQ4_XS 简写别名"),
    ("qwen3.8-flash-next-iq4xs",      "strata-local", "本机 strata UD-IQ4_XS 显式名（2026-10-19 起为默认后端，262K）"),
    ("UD-IQ4_XS",                     "strata-local", "UD-IQ4_XS 直白显示名（四端下拉框用这个）"),
    ("qwen3.8-flash-next-ud-iq4_xs",  "strata-local", "UD-IQ4_XS 全名别名（大小写不敏感客户端友好）"),
    ("iq4xs",                         "strata-local", "UD-IQ4_XS 最短别名"),
    ("iq3s",                          "strata-local", "旧名(IQ3_S 已退下)：仍指向本机 strata，实际是 UD-IQ4_XS 后端"),
    ("flash-next",                    "flash",  "Qwen3.8-Flash-Next 简写别名（@141 常驻）"),
]
MODEL_ALIAS = {m: b for m, b, _ in MODEL_CATALOG}

HEALTH_TIMEOUT = 1.5
REFRESH_INTERVAL = 2.0
CONNECT_TIMEOUT = 10.0
MAX_BODY = 32 * 1024 * 1024
SAFETY = 0.80            # 修剪到上限的 80%，留生成余量
TRIM_TRIGGER = 0.92      # 精确值达到 ctx 的 92% 就提前修剪（P1-A v2：计数已是精确值，0.92 只是提前量）
CHARS_PER_TOKEN = 2.2    # 中文保守估算（仅兜底估算器使用）
# ★ 2026-10-15 长上下文导流（07:11 KV 挤爆事故教训，见 BACKENDS 里 flash 条目注释）：
#   当时瓶颈是 11437 的 175K 共享 KV，>60K tokens 的请求导流到 flash（141 strata 262K）。
# ★ 2026-10-19 停用：默认后端换成 strata-local（现 = UD-IQ4_XS :11448，131K），KV int8 流式走 pinned RAM，
#   长上下文不吃显存，长请求留本地跑——更快且不依赖 141 网络。
#   flash 仍在 BACKENDS 优先级里，本地不健康时自然兜底。要恢复导流把开关改回 True。
LONG_CTX_STEER = False
BIG_LONG_TOKENS = 60000
STEER_MIN_BODY = 100_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("resolver")

_health: dict = {}
_current = None
_FORCE_FILE = "/home/ruanlinbo/harness/h3/.resolver_forced"


def _load_forced():
    try:
        v = open(_FORCE_FILE, encoding="utf-8").read().strip()
        return v if v in {n for n, _, _, _ in BACKENDS} else None
    except Exception:
        return None


_forced = _load_forced()   # 手动强制后端（None=自动）；持久化到 _FORCE_FILE，重启不丢


def _body_is_loaded(data: bytes) -> bool:
    """HTTP 200 之后的二次判活：body 里**确实带了** loaded 字段时，用它投票。

    ★ 2026-10-15 事故（08:21-08:36）：141 机 strata（flash 后端）的引擎 SIGABRT 崩溃后，
    supervisor 一边以 ~15 分钟重载 90GB 权重，一边继续用 **HTTP 200** 应答 /health：
        {"status":"ok","max_context":262144,"model":"...","loaded":false,"service":"strata"}
    旧实现只认 " 200 " → 整个重载窗口 flash 都被判为**健康**。而当天新上线的长上下文导流
    （>60K tokens 优先走 flash）正指向它 → 全机长上下文 agent 会话被静默导到这台**假健康**
    后端，请求挂 225-334s 后收到空流（客户端超时取消，看着像"模型坏了"）。
    与 07:11 的 KV 挤爆是同一类故障模式：服务端"在"，但答不了。

    只对**明确带 loaded 字段**的 /health 生效；llama.cpp 的 {"status":"ok"} 没有该字段 →
    照旧只看 200，行为与修改前一致（不会把正常后端误判死）。
    非 JSON / JSON 被截断 / 不是 dict → 同样退回只看 200。
    """
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        return True
    body = data[sep + 4:].strip()
    if not body:
        return True
    try:
        obj = json.loads(body)
    except Exception:
        return True
    if not isinstance(obj, dict) or "loaded" not in obj:
        return True
    return bool(obj["loaded"])


async def _probe(host, port):
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=HEALTH_TIMEOUT)
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await asyncio.wait_for(writer.drain(), timeout=HEALTH_TIMEOUT)
        # 1024（原 200）：headers 就占掉 ~150B，只读 200 会把 JSON body 截断，
        # 于是 loaded 检查永远解析失败、静默退回旧行为 —— 等于没改。
        data = await asyncio.wait_for(reader.read(1024), timeout=HEALTH_TIMEOUT)
        if b" 200 " not in data:
            return False
        return _body_is_loaded(data)
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


def candidates():
    """按优先级返回健康后端；若手动强制了某个后端，它在最前（失败仍可回落）。"""
    order = list(BACKENDS)
    if _forced:
        order.sort(key=lambda b: 0 if b[0] == _forced else 1)
    return [b for b in order if _health.get(b[0])]


def pick_backend():
    if _forced:                                  # 手动强制：指定后端健康就用它，否则回落到自动
        for name, host, port, ctx in BACKENDS:
            if name == _forced and _health.get(name):
                return name, host, port, ctx
    for name, host, port, ctx in BACKENDS:
        if _health.get(name):
            return name, host, port, ctx
    return None


_ctx_live: dict = {}      # 修复3：后端由不健康变健康时探测 /props 的 n_ctx，裁剪优先用它


async def _probe_ctx(host, port, timeout=1.5):
    """取后端 /props 的 default_generation_settings.n_ctx；失败返回 None（静默退回静态值）。"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.write(b"GET /props HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        data = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        writer.close()
        body = data.split(b"\r\n\r\n", 1)[-1]
        doc = json.loads(body)
        n = doc.get("default_generation_settings", {}).get("n_ctx")
        return int(n) if isinstance(n, int) else None
    except Exception:
        return None


async def refresh_loop():
    global _current
    while True:
        results = await asyncio.gather(*[_probe(h, p) for _, h, p, _ in BACKENDS])
        for (name, host, port, _), ok in zip(BACKENDS, results):
            prev = _health.get(name)
            _health[name] = ok
            if ok and not prev:          # 不健康 → 健康的那一次才探测 ctx
                n = await _probe_ctx(host, port)
                if n:
                    _ctx_live[name] = n
                    log.info("ctx 自动探测: %s=%d", name, n)
                else:
                    _ctx_live.pop(name, None)   # 探测失败退回静态值
        best = pick_backend()
        cur = best[0] if best else None
        if cur != _current:
            log.info("生效后端: %s  (健康: %s)", cur or "无", dict(_health))
            _current = cur
        await asyncio.sleep(REFRESH_INTERVAL)


async def read_head(reader, limit=64 * 1024):
    """读 HTTP 请求头，返回 (head, 头之后多读到的字节)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            return buf, b""
        buf += chunk
        if len(buf) > limit:
            break
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return buf, b""
    return buf[: idx + 4], buf[idx + 4:]


def parse_head(head: bytes):
    try:
        lines = head.split(b"\r\n")
        method, path, _ = lines[0].decode("latin-1").split(" ", 2)
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
        return method, path, headers
    except Exception:
        return None, None, {}


# ===================== 精确 token 计数（P1-A v2 核心） =====================
# 旧版按字符系数估算（CJK 1.7 / 非 CJK 2.5），对符号密集内容会低估 ~1.6 倍
# （本机 /tokenize 实测：纯符号 1.53 chars/token、JSON 2.06、aionrs 会话 2.54、
# 英文 4.89）⇒ 2026-09-23 那次"32,982 token 打 32,768 ctx 没被修剪"的老 bug 会回归。
# 现在直接问目标后端要精确值：/apply-template（套 chat 模板）+ /tokenize（数 token）。
# 模板与分词器因模型而异，所以**必须打到即将转发的那一个后端**（host/port 由调用方传入，
# 不写死 11437）。短超时 2.5s，失败退回字符估算器。

EXACT_TIMEOUT = 2.5
_exact_warn: dict = {}   # reason -> 上次告警的 monotonic 时间（同一原因 60s 只打一条）


def _norm_content(c):
    """把 content 里的 list 块折成字符串，让 /apply-template 认得。

    aionrs（AionUi agent）的 content 是 Anthropic 风格块列表（text / thinking /
    tool_use / tool_result），llama.cpp 的 /apply-template 只认 text/image，
    直接原样送会 400 `unsupported content[].type`。真实转发请求**原样照发**
    （后端模板引擎自己处理），这里只是计数通道的规范化副本：
      text→文本 · thinking→"thinking: …" · 其它块（tool_use/tool_result 等）→其 JSON。
    折出的文本比原块略短（丢了块的 JSON 外壳），token 数会偏小，
    但 TRIM_TRIGGER=0.92 与 SAFETY=0.80 的留量足以吸收，且修剪后的
    复算用同一口径、前后一致。
    """
    if not isinstance(c, list):
        return c
    parts = []
    for p in c:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict):
            t = p.get("type")
            if t == "text":
                parts.append(p.get("text") or "")
            elif t == "thinking":
                parts.append(f"thinking: {p.get('thinking') or p.get('text') or ''}")
            else:
                parts.append(json.dumps(p, ensure_ascii=False))
        else:
            parts.append(str(p))
    return "".join(parts)


def _countable(messages, tools=None):
    """exact_tokens 的入参规范化：content 折成字符串，其余字段一律原样保留。"""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m = dict(m)
        m["content"] = _norm_content(m.get("content"))
        out.append(m)
    return out, (tools if tools else None)


def _warn_fallback(reason):
    now = time.monotonic()
    if now - _exact_warn.get(reason, 0.0) >= 60.0:
        log.warning("精确 token 计数不可用（%s）→ 退回字符估算 estimate_tokens_fallback", reason)
        _exact_warn[reason] = now


async def _http_json(host, port, method, url, payload, timeout=EXACT_TIMEOUT):
    """对后端发一次短超时 JSON HTTP 请求，返回 (status_code, json_doc)。"""
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = (f"{method} {url} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Content-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n").encode() + body
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        data = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(1 << 20), timeout=timeout)
            if not chunk:
                break
            data += chunk
        head, _, resp_body = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(resp_body)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def exact_tokens(messages, tools=None, host=None, port=None, template_kwargs=None):
    """向目标后端要**精确** token 数。返回 (n_tokens, ok)。

    1) POST /apply-template {"messages": 原样(仅 content 块列表折成字符串, 见
       _norm_content), "tools": 原样(有才带), "chat_template_kwargs": 原样(有才带)}
       → {"prompt": 套好模板的完整字符串}
    2) POST /tokenize {"content": prompt} → {"tokens": [...]}  ⇒ len(tokens)

    诚实声明：真实转发请求是**原样**发的（后端模板引擎自己处理 aionrs 的块列表），
    计数通道只把 content 块折成字符串送 /apply-template（否则 400），所以这里的
    "精确"是**按折叠口径的精确** —— 对同一份消息前后一致，但可能与服务端最终
    实际 prefill 的 token 数有小偏差（折叠丢了块 JSON 外壳，偏小）。验收 1 会拿
    纯字符串 payload 做零偏差对照。
    任一环节失败/超时/非 200/字段缺失 → 退回 estimate_tokens_fallback 且 ok=False
    （同一原因 60s 只 warning 一次）。apply-template/tokenize 都是服务端事件循环里
    的本地小请求，不占生成槽，后端正忙也照样应答。
    """
    if host is None or port is None:
        return estimate_tokens_fallback(messages, tools), False
    t0 = time.monotonic()
    try:
        msgs_norm, tools_norm = _countable(messages, tools)
        payload = {"messages": msgs_norm}
        if tools_norm:
            payload["tools"] = tools_norm
        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs
        st, doc = await _http_json(host, port, "POST", "/apply-template", payload)
        prompt = doc.get("prompt") if isinstance(doc, dict) else None
        if st != 200 or not isinstance(prompt, str):
            _warn_fallback(f"apply-template 异常: HTTP {st}")
            return estimate_tokens_fallback(messages, tools), False
        st2, doc2 = await _http_json(host, port, "POST", "/tokenize", {"content": prompt})
        toks = doc2.get("tokens") if isinstance(doc2, dict) else None
        if st2 != 200 or not isinstance(toks, list):
            _warn_fallback(f"tokenize 异常: HTTP {st2}")
            return estimate_tokens_fallback(messages, tools), False
        log.info("精确计数: %d tokens（apply-template+tokenize 实测 %.0f ms，后端 %s:%d）",
                 len(toks), (time.monotonic() - t0) * 1000, host, port)
        return len(toks), True
    except Exception as e:  # noqa: BLE001
        _warn_fallback(type(e).__name__)
        return estimate_tokens_fallback(messages, tools), False


def estimate_tokens_fallback(messages, tools=None):
    """字符估算 token 数 —— 现在是精确计数（exact_tokens）的**兜底**。

    2026-10-06 P1-A 重写史：旧版只统计 m["content"]，而 aionrs payload 的大头在
    thinking / tool_use / tools schema 里，整条 13.1 万 token 的请求只被估成 5.1 万。
    现对每条消息 json.dumps 全量序列化 + 顶层 tools 同规则计入，系数按本机 /tokenize
    校准（CJK 1.7、非 CJK 2.5，英文仍保守 ~2 倍）。已知短板：符号密集内容会低估
    ~1.6 倍（纯符号实测 1.53 chars/token）——这正是主路径改用精确计数的原因。
    """
    def _count(text: str) -> float:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk / 1.7 + (len(text) - cjk) / 2.5
    total = 0.0
    for m in messages:
        total += _count(json.dumps(m, ensure_ascii=False))
    if tools:
        total += _count(json.dumps(tools, ensure_ascii=False))
    return int(total) + 16 * len(messages) + 256


async def trim_messages(messages, max_ctx, tools=None, count_fn=None):
    """保留 system + **能放下的最大后缀**，只在 role=='user' 处切。

    返回 (cand, n_tokens)：
      * cand=None  ⇒ 连 "system + 最后一条 user" 都超预算，调用方应走 413，不转发；
      * 否则 cand = sys_msgs + messages[s:]，s 是满足预算且**最小**（保留历史最多）
        的 user 切点，n_tokens 为该后缀的计数值。

    2026-10-06 P1-A v2 修正：旧版 `for s in reversed(starts)` 第一个满足预算的候选
    就是**最短**后缀（只留最后一条 user，它必然满足预算）⇒ 一旦真触发修剪，整段
    对话被砍成 1 条。以前估算器长期低估、修剪几乎不触发所以没人发现；计数改成
    精确值后这个 bug 立刻变成用户可见事故。现在对 user 下标列表**二分**找最小
    满足切点（235 个 user 候选 ≤ 8 次计数 + 边界确认）；若缓存值显示单调性不成立
    （f(k) 应随 k 增大单调不减地变小），退化为在二分结果 ±2 线性确认并记日志。

    count_fn: async fn(cand_msgs) -> int，由调用方注入**绑到目标后端**的精确计数；
    缺省用字符估算（离线测试用）。单调性前提：messages[s:] 的 token 数随 s 减小
    单调不减 —— 只要用户消息非空即成立。
    """
    budget = int(max_ctx * SAFETY)
    if count_fn is None:
        async def count_fn(m):
            return estimate_tokens_fallback(m, tools)
    sys_msgs = [m for m in messages[:2]
                if isinstance(m, dict) and m.get("role") == "system"]
    starts = [i for i, m in enumerate(messages)
              if isinstance(m, dict) and m.get("role") == "user"]
    if not starts:                       # 退化解：连 user 都没有，原样给最后一条（模板会自己报错）
        cand = messages[-1:]
        return cand, await count_fn(cand)
    n = len(starts)
    cache = {}

    async def f(k):
        v = cache.get(k)
        if v is None:
            s = starts[k]
            v = await count_fn(sys_msgs + messages[s:])
            cache[k] = v
        return v

    n_last = await f(n - 1)
    if n_last > budget:
        return None, n_last              # 最后一条 user 都放不下 → 413
    lo, hi = 0, n - 1                    # f 随 k 单调不增；找最小的 k 使 f(k) <= budget
    while lo < hi:
        mid = (lo + hi) // 2
        if await f(mid) <= budget:
            hi = mid
        else:
            lo = mid + 1
    k = lo
    ks = sorted(cache)
    if any(cache[a] < cache[b] for a, b in zip(ks, ks[1:])):
        log.warning("trim: 切点 token 数不单调（cache=%s），在二分结果 k=%d 附近 ±2 线性确认",
                    {a: cache[a] for a in ks}, k)
        ok = [kk for kk in range(max(0, k - 2), min(n, k + 3)) if await f(kk) <= budget]
        if ok:
            k = min(ok)
    cand = sys_msgs + messages[starts[k]:]
    return cand, cache[k]


# ===================== 客户端归属（/admin/clients） =====================
# llama.cpp 日志只有 task id 没有客户端信息，归属只能由路由器提供：
# peername (ip,port) → /proc/net/tcp{,6} 找 ESTABLISHED 连接的 socket inode
# → 扫 /proc/<pid>/fd 找 socket:[inode] → /proc/<pid>/comm + cmdline 得可读名字。
# 同一 (ip,port) 的映射缓存 10s；查不到就退化显示 ip:port，绝不因此报错或丢请求。

CLIENTS_MAX = 64
PEER_TTL = 10.0
_clients: "OrderedDict" = OrderedDict()   # (ip, port) -> {name,pid,requests,in_flight,last_seen,last_tokens}
_peer_cache: dict = {}                    # (ip, port) -> (name, pid, monotonic_ts)
_GENERIC_COMM = {"python3", "python", "python3.10", "bash", "sh", "node", "nodejs"}


def _find_inode(ip, port):
    """在 /proc/net/tcp{,6} 里找 remote=(ip, port) 的 ESTABLISHED 连接的 inode。

    /proc 地址格式（内核 inet_seq_show）：
      tcp4：地址 4 字节按网络序原样打印，127.0.0.1 → "0100007F"（p[1]=local, p[2]=rem）
      tcp6：4 个 32 位 word 各经 ntohl 后 %08X，故 IPv4-mapped ::ffff:127.0.0.1 的
            末 8 位是"字节反转"的 "7F000001"。
    对端 (ip,port) 只可能是某行 socket 的【local】端（p[1]）—— 该行的 inode 才
    属于拥有 (ip,port) 端点的进程（即客户端）；若 (ip,port) 出现在 rem 端（p[2]），
    那行的 inode 属于服务端，绝不能拿来查 pid。
    """
    try:
        v4_net = struct.pack("4B", *[int(x) for x in ip.split(".")]).hex()
        v4_le = v4_net[6:8] + v4_net[4:6] + v4_net[2:4] + v4_net[0:2]   # 字节反转（tcp6 ntohl 写法）
        targets = {v4_net.upper(), v4_le.upper()}
        ph = f":{port:04X}"
        for f in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = open(f, encoding="ascii").read().splitlines()
            except OSError:
                continue
            for ln in lines[1:]:
                p = ln.split()
                if len(p) < 10 or p[3] != "01":
                    continue
                addr = p[1]
                if addr.endswith(ph) and addr[: -len(ph)].upper() in targets:
                    return int(p[9])
    except Exception:
        pass
    return None


def _pid_for_inode(inode):
    target = f"socket:[{inode}]"
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fddir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fddir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(f"{fddir}/{fd}") == target:
                        return int(pid)
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _name_for_pid(pid):
    """可读名字：comm；comm 是通用解释器时用 cmdline 补上下文。"""
    try:
        comm = open(f"/proc/{pid}/comm", encoding="ascii").read().strip()
    except OSError:
        return None
    if comm in _GENERIC_COMM:
        try:
            parts = [c for c in open(f"/proc/{pid}/cmdline", encoding="utf-8",
                                     errors="replace").read().split("\0") if c]
            if parts:
                # stdin 启动（末参是 "-"）时补上脚本路径，否则名字只剩 "python3 -"
                if parts[-1] == "-":
                    try:
                        fd0 = os.readlink(f"/proc/{pid}/fd/0")
                        if fd0.startswith("/"):
                            parts = parts[:-1] + [fd0]
                    except OSError:
                        pass
                nm = " ".join(parts)
                return nm if len(nm) <= 60 else nm[:57] + "..."
        except OSError:
            pass
    return comm


def _resolve_peer(ip, port):
    now = time.monotonic()
    hit = _peer_cache.get((ip, port))
    if hit and now - hit[2] < PEER_TTL:
        return hit[0], hit[1]
    name, pid = f"{ip}:{port}", None
    try:
        inode = _find_inode(ip, port)
        if inode is not None:
            pid = _pid_for_inode(inode)
        if pid is not None:
            nm = _name_for_pid(pid)
            if nm:
                name = nm
    except Exception:
        pass
    _peer_cache[(ip, port)] = (name, pid, now)
    if len(_peer_cache) > 256:
        _peer_cache.clear()
    return name, pid


def track_client_start(peer):
    """请求开始：登记/累加 (requests+1, in_flight+1)，解析可读名字。返回 (key, entry)。"""
    ip, port = peer[0], peer[1]
    key = (ip, port)
    entry = _clients.get(key)
    if entry is None:
        entry = {"name": f"{ip}:{port}", "pid": None, "requests": 0,
                 "in_flight": 0, "last_seen": time.time(), "last_tokens": 0}
        _clients[key] = entry
        if len(_clients) > CLIENTS_MAX:
            _clients.popitem(last=False)      # LRU：淘汰最久未活跃的
    entry["requests"] += 1
    entry["in_flight"] += 1
    entry["last_seen"] = time.time()
    _clients.move_to_end(key)
    try:
        name, pid = _resolve_peer(ip, port)
        entry["name"] = name
        entry["pid"] = pid
    except Exception:
        pass
    return key, entry


def track_client_end(key, entry):
    """请求结束（正常或异常路径都要调）：in_flight-1。"""
    if entry is None:
        return
    try:
        entry["in_flight"] = max(0, entry.get("in_flight", 1) - 1)
        entry["last_seen"] = time.time()
        if key is not None and key in _clients:
            _clients.move_to_end(key)
    except Exception:
        pass


def admin_clients():
    return {"inflight_total": sum(c.get("in_flight", 0) for c in _clients.values()),
            "clients": [
                {"name": c["name"], "pid": c["pid"], "in_flight": c["in_flight"],
                 "requests": c["requests"], "last_seen": c["last_seen"],
                 "last_tokens": c["last_tokens"]}
                for c in _clients.values()
            ]}


# ---------------------------------------------------------------- 管理 API（/admin/*）
def _tcp_open(host, port, timeout=0.6):
    """快速探测端口是否在监听（用于判断 ComfyUI 是否在跑）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _json_response(writer, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode()
    writer.write(
        f"HTTP/1.1 {status}\r\n".encode()
        + b"Content-Type: application/json; charset=utf-8\r\n"
        + b"Access-Control-Allow-Origin: *\r\n"
        + b"Access-Control-Allow-Headers: Content-Type\r\n"
        + b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        + b"Cache-Control: no-store\r\n"
        + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        + b"Connection: close\r\n\r\n" + body)


def comfy_jobs():
    """ComfyUI 队列里正在跑/排队的任务数（用于提示"恢复大模型会不会打断视频"）。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=0.6) as r:
            q = json.loads(r.read())
        return len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    except Exception:
        return None


def _video_token_fresh(max_age=1800):
    """视频模式令牌存在且 30 分钟内更新过（防崩溃残留把面板钉死在'出视频'）。"""
    p = H3 + "/.allow-video-mode"
    try:
        return os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age
    except OSError:
        return False


def _h3_queue_pending():
    """视频队列中排队/进行中的任务数（queue/*.json；完成的移入 queue/done/）。"""
    try:
        return len([f for f in os.listdir(H3 + "/queue") if f.endswith(".json")])
    except OSError:
        return 0


def admin_state():
    best = pick_backend()
    comfy = _tcp_open("127.0.0.1", 8188)
    # 【2026-11-06 修复】视频模式的"对话大脑"就是 m2（11442 在岗），旧版纯按端口判断 →
    # 视频模式启动窗口（m2 已起、ComfyUI 未起，约 4 分钟）面板一直显示"多模型"，
    # 把用户误导去点切换按钮 → 双触发切换风暴 → ComfyUI 被连坐杀死（21:39 事故）。
    # 现在结合模式令牌：video = ComfyUI 在岗 或 视频令牌新鲜（含启动准备窗口）；
    # multi  = m2 在岗 且 无视频令牌 且 多模型令牌存在。
    video_tok = _video_token_fresh()
    multi_tok = os.path.exists(H3 + "/.allow-multi-mode")
    m2 = _tcp_open("127.0.0.1", 11442)
    return {
        "forced": _forced or "auto",
        "active": best[0] if best else None,
        "health": {name: bool(_health.get(name)) for name, _, _, _ in BACKENDS},
        "backends": [{"name": n, "port": p, "ctx": c} for n, _, p, c in BACKENDS],
        "video_mode": comfy or video_tok,
        "video_preparing": bool(video_tok and not comfy),
        "comfy": comfy,
        "comfy_jobs": comfy_jobs(),                      # 正在跑/排队的视频任务数
        "multi_mode": bool(m2 and not video_tok and multi_tok),
        # 2026-09-30: VLM 模式 —— 它钉 PG500 与大模型互斥，开启期间没有别的对话后端，
        # 所以这个标志对面板/agent 判断"现在是谁在服务"很关键。
        "vlm_mode": _tcp_open("127.0.0.1", 11445),
    }


def spawn_script(script, *args):
    """后台执行 H3 的切换脚本，并把输出落盘（不再丢进 /dev/null）。

    2026-09-30 起因：面板上点「回平时」看起来"毫无反应"。排查时发现脚本输出
    全被 stdout=DEVNULL 吞掉了，systemd 那边也没有启动记录 —— 只能手动重跑
    脚本才看到它卡在 h3_video_mode.sh 里两处共 130 秒的白等（见该脚本注释）。
    现在每次触发都追加记录到 /home/ruanlinbo/h3-logs/<script>-manual.log。
    """
    import subprocess
    logdir = "/home/ruanlinbo/h3-logs"
    try:
        os.makedirs(logdir, exist_ok=True)
        outf = open(f"{logdir}/{script.replace('.sh', '')}-manual.log", "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        outf.write(f"\n=== {stamp} 面板触发: {script} {' '.join(args)} ===\n".encode())
    except Exception:
        outf = subprocess.DEVNULL
    subprocess.Popen(["bash", f"{H3}/{script}", *args],
                     stdout=outf, stderr=subprocess.STDOUT,
                     start_new_session=True)


async def handle_admin(client_reader, client_writer, method, path, headers, leftover):
    """处理 /admin/*；带 CORS，供 HUD 直接调用（在 pick_backend() 守卫之前处理）。"""
    global _forced
    if method == "OPTIONS":
        client_writer.write(
            b"HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\n"
            b"Access-Control-Allow-Headers: Content-Type\r\n"
            b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    body = b""
    clen = headers.get("content-length")
    if clen and clen.isdigit():
        need = int(clen) - len(leftover)
        body = leftover
        if need > 0:
            try:
                body += await asyncio.wait_for(client_reader.readexactly(need), timeout=10)
            except Exception:
                pass
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    if path.startswith("/admin/state"):
        _json_response(client_writer, admin_state())
    elif path.startswith("/admin/clients"):
        # 客户端归属（是谁在用 11435）：纯只读，直接返回台账。
        _json_response(client_writer, admin_clients())
    elif path.startswith("/admin/route"):
        mode = str(payload.get("mode", "auto")).lower()
        valid = {n for n, _, _, _ in BACKENDS} | {"auto"}
        if mode not in valid:
            _json_response(client_writer, {"ok": False, "error": f"mode 必须是 {sorted(valid)}"}, "400 Bad Request")
        else:
            _forced = None if mode == "auto" else mode
            try:
                open(_FORCE_FILE, "w", encoding="utf-8").write(_forced or "auto")
            except Exception:
                pass
            log.info("手动切换路由 → %s（可用后端: %s）", mode,
                     [n for n, _, _, _ in BACKENDS if _health.get(n)])
            _json_response(client_writer, {"ok": True, **admin_state()})
    elif path.startswith("/admin/video"):
        action = str(payload.get("action", "")).lower()
        if action not in ("on", "off"):
            _json_response(client_writer, {"ok": False, "error": "action 必须是 on/off"}, "400 Bad Request")
        else:
            # 【2026-11-06 双触发事故】队列有任务时禁止手动切换：守护进程正拿着模式锁
            # 跑"切换→生成→恢复"全流程，此时手动 on/off 会与守护交错（21:39 那次
            # 面板两次点击直接杀死了 ComfyUI，两个任务全废）。脚本侧已有 flock 兜底，
            # 这里给出明确原因，避免用户反复点。
            _pend = _h3_queue_pending()
            if _pend:
                _json_response(client_writer, {"ok": False, "error":
                    f"视频队列中有 {_pend} 条任务（排队/生成中），模式由守护进程统一管理，"
                    "请勿手动切换。进度看 h3_status.py / HOLO-MONITOR；确需强制终止请终端执行 "
                    "bash /home/ruanlinbo/harness/h3/h3_video_mode.sh off"}, "409 Conflict")
                return
            spawn_script("h3_video_mode.sh", action)
            log.info("手动切换视频模式 → %s（异步执行中）", action)
            _json_response(client_writer, {"ok": True, "action": action, "note": "已在后台执行，约 10-60 秒生效"})
    elif path.startswith("/admin/ctl"):
        action = str(payload.get("action", "")).lower()
        # 2026-09-26 重构：小脑(11440)/旧 27B·14B 大脑(11438/11439) 已退役 →
        # 去掉 brain-*/mini 动作（brain-on 会先停大模型再去起退役单元 → 直接造成 503），
        # 改为真实槽位 m1(11441) / m2(11442) / 视频大脑(11444) 的 restart|stop。
        valid = ("restart-strata", "stop-strata",      # ★ 2026-10-19：strata-local 现在是默认常驻后端
                 "restart-big", "stop-big",
                 "restart-m1", "stop-m1", "restart-m2", "stop-m2",
                 "restart-vornith", "stop-vornith")
        if action not in valid:
            _json_response(client_writer, {"ok": False, "error": f"action 必须是 {list(valid)}"}, "400 Bad Request")
        else:
            spawn_script("h3_ctl.sh", action)
            log.info("手动服务控制 → %s（异步执行中）", action)
            _json_response(client_writer, {"ok": True, "action": action, "note": "已在后台执行"})
    elif path.startswith("/admin/multi"):
        action = str(payload.get("action", "")).lower()
        if action not in ("on", "off"):
            _json_response(client_writer, {"ok": False, "error": "action 必须是 on/off"}, "400 Bad Request")
        else:
            # 【2026-11-06】视频队列有任务时同样禁止：m2 在视频模式里是对话大脑，
            # 多模型 on/off 会把它当多模型 m2 停掉/重启，直接打断正在生成的视频。
            _pend = _h3_queue_pending()
            if _pend:
                _json_response(client_writer, {"ok": False, "error":
                    f"视频队列中有 {_pend} 条任务（排队/生成中），m2 当前是视频大脑，"
                    "请勿切换多模型模式，等视频任务完成即可。"}, "409 Conflict")
                return
            spawn_script("h3_multi_mode.sh", action)
            log.info("多模型模式切换 → %s（异步执行中）", action)
            _json_response(client_writer, {"ok": True, "action": action,
                                           "note": "已在后台执行：切换三张卡各跑一个模型，约 2-4 分钟"})
    elif path.startswith("/admin/vbrain"):
        # 视频期对话大脑热切换（Ornith-1.0-35B-A3B ⇄ 越狱35B-A3B-MoE-Q5）
        # 两者都占满 PG500、unit 用 Conflicts 互斥，所以是"先停后起"，期间 20~60 秒没有对话后端。
        # 非视频模式会被 h3_video_mode.sh 拒绝（它会打印原因并以非 0 退出）。
        action = str(payload.get("action", "")).lower()
        brain = str(payload.get("brain", "")).lower()
        if action != "switch" or brain not in ("moe", "ornith"):
            _json_response(client_writer,
                           {"ok": False, "error": "需要 {action:'switch', brain:'moe'|'ornith'}"},
                           "400 Bad Request")
        else:
            spawn_script("h3_video_mode.sh", "switch-brain", brain)
            log.info("手动切换视频大脑 → %s（异步执行中）", brain)
            _json_response(client_writer, {"ok": True, "brain": brain,
                                           "note": "已在后台执行：先停当前大脑再起新大脑，"
                                                   "期间 20-60 秒没有对话后端"})
    else:
        _json_response(client_writer, {"ok": False, "error": "unknown admin endpoint"}, "404 Not Found")
    try:
        await client_writer.drain()
    except Exception:
        pass
    client_writer.close()


async def pump(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve_backend(client_reader, client_writer, head, leftover, backend, client_entry=None):
    name, host, port, max_ctx = backend
    max_ctx = _ctx_live.get(name) or max_ctx   # 修复3：探测值优先，否则退回静态表
    _, path, headers = parse_head(head)

    clen = headers.get("content-length")
    maybe_trim = (max_ctx is not None and path is not None and "chat/completions" in path
                  and clen is not None and clen.isdigit() and int(clen) <= MAX_BODY)

    out_head, out_body = head, leftover
    if maybe_trim and leftover is not None:
        need = int(clen) - len(leftover)
        body = leftover
        if need > 0:
            try:
                body += await asyncio.wait_for(client_reader.readexactly(need), timeout=120)
            except Exception as e:  # noqa: BLE001
                log.warning("读取请求体失败: %s", e)
        if body:
            try:
                doc = json.loads(body)
                msgs = doc.get("messages")
                if isinstance(msgs, list):
                    tools = doc.get("tools")
                    tkwargs = doc.get("chat_template_kwargs")
                    # ★ P1-A v2：先向目标后端要精确计数（独立短连接，不占生成槽），再决定修剪/放行。
                    n_before, _exact_ok = await exact_tokens(msgs, tools, host, port, tkwargs)
                    if client_entry is not None:
                        client_entry["last_tokens"] = n_before
                    if n_before > int(max_ctx * TRIM_TRIGGER):
                        async def _cnt(cand_msgs, _t=tools, _tk=tkwargs):
                            n, _ = await exact_tokens(cand_msgs, _t, host, port, _tk)
                            return n
                        trimmed, n_after = await trim_messages(msgs, max_ctx, tools, count_fn=_cnt)
                        if client_entry is not None:
                            client_entry["last_tokens"] = n_after
                        if trimmed is None or n_after > max_ctx:
                            # ★ 硬拒绝：连"system + 最后一条 user"都放不下（或修剪后仍超上限）→
                            # 413，绝不转发（转发=被 llama-server 静默截断，客户端拿到畸形流、
                            # 会话报废。明确的 413 错误 > 静默丢数据，2026-10-06 事故教训）。
                            log.error("硬拒绝(后端=%s): %d tokens 仍超上限 %d（原始消息 %d 条）→ 413",
                                      name, n_after, max_ctx, len(msgs))
                            _json_response(client_writer, {
                                "error": {
                                    "message": f"prompt {n_after} tokens 超过后端 {name} 的上下文上限 {max_ctx}，"
                                               f"即使只保留最后一条 user 也放不下，已拒绝转发（请压缩会话后重试）",
                                    "code": 413,
                                    "estimated_tokens": n_after,
                                    "max_ctx": max_ctx,
                                    "messages": len(msgs),
                                }}, "413 Payload Too Large")
                            try:
                                await client_writer.drain()
                            except Exception:
                                pass
                            client_writer.close()
                            return True
                        doc["messages"] = trimmed
                        body = json.dumps(doc, ensure_ascii=False).encode()
                        log.info("按需修剪(后端=%s): %d → %d tokens（上限 %d，预算 %d；消息 %d → %d 条）",
                                 name, n_before, n_after, max_ctx, int(max_ctx * SAFETY),
                                 len(msgs), len(doc["messages"]))
            except Exception as e:  # noqa: BLE001
                log.warning("请求体解析/精确计数失败，按原样透传: %s", e)
            hdrs = [ln for ln in head.split(b"\r\n")
                    if ln and not ln.lower().startswith(b"content-length:")]
            out_head = b"\r\n".join(hdrs) + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            out_body = body

    try:
        srv_reader, srv_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("连接后端 %s(%s:%d) 失败: %s", name, host, port, e)
        _health[name] = False
        return False

    srv_writer.write(out_head + out_body)
    await srv_writer.drain()
    await asyncio.gather(
        pump(client_reader, srv_writer),
        pump(srv_reader, client_writer),
        return_exceptions=True,
    )
    return True


async def read_body_small(reader, leftover, headers):
    """预读完整请求体（仅小体量请求）；返回 bytes 或 None。"""
    clen = headers.get("content-length")
    if not (clen and clen.isdigit() and int(clen) <= MAX_BODY):
        return None
    need = int(clen) - len(leftover)
    body = leftover
    if need > 0:
        try:
            body += await asyncio.wait_for(reader.readexactly(need), timeout=120)
        except Exception:  # noqa: BLE001
            return None
    return body


def wanted_backend(body):
    """从请求体里取 model 字段并映射到后端；未知/缺省 → None（走自动优先级）。"""
    if not body:
        return None, None
    try:
        doc = json.loads(body)
        want = doc.get("model")
    except Exception:  # noqa: BLE001
        return None, None
    if not isinstance(want, str):
        return None, None
    w = want.strip()
    if w in MODEL_ALIAS:
        return MODEL_ALIAS[w], w      # 命中目录（后端名或 None=自动）
    return None, w                     # 未知模型名


def models_payload():
    """同时给出 OpenAI 风格(data) 与 llama.cpp/ollama 风格(models)，各 agent 都能识别。"""
    data, models = [], []
    for name, backend, desc in MODEL_CATALOG:
        ok = True if backend is None else bool(_health.get(backend))
        if backend is not None and not ok:
            continue                      # 后端不在线就不列（避免选了必然失败）
        data.append({"id": name, "object": "model", "created": 0, "owned_by": "local",
                     "description": desc})
        models.append({"name": name, "model": name, "modified_at": "", "size": "", "digest": "",
                       "type": "model", "description": desc, "tags": ["local"],
                       "capabilities": ["completion"],
                       "parameters": "", "details": {"parent_model": "", "format": "gguf",
                       "family": "", "families": [], "parameter_size": "", "quantization_level": ""}})
    return {"object": "list", "data": data, "models": models}


async def handle_model_list(client_writer):
    body = json.dumps(models_payload(), ensure_ascii=False).encode()
    client_writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Access-Control-Allow-Origin: *\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    try:
        await client_writer.drain()
    except Exception:  # noqa: BLE001
        pass
    client_writer.close()


async def handle(client_reader, client_writer):
    head, leftover = await read_head(client_reader)
    if not head:
        client_writer.close()
        return

    # 管理 API（HOLO-MONITOR 的手动开关走这里；在 pick_backend() 守卫之前返回）
    _m, _p, _h = parse_head(head)
    if _p and _p.startswith("/admin/"):
        await handle_admin(client_reader, client_writer, _m, _p, _h, leftover)
        return

    # 模型清单：聚合对外可选模型（agent 下拉框靠这个发现小脑）
    if _m == "GET" and _p and (_p.rstrip("/").endswith("/v1/models") or _p.rstrip("/").endswith("/api/tags")):
        await handle_model_list(client_writer)
        return

    # ★ 2026-09-30 修复：后端可用性检查必须放在【管理接口与模型清单之后】。
    #
    # 原来这一段在最前面（读请求头之前）就 pick_backend() 并在无后端时直接 503 返回，
    # 于是 /admin/* 与 /v1/models 也被一并 503 —— 面板读不到 /admin/state，显示
    # 「控制接口不可达」，切换按钮全部失效。**而这恰恰是"没有后端"时最需要它们的时候**：
    # 用户正是要靠这些按钮把后端救回来（点「回平时」/「出视频」等）。
    #
    # 只有真正要转发到后端的推理请求才需要后端存在。
    backend = pick_backend()
    if backend is None:
        body = b'{"error":{"message":"no backend available","code":503}}'
        client_writer.write(
            b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
            b"Retry-After: 5\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n" + body)
        try:
            await client_writer.drain()
        except Exception:
            pass
        client_writer.close()
        return

    # 客户端归属（/admin/clients）：连接建立时取 peername，开始计数。
    peer = None
    try:
        peer = client_writer.get_extra_info("peername")
    except Exception:
        pass
    ckey, client_entry = track_client_start(peer) if peer and len(peer) >= 2 else (None, None)

    # 按请求体里的 model 字段路由（显式选择优先；未知/缺省→自动）
    body_full = None
    want, want_raw = None, None
    if _m == "POST" and _p and ("chat/completions" in _p or "completions" in _p or _p.rstrip("/").endswith("/completion")):
        body_full = await read_body_small(client_reader, leftover, _h)
        if body_full is not None:
            leftover = body_full
            want, want_raw = wanted_backend(body_full)

    # 连接阶段依次重试其他后端（此时尚未向客户端转发任何字节）：
    # 于是"首选刚停、健康缓存还没刷新"的 1-3 秒毛刺也被兜住。
    # candidates() 已把「手动强制」的后端排在最前，失败时仍会回落到其他健康后端。
    cand_list = candidates()
    if want_raw is not None:
        if want_raw in MODEL_ALIAS:
            if want:
                cand_list = [c for c in cand_list if c[0] == want] + [c for c in cand_list if c[0] != want]
                log.info("按 model 字段路由: %r → 优先 %s", want_raw, want)
            else:
                log.info("model=%r → 自动优先级（大模型优先，其余按健康度）", want_raw)
        else:
            log.info("model=%r 未在目录中 → 走自动优先级", want_raw)
    # ★ 2026-10-15 长上下文导流：自动路由 + 大请求体 → 精确计数，超阈值改走 flash（141 262K）。
    #   计数用当前首选后端的分词器（跨模型近似足够做路由决策）；失败就照常转发，绝不因导流出错。
    if LONG_CTX_STEER and want is None and body_full and len(body_full) > STEER_MIN_BODY and _health.get("flash"):
        try:
            doc = json.loads(body_full)
            msgs = doc.get("messages")
            if isinstance(msgs, list):
                base = next((c for c in cand_list if c[0] != "flash"), None)
                if base is not None:
                    n_est, ok = await exact_tokens(msgs, doc.get("tools"), base[1], base[2],
                                                   doc.get("chat_template_kwargs"))
                    if ok and n_est > BIG_LONG_TOKENS:
                        flash_b = next(c for c in BACKENDS if c[0] == "flash")
                        cand_list = [flash_b] + [c for c in cand_list if c[0] != "flash"]
                        log.info("长上下文导流: %d tokens > %d → 优先 flash(141 262K)（原选 %s）",
                                 n_est, BIG_LONG_TOKENS, base[0])
        except Exception as e:  # noqa: BLE001
            log.warning("长上下文导流计数失败（照常转发）: %s", e)
    try:
        for cand in cand_list:
            if not _health.get(cand[0]):
                continue
            try:
                if await serve_backend(client_reader, client_writer, head, leftover, cand, client_entry):
                    track_client_end(ckey, client_entry)   # 正常结束：in_flight-1
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("后端 %s 处理异常: %s", cand[0], e)
                _health[cand[0]] = False
    finally:
        # 异常路径也要把 in_flight 减回去
        track_client_end(ckey, client_entry)
        try:
            client_writer.close()
        except Exception:
            pass


async def main():
    log.info("11435 路由启动 | 优先级: %s", [(n, f"{h}:{p}", c) for n, h, p, c in BACKENDS])
    asyncio.create_task(refresh_loop())
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
