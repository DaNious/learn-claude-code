# s14 学习笔记：MCP 工具发现、动态注册与调用

## 1. 本章目标

`s14_mcp_plugin/code.py` 实现了一个带 MCP 工具发现能力的简化版编程 Agent。它在前面章节已有的基础工具、Agent 循环和 Hook 机制上，增加了以下能力：

1. Agent 一开始只知道一个 `connect_mcp` 工具。
2. 模型可以通过 `connect_mcp` 选择并连接 MCP Server。
3. 宿主程序从服务器发现工具，并在下一轮模型请求中动态加入工具池。
4. MCP 工具统一转换成 `mcp__服务器名__工具名` 的形式。
5. 模型发起 MCP 工具调用后，由宿主程序找到对应服务器和原始工具并执行。
6. MCP 工具的授权由宿主程序控制，而不是由服务器的描述或 annotations 决定。

最重要的程序主线是：

```text
用户输入任务
    ↓
模型看到内置工具
    ↓
模型调用 connect_mcp("docs")
    ↓
宿主创建并登记 docs MCPClient
    ↓
下一轮重新组装工具池
    ↓
模型看到 mcp__docs__search 等动态工具
    ↓
模型调用动态 MCP 工具
    ↓
宿主将调用还原成 docs/search 并交给 MCPClient
    ↓
工具结果作为 tool_result 返回模型
    ↓
模型继续调用工具或生成最终答案
```

需要特别说明：本章代码没有通过 stdio、SSE 或 HTTP 连接真正独立运行的 MCP Server。`MCPClient` 和两个服务器都在同一个 Python 进程中，是对 MCP `tools/list` 和 `tools/call` 两个核心概念的教学模拟。

---

## 2. 运行条件与启动配置

文件开头给出的运行方式是：

```bash
python s14_mcp_plugin/code.py
```

需要安装：

```bash
pip install anthropic python-dotenv
```

`.env` 至少需要提供模型及 API 认证所需的环境变量，例如：

```dotenv
ANTHROPIC_API_KEY=...
MODEL_ID=...
```

如果使用兼容 Anthropic API 的自定义服务，还可以配置：

```dotenv
ANTHROPIC_BASE_URL=...
```

程序启动时执行：

```python
load_dotenv(override=True)
```

它读取 `.env`，并用其中的值覆盖已有环境变量。

如果配置了 `ANTHROPIC_BASE_URL`，代码会移除可能冲突的 `ANTHROPIC_AUTH_TOKEN`：

```python
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
```

随后初始化三个关键对象：

```python
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

- `WORKDIR` 是 Agent 操作文件和运行命令时使用的工作目录。
- `client` 是 Anthropic SDK 客户端。
- `MODEL` 是每次调用 `client.messages.create()` 时使用的模型。

`MODEL_ID` 使用 `os.environ[...]` 读取，因此缺少这个环境变量时，程序会在启动阶段直接抛出 `KeyError`。

基础系统提示词是：

```python
BASE_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use built-in and connected MCP "
    "tools to solve tasks. Call connect_mcp before using a server."
)
```

其中明确告诉模型：必须先连接某个 MCP Server，之后才能使用该服务器的工具。

---

## 3. 工具系统的两层结构

这份代码中的每个工具都分成两层：

```text
工具定义（tool definition）
    给模型看：名称、描述、参数 Schema

工具处理器（handler）
    给宿主程序用：实际执行操作的 Python 函数
```

例如，模型看到的 bash 工具定义是：

```python
{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}
```

真正执行命令的则是：

```python
def run_bash(command: str) -> str:
    ...
```

二者通过 handler 字典关联：

```python
BASE_HANDLERS = {
    "bash": run_bash,
    ...
}
```

可以把工具定义理解为“菜单”，handler 理解为“后厨实现”。模型只能根据菜单提出调用请求，不能直接执行 Python 函数；真正的执行权始终在宿主程序手中。

---

## 4. 基础工具概览

基础工具来自前面章节，本章只需掌握它们如何进入统一工具池。

### 4.1 `run_bash(command)`

使用 `subprocess.run()` 在 `WORKDIR` 下执行 shell 命令：

```python
result = subprocess.run(
    command,
    shell=True,
    cwd=WORKDIR,
    capture_output=True,
    text=True,
    timeout=120,
)
```

主要行为：

- 最长运行 120 秒。
- 合并标准输出和标准错误。
- 返回内容最多保留 50,000 个字符。
- 非零退出码被包装成错误字符串。
- 超时和 `OSError` 也会被转换为错误字符串。

因为使用了 `shell=True`，字符串会交给 shell 解释，所以后面的权限 Hook 很重要。

### 4.2 `run_read(path, limit=None)`

读取 UTF-8 文本文件。如果设置 `limit`，只返回前 `limit` 行，并在末尾说明还有多少行未返回。

### 4.3 `run_write(path, content)`

必要时创建父目录，然后以 UTF-8 覆盖写入文件。

### 4.4 `run_edit(path, old_text, new_text)`

执行精确文本替换，但要求 `old_text` 恰好出现一次：

```python
count = content.count(old_text)
if count != 1:
    return f"Error: Expected 1 occurrence, found {count}"
