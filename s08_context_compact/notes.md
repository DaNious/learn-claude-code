# s08 Context Compact 学习笔记

## 按顺序理解这个代码

可以，我们就按“程序实际跑起来的顺序”理解这份代码。核心结论先说：

这不是一个普通聊天脚本，它是一个“会自己压缩上下文”的 agent。每次调用模型前，它都会先清理 `messages`，尽量把历史变短，但又不丢掉当前任务需要的关键信息。相关代码主要在 [code.py](./code.py) 这一段的 `agent_loop()`，设计说明在 [README.zh.md](./README.zh.md)。

### 1. 程序启动后先做初始化

从文件顶部往下看：

- `load_dotenv()` 先加载环境变量。
- 然后定义工作目录、转存目录、 Anthropic client 和模型名。
- `SYSTEM` 是系统提示词，重点是这句：压缩后只把 `Current user request` 当指令，把 `Conversation summary` 当参考资料。

这一步的意义是先把 agent 的运行环境搭好，并提前规定“压缩后的摘要不能反客为主”。

### 2. 然后定义工具系统

接着是几个基础工具函数：

- `run_bash`
- `run_read`
- `run_write`
- `run_edit`
- `run_glob`

然后把这些函数包装成模型可调用的工具描述 `TOOLS` 和本地执行映射 `TOOL_HANDLERS`。

这里额外新增了一个特殊工具 `compact`。它不是做文件操作，而是让模型主动说：“这一阶段结束了，帮我压缩历史”。

### 3. 工具执行前后会走 Hook

接着是 Hook 机制。

- `permission_hook()` 负责拦危险命令和越界路径。
- `log_hook()` 打印工具调用日志。
- `large_output_hook()` 提示输出特别大。

真正执行工具的是 `execute_tool()`：

- 先触发 `PreToolUse`
- 再调用真实 handler
- 最后触发 `PostToolUse`

也就是说，工具不是模型直接跑的，而是“先过守卫，再执行”。

### 4. 这份代码最重要的是 `ContextCompactor`

从 `ContextCompactor` 开始进入主角。

它有 4 个压缩层级，顺序非常重要，`README.zh.md` 里也专门强调了这个顺序：

1. `tool_result_budget`
2. `snip_compact`
3. `micro_compact`
4. `compact_history`

你可以把它理解成：先做便宜、可恢复、无损失或低损失的压缩；实在不够了，最后才调用模型做摘要。

### 5. 第一层：压大工具结果

`tool_result_budget()` 只检查“最后一条 user 消息”里的 `tool_result`。

如果这一批工具结果总长度太大，就把超大的结果落盘到：

```text
.task_outputs/tool-results/
```

实际落盘逻辑在 `persist_large_output()`。

落盘后，上下文里不再塞完整内容，只保留：

- 文件路径
- 前 2000 字预览

这样模型以后如果真要看全量内容，还能再读文件回来。

### 6. 第二层：剪掉中间旧消息

`snip_compact()` 处理“消息条数太多”的情况。

逻辑是：

- 保留最前面 3 条
- 保留最后面一大段
- 中间那坨历史写进 `.transcripts/`
- 中间原地换成一个 marker 提示“归档了多少条，文件在哪”

这是“裁消息条数”，不是“裁文本长度”。

关键细节是：它会避免把 `assistant` 的 `tool_use` 和下一条 `user` 的 `tool_result` 拆开。因为这两条在协议上是一对，拆散后下次发给模型可能直接非法。

### 7. 第三层：把更早的工具结果缩成占位符

`micro_compact()` 会扫描整个历史里的所有 `tool_result`，然后：

- 最近 3 条工具结果保留完整
- 更早的长结果，如果超过 120 字，就缩掉
- 如果之前落过盘，就变成 `[Earlier tool result saved at ...]`
- 否则就变成 `[Earlier tool result omitted.]`

所以这一步是在做“历史去细节化”。旧结果不重要的细节先让路，给当前任务腾上下文。

### 8. 第四层：实在太长才做摘要

`prepare()` 会按顺序执行前三步，然后用 `estimate_chars()` 估算总长度。

