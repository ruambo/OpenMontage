"""H3 LAN video — MiniMax H3 (T2V/I2V) on the home 101 GPU box via h3-bridge.

Zero-API-cost local route: 141 (this OpenMontage install) submits to the bridge on
the 101 machine, which enqueues into the existing H3 queue daemon. The daemon does
the heavy lifting — waits for the chat LLM to idle, hands VRAM over
(``h3_video_mode.sh on``), renders on the Tesla V100, and always restores the box.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class H3LanVideo(BaseTool):
    name = "h3_lan_video"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "h3_lan"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["env:H3_BRIDGE_URL"]
    install_instructions = (
        "Point H3_BRIDGE_URL at the H3 queue bridge on the 101 box "
        "(default http://192.168.2.101:8189). Set H3_BRIDGE_TOKEN if the bridge has auth."
    )

    capabilities = ["text_to_video", "image_to_video", "local_gpu", "audio_in_video"]
    supports = {
        "t2v": True,
        "i2v": True,
        "native_audio": True,
        "cost_per_clip_usd": 0.0,
        "portrait_default": "480x832",
    }
    best_for = [
        "zero-cost home GPU renders (MiniMax H3 with audio) via the 101 box",
        "i2v first-frame animation from stills",
        "any pipeline that can wait: box auto-switches LLM↔video between jobs",
    ]
    not_good_for = [
        "renders needed in under ~5 minutes (queue waits for LLM idle + mode switch)",
        "4K / wide 16:9 (V100 is tight; portrait 480x832 is the sweet spot)",
    ]
    fallback_tools = ["ltx_video_local", "comfyui_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {"type": "string", "enum": ["t2v", "i2v"], "default": "t2v"},
            "image_path": {
                "type": "string",
                "description": "First-frame image for i2v (uploaded to 101 ComfyUI input/)",
            },
            "width": {"type": "integer", "default": 480},
            "height": {"type": "integer", "default": 832},
            "duration_seconds": {"type": "number", "default": 5.2},
            "fps": {"type": "integer", "default": 24},
            "seed": {"type": "integer"},
            "steps": {"type": "integer", "default": 8},
            "spectrum": {"type": "boolean", "default": False},
            "lane": {
                "type": "string",
                "enum": ["fast"],
                "description": "'fast' = PG500 draft lane running the 4B text encoder. IRON RULE (101 shared-memory rule 4): 4B encoder drifts on complex prompts -- DRAFTS/THUMBNAILS ONLY, never for finals. Default lane: V100 DiT + 32B encoder on CPU.",
            },
            "output_path": {"type": "string"},
            "job_timeout_seconds": {"type": "integer", "default": 2400},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "video_path": {"type": "string"},
            "bridge_job_id": {"type": "string"},
            "length_frames": {"type": "integer"},
        },
    }

    resource_profile = ResourceProfile(vram_mb=0, ram_mb=0)
    idempotency_key_fields = ["prompt", "operation", "image_path", "width", "height", "duration_seconds", "seed"]
    side_effects = ["enqueues a job on the 101 H3 GPU box", "writes video to output_path"]
    model = "minimax-h3-lan"
    agent_skills: list[str] = []

    # ---- bridge plumbing ----

    def _base(self) -> str:
        return os.environ.get("H3_BRIDGE_URL", "http://192.168.2.101:8189").rstrip("/")

    def _headers(self) -> dict:
        tok = os.environ.get("H3_BRIDGE_TOKEN", "")
        return {"X-Auth": tok} if tok else {}

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        # H3 on V100: render + queue wait + LLM↔video mode switch window (~4 min worst case)
        secs = float(inputs.get("duration_seconds", 5.2))
        return 240 + max(600, secs * 150)

    def get_status(self):
        from tools.base_tool import ToolStatus

        if not os.environ.get("H3_BRIDGE_URL"):
            return ToolStatus.AVAILABLE  # default LAN box is assumed present
        import requests

        try:
            r = requests.get(f"{self._base()}/health", headers=self._headers(), timeout=5)
            return ToolStatus.AVAILABLE if r.ok else ToolStatus.DEGRADED
        except Exception:
            return ToolStatus.DEGRADED

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import requests

        t0 = time.time()
        base = self._base()
        out_path = Path(
            inputs.get("output_path")
            or f"projects/assets/h3_lan_{int(t0)}.mp4"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {
            "prompt": inputs["prompt"],
            "width": int(inputs.get("width", 480)),
            "height": int(inputs.get("height", 832)),
            "seconds": float(inputs.get("duration_seconds", 5.2)),
            "fps": int(inputs.get("fps", 24)),
            "steps": int(inputs.get("steps", 8)),
            "spectrum": bool(inputs.get("spectrum", False)),
            "source": "openmontage",
        }
        if inputs.get("length"):
            payload["length"] = int(inputs["length"])  # explicit frames override seconds
        if inputs.get("seed") is not None:
            payload["seed"] = int(inputs["seed"])
        if inputs.get("lane"):
            payload["lane"] = str(inputs["lane"])  # "fast" -> 101 PG500 lane

        if str(inputs.get("operation", "t2v")) == "i2v":
            img = inputs.get("image_path")
            if not img or not Path(img).exists():
                return ToolResult(success=False, error=f"i2v needs an existing image_path, got {img!r}")
            b64 = base64.b64encode(Path(img).read_bytes()).decode()
            up = requests.post(
                f"{base}/upload",
                json={"name": Path(img).name, "b64": b64},
                headers=self._headers(),
                timeout=120,
            )
            if not up.ok:
                return ToolResult(success=False, error=f"upload failed: {up.text[:200]}")
            payload["first_frame"] = up.json()["first_frame"]

        r = requests.post(f"{base}/jobs", json=payload, headers=self._headers(), timeout=60)
        if not r.ok:
            return ToolResult(success=False, error=f"submit failed: {r.text[:300]}")
        job_id = r.json()["job_id"]
        length_frames = r.json().get("length")

        deadline = time.time() + float(inputs.get("job_timeout_seconds", 2400))
        state: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(15)
            try:
                st = requests.get(f"{base}/jobs/{job_id}", headers=self._headers(), timeout=15).json()
            except Exception:
                continue
            state = st
            if st.get("state") == "done":
                break
            if st.get("state") == "failed":
                return ToolResult(
                    success=False,
                    error=f"h3 job {job_id} failed on 101: {st.get('hint', 'see h3_queue_daemon.log')}",
                )
        else:
            return ToolResult(success=False, error=f"h3 job {job_id} timed out (state={state.get('state')})")

        with requests.get(
            f"{base}/jobs/{job_id}/file", headers=self._headers(), timeout=300, stream=True
        ) as fr:
            if not fr.ok:
                return ToolResult(success=False, error=f"download failed: HTTP {fr.status_code}")
            with open(out_path, "wb") as f:
                for chunk in fr.iter_content(1 << 16):
                    f.write(chunk)

        return ToolResult(
            success=True,
            data={
                "video_path": str(out_path),
                "bridge_job_id": job_id,
                "length_frames": length_frames,
                "provider": "h3_lan",
                "model": "minimax-h3",
                "cost_usd": 0.0,
            },
            artifacts=[str(out_path)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - t0, 1),
            model="minimax-h3-480p-audio",
        )