```

这样可以避免目标不存在或一次误改多个位置。

### 4.5 `run_glob(pattern)`

根据 glob 表达式搜索文件，最多返回 200 个结果，并过滤掉解析后不在 `WORKDIR` 内的路径。

### 4.6 基础工具表和 handler 表

```python
BASE_TOOLS = [...]
BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}
```

`BASE_TOOLS` 被发送给模型，`BASE_HANDLERS` 被 `execute_tool()` 用来实际执行工具。

---

## 5. `MCPClient`：进程内的 MCP 模拟客户端

```python
class MCPClient:
    """Small in-process stand-in for MCP tools/list and tools/call."""
```

每个 `MCPClient` 代表一个已经连接的服务器，内部保存：

```python
self.name = name
self.tools: list[dict] = []
self._handlers: dict[str, callable] = {}
```

- `name`：服务器名称，例如 `docs`。
- `tools`：服务器公开的工具定义，模拟 MCP 的 `tools/list` 返回值。
- `_handlers`：服务器内部实际实现，模拟 MCP 的 `tools/call` 执行端。

### 5.1 `register(tool_defs, handlers)`

注册服务器工具前进行三类校验：

1. 每个工具都必须有非空字符串名称。
2. 同一个服务器不能出现重复工具名。
3. 每个工具定义必须有对应 handler。

校验通过后复制工具定义和 handler：

```python
self.tools = list(tool_defs)
self._handlers = dict(handlers)
```

这里复制容器可以避免调用方之后直接修改原始 list 或 dict 时意外影响客户端内部状态。

### 5.2 `call_tool(tool_name, args)`

调用流程为：

```python
handler = self._handlers.get(tool_name)
```

找不到时返回：

```text
MCP error: unknown tool '...'
```

找到后执行：

```python
handler(**args)
```

例如：

```python
call_tool("search", {"query": "MCP"})
```

等价于：

```python
search_handler(query="MCP")
```

handler 抛出的异常不会继续向上冒泡，而是被包装成：

```text
MCP error: TypeError: ...
```

---

## 6. MCP 状态与宿主权限策略

程序使用两个全局字典保存 MCP 状态：

```python
mcp_clients: dict[str, MCPClient] = {}
mcp_tool_policies: dict[str, str] = {}
```

### 6.1 `mcp_clients`

保存已经连接的服务器：

```python
{
    "docs": <MCPClient docs>,
    "deploy": <MCPClient deploy>,
}
```

### 6.2 `mcp_tool_policies`

保存已经转换为模型工具名之后的权限：

```python
{
    "mcp__docs__search": "allow",
    "mcp__docs__get_version": "allow",
    "mcp__deploy__status": "allow",
    "mcp__deploy__trigger": "confirm",
}
```

权限来源是宿主配置：

```python
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}
```

设计原则是：

> MCP Server 可以描述工具，但不能自己给自己授权。最终权限由运行 Agent 的宿主程序决定。

例如 `deploy/trigger` 的定义带有：

```python
"annotations": {"destructiveHint": True}
```

但代码并不直接根据这个 annotation 授权。真正让它要求确认的是：

```python
("deploy", "trigger"): "confirm"
```

未出现在 `MCP_HOST_POLICY` 中的 MCP 工具默认使用 `confirm`，采取保守策略。

---

## 7. MCP 名称规范化与命名空间

模型工具名只允许一组有限字符。代码用正则表达式处理服务器名和工具名：

```python
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
```

```python
def normalize_mcp_name(name: str) -> str:
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized
```

例如：

```text
my.docs/server → my_docs_server
```

连接工具被暴露给模型时，最终名称采用：

```text
mcp__<server>__<tool>
```

例如：

```text
docs/search        → mcp__docs__search
docs/get_version   → mcp__docs__get_version
deploy/status      → mcp__deploy__status
deploy/trigger     → mcp__deploy__trigger
```

增加服务器前缀可以避免不同服务器都拥有 `search`、`status` 等常见名称时发生冲突。

由于不同原始名称经过规范化后可能变成同一个名称，`assemble_tool_pool()` 还会记录 `origins` 并检查冲突。例如 `a.b` 和 `a/b` 都会规范化成 `a_b`，这种情况会抛出异常，而不是静默覆盖已有工具。

最终工具名超过 64 个字符时也会报错。

---

## 8. 两个模拟 MCP Server

### 8.1 docs Server

`_mock_server_docs()` 创建：

```python
server = MCPClient("docs")
```

然后注册两个工具。

#### `search(query)`

定义：

```python
{
    "name": "search",
    "description": "Search the documentation.",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "annotations": {"readOnlyHint": True},
}
```

实现：

```python
lambda query: f"[docs] Found 3 results for '{query}'"
```

#### `get_version()`

实现：

```python
lambda: "[docs] API v2.1.0"
```

这里没有真正查询文档服务，只返回固定格式的教学数据。

### 8.2 deploy Server

`_mock_server_deploy()` 创建 `MCPClient("deploy")`，并注册：

#### `trigger(service)`

```python
lambda service: f"[deploy] Triggered: {service}"
```

它被标注为可能具有破坏性，并由宿主策略设置为 `confirm`。

#### `status(service)`

```python
lambda service: f"[deploy] {service}: running (v1.4.2)"
```

它是只读操作，宿主策略为 `allow`。

### 8.3 延迟创建服务器

`MOCK_SERVERS` 保存的是工厂函数，而不是已创建的对象：

```python
MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}
```

只有调用 `connect_mcp()` 时才会执行对应工厂函数。这种方式使服务器按需初始化。

---

## 9. `connect_mcp()`：连接和发现入口

模型实际可调用的定义是：

```python
CONNECT_TOOL = {
    "name": "connect_mcp",
    "description": "Connect to an MCP server and discover its tools.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": ["docs", "deploy"],
            }
        },
        "required": ["name"],
    },
}
```

模型只能从 `docs` 和 `deploy` 中选择。handler 映射是：

```python
BUILTIN_HANDLERS = {
    **BASE_HANDLERS,
    "connect_mcp": run_connect_mcp,
}
```

实际调用链：

```text
模型调用 connect_mcp(name="docs")
    ↓
