# S15 Integrated Harness 模块分析笔记

## 1. 项目定位

`code-new` 是原单文件版 `s15_integrated_harness/code.py` 的包化重构。它没有把每种能力做成彼此独立的程序，而是把工具调用、权限、记忆、技能、任务图、子代理、持久队友、后台命令、Cron、上下文压缩、错误恢复、Git worktree 和 MCP 都接到同一个 Agent 循环上。

核心思想可以概括为：

```text
用户输入 / Cron / 队友事件 / 后台任务完成
                    │
                    v
          统一写入 messages 历史
                    │
                    v
       上下文整理 + 动态 system prompt
                    │
                    v
                  LLM
              ┌─────┴─────┐
              │有 tool_use│没有 tool_use
              v           v
      权限钩子、工具分发   Stop 钩子、记忆提取、结束本轮
              │
              v
       tool_result 写回 messages
              │
              └──────────> 下一轮 LLM
```

从职责上看，代码分成七层：

| 层次 | 目录/模块 | 主要职责 |
|---|---|---|
| 入口与兼容层 | `code.py`、`s15_runtime/api.py`、`cli.py` | 保留原单模块 API，启动交互式 CLI |
| 基础设施层 | `foundation/bootstrap.py` | 环境、客户端、常量、共享依赖、终端输入输出 |
| Agent 层 | `agent/` | system prompt、主 Agent 循环、一次性子代理 |
| 工具层 | `tools/` | 工具 schema、基础实现、适配器、动态注册 |
| 运行时层 | `runtime/` | Hook、恢复、压缩、后台任务、Cron |
| 协作层 | `collaboration/` | 文件邮箱、队友线程、计划审批、停机协议 |
| 工作区层 | `workspace/` | 持久任务图、任务认领、worktree 与 cwd 租约 |

`s15_runtime` 及各子目录下的 `__init__.py` 都是空文件，只负责把目录声明为标准 Python 包；实际公共符号由各功能模块的 `__all__` 和 `api.py` 汇总。

---

## 2. 启动过程与主调用链

推荐从仓库根目录运行：

```bash
python s15_integrated_harness/code-new/code.py
```

程序启动时依次发生：

1. `code.py` 把 `code-new` 加到 `sys.path`，导入 `s15_runtime.api`。
2. `api.py` 按固定顺序导入各功能模块。这个顺序很重要，因为导入期间会发生环境初始化、技能扫描、Hook 注册、信号/退出清理注册等副作用。
3. `bootstrap.py` 读取 `.env`，创建 Anthropic 客户端，读取模型配置，并动态加载 `s09_memory/code.py` 作为记忆运行时。
4. `prompting.py` 在导入时扫描 `<WORKDIR>/skills/*/SKILL.md`。
5. `basic.py` 注册 shell 子进程的 `atexit` 和 `SIGTERM` 清理。
6. `hooks.py` 注册默认 Hook。
7. 以脚本方式运行时，`code.py` 调用 `api.run_cli()`，最终进入 `cli.run_cli()`。
8. CLI 启动 Cron 服务和一个异步事件线程，然后主线程循环读取用户输入。

一次普通用户请求的调用链为：

```text
cli.run_cli
  -> UserPromptSubmit hooks
  -> history.append(user message)
  -> agent.loop.agent_loop
       -> 注入 Cron / 后台通知
       -> prepare_context
       -> update_context（记忆、MCP、队友）
       -> assemble_tool_pool
       -> call_llm
       -> 遍历 tool_use
            -> PreToolUse hooks
            -> 同步执行或转入后台线程
            -> PostToolUse hooks
            -> 生成 tool_result
       -> 继续循环，直到模型不再返回 tool_use
       -> Stop hooks
       -> 提取并整理长期记忆
       -> 释放已完成任务的 cwd 租约
  -> 更新 context
  -> 打印本轮 assistant 文本
```

异步事件线程 `async_event_loop()` 每秒检查三类事件：到期的 Cron、Lead 邮箱中的队友消息、已经结束但尚未投递的后台任务。只要其中任意一种存在，就在同一份 `history` 上自动发起一个 Agent 回合。`agent_lock` 保证交互回合和异步回合不会同时修改消息历史。

---

## 3. 入口与兼容层

### 3.1 `code.py`

这是轻量入口，同时承担与原单文件版 API 的兼容。

- `_PACKAGE_ROOT`：确保直接执行文件时可以导入相邻的 `s15_runtime` 包。
- `_CompatibilityModule`：把模块属性访问转发给 `s15_runtime.api`。
  - `__getattr__`：访问 `code.some_symbol` 时，去真正拥有该符号的功能模块读取。
  - `__setattr__`：测试或宿主修改 `code.client`、`code.MODEL` 等导出变量时，不只修改入口模块，而是同步修改符号所有者以及已经导入同名符号的模块。
  - `__dir__`：让 IDE、调试器和反射仍能看到兼容 API。
