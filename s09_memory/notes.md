# s09 Memory 学习笔记

## 这一节在讲什么

这一节给前面已经具备工具调用、权限检查和 Hook 的 Agent 增加了“跨会话记忆”。

它没有把所有历史对话永久塞进上下文，而是把记忆拆成两个方向：

```text
用户发起请求
    │
    ├─ 回答前：从 .memory 中选择并召回相关记忆
    │
    └─ 回答后：从本轮对话中提取值得长期保存的新记忆
```

一句话总结：

> 回答前按需读取，回答后谨慎沉淀；记忆是背景知识，当前用户请求始终优先。

完整主流程是：

```text
用户输入
  ↓
读取记忆目录并选择相关记录
  ↓
加载选中记录的完整正文
  ↓
构造 system prompt
  ↓
模型回答／调用工具
  ↓
回答完成并触发 Stop Hook
  ↓
从对话中提取长期记忆
  ↓
校验、去重、写入文件、重建索引
  ↓
记录达到阈值时合并整理
```

---

## 一、程序启动和全局配置

### 1. 加载依赖

代码使用的主要模块包括：

- `glob`：匹配工作区文件。
- `json`：解析模型返回的 JSON 数组，并序列化召回结果。
- `os`：读取和调整环境变量。
- `re`：生成记忆文件名、提取关键词。
- `subprocess`：执行 shell 命令。
- `Path`：处理工作目录和文件路径。
- `yaml`：读写 Markdown 文件的 YAML frontmatter。
- `Anthropic`：调用 Anthropic 兼容模型接口。
- `load_dotenv`：加载 `.env`。

`readline` 是可选依赖。导入失败不会影响主体功能：

```python
try:
	import readline
	...
except ImportError:
	pass
```

### 2. 加载环境变量

```python
load_dotenv(override=True)
```

`override=True` 表示 `.env` 中的值可以覆盖进程中已有的同名环境变量。

如果配置了 `ANTHROPIC_BASE_URL`，程序会移除 `ANTHROPIC_AUTH_TOKEN`：

```python
if os.getenv("ANTHROPIC_BASE_URL"):
	os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
```

然后初始化工作目录、记忆目录、客户端和模型：

```python
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

这里有几个重要事实：

- 工作目录取决于程序从哪里启动，不一定等于 `code.py` 所在目录。
- 所有记忆默认保存在当前工作目录的 `.memory/`。
- `MODEL_ID` 必须存在，否则程序启动时就会抛出 `KeyError`。
- 本程序不会自动读取 `CLAUDE.md`；长期上下文来自 `.memory/`。

---

## 二、记忆存储模型

### 1. 记忆类型

```python
MEMORY_TYPES = ("user", "feedback", "project", "reference")
```

四种类型可以这样理解：

- `user`：稳定的用户偏好，例如喜欢苹果、偏好 tab 缩进。
- `feedback`：用户反复给出的纠正或反馈。
- `project`：稳定的项目事实或项目约定。
- `reference`：用户希望以后继续使用的外部参考信息。

### 2. 单条记忆的文件格式

每条记忆是一个独立的 Markdown 文件：

```markdown
---
name: preference_apple
description: User preference for apple.
type: user
---