execute_tool()
    ↓
run_connect_mcp("docs")
    ↓
connect_mcp("docs")
```

`connect_mcp()` 的步骤是：

1. 检查服务器是否已经连接。
2. 从 `MOCK_SERVERS` 找到对应工厂函数。
3. 调用工厂函数创建并注册服务器工具。
4. 把服务器放入 `mcp_clients`。
5. 返回发现的工具数量和名称。

核心代码：

```python
factory = MOCK_SERVERS.get(name)
server = factory()
mcp_clients[name] = server
```

重复连接时直接返回：

```text
MCP server 'docs' already connected
```

不会重复创建服务器对象。

如果直接调用 Python 函数并传入未知名称，会返回可用服务器列表。不过正常模型调用受 `enum` 限制，通常不会生成未知名称。

---

## 10. `assemble_tool_pool()`：本章核心

该函数在每次调用模型之前运行：

```python
def assemble_tool_pool() -> tuple[list[dict], dict[str, callable]]:
```

它返回：

1. 当前要发送给模型的完整工具定义列表。
2. 当前工具名到可执行 handler 的完整映射。

### 10.1 从内置工具开始

```python
tools = list(BUILTIN_TOOLS)
handlers = dict(BUILTIN_HANDLERS)
```

使用浅复制，避免本轮动态追加 MCP 工具时直接修改全局的内置工具集合。

### 10.2 遍历已经连接的服务器

```python
for server_name, server in mcp_clients.items():
```

程序刚启动时 `mcp_clients` 为空，因此初始工具池只有：

```text
bash
read_file
write_file
edit_file
glob
connect_mcp
```

连接服务器之后，下一轮才会进入下面的动态转换逻辑。

### 10.3 构造模型侧名称

```python
safe_server = normalize_mcp_name(server_name)
safe_tool = normalize_mcp_name(raw_name)
prefixed = f"mcp__{safe_server}__{safe_tool}"
```

原始的 `docs/search` 被转换为 `mcp__docs__search`。

### 10.4 转换 Schema 字段名

MCP 工具定义使用：

```python
"inputSchema"
```

Anthropic 工具接口使用：

```python
"input_schema"
```

因此代码执行适配：

```python
schema = tool_def.get("inputSchema", {})
tools.append({
    "name": prefixed,
    "description": tool_def.get("description", ""),
    "input_schema": schema,
})
```

Schema 必须是字典，并且顶层类型必须为 `object`。否则抛出 `ValueError`。

### 10.5 创建动态 handler

```python
handlers[prefixed] = (
    lambda *, client=server, tool=raw_name, **kwargs:
    client.call_tool(tool, kwargs)
)
```

例如模型调用：

```text
mcp__docs__search(query="MCP")
```

动态 handler 会将它还原为：

```python
docs_client.call_tool("search", {"query": "MCP"})
```

lambda 中的：

```python
client=server, tool=raw_name
```

是有意使用的默认参数。它们在创建 lambda 时立即捕获当前循环中的服务器和工具名，避免 Python 闭包的延迟绑定问题。如果直接引用循环变量，所有 lambda 最后可能都指向循环中的最后一个服务器和最后一个工具。

### 10.6 设置宿主权限

```python
policies[prefixed] = MCP_HOST_POLICY.get(
    (server_name, raw_name), "confirm"
)
```

组装完成后，整体替换全局策略：

```python
mcp_tool_policies = policies
```

每一轮都根据当前连接状态重新计算，因此断开的、改变的或新增的服务器不会遗留旧策略。当前示例没有实现断开连接，但这种重建方式为状态变化提供了基础。

---

## 11. 动态系统提示词

`assemble_system_prompt()` 与动态工具池配套：

```python
def assemble_system_prompt() -> str:
    if not mcp_clients:
        return BASE_SYSTEM
    return BASE_SYSTEM + "\n\nConnected MCP servers: " + ", ".join(mcp_clients)
```

连接前：

```text
You are a coding agent ... Call connect_mcp before using a server.
```

连接后：

```text
You are a coding agent ... Call connect_mcp before using a server.

Connected MCP servers: docs, deploy
```

工具定义告诉模型“具体有哪些工具”，系统提示词补充告诉模型“哪些服务器已经连接”。

---

## 12. Hook 与权限检查简述

Hook 分为四类：

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
```

注册关系是：

```text
UserPromptSubmit → context_hook
PreToolUse       → permission_hook → log_hook
PostToolUse      → large_output_hook
Stop             → summary_hook
```

`trigger_hooks()` 按注册顺序调用回调。如果某个回调返回非 `None`，会立即返回，不再执行后面的 Hook。

因此 `PreToolUse` 中如果 `permission_hook()` 拒绝了操作，后面的 `log_hook()` 也不会运行。

### 12.1 bash 权限

- 命中 `DENY_LIST`：直接拒绝。
- 命中 `DESTRUCTIVE`：询问用户。