- 模块加载完后把当前模块实例的 `__class__` 换成 `_CompatibilityModule`。
- 只有 `__name__ == "__main__"` 时才启动 CLI；作为模块导入时只提供 API。

这层的价值是：内部代码已经拆包，但依赖原 `code.py` 顶层符号的测试、教学代码和 monkey patch 仍可工作。

### 3.2 `s15_runtime/api.py`

这是兼容符号表的中心。

- 按原单文件的功能顺序导入模块，以保留导入副作用顺序。
- 扫描每个模块的 `__all__`，建立 `_EXPORT_OWNERS: 符号名 -> 所属模块`。
- `get_symbol()` / `has_symbol()` / `exported_names()` 提供查询。
- `set_symbol()` 先修改真正的所有者，再修改所有已经通过 `from ... import name` 持有同名绑定的模块，避免打桩只改到一处。
- `run_cli()` 是对 `cli.run_cli()` 的稳定转发入口。

后导入模块若导出同名符号，会覆盖 `_EXPORT_OWNERS` 中的前一个所有者，因此新增公共符号时应避免重名。

### 3.3 `s15_runtime/cli.py`

实现交互式命令行宿主。

- 设置 `bootstrap.CLI_ACTIVE = True`，让后台线程输出时使用不破坏输入行的打印方式。
- `cron.start_runtime_services()` 只启动一次持久 Cron 加载与调度线程。
- 初始化：
  - `history`：完整会话消息；
  - `context`：记忆/MCP/队友等实时上下文；
  - `session_state["active_user_request"]`：异步事件回合需要沿用的权威用户目标。
- 启动 `async_event_loop` 守护线程。
- 主线程通过 `ConsoleBroker` 串行读取用户输入；空输入、`q`、`exit` 退出。
- 每次请求持有 `agent_lock`，触发提交 Hook，调用主循环，并只打印本回合新增的 assistant 文本。

---

## 4. 基础设施层

### `foundation/bootstrap.py`

该模块集中放置所有模块共享的环境与依赖，其他文件大量从这里导入标准库对象和运行时单例。

#### 环境与客户端

- `load_dotenv(override=True)`：用 `.env` 覆盖已有环境变量。
- 若设置了 `ANTHROPIC_BASE_URL`，删除 `ANTHROPIC_AUTH_TOKEN`，然后用自定义 base URL 创建 `Anthropic` 客户端。
- `MODEL_ID` 是必需环境变量；`FALLBACK_MODEL_ID` 可选。
- `WORKDIR = Path.cwd()`，所以运行时工作区取决于启动命令所在目录，而不是 `code.py` 所在目录。

主要路径：

| 变量 | 默认位置 | 用途 |
|---|---|---|
| `SKILLS_DIR` | `<WORKDIR>/skills` | 技能目录 |
| `TRANSCRIPT_DIR` | `<WORKDIR>/.transcripts` | 压缩前的完整对话备份 |
| `TOOL_RESULTS_DIR` | `<WORKDIR>/.task_outputs/tool-results` | 超大工具输出落盘 |

#### 记忆复用

`load_memory_runtime()` 通过文件路径动态导入仓库中的 `s09_memory/code.py`，再把当前 S15 的 `WORKDIR`、`.memory` 路径、Anthropic 客户端和模型注入进去。这样 S15 直接复用 S09 的记忆读取、相关记忆选择、提取和合并逻辑，而不是复制实现。

#### 关键限制常量

- 默认/升级输出上限：8,000 / 16,000 tokens。
- API 重试次数：3。
- 连续 529 切换备用模型阈值：2。
- 上下文粗略限制：50,000 个 JSON 字符，而非精确 token 数。
- 最近保留的完整旧工具结果：3 个。
- 单个输出落盘阈值：30,000 字符。

#### 终端并发

`ConsoleBroker` 用锁序列化普通输入和权限确认，防止两个线程同时读取 stdin。`terminal_print()` 在后台线程输出时尝试保存并重绘 readline 当前输入行；没有 readline 或非 CLI 模式则直接 `print`。

#### 平台注意

模块直接导入 Unix 的 `fcntl`，shell 进程管理也使用 `os.killpg`、`SIGKILL` 等 Unix 语义。因此虽然入口强调 IDE/Pylance 友好，这套运行时代码本身主要面向 Unix-like 环境；在原生 Windows Python 中导入 `fcntl` 会失败。

---

## 5. Agent 层

### 5.1 `agent/prompting.py`

负责技能发现和 system prompt 的实时组装。

#### 技能加载

