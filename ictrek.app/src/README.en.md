# ComfyUI `__APP_VERSION__`

Keep app storage when upgrading and explicitly select the matching canonical Profile. `amd` → `amd-cuda`, `amd-without-cuda` → `amd-cpu`, `arm` → `arm-cuda`, `arm-without-cuda` → `arm-cpu`, `l4t` → `jetson-orin`, `thor-spark` → `jetson-thor`. On a device that reports DGX Spark, select `dgx-spark` for the old `thor-spark`. Provide the new `profiles` explicitly for API upgrades. Select the matching old Profile on rollback. Application APIs stay the same.

A node-based editor for local image and video workflows on VOS. Select one profile for your hardware: AMD64 CPU or CUDA, ARM64 CPU or CUDA, Jetson Orin / L4T, or Thor / DGX Spark. CPU mode is intended for lightweight workflows; large models generally need a GPU.

1. Install V-Scheduler followed by ComfyUI. Select the same V-Scheduler shared model directory in both apps.
2. Open **App Drawer → ComfyUI → Workflow Editor**. The editor signs in with your VOS account and follows the VOS language.
3. Download compatible shared weights in V-Scheduler's model manager. ComfyUI checks for completed downloads about every 30 seconds. Refresh the loader options, select the weights in the matching model category, connect the workflow, and click Run.
4. Open **ComfyUI → API Integration Guide** for instructions, model compatibility, and API examples.

Workflows, uploads, outputs, and queues are isolated by VOS account. The shared model directory is read-only. For Comfy Partner Nodes, use the editor's profile menu to sign in to Comfy or enter a Comfy account API key. On a LAN address, choose the API key option. Local nodes use VOS sign-in alone. GGUF uses the bundled loaders.

For a workflow with missing repository-linked weights, use **Issues → Missing Models → Download / Download All**. V-Scheduler manages the downloads. Choose Hugging Face or ModelScope when the matching SHA-256 can be verified. ComfyUI refreshes the loader choices and uses the model paths already saved in the workflow.
