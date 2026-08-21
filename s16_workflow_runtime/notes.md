# s16 Workflow Runtime 学习笔记

> 对应源码：[code.py](./code.py)  
> 本章主题：**模型决定单个步骤，脚本决定整体编排。**

## 1. 本章解决了什么问题

在前面的 Agent Loop 中，通常由主模型反复决定下一步调用什么工具：

```text
用户请求
  → 主模型思考
  → tool_use
  → tool_result 回到 messages[]
  → 主模型再次思考
  → 下一次 tool_use
  → ……
```

这种模式适合执行路径无法提前确定的开放任务。但是，有些任务本身就有稳定、可复用的执行结构，例如代码审查：

1. 从 correctness、security、performance、style 四个维度并行审查。
2. 对每条发现再调用一个独立 Agent 做对抗验证。
3. 只保留验证为真的问题。
4. 按严重度排序并返回。

如果每次都让主模型在聊天循环里临时组织这些步骤，会带来几个问题：

- 编排顺序依赖模型当轮决策，不够稳定；
- 每个中间结果都进入主对话历史，迅速消耗上下文；
- 中途失败后，不容易从已完成的位置继续；
- 并发、预算、结构校验等规则分散在模型提示词里。

s16 将固定流程保存成受信任的 Python workflow。主模型只需要调用一次 `Workflow` 工具，workflow runtime 会在内部完成多次子 Agent 调用、并发、校验、持久化和恢复。

```text
主 Agent
  └─ 一次 Workflow tool_use
       └─ 受信任的 workflow 脚本
            ├─ agent()
            ├─ parallel()
            ├─ pipeline()
            ├─ phase()
            └─ workflow()
```

最重要的职责边界是：

```text
主 Agent：决定是否运行某个已注册 workflow，以及传入什么参数
workflow 脚本：决定步骤、依赖关系、并发关系和结果整理方式
子 Agent：完成某一个具体且受限的推理步骤
runtime：负责限制、校验、journal、恢复、状态和持久化
```

---

## 2. 两种“顺序”不要混淆

阅读本文件时要区分两种顺序。

### 2.1 Python 的加载顺序

Python 从文件顶部向下执行模块级代码：

1. 导入模块；
2. 创建常量；
3. 定义函数和类；
4. 创建 schema、示例元数据和 workflow registry；
5. 最后执行 `if __name__ == "__main__"`。

函数和类定义阶段只是创建对象，并不代表工作流已经执行。

### 2.2 一次请求的运行顺序

程序真正处理一条用户 Prompt 时，入口从文件底部开始：

```text
run_cli()
  → s15 agent_loop()
  → 主模型生成 Workflow tool_use
  → run_workflow_sync()
  → run_workflow()
  → WorkflowTool.call()
  → WorkflowTool._call_locked()
  → sample_workflow()
  → ExecutionState.pipeline()/agent()/parallel()
  → 保存结果
  → Workflow tool_result 返回主模型
```

理解行为时，应以第二条调用链为主；理解实现时，再按源码模块逐段阅读。

---

## 3. 源码结构总览

`code.py` 可以划分为以下部分：

| 部分 | 主要对象 | 职责 |
|---|---|---|
| 运行保护 | `AGENT_CAP`、`CONCURRENCY`、run ID、run lock | 控制调用上限、并发和同一运行的排他访问 |
| 输入校验 | `validate_meta()`、`check_permission()` | 启动前验证 workflow 元数据和权限 |
| 结构化输出 | `SimpleJsonSchema` | 校验子 Agent 返回值 |
| Agent 执行器 | `MockAgentRunner`、`AnthropicAgentRunner` | 运行单个 Agent 步骤 |
| 恢复日志 | `WorkflowJournal` | 保存成功调用并在 resume 时命中缓存 |
| 资源与状态 | `Budget`、`LocalWorkflowTask`、`ExecutionLimits` | 记录 token、进度和运行级限制 |
| 编排原语 | `ExecutionState` | 暴露 `agent/parallel/pipeline/phase/workflow` |
| 生命周期 | `WorkflowTool` | 启动、执行、失败处理、落盘和返回 |
| 示例业务 | `sample_workflow()` | 实现“多维审查 → 逐条验证 → 汇总” |
| 主 Agent 适配 | `run_workflow_sync()`、`install_workflow_tool()` | 把 workflow 接入 s15 工具池 |
| CLI | `run_demo()`、`run_cli()` | demo、resume 和交互入口 |

---

## 4. 运行保护、ID 和文件位置

### 4.1 全局保护参数

```python
AGENT_CAP = 1000
CONCURRENCY = 8
STORE = Path(__file__).parent / ".runtime"
MISS = object()
```

