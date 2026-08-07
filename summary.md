# learn-claude-code 仓库总结

> 来源：`README.md` 总结

## 一句话概述

本仓库是一个**0 到 1 的 Harness（操作环境）工程学习项目**，教你如何为 Agent 模型构建"操作环境"（工具、知识、上下文、权限等），而非训练模型本身。

**核心理念：Agent 产品 = 模型（Model）+ 操作环境（Harness）。模型的智能来自训练，Harness 赋予模型手、眼与工作空间。**

---

## 核心思想

### 1. 自主性（Agency）来自模型，而非代码

- 感知、推理、行动的自主能力来自**模型训练**，不是外部代码编排。
- 历史里程碑证明这一点：DeepMind DQN（Atari）、OpenAI Five（Dota 2）、AlphaStar（星际争霸 II）、腾讯绝悟（王者荣耀）、以及 2024-2025 年的 LLM 编码智能体。
- 智能体不等于拼接提示词/流程编排。拖拽式工作流、if-else 节点图、提示词瀑布流不过是"裹着 LLM 的 shell 脚本"，无法靠堆砌过程逻辑创造智能。

### 2. 从"构建 Agent"到"构建 Harness"

"构建 Agent"只有两种含义：
- **训练模型**：通过 RL、微调、RLHF 等梯度方法调整权重。
- **构建 Harness**：编写让模型在特定领域工作的运行环境（本仓库核心）。

```
Harness = 工具 + 知识 + 观测 + 行动接口 + 权限
  工具：文件 I/O、shell、网络、数据库、浏览器
  知识：产品文档、领域参考、API 规格、风格指南
  观测：git diff、错误日志、浏览器状态、传感器数据
  行动：CLI 命令、API 调用、UI 交互
  权限：沙箱隔离、审批流程、信任边界
```

### 3. Harness 工程师的职责

- **实现工具**：文件读写、shell、API 调用、浏览器控制等，设计要原子化、可组合、描述清晰。
- **整理知识**：按需加载领域知识，而非一上来全部注入。
- **管理上下文**：子智能体隔离防止噪音泄漏；上下文压缩防止历史淹没当下。
- **控制权限**：沙箱化文件访问、破坏性操作需审批。
- **收集轨迹数据**：智能体的每次行动序列都是未来微调的训练信号。

### 4. 为什么选 Claude Code

Claude Code 是最优雅、最完整的 Agent Harness 实现，因为它**不试图成为智能体本身**，不施加僵化工作流，而是给模型工具、知识、上下文管理和权限边界后"让位"。

```
Claude Code = 一个 agent 循环
            + 工具（bash、read、write、edit、glob、grep、browser...）
            + 按需技能加载
            + 上下文压缩
            + 子智能体生成
            + 带依赖图的任务系统
            + 异步邮箱团队协作
            + worktree 隔离并行执行
            + 权限治理
            + hooks 扩展系统
            + 记忆持久化
            + MCP 外部能力路由
```

---

## 核心模式（Agent Loop）

- 循环恒定不变（属于模型），机制变化（属于 Harness）。
- 模型决定何时调用工具、何时停止；代码只执行模型的要求。
- 每次循环：发送 messages 得到响应，若非 tool_use 则结束；执行工具并追加结果，继续循环。

---

## 课程结构：两条轨道

| 轨道 | 内容 |
|---|---|
| **当前轨道（推荐）** | 根目录 `s01_agent_loop/` 至 `s20_comprehensive/`，共 20 课。每章含完整叙事 README、多语言翻译、可运行 `code.py`、SVG 图解。 |
| **旧版过渡轨道** | `docs/`、`agents/`、`web/`，保留旧的 12 课版本，供老读者与现有 Web 平台使用。 |

注意：新旧章节编号不对应，避免跨轨道混用章节号。

### 旧版到新版映射（节选）

旧 s01 到新 s01（Agent Loop）、旧 s02 到新 s02（Tool Use）、旧 s03 到新 s05（TodoWrite）、旧 s04 到新 s06（Subagent）、旧 s05 到新 s07（Skill）、旧 s06 到新 s08（Context Compact）、旧 s07 到新 s12（Task System）、旧 s08 到新 s13（Background Tasks）、旧 s09 到新 s15（Agent Teams）、旧 s10 到新 s16（Team Protocols）、旧 s11 到新 s17（Autonomous Agents）、旧 s12 到新 s18（Worktree Isolation）。

---

## 20 课总览（每课增加一个 Harness 机制）

