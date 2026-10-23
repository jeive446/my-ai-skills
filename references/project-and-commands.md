# 项目清单与入口

示例assets/project.example.json；项目可有characters、scenes、shots、titles。生成器消费project/audio/lrc/width/height/fps/shots，其他字段留在原清单供agent组织。统一清单是信息依据。

建议文件：project.json、project_memory.md、characters/、scenes/、frames/、titles/、workflows/、render/、qa/、deliverables/。无需预建无用空目录。

执行Python用D:/AI_Video/ComfyUI/.venv/Scripts/python.exe。脚本用本skill安装绝对路径C:/Users/Wang/.codex/skills/local-2d-anime-mv/scripts/mv.py。下面参数标记需替换实际路径，不能原样执行。

- environment：只读依赖/服务检查，不展示凭据。
- image --spec image-spec.json [--dry-run]：spec含prompt、output、references（路径相对spec）。已有输出保留；重做改revision文件名。每请求隔离工作目录；外部请求失败不会自动重复，先确认原响应/用量。
- canvas --project-dir 工程绝对目录 [--project-id 项目名]：启动通用画布连接并选择项目，不提交GPU；默认先完成此步骤，详见live-canvas.md。
- plan --manifest project.json [--canvas]：不采样；检查素材并保存每镜API和plan.json。canvas通过原生前端导出同参数GUI与总览，不再按API字典顺序拼控件。结构设计/修改使用节点MCP逐步展示。
- generate --manifest project.json --dry-run：校验文件与计划，不提交GPU。
- generate --manifest project.json [--resume]：默认前端核对并保存真实GUI/API后单次提交；状态在项目/render/项目名/state.json，单卡串行。连接失败不自动后台降级；仅用户明确无网页时设MV_CANVAS_MODE=headless。
- qa --video clip.mp4 --output-dir qa目录：逐帧解码、采样图、最大亮度跳变帧对及quality.json。还需视觉审看，不自动宣布语义合格。
- compose --manifest project.json --output final.mp4 --width 1080 --height 1920：基础30fps合成；横版用1920/1080。默认cover，contain留白按美术选择。LRC生成可读字幕，艺术字另层处理。
- scripts/batch_rtx_vsr.py --input-dir 合格独立镜头目录 --output-dir 新目录 --target-width 1080 --backend rtx --cq 16：9:16输出；横版用target-height 1080。常规scale到1080p不等于AI超分。
- overlay_titles.py --video clean.mp4 --titles titles.json --output titled.mp4：已校字PNG定时淡入淡出；最后尺寸叠加。复杂转场/高级文字动效由agent按项目生成FFmpeg/ASS方案，不宣称已自动实现所有包装。

## 恢复和单镜修订

resume校验manifest、图片和模板哈希。超时任务可能仍运行，先查prompt_id历史与队列再续跑；不并发重启同项目。

改一镜用新revision项目，仅生成该镜。通过QA后新建终剪版本manifest，未改镜引用旧合格输出、新镜引用新输出；用adopt_outputs.py校验现有30fps源片并登记，不能手工伪造completed。该工具不接纳已超分/60fps转码片为原始源。

当前不能用一次resume自动接受任意manifest变化。新版本项目/状态保持可追溯；agent做有界质量修订，不无限无人值守循环。

## 标题时间层

titles.json为数组，各项image、start、end、x、y、width，可选fade。图像路径相对JSON，位置/宽度为最终像素，需真实alpha。先构建clean版再叠文字；底层视频不经过Wan重绘标题。


## 推荐的无字幕后期分支

需要VSR/艺术字时，先另存clean项目清单（lrc=null）并adopt同一批合格源片，按源尺寸compose，保留无字幕母版。对clean版用VSR达到最终尺寸，再叠艺术字；普通歌词使用原合成器生成的ASS（PlayRes须匹配最终尺寸）最后烧录并保留clean文件。VSR可处理已剪clean整片，因其逐帧处理不会跨切点插帧。不要用基础compose常规scale代替VSR。

可选60fps：scripts/interpolate.py --video 独立无声30fps镜头 --workdir 单镜插帧工作目录 [--dry-run]。使用RIFE2倍、60fps封装、保存任务ID，不重跑未知任务。此分支需要项目样片确认，不能跨硬切或对已合成整片调用。得到60fps独立镜头后由agent按原音乐时间轴合成60fps分支；基础compose有意固定30fps，避免把复制帧误称插帧。