### 12.2 文件路径权限

`read_file`、`write_file`、`edit_file` 操作解析后位于 `WORKDIR` 外的路径时询问用户。

### 12.3 MCP 权限

```python
if block.name.startswith("mcp__"):
    policy = mcp_tool_policies.get(block.name, "confirm")
```

策略不是 `allow` 时打印调用信息，并要求用户输入 `y` 或 `yes`。其他输入均视为拒绝。

---

## 13. `execute_tool()`：统一工具执行入口

无论基础工具还是动态 MCP 工具，都经过：

```python
def execute_tool(block, handlers: dict[str, callable]) -> str:
```

完整执行顺序：

```text
收到模型的 tool_use block
    ↓
触发 PreToolUse Hooks
    ↓
若返回拒绝原因，直接作为工具结果返回
    ↓
根据 block.name 查找 handler
    ↓
执行 handler(**block.input)
    ↓
将异常转换成错误字符串
    ↓
触发 PostToolUse Hooks
    ↓
返回工具输出
```

Anthropic SDK 的工具调用 block 主要包含：

```text
block.id       本次调用的唯一 ID
block.name     工具名称
block.input    模型生成的参数字典
block.type     tool_use
```

如果 handler 字典中不存在对应名称，返回：

```text
Unknown tool: <name>
```

---

## 14. `agent_loop()`：模型与工具之间的循环

核心循环：

```python
def agent_loop(messages: list):
    while True:
```

每轮按下面的顺序运行。

### 14.1 重新组装工具和系统提示词

```python
tools, handlers = assemble_tool_pool()
response = client.messages.create(
    model=MODEL,
    system=assemble_system_prompt(),
    messages=messages,
    tools=tools,
    max_tokens=8000,
)
```

之所以每轮都重新组装，是因为上一轮可能刚刚调用过 `connect_mcp`，连接状态已经变化。

### 14.2 保存 Assistant 响应

```python
messages.append({
    "role": "assistant",
    "content": response.content,
})
```

`response.content` 可能同时包含文本块和一个或多个 `tool_use` 块。

### 14.3 找出所有工具调用

```python
tool_calls = [
    block for block in response.content
    if block.type == "tool_use"
]
```

如果没有工具调用，说明模型已经给出最终回答：

```python
if not tool_calls:
    trigger_hooks("Stop", messages)
    return
```

### 14.4 执行工具并构造结果

每个工具调用都经过 `execute_tool()`。返回结果被包装成：

```python
{
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
}
```

`tool_use_id` 用于告诉模型：这个结果对应哪一个先前发出的工具调用。

所有结果合并成一条新的 user 消息：

```python
messages.append({
    "role": "user",
    "content": results,
})
```

这里的 `role: user` 并不表示终端用户又输入了一句话，而是 Anthropic 工具协议规定：工具结果作为 user role 的内容块送回模型。

然后 `while True` 开始下一轮，模型可以基于工具结果继续调用工具或输出最终答案。

### 14.5 API 异常处理

模型请求或动态工具组装抛出异常时，代码会向历史添加一条 Assistant 错误文本，触发 `Stop` Hook，然后结束本轮 Agent 循环。

---

## 15. 主程序与多轮会话

入口：

```python
if __name__ == "__main__":
```

程序创建：

```python
history = []
```

该列表会在终端会话期间持续存在，因此保存：

- 用户之前的问题；
- 模型回复；
- 工具调用；
- 工具结果。

`mcp_clients` 同样是进程级全局状态，因此连接一次之后，后续用户问题仍可继续使用对应 MCP 工具。

每次输入的处理顺序：

```python
query = input("s14 >> ")
trigger_hooks("UserPromptSubmit", query)
history.append({"role": "user", "content": query})
agent_loop(history)
```

`agent_loop()` 返回后，入口代码打印最后一条消息中的文本块。

以下输入会退出程序：

```text
q
exit
空字符串
Ctrl+C
EOF
```

---

## 16. 使用一条 Prompt 模拟完整程序流转

下面的 prompt 可以覆盖两个 MCP Server 的连接、动态发现、四个 MCP 工具调用、重复连接分支，以及五个基础工具的正常路径。

```text
请严格按以下阶段完成任务，不要跳过任何一步；每完成一个阶段后再进入下一阶段：

1. 使用 glob 查找 s14_mcp_plugin 下的 Python 文件，读取 s14_mcp_plugin/code.py 的前 20 行；执行 bash 命令 `python --version`；创建文件 s14_mcp_plugin/_flow_demo.txt，内容为 `version=1`，然后把它精确修改为 `version=2`，最后重新读取确认。
2. 分别调用 connect_mcp 连接 docs 和 deploy 两个 MCP 服务器。
3. 连接完成后，依次调用：
   - mcp__docs__get_version
   - mcp__docs__search，query 为 `MCP dynamic tool discovery`
   - mcp__deploy__status，service 为 `learn-claude-code`
   - mcp__deploy__trigger，service 为 `learn-claude-code`
4. 再调用一次 connect_mcp("docs")，确认重复连接时的行为。
5. 最后不要再调用工具，用中文汇总每一步及所有工具返回结果。
```

说明：模型输出具有一定非确定性，它可能将同一阶段的多个工具调用放在一轮中，也可能拆成多轮。但只要它遵守任务要求，程序内部的连接、重建工具池和 MCP 分发机制不变。

`deploy.trigger` 的宿主策略是 `confirm`。终端出现：