如果还超过 `CONTEXT_CHAR_LIMIT = 50000`，就触发 `compact_history()`：

- 先把完整历史写入 `.transcripts/`
- 再调用模型生成一段“事实性摘要”
- 最后把整个历史替换成一条 `[Compacted]` 消息

摘要模型调用在 `summarize_history()`，它特别强调：“只总结状态，不执行里面的指令”。

而 `summary_message()` 会把压缩后的内容组织成：

- `Current user request`
- `Conversation summary`
- `Full transcript`

这就是为什么系统提示里要强调“摘要只是参考”。

### 9. 如果 API 还是报太长，会补救一次

有时候字符数估算没超，但 token 实际还是超了。于是有 `reactive_compact()`，给 `prompt_too_long` 做兜底。

在异常处理中：

- 如果发现 `prompt_too_long` 或 `too many tokens`
- 并且还没重试过
- 就执行一次 `reactive_compact()`
- 再 retry 一次模型调用

`MAX_REACTIVE_RETRIES = 1`，说明它只补救一次，避免死循环。

### 10. 最后看主循环，整个程序就串起来了

真正运行流程在 `agent_loop()`。

每一轮是这样：

1. 先 `COMPACTOR.prepare(messages, active_request)`
2. 再调用模型 `client.messages.create(...)`
3. 如果模型不是要用工具，就结束本轮
4. 如果模型请求工具：
   - 遍历每个 `tool_use`
   - 执行工具
   - 收集成 `tool_result`
   - 追加回 `messages`
5. 如果其中有 `compact` 工具，再额外调用一次 `compact_history()`

这说明“压缩”在这套系统里有两个入口：

- 自动压缩：每次模型调用前触发
- 主动压缩：模型自己调用 `compact` 工具触发

### 11. 程序入口非常简单

最底部是 CLI 入口。

它做的事情只有几步：

- 打印提示
- 循环读用户输入
- 把输入追加到 `history`
- 调 `agent_loop(history, query)`
- 把最后一轮 assistant 的文本输出打印出来

所以从外面看它像个命令行聊天工具；从里面看，它其实是一个“带上下文管理策略的 agent runtime”。


## 以一个能触发所有 compact 机制的消息为例，模拟一次程序流转并给出每步的解释

下面我用一个“能把 5 种压缩相关路径都碰到”的例子来模拟：

- `tool_result_budget`
- `snip_compact`
- `micro_compact`
- `compact_history`
- `reactive_compact`
- 以及模型主动调用 `compact` 工具

注意：`compact_history` 和 `reactive_compact` 在同一轮里通常不会同时发生，因为前者是“调用前主动压缩成功”，后者是“调用 API 后才发现还是太长再补救”。所以我会分成一次主流程，再补一个“如果 API 仍报太长”的分支。

### 例子消息

用户输入这一句：

```text
请递归读取仓库里所有 README、Python 文件和最近测试日志，比较架构演进，找出上下文管理策略的变化，写一个结论；如果你觉得这一阶段信息已经够了，可以自行 compact 后继续。
```

这个请求为什么容易触发全部机制：

- 会读很多文件，工具结果很大
- 对话历史假设已经很长，超过 50 条消息
- 历史里已经积累了很多旧 `tool_result`
- 当前轮模型还有可能主动调用 `compact`

### 初始状态

假设进入这轮之前，`history` 已经很大：

- 总消息数：`68`
- 其中有很多早期 `tool_result`
- 最近一批工具结果特别大，比如上一轮刚读过一个超长日志
- 当前 `history` 末尾还没有压缩

CLI 主循环先做这件事：

```python
history.append({"role": "user", "content": query})
agent_loop(history, query)
```

所以此时 `messages[-1]` 就是这次用户的新请求。

### 第 1 步：进入 `agent_loop`

进入 `agent_loop(messages, active_request)`。

第一句就是：

```python
messages[:] = COMPACTOR.prepare(messages, active_request)
```

也就是说，模型还没调用，先压缩上下文。

### 第 2 步：`tool_result_budget` 先处理“最后一批工具结果”

进入 `tool_result_budget()`。

这一步只看最后一条消息是不是“装着一批 `tool_result` 的 user 消息”。