- `_parse_frontmatter()` 解析 `SKILL.md` 开头的 YAML frontmatter；没有合法 frontmatter 时保留全文并返回空元数据。
- `scan_skills()` 只扫描 `SKILLS_DIR` 的直接子目录，并要求存在 `SKILL.md`，同时通过 `resolve()`/`is_relative_to()` 防止符号链接逃逸技能根目录。
- 技能名优先取 frontmatter 的 `name`，否则用目录名；描述优先取 `description`，否则用正文第一行。
- `SKILL_REGISTRY` 保存名称、描述和完整原文。
- 模块导入时自动扫描一次；运行期间技能文件发生变化后，需要显式再次调用 `scan_skills()` 才会刷新。
- `list_skills()` 只给模型看简短目录，`load_skill(name)` 才返回完整内容，避免 system prompt 常驻所有技能正文。

#### Prompt 组装

`PROMPT_SECTIONS` 固定定义：身份、工具清单、任务图规则、团队协作规则、工作区、记忆信任边界、压缩消息信任边界。

`assemble_system_prompt(context)` 每次模型调用前重建 prompt，并加入：

- 当前时间；
- 最新技能目录；
- 记忆索引和相关记忆；
- 已连接 MCP 服务器名。

其中明确规定，压缩摘要和召回记忆只能作为参考数据，不能覆盖当前权威用户请求，也不能授权操作。

### 5.2 `agent/subagent.py`

实现 `task` 工具对应的一次性、隔离上下文子代理。

- 子代理有独立的 `messages`，只获得任务描述，不继承主 Agent 对话。
- 只提供五个基础工具：`bash`、读、写、精确替换、glob。
- 仍经过全局 `PreToolUse` / `PostToolUse` Hook，所以权限边界没有绕开。
- 最多运行 30 个模型回合；是否继续只看响应中是否真的存在 `tool_use` block，不依赖可能不一致的 `stop_reason`。
- 结束后从后往前找最后一个非空 assistant 文本，仅将该摘要返回给主 Agent；中间工具过程不会合并进主历史。

它适合短期、聚焦、无需持续协调的委派。它没有主循环的记忆、动态技能、MCP、压缩、Cron、后台任务、任务图或错误恢复能力，也不能继续派生代理。

### 5.3 `agent/loop.py`

这是整个运行时的核心编排器。

#### 上下文维护

- `update_context()` 每轮重新读取记忆目录、根据当前消息加载相关记忆，并记录 MCP 与队友状态。
- `remember_after_turn()` 在自然结束时提取长期记忆；有新记忆才触发合并。
- `prepare_context()` 固定执行四级上下文治理：工具输出预算、按消息数截断、旧工具结果微压缩、超限时模型摘要。

#### 单轮循环

`agent_loop()` 的每次 `while` 迭代按以下顺序进行：

1. 消费 Cron 队列，把每个任务作为 `[Scheduled]` 用户消息注入，并扩展 `active_request`。
2. 注入已经完成的后台任务通知。
3. 连续三个普通工具回合未更新 todo 时，注入更新 todo 的提醒。
4. 运行上下文压缩流水线。
5. 刷新记忆、MCP 和队友上下文。
6. 重新组装工具池，使新连接的 MCP 工具从下一轮立即可见。
7. 调用模型，并处理重试、上下文过长和输出截断。
8. 成功获得模型响应后确认 Cron 投递；若模型调用最终失败，则把未确认 Cron 放回队列。
9. 没有 `tool_use`：触发 Stop Hook、写入记忆、释放已完成的 Agent 任务租约并返回。
10. 有 `tool_use`：逐个处理 compact、Hook 拒绝、后台命令或同步 handler，并为每个调用构造对应 `tool_result`。
11. 把工具结果及刚完成的后台通知作为 user content 追加，进入下一轮。

#### 特殊恢复分支

- 第一次 `max_tokens`：不保存被截断响应，改用 16,000 tokens 重试。
- 升级后仍被截断：保存部分响应，最多追加两次 continuation prompt。
- prompt 太长：只允许一次 reactive compaction 后重试。
- 其他最终错误：在历史中写入 `[Error]` assistant 消息后结束。

#### `compact` 工具

`compact` 没有普通 handler。主循环先为它生成一个完成结果，等本轮所有工具结果写回后，再调用 `compact_history()` 把历史替换成压缩交接消息。这样不会留下没有匹配结果的 `tool_use`。

#### 异步事件循环

`async_event_loop()` 是 CLI 的第二个守护线程。它用同一个 `agent_lock` 串行处理 Cron、团队事件和后台完成通知，并把结果继续写入同一份 history。因为该回合不在主线程，权限 Hook 对需要交互确认的 bash 和 MCP 调用会 fail closed。

---

## 6. 工具层

