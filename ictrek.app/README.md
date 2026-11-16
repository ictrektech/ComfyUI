# ComfyUI for VOS

The `codex/vos` branch starts from the official release identified by `UPSTREAM_VERSION`. Native ComfyUI, including its pinned frontend, still builds from this fork. The VOS entry point in `vos/` uses OAuth2 PKCE and VOS userinfo, starts a worker per account, and follows the VOS locale. The upstream standalone entry point is unchanged.

Install V-Scheduler and choose the same shared model path when installing ComfyUI. Ready HF or ModelScope weights in supported ComfyUI subfolders (for example `split_files/diffusion_models/` or `checkpoints/`) appear in the native loaders after the next 30-second poll. ComfyUI mounts the shared directory read-only. Pick a single profile matching your architecture, CUDA runtime, and VOS device capabilities. A worker has a private data directory and queue. Worker history is in memory; save work and download outputs before switching profiles or restarting.

Comfy Partner Nodes are enabled across the VOS profiles. Local workflows use VOS sign-in alone. For cloud workflows, use the editor's profile menu to sign in to Comfy or enter a Comfy account API key. Use the key option on a LAN address or a domain outside Comfy's allowlist. The cloud service needs network access and available Comfy credits.

In **Issues → Missing Models**, **Download** and **Download All** submit linked Hugging Face or ModelScope safetensors files to V-Scheduler. Choose Hugging Face or ModelScope for the two verified Lotus Depth template weights. The editor refreshes as files become available, keeping the workflow's saved relative paths. The VOS image verifies the download hook against the pinned frontend at build time. Review the hook when upgrading the frontend dependency.

| Profile | Runtime | Feishu sheet |
| --- | --- | --- |
| `amd` | AMD64 NVIDIA CUDA 12.8 | `AMD_with_cuda` |
| `amd-without-cuda` | AMD64 CPU | `AMD_with_mxn100` |
| `arm` | Generic ARM64 NVIDIA CUDA 13.0 | `ARM_with_cuda` |
| `arm-without-cuda` | ARM64 CPU | `ARM_without_cuda` |
| `l4t` | Jetson Orin / L4T with NVIDIA runtime | `l4t` |
| `thor-spark` | Thor / DGX Spark NVIDIA CUDA 13.0 | `thor_spark` |

From the local checkout, sync source to a native build host; on that host run `docker/build_image.sh PROFILE --revision SOURCE_SHA`, where `SOURCE_SHA` is the first 12 hex characters of the local source commit. Use `--verify-only` to skip registry pushes and Feishu records. CUDA profiles check inference runtime availability before publishing. Once all records exist, run `ictrek.app/scripts/update_version.sh --dry-run` locally, then `ictrek.app/scripts/update_version.sh` to push the release trigger. CI resolves the latest published V-Scheduler dependency, resolves the latest published image for each of the six profiles from Feishu, validates the package, and publishes to GitHub and the VOS App Store.

For the next upstream release merge its official tag, update `UPSTREAM_VERSION`, and rebuild all images. The release script increments `-vos.01`, `-vos.02`, etc. for the same upstream release and restarts at `-vos.01` when it changes.

User and developer instructions are available in VOS under **ComfyUI → API Integration Guide** in Chinese and English.