- `AGENT_CAP`：一次 workflow run 最多进入 1000 次 `agent()`。
- `CONCURRENCY`：最多同时占用 8 个模型调用名额。
- `STORE`：快照、输出、journal 和锁文件的存放目录。
- `MISS`：缓存未命中的哨兵对象。不能用 `None` 表示未命中，因为合法 Agent 结果本身可能是 `None`。

### 4.2 稳定哈希

```python
def _stable_hash(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)
```

这里不能使用 Python 内置 `hash()`。内置哈希会按进程加盐，同一字符串在两个进程中可能得到不同值，这会破坏跨进程 resume 的 journal key。

### 4.3 run ID

run ID 的格式是：

```text
wf_<workflow-name>_<16位十六进制随机数>
```

例如：

```text
wf_review-changes_a1b2c3d4e5f60718
```

`reserve_run_id()` 使用：

```python
os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
```

原子地预留快照文件名。`O_EXCL` 确保两个进程不能同时成功创建同一个 run ID。

### 4.4 两层运行锁

`workflow_run_lock()` 同时使用：

```text
threading.Lock：防止同一进程内的两个线程同时运行同一个 run ID
fcntl.flock：防止两个宿主进程同时运行或 resume 同一个 run ID
```

锁覆盖从加载快照、执行 workflow 到最终持久化的完整生命周期。

注意：`fcntl` 是 Unix/Linux 接口。在原生 Windows Python 下需要 WSL/Linux，或者替换成 Windows 兼容的文件锁方案。

---

## 5. Workflow 元数据与权限

每个保存好的 workflow 都有宿主管理的可信元数据：

```python
SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],
}
```

`validate_meta()` 在 workflow 启动前检查：

- `meta` 必须是字典；
- 必须存在非空 `name` 和 `description`；
- `name` 必须是 1～64 字符的安全 slug；
- `description` 必须是字符串；
- `phases` 如果存在，必须是非空字符串列表。

`name` 最终会出现在本地文件名中，所以只允许字母、数字、`.`、`_` 和 `-`。

`check_permission()` 是从 s03 延续下来的简化权限入口。目前只实现 deny list：

```python
if meta["name"] in settings.get("deny", []):
    raise WorkflowInputError(...)
```

元数据属于宿主 registry，不是模型提交的内容。模型只能提供 workflow 名称和参数。

---

## 6. 最小 JSON Schema 校验器

`SimpleJsonSchema` 用于验证子 Agent 的结构化输出，支持：

- `object`
- `array`
- `string`
- `boolean`
- `number` / `integer`
- `required`
- `enum`

示例 workflow 使用两个 schema。

### 6.1 审查结果 schema

```python
FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "severity"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
        }
    },
}
```

合法结果示例：

```json
{
  "findings": [
    {
      "title": "SQL injection through interpolated user_id",
      "severity": "high"
    }
  ]
}
```

### 6.2 验证结果 schema

```python
VERDICT_SCHEMA = {
    "type": "object",
    "required": ["isReal", "reason"],
    "properties": {
        "isReal": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}
```

合法结果示例：

```json
{
  "isReal": true,
  "reason": "user_id is directly interpolated into SQL"
}
```

这是教学用的最小实现，并非完整 JSON Schema：

- 不检查 `additionalProperties`；
- `integer` 和 `number` 使用相同判断，因此浮点数也可能通过 `integer`；
- 不支持长度、数值范围、正则等约束。

---

## 7. Agent Runner：完成一个具体步骤

所有 runner 返回统一对象：

```python
@dataclass(frozen=True)
class RunnerOutput:
    value: object
    tokens: int
```

### 7.1 `MockAgentRunner`

用于 demo 和单元测试。它不会访问真实模型，而是根据 prompt 的稳定哈希生成确定性结果。

确定性很重要：同一个 prompt 在多次测试中会得到相同结果，便于验证 pipeline、journal 和 resume 行为。

模块初始默认值是：

```python
RUNNER_FACTORY = MockAgentRunner
```

### 7.2 `AnthropicAgentRunner`

交互模式下使用真实 API Client：

```python
response = self.client.messages.create(
    model=self.model,
    system="You are a focused workflow agent...",
    messages=[{"role": "user", "content": request}],
    max_tokens=2000,
)
```

子 Agent 的系统提示限制它只完成当前步骤，并且不能声称访问了 prompt 中没有提供的文件或结果。

提供 schema 时，runner 会在请求末尾追加：

```text
Return only one JSON object matching this schema:
<schema>
```

`_parse_runner_json()` 依次尝试解析：

1. 完整文本就是 JSON；
2. Markdown 代码块中的 JSON；
3. 文本中从某个 `{` 开始出现的第一个合法 JSON 对象。

