"""H3 LAN image — Z-Image text-to-image on the 101 GPU box via h3-bridge.

Uses the Z-Image-Turbo queue template (daemon `"zimage": true` route). Zero cost,
zero API; the 101 box auto-switches into video mode for the render.
"""

from __future__ import annotations

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


class H3LanImage(BaseTool):
    name = "h3_lan_image"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "h3_lan"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["env:H3_BRIDGE_URL"]
    install_instructions = "Needs the H3 bridge on the 101 box (see h3_lan_video)."

    capabilities = ["text_to_image", "local_gpu"]
    supports = {"t2i": True, "cost_per_image_usd": 0.0}
    best_for = ["zero-cost local stills (Z-Image) from the 101 box"]
    not_good_for = ["sub-minute turnarounds (queue + mode switch overhead)"]
    fallback_tools = ["local_comfyui", "local_diffusion_tools"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "width": {"type": "integer", "default": 1024},
            "height": {"type": "integer", "default": 1536},
            "seed": {"type": "integer"},
            "output_path": {"type": "string"},
            "job_timeout_seconds": {"type": "integer", "default": 900},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
    }

    resource_profile = ResourceProfile(vram_mb=0, ram_mb=0)
    idempotency_key_fields = ["prompt", "width", "height", "seed"]
    side_effects = ["enqueues an image job on the 101 H3 GPU box", "writes image to output_path"]
    model = "z-image-turbo"
    agent_skills: list[str] = []

    def _base(self) -> str:
        return os.environ.get("H3_BRIDGE_URL", "http://192.168.2.101:8189").rstrip("/")

    def _headers(self) -> dict:
        tok = os.environ.get("H3_BRIDGE_TOKEN", "")
        return {"X-Auth": tok} if tok else {}

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 420.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import requests

        t0 = time.time()
        base = self._base()
        out_path = Path(
            inputs.get("output_path") or f"projects/assets/h3_image_{int(t0)}.png"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "prompt": inputs["prompt"],
            "width": int(inputs.get("width", 1024)),
            "height": int(inputs.get("height", 1536)),
            "zimage": True,
            "source": "openmontage",
        }
        if inputs.get("seed") is not None:
            payload["seed"] = int(inputs["seed"])
        r = requests.post(f"{base}/jobs", json=payload, headers=self._headers(), timeout=60)
        if not r.ok:
            return ToolResult(success=False, error=f"submit failed: {r.text[:300]}")
        job_id = r.json()["job_id"]

        deadline = time.time() + float(inputs.get("job_timeout_seconds", 900))
        state: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(10)
            try:
                state = requests.get(f"{base}/jobs/{job_id}", headers=self._headers(), timeout=15).json()
            except Exception:
                continue
            if state.get("state") == "done":
                break
            if state.get("state") == "failed":
                return ToolResult(success=False, error=f"h3 image job failed: {state.get('hint', '')}")
        else:
            return ToolResult(success=False, error=f"h3 image job {job_id} timed out ({state.get('state')})")

        with requests.get(
            f"{base}/jobs/{job_id}/file", headers=self._headers(), timeout=120, stream=True
        ) as fr:
            if not fr.ok:
                return ToolResult(success=False, error=f"download failed: HTTP {fr.status_code}")
            with open(out_path, "wb") as f:
                for chunk in fr.iter_content(1 << 16):
                    f.write(chunk)

        return ToolResult(
            success=True,
            data={"image_path": str(out_path), "bridge_job_id": job_id,
                  "provider": "h3_lan", "model": "z-image-turbo", "cost_usd": 0.0},
            artifacts=[str(out_path)],
            cost_usd=0.0,
            duration_seconds=round(time.time() - t0, 1),
            model="z-image-turbo",
        )
