# ComfyUI `__APP_VERSION__`

VOS 上的节点式图像与视频工作流编辑器。安装时按本机架构和加速器选择一个 profile：AMD64 CPU / CUDA、ARM64 CPU / CUDA、Jetson Orin / L4T 或 Thor / DGX Spark。CPU 模式适合轻量工作流；大型模型通常需要 GPU。

1. 安装 V-Scheduler，再安装 ComfyUI；两个应用选择同一个 V-Scheduler 共享模型目录。
2. 在 **应用抽屉 → ComfyUI → 工作流编辑器** 打开。浏览器使用 VOS 账户自动登录，界面跟随 VOS 中英文切换。
3. 在 V-Scheduler 的模型管理中下载 ComfyUI 兼容的共享权重。下载完成后，ComfyUI 约每 30 秒更新一次索引。刷新加载器选项，从对应模型分类选择权重，连接工作流后点击运行。
4. 通过 **ComfyUI → API 集成指南** 查看操作说明、适用模型和接入示例。

工作流、上传、输出和队列按 VOS 账户隔离。模型目录只读共享；合作伙伴节点和第三方自定义节点未启用。