如果无法解析，它先把原始文本交给 `ExecutionState.agent()`，由后者的 schema 检查触发一次重试。

---

## 8. Journal：断点恢复的核心

每次 run 对应一个 append-only 文件：

```text
.runtime/<run-id>.journal.jsonl
```

每行是一条已经成功完成的 Agent 调用：

```json
{"key":"agent-5831047281","value":{"isReal":true,"reason":"reproduced"}}
```

### 8.1 语义 key

调用 key 根据以下内容生成：

```python
basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
```

即：

```text
调用类型 + label + prompt + schema
```

它不依赖“第几个完成”或全局计数器，因为并发任务的完成顺序不稳定。如果使用完成顺序作为 key，两次运行就可能把不同调用的缓存对应错。

### 8.2 新运行

```python
WorkflowJournal(run_id, resume=False)
```

以 `w` 模式打开 journal，表示新的 run 从空 journal 开始。

### 8.3 恢复运行

```python
WorkflowJournal(run_id, resume=True)
```

启动时逐行读取旧 journal，构建：

```python
self.cache[key] = value
```

随后以追加模式打开文件。

workflow 脚本仍然会从头执行，但每个 `agent()` 都会先计算语义 key：

```python
cached = self.journal.cached(key)
if cached is not MISS:
    return cached
```

所以“从头执行脚本”不等于“从头调用所有模型”。未变化的 Agent 步骤会直接重放缓存值。

### 8.4 写入时立即 flush

```python
self._f.write(json.dumps({"key": key, "value": value}) + "\n")
self._f.flush()
```

每个 Agent 成功后立即落入 journal，而不是等整条 workflow 完成。即使后续步骤失败，前面完成的调用仍可用于 resume。

---

## 9. Budget、任务状态和运行级限制

### 9.1 `Budget`

`Budget` 保存：

```python
total
_spent
```

提供：

```python
spent()
remaining()
add(tokens)
```

当累计 token 超过上限时抛出 `WorkflowInputError`。

需要注意：真实 token 数只有模型返回后才知道。因此并发调用可能已经发出，之后 `Budget.add()` 才发现超限。这个预算实现保证 workflow 不会把超限状态当成功，但不是调用前的精确 token 预留系统。

### 9.2 `LocalWorkflowTask`

保存一次本地 workflow 任务的状态：

```python
status = "running"
usage = {"agents": 0, "tokens": 0}
progress = []
```

事件分为两类：

- `event()`：启动、开始、结束等生命周期信息；
- `progress_event()`：阶段、Agent 完成情况和 workflow 日志。

### 9.3 `ExecutionLimits`

一次顶层 workflow 及其嵌套子 workflow 共享：

```python
self.agents
self.semaphore = asyncio.Semaphore(CONCURRENCY)
```

- `agents` 是整次 run 的逻辑 `agent()` 进入次数；
- semaphore 限制同时运行的模型调用最多为 8 个。

缓存命中的 `agent()` 也会经过 `claim_agent()`，所以它仍计入 1000 次安全上限；但不会增加 `task.usage["agents"]` 和 token，因为本次没有真的调用模型。

---

## 10. `ExecutionState`：workflow 的编排 API

workflow 脚本接收的 `ctx` 是 `ExecutionState`。它只暴露少量编排原语。

### 10.1 `phase(title)`

```python
ctx.phase("Review")
```

设置当前进度阶段。同一个 title 只发布一次阶段事件。

`phase()` 只是进度标记，不会形成同步屏障。调用 `ctx.phase("Verify")` 不代表所有 Review 都已经完成。

### 10.2 `log(message)`

```python
ctx.log("confirmed 2 real finding(s)")
```

向任务进度中追加一条 `workflow_log`。

### 10.3 `agent(prompt, schema=None, label=None, phase=None)`

一次 `agent()` 按以下顺序执行：

```text
1. 补全 label
2. 占用一次 run 级 Agent 调用额度
3. 检查 token 预算是否已经耗尽
4. 根据 kind/label/prompt/schema 计算 journal key
5. 查询 journal 缓存
6. 命中缓存：校验缓存并直接返回
7. 未命中：获取并发信号量
8. 用 asyncio.to_thread() 调用同步 runner
9. 如果有 schema，验证结果
10. 验证失败则提醒模型并重试一次
11. 重试仍失败则让 workflow 失败
12. 统计 token 和真实 Agent 次数
13. 写 journal 并 flush
14. 发布 workflow_agent 进度事件
15. 返回结构化结果
```

这里用 `asyncio.to_thread()` 是因为 API Client 调用是同步阻塞的。如果直接在事件循环线程调用，其他并发 workflow coroutine 无法继续运行。