### 6.1 `tools/schemas.py`

集中定义模型可见的 26 个内置工具 schema 以及工具名到 Python handler 的映射。

| 类别 | 工具 |
|---|---|
| 文件与命令 | `bash`、`read_file`、`write_file`、`edit_file`、`glob` |
| 本轮规划与委派 | `todo_write`、`task`、`load_skill`、`compact` |
| 持久任务图 | `create_task`、`update_task`、`list_tasks`、`get_task`、`claim_task`、`complete_task` |
| 定时任务 | `schedule_cron`、`list_crons`、`cancel_cron` |
| 持久团队 | `spawn_teammate`、`list_teammates`、`send_message`、`request_shutdown`、`request_plan`、`review_plan` |
| 隔离工作区 | `create_worktree` |
| 外部能力 | `connect_mcp` |

`compact` 出现在 schema 中但不在 `BUILTIN_HANDLERS` 中，因为它由主循环特殊处理。`remove_worktree()` 也故意不暴露给模型，删除操作保留给宿主或用户。

### 6.2 `tools/basic.py`

实现基础文件/命令工具和轻量 todo。

#### 路径安全

`safe_path(path, cwd)` 把相对路径解析到当前有效工作目录，拒绝解析后逃逸该目录的路径。对主 Agent 来说，有任务时有效 cwd 可以是任务绑定 worktree；无任务时是 `WORKDIR`。队友也通过自己的任务分配得到 cwd。

#### Shell 生命周期

- `_run_bash_process()` 使用 `shell=True` 启动命令，但给每个命令创建独立进程组。
- 命令最多运行 120 秒，stdout 和 stderr 合并返回，最多暴露 50,000 字符。
- 无论正常结束、超时还是异常，`finally` 都尝试向原进程组发 `SIGTERM`，再发 `SIGKILL`。
- 全局集合记录所有 shell 子进程，进程退出或收到 SIGTERM 时统一清理。
- 返回值会保留非零退出码，格式为 `Error: command exited with status ...`。

进程组清理只能控制仍处于原组的进程；自行创建新 session 的后代可能逃逸。

#### 文件与 Glob

- `run_read()` 支持按行 `offset`/`limit`。
- `run_write()` 自动创建父目录并整体覆盖文件。
- `run_edit()` 只替换第一次出现的完全匹配文本。
- `run_glob()` 使用当前 cwd 为 root，并再次检查结果不逃逸。

#### 分发与 Todo

- `call_tool_handler()` 统一捕获 handler 异常并转换为字符串错误，防止工具异常击穿 Agent 循环。
- `todo_write` 是内存中的单份会话清单，每次调用整体替换 `CURRENT_TODOS`；它不是 `.tasks` 中的持久任务系统。
- `_normalize_todos()` 兼容真正的 list、JSON 字符串和 Python 字面量字符串，并检查字段和状态。

### 6.3 `tools/handlers.py`

这是 schema 与领域模块之间的薄适配层：

- 把任务系统返回的对象/异常转换成适合模型阅读的文本；
- 固定 Lead 的任务 owner 为 `agent`；
- 把协作工具转发到消息总线和队友线程；
- 把 worktree、MCP 连接暴露为模型工具；
- 负责少量彩色状态日志，不承载核心业务规则。

### 6.4 `tools/registry.py`

`assemble_tool_pool()` 每个 Agent 回合动态合并内置工具和所有已连接 MCP 工具。

- MCP 工具命名为 `mcp__{normalized_server}__{normalized_tool}`。
- 限制最终名称不超过 64 字符。
- 检查标准化后的名称冲突，避免两个原始名称映射为同一个模型工具名。
- 验证 MCP input schema 必须是 object。
- 用闭包为每个 MCP 工具生成 handler，并正确冻结 client/tool 名，避免循环变量晚绑定错误。
- 把宿主策略写入 `hooks.mcp_tool_policies`，权限判断使用宿主配置而不是服务端自报的 annotations。

---

## 7. 运行时层

### 7.1 `runtime/hooks.py`

提供四类同步 Hook：

| 事件 | 触发时机 | 默认 Hook |
|---|---|---|
| `UserPromptSubmit` | CLI 收到用户输入后 | 打印工作区日志 |
| `PreToolUse` | 工具 handler 之前 | 权限检查、工具日志 |
| `PostToolUse` | 工具执行之后 | 超大输出警告 |
| `Stop` | 主 Agent 自然停止时 | 统计工具结果数 |

`trigger_hooks()` 按注册顺序执行，一旦某个 callback 返回非 `None` 就立即返回。因此权限 Hook 拒绝时，后面的日志 Hook 不再执行。

权限策略：