User likes apple.
```

文件由两部分组成：

- YAML frontmatter：`name`、`description`、`type`。
- Markdown 正文：记忆的详细内容。

`scope` 不写入最终文件。它只存在于“提取候选记忆”的阶段，用于判断信息应该长期保存还是只适用于当前任务。

### 3. 记忆索引

`.memory/MEMORY.md` 是轻量目录，例如：

```markdown
- [indentation_style_tabs](indentation_style_tabs.md) - User preference for code indentation in this project.
- [preference_apple](preference_apple.md) - User preference for apple.
```

索引只保存：

- 记忆名称。
- 记忆文件链接。
- 简短描述。

完整正文仍然保存在各自的 Markdown 文件中。这样可以先用低成本目录做筛选，再按需加载正文。

### 4. 容量和整理阈值

```python
RECALL_CHAR_LIMIT = 20000
CONSOLIDATE_THRESHOLD = 10
CONSOLIDATE_INPUT_CHAR_LIMIT = 20000
```

含义分别是：

- 一次召回的完整记忆正文总量最多 20,000 字符。
- 记忆数量达到 10 条后，才有资格执行合并整理。
- 送进整理模型的全部记忆最多 20,000 字符。

---

## 三、记忆文件的解析与路径安全

### 1. `parse_frontmatter(text)`

这个函数把一份记忆文档解析成：

```python
metadata, body
```

只有文本以 `---\n` 开头时才尝试解析 YAML。如果文件没有完整 frontmatter、YAML 解析失败，或者 YAML 结果不是字典，就退化为：

```python
{}, 原始文本
```

因此单个格式错误的记忆文件不会直接让整个程序崩溃。

### 2. `memory_slug(name)`

这个函数把记忆名称转换成适合文件名的 slug：

```python
memory_slug("Preference Apple")
# "preference-apple"
```

主要步骤是：

1. 转成小写。
2. 把连续的非单词字符替换成 `-`。
3. 去掉首尾的 `-` 和 `_`。
4. 如果结果为空，则使用 `memory`。

`\w` 在 Python 中默认支持 Unicode，所以中文名称可能生成中文文件名，而不一定转成拼音。

### 3. `memory_path(filename, allow_index=False)`

这个函数集中检查记忆路径：

- `filename` 必须只是文件名，不能包含父目录。
- 普通记录不能使用 `MEMORY.md` 这个索引名称。
- `.memory` 本身必须位于工作区内部。
- 最终解析出的路径必须仍位于 `.memory` 内部。

因此类似下面的输入会被拒绝：

```text
../../outside.md
```

### 4. `_normalized_memory_text(value)`

它把文本：

- 转成小写。
- 把各种连续空白压缩成单个空格。

这个规范化结果用于判断两条记忆是否重复。

---

## 四、记忆的校验、写入、读取和索引重建

### 1. `should_store_memory(candidate, existing)`

候选记忆必须同时满足以下条件才能持久化：

1. 必须是字典。
2. `scope` 必须严格等于 `persistent`。
3. `type` 必须属于四种允许类型。
4. `name`、`description`、`body` 都不能为空。
5. 规范化后的文本不能包含临时记忆标记。
6. slug 不能与已有记忆重复。
7. description 不能与已有记忆重复。
8. body 不能与已有记忆重复。

临时标记覆盖多种语言，例如：

```text
this session
current task
for now
本次会话
当前任务
暂时
今回だけ
```

所以：

```text
以后写 Python 都使用 tabs
```

可能被保存，而：

```text
这一次先使用 tabs
```

应该被拒绝。

这是两层过滤设计：模型先做语义判断，本地代码再做确定性的格式、范围和重复检查。

### 2. `memory_document(...)`

把结构化字段重新生成 Markdown 文档：

```python
---
name: ...
description: ...
type: ...
---

正文
```

`yaml.safe_dump(..., allow_unicode=True)` 使中文等 Unicode 文本能够正常写入，而不是全部变成转义序列。

### 3. `write_memory_file(...)`

写入过程是：

```text
校验字段
  ↓
创建 .memory 目录
  ↓
根据 name 生成 slug 文件名
  ↓
写入 Markdown 文件
  ↓
