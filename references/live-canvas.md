# 默认实时画布与节点编辑

`scripts/canvas_runtime/` 是本skill自带可复用连接，不引用任何MV项目文件。运行状态在 `~/.codex/integrations/local-mv-canvas/`；每个项目的GUI/API、核对、逐次编辑和提交记录在该项目 `workflows_synced/`。

## 初始化

用视频环境Python执行 `scripts/mv.py canvas --project-dir <工程绝对目录> --project-id <manifest中的project>`。安装/更新本机前端加载器、启动独立本机桥并选择工程；不下载模型或重启ComfyUI。当前MCP环境为 `~/.codex/integrations/comfy-mcp/.venv`，已安装mcp2和aiohttp。

MCP服务 `local_mv_canvas` 入口为 `scripts/canvas_mcp.py`。先 `canvas_status` 确认工程路径和一个浏览器连接，必要时 `canvas_select_project`。ComfyUI页面应出现“MV Agent / 跟随 Agent / 保存手动修改”。打开/刷新页面供用户观察；同一桥只连接一个可编辑页面。

默认前端目录为现有KJNodes的web目录；`MV_CANVAS_WEB_DIR` 可指定另一已由ComfyUI提供的扩展web目录。无需重启忙碌后端；目录不存在则报告接入条件，不能谎称已连接。ComfyUI根和URL由配置与 `WAN_COMFY_ROOT`、`WAN_COMFY_URL` 指定。不打印、复制或交付连接令牌。

## 真实节点操作

1. `canvas_export` 读取当前GUI/API；`canvas_new_graph(name, expected_revision)` 先备份再建新图。
2. `canvas_add_node(class_type, inputs, position, expected_revision)` 创建一个真实节点并返回实际 `node_id`。`canvas_list_nodes` 读取控件和插槽。
3. `canvas_set_inputs(node_id, inputs, expected_revision)` 按控件名称修改提示词、seed、采样器等。动态控件先改父项再读取子项；未知字段回退本次修改，不留下半改的图。
4. `canvas_connect(source_node, source_output, target_node, target_input, expected_revision)` 连一条真实线；输出用名称或索引，目标用输入名称。已占用输入先显式 `canvas_disconnect`，不隐式替换；支持时通过原生接口把widget转input。
5. `canvas_remove_node` 删除节点；动作及实际GUI/API留在 `workflows_synced/edits/`。依赖操作逐个调用，让每次修改出现在画布。
6. `canvas_save_edits` 保存独立修订，不采样。`canvas_submit_current` 是真实GPU操作，只用于已认可的制作范围。Skill的 `generate` 也默认保存画布与任务映射后单次提交。

每次编辑返回 `revision`，下一次写操作携带它。版本不符或检测到用户未保存修改时重新读取，不盲目重试或覆盖。用户关闭跟随时节点写操作拒绝执行；保存不自动重开跟随、不自动跑GPU。

## 生成与隔离

`mv.py generate --manifest ...` 绑定工程，原生导入当次真实API并逐输入核对，保留原任务节点ID以兼容已有state，附GUI元数据和浏览器client_id。响应不明先检查原prompt_id，不重复提交。

节点编辑期间暂停自动换镜；明确恢复跟随或准备生成后返回执行跟随。切换工程拒绝接管旧工程队列，备份旧画布并按工程路径生成独立工作流目录。RIFE走同一可见提交；FFmpeg及本地VSR不属于ComfyUI节点执行，另行报告。

只有用户明确选择无网页运行才设置 `MV_CANVAS_MODE=headless`，不能连接失败自动降级。dry-run和只读检查不要求浏览器、不提交GPU。

## 验证

用无模型LoadImage/PreviewImage图实际调用MCP测试创建、参数、连线、断线、删除、保存及GUI/API恢复，不必提交GPU。测试前备份，finally恢复原画布与跟随状态。协议握手/节点测试不能替代真实视频质量验收。