如果 schema 重试发生：

- 它仍算一次逻辑 `agent()`；
- token 会包含第一次和重试两次调用；
- 只有最终合法结果会写入 journal。

### 10.4 `parallel(thunks)`

```python
async def parallel(self, thunks):
    return await asyncio.gather(*[thunk() for thunk in thunks])
```

`parallel()` 是一个屏障：所有 thunk 并发启动，调用者等待它们全部结束。

参数使用 thunk，而不是预先创建好的结果：

```python
await ctx.parallel([
    lambda: ctx.agent("verify finding A"),
    lambda: ctx.agent("verify finding B"),
])
```

如果任一分支抛出异常，`gather()` 会让当前 workflow 路径失败。

### 10.5 `pipeline(items, *stages)`

```python
async def pipeline(self, items, *stages):
    async def run_item(item, idx):
        value = item
        for stage in stages:
            value = await stage(value, item, idx)
        return value
    return await asyncio.gather(
        *[run_item(it, i) for i, it in enumerate(items)]
    )
```

每个 item 独立、依次经过所有 stage；不同 item 之间并发：

```text
item A：stage 1 → stage 2 → stage 3
item B：stage 1 → stage 2 → stage 3
item C：stage 1 → stage 2 → stage 3
```

它没有“所有 item 完成 stage 1 后才一起进入 stage 2”的全局屏障。因此可能出现：

```text
item A 已到 stage 3
item B 仍在 stage 1
```

虽然完成顺序不固定，`asyncio.gather()` 的返回列表仍与输入 item 的顺序一致。

### 10.6 `workflow(name, args)`

允许 workflow 内联调用另一个已注册 workflow：

```python
await ctx.workflow("child-workflow", args)
```

只允许嵌套一层。子 workflow 与父 workflow 共享：

- task；
- journal；
- runner；
- token budget；
- Agent 次数；
- 并发 semaphore。

因此嵌套不会绕过顶层运行限制。

---

## 11. `WorkflowTool`：一次完整运行的生命周期

`WorkflowTool.call()` 是 workflow runtime 的正式入口：

```text
validate_meta(meta)
  → check_permission(meta)
  → 创建新 run ID 或校验 resume run ID
  → 获取整个 run 生命周期的独占锁
  → _call_locked()
```

### 11.1 新运行

新运行会：

1. 把空字典作为缺省 args；
2. 创建并清空本次 journal；
3. 创建 task ID 和 `LocalWorkflowTask`；
4. 发布 `async_launched`；
5. 发布 `task_started`；
6. 写入初始 `running` 快照；
7. 创建 `ExecutionState`；
8. `await script_fn(ctx, args)`；
9. 成功则设置 `completed`；
10. 失败则设置 `failed` 并生成 `{"error": ...}`；
11. 关闭 journal；
12. 写最终 output；
13. 更新最终 snapshot；
14. 保存 `last_run.txt`；
15. 发布 `task_notification`；
16. 返回 launch envelope、result 和 task。

### 11.2 恢复运行

提供 `resume_from_run_id` 时，会额外检查：

- 快照必须存在且是合法 JSON object；
- 快照中的 `workflowName` 必须与当前 workflow 相同；
- 如果重新传 args，新 args 必须与原 args 完全相等；
- journal 必须存在且每一行都是合法的 key/value 记录。

不传新 args 时，直接使用快照保存的原 args。

### 11.3 `async_launched` 的真实含义

返回 envelope 中有：

```json
{"status":"async_launched"}
```

但当前实现并没有把整个 workflow 放进后台线程。调用链仍然会等待：

```python
result = await script_fn(ctx, args)
```

因此它是一个生命周期事件名称，而不是“工具立即返回，工作流在后台继续”的实现。

### 11.4 错误的返回方式

`script_fn()` 内部的大多数异常会在 `_call_locked()` 中被捕获：

```python
except Exception as e:
    task.status = "failed"
    result = {"error": str(e)}
```

因此已经进入 workflow 生命周期后的错误通常返回：

```json
{
  "result": {"error": "..."},
  "task": {"status": "failed"}
}
```

启动前的 `WorkflowInputError`，例如 workflow 名称未知，则可能由 `run_workflow_sync()` 转成：

```text
Error: unknown workflow '...'
```

---

## 12. 示例 workflow：`review-changes`

注册表是：

```python
WORKFLOWS = {
    "review-changes": (SAMPLE_META, sample_workflow)
}
```

审查维度是：

```python
DIMENSIONS = ["correctness", "security", "performance", "style"]
```

`sample_workflow()` 的业务流程是：