```text
Allow? [y/N]
```

时必须输入 `y`，否则 `trigger` 的 MCP handler 不会真正执行。这次 `y` 是权限确认，不是第二条任务 prompt。

---

## 17. 模拟流转第 0 阶段：程序启动

Python 从上到下执行模块级代码：

```text
加载 .env
    ↓
创建 Anthropic client
    ↓
读取 MODEL_ID
    ↓
定义基础工具函数
    ↓
定义 MCPClient 和模拟服务器工厂
    ↓
创建内置工具定义及 handler 映射
    ↓
注册 Hooks
    ↓
进入 __main__
```

初始状态：

```python
mcp_clients == {}
mcp_tool_policies == {}
```

模型初始可见工具：

```text
bash
read_file
write_file
edit_file
glob
connect_mcp
```

此时没有任何 `mcp__...` 工具。

---

## 18. 模拟流转第 1 阶段：接收 Prompt

用户粘贴 prompt 后，入口执行：

```python
trigger_hooks("UserPromptSubmit", query)
```

触发 `context_hook()`，可能输出：

```text
[hook] UserPromptSubmit: working in C:\Users\songx\Codes\Github\learn-claude-code
```

然后将用户消息加入历史：

```python
history = [
    {
        "role": "user",
        "content": "请严格按以下阶段完成任务……",
    }
]
```

接着调用：

```python
agent_loop(history)
```

---

## 19. 模拟流转第 2 阶段：第一轮模型请求

第一轮开头执行：

```python
tools, handlers = assemble_tool_pool()
```

因为此时 `mcp_clients` 为空，得到：

```python
tools = [
    bash,
    read_file,
    write_file,
    edit_file,
    glob,
    connect_mcp,
]
```

对应 handlers：

```python
handlers = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "connect_mcp": run_connect_mcp,
}
```

`assemble_system_prompt()` 只返回 `BASE_SYSTEM`，因为尚未连接任何服务器。

随后程序把用户消息、系统提示词和六个内置工具发送给模型。

---

## 20. 模拟流转第 3 阶段：基础工具调用

模型根据 prompt 发出基础工具调用，概念上类似：

```text
glob(pattern="s14_mcp_plugin/*.py")

read_file(
    path="s14_mcp_plugin/code.py",
    limit=20
)

bash(command="python --version")

write_file(
    path="s14_mcp_plugin/_flow_demo.txt",
    content="version=1"
)

edit_file(
    path="s14_mcp_plugin/_flow_demo.txt",
    old_text="version=1",
    new_text="version=2"
)

read_file(path="s14_mcp_plugin/_flow_demo.txt")
```

这些调用都经过统一路径：

```text
execute_tool
    ↓
PreToolUse Hooks
    ↓
基础 handler
    ↓
PostToolUse Hooks
    ↓
tool_result
```

可能产生：

```text
s14_mcp_plugin/code.py
Python 3.x.x
Wrote 9 bytes to s14_mcp_plugin/_flow_demo.txt
Edited s14_mcp_plugin/_flow_demo.txt
version=2
```

这部分沿用前面章节的机制，本章重点是后续 MCP 状态变化。

---

## 21. 模拟流转第 4 阶段：连接 docs

模型调用：

```text
connect_mcp(name="docs")
```

完整调用链：

```text
execute_tool(block, handlers)
    ↓
trigger_hooks("PreToolUse", block)
    ↓
handlers["connect_mcp"]
    ↓
run_connect_mcp("docs")
    ↓
connect_mcp("docs")
```

`connect_mcp()` 先查找：

```python
factory = MOCK_SERVERS.get("docs")
```

得到 `_mock_server_docs`，随后执行：

```python
server = factory()
```

工厂内部创建：

```python
server = MCPClient("docs")
```

新对象初始状态：

```python
server.name == "docs"
server.tools == []
server._handlers == {}
```

调用 `server.register()` 后，`server.tools` 中有 `search` 和 `get_version` 的 MCP 工具定义，`server._handlers` 中有二者的 Python lambda 实现。

然后连接被保存：

```python
mcp_clients["docs"] = server
```

终端输出类似：

```text
[mcp] connected: docs -> search, get_version
Connected to MCP server 'docs'. Discovered 2 tools: search, get_version
```

此时：

```python
mcp_clients = {
    "docs": <MCPClient docs>,
}
```

关键点：虽然 `docs` 已加入 `mcp_clients`，但当前这一轮的 `tools` 和 `handlers` 是调用模型之前组装的，不会原地自动增加 `mcp__docs__...` 工具。动态工具要到下一次 `while True` 循环重新组装后才会出现。

---

## 22. 模拟流转第 5 阶段：连接 deploy

模型调用：

```text
connect_mcp(name="deploy")
```

调用链与 docs 相同：

```text
execute_tool
    ↓
run_connect_mcp("deploy")
    ↓
connect_mcp("deploy")
    ↓
_mock_server_deploy()
    ↓
MCPClient("deploy")
    ↓
server.register(...)
```

注册 `trigger` 和 `status` 后保存：

```python
mcp_clients["deploy"] = server
```

终端输出：

```text
[mcp] connected: deploy -> trigger, status
Connected to MCP server 'deploy'. Discovered 2 tools: trigger, status
```

连接状态变成：

```python
mcp_clients = {
    "docs": <MCPClient docs>,
    "deploy": <MCPClient deploy>,
}
```

