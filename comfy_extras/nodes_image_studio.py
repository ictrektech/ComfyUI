"""Image Studio generation node for VOS deployments.

Image Studio owns the job lifecycle: it queues the request through
V-Scheduler, runs the local backend, and stores the rendered files. This node
submits a job, polls its state, and downloads the rendered images.
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
from functools import lru_cache
from io import BytesIO
from urllib.parse import quote

import aiohttp
import numpy as np
import torch
from PIL import Image
from typing_extensions import override

import comfy.model_management
import comfy.utils
from comfy_api.latest import ComfyExtension, io

def env_number(name, default, cast):
    try:
        return cast(os.getenv(name, default))
    except ValueError:
        logging.warning("Invalid %s; using %s", name, default)
        return cast(default)


API_URL = (os.getenv("IMAGE_STUDIO_API_URL") or "http://image-studio-backend:5000").rstrip("/")
VOS_SUB = os.getenv("IMAGE_STUDIO_VOS_SUB") or os.getenv("COMFYUI_VOS_SUB", "")
TIMEOUT_SECONDS = env_number("IMAGE_STUDIO_TIMEOUT_SECONDS", "900", int)
POLL_SECONDS = env_number("IMAGE_STUDIO_POLL_SECONDS", "2", float)
MODEL_KEYS = [item.strip() for item in os.getenv("IMAGE_STUDIO_MODEL_KEYS", "sd-turbo").split(",") if item.strip()]
SAMPLERS = ["euler_a", "euler", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v",
            "lcm", "ddim_trailing", "tcd", "res_multistep", "res_2s", "er_sde", "euler_cfg_pp",
            "euler_a_cfg_pp", "euler_ge", "dpm++2m_sde", "dpm++2m_sde_bt", "lms"]
SCHEDULERS = ["", "discrete", "karras", "exponential", "ays", "gits", "sgm_uniform", "simple", "smoothstep",
              "kl_optimal", "lcm", "bong_tangent", "ltx2", "logit_normal", "flux2", "flux", "beta", "normal"]
PRIORITIES = ["background", "interactive", "high"]


@lru_cache(maxsize=1)
def model_keys():
    keys = MODEL_KEYS or ["sd-turbo"]
    try:
        request = urllib.request.Request(API_URL + "/api/v1/models", headers=api_headers())
        with urllib.request.urlopen(request, timeout=2) as response:
            models = json.loads(response.read())
        ready = [model["key"] for model in models
                 if isinstance(model, dict) and model.get("key") and model.get("status") == "ready"]
        if ready:
            keys = ready
    except (OSError, ValueError):
        pass
    return keys


def header_value(text):
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return quote(text, safe="")
    return text


def api_headers():
    result = {"Accept": "application/json"}
    if VOS_SUB:
        result["X-Vos-Sub"] = header_value(VOS_SUB)
    return result


def error_detail(text):
    try:
        body = json.loads(text)
    except ValueError:
        return text
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or text)
    return text


async def api_request(session, method, path, payload=None):
    async with session.request(method, API_URL + path, json=payload, headers=api_headers()) as response:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Image Studio {response.status}: {error_detail(text)}")
        return json.loads(text) if text else {}


def image_tensor(data):
    image = Image.open(BytesIO(data)).convert("RGB")
    return torch.from_numpy(np.array(image).astype(np.float32) / 255.0).unsqueeze(0)


class ImageStudioGenerate(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        keys = model_keys()
        return io.Schema(
            node_id="ImageStudioGenerate",
            display_name="Image Studio Generate",
            category="image/Image Studio",
            description="Generate images through the Image Studio app. Requires Image Studio; jobs queue through V-Scheduler.",
            inputs=[
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Combo.Input("model_key", options=keys, default=keys[0]),
                io.Int.Input("width", default=512, min=256, max=2048, step=8),
                io.Int.Input("height", default=512, min=256, max=2048, step=8),
                io.Int.Input("steps", default=4, min=1, max=150),
                io.Float.Input("cfg_scale", default=1.0, min=1.0, max=30.0, step=0.1),
                io.Int.Input("seed", default=-1, min=-1, max=0xFFFFFFFFFFFFFFFF, control_after_generate=True),
                io.Int.Input("batch_count", default=1, min=1, max=4),
                io.String.Input("negative_prompt", default="", multiline=True, advanced=True),
                io.String.Input("model_variant", default="", advanced=True),
                io.Combo.Input("sampler", options=SAMPLERS, default="euler_a", advanced=True),
                io.Combo.Input("scheduler", options=SCHEDULERS, default="", advanced=True,
                               tooltip="Empty means the model default."),
                io.Int.Input("clip_skip", default=-1, min=-1, max=24, advanced=True),
                io.Boolean.Input("hires", default=False, advanced=True),
                io.Float.Input("hires_scale", default=2.0, min=1.0, max=8.0, step=0.1, advanced=True),
                io.Combo.Input("priority", options=PRIORITIES, default="background", advanced=True),
                io.String.Input("extra_params", default="{}", multiline=True, advanced=True,
                               tooltip="JSON object merged into the Image Studio request."),
            ],
            outputs=[io.Image.Output()],
        )

    @classmethod
    async def execute(cls, prompt, model_key, width, height, steps, cfg_scale, seed, batch_count,
                      negative_prompt, model_variant, sampler, scheduler, clip_skip, hires, hires_scale,
                      priority, extra_params):
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "model_key": model_key,
            "model_variant": model_variant,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sampler": sampler,
            "scheduler": scheduler,
            "clip_skip": clip_skip,
            "seed": seed,
            "batch_count": batch_count,
            "hires": hires,
            "hires_scale": hires_scale,
            "scheduling": {"priority": priority},
        }
        if extra_params.strip():
            try:
                extra = json.loads(extra_params)
            except ValueError as error:
                raise ValueError("extra_params must be a JSON object") from error
            if not isinstance(extra, dict):
                raise ValueError("extra_params must be a JSON object")
            payload.update(extra)

        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            job = await api_request(session, "POST", "/api/v1/generate", payload)
            job_id = job.get("job_id")
            if not job_id:
                raise RuntimeError("Image Studio did not return a job_id")
            progress = comfy.utils.ProgressBar(100)
            deadline = time.monotonic() + TIMEOUT_SECONDS
            completed = False
            try:
                while True:
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Image Studio job {job_id} exceeded {TIMEOUT_SECONDS}s")
                    await asyncio.sleep(POLL_SECONDS)
                    job = await api_request(session, "GET", f"/api/v1/jobs/{job_id}")
                    progress.update_absolute(int(float(job.get("progress") or 0) * 100))
                    if job.get("status") == "succeeded":
                        break
                    if job.get("status") == "failed":
                        raise RuntimeError(f"Image Studio job {job_id} failed: {job.get('error') or 'unknown error'}")
                outputs = job.get("outputs") or []
                if not outputs:
                    raise RuntimeError(f"Image Studio job {job_id} returned no outputs")
                images = []
                for filename in outputs:
                    url = f"{API_URL}/api/v1/outputs/{job_id}/{quote(filename, safe='')}"
                    async with session.get(url, headers=api_headers()) as response:
                        if response.status >= 400:
                            raise RuntimeError(f"Image Studio output download failed: {response.status}")
                        images.append(image_tensor(await response.read()))
                completed = True
            finally:
                if not completed:
                    await cls.cancel(session, job_id)
        return io.NodeOutput(torch.cat(images, dim=0))

    @classmethod
    async def cancel(cls, session, job_id):
        try:
            async with session.delete(f"{API_URL}/api/v1/jobs/{job_id}", headers=api_headers()) as response:
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logging.warning("Image Studio job %s cancel request failed", job_id)


class ImageStudioExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [ImageStudioGenerate]


async def comfy_entrypoint() -> ImageStudioExtension:
    return ImageStudioExtension()