- bash 命中简单字符串 deny list 时直接拒绝；其余每一条 shell 命令都询问用户。
- 后台/异步线程不能发起交互确认，因此需要确认的 shell 和 MCP 调用直接拒绝。
- 文件工具必须落在主 `WORKDIR` 内；实际 handler 随后还会按当前任务 cwd 做更严格的 containment 检查。
- MCP 权限由精确的宿主策略表决定；非 `allow` 工具默认要求确认。

deny list 只是演示型字符串检查，并不是完整 shell 解析或安全沙箱；worktree 也只是 cwd 隔离。

### 7.2 `runtime/recovery.py`

负责 LLM API 错误恢复。

- `RecoveryState` 跨同一次 `agent_loop` 保存 token 升级、continuation 次数、连续 529、reactive compact 是否已尝试和当前模型。
- 429：指数退避加 0–25% jitter，最多三次。
- 529/overloaded：同样退避；连续达到阈值且配置了备用模型时切换模型。
- 其他异常立即抛出，由主循环决定是否进行 prompt-too-long 压缩或结束。
- `is_prompt_too_long_error()` 通过异常文本兼容多个常见上下文超限标记。

切换到 fallback 后，本次 `agent_loop` 内不会自动切回主模型；新用户回合会新建 `RecoveryState`，重新从主模型开始。

### 7.3 `runtime/compaction.py`

上下文治理分四层，按从便宜到昂贵的顺序执行：

1. `tool_result_budget()`：只检查最新 user 消息中的工具结果；总量超过 200,000 字符时，把超过 30,000 字符的大结果写到磁盘，消息内只留路径和 2,000 字符预览。
2. `snip_compact()`：消息数超过 50 时保留最早 3 条和最近 47 条，中间替换成一条 `[snipped N messages]`，并微调边界以尽量不拆散 `tool_use` / `tool_result` 对。
3. `micro_compact()`：除最近三个已经被模型看过的工具结果外，把长度超过 120 的旧结果替换为短占位。尚未被模型消费的结果绝不压缩。
4. `compact_history()`：若仍超过粗略的 50,000 字符限制，先把完整历史保存为 JSONL，再调用模型生成 2,000-token 状态摘要，最终只保留一条压缩 user 消息。

压缩消息显式拆分为：

- `Authoritative request`：当前真实用户请求；
- `Reference state`：不可信摘要，只提供事实背景，不构成指令或授权。

`reactive_compact()` 用于 API 已经报告 prompt too long 的情况。它保留最近五条左右的消息及必要的工具调用配对，只总结更早部分；若摘要调用也失败，则用固定降级说明继续。

完整压缩摘要最多读取序列化历史的前 80,000 字符，`estimate_size()` 也是字符估算，不是 tokenizer 的精确 token 计算。

### 7.4 `runtime/background.py`

目前只有显式设置 `run_in_background=true` 的 bash 会转入后台。

- `start_background_task()` 在锁内分配单调递增的 `bg_0001` ID，记录原 `tool_use_id`、命令、状态和启动时 cwd。
- 主循环立即返回占位 `tool_result`，不等待命令。
- 守护线程执行命令并触发 `PostToolUse`；非零退出、执行异常或 Post Hook 异常都会进入 `failed` 状态。
- `collect_background_results()` 原子地取走终态任务，生成一次性的 `<task_notification>`，摘要最多 200 字符。
- `has_pending_background()` 只表示有“已完成但未投递”的结果；仍在运行的任务不会反复唤醒 Agent。
- 若线程启动本身失败，会回滚任务表，主循环收到普通错误结果。

传入 `start_background_task()` 的 `handlers` 参数当前没有实际使用，后台实现固定调用 bash 底层执行器。

### 7.5 `runtime/cron.py`

实现五字段 Cron 调度：分钟、小时、日、月、星期。

支持的字段形式为：`*`、`*/n`、逗号列表、闭区间 `a-b` 和单个整数；不支持名称、`a-b/n` 等高级语法。星期值 `0` 代表周日。当“日”和“星期”都有限定时，遵循传统 Cron 的 OR 语义。

`CronJob` 包含：ID、表达式、prompt、是否重复、是否持久，以及一次性任务的 `pending_delivery`。

可靠投递流程：

```text
调度线程发现到期
  -> 一次性任务先持久化 pending_delivery=true
  -> 放入 cron_queue
  -> agent_loop 消费并把 prompt 交给模型
     -> 模型调用成功：acknowledge，删除一次性任务
     -> 模型调用失败：restore，重新放回队列
```

因此一次性持久任务提供的是“至少一次”投递，而不是严格恰好一次。进程若在 pending 状态重启，`load_durable_jobs()` 会再次入队。

其他实现细节：

