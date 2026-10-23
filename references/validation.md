# 本机验证与边界（2026-10-21）

## 2026-10-22 默认画布MCP

- 通用 `scripts/canvas_runtime/` 与 `local_mv_canvas` 已实际运行，不依赖《第57次取消發送》工程路径。MCP握手和15个工具的列表已验证。
- 实际通过MCP调用节点创建、改参、连线、断线、删除、保存、恢复，17次调用/检查通过；旧revision写入、占用输入的隐式覆盖均拒绝。测试后恢复原35节点GUI并恢复原跟随状态。
- 默认可见提交、断线不静默降级、显式headless例外、两个独立新项目目录的输出/命名隔离均通过无GPU测试。GUI序列化后再载入、导出API核对无差异。
- 本轮测试没有提交GPU任务。带新通用提交元数据的实际Wan采样仍留给下一次正常获准生成；不把模拟提交或节点测试称作视频质量验收。
- 验证证据在当前工程qa/canvas_skill_verification.json、canvas_skill_defaults_verification.json与canvas_nodes_mcp_verified.jpg；这些项目证据不随skill打包。

- 用户选择720p30fps直接生成B，认可修复后观感。
- Wan2.2 I2V A14B FP8/4步/CFG1/Sage：1280×720约2秒61帧直出成功，wall约278秒（含调度/加载/编码，非性能承诺），整卡采样最大约11.6GB。
- 同latent比较VAE时间16/4、32/8、64/16，PNG最大相邻帧全图平均亮度跳变6.7307、5.8915、0.5518。64/16覆盖61帧，减少约91.8%；同CFG1/空间256/64/CRF16，主因确认为外部时间切块，未逐算子归因。
- RIFE4.9 FP32 ensemble4倍：三段768方形16fps VSR素材→64fps→60fps，全解码通过，用户认可；16→32→30另测通过。
- NVIDIA VSR55段素材跑通并重剪MV；旧片源多数384方形，不能当成本skill已完成全曲720p30fps生产的证据。
- 原生720p30fps更长、竖屏、大动作；全曲新画风美术质量仍需项目样片。时间窗口需覆盖镜头并测试内存，不能假定T64适合所有长度。
- 包制作测试记录在外部package_validation.json，覆盖30fps接口、长时长帧数/时间窗口、计划/画布、合成音频/字幕、恢复/迁移和标题alpha。没有为安装包重新启动整首GPU生成或外部收费生图。

原始证据目录：H:/GPT_WorkPlace/Create_mv/wan_flicker_test、wan_720p_fps_test、rife_test、mv_skill_design/baselines/wan22_720p_v1。它们不随skill打包；迁移机器时这些路径不能当已存在依赖。

打包时额外验证interpolate.py：用户认可的1280×720/30fps修复片60帧→RIFE2倍119帧/60fps，完整解码通过。两端点之间插帧输出2N-1，容器时长约1.983秒；终剪显式处理端点，不自动改音乐。未新增Wan采样或外部生图请求。
