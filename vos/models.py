"""Read-only projection of ready Model Hub weights into native ComfyUI folders."""
import asyncio
import json
import logging
from pathlib import Path

import aiohttp

CATEGORIES = frozenset("""
checkpoints clip_vision controlnet diffusion_models embeddings loras text_encoders
upscale_models style_models vae vae_approx hypernetworks gligen photomaker model_patches
audio_encoders latent_upscale_models background_removal frame_interpolation
geometry_estimation detection optical_flow
""".split())
ALIASES = {"clip": "text_encoders", "unet": "diffusion_models", "t2i_adapter": "controlnet"}


def infer_category(file):
    """Recognize common single-file layouts without loading tensors."""
    try:
        with file.open("rb") as stream:
            size = int.from_bytes(stream.read(8), "little")
            if not 0 < size <= 8 * 1024 * 1024:
                return None
            header = json.loads(stream.read(size))
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    keys = [key for key in header if key != "__metadata__"]
    def prefixes(*values):
        return any(key.startswith(values) for key in keys)
    if prefixes("lora_unet_", "lora_te_", "lora_transformer_") or any(
        ".lora_A." in key or ".lora_down." in key for key in keys
    ):
        return "loras"
    if prefixes("control_model.", "input_hint_block.", "controlnet_cond_embedding."):
        return "controlnet"
    if prefixes("model.diffusion_model."):
        return "checkpoints" if prefixes("first_stage_model.", "cond_stage_model.", "conditioner.") else "diffusion_models"
    if prefixes("double_blocks.", "joint_blocks.", "single_blocks."):
        return "diffusion_models"
    if prefixes("encoder.") and prefixes("decoder.") and prefixes("quant_conv.", "post_quant_conv."):
        return "vae"
    return None


def catalog_links(models, shared, aliases=()):
    export = (shared / "export").resolve()
    if export != shared / "export":
        return {}
    result = {}
    roots = {}
    for model in models:
        if not isinstance(model, dict) or model.get("status") != "ready" or model.get("source") not in {"hf", "ms"}:
            continue
        access = model.get("access_path", "")
        if not isinstance(access, str) or not access.startswith("/data/export/"):
            continue
        relative = Path(access).relative_to("/data/export")
        if ".." in relative.parts or len(relative.parts) < 3 or relative.parts[0] != model["source"]:
            continue
        root = (export / relative).resolve()
        if not root.is_relative_to(export / model["source"]) or not root.is_dir():
            continue
        roots[f"{relative.parts[0]}://{relative.parts[1]}/{relative.parts[2]}".lower()] = root
        for file in root.rglob("*.safetensors"):
            resolved = file.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            parts = file.relative_to(root).parts
            folders = [ALIASES.get(part, part) for part in parts[:-1]]
            category = next((folder for folder in reversed(folders) if folder in CATEGORIES), None)
            # Diffusers shards are not standalone native weights.
            if category is None and len(parts) == 1:
                category = infer_category(resolved)
            if category is None:
                continue
            name = relative.parts[:-1] if relative.name == "current" else relative.parts
            result[Path(category, *name, *parts)] = resolved
    for category, name, repo, file in aliases:
        if root := roots.get(repo.lower()):
            target = (root / file).resolve()
            if target.is_relative_to(root) and target.is_file():
                result.setdefault(Path(category, name), target)
    return result


def sync_links(projection, links):
    for path in projection.rglob("*"):
        if path.suffix in {".safetensors", ".sft"} and path.is_symlink() and path.relative_to(projection) not in links:
            path.unlink()
    for name, target in links.items():
        link = projection / name
        if link.is_symlink() and link.readlink() == target:
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        pending = link.with_suffix(".tmp-link")
        pending.unlink(missing_ok=True)
        pending.symlink_to(target)
        pending.replace(link)


async def refresh(client, url, shared, projection, aliases=()):
    try:
        async with client.get(url + "/api/v1/models?status=ready", allow_redirects=False,
                              timeout=aiohttp.ClientTimeout(total=8)) as response:
            response.raise_for_status()
            models = await response.json()
        if not isinstance(models, list):
            raise ValueError("expected a Model Hub model list")
        await asyncio.to_thread(lambda: sync_links(projection, catalog_links(models, shared, aliases)))
        return True
    except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError, ValueError):
        logging.warning("V-Scheduler model refresh failed; keeping the last model index")