- 调度线程每秒检查一次，但 `_last_fired` 保证同一任务每分钟只入队一次。
- 持久任务保存在 `<WORKDIR>/.scheduled_tasks.json`，通过临时文件加 `os.replace` 原子替换。
- 加载文件时任何异常都会被吞掉，损坏文件不会阻止程序启动，但也不会报告详细诊断。
- `start_runtime_services()` 有 once guard，避免重复加载和重复启动线程。
- Cron ID 使用六位随机数生成，没有显式碰撞重试；极低概率下同 ID 会覆盖内存字典中的旧任务。

---

## 8. 协作层

S15 明确区分两种委派：

| 能力 | `task` 一次性子代理 | `spawn_teammate` 持久队友 |
|---|---|---|
| 生命周期 | 最多 30 个模型回合，返回摘要即销毁 | 独立守护线程，可长期 IDLE |
| 上下文 | 仅任务描述 | 保存自身消息历史并持续接收消息 |
| 协作 | 无 | 文件邮箱、计划审批、停机协议、自动认领 |
| 任务图 | 不接入 | 可绑定、认领、完成 Task |
| 工作目录 | 主工作区 | 必须来自当前 Task，可绑定 worktree |

### 8.1 `collaboration/messaging.py`

#### MessageBus

每个收件人使用 `<WORKDIR>/.mailboxes/{agent}.jsonl`：

- Agent 名只能包含 1–64 个字母、数字、下划线或短横线。
- `send()` 追加 JSON 行并通过 `Condition` 唤醒等待线程。
- `read_inbox()` 读取全部消息后删除邮箱文件，语义是一次性消费。
- `wait_for_messages()` 支持阻塞等待和超时，用于队友 IDLE 状态。
- 路径会 `resolve()` 后检查仍在 `.mailboxes` 根目录内。

锁和条件变量只协调当前进程中的线程；邮箱虽落盘，但没有跨进程文件锁或确认日志，不应视为完整的持久消息队列。

#### 协议状态

`ProtocolState` 记录计划审批或停机请求的 request ID、发送方、目标、状态、payload，以及可选的任务 ID 和 assignment version。

全局状态包括：

- `active_teammates`：队友及其 working/idle/waiting_approval/stopping 状态；
- `plan_gates`：`not_required`、`required`、`pending`、`approved`、`rejected`；
- `plan_request_ids`：每个队友当前有效的计划请求；
- `pending_requests`：所有协议请求状态。

`match_response()` 校验响应类型、request ID、双方身份和 pending 状态，防止伪造、错配或重复响应。`consume_lead_inbox()` 会先路由协议响应，再把原消息交给 Lead Agent 作为团队事件。

### 8.2 `collaboration/teammates.py`

这是持久队友的状态机和协议实现。

#### 创建与初始任务

`spawn_teammate_thread()`：

- 校验名称并拒绝保留名 `lead`、`agent`，名称比较不区分大小写；
- 初始化队友状态、计划门禁和 assignment version；
- 若给定 `task_id`，必须先成功认领任务才启动线程，失败则回滚状态；
- 给队友建立独立 system prompt、消息历史、工具 schema 和绑定了队友身份的 handler。

队友可使用基础文件工具、发送消息、提交计划、查看/认领/完成任务。它不能创建任务依赖、创建 worktree、派生其他队友或使用主 Agent 的动态 MCP 工具。

#### cwd 与任务约束

队友没有当前 Task 时，所有工作区工具都会返回“先认领 Task”。有任务后，读写、glob 和 shell 都以任务分配的 cwd 执行。这样队友不会因 prompt 自报目录而切换位置，目录选择来自运行时任务状态。

#### 计划门禁

当计划状态为 `required`、`pending` 或 `rejected` 时，队友的 `bash`、`write_file`、`edit_file` 被 `_run_teammate_tool()` 阻止；只读工具和沟通工具仍可用。任务完成操作还会在任务系统中再次检查门禁。

计划流程为：

```text
队友 submit_plan
  -> 创建绑定当前 task_id + assignment_version 的请求
  -> plan gate = pending，队友 waiting_approval
  -> Lead 收到 plan_approval_request
  -> Lead review_plan(approve/reject)
  -> 响应写入队友邮箱
  -> apply_plan_response 再次核对请求、任务和版本
  -> 更新 gate，队友继续工作
```

assignment version 在认领和释放任务时递增，因此上一任务的旧审批不能授权下一任务。

#### 工作循环与 IDLE

队友每次调用模型前先清空邮箱，保证消息和停机请求不会长期排在连续工具调用之后。模型返回工具调用时执行并继续；返回纯文本时：

- 把最终文本作为 `result` 发给 Lead（计划仍 pending 时不发送）；
- 释放已经完成的任务租约；
- 进入 `idle` 并发出 `idle_notification`；
- 先等待邮箱，超时后扫描任务板；
- 若发现依赖已完成、无人拥有且 worktree 可用的任务，则原子认领一个并重新进入工作。