这次我们的最后一条是普通用户请求：

```python
{"role": "user", "content": "请递归读取仓库里所有 README..."}
```

所以这一轮这里其实不会动任何东西，直接返回。

解释：

- `tool_result_budget` 不是全历史扫描
- 它只负责“上一轮刚产生的那一批工具结果太大怎么办”
- 当前轮刚开始时，最后一条是自然语言请求，不是工具结果批次

### 第 3 步：`snip_compact` 发现消息条数超了

进入 `snip_compact()`。

假设当前有 `69` 条消息，超过默认 `max_messages=50`，于是开始剪。

它的大致动作：

- 前 3 条保留
- 后 47 条保留
- 中间那一段写到 `.transcripts/transcript_xxx.jsonl`
- 中间原地插一个 marker

`messages` 会从这样：

```python
[m0, m1, m2, m3, m4, ..., m67, m68]
```

变成这样：

```python
[m0, m1, m2, {"role":"user","content":"[19 messages archived at ...]"}, m22, ..., m68]
```

解释：

- 这是“裁消息条数”，不是“裁文本长度”
- 它保留最前面的开场设定和最近的工作区间
- 中间旧历史归档到磁盘，之后还能追溯

关键保护逻辑：

如果裁切点刚好落在

- `assistant(tool_use)`
- `user(tool_result)`

这对消息中间，它会挪一下边界，避免拆散。因为 Anthropic 的工具协议要求这两者成对存在。

### 第 4 步：`micro_compact` 缩旧工具结果

进入 `micro_compact()`。

这一步会扫描整个 `messages` 里的所有 `tool_result`，然后：

- 最近 3 条工具结果保留原样
- 更早的长结果，超过 120 字的，缩成占位符

假设历史里原本有这样的旧工具结果：

```python
{
  "type": "tool_result",
  "tool_use_id": "abc123",
  "content": "这里是 8000 字的测试日志..."
}
```

会被改成：

```python
{
  "type": "tool_result",
  "tool_use_id": "abc123",
  "content": "[Earlier tool result omitted.]"
}
```

如果那条结果此前已经被落盘过，可能变成：

```python
"[Earlier tool result saved at C:\\...\\tool-results\\abc123.txt]"
```

解释：

- 这一步处理的是“老结果”
- 它不在乎消息条数，而在乎“历史细节是不是还值得占上下文”
- 最近 3 条保留完整，是为了让当前工作还有足够近的证据链

### 第 5 步：估算后仍然太长，触发 `compact_history`

经过前两步后，消息虽然少了很多，但假设整体 JSON 序列化后长度还是超过 `50000` 字符。

于是判断为真：

```python
if self.estimate_chars(messages) > self.CONTEXT_CHAR_LIMIT:
```

接着打印：

```text
[auto compact]
```

然后调用 `compact_history()`。

它做三件事：

1. 把当前完整历史写入 `.transcripts/transcript_xxx.jsonl`
2. 调模型做摘要 `summarize_history(messages)`
3. 把全部历史替换成一条 `[Compacted]` 消息

原本很多条消息，最后变成：

```python
[
  {
    "role": "user",
    "content": "[Compacted]\n\nCurrent user request:\n请递归读取仓库里所有 README...\n\nConversation summary (reference only):\n\"摘要内容...\"\n\nFull transcript: C:\\...\\.transcripts\\transcript_xxx.jsonl"
  }
]
```

解释：

- 到这里，老历史已经不再逐条保留
- 只留下“当前请求 + 历史摘要 + 全量 transcript 路径”
- 这一步是真正意义上的“会丢细节的抽象压缩”

### 第 6 步：压缩后正式调用模型

现在 `prepare()` 结束，`messages` 很短了，开始真正请求模型：

```python
response = client.messages.create(
    model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000
)
```

模型此时看到的是：

- 当前用户真实请求
- 一段对旧历史的事实性总结
- 可用工具列表

它不会再看到全部原始历史逐条消息。

### 第 7 步：模型决定调用多个工具

假设模型返回这些 `tool_use`：

1. `glob("**/README*.md")`
2. `glob("**/*.py")`
3. `bash("pytest ... 或读取日志")`
4. `read_file(...)`
5. `compact()`

