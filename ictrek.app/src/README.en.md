# ComfyUI `__APP_VERSION__`

A node-based editor for local image and video workflows on VOS. Select one profile for your hardware: AMD64 CPU or CUDA, ARM64 CPU or CUDA, Jetson Orin / L4T, or Thor / DGX Spark. CPU mode is intended for lightweight workflows; large models generally need a GPU.

1. Install V-Scheduler followed by ComfyUI. Select the same V-Scheduler shared model directory in both apps.
2. Open **App Drawer → ComfyUI → Workflow Editor**. The editor signs in with your VOS account and follows the VOS language.
3. Download compatible shared weights in V-Scheduler's model manager. ComfyUI checks for completed downloads about every 30 seconds. Refresh the loader options, select the weights in the matching model category, connect the workflow, and click Run.
4. Open **ComfyUI → API Integration Guide** for instructions, model compatibility, and API examples.

Workflows, uploads, outputs, and queues are isolated by VOS account. The shared model directory is read-only. For Comfy Partner Nodes, use the editor's profile menu to sign in to Comfy or enter a Comfy account API key. On a LAN address, choose the API key option. Local nodes use VOS sign-in alone. Third-party custom nodes are disabled.

For a workflow with missing safetensors weights, use **Issues → Missing Models → Download / Download All**. V-Scheduler manages the downloads. The Lotus Depth template weights let you choose Hugging Face or ModelScope per file. ComfyUI refreshes the loader choices and uses the model paths already saved in the workflow.
