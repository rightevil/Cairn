> [!IMPORTANT]
> **本 fork 已停止二次开发（2026-10-30）。**
>
> 后续工作转向 [Devloom](https://github.com/rightevil/devloom)，围绕“设计优先的自治软件工程平台”重新设计与开发。
> 本仓库保留 Cairn 二开阶段的代码、历史记录和探索成果，作为后续设计的参考；下方功能介绍与使用说明保留为历史资料。
> Devloom 当前处于概念与设计阶段，尚未提供可运行的软件，进展请以新仓库为准。
>
> Development of this fork has ended. Further work continues in [Devloom](https://github.com/rightevil/devloom),
> a new project being redesigned from the ground up. This repository remains available as a historical reference.

## Windows 免安装桌面版

从 [Releases](https://github.com/rightevil/Cairn/releases) 下载 `Cairn-<版本>-windows-x64.zip`，解压完整目录后双击 `Cairn.exe`。
请保留同目录的 `_internal` 和 `cairn-runtime.exe`；无需安装 Python。需要 Microsoft Edge WebView2 Runtime、本机 Git 和所选 Agent CLI。
程序未做代码签名，Windows 可能提示未知发布者。

首次启动在设置页添加供应商（协议、地址、密钥、模型），再为 Agent 选择供应商、模型与角色。
工作台“运行设置”保存后热加载：当前任务继续使用原配置，新任务采用新配置。designgod / BMAD / grilling 已内置。
关闭窗口后继续在托盘运行；单击托盘图标恢复，右键可打开或退出 Cairn。

升级前退出 Cairn，保留 `dispatch.yaml`、`desktop.local.json` 和 `.cairn` 中的配置及数据，再替换程序文件。
本地配置可能包含密钥，请勿分享。发布包只收集程序与公共示例，附带 `.sha256` 校验文件。

### 构建与版本发布

在仓库根目录运行：

```sh
uv sync --locked --project cairn --extra desktop --group packaging
uv run --locked --project cairn python cairn/packaging/build.py
uv run --locked --project cairn python cairn/packaging/verify.py
```

构建需要 Windows x64，产物位于 `cairn/dist/`。GitHub Actions 在主分支、PR 和手动触发时测试并构建便携包；
推送与包版本一致的 `v*` 标签后，通过全部检查才创建 GitHub Release 并上传 ZIP 与校验文件。

版本由 [Commitizen](https://commitizen-tools.github.io/commitizen/commands/bump/) 统一管理：
在工作区干净、功能改动已提交后，运行 `uv run --locked --project cairn cz bump --check-consistency`，
即可按 Conventional Commits 更新 `.cz.toml`、包版本、锁文件和 `CHANGELOG.md`，并创建版本提交与标签。
确需指定级别时使用 `--increment MINOR` 或 `--increment PATCH`。发布时需将对应主分支提交及该版本标签同步到 GitHub。

<div align="center">

<img src="./README/banner.png" alt="Cairn Banner"/>

# Cairn
### More Than Just AI Penetration Testing — Towards General State-Space Search

<p>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tencent.png" alt="Tencent" height="55" />
  </a>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tch.png" alt="TCH" height="55" />
  </a>
  <a href="https://wiki.chainreactors.red" target="_blank" rel="noopener noreferrer">
    <img src="./README/c.png" alt="ChainReactors" height="45" />
  </a>
</p>

Cairn is a general-purpose problem-solving engine. <br/>It defines no roles, no workflows. Given an origin and a goal, it searches for a path through an unknown state space. <br/>AI Penetration Testing is one such problem — and a proven one.

<p>
  <a href="https://discord.gg/nDSy4NZVP" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord" />
  </a>
  <a href="https://x.com/le1xia0" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/X-000000?style=flat-square&logo=x&logoColor=white" alt="X" />
  </a>
</p>

</div>

## This fork: development history

二开阶段新增了方案设计能力：网页直接对话，由 Claude Code 自动编排
BMAD 主仓库与 Grilling；设计草案经独立评审和用户确认后，直接成为 Cairn 原生任务。

本 fork 曾探索面向软件开发的无人值守闭环：Magpie 按角色提供强／普通模型，
复用 Claude Code、Codex 和已有 Pi；功能分支独立开发，经过独立审查、串行 PR
合并与集成验证。原有网页图谱新增开发面板、建议／指令、运行日志和证据入口。

运行配置参考唯一的 [dispatch.example.yaml](dispatch.example.yaml)。
使用指南与实现说明保留在本地 `docs/`，不随仓库发布。
下文保留上游通用探索模式的介绍；其中的无固定角色、容器和渗透测试描述适用于该模式。

<p align="center">
  <a href="https://www.bilibili.com/video/BV1a8R5BhEVi/" target="_blank" rel="noopener noreferrer">
    <img src="./README/cairn.png" alt="Cairn runtime screenshot" width="900" />
  </a>
</p>

## What is Cairn?

Penetration testing is fundamentally a **directed search through a near-infinite state space**:

- **Origin**: known (target IP, target system)
- **Goal**: defined (get a shell, capture the flag)
- **Path**: unknown

This structure is not unique to penetration testing. Vulnerability research, mathematical proof, CTF challenges — any problem with a clear starting point, a clear success condition, and an unknown path in between shares the same shape.

Cairn is built for this class of problems. Penetration testing is the first domain it has been validated on.

The engine is built on a **Blackboard Architecture** with an explicit fact-intent graph. Three primitives are all it needs:

| Concept | Meaning |
|---------|---------|
| **Fact** | A confirmed, objective finding written to the board |
| **Intent** | A declared direction of exploration, not yet executed |
| **Hint** | Human judgment injected at any time; absorbed by agents on the next read |

The graph grows from `origin` toward `goal`. Every new Fact is a stepping stone; every Intent is a step into the unknown.

Agent Workers run an OODA loop — Observe the full graph, Orient to the current state, Decide on next intents, Act to explore — and write their findings back as new Facts. Workers have no fixed roles. Tasks are generated at runtime from the graph's current state, not from predefined job descriptions.

Agents coordinate exclusively through the shared board (Stigmergy). No direct communication. No information silos.

## Cairn in Action

https://github.com/user-attachments/assets/e557b1ac-dda4-41cb-87dd-9d56dbf05133


## How It Works

Three task types, all executed by the same Worker:

| Task | What it does | Output |
|------|-------------|--------|
| **Bootstrap** | At project start, attempts to solve the problem directly | Fact + possible Complete |
| **Reason** | Reads the full graph: is the goal met? What should be explored next? | Complete / new Intents / no-op |
| **Explore** | Claims one Intent, executes the exploration, reports findings | One Fact |

System architecture:

```
          ┌──────────────────────────────────┐
          │           Cairn Server           │
          │    Facts + Intents + Hints       │
          └─────────────────┬────────────────┘
                            │
                     Read / Write API
                            │
          ┌─────────────────┴────────────────┐
          │             Dispatcher           │
          │   Schedules tasks, manages       │
          │   containers, writes protocol    │
          └──────────┬───────────────┬───────┘
                     │               │
     ┌───────────────┴──┐     ┌──────┴──────────────┐
     │  Worker Container│     │  Worker Container   │
     │   (Project A)    │     │   (Project B)       │
     │  ┌────┐  ┌────┐  │     │  ┌────┐  ┌────┐     │
     │  │ W. │  │ W. │  │     │  │ W. │  │ W. │     │
     │  └────┘  └────┘  │     │  └────┘  └────┘     │
     └──────────────────┘     └─────────────────────┘
```

**Cairn Server** maintains graph consistency only.

**Cairn Dispatcher** reads the graph, schedules tasks, spins up and tears down worker containers, and is the sole writer to the protocol. Each project gets its own Worker Container; multiple Agent Workers run concurrently inside it. Agent Workers only receive a prompt and return structured output.

Workers can also run directly on the dispatcher host instead of in per-project containers — **local mode**, no Docker required. See [Local mode](#local-mode-no-docker) below.

Supported worker backends: **Claude Code**, **Codex**, and **Pi**.

## Results

**Tencent Cloud Hackathon · AI Penetration Testing Challenge · 2nd Edition**

610 teams · 1,345 participants · top universities and security firms across China

| Metric | Value |
|--------|-------|
| Problems solved | **54 / 54 — only team to AK** |
| Final ranking | 3rd |

> The system had never been tested before the competition. The full pipeline came online for the first time at 4 AM on race day. No training, no tuning, no domain-specific tooling. Zero MCP tools, zero RAG, zero predefined agent roles.

## Further Reading

- <a href="https://mp.weixin.qq.com/s/DlpEH7bVr0xi0VawPJs3XA" target="_blank" rel="noopener noreferrer">The Strongest AI Penetration Testing Agent: Postmortem of the Only Team to Achieve AK at the TCH Tencent Cloud Hackathon Intelligent Penetration Testing Challenge (2nd Edition)</a>
- <a href="https://mp.weixin.qq.com/s/2rEqFLvkxvYWM3gW170C2w" target="_blank" rel="noopener noreferrer">The Pathless Path: Cairn AI from Penetration Testing to General Problem Solving</a>

## Getting Started

**Prerequisites**
 
- macOS or Linux
- Python ≥ 3.12
- Docker (container execution only — not needed for local mode)


### Pull required images
 
Container execution requires the worker image; host execution does not:
 
```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

Create your local dispatcher configuration and fill in your LLM endpoints and API keys:

```bash
cp dispatch.example.yaml dispatch.yaml
```
 
### Docker Compose (container mode)

The single example defaults to host development. Before using container dispatch,
adapt your local `dispatch.yaml`: select `runtime.execution: container`, configure
the container and exploration workers, and set `server: http://cairn-server:8000`.
 
Pull the base image used to build Cairn:
 
```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie
```
 
```bash
docker compose up --build
```
 
This starts `cairn-server` on port `8000` and `cairn-dispatcher` once the server passes its health check. The dispatcher mounts `dispatch.yaml` from the project root and connects to Docker via the host socket. Data is persisted to `./datas/cairn/`.
 
### Manual
 
```bash
# Start the server
uv run --project cairn cairn serve --db-path .cairn/data/cairn.db
 
# Run the dispatcher
uv run --project cairn cairn dispatch --config dispatch.yaml
 
# Run startup health checks only
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

### Local mode (no Docker)

Instead of one container per project, workers can run directly on the dispatcher host, reusing the machine's already-configured `claude` / `codex` / `pi` CLIs — no Docker, and no API keys in the config.

```bash
cp dispatch.example.yaml dispatch.yaml

# Start the server
uv run --project cairn cairn serve --db-path .cairn/data/cairn.db

# Run the dispatcher on the same host, where the CLIs are installed and logged in
uv run --project cairn cairn dispatch --config dispatch.yaml
```

Local mode is selected by `runtime.execution: local` (see `dispatch.example.yaml`). Set the Magpie environment values in your local startup script before launching. On startup the dispatcher checks each configured worker CLI is installed and runnable. Each project gets an isolated working directory under `local.workspace_root`. Run the dispatcher directly on the host, not inside Docker.

### Tests

Run the fast regression suite without Docker or live model endpoints:

```bash
uv run --project cairn --group dev pytest
```

## Disclaimer

Cairn is a general-purpose problem-solving engine. Although it supports penetration testing, CTF solving, security assessment, and vulnerability research workflows, it is intended to be used only in environments where you have explicit authorization to operate.

You are solely responsible for how you use this project. Do not use Cairn against systems, networks, applications, or data without clear prior permission from the owner or operator. Unauthorized security testing, exploitation, or data access may be illegal and may cause harm.

The developers and contributors of this project do not endorse or accept responsibility for any misuse, abuse, damage, loss, or legal consequences arising from its use. By using this project, you agree to ensure that your activities comply with all applicable laws, regulations, contractual obligations, and professional or organizational policies in your jurisdiction.

## Star History

<a href="https://www.star-history.com/#oritera/Cairn&Date" target="_blank" rel="noopener noreferrer">
  <img src="https://api.star-history.com/svg?repos=oritera/Cairn&type=Date" alt="Star History Chart" />
</a>

## ⚖️ License
This project is licensed under **GNU AGPLv3** for personal and educational use.

**Commercial Use**: If you wish to use this project in a commercial or proprietary environment without the AGPL-3.0 open-source obligations, **please contact me to obtain a commercial license.**

**Contributions**: By submitting a Pull Request, you agree that your contributions may be used under both the AGPL-3.0 and the project's commercial license.