队友一次只能持有一个任务；必须完成并在回合边界释放后，才可自动认领下一个。

#### 停机与异常清理

Lead 的 `request_shutdown` 生成请求并写入队友邮箱。队友校验后回复 `shutdown_response` 并退出。无论正常、模型异常还是工具分发异常，线程 `finally` 都会：

- 把未完成任务恢复为 pending、清空 owner；
- 释放 assignment；
- 递增版本使旧审批失效；
- 清理队友状态。

模型或线程异常会尽量作为 `error` 消息发给 Lead。

由于队友运行在非主线程，默认权限策略会拒绝其所有需要交互确认的 bash；普通读写仍可在 Hook 与 cwd 双重检查通过后执行。这是“异步回合不能争用 stdin”的安全取舍。

---

## 9. 工作区层

### 9.1 `workspace/tasks.py`

实现文件持久化的依赖任务图。每个任务保存在 `<WORKDIR>/.tasks/task_<8位hex>.json`。

`Task` 字段：

| 字段 | 含义 |
|---|---|
| `id` | 运行时随机生成的稳定 ID |
| `subject` / `description` | 任务标题和详情 |
| `status` | `pending` / `in_progress` / `completed` |
| `owner` | `agent` 或具体队友名 |
| `blockedBy` | 前置任务 ID 列表 |
| `worktree` | 可选的 worktree 名 |

#### 锁与持久化

- `task_lock`：进程内可重入锁。
- `task_store_lock()`：在此基础上用 `.tasks/.lock` 和 `fcntl.flock` 提供跨进程互斥，并通过线程局部 depth 支持同线程嵌套。
- 新任务用文件独占创建避免 ID 碰撞。
- 更新任务先写同目录临时文件，再用 `os.replace` 原子替换。
- Task ID 和最终路径都经过严格校验，防止路径穿越。

#### 依赖图

- 依赖只能在任务仍 pending 且无人拥有时追加。
- 先创建所有节点，再使用运行时返回的真实 ID 添加边。
- 自动去重依赖，拒绝自依赖、缺失节点和传递环。
- `can_start()` 要求所有 blocker 文件存在且状态为 completed。

只有 Lead 的工具集能调用 `update_task`；队友只能认领和完成，避免并发工作期间修改图结构。

#### 认领与完成

`claim_task()` 在持久锁内原子检查：任务 pending、无 owner、该 owner 没有其他租约或遗留 in-progress 任务、依赖完成、绑定 worktree 有效。成功后同时写入任务状态和内存中的 `teammate_assignments[owner] = {task_id, cwd}`。

`complete_task()` 要求调用者就是 owner，且计划门禁不是 required/pending/rejected。完成后列出因此解除阻塞的任务，但 cwd 租约不会立刻释放，要等当前模型回合结束，保证同一响应中的后续工具仍在原任务目录执行。

任务文件是持久的，但 `teammate_assignments` 和审批状态是内存数据。正常线程退出会回收未完成任务；进程崩溃则可能留下 in-progress/owner，需要宿主人工恢复。

### 9.2 `workspace/worktrees.py`

把 pending Task 绑定到 `<WORKDIR>/.worktrees/{name}` 和分支 `wt/{name}`。

#### 创建

`create_worktree()` 在运行 Git 前检查：

- 名称合法且不含 `..`；
- 任务存在、pending、无 owner、尚未绑定 worktree；
- 名称未被其他任务占用，目标目录不存在；
- `WORKDIR` 确实是 Git 仓库根；
- 分支名合法且不存在；
- Git worktree registry 中没有目标路径。

随后使用参数数组而非 shell 拼接执行 `git worktree add -b wt/<name> <path> HEAD`。若 Git 报错，会重新检查路径、registry 和分支；如果留下部分产物，不自动删除，而是返回明确的人工恢复提示。成功创建后才把 worktree 名写入 Task。若“Git 成功、任务绑定失败”，同样保留 Git 数据供手动恢复。

#### cwd 租约

- `task_worktree_cwd()` 对未绑定任务返回主 `WORKDIR`；绑定任务则验证路径确实登记在 Git registry、目录存在、分支完全匹配。
- `assignment_cwd()` 可以从持久任务重建丢失的内存 assignment，但会检查 owner、状态、任务和 cwd 没有漂移。
- `release_completed_assignment()` 只在模型回合边界释放已完成任务的 cwd，并重置计划门禁。
- `release_teammate_assignment()` 用于线程异常/退出，把未完成任务退回 pending。

#### 删除

`remove_worktree()` 是宿主 helper，没有暴露给模型。它要求：