```text
读取 changes
  → 四个维度分别进入 pipeline
       → audit：找出该维度的问题
       → verify：每条 finding 单独做对抗验证
  → 只保留 isReal=true 的 finding
  → 展平四个维度的结果
  → 按 high/medium/low 排序
  → 记录 confirmed 数量
  → 返回 {"confirmed": [...]}
```

### 12.1 `audit()`

每个维度调用一个子 Agent：

```python
out = await ctx.agent(
    f"Review this change context for {dimension} issues...",
    schema=FINDINGS_SCHEMA,
    label=f"audit:{dimension}",
    phase="Review",
)
```

输出被转换为：

```python
{
    "dimension": dimension,
    "findings": out["findings"],
}
```

### 12.2 `verify()`

同一个维度中的所有 finding 并发验证：

```python
verdicts = await ctx.parallel([
    lambda f=f: ctx.agent(...)
    for f in audited["findings"]
])
```

这里的 `lambda f=f` 非常重要。它把当前循环的 `f` 绑定为 lambda 默认参数，避免 Python 闭包晚绑定导致所有 lambda 最终都引用最后一个 finding。

验证完成后只保留：

```python
v and v.get("isReal")
```

的 finding。

### 12.3 汇总与排序

```python
confirmed = [
    {"dimension": r["dimension"], **f}
    for r in results
    if r
    for f in r["confirmed"]
]
```

最后按严重度排序：

```text
high → medium → low → 未知值
```

当前示例没有实现跨维度去重。如果两个 audit 维度返回语义相同的问题，且都通过验证，最终结果中可能出现两份。

---

## 13. `Workflow` 工具如何接入 s15

模型可见的工具定义只接受：

```json
{
  "name": "review-changes",
  "args": {},
  "resume_from_run_id": "可选"
}
```

`run_workflow()` 是模型侧适配器：

1. 校验 name 和 args 的基本类型；
2. 从 `WORKFLOWS` registry 解析可信 `meta` 与 `script_fn`；
3. 调用 `WorkflowTool.call()`；
4. 把 task 转成可 JSON 序列化的字典。

`run_workflow_sync()` 则是同步 s15 dispatcher 与异步 workflow runtime 之间的桥：

```python
def run_workflow_sync(**tool_input):
    return json.dumps(
        asyncio.run(run_workflow(**tool_input)),
        default=str,
    )
```

交互模式启动时，`install_workflow_tool()`：

1. 把 runner factory 换成真实 `AnthropicAgentRunner`；
2. 包装 s15 的 `assemble_tool_pool()`；
3. 向 tools 中追加 `WORKFLOW_TOOL`；
4. 把 `run_workflow_sync` 注册成 `Workflow` handler；
5. 保留 s15 原来的其他工具和 handler。

所以 s16 没有重写 s15 的主循环，只是在既有工具池中增加了一个编排型工具。

---

# 14. 用一条 Prompt 模拟完整运行

下面模拟交互模式的一次成功运行。真实模型输出、token 数、完成顺序和随机 run ID 并不固定；示例值仅用于展示数据怎样流动。

## 14.1 用户输入

```text
请使用 review-changes 工作流审查下面的代码改动，预算不限：

def load_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query).fetchone()
```

## 14.2 主 Agent 选择 `Workflow`

主 Agent 看到 s16 安装的工具 schema，生成一次 tool use：

```json
{
  "type": "tool_use",
  "id": "toolu_001",
  "name": "Workflow",
  "input": {
    "name": "review-changes",
    "args": {
      "budget": null,
      "changes": "def load_user(user_id):\n    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n    return db.execute(query).fetchone()"
    }
  }
}
```

从 s15 只需要知道：dispatcher 找到 `handlers["Workflow"]`，随后调用 `run_workflow_sync(**block.input)`。

## 14.3 同步桥进入异步 runtime

实际调用相当于：

```python
run_workflow_sync(
    name="review-changes",
    args={
        "budget": None,
        "changes": "def load_user...",
    },
)
```

`asyncio.run()` 为本次 workflow 创建事件循环，并等待 `run_workflow()` 完成。

## 14.4 查找可信 workflow

`run_workflow()` 从 registry 取得：

```python
meta = SAMPLE_META
script_fn = sample_workflow
```

然后调用：

```python
await WorkflowTool().call(
    meta,
    sample_workflow,
    args=args,
    resume_from_run_id=None,
)
```

此处模型只提供名称和数据，没有提供可执行 Python 代码。

## 14.5 创建本次 run

假设生成：

```text
runId  = wf_review-changes_a1b2c3d4e5f60718
taskId = local_workflow_wf_review-changes_a1b2c3d4e5f60718
```

runtime：

