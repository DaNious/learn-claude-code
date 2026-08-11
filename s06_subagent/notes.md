# s06 Subagent 学习笔记

## 这一节在解决什么问题

这一节要解决的是：当父 agent 处理复杂任务时，会把大量中间过程塞进同一个 `messages[]` 里，比如查文件、跑命令、反复调用工具。这样上下文会越来越臃肿，最后主任务反而容易被淹没。

`s06` 的解法是新增一个 `task` 工具。父 agent 遇到复杂子问题时，不自己在当前上下文里硬做，而是启动一个 subagent。这个 subagent 拿到一套全新的 `messages[]`，自己完成子任务，最后只把简短结论返回给父 agent。这样父 agent 的上下文就保持干净。

一句话概括：

`task` 不是直接做事的工具，而是“启动一个独立子 agent 去做事”的工具。

---

## 先从整体结构理解 `code.py`

最适合按下面这个顺序理解：

1. 程序入口在哪里
2. 父 agent 的主循环怎么工作
3. 工具系统怎么 dispatch
4. subagent 是怎么被创建出来的
5. subagent 和父 agent 的上下文为什么是隔离的

---

## 1. 程序入口

程序入口在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:363) 的 `if __name__ == "__main__":`。

它做的事情很直接：

1. 打印欢迎语
2. 进入一个命令行循环
3. 等用户输入问题
4. 把用户输入追加到 `history`
5. 调用 `agent_loop(history)`
6. 把最后一轮 assistant 返回的文本打印出来

这里的 `history` 就是父 agent 的对话历史，也就是父 agent 的 `messages[]`。

---

## 2. 初始化部分

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:43) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:62)，代码完成了这些初始化：

- 用 `load_dotenv()` 加载环境变量
- 初始化 `Anthropic` 客户端
- 读取 `MODEL_ID`
- 设置工作目录 `WORKDIR`
- 定义父 agent 的 `SYSTEM`
- 定义子 agent 的 `SUB_SYSTEM`

其中两套 system prompt 很关键：

- `SYSTEM`：告诉父 agent，复杂子问题可以使用 `task`
- `SUB_SYSTEM`：告诉 subagent，只完成当前任务并返回简洁总结，不要继续委派

这一步决定了父子 agent 的职责边界。

---

## 3. 基础工具实现

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:69) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:155)，定义了所有基础工具对应的 Python 函数。

### `safe_path(p)`

作用：把相对路径转成绝对路径，并确保它还在工作目录内。

意义：避免 agent 访问工作区外的文件。

### `run_bash(command)`

作用：执行 shell 命令。

特点：

- 在 `WORKDIR` 下执行
- 最长 120 秒
- 返回 stdout + stderr
- 输出太长会截断到 50000 字符

### `run_read(path, limit=None)`

作用：读取文件内容。

特点：

- 会经过 `safe_path()` 检查
- 支持按行数限制输出

### `run_write(path, content)`

作用：写文件。

特点：

- 如果父目录不存在会自动创建

### `run_edit(path, old_text, new_text)`

作用：在文件里做一次精确替换。

特点：

- 只替换第一次出现的匹配内容
- 如果找不到旧文本会返回错误

### `run_glob(pattern)`

作用：按 glob 模式找文件。

### `_normalize_todos()` 和 `run_todo_write()`

作用：清洗 todo 数据并打印任务列表。

这部分是从前几节延续来的功能，不是 `s06` 的重点，但父 agent 仍然可以用它。

---

## 4. 父 agent 的工具注册机制

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:157) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:175)，代码定义了：

- `TOOLS`：告诉模型“你有哪些工具可以调用”
- `TOOL_HANDLERS`：告诉 Python“每个工具名对应哪个函数”

父 agent 默认有这些工具：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`
- `todo_write`

这一层很重要，因为后面的主循环根本不关心某个工具的内部实现，它只按名字做分发。

---

## 5. s06 新增的核心：Subagent

`s06` 最关键的新增逻辑在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:182) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:257)。

### `SUB_TOOLS`

subagent 只有这些工具：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`

