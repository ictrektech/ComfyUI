import os
import json
from pathlib import Path

import comfy.sd
import comfy.utils

def first_file(path, filenames):
    for f in filenames:
        p = os.path.join(path, f)
        if os.path.exists(p):
            return p
        index = p + ".index.json"
        if os.path.isfile(index):
            return index
    return None


def load_component(path):
    if path is None:
        raise FileNotFoundError("Diffusers component weights not found")
    if not path.endswith(".index.json"):
        return comfy.utils.load_torch_file(path, safe_load=True)
    index = Path(path)
    if index.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Diffusers shard index exceeds 16 MiB")
    directory = index.parent.resolve()
    with index.open(encoding="utf-8") as stream:
        mapping = json.load(stream).get("weight_map")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("Diffusers shard index has no weight_map")
    shards = {}
    for name, filename in mapping.items():
        if not isinstance(filename, str):
            raise ValueError("Diffusers shard filename must be a string")
        shard = (directory / filename).resolve()
        if not shard.is_relative_to(directory) or shard.suffix not in {".safetensors", ".bin"}:
            raise ValueError("Diffusers shard points outside the component")
        shards.setdefault(shard, []).append(name)
    result = {}
    for path, keys in shards.items():
        tensors = comfy.utils.load_torch_file(str(path), safe_load=True)
        for key in keys:
            result[key] = tensors[key]
    return result


def load_diffusers(model_path, output_vae=True, output_clip=True, embedding_directory=None):
    diffusion_model_names = ["diffusion_pytorch_model.fp16.safetensors", "diffusion_pytorch_model.safetensors", "diffusion_pytorch_model.fp16.bin", "diffusion_pytorch_model.bin"]
    unet_path = first_file(os.path.join(model_path, "unet"), diffusion_model_names)
    vae_path = first_file(os.path.join(model_path, "vae"), diffusion_model_names)

    text_encoder_model_names = ["model.fp16.safetensors", "model.safetensors", "pytorch_model.fp16.bin", "pytorch_model.bin"]
    text_encoder1_path = first_file(os.path.join(model_path, "text_encoder"), text_encoder_model_names)
    text_encoder2_path = first_file(os.path.join(model_path, "text_encoder_2"), text_encoder_model_names)

    text_encoder_paths = [text_encoder1_path]
    if text_encoder2_path is not None:
        text_encoder_paths.append(text_encoder2_path)

    if unet_path and unet_path.endswith(".index.json"):
        unet = comfy.sd.load_diffusion_model_state_dict(load_component(unet_path))
        if unet is None:
            raise RuntimeError("Unsupported Diffusers diffusion model")
    else:
        unet = comfy.sd.load_diffusion_model(unet_path)

    clip = None
    if output_clip:
        if any(path and path.endswith(".index.json") for path in text_encoder_paths):
            clip = comfy.sd.load_text_encoder_state_dicts(
                [load_component(path) for path in text_encoder_paths], embedding_directory=embedding_directory)
        else:
            clip = comfy.sd.load_clip(text_encoder_paths, embedding_directory=embedding_directory)

    vae = None
    if output_vae:
        sd = load_component(vae_path)
        vae = comfy.sd.VAE(sd=sd)

    return (unet, clip, vae)