1. 验证元数据；
2. 做权限检查；
3. 原子预留 run ID；
4. 获取线程锁和文件锁；
5. 创建空 journal；
6. 创建 `LocalWorkflowTask`。

发布：

```text
event async_launched
      runId=wf_review-changes_a1b2c3d4e5f60718
      taskId=local_workflow_wf_review-changes_a1b2c3d4e5f60718

event task_started
      workflow=review-changes
      phases=Review,Verify
      resume=False
```

初始 task 是：

```json
{
  "status": "running",
  "usage": {"agents": 0, "tokens": 0},
  "progress": []
}
```

这时先写一次 running 快照，保证 workflow 真正执行前已经存在启动记录。

## 14.6 创建 `ExecutionState`

```python
ctx = ExecutionState(
    task=task,
    journal=journal,
    runner=AnthropicAgentRunner(host.client, host.MODEL),
    budget=Budget(None),
    args=args,
)
```

然后执行：

```python
result = await sample_workflow(ctx, args)
```

## 14.7 进入 Review 阶段

```python
ctx.phase("Review")
```

进度新增：

```json
{"type":"workflow_phase","title":"Review"}
```

从 args 读取 `changes`，检查它必须是字符串，然后调用：

```python
await ctx.pipeline(
    ["correctness", "security", "performance", "style"],
    audit,
    verify,
)
```

## 14.8 创建四条并发 pipeline

`pipeline()` 等价于并发启动：

```text
run_item("correctness", 0)
run_item("security", 1)
run_item("performance", 2)
run_item("style", 3)
```

每个 item 内部严格执行：

```text
audit → verify
```

总体结构为：

```text
correctness ── audit ── verify its findings ── result
security    ── audit ── verify its findings ── result
performance ── audit ── verify its findings ── result
style       ── audit ── verify its findings ── result
```

## 14.9 四个 audit Agent

以 security 为例，prompt 为：

```text
Review this change context for security issues.
Report only issues supported by the supplied text.

def load_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query).fetchone()
```

调用参数：

```python
ctx.agent(
    prompt,
    schema=FINDINGS_SCHEMA,
    label="audit:security",
    phase="Review",
)
```

另外三条并发调用是：

```text
audit:correctness
audit:performance
audit:style
```

每个调用内部都会：

1. 占用 Agent 次数；
2. 生成稳定 journal key；
3. 检查缓存；
4. 获取 8 并发 semaphore；
5. 在线程中调用真实 runner；
6. 解析并验证 `FINDINGS_SCHEMA`；
7. 统计 token；
8. 写 journal；
9. 发布 `status=done`。

假设四个 audit 各返回一个 finding：

```json
{
  "correctness": {
    "findings": [
      {
        "title": "Missing behavior for nonexistent users",
        "severity": "medium"
      }
    ]
  },
  "security": {
    "findings": [
      {
        "title": "SQL injection through interpolated user_id",
        "severity": "high"
      }
    ]
  },
  "performance": {
    "findings": [
      {
        "title": "SELECT * fetches unnecessary columns",
        "severity": "low"
      }
    ]
  },
  "style": {
    "findings": [
      {
        "title": "SQL construction is mixed with data access",
        "severity": "low"
      }
    ]
  }
}
```

security 的 journal 记录类似：

```json
{"key":"agent-5831047281","value":{"findings":[{"title":"SQL injection through interpolated user_id","severity":"high"}]}}
```

## 14.10 audit 完成后立即进入 verify

假设 security audit 最先完成，它会立即调用自己的 `verify()`：

```python
ctx.phase("Verify")
```

产生一次：

```json
{"type":"workflow_phase","title":"Verify"}
```

此时 performance 或 style audit 可能尚未结束。`phase("Verify")` 只是进度事件，不会等待所有 Review。

security 验证 prompt 类似：

```text
Adversarially verify this security finding against the supplied change context.

Change context:
def load_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query).fetchone()

Finding:
{"title": "SQL injection through interpolated user_id", "severity": "high"}
```

如果一个 audit 返回多条 finding，同一维度会通过 `ctx.parallel()` 并发验证这些 finding。

假设验证结果为：

```json
{
  "correctness": {
    "isReal": false,
    "reason": "The required behavior for missing users is not stated."
  },
  "security": {
    "isReal": true,
    "reason": "user_id is directly interpolated into SQL."
  },
  "performance": {
    "isReal": true,
    "reason": "SELECT * can retrieve columns the caller does not need."
  },
  "style": {
    "isReal": false,
    "reason": "This is a design preference rather than a demonstrated defect."
  }
}
```

每个 verdict 同样通过 schema 校验并写入 journal。

## 14.11 一种可能的事件交错