第一轮产生的所有工具输出都被包装成 `tool_result`：

```python
{
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
}
```

并以一条 user role 消息加入历史。随后 `agent_loop` 开始下一轮。

---

## 23. 模拟流转第 6 阶段：重新组装动态工具池

新一轮再次执行：

```python
tools, handlers = assemble_tool_pool()
```

这次 `mcp_clients` 中有 docs 和 deploy，所以遍历两个服务器。

以 `docs/search` 为例：

```python
safe_server = normalize_mcp_name("docs")
safe_tool = normalize_mcp_name("search")
prefixed = "mcp__docs__search"
```

然后将 MCP 的 `inputSchema` 转换成模型 API 使用的 `input_schema`：

```python
{
    "name": "mcp__docs__search",
    "description": "Search the documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
}
```

并创建动态 handler：

```python
handlers["mcp__docs__search"] = (
    lambda **kwargs:
    docs_client.call_tool("search", kwargs)
)
```

四个动态工具全部生成后，模型看到的完整工具池是：

```text
bash
read_file
write_file
edit_file
glob
connect_mcp
mcp__docs__search
mcp__docs__get_version
mcp__deploy__trigger
mcp__deploy__status
```

权限表为：

```python
{
    "mcp__docs__search": "allow",
    "mcp__docs__get_version": "allow",
    "mcp__deploy__trigger": "confirm",
    "mcp__deploy__status": "allow",
}
```

系统提示词也追加：

```text
Connected MCP servers: docs, deploy
```

然后程序使用扩展后的工具池请求模型。

---

## 24. 模拟流转第 7 阶段：调用 docs/get_version

模型发出：

```text
mcp__docs__get_version({})
```

执行过程：

```text
execute_tool
    ↓
permission_hook
    ↓
mcp_tool_policies["mcp__docs__get_version"] == "allow"
    ↓
动态 handler
    ↓
docs_client.call_tool("get_version", {})
    ↓
docs_client._handlers["get_version"]
    ↓
lambda()
```

返回：

```text
[docs] API v2.1.0
```

名称在这一过程中经历：

```text
模型侧名称：mcp__docs__get_version
    ↓ 动态 handler
服务器原始名称：get_version
    ↓ MCPClient.call_tool
Python 实现：lambda: "[docs] API v2.1.0"
```

---

## 25. 模拟流转第 8 阶段：调用 docs/search

模型发出：

```text
mcp__docs__search({
    "query": "MCP dynamic tool discovery"
})
```

执行链：

```text
execute_tool
    ↓
permission_hook：allow
    ↓
handlers["mcp__docs__search"]
    ↓
docs_client.call_tool(
    "search",
    {"query": "MCP dynamic tool discovery"}
)
    ↓
handler(**args)
```

`handler(**args)` 等价于：

```python
search_handler(query="MCP dynamic tool discovery")
```

返回：

```text
[docs] Found 3 results for 'MCP dynamic tool discovery'
```

---

## 26. 模拟流转第 9 阶段：调用 deploy/status

模型发出：

```text
mcp__deploy__status({
    "service": "learn-claude-code"
})
```

宿主策略为：

```python
("deploy", "status"): "allow"
```

因此无需询问，直接执行：

```python
deploy_client.call_tool(
    "status",
    {"service": "learn-claude-code"},
)
```

返回：

```text
[deploy] learn-claude-code: running (v1.4.2)
```

---

## 27. 模拟流转第 10 阶段：调用 deploy/trigger

模型发出：

```text
mcp__deploy__trigger({
    "service": "learn-claude-code"
})
```

`permission_hook()` 查找：

```python
mcp_tool_policies["mcp__deploy__trigger"]
```

得到 `confirm`，所以终端显示：

```text
[permission] External tool mcp__deploy__trigger(
    {'service': 'learn-claude-code'}
)
Allow? [y/N]
```

输入：

```text
y
```

权限 Hook 返回 `None`，执行继续：

```text
动态 handler
    ↓
deploy_client.call_tool(
    "trigger",
    {"service": "learn-claude-code"}
)
    ↓
deploy_client._handlers["trigger"]
    ↓
lambda(service="learn-claude-code")
```

返回：

```text
[deploy] Triggered: learn-claude-code
```

如果用户没有输入 `y` 或 `yes`，`permission_hook()` 会返回：

```text
Permission denied by user
```

这个字符串会直接作为工具结果交给模型，真正的 MCP handler 不会执行。

---

## 28. 模拟流转第 11 阶段：重复连接 docs

模型再次调用：

```text
connect_mcp(name="docs")
```

`connect_mcp()` 最开始检查：

```python
if name in mcp_clients:
    return f"MCP server '{name}' already connected"
```

由于 docs 已存在，直接返回：

```text
MCP server 'docs' already connected
```

这次不会再次调用 `_mock_server_docs()`、`MCPClient()` 或 `register()`，原有连接和动态工具保持不变。

---

## 29. 模拟流转第 12 阶段：模型生成最终总结

MCP 工具结果被加入消息历史后，`agent_loop()` 再次请求模型。这一轮依然会重新组装完整动态工具池，但模型根据 prompt 的第 5 步不再调用工具，只输出总结文本，例如：

```text
已完成完整流程：

1. 找到并读取了 s14_mcp_plugin/code.py。
2. Python 版本为 Python 3.x.x。
3. 测试文件已从 version=1 修改为 version=2。
4. 已连接 docs 和 deploy MCP 服务器。
5. docs API 版本为 v2.1.0。
6. 文档搜索返回 3 条模拟结果。
7. learn-claude-code 当前正在运行 v1.4.2。
8. 经用户确认后触发了部署。
9. 再次连接 docs 时返回 already connected。
```

