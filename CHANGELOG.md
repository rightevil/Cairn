## v0.3.1 (2026-10-30)

### Fix

- **packaging**: write portable checksums with LF line endings

## v0.3.0 (2026-10-30)

### Feat

- **desktop**: add portable tray app, live provider settings and release workflow
- **ui**: visualize implementation progress as a node flow
- **ui**: 以浅色暮光玻璃工作台突出原生开发画板
- **design**: 接入受控原生终端与可重连的设计评审会话
- **projects**: 添加安全的项目删除入口并保留磁盘文件
- **workspace**: 重构全局视觉并补齐工具详情与旧方案材料交接
- **workspace**: 重构项目工作台并完善方案设计交互
- **design**: 新增交互式方案设计与原生任务交付
- **delivery**: 支持本地独立审查交付与可恢复的合并回执
- **workers**: 为开发执行器注入专用系统提示词并隔离会话上下文
- **web**: 将操作面板本地化为简体中文
- **web**: 展示开发控制、进度与验证证据
- **runtime**: 执行独立工作树并集成已审查的变更
- **planner**: 协调开发计划评审与模型角色分工
- **blackboard**: 持久化开发任务、运行记录与可信控制指令
- run worker health checks in code instead of in-container curl
- add local execution mode for host-run workers
- update Dockerfile to install aliyun-cli alongside tccli and awscli
- add installation of tccli, awscli and aliyun-cli
- add optional project bootstrap and skip it when no worker supports the task
- add configurable worker healthcheck modes and default to startup-only checks
- enhance project dispatch logic to handle reason triggers more effectively
- implement clipboard copy fallback
- add stream closing logic in ManagedProcess to prevent resource leaks
- add ripgrep and fd installation to Dockerfile for enhanced file searching capabilities
- skip repeated inactive cleanup and keep orphan containers for inspection
- enhance dispatch.yaml with detailed comments for better clarity
- add supported worker backends to documentation
- add pi worker support
- add caching for Docker build to improve build performance
- add installation of pi-coding-agent to Dockerfile
- add further reading section to README with relevant links
- add disclaimer section to README for responsible usage guidelines
- add Discord and X social media badges to README
- update README to use new banner image
- add "Cairn in Action" section with demo link to README
- add runtime screenshot to README
- 增强验证，添加数据结构检查
- 修改提示词为英文
- 添加明确的配置示例
- 添加 Docker Compose
- 添加 Cairn 核心代码
- 自动编译上传 worker 镜像
- 工作容器

### Fix

- **terminal**: 持久回放设计会话历史并移除网页输入框
- **workbench**: 修复设计终端、评审问答与协作运行边界
- **progress**: 按任务聚合开发运行并收敛项目会话导航
- **blackboard**: 区分节点状态并保留视口与运行定位
- **agent-ui**: 展开工具输入结果并合并真实调用与脱敏记录
- **review**: 约束独立审查执行并复用同候选检查证据
- **workspace**: 将新项目工作区和设计记录归入项目目录
- **dispatch**: 区分推理基础设施故障并避免网关重试叠加
- **design**: 统一会话交互并闭合受控评审修订与材料交接
- **dispatch**: 区分工作区文件与共享资源并显示真实调度状态
- **design**: 接续评审修订任务并恢复已解冻方案上下文
- **design**: 展示独立评审进度并保留完整报告入口
- **design**: 分离设计自检独立评审与未来验收证据
- **design**: 区分阶段冻结评审与最终开发交付
- **design**: 自动同步方案产物到项目目录并展示保存位置
- **design**: 展示方案源文件正文并固定完整评审快照
- **design**: 隐藏空白流式消息的重复标头
- **design**: 修复文档章节引用并同步评审就绪校验
- **scheduler**: 优先派发阻塞实现任务的方向评审
- **workflow**: 在计划与裁决更新时隔离失效的控制指令
- **workers**: 要求返回真实 JSON 结果而非后续处理承诺
- **review**: 要求评估用户建议时提供匹配的证据
- **planner**: 明确未完成任务的原位计划调整方式
- **workflow**: 复用中断的裁决并清理排队的重复任务
- **workflow**: 应用架构裁决中已验证的前置依赖变更
- **planner**: 明确架构结果协议与格式错误恢复方式
- **review**: 要求交付评审核对完整候选清单
- **workers**: 明确复用与当前任务匹配的验证证据
- **workers**: 避免仅修改文档时重复执行作者检查
- **workflow**: 在控制版本切换时保留任务交接证据
- **planner**: 明确替换依赖时的架构裁决范围
- **runtime**: 保留近期窗口之外的任务尝试历史
- **planner**: 引导开发计划拆分为可独立并行交付的任务
- **workflow**: 保留实现检查点并避免重复空转
- **web**: 展示运行中的开发执行器与修订状态
- **workers**: 向重试执行器提供被拒结果与验证反馈
- **planner**: 明确调度等待原因与任务依赖标识
- **config**: 明确 YAML 字面量配置并隐藏错误中的敏感输入
- **runtime**: 支持 Windows 原生 Claude 启动并完善失败日志
- **git**: 保留执行器意外切换分支后的改动
- **runtime**: 隔离并发恢复中的不确定外部操作
- **workflow**: 加固持久化决策、检查点与交付评审
- **runtime**: 修复 Windows 执行器的生命周期与快照
- pass graph snapshots through container files to avoid argv limits
- prevent container build workflow from running on tag pushes
- keep codex healthcheck on responses api for codex cli compatibility
- switch codex healthcheck to chat completions
- tighten bootstrap conclude compatibility
- 有些模型在 conclude 阶段仍然继续探索，优化提示词强调一下
- 增加重试等待时间至 5 秒
- 删除不再使用的 README 图片文件
- 更新协议名称
- 添加 datas 目录到 .gitignore
- 更换镜像源

### Perf

- **context**: 去重完整设计并提供依赖与续做上下文
- **runtime**: 精简开发上下文并跳过被拒评审的验证