- worktree 已登记、绑定任务都 completed；
- 没有 owner 仍租用该 cwd；
- 没有后台命令仍在该 cwd 运行；
- Git 状态可读取；若有未提交或 ignored 内容，默认拒绝删除。

只有显式 `discard_changes=True` 才使用 `git worktree remove --force`。无论如何保留 `wt/<name>` 分支。删除成功后再解除 Task 绑定；若解绑失败会报告部分成功，避免静默丢失状态。

worktree 只改变工具默认 cwd，目的是减少并行编辑冲突，不是安全沙箱。

---

## 10. MCP 集成层

### `integrations/mcp.py`

这是教学用的进程内 MCP 模拟器，不是真正的网络 MCP transport。

- `MCPClient.register()` 检查工具名非空、服务内不重复、每个 schema 都有 handler。
- `call_tool()` 把异常转换为 `MCP error` 文本。
- 内置两个 mock server：
  - `docs`：`search`、`get_version`；
  - `deploy`：`trigger`、`status`。
- `connect_mcp()` 只负责实例化服务器并加入 `mcp_clients`；主循环下一轮重新组装工具池后，发现的工具才会暴露给模型。
- `normalize_mcp_name()` 把模型工具名不允许的字符替换为下划线。

权限由宿主 `MCP_HOST_POLICY` 决定：文档查询、版本查询和部署状态可直接调用，部署触发需要用户确认。服务端 schema 中的 `readOnlyHint` / `destructiveHint` 只是元数据，不作为授权来源；未知 MCP 工具默认确认。

---

## 11. 持久数据与内存状态

| 状态 | 位置 | 生命周期/说明 |
|---|---|---|
| 对话 history | 进程内；压缩时备份到 `.transcripts` | CLI 会话级 |
| Todo | `CURRENT_TODOS` 内存变量 | 进程级，整体替换 |
| Task | `.tasks/task_*.json` | 跨会话持久化 |
| Task 文件锁 | `.tasks/.lock` | 跨线程/进程互斥 |
| Assignment/cwd 租约 | `teammate_assignments` | 进程内，可部分由 Task 重建 |
| 队友消息 | `.mailboxes/*.jsonl` | 读取即删除，主要用于同进程线程通信 |
| 队友/审批协议 | 多个内存字典 | 进程级 |
| Cron | `.scheduled_tasks.json`（durable=true） | 可跨重启，one-shot 至少一次投递 |
| 后台任务 | 内存字典 | 进程级，完成通知只投递一次 |
| 超大工具输出 | `.task_outputs/tool-results/*.txt` | 压缩后保留完整内容 |
| 长期记忆 | `.memory` | 由 S09 runtime 管理 |
| Skills | `skills/*/SKILL.md` | 启动扫描，正文按需加载 |
| Worktree | `.worktrees/<name>` + Git branch | Git 管理，删除保留分支 |
| MCP 连接 | `mcp_clients` | 进程级 |

---

## 12. 关键设计原则与边界

1. **所有事件回到同一对话。** 用户输入、定时 prompt、队友事件、后台结果最终都进入 `messages`，因此只有一套推理和工具反馈闭环。
2. **能力动态、权限宿主化。** MCP 工具可以动态加入，但是否允许执行由本地主机的 Hook 策略决定。
3. **轻计划与持久任务分离。** `todo_write` 防止当前 Agent 漂移；Task 图负责跨回合、依赖和团队所有权；`task` 工具则表示一次性子代理，三者不要混淆。
4. **任务身份决定 cwd。** Agent 不能仅靠 prompt 改变工作目录；运行时根据 owner 当前认领的 Task 选择主工作区或 worktree。
5. **完成与释放分离。** Task 可以在一次模型响应中先完成，但 cwd 要到回合边界才释放，避免同批后续工具突然切换目录。
6. **审批绑定具体工作版本。** 计划审批同时绑定 teammate、task ID 和 assignment version，旧审批不会跨任务复用。
7. **异步执行 fail closed。** 后台线程无法安全询问 stdin，凡需交互批准的能力直接拒绝。
8. **压缩保留权威边界。** 摘要只是事实参考，当前请求被单独保留为权威指令，降低摘要中的提示注入风险。
9. **部分失败优先保留数据。** worktree 创建/删除遇到半成功时不自动做破坏性清理，而是报告残留物供人工核对。
10. **这不是安全沙箱。** 简单 deny list、路径检查、cwd/worktree 和进程组提供的是演示型边界，不等同于容器、系统调用隔离或完善的命令策略引擎。

总体上，`code-new` 的主要价值不在任何单一能力，而在于展示这些能力如何共享一条稳定的 Agent 主循环，同时把状态、授权、异步事件和并发工作目录的边界拆到可独立理解的模块中。