注意：没有 `task`。

这表示 subagent 不能继续创建下一层 subagent，避免递归失控。

### `SUB_HANDLERS`

这是 subagent 的工具分发表，对应上面那几个基础函数。

### `extract_text(content)`

作用：从模型返回的 content block 里提取纯文本。

因为 Anthropic 的响应内容可能不是简单字符串，而是一个 block 列表，所以要专门做一次抽取。

### `spawn_subagent(description)`

这是整个文件最核心的函数。

它做的事可以拆成 5 步：

1. 打印 `[Subagent spawned]`
2. 创建一套全新的 `messages = [{"role": "user", "content": description}]`
3. 用 `SUB_SYSTEM + SUB_TOOLS` 启动一个子循环
4. 让 subagent 自己反复调用工具完成任务
5. 最后只提取最终文本结论并返回给父 agent

其中最关键的一行是：

```python
messages = [{"role": "user", "content": description}]
```

这说明 subagent 不继承父 agent 的全部历史，只拿到任务描述本身。

这就是“上下文隔离”的核心。

---

## 6. `task` 工具是怎么接进父 agent 的

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:251) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:257)，代码做了两件事：

1. 把 `task` 加进父 agent 的 `TOOLS`
2. 把 `task` 对应到 `spawn_subagent`

也就是：

```python
TOOL_HANDLERS["task"] = spawn_subagent
```

这意味着对父 agent 来说，subagent 不是一个特殊分支，而只是“又多了一个工具”。

模型如果决定调用 `task`，主循环会像处理别的工具一样去执行它。

---

## 7. Hook 系统

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:264) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:308)，是 hook 系统。

可注册的事件有：

- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`

这几个默认 hook 的作用分别是：

### `permission_hook`

在工具执行前检查 bash 命令是否命中 deny list。

### `log_hook`

在工具执行前打印日志。

### `context_inject_hook`

在用户提交 prompt 时打印当前工作目录。

### `summary_hook`

在主循环结束时统计这次会话用了多少次工具。

最值得注意的是：

subagent 在执行工具时，也会调用 `trigger_hooks("PreToolUse", block)`。

这意味着：

- 上下文虽然隔离了
- 但安全规则没有被绕过

---

## 8. 父 agent 的主循环 `agent_loop()`

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:315) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:360)，是父 agent 的执行引擎。

它的运行方式非常重要，可以记成一个固定模板：

1. 调模型
2. 看模型是要输出文本还是要调工具
3. 如果调工具，就执行工具
4. 把工具结果作为新的消息塞回去
5. 再继续下一轮

更具体一点：

### 第一步：todo 提醒

如果连续 3 轮没更新 todo，就自动追加一个提醒消息：

```python
{"role": "user", "content": "<reminder>Update your todos.</reminder>"}
```

### 第二步：请求模型

调用：

```python
client.messages.create(
    model=MODEL,
    system=SYSTEM,
    messages=messages,
    tools=TOOLS,
    max_tokens=8000,
)
```

### 第三步：如果不是工具调用

如果 `response.stop_reason != "tool_use"`，说明模型这轮直接给出了最终文本回答。

这时：

- 触发 `Stop` hook
- 没有额外要求就返回，主循环结束

### 第四步：如果是工具调用

遍历 `response.content` 里的每个 `tool_use` block。

对每个工具调用：

1. 先跑 `PreToolUse`
2. 找 handler
3. 执行 handler
4. 跑 `PostToolUse`
5. 把结果封装成 `tool_result`

最后把整组 `tool_result` 作为一条新的 user message 追加回 `messages`。

这意味着模型下一轮能“看到自己刚刚调工具得到的结果”，然后继续决定下一步。

---

## 9. 为什么说 s06 的设计很巧

这一节最值得学习的设计点有 4 个：

1. 没有改坏原来的主循环，只是新增了一个 `task` 工具
2. `task` 的实现不是直接完成任务，而是启动一个小型 agent_loop
3. subagent 使用全新 `messages[]`，所以中间过程不会污染父上下文
4. subagent 最终只返回摘要，不返回完整内部历史

所以它的核心思想不是“多线程”或者“并发”，而是：

把复杂任务拆出去，让中间推理留在子上下文中。

---

## 示例：用 subtask 总结 `agents/` 目录下的文件

假设用户输入：

```text
Use a subtask to summarize files under the agents/ directory
```

下面按程序真实流转顺序模拟一次。

---

## 第 1 步：主程序收到用户输入

在 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:368) 到 [`code.py`](C:\XS\CodesToGithub\learn-claude-code\s06_subagent\code.py:377)：

1. `input()` 读到用户输入
2. 触发 `UserPromptSubmit` hook
3. 把用户输入加到 `history`
4. 调用 `agent_loop(history)`

此时父 agent 的 `messages` 大概是：

```python
[
  {"role": "user", "content": "Use a subtask to summarize files under the agents/ directory"}
]
```

---

## 第 2 步：父 agent 第一次调用模型

父 agent 在 `agent_loop()` 里调用模型，并把这些东西交给模型：

- `SYSTEM`
- 当前 `messages`
- 所有 `TOOLS`

因为 prompt 里明确要求“Use a subtask”，模型大概率会选择调用 `task`。

所以这轮响应通常不是直接文本，而是一个 `tool_use` block，类似：

```python
[
  ToolUseBlock(
    name="task",
    input={
      "description": "Inspect files under agents/ and summarize what each one does."
    }
  )
]
```

---

## 第 3 步：父 agent dispatch 到 `task`

主循环遍历工具调用时，会做这几件事：

1. 执行 `PreToolUse` hook
2. 在 `TOOL_HANDLERS` 中找到 `task`
3. 对应到 `spawn_subagent(description)`
4. 真正启动 subagent

也就是说，父 agent 并不知道自己在处理一个多复杂的机制，它只是像执行普通工具一样执行了 `task`。

---

## 第 4 步：subagent 创建自己的全新上下文

进入 `spawn_subagent()` 后，最关键的一行是：

```python
messages = [{"role": "user", "content": description}]
```

这表示 subagent 的初始上下文只有任务描述，不包含父 agent 的完整历史。

所以子 agent 的起点大概是：

```python
[
  {"role": "user", "content": "Inspect files under agents/ and summarize what each one does."}
]
```

这一步就是上下文隔离的本质。

---

## 第 5 步：subagent 先找 `agents/` 目录下有哪些文件

subagent 拿着：

- `SUB_SYSTEM`
- `SUB_TOOLS`
- 自己独立的 `messages`

开始进入自己的 while 循环。

为了完成“总结 `agents/` 目录下的文件”这个任务，它第一步通常会先找文件，比如调用：

```python
glob(pattern="agents/**/*.py")
```

或者用 bash 查目录。

假设这里它使用 `glob`。

---

## 第 6 步：subagent 执行 `glob`

子循环处理这个工具调用时，会：

1. 先跑 `PreToolUse` hook
2. 找到 `SUB_HANDLERS["glob"]`
3. 执行 `run_glob()`
4. 得到文件列表
5. 把结果包装成 `tool_result`
6. 追加回 subagent 自己的 `messages`

假设返回结果是：

```text
agents/base.py
agents/reviewer.py
agents/builder.py
```

此时 subagent 的 `messages` 会增长成类似这样：

```python
[
  {"role": "user", "content": "Inspect files under agents/ and summarize what each one does."},
  {"role": "assistant", "content": [ToolUseBlock(name="glob", input={"pattern": "agents/**/*.py"})]},
  {"role": "user", "content": [
      {"type": "tool_result", "content": "agents/base.py\nagents/reviewer.py\nagents/builder.py"}
  ]}
]
```

这一步非常像父 agent 的处理方式，只不过是在子上下文里进行。

---

## 第 7 步：subagent 继续读取每个文件

下一轮模型看到文件列表后，就会继续调用 `read_file` 来读每个文件内容。

例如：

```python
read_file(path="agents/base.py")
read_file(path="agents/reviewer.py")
read_file(path="agents/builder.py")
```

对应的 Python 函数 `run_read()` 会被依次调用，文件内容再被塞回 subagent 的 `messages`。

这时 subagent 的历史会越来越长，但这些中间过程只存在于 subagent 这边，父 agent 完全看不到。

这正是设计目的：

- 子 agent 可以处理复杂中间步骤
- 父 agent 只保留最终结论

---

## 第 8 步：subagent 产出最终摘要

当子 agent 读完并理解这些文件后，某一轮就不会再调工具，而是直接输出总结文本，比如：

```text
Summary:
- agents/base.py defines shared agent utilities and common interfaces.
- agents/reviewer.py reviews outputs and identifies issues.
- agents/builder.py handles implementation-oriented tasks.
```

这时 `response.stop_reason != "tool_use"`，subagent 的循环结束。

然后 `spawn_subagent()` 会执行：

```python
result = extract_text(messages[-1]["content"])
return result
```

也就是说，subagent 返回给父 agent 的不是完整消息历史，而只是这段文本总结。

---

## 第 9 步：父 agent 收到 `task` 的工具结果

`spawn_subagent()` 返回的摘要字符串，会被父 agent 当成一次普通工具执行结果。

父 agent 会把它封装成：

```python
{
  "type": "tool_result",
  "tool_use_id": ...,
  "content": "Summary:\n- agents/base.py ..."
}
```

然后追加回父 agent 的 `messages`。

于是父 agent 的上下文大概变成：

```python
[
  {"role": "user", "content": "Use a subtask to summarize files under the agents/ directory"},
  {"role": "assistant", "content": [ToolUseBlock(name="task", ...)]},
  {"role": "user", "content": [
      {"type": "tool_result", "content": "Summary:\n- agents/base.py ..."}
  ]}
]
```

注意这里很关键：

父 agent 只看到“`task` 返回了一段摘要”，并不会看到 subagent 中间如何 `glob`、如何 `read_file`、读了多少文件。

---

## 第 10 步：父 agent 基于摘要生成最终回答

接下来父 agent 再次调用模型。

因为现在它已经拿到了 subagent 的结论，所以通常不需要再调工具，而会直接生成最终答复，例如：

```text
I used a subtask to inspect the agents/ directory.

Here is a summary:
- agents/base.py ...
- agents/reviewer.py ...
- agents/builder.py ...
```

这一轮不是 `tool_use`，所以 `agent_loop()` 会触发 `Stop` hook，然后结束。

---

## 第 11 步：主程序把最终文本打印给用户

最后回到 `__main__` 部分，程序遍历最后一条 assistant 消息里的 text block，并打印到终端。

所以用户最终看到的是父 agent 的整理结果，而不是 subagent 的内部操作流水。

---

## 这个例子里最值得记住的点

### 1. 父 agent 只是普通地调用了 `task`

它没有特殊分支逻辑，只是按工具分发表调到 `spawn_subagent()`。

### 2. subagent 拥有独立的 `messages[]`

这保证了中间过程不会污染父上下文。

### 3. subagent 能调基础工具，但不能再调 `task`

这避免了递归不断展开。

### 4. 返回给父 agent 的只有摘要

这让父 agent 的上下文保持简洁，只保留高价值结论。

---

## 最后用一句话记住 s06

`s06` 的核心不是“多开一个 agent”本身，而是：

把复杂任务拆到独立上下文里执行，让中间推理留在子上下文中，只把结论带回主流程。