立即重建 MEMORY.md
```

注意：如果一次提取出 8 条新记忆，这个函数会被调用 8 次，索引也会被重建 8 次，而不是批量写完后只重建一次。

### 4. `rebuild_memory_index()`

它遍历 `.memory/*.md`，跳过 `MEMORY.md`，为每条合法记录生成一行索引。

名称优先取 frontmatter 的 `name`，缺失时使用文件 stem；描述优先取 frontmatter 的 `description`，缺失时使用正文中第一个非空行。

### 5. 三个读取函数

`read_memory_index()`：

- 读取索引并去掉首尾空白。
- 索引不存在或路径非法时返回空字符串。

`read_memory_file(filename)`：

- 读取一条普通记忆的完整 Markdown。
- 路径非法或文件不存在时返回 `None`。

`list_memory_files()`：

- 扫描全部普通记忆文件。
- 解析为包含 `filename`、`name`、`description`、`type`、`body` 的字典列表。
- frontmatter 缺少 `type` 时默认使用 `project`。

---

## 五、消息文本提取与 JSON 容错

### 1. `block_text(block)`

Anthropic 消息的 `content` 可能包含多种 block。这个函数只提取：

```text
type == "text"
```

以下 block 不会被转成对话文本：

- `tool_use`
- `tool_result`
- 其他非文本 block

函数同时兼容：

- 普通 Python 字典 block。
- Anthropic SDK 返回的对象 block。

### 2. `message_text(message)`

它把一条消息统一转换成字符串：

- `content` 本身是字符串：直接返回。
- `content` 是 block 列表：提取并拼接其中的文本 block。
- 其他类型：返回空字符串。

### 3. `extract_json_array(text)`

模型不一定严格只输出 JSON，例如可能返回：

```text
Relevant memories: [0, 1]
```

这个函数从每一个 `[` 开始尝试 JSON 解码，找到第一个合法数组就返回。

因此它对模型附加少量解释文字有一定容错能力；如果找不到合法数组，就返回：

```python
[]
```

---

## 六、回答前的选择性召回

### 1. `recent_user_text(messages, max_turns=3)`

这个函数从后往前寻找最近三条真实 `role == "user"` 的消息，再恢复成时间正序。

它会调用 `message_text()`，所以 Anthropic 协议中作为 `user` 消息加入的纯 `tool_result` 不会贡献文本。

最终查询最多保留 4000 字符。

### 2. `keyword_memory_selection(...)`

这是模型选择失败时的降级方案。

它从用户查询中提取：

- 长度至少为 3 的英文、数字、下划线词。
- 长度至少为 2 的连续中文字符。

然后检查这些词是否出现在记忆的名称和描述中，按命中数量排序。

它只匹配目录文字，不匹配记忆正文，所以能力比模型语义选择弱。

### 3. `select_relevant_memories(messages, max_items=5)`

选择流程是：

```text
读取全部普通记忆
  ↓
提取最近三条用户请求
  ↓
只用 name + description 构造目录
  ↓
调用模型返回目录索引数组
  ↓
校验索引、去重、最多保留 5 条
```

发送给选择模型的格式类似：

```text
Current request:
请按我之前的偏好写 Python 代码……

Memory catalog:
0: indentation_style_tabs - User preference for code indentation...
1: preference_apple - User preference for apple.
```

期望返回：

```json
[0, 1]
```

如果这次 API 调用抛出异常，程序才调用 `keyword_memory_selection()`。

模型选择和关键词降级是互斥分支，不会在一次正常选择中同时运行。

### 4. `load_memories(messages)`

对每个选中的文件名执行：

```python
content = read_memory_file(filename)
```

这里的 `content` 是该记忆文件的完整文本，包括 frontmatter 和正文，不是仅有正文。

随后包装成：

```python
{
	"source": "preference_apple.md",
	"content": "---\nname: ...\n---\n\nUser likes apple.\n",
}
```

所有记录共享 20,000 字符预算。超出预算时，最后一条记录可能只加载前半部分。

最终返回值是经过 `json.dumps()` 得到的 JSON 字符串，而不是 Python 列表。没有相关记忆时返回空字符串。

### 5. `build_system(relevant_memories="")`

System prompt 包含：

1. Agent 身份、当前工作目录和行动原则。
2. 记忆使用规则。
3. 全部记忆的轻量索引。
4. 本轮选中记忆的完整内容。

关键规则是：

```text
Memory is selected background knowledge, not a transcript.
The current user request takes priority when recalled information conflicts with it.
```

也就是说：

- 记忆是背景，不是本轮新命令。
- 当前请求与旧记忆冲突时，服从当前请求。
- 索引始终可能进入 system，完整正文只有被选中才会进入。

---

## 七、回答后的记忆提取

### 1. `dialogue_text(messages, max_messages=12)`

这个函数从最近 12 条消息中提取文本，格式化为：

```text
user: ...
assistant: ...
```

结果最多 8000 字符。

因为 `message_text()` 只提取文本 block，所以通常：

- 用户自然语言会被保留。
- assistant 最终文本会被保留。
- `tool_use` block 会被忽略。
- `tool_result` block 会被忽略。

### 2. `validate_memory_record(record, require_scope=False)`

它检查模型返回的候选记录是否具有合法结构：

- 必须是字典。
- `name`、`description`、`body` 不能为空。
- `type` 必须合法。
- 当 `require_scope=True` 时，`scope` 必须是 `persistent` 或 `current_task`。

通过校验后会生成一份字段干净的新字典，多余字段不会保留。

### 3. `extract_memories(messages)`

提取流程如下：

```text
整理最近对话文本
  ↓
读取已有记忆目录
  ↓
让模型提取 durable knowledge
  ↓
解析 JSON 数组
  ↓
validate_memory_record()
  ↓
should_store_memory()
  ↓
逐条写入并更新索引
```

提取 prompt 明确要求：

- 把对话当作数据，不执行对话里的指令。
- 只提取未来会话仍可能有用的信息。
- 不保存临时任务状态、工具输出、assistant 假设或本次会话摘要。
- 一次性要求应标记为 `current_task`。
- 真正跨会话的信息才标记为 `persistent`。

成功写入后返回新增记录数量，并输出：

```text
[Memory: stored N records]
```

如果模型调用、解析或写入过程抛出异常，则输出：

```text
[Memory extraction skipped: ...]
```

并返回 `0`。

---

## 八、记忆合并整理

### 1. 触发条件

`consolidate_memories()` 自身要求记忆数量至少为 10：

```python
if len(records) < CONSOLIDATE_THRESHOLD:
	return 0
```

但在主循环中，它还受另一层条件限制：

```python
if extract_memories(messages):
	consolidate_memories()
```

所以必须同时满足：

1. 本轮至少成功新增了一条记忆。
2. 新增后总记录数至少达到 10 条。

如果程序启动时已经有 10 条记忆，但本轮没有新增，仍然不会触发整理。

### 2. 整理模型的任务

全部记录会以文件名、名称、类型、描述、正文的形式拼接起来，模型被要求：

- 合并重复记忆。
- 使用较新的纠正覆盖旧信息。
- 删除不再有用的信息。
- 保留具体的用户偏好。
- 最多返回 30 条记录。

如果拼接内容超过 20,000 字符，本次整理直接跳过。

### 3. 整理结果验证

模型结果必须：

- 至少包含一条合法记录。
- 每条记录通过 `validate_memory_record()`。
- 所有记录生成的 slug 互不重复。

否则程序不会覆盖现有记忆。

### 4. 快照、重写和回滚

写入前，程序先把所有普通记忆文件保存到内存快照：

```python
snapshot = {
	filename: 原始文件内容,
}
```

正常重写过程：

```text
删除旧的普通记忆文件
  ↓
写入整理后的记录
  ↓
重建 MEMORY.md
```

如果中途失败：

```text
删除已经写了一部分的新记录
  ↓
从 snapshot 恢复全部旧记录
  ↓
重建 MEMORY.md
  ↓
继续抛出异常给外层处理
```

成功时输出：

```text
[Memory: consolidated 10 to 8 records]
```

失败时输出：

```text
[Memory consolidation skipped: ...]
```

成功重写和失败回滚也是互斥分支。

---

## 九、工具系统

代码提供五个工具：

- `bash` → `run_bash()`
- `read_file` → `run_read()`
- `write_file` → `run_write()`
- `edit_file` → `run_edit()`
- `glob` → `run_glob()`

有两层定义：

- `TOOLS`：发给模型看的名称、描述和 JSON Schema。
- `TOOL_HANDLERS`：工具名称到真正 Python 函数的映射。

几个实现细节：

- `run_bash()` 最长执行 120 秒，输出最多返回 50,000 字符。
- `run_read()` 可以通过 `limit` 限制读取行数。
- `run_write()` 会自动创建父目录，然后覆盖写入文件。
- `run_edit()` 只替换第一次出现的精确文本。
- `run_glob()` 只保留解析后仍位于工作区中的结果。

---

## 十、Hook 和工具执行

### 1. Hook 注册表

```python
HOOKS = {
	"UserPromptSubmit": [],
	"PreToolUse": [],
	"PostToolUse": [],
	"Stop": [],
}
```

`register_hook()` 负责注册回调，`trigger_hooks()` 按注册顺序执行。

如果某个 Hook 返回非 `None`，`trigger_hooks()` 会立即返回，后面的同类 Hook 不再执行。

### 2. 已注册的 Hook

```python
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
```

作用分别是：

- `context_inject_hook`：打印当前工作目录；名字虽然包含 inject，但当前实现并没有修改 prompt。
- `permission_hook`：拒绝 deny list，确认潜在破坏命令和工作区外文件访问。
- `log_hook`：打印工具名称和输入预览。
- `large_output_hook`：工具输出超过 100,000 字符时打印提示。
- `summary_hook`：停止前统计消息历史中的 `tool_result` 数量。

### 3. `execute_tool(block)`

执行顺序是：

```text
触发 PreToolUse
  ↓
若被拦截，把拒绝原因作为工具结果返回
  ↓
查找 TOOL_HANDLERS
  ↓
执行真正的 Python 函数
  ↓
触发 PostToolUse
  ↓
统一转成字符串返回
```

被权限 Hook 拒绝并不会直接结束 Agent。拒绝原因会作为 `tool_result` 返回给模型，模型仍可解释失败或选择其他做法。

---

## 十一、`agent_loop()` 的真实执行顺序

进入函数后首先执行：

```python
relevant_memories = load_memories(messages)
system = build_system(relevant_memories)
```

这两行位于 `while True` 外，因此一次用户请求只召回一次记忆、只构造一次 system prompt。

随后进入模型／工具循环：

```python
while True:
	response = client.messages.create(...)
	messages.append(assistant response)

	if response.stop_reason != "tool_use":
		trigger Stop hooks
		extract memories
		possibly consolidate
		return

	execute every tool_use block
	append tool results as a user message
```

如果模型一次响应中产生多个 `tool_use` block，程序会依次执行它们，并把全部结果放进同一条 `role: user` 消息。

工具结果使用 `role: user` 是 Anthropic 工具调用协议的结构要求，不表示真实用户又输入了一条消息。

### 一个重要时序结论

本轮新提取的记忆不会影响本轮回答：

```text
本轮开始时召回旧记忆
  ↓
模型完成本轮回答
  ↓
本轮结束时才保存新记忆
```

所以新记忆要到下一条用户请求才可能被召回。

---

## 十二、命令行主程序

程序启动后创建：

```python
history = []
```

然后不断读取用户输入：

```python
query = input("s09 >> ")
```

输入以下任一内容会退出：

```text
q
exit
空字符串
EOF
Ctrl+C
```

正常输入的处理顺序：

```text
触发 UserPromptSubmit Hook
  ↓
把用户消息加入 history
  ↓
调用 agent_loop(history)
  ↓
遍历最后一条消息中的 text block
  ↓
打印最终回答
```

`history` 在整个命令行会话中持续复用，因此当前会话的对话历史仍会直接发给主模型；`.memory` 解决的是跨程序运行、跨会话保存稳定信息的问题。

---

# 用一条 prompt 模拟完整 Memory 流转

## 示例 prompt

当前 `.memory` 中已经有两条记录：

- 用户喜欢苹果。
- 本项目代码使用 tab 缩进。

为了让一次请求在理想成功路径中触发召回、工具、提取、写入和 consolidation，可以输入：

```text
请创建 apple_example.py，写一个 Python 示例程序，输出我喜欢的水果，并遵循我之前的代码缩进偏好。

另外请长期记住以下项目约定，后续任务都继续使用：
1. Python 代码需要添加类型标注。
2. 文件路径优先使用 pathlib。
3. 测试框架统一使用 pytest。
4. 源文件统一使用 UTF-8。
5. Python 函数尽量保持在 40 行以内。
6. 对外公开函数需要编写 docstring。
7. 项目日志统一使用 logging，不直接使用 print。
8. 新增功能需要同时补充测试。
```

下面假设：

- 所有 API 调用成功。
- 记忆选择模型选中了现有两条相关记忆。
- 主模型决定调用一次 `write_file`。
- 提取模型把八条约定识别成八条独立的 persistent 记忆。
- 整理输入没有超过字符上限。

LLM 的输出具有不确定性，所以这是一条用于理解代码的理想执行轨迹，不是对每次实际输出的保证。

---

## 第 1 步：接收用户输入

主程序执行：

```python
trigger_hooks("UserPromptSubmit", query)
history.append({"role": "user", "content": query})
agent_loop(history)
```

终端先打印类似：

```text
[HOOK] UserPromptSubmit: working in C:\...\learn-claude-code
```

此时消息历史大致是：

```python
[
	{
		"role": "user",
		"content": "请创建 apple_example.py……",
	}
]
```

---

## 第 2 步：扫描现有记忆

`agent_loop()` 调用：

```python
relevant_memories = load_memories(messages)
```

`list_memory_files()` 得到类似：

```python
[
	{
		"filename": "indentation_style_tabs.md",
		"name": "indentation_style_tabs",
		"description": "User preference for code indentation in this project.",
		"type": "user",
		"body": "Use tabs for indentation.",
	},
	{
		"filename": "preference_apple.md",
		"name": "preference_apple",
		"description": "User preference for apple.",
		"type": "user",
		"body": "User likes apple.",
	},
]
```

---

## 第 3 步：第一次模型调用——选择相关记忆

`recent_user_text()` 取到当前 prompt，选择器只把现有记忆的名称和描述发给模型：

```text
0: indentation_style_tabs - User preference for code indentation in this project.
1: preference_apple - User preference for apple.
```

由于请求提到了“喜欢的水果”和“之前的代码缩进偏好”，理想返回是：

```json
[0, 1]
```

`extract_json_array()` 解析后，代码映射出：

```python
[
	"indentation_style_tabs.md",
	"preference_apple.md",
]
```

如果这次模型调用抛出异常，则改走关键词匹配降级路径。

---

## 第 4 步：读取完整记忆正文

程序依次执行：

```python
content = read_memory_file(filename)
```

第一次的 `content` 类似：

```markdown
---
name: indentation_style_tabs
description: User preference for code indentation in this project.
type: user
---

Use tabs for indentation.
```

第二次的 `content` 类似：

```markdown
---
name: preference_apple
description: User preference for apple.
type: user
---

User likes apple.
```

随后形成：

```python
loaded = [
	{"source": "indentation_style_tabs.md", "content": "完整 Markdown"},
	{"source": "preference_apple.md", "content": "完整 Markdown"},
]
```

并序列化为 JSON 字符串 `relevant_memories`。

---

## 第 5 步：构造主模型的 system prompt

`build_system()` 会加入：

- Agent 身份和工作目录。
- 记忆使用规则。
- `MEMORY.md` 中的完整目录。
- 本轮召回的两条完整记录。

主模型由此知道：

- 喜欢的水果是 apple。
- 代码应使用 tab 缩进。
- 这些信息只是背景；当前请求优先。

---

## 第 6 步：第二次模型调用——主 Agent 请求工具

主模型收到：

- `system`。
- 完整 `messages`。
- 五个工具定义。

它可能返回一个 `write_file` 工具调用：

```python
{
	"name": "write_file",
	"input": {
		"path": "apple_example.py",
		"content": (
			"def favorite_fruit() -> str:\n"
			"\treturn \"apple\"\n"
		),
	},
}
```

模型使用了两个召回结果：

- 返回 `apple`。
- 函数体使用 tab 缩进。

模型响应先作为 assistant 消息加入 `history`。

---

## 第 7 步：执行工具和 Hook

`execute_tool()` 首先触发 `PreToolUse`：

1. `permission_hook()` 检查写入路径是否位于工作区。
2. 通过后，`log_hook()` 打印工具调用预览。

随后映射到：

```python
run_write(path="apple_example.py", content="...")
```

成功后返回：

```text
Wrote N bytes to apple_example.py
```

再触发 `PostToolUse` 的 `large_output_hook()`。

工具结果被包装为：

```python
{
	"type": "tool_result",
	"tool_use_id": block.id,
	"content": "Wrote N bytes to apple_example.py",
}
```

并作为一条 `role: user` 消息加入历史。

---

## 第 8 步：第三次模型调用——生成最终回答

主循环再次调用模型。此时模型能看到：

- 自己先前请求了 `write_file`。
- 工具已经成功写入文件。

它可能输出：

```text
已创建 apple_example.py，程序返回 apple，并使用了 tab 缩进。
```

这条 assistant 响应加入 `history`。由于 `stop_reason` 不再是 `tool_use`，工具循环结束。

---

## 第 9 步：触发 Stop Hook

程序执行：

```python
force = trigger_hooks("Stop", messages)
```

`summary_hook()` 统计历史中的 `tool_result` block，输出类似：

```text
[HOOK] Stop: session used 1 tool calls
```

当前 `summary_hook()` 返回 `None`，所以继续结束流程。

如果某个 Stop Hook 返回字符串，代码会把该字符串作为新的用户消息加入历史，然后继续调用主模型。

---

## 第 10 步：第四次模型调用——提取长期记忆

`extract_memories(messages)` 使用最近对话构造独立的提取请求。

对话数据大致是：

```text
user: 请创建 apple_example.py……另外请长期记住以下项目约定……
assistant: 已创建 apple_example.py……
```

工具调用和工具结果通常不会出现在这里，因为它们不是 text block。

模型理想返回八条类似的记录：

```json
[
  {
    "name": "python_type_annotations",
    "type": "project",
    "scope": "persistent",
    "description": "Python code should include type annotations.",
    "body": "Add type annotations to Python code in this project."
  },
  {
    "name": "prefer_pathlib",
    "type": "project",
    "scope": "persistent",
    "description": "Use pathlib for filesystem paths.",
    "body": "Prefer pathlib when working with filesystem paths."
  },
  {
    "name": "pytest_framework",
    "type": "project",
    "scope": "persistent",
    "description": "The project uses pytest.",
    "body": "Use pytest as the testing framework."
  }
]
```

这里省略其余五条。

---

## 第 11 步：验证、去重、写入并重建索引

每条模型结果依次经过：

```text
validate_memory_record(require_scope=True)
  ↓
should_store_memory(candidate, existing_records)
  ↓
write_memory_file(...)
```

如果提取模型再次输出“喜欢苹果”或“使用 tab”，会因为已有同名、同描述或同正文而被拒绝。

八条新规则成功写入后，终端输出：

```text
[Memory: stored 8 records]
```

记忆总数从原来的 2 条变成 10 条。

---

## 第 12 步：第五次模型调用——合并整理记忆

因为本轮新增数量非零，所以调用：

```python
consolidate_memories()
```

当前记录数已经达到阈值 10，程序把所有记忆发给整理模型。

模型可能：

- 保持十条记录不变。
- 把若干 Python 编码约定合并成一条综合记录。
- 使用更新的信息纠正旧记录。

写入前保存快照，验证通过后删除旧记录并重写。如果最终从 10 条合并到 8 条，会输出：

```text
[Memory: consolidated 10 to 8 records]
```

如果重写过程失败，则从快照恢复旧记忆。

---

## 第 13 步：主程序打印回答

记忆选择、提取和整理使用的响应都不会加入主对话 `history`。

因此 `agent_loop()` 返回后，`history[-1]` 仍然是主 Agent 的最终回答。主程序遍历其中的 text block 并打印：

```text
已创建 apple_example.py，程序返回 apple，并使用了 tab 缩进。
```

---

## 这次示例中的模型调用次数

假设主模型只调用一次工具，总共发生五次模型 API 调用：

```text
① 记忆选择模型
   返回 [0, 1]

② 主 Agent 模型
   返回 write_file 工具调用

   Python 执行 write_file（这一步不是模型调用）

③ 主 Agent 模型
   读取工具结果，返回最终回答

④ 记忆提取模型
   返回 8 条 persistent 候选记忆

   Python 校验、去重、写入并重建索引

⑤ 记忆整理模型
   合并整理达到阈值的记忆库
```

如果主模型连续调用多轮工具，主 Agent 的 API 调用次数会相应增加。

---

## 正常路径和异常路径不能全部同时触发

“所有 Memory 机制”需要区分正常主路径和互斥的异常分支：

| 正常路径 | 对应异常／降级路径 |
| --- | --- |
| 模型成功选择相关记忆 | 选择 API 抛异常后使用关键词匹配 |
| 记忆提取成功 | 抛异常后跳过本轮提取 |
| consolidation 成功重写 | 重写失败后从快照回滚 |

因此，一次正常执行可以覆盖召回、注入、提取、持久化和整理，但不可能同时既“选择成功”又“走关键词降级”，也不可能既“整理成功”又“触发失败回滚”。

---

## 最值得记住的设计点

### 1. 目录和正文分层

先把轻量目录交给选择器，只有相关记录才加载完整正文，避免记忆越多时主上下文无限膨胀。

### 2. 召回和提取使用独立模型调用

主 Agent 不直接决定 Python 应该读写哪些记忆文件：

- 一个独立调用负责选择相关记录。
- 一个独立调用负责提取长期知识。
- 达到阈值后，再用一个独立调用负责合并整理。

### 3. 模型判断加本地硬校验

模型擅长判断语义是否值得保存，本地代码负责：

- 格式是否合法。
- scope 是否持久。
- 类型是否允许。
- 是否明显临时。
- 是否与已有记录重复。

### 4. 当前请求优先于记忆

旧记忆不能覆盖用户当前的明确要求。记忆只是背景，不是新的用户命令。

### 5. 新记忆下一轮才生效

召回发生在回答之前，提取发生在回答之后，所以本轮新信息不会反向改变已经生成的本轮回答。

### 6. 会话历史和跨会话记忆是两套机制

- `history`：当前进程中的完整对话上下文。
- `.memory`：写到磁盘、供未来会话选择性读取的稳定知识。

### 7. consolidation 不是每轮自动运行

只有本轮实际新增记忆，并且总数达到阈值，才会尝试整理。

---

## 可以继续改进的地方

这份代码适合教学，但如果用于更完整的 Agent，可以继续考虑：

- 批量写入多条记忆后只重建一次索引。
- 为记忆增加创建时间、更新时间和来源。
- 使用 embedding 或全文检索增强相关记忆选择。
- 对被截断的召回记录增加明确标记，避免注入半条 Markdown。
- 把 consolidation 改成分批或分类型处理，突破 20,000 字符限制。
- 使用更严格的结构化输出代替从自由文本中寻找 JSON 数组。
- 对写文件工具本身增加强制工作区边界，而不只依赖交互式 Hook。
- 为记忆更新和用户纠错设计显式版本规则。
- 为写入与索引重建提供真正的文件级事务或临时文件原子替换。

---

## 最终总结

s09 的核心不是“多存几个 Markdown 文件”，而是建立了一条完整的记忆生命周期：

```text
发现相关旧知识
  ↓
按需召回并约束其优先级
  ↓
完成当前任务
  ↓
识别新的长期知识
  ↓
过滤临时信息和重复内容
  ↓
持久化并维护索引
  ↓
在规模增长后进行合并整理
```

这使 Agent 同时具备两种能力：

- 当前会话里依靠 `history` 连续对话。
- 不同会话之间依靠 `.memory` 保留稳定偏好和项目知识。
