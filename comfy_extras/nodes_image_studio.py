"""Image Studio generation node for VOS deployments.

Image Studio owns the job lifecycle: it queues the request through
V-Scheduler, runs the local backend, and stores the rendered files. This node
submits a job, polls its state, and downloads the rendered images.
"""

import asyncio
import json
import logging
import os
import random
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
FALLBACK_MODEL_KEYS = ["sd-turbo"]
SAMPLERS = ["euler_a", "euler", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v",
            "lcm", "ddim_trailing", "tcd", "res_multistep", "res_2s", "er_sde", "euler_cfg_pp",
            "euler_a_cfg_pp", "euler_ge", "dpm++2m_sde", "dpm++2m_sde_bt", "lms"]
SCHEDULERS = ["", "discrete", "karras", "exponential", "ays", "gits", "sgm_uniform", "simple", "smoothstep",
              "kl_optimal", "lcm", "bong_tangent", "ltx2", "logit_normal", "flux2", "flux", "beta", "normal"]
PRIORITIES = ["background", "interactive", "high"]
# Left at these values, the widget means "use the model family default"
WIDGET_DEFAULTS = {"width": 512, "height": 512, "steps": 4, "cfg_scale": 1.0, "sampler": "euler_a"}


@lru_cache(maxsize=1)
def model_catalog():
    """Ready Image Studio models keyed by model_key; empty when unreachable."""
    try:
        request = urllib.request.Request(API_URL + "/api/v1/models", headers=api_headers())
        with urllib.request.urlopen(request, timeout=2) as response:
            models = json.loads(response.read())
    except (OSError, ValueError):
        return {}
    return {model["key"]: model for model in models
            if isinstance(model, dict) and model.get("key") and model.get("status") == "ready"}


def model_keys():
    keys = [key for key, model in model_catalog().items()
            if {"txt2img", "img2img"} & set(model.get("capabilities") or [])]
    return keys or FALLBACK_MODEL_KEYS


def reference_supported(model):
    runtime = model.get("runtime") or {}
    capabilities = set(model.get("capabilities") or [])
    return bool({"img2img", "edit"} & capabilities) or bool((runtime.get("parameters") or {}).get("reference_image"))


def apply_model_defaults(payload, model):
    """Drop widget defaults the model family overrides, so Image Studio picks its own."""
    defaults = (model.get("runtime") or {}).get("defaults") or {}
    for field, value in WIDGET_DEFAULTS.items():
        if field in defaults and payload.get(field) == value:
            del payload[field]


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


async def api_request(session, method, path, payload=None, data=None):
    async with session.request(method, API_URL + path, json=payload, data=data, headers=api_headers()) as response:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Image Studio {response.status}: {error_detail(text)}")
        return json.loads(text) if text else {}


async def upload_reference(session, image):
    pixels = image[:1].cpu().clamp(0.0, 1.0).mul(255).round().to(torch.uint8).numpy()[0, :, :, :3]
    buffer = BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels), mode="RGB").save(buffer, format="PNG")
    form = aiohttp.FormData()
    form.add_field("file", buffer.getvalue(), filename="reference.png", content_type="image/png")
    uploaded = await api_request(session, "POST", "/api/v1/inputs", data=form)
    input_id = uploaded.get("input_id")
    if not input_id:
        raise RuntimeError("Image Studio did not return an input_id for the reference image")
    return input_id


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
            description="Generate images through the Image Studio app. Requires Image Studio; jobs queue through V-Scheduler. "
                        "Unchanged size, steps, CFG and sampler follow the model family defaults.",
            inputs=[
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Image.Input("init_image", optional=True,
                               tooltip="Reference image for img2img, resized to width and height. "
                                       "Needs a model with img2img or edit support."),
                io.Float.Input("strength", default=0.75, min=0.0, max=1.0, step=0.01, advanced=True,
                               tooltip="How much the reference image changes. 0 keeps it, 1 ignores it."),
                io.Float.Input("img_cfg_scale", default=0.0, min=0.0, max=30.0, step=0.1, advanced=True,
                               tooltip="CFG against the reference image. 0 uses the model default."),
                io.Combo.Input("model_key", options=keys, default=keys[0]),
                io.Int.Input("width", default=512, min=256, max=2048, step=8),
                io.Int.Input("height", default=512, min=256, max=2048, step=8),
                io.Int.Input("steps", default=4, min=1, max=150, tooltip="The model family default replaces an unchanged 4."),
                io.Float.Input("cfg_scale", default=1.0, min=1.0, max=30.0, step=0.1,
                               tooltip="The model family default replaces an unchanged 1.0."),
                io.Int.Input("seed", default=-1, min=-1, max=0xFFFFFFFFFFFFFFFF, control_after_generate=True,
                             tooltip="-1 picks a random seed for this run and reports it on the seed output."),
                io.Int.Input("batch_count", default=1, min=1, max=4),
                io.String.Input("negative_prompt", default="", multiline=True, advanced=True),
                io.String.Input("model_variant", default="", advanced=True),
                io.Combo.Input("sampler", options=SAMPLERS, default="euler_a", advanced=True,
                               tooltip="The model family default replaces an unchanged euler_a."),
                io.Combo.Input("scheduler", options=SCHEDULERS, default="", advanced=True,
                               tooltip="Empty means the model default."),
                io.Int.Input("clip_skip", default=-1, min=-1, max=24, advanced=True),
                io.Boolean.Input("hires", default=False, advanced=True),
                io.Float.Input("hires_scale", default=2.0, min=1.0, max=8.0, step=0.1, advanced=True),
                io.Int.Input("hires_steps", default=0, min=0, max=150, advanced=True,
                             tooltip="Second-pass steps. 0 uses the model default."),
                io.Float.Input("hires_denoising_strength", default=0.7, min=0.0, max=1.0, step=0.01, advanced=True),
                io.Combo.Input("priority", options=PRIORITIES, default="background", advanced=True),
                io.String.Input("extra_params", default="{}", multiline=True, advanced=True,
                               tooltip="JSON object merged into the Image Studio request."),
            ],
            outputs=[io.Image.Output(), io.Int.Output(display_name="seed")],
        )

    @classmethod
    async def execute(cls, prompt, model_key, width, height, steps, cfg_scale, seed, batch_count,
                      negative_prompt, model_variant, sampler, scheduler, clip_skip, hires, hires_scale,
                      hires_steps, hires_denoising_strength, priority, extra_params,
                      init_image=None, strength=0.75, img_cfg_scale=0.0):
        model = model_catalog().get(model_key) or {}
        if init_image is not None and model and not reference_supported(model):
            raise ValueError(f"Image Studio model {model_key} does not accept a reference image")
        if seed < 0:
            seed = random.randint(0, 0xFFFFFFFF)
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
            "hires_steps": hires_steps,
            "hires_denoising_strength": hires_denoising_strength,
            "scheduling": {"priority": priority},
        }
        apply_model_defaults(payload, model)
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
            if init_image is not None:
                payload["init_input_id"] = await upload_reference(session, init_image)
                payload["strength"] = strength
                payload["img_cfg_scale"] = img_cfg_scale
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
        return io.NodeOutput(torch.cat(images, dim=0), seed)

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