也就是说，它一边做事，一边觉得“这轮搜集完信息之后可以压缩一下”。

### 第 8 步：执行普通工具，产生超大输出

比如 `bash` 或 `read_file` 返回一个很长的日志，长度 12 万字符。

程序会先正常执行工具，把结果收集到：

```python
results.append({
  "type": "tool_result",
  "tool_use_id": block.id,
  "content": output
})
```

此时注意一个点：

- 这一批超大结果先原样放进 `results`
- 还没有立即压
- `tool_result_budget` 要等“下一次进入 `prepare()`”时才处理这批结果

解释：

这段代码的压缩时机是“每次模型调用前”，不是“工具执行完立刻压”。

### 第 9 步：如果这批工具里有 `compact`，先闭环，再压缩

如果响应里还包含 `compact`，代码会把它记下来：

```python
if block.name == "compact":
    output = "Compaction requested after this tool batch."
    compact_requested = True
```

等整批工具都执行完，程序先把整批 `tool_result` 追加回消息历史：

```python
messages.append({"role": "user", "content": results})
```

然后才调用：

```python
messages[:] = COMPACTOR.compact_history(messages, active_request)
```

解释这个顺序为什么重要：

- 如果先压缩、后追加工具结果，就会出现“孤立的工具结果”
- 模型下一轮看到的上下文可能协议不完整
- 所以必须先把 `tool_use -> tool_result` 这轮闭合，再整体摘要

这是这份代码里非常关键的细节。

### 第 10 步：下一轮再进来时，会触发 `tool_result_budget`

假设这时没有立即结束，模型还要继续下一轮推理。

再次进入 `agent_loop` 的 while 顶部时，`messages[-1]` 就是刚才那批 `tool_result`，其中包含一个超大的日志输出。

于是这次 `tool_result_budget()` 就会真的生效：

- 统计这一批 `tool_result` 总大小
- 如果超过 `200000`
- 从最大的结果开始处理
- 对超过 `30000` 的结果调用 `persist_large_output()`

比如那条 12 万字符的日志会被写到：

```text
.task_outputs/tool-results/<tool_use_id>.txt
```

上下文里的内容替换成：

```text
<persisted-output>
Full output: C:\...\tool-results\<tool_use_id>.txt
Preview:
前 2000 字...
</persisted-output>
```

解释：

- 这是四步压缩链里最偏“无损”的一步
- 因为完整内容还在磁盘上，只是从 prompt 里挪走了

### 补充分支：如果 `prepare()` 估算没超，但 API 还是报 `prompt_too_long`

这就是 `reactive_compact` 的场景。

假设某一轮里：

- `tool_result_budget`
- `snip_compact`
- `micro_compact`

都做完了

- `estimate_chars(messages)` 看起来还没超过 50000
- 但实际 token 还是过大

调用 API 时会抛异常，被这里接住：

```python
except Exception as error:
    too_long = any(text in str(error).lower()
                   for text in ("prompt_too_long", "too many tokens"))
```

然后触发：

```python
messages[:] = COMPACTOR.reactive_compact(messages, active_request)
```

`reactive_compact()` 跟 `compact_history()` 的区别是：

- 它只摘要较早历史
- 尽量保留最近 `5` 条消息
- 然后重试一次 API

结果会像这样：

```python
[
  {"role": "user", "content": "[Reactive compact] ...摘要旧历史..."},
  recent_message_1,
  recent_message_2,
  recent_message_3,
  recent_message_4,
  recent_message_5
]
```

解释：

- `compact_history` 是“调用前主动全量压”
- `reactive_compact` 是“调用失败后紧急保近丢远再重试”
- 它只允许重试一次，避免死循环

### 把整条链路压成一句话

这条消息的一次完整流转可以概括成：