程序检查：

```python
tool_calls = []
```

于是执行：

```python
trigger_hooks("Stop", messages)
return
```

`summary_hook()` 统计消息历史中的 `tool_result` 块并打印工具调用次数。

回到 `__main__` 后，程序打印最后一条 Assistant 消息中的文本，然后显示下一次输入提示：

```text
s14 >>
```

---

## 30. 消息历史在完整流程中的变化

消息历史大致按以下结构增长：

```python
[
    # 1. 真实终端用户输入
    {
        "role": "user",
        "content": "请严格按以下阶段完成任务……",
    },

    # 2. 模型发出基础工具和 connect_mcp 调用
    {
        "role": "assistant",
        "content": [
            ToolUseBlock(...),
            ToolUseBlock(...),
        ],
    },

    # 3. 宿主返回第一批工具结果
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "...",
                "content": "...",
            },
        ],
    },

    # 4. 模型看到动态 MCP 工具并调用
    {
        "role": "assistant",
        "content": [
            ToolUseBlock(
                name="mcp__docs__search",
                input={"query": "..."},
            ),
        ],
    },

    # 5. 宿主返回 MCP 工具结果
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "...",
                "content": "[docs] Found 3 results ...",
            },
        ],
    },

    # 6. 模型最终回答
    {
        "role": "assistant",
        "content": [
            TextBlock(text="已完成完整流程……"),
        ],
    },
]
```

这里要区分两种 user role：

1. 第一条是终端用户真正输入的自然语言。
2. 后续包含 `tool_result` 的 user 消息是宿主程序按照 API 协议自动构造的。

---

## 31. 为什么连接后要到下一轮才能使用 MCP 工具

这是本章最关键的时序问题。

第一轮请求模型之前已经执行：

```python
tools, handlers = assemble_tool_pool()
```

当时还没有连接 docs，因此这一轮局部变量 `tools` 中不存在 `mcp__docs__search`。

模型在这一轮调用 `connect_mcp("docs")` 后，只改变了全局状态：

```python
mcp_clients["docs"] = server
```

它不会回头修改已经发送给模型的工具列表。

只有工具结果送回模型、`while True` 进入下一轮后，程序再次执行：

```python
tools, handlers = assemble_tool_pool()
```

新的工具池才会包含 docs 工具。

完整时序：

```text
第 1 轮组装 tools
    此时没有 docs 工具
        ↓
模型调用 connect_mcp("docs")
        ↓
mcp_clients 增加 docs
        ↓
第 1 轮工具结果返回模型
        ↓
第 2 轮重新组装 tools
        ↓
加入 mcp__docs__search 和 mcp__docs__get_version
        ↓
模型现在才能调用 docs 工具
```

---

## 32. MCP 调用的三层名称

一次 MCP 工具调用可以分成三层：

```text
模型侧工具名
mcp__docs__search
        ↓
MCP Server 原始工具名
search
        ↓
Python handler
lambda query: ...
```

每层职责不同：

| 层次 | 示例 | 用途 |
|---|---|---|
| 模型工具名 | `mcp__docs__search` | 避免跨服务器重名，满足模型工具命名要求 |
| MCP 原始名 | `search` | 服务器自己的工具标识 |
| Python handler | `lambda query: ...` | 本示例中真正执行工具的代码 |

动态 lambda 是模型工具名和 MCP 原始工具名之间的适配器。

---

## 33. MCP 工具定义的转换

MCP Server 中的原始定义：

```python
{
    "name": "search",
    "description": "Search the documentation.",
    "inputSchema": {...},
    "annotations": {"readOnlyHint": True},
}
```

发送给 Anthropic 模型的定义：

```python
{
    "name": "mcp__docs__search",
    "description": "Search the documentation.",
    "input_schema": {...},
}
```

转换内容包括：

1. 名称增加服务器命名空间。
2. `inputSchema` 改为 `input_schema`。
3. description 被保留。
4. annotations 没有直接传入当前模型工具定义。
5. 权限被独立写入 `mcp_tool_policies`。

这体现了一个适配层的作用：外部协议中的工具描述不能不经处理就直接交给模型或执行器。

---

## 34. 错误和边界情况

### 34.1 未知服务器

直接调用：

```python
connect_mcp("unknown")
```

返回：

```text
Unknown server 'unknown'. Available: docs, deploy
```

正常模型调用受 `CONNECT_TOOL` 的 enum 限制，但宿主函数本身仍做了防御性检查。

### 34.2 重复连接

重复连接同一个服务器不会重复注册，返回 `already connected`。

### 34.3 MCP 工具缺少 handler

`MCPClient.register()` 会抛出 `ValueError`，避免把一个无法执行的工具暴露出去。

### 34.4 MCP 工具重名

同一服务器的原始工具名重复时，`register()` 拒绝注册。

不同服务器或不同原始名称在规范化、加前缀后发生冲突时，`assemble_tool_pool()` 拒绝组装。

### 34.5 Schema 非法

`inputSchema` 不是字典，或者顶层类型不是 `object` 时，动态组装失败。

### 34.6 MCP handler 异常

由 `MCPClient.call_tool()` 捕获并返回 `MCP error: ...`。