由于不同维度并发，一种合法输出顺序是：

```text
workflow_phase Review

audit:security done
workflow_phase Verify

audit:correctness done
verify:security:SQL injection through interpolated user_id done

audit:style done
verify:correctness:Missing behavior for nonexistent users done

audit:performance done
verify:style:SQL construction is mixed with data access done
verify:performance:SELECT * fetches unnecessary columns done
```

真实完成顺序每次可能不同，但 `pipeline()` 最终结果仍按：

```text
correctness → security → performance → style
```

排列。

## 14.12 过滤、展平和排序

每个维度只保留 `isReal=true` 的 finding，因此得到：

```python
[
    {"dimension": "correctness", "confirmed": []},
    {
        "dimension": "security",
        "confirmed": [
            {
                "title": "SQL injection through interpolated user_id",
                "severity": "high",
            }
        ],
    },
    {
        "dimension": "performance",
        "confirmed": [
            {
                "title": "SELECT * fetches unnecessary columns",
                "severity": "low",
            }
        ],
    },
    {"dimension": "style", "confirmed": []},
]
```

展平并排序后：

```json
{
  "confirmed": [
    {
      "dimension": "security",
      "title": "SQL injection through interpolated user_id",
      "severity": "high"
    },
    {
      "dimension": "performance",
      "title": "SELECT * fetches unnecessary columns",
      "severity": "low"
    }
  ]
}
```

记录：

```text
workflow_log message=confirmed 2 real finding(s)
```

## 14.13 Workflow 收尾

`sample_workflow()` 正常返回后：

```python
task.status = "completed"
```

假设本次执行了 4 个 audit 和 4 个 verify，使用 1450 tokens：

```json
{
  "agents": 8,
  "tokens": 1450
}
```

runtime 随后：

1. 关闭 journal；
2. 把 result 写入 `<run-id>.output.json`；
3. 把最终 task 写入 `<run-id>.json`；
4. 把 run ID 写入 `last_run.txt`；
5. 发布 `task_notification`；
6. 释放文件锁和线程锁。

通知类似：

```text
event task_notification
      status=completed
      agents=8
      tokens=1450
      outputFile=.runtime/wf_review-changes_a1b2c3d4e5f60718.output.json
```

## 14.14 返回一个顶层工具结果

`WorkflowTool.call()` 返回：

```python
{
    "launched": launched,
    "result": result,
    "task": task,
}
```

经 `serialize_task()` 和 `json.dumps()` 后，顶层 `Workflow` 工具结果大致为：

```json
{
  "launched": {
    "status": "async_launched",
    "taskId": "local_workflow_wf_review-changes_a1b2c3d4e5f60718",
    "taskType": "local_workflow",
    "runId": "wf_review-changes_a1b2c3d4e5f60718",
    "workflowName": "review-changes"
  },
  "result": {
    "confirmed": [
      {
        "dimension": "security",
        "title": "SQL injection through interpolated user_id",
        "severity": "high"
      },
      {
        "dimension": "performance",
        "title": "SELECT * fetches unnecessary columns",
        "severity": "low"
      }
    ]
  },
  "task": {
    "taskId": "local_workflow_wf_review-changes_a1b2c3d4e5f60718",
    "taskType": "local_workflow",
    "runId": "wf_review-changes_a1b2c3d4e5f60718",
    "workflowName": "review-changes",
    "status": "completed",
    "usage": {
      "agents": 8,
      "tokens": 1450
    },
    "progress": [
      {"type": "workflow_phase", "title": "Review"},
      {"type": "workflow_agent", "label": "audit:security", "phase": "Review", "status": "done"},
      {"type": "workflow_phase", "title": "Verify"},
      {"type": "workflow_agent", "label": "verify:security:SQL injection through interpolated user_id", "phase": "Verify", "status": "done"},
      {"type": "workflow_log", "message": "confirmed 2 real finding(s)"}
    ]
  }
}
```

s15 将这个字符串包装为与 `toolu_001` 对应的一个 `tool_result`，再调用一次主模型生成用户可读回复。

关键点是：workflow 内部 8 个子 Agent 的原始交互没有逐条进入主 Agent 的 `messages[]`。主历史中主要只有：

```text
一次 Workflow tool_use
一次 Workflow tool_result
```

## 14.15 主 Agent 的最终回复

主 Agent 可能整理为：

```text
审查完成，确认了两个问题：

1. 高危：user_id 被直接插入 SQL，存在 SQL 注入风险，应改为参数化查询。
2. 低危：SELECT * 可能读取不需要的字段，建议明确指定返回列。

工作流共实际执行 8 个 Agent。
```

---

## 15. 同一次 run 如何 resume