1. 用户输入一个会导致大量读文件和日志分析的请求
2. `agent_loop()` 先调用 `prepare()`
3. `snip_compact` 把过长的消息历史中段归档
4. `micro_compact` 把更早的工具结果缩成占位符
5. 如果还太长，`compact_history` 把整段旧历史摘要成一条 `[Compacted]`
6. 模型开始工作，调用多个工具
7. 工具结果先完整追加回 `messages`
8. 如果模型还调用了 `compact`，这一整批结果闭环后立刻再做一次 `compact_history`
9. 下一轮调用模型前，`tool_result_budget` 会把上一轮超大的工具输出落盘
10. 如果即便这样 API 仍说太长，走 `reactive_compact`，保留最近 5 条后重试一次

### 你真正该抓住的核心

这份代码的设计重点不是“如何摘要”，而是“按损失从低到高逐层腾空间”：

- 先落盘大输出
- 再裁掉中间旧消息
- 再把更老的工具结果替换成占位符
- 最后才让模型总结历史
- 如果估算失误，再补一次 `reactive_compact`


## `messages` 数组变化前后对照表

下面这版只看 `messages` 怎么变。为了好读，我不会把全部 60 多条消息都展开，只保留关键片段。

先约定一下符号：

- `U(...)` 表示 `role="user"`
- `A(...)` 表示 `role="assistant"`
- `TU[...]` 表示 assistant 里有 `tool_use`
- `TR[...]` 表示 user 里有 `tool_result`
- `S` 表示普通文本回复
- `M1/M2/...` 表示消息编号

### 0. 用户刚输入，还没进压缩前

用户输入：

```text
请递归读取仓库里所有 README、Python 文件和最近测试日志，比较架构演进，找出上下文管理策略的变化，写一个结论；如果你觉得这一阶段信息已经够了，可以自行 compact 后继续。
```

此时 `history.append(...)` 之后，`messages` 可以抽象成：

```python
[
  M1: U("最早的用户任务"),
  M2: A(S),
  M3: A(TU[read_file]),
  M4: U(TR["很长的文件内容A"]),
  M5: A(TU[bash]),
  M6: U(TR["很长的日志B"]),
  ...
  M66: A(TU[read_file, bash]),
  M67: U(TR["很长的输出C", "很长的输出D"]),
  M68: A(S),
  M69: U("请递归读取仓库里所有 README、Python 文件和最近测试日志...")
]
```

特点：

- 总消息数已经超过 50
- 历史里有很多旧 `tool_result`
- 当前最后一条 `M69` 是新的用户请求

### 1. `tool_result_budget` 之后

这一步只检查最后一条消息 `messages[-1]`。

现在最后一条是：

```python
M69: U("请递归读取仓库里所有 README...")
```

不是 `tool_result` 批次，所以完全不变：

```python
[
  M1, M2, M3, ..., M68,
  M69: U("请递归读取仓库里所有 README...")
]
```

解释：

- 这一步这次“没触发效果”
- 但它仍然被调用了
- 它的目标是“上一轮刚生成的大工具结果”，不是普通用户提问

### 2. `snip_compact` 之后

假设现在有 69 条消息，超过 50，于是开始剪中间。

原本大致是：

```python
[
  M1, M2, M3, M4, M5, M6, ..., M66, M67, M68, M69
]
```

压完后变成：

```python
[
  M1: U("最早的用户任务"),
  M2: A(S),
  M3: A(TU[read_file]),
  Mx: U("[19 messages archived at C:\\...\\.transcripts\\transcript_xxx.jsonl]"),
  M23: ...,
  M24: ...,
  ...
  M66: A(TU[read_file, bash]),
  M67: U(TR["很长的输出C", "很长的输出D"]),
  M68: A(S),
  M69: U("请递归读取仓库里所有 README...")
]
```

要注意两点：

1. 中间很多条消息没了，但不是丢了，而是写进 `.transcripts`
2. 它会保护 `A(TU[...]) -> U(TR[...])` 这种配对，不会从中间劈开

如果裁切点刚好卡在：

```python
M3: A(TU[read_file])
M4: U(TR["很长的文件内容A"])
```

之间，它会自动把边界后移或前移，保证这一对要么都留，要么都归档。

### 3. `micro_compact` 之后

这一步会扫描全历史里的所有 `TR[...]`，然后把“不是最近 3 条”的长工具结果缩掉。

压缩前，可能还有这些较早工具结果：