### 34.7 通用 handler 异常

由 `execute_tool()` 再做一层捕获，返回 `Error: ...`。

### 34.8 API 或组装工具池异常

`agent_loop()` 捕获异常、把错误放入 Assistant 文本、触发 Stop Hook 并退出当前循环。

### 34.9 一条 prompt 无法覆盖所有异常分支

上面的模拟 prompt 可以触发主要函数的正常执行路径，但无法同时触发所有异常分支。例如“合法 Schema”和“非法 Schema”、“首次连接”和“未知服务器”属于不同条件，有些异常也无法通过当前模型公开的工具接口构造。

---

## 35. 安全设计观察

### 35.1 值得学习的部分

- 外部 MCP 工具默认 `confirm`，而不是默认允许。
- 权限来自宿主策略，不信任服务器的自我声明。
- MCP 工具使用命名空间，避免跨服务器覆盖。
- 规范化后再次检查冲突。
- 工具 Schema 在暴露给模型前经过验证。
- 文件访问工作目录外时要求确认。
- 危险 bash 模式有拒绝列表和确认列表。
- 工具异常被转换成结果，让模型有机会解释或采取替代方案。

### 35.2 教学示例的简化之处

- `MCPClient` 不是真正的网络或子进程 MCP 客户端。
- 模拟 Server 没有认证、连接生命周期、断线重连和超时机制。
- `shell=True` 的安全控制只依赖简单字符串匹配，不能视为生产级沙箱。
- `run_read()`、`run_write()` 和 `run_edit()` 自身不限制路径，依赖统一入口中的 Hook；如果其他代码绕过 `execute_tool()` 直接调用它们，就不会经过权限检查。
- MCP handler 只返回字符串，没有模拟结构化 content、图片、资源或错误对象。
- 没有实现断开 MCP Server 的工具。
- 没有限制 MCP 返回数据的大小，也没有为单个 MCP 调用设置独立超时。

因此，这份代码适合学习动态工具架构，不应直接作为生产环境安全实现。

---

## 36. 函数职责速查

| 函数或结构 | 主要职责 |
|---|---|
| `run_bash` | 执行 shell 命令 |
| `run_read` | 读取文件 |
| `run_write` | 写入文件 |
| `run_edit` | 精确替换文件内容 |
| `run_glob` | 按模式查找工作目录内文件 |
| `BASE_TOOLS` | 提供给模型的基础工具定义 |
| `BASE_HANDLERS` | 基础工具的实际 Python 实现映射 |
| `MCPClient.register` | 校验并保存服务器工具定义和实现 |
| `MCPClient.call_tool` | 模拟 MCP `tools/call` |
| `_mock_server_docs` | 创建 docs 模拟服务器 |
| `_mock_server_deploy` | 创建 deploy 模拟服务器 |
| `connect_mcp` | 按名称创建和登记服务器 |
| `run_connect_mcp` | `connect_mcp` 模型工具的 handler 包装 |
| `normalize_mcp_name` | 将 MCP 名称转换为安全工具名 |
| `assemble_tool_pool` | 合并内置工具和全部已连接 MCP 工具 |
| `assemble_system_prompt` | 将服务器连接状态加入系统提示词 |
| `register_hook` | 注册 Hook 回调 |
| `trigger_hooks` | 按顺序触发 Hook |
| `permission_hook` | 在执行工具前检查权限 |
| `log_hook` | 记录工具调用摘要 |
| `large_output_hook` | 检测超大工具输出 |
| `context_hook` | 用户提交 Prompt 时打印工作目录 |
| `summary_hook` | Agent 停止时统计工具调用 |
| `execute_tool` | 统一执行基础工具或 MCP 工具 |
| `agent_loop` | 驱动模型、工具调用和工具结果循环 |
| `history` | 保存整个终端会话的消息历史 |

---

## 37. 最终应掌握的核心结论

### 结论一：模型不会自行执行工具

模型只生成 `tool_use` 请求。真正的权限判断、handler 查找和代码执行都由 Python 宿主完成。

### 结论二：MCP 的关键价值是动态发现

宿主不需要在初始工具池中硬编码每一个外部工具。它只提供 `connect_mcp`，连接后再读取并转换服务器工具定义。

### 结论三：连接改变的是下一轮工具池

`connect_mcp()` 修改全局连接状态；下一轮的 `assemble_tool_pool()` 才把新工具暴露给模型。

### 结论四：命名空间解决工具重名

`mcp__server__tool` 同时表达来源和工具名称，让多个服务器的同名工具可以共存。

### 结论五：动态 handler 完成协议分发

```text
mcp__docs__search
    ↓
docs_client.call_tool("search", kwargs)
```

这一层把模型侧统一名称映射回服务器自己的工具名称。

### 结论六：能力发现与能力授权必须分离

服务器告诉宿主“我有哪些工具”；宿主决定“哪些工具可以自动执行，哪些必须询问，哪些应该拒绝”。

### 结论七：Agent 循环是 MCP 能工作的基础

如果程序只调用模型一次，连接之后就没有机会重新构建工具池。正是 `while True`、`tool_result` 回传和下一轮重新请求，让动态工具发现真正生效。

最终可以把本章浓缩为一句话：

> Python 宿主负责连接 MCP Server、发现和转换工具、建立动态分发 handler、执行权限检查，再把工具结果送回模型；模型负责根据任务选择何时连接、调用哪些工具以及如何使用结果继续推理。