如果稍后执行：

```bash
python s16_workflow_runtime/code.py resume
```

demo 会从 `.runtime/last_run.txt` 取得 run ID，并传给：

```python
run_workflow(
    name="review-changes",
    args=原参数,
    resume_from_run_id=旧run_id,
)
```

恢复时的流程是：

```text
读取 snapshot
  → 验证 workflowName
  → 验证 args 与原运行相同
  → 加载 journal cache
  → 从头执行 sample_workflow()
  → 每个 agent() 重新计算语义 key
  → key 命中则返回 cached value
  → 未命中的步骤才真正调用模型
```

缓存命中的进度事件为：

```text
workflow_agent label=... phase=... status=cached
```

如果所有 prompt、label 和 schema 都没变，那么本次真实调用数和 token 用量通常为 0：

```json
{
  "usage": {
    "agents": 0,
    "tokens": 0
  }
}
```

但 pipeline、过滤、排序等普通 Python 代码仍会重新执行，并根据缓存结果重新生成最终 output。

如果 workflow 代码改变了某个 Agent 的 prompt、label 或 schema，对应语义 key 会改变，该调用不会命中旧缓存；依赖其新结果生成新 prompt 的后续步骤通常也会形成新 key 并重新运行。

---

## 16. 运行文件的含义

一次 run 在 `.runtime/` 中涉及：

| 文件 | 内容 |
|---|---|
| `<run-id>.json` | workflow 名称、原 args、任务状态、用量和 progress 快照 |
| `<run-id>.output.json` | workflow 最终业务返回值，成功结果或错误对象 |
| `<run-id>.journal.jsonl` | 每个成功 Agent 步骤的 key/value 记录 |
| `<run-id>.lock` | 跨进程排他锁载体；释放锁后文件可以继续存在 |
| `last_run.txt` | 最近完成或失败的 run ID，供 demo resume 使用 |

快照使用临时文件加 `os.replace()`：

```text
先完整写入 <name>.tmp
  → 再原子替换正式文件
```

这样可以降低进程在写文件中途终止导致正式 JSON 只写了一半的风险。

---

## 17. 关键细节与易错点

### 17.1 `pipeline` 的 phase 不是全局阶段

`ctx.phase("Verify")` 可能在其他维度仍处于 Review 时发生。`phase` 是展示信息，不是调度控制。

代码通过给每个 `agent()` 显式传入 `phase="Review"` 或 `phase="Verify"`，避免共享的 `ctx._phase` 因并发切换而把 Agent 标到错误阶段。

### 17.2 结构化输出仍然必须验证

即使 prompt 明确要求 JSON，模型仍可能返回 Markdown、解释文字、缺失字段或非法枚举值。runtime 做解析、schema 校验和一次重试，使 workflow 后续 Python 代码能依赖稳定的数据形状。

### 17.3 journal 只记录成功结果

Agent 输出通过校验、预算检查完成后才记录。如果 runner 报错或两次输出都不合法，该步骤没有可恢复结果，resume 时会重新执行。

### 17.4 task usage 统计真实执行，不统计缓存重放

`task.usage["agents"]` 在 runner 完成并通过校验后才增加。resume 缓存命中不增加该字段。

### 17.5 workflow 失败也会完成收尾

脚本异常会被转换成 failed task，但 runtime 仍会：

- 关闭 journal；
- 写 output；
- 更新 snapshot；
- 保存 last run；
- 发布最终通知。

这保证失败也是一个闭合、可检查、可恢复的运行状态。

### 17.6 当前没有真正的后台 workflow

顶层工具调用会等待完整 workflow。若要真正后台化，需要把 run 注册到独立线程、进程或任务队列，并让顶层工具立即返回 task ID；这不是当前 s16 的实现范围。

### 17.7 并发限制是整次 run 共享的

`parallel()`、`pipeline()` 和一层嵌套 workflow 都共享同一个 semaphore，所以任意组合都不能让同时运行的模型调用超过 8 个。

---

## 18. 最终心智模型

可以把 s16 记成五层：

```text
第一层：主 Agent
  决定调用哪个保存好的 workflow

第二层：Workflow 工具适配器
  把模型参数映射到可信 registry 和异步 runtime

第三层：WorkflowTool 生命周期
  负责 run ID、锁、快照、状态、错误收尾和返回值

第四层：ExecutionState 编排
  提供 agent、parallel、pipeline、phase、log、嵌套 workflow

第五层：Runner + Journal
  Runner 完成具体模型步骤，Journal 保存并恢复成功结果
```

一句话总结：

> s16 把一组稳定的多 Agent 操作封装成一个可信、结构化、有限制、可持久化、可恢复的顶层工具调用。