```python
M40: U(TR["README 内容，3000 字"])
M47: U(TR["pytest 日志，9000 字"])
M53: U(TR["目录扫描结果，5000 字"])
M61: U(TR["Python 文件内容片段，2000 字"])
M67: U(TR["很长的输出C", "很长的输出D"])
```

压完后，假设最近 3 条工具结果是 `M53/M61/M67`，则更早的 `M40/M47` 会变成：

```python
M40: U(TR["[Earlier tool result omitted.]"])
M47: U(TR["[Earlier tool result omitted.]"])
M53: U(TR["目录扫描结果，5000 字"])
M61: U(TR["Python 文件内容片段，2000 字"])
M67: U(TR["很长的输出C", "很长的输出D"])
```

如果 `M47` 之前已经被落盘过，则可能变成：

```python
M47: U(TR["[Earlier tool result saved at C:\\...\\tool-results\\abc123.txt]"])
```

解释：

- 这一步不是删消息
- 是把旧工具结果的“正文”缩成占位符
- 目的是保留“曾经有过这次工具调用”这个事实，但不再把正文都塞在上下文里

### 4. `compact_history` 之后

假设经过前两步后，`estimate_chars(messages)` 还是超过 `50000`，于是触发自动摘要。

压缩前，`messages` 还是很多条，只是中间有一条 archive marker、旧工具结果被缩短了。

压缩后直接收敛成一条：

```python
[
  {
    "role": "user",
    "content": "[Compacted]\n\nCurrent user request:\n请递归读取仓库里所有 README、Python 文件和最近测试日志，比较架构演进，找出上下文管理策略的变化，写一个结论；如果你觉得这一阶段信息已经够了，可以自行 compact 后继续。\n\nConversation summary (reference only):\n\"之前已经读过若干 README 和代码文件，确认了工具协议、hook 机制、上下文压缩策略的演进；已有一些文件路径、阶段结论和未完成项...\"\n\nFull transcript: C:\\...\\.transcripts\\transcript_yyy.jsonl"
  }
]
```

这是最关键的一次结构变化：

- 原来是“很多条真实对话消息”
- 现在变成“1 条带摘要的 user 消息”

这里也能看出这份代码的设计理念：

- 当前用户请求被明确放在 `Current user request`
- 历史摘要降级成参考材料 `Conversation summary`

### 5. 模型返回 `tool_use` 后

现在模型基于这条 `[Compacted]` 消息工作，假设它返回：

```python
A(
  TU[
    glob("**/README*.md"),
    glob("**/*.py"),
    bash("读取最近日志"),
    read_file("s08_context_compact/code.py"),
    compact()
  ]
)
```

于是 `messages` 变成：

```python
[
  C1: U("[Compacted] ..."),
  C2: A(TU[glob, glob, bash, read_file, compact])
]
```

解释：

- 这一条 assistant 消息内部可能包含多个 `tool_use`
- 不是“一次响应只能调一个工具”

### 6. 工具执行完、追加 `tool_result` 之后

程序遍历每个 `tool_use`，执行后收集成 `results`，最后统一追加一条 user 消息。

假设几个工具结果是：

- `glob README` 返回 40 行路径
- `glob py` 返回 120 行路径
- `bash` 返回 12 万字符日志
- `read_file` 返回 2 万字符代码
- `compact` 返回固定说明字符串

此时 `messages` 变成：

```python
[
  C1: U("[Compacted] ..."),
  C2: A(TU[glob, glob, bash, read_file, compact]),
  C3: U(TR[
    "README 路径列表...",
    "Python 文件路径列表...",
    "12万字符日志......",
    "2万字符代码内容......",
    "Compaction requested after this tool batch."
  ])
]
```

这个阶段非常重要，因为它体现了“工具调用闭环”：

- assistant 发出 `tool_use`
- user 回 `tool_result`

如果没有这一步，协议就断了。

### 7. 因为本轮包含 `compact`，立刻再做一次 `compact_history`

当前批次工具执行完后，程序发现 `compact_requested=True`，于是再次压缩。

压缩前：

```python
[
  C1: U("[Compacted] ..."),
  C2: A(TU[glob, glob, bash, read_file, compact]),
  C3: U(TR["README 路径列表...", "Python 文件路径列表...", "12万字符日志...", ...])
]
```