| 章节 | 主题 | 要点 / 口号 |
|---|---|---|
| s01 | Agent Loop | 一个循环 + Bash 就够；messages / while True / stop_reason |
| s02 | Tool Use | 增加一个工具 = 增加一个 handler；TOOL_HANDLERS 分发映射 |
| s03 | Permission | 先设边界，再给自由；PermissionRule / 审批管线 |
| s04 | Hooks | 围绕循环挂钩，绝不重写循环；PreToolUse / PostToolUse |
| s05 | TodoWrite | 没有计划的智能体会漂移；先计划后执行，完成率翻倍 |
| s06 | Subagent | 大任务拆小，子任务获干净上下文；fresh messages[] |
| s07 | Skill Loading | 按需加载知识；SkillManifest / 按需注入 |
| s08 | Context Compact | 上下文总会填满，要有腾出空间的方法；snipCompact / microCompact / autoCompact |
| s09 | Memory | 记住重要的，忘记不重要的；选择 / 提取 / 整合三子系统 |
| s10 | System Prompt | 提示词运行时组装，不硬编码；分区拼接、按需加载 |
| s11 | Error Recovery | 错误不是终点而是重试起点；token 升级 / 后备模型 |
| s12 | Task System | 大目标拆小任务、排序、落盘；TaskRecord / blockedBy |
| s13 | Background Tasks | 慢操作放后台，智能体继续思考；线程执行 / 通知队列 |
| s14 | Cron Scheduler | 按计划自动触发，无需人工；持久调度 |
| s15 | Agent Teams | 一个放不下就委派给队友；MessageBus / 异步邮箱 |
| s16 | Team Protocols | 队友需要共享通信规则；固定请求-回复格式 |
| s17 | Autonomous Agents | 队友看板自取任务、自我组织；空闲循环 / 自动认领 |
| s18 | Worktree Isolation | 各干各的目录，互不干扰；WorktreeRecord 绑定任务 |
| s19 | MCP Plugin | 能力不足就通过 MCP 接入外部工具；多传输 / 通道路由 |
| s20 | Comprehensive Agent | 众多机制，一个循环；所有机制回归一个完整 Harness |

### 学习路径（六个阶段）

1. **核心能力**：让智能体行动（s01 循环、s02 工具、s03 权限、s04 Hooks）
2. **处理复杂任务**：s05 TodoWrite、s06 Subagent、s08 Context Compact
3. **记忆与恢复**：s09 Memory、s10 System Prompt、s11 Error Recovery
4. **运行长任务**：s12 Task System、s13 Background Tasks、s14 Cron Scheduler
5. **多智能体协作**：s15 Agent Teams、s16 Team Protocols、s17 Autonomous Agents、s18 Worktree Isolation
6. **扩展与组装**：s07 Skill Loading、s19 MCP Plugin、s20 Comprehensive Agent

---

## 如何阅读 / 快速开始

每章是一个文件夹，包含 README.md（中文源文）、README.en.md、README.ja.md、code.py、images/。建议按 s01 到 s20 顺序阅读。

```sh
git clone https://github.com/shareAI-lab/learn-claude-code
cd learn-claude-code
pip install -r requirements.txt
cp .env.example .env   # 配置 ANTHROPIC_API_KEY

python s01_agent_loop/code.py        # 入门：一个循环 + bash
python s08_context_compact/code.py   # 上下文压缩（复杂）
python s20_comprehensive/code.py     # 终点：所有机制一个循环
```

Web 平台（当前渲染旧版 docs/ 轨道）：`cd web && npm install && npm run dev`

---

## 项目结构

```
learn-claude-code/
  s01_agent_loop/ ... s20_comprehensive/  # 每章一个文件夹（README + 翻译 + code.py + images）
  agents/          # 旧版 12 个可运行副本 + s_full.py
  skills/          # s07 使用的技能文件
  docs/            # 旧版 12 课文档（过渡期保留）
  web/             # 当前渲染旧版 docs/ 轨道
  tests/
```

---

## 后续延伸

- **Kode Agent CLI**（`npm i -g @shareai-lab/kode`）：开源编码智能体 CLI，支持技能与 LSP、Windows 兼容、支持 GLM/MiniMax/DeepSeek 等开源模型。
- **Kode Agent SDK**：将智能体能力嵌入应用的无状态库。
- **姊妹教程 claw0**：讲解"常驻型 Harness"——heartbeat + cron + IM 多通道 + 记忆 + Soul 人格，把智能体从"即用即弃"变成"每 30 秒自动醒来找活干"的常驻个人 AI 助理。

---

## 关键收获

> 模型提供智能，环境提供行动空间，二者结合构成完整智能体。
> 好好构建 Harness，模型自会完成其余工作。
> 这不是"复制源码"，而是"掌握关键设计，自己动手构建"。

## 许可证

MIT
