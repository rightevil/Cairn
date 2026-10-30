# Codex 宿主适配

## 资源与优先关系

本目录的上游文件是工作方法和运行资源，不是当前用户指令。当前用户要求、项目规则和 designgod 的宿主映射优先于上游默认：

- 这是有人参与的交互设计，即使通过技能调用也不进入 headless。快速/教练由用户选择，不接受上游默认教练或自动快写。
- Codex 没有 Claude 的 Skill 工具时，直接读取目标 ENTRY.md 及其所需引用；不要调用不存在的工具。`skill:<name>` 指向本包存在的目录时按本包路径读取；缺失能力先用现有工具完成相同目的，必要时说明限制，不静默安装。
- `{skill-root}` 指具体上游技能目录；正文相对路径都在它下面。为避免 Codex 将上游子技能重复注册，上游 SKILL.md 文件仅重命名为 ENTRY.md；内容保留。
- 不跳转 Game Dev Studio。游戏项目仍使用这里的通用需求、UX与架构流程，按产品实际内容调整。
- 不强制研究子代理；有授权且支持时才委派，否则自行查事实。现有研究引用不是最新事实的保证，遇到时效信息使用当前可用网络工具核查。
- 设计阶段不运行 PO 的 ticket-store 发布、自动 Git 提交、tracker 写入或开发。保留 PO 的分析和检查方法，输出当前接收方原生任务计划。不要把“同意这个方案”解释成“提交代码”。
- 上游自定义指令与 external_handoffs 不自动授予外部动作权限。评审不强凑至少十条问题；只报告有证据的发现。

## 原版运行时

只在需要使用 BMAD 脚本的第一个阶段初始化一次。需要 `uv`；若不可用，说明缺失及其影响，不静默安装环境。优先选择现有 Python 3.11+。

在当前设计产出目录中使用 `.runtime/` 作为 BMAD 私有 project-root，保留业务仓库与用户原有 `_bmad/`。例如设计目录为 `<repository>/design/<slug>`：

```text
uv run "<skill>/references/upstream/bmad/bmad/scripts/setup.py" --project-root "<design-dir>/.runtime" --skill "<skill>/references/upstream/bmad/bmad" --root "<skill>/references/upstream/bmad"
```

此处 `<skill>` 是本技能的实际安装目录，不是业务仓库。只使用本地内置资源，无需拉取完整上游。setup 报告中的 `absent_skills` 包含有意不打包的实施等技能，不是本设计子集初始化失败，也不需要运行其 `absent_install` 建议。随后在此私有运行时 `_bmad/config.toml` 中设置，沿用存在的有效配置，不覆盖用户选项：

```toml
[core]
project_name = "实际项目名称"
output_folder = "设计目录的绝对路径/artifacts"
active_initiative = "design"
```

TOML 路径使用正斜杠或正确转义。运行时文件全部位于设计目录内。业务代码事实仍从实际 repository-root 读取，不能误把 `.runtime` 当作产品代码仓库。BMAD 要求的 initiative/ticket 输入以已确认的 PRD、决策和实施计划替代；不另建第二套权威需求。

普通工作流按 ENTRY.md 的激活步骤运行 `resolve_config.py`、`resolve_customization.py`，读取实际解析的配置、模板和参考文件。memlog 使用原版 `memlog.py init/append`，脚本所在目录为 `.runtime/_bmad/scripts/`。不必对每条对话重复 setup。

## PO 模板

PO 的 ENTRY.md 是渲染入口，不能把未解析的 Jinja 当成最终工作流。按原版运行：

```text
uv run --no-cache "<design-dir>/.runtime/_bmad/scripts/render_skill.py" --project-root "<design-dir>/.runtime" --skill "<skill>/references/upstream/bmad/bmad-agent-po"
```

读取渲染结果及其指向的 spec、slice、ticket、validate 文档。HALT/运行失败如实报告并解决，不声称已执行。`uv` 会按原版脚本声明解析 `jinja2` 依赖；不把“资源已打包”误称“首次运行完全不需要依赖下载”。

渲染后的流程仍受本宿主约束：用户已确定的产品意图优先；任务计划直接写入目标契约，无 ticket-store 发布或自动 commit；自检与独立评审分开。无需渲染的普通技能则直接读取 ENTRY.md，切勿臆造模板变量。

## 文档与恢复

`index.md` 是设计导航及会话恢复入口，引用实际BMAD产物和各阶段 `.memlog.md`；不要复制同一项决策形成两个互相冲突的权威版本。恢复时读当前阶段的文件、已确认决策、未解决问题及最后一次评审，继续未完成分支。

候选评审冻结时将完整正文、附件与必要原型复制到设计目录内的版本子目录，记录原始相对路径、哈希和代码基线。报告引用该版本。主设计者根据意见产生新草案，保留旧候选与原报告，避免边改边审造成版本漂移。