压缩后再次收敛成：

```python
[
  D1: U("[Compacted]\n\nCurrent user request:\n请递归读取仓库里所有 README、Python 文件和最近测试日志...\n\nConversation summary (reference only):\n\"本轮新增完成了 README 扫描、Python 文件扫描、日志读取和关键代码读取，已获得足够材料进入结论阶段...\"\n\nFull transcript: C:\\...\\.transcripts\\transcript_zzz.jsonl")
]
```

解释：

- 模型主动要求 compact，表示“这批信息采集完成，可以进入下一个阶段”
- harness 会先把这批工具结果完整落进历史，再整体摘要
- 所以下一轮模型看到的不是超大原始输出，而是摘要后的状态

### 8. 下一轮开始时，`tool_result_budget` 会怎么处理

如果这时没有立刻压成一条，而是继续保留 `C3` 原始结果进入下一轮，那么 `prepare()` 顶部就会处理 `C3`。

`C3` 原本是：

```python
C3: U(TR[
  "README 路径列表...",
  "Python 文件路径列表...",
  "12万字符日志......",
  "2万字符代码内容......"
])
```

`tool_result_budget` 后会变成：

```python
C3: U(TR[
  "README 路径列表...",
  "Python 文件路径列表...",
  "<persisted-output>\nFull output: C:\\...\\.task_outputs\\tool-results\\toolu_123.txt\nPreview:\n前2000字...\n</persisted-output>",
  "2万字符代码内容......"
])
```

如果这一批总量还特别大，它还会继续处理更大的块，直到总量降到预算附近。

解释：

- 这是“只压最新一批工具结果”的机制
- 也是最接近无损的一种压缩

### 9. `reactive_compact` 分支的前后对照

这是补充场景，不一定和上面的自动 `compact_history` 在同一轮同时发生。

假设某一轮 `prepare()` 后看起来还行，`messages` 类似：

```python
[
  R1: U("[Compacted] ..."),
  R2: A(TU[read_file, bash]),
  R3: U(TR["很长代码片段...", "很长日志片段..."]),
  R4: A(S),
  R5: U("继续比较 s08 和 s09 的差别")
]
```

结果 API 还是报 `prompt_too_long`。

这时 `reactive_compact()` 会保留最近 5 条附近的内容，较早历史做摘要。

压缩后可能变成：

```python
[
  {
    "role": "user",
    "content": "[Reactive compact]\n\nCurrent user request:\n继续比较 s08 和 s09 的差别\n\nConversation summary (reference only):\n\"较早历史中已经确认 s08 负责当前会话上下文压缩，s09 负责持久记忆...\"\n\nFull transcript: C:\\...\\.transcripts\\transcript_rrr.jsonl"
  },
  R2: A(TU[read_file, bash]),
  R3: U(TR["很长代码片段...", "很长日志片段..."]),
  R4: A(S),
  R5: U("继续比较 s08 和 s09 的差别")
]
```

解释：

- 它不是把所有东西压成 1 条
- 而是“摘要较早历史，保住最近局部上下文”
- 这样 retry 时，模型还能看到刚刚发生的近端交互

### 最后给一张总表

```text
初始：
[很多历史消息 ... , 用户新请求]

tool_result_budget 后：
[几乎不变]
因为最后一条不是 tool_result 批次

snip_compact 后：
[前3条, archive marker, 后47条]
中间旧消息归档到 .transcripts

micro_compact 后：
[消息条数不一定变，但更早 tool_result 正文变成占位符]
旧细节被抽空

compact_history 后：
[[Compacted] + Current user request + Conversation summary + transcript路径]
全部历史收敛成1条

模型 tool_use 后：
[[Compacted]..., assistant(tool_use...)]
开始一轮新的工具闭环

tool_result 追加后：
[[Compacted]..., assistant(tool_use...), user(tool_result...)]
这一轮工具调用闭合

若包含 compact 工具：
再次 compact_history
把这一整轮新采集的信息再压成1条

若 API 仍报太长：
reactive_compact
[Reactive compact摘要, 最近几条原消息]
```
