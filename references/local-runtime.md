# 本地环境

ComfyUI默认D:/AI_Video/ComfyUI；Python D:/AI_Video/ComfyUI/.venv/Scripts/python.exe；服务127.0.0.1:8188。RTX5070 12GB/约48GB RAM，Python3.12/PyTorch2.11.0+cu128。

进程变量WAN_COMFY_ROOT、WAN_COMFY_URL可更改路径/服务，MV_IMAGE_GEN_DIR可指定已有image-gen目录。默认~/.codex/skills/image-gen。不改系统网络/依赖，不读取provider密钥。包不含模型、凭据、歌曲或旧MV。

所需模型：
- diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors
- diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors
- loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors
- loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors
- text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors
- vae/wan_2.1_vae.safetensors

采样总4步、高低各2步、CFG1、euler/simple、shift5。两分支PathchSageAttentionKJ（上游原拼写）用sageattn_qk_int8_pv_fp16_cuda，关闭编译。CPU文本编码、原生动态卸载；不把Wrapper BlockSwap接进原生MODEL图。

VAE空间256/64；时间窗口覆盖整个生成帧数，向上对齐4且至少64，重叠16。2秒→61帧→窗口64；4秒→121帧→窗口124。长镜头内存需实测。因果VAE与外部切块上下文有关，不把“各块独立亮度归一化”当成已证实算子机制。

独立模板H.264 CRF16。plan --canvas通过实时连接调用原生前端创建并序列化GUI，导出API逐输入核对；不按字典顺序拼控件。编辑通过local_mv_canvas节点工具逐步展示，默认生成附实际GUI元数据。旧16fps模板不更新。初始化见live-canvas.md。

服务未运行时先查启动进程，按制作授权可Hidden方式启动main.py --reserve-vram 1.0 --port 8188。不要重启忙碌服务或升级驱动/torch。

RIFE4.9模型位于ComfyUI-Frame-Interpolation/ckpts/rife/rife49.pth，SHA256=e55fd00f3cc184e3c65961f4bb827a9da022e78eed36b055242c0ac30000d533。插件已注册。NVIDIA VSR已有nvidia-vfx0.2.0.0，包内脚本复用已测实现。

image-gen多参考会拼为单张画板；提示词说明分区职责，不承诺原生多图条件。当前固定1024方图，不把提示词比例当作尺寸保证。

包内assets/wan22_720p30.workflow.json为可直接加载的2秒GUI模板；reference.png需替换为项目首帧。plan导出每镜参数版，运行以实际API为准。
