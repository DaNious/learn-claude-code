# s10 Task System 学习笔记

## 一、这一节在讲什么

`s10_task_system` 在已有 Agent Loop、工具调用、权限和 Hook 的基础上，增加了一套可以持久化的任务系统。

这一节的重点不再是“模型接下来要调用哪个文件工具”，而是让 Agent 能够回答下面这些问题：

- 一个大目标被拆成了哪些任务？
- 哪些任务还不能开始？
- 一个任务依赖哪些前置任务？
- 当前是谁在执行这个任务？
- 程序退出后，任务进度能否保留下来？
- 完成一个任务后，哪些下游任务刚刚被解锁？

一句话总结：

> s10 把一次会话里的执行清单，升级成了跨会话保存、带依赖关系和负责人信息的任务图。

任务的基本状态机是：

```text
pending ──claim_task──> in_progress ──complete_task──> completed
```

完整的 Agent 运行流程仍然是：

```text
用户输入
  ↓
模型读取对话和工具定义
  ↓
模型决定调用任务工具或基础工具
  ↓
Python 执行工具并返回 tool_result
  ↓
模型根据结果继续规划和执行
  ↓
没有新的工具调用时输出最终回答
```

---

## 二、s05 TodoWrite 和 s10 Task System 的区别

理解本章前，最重要的是区分 todo 和 task。

| 对比项 | s05 TodoWrite | s10 Task System |
| --- | --- | --- |
| 主要用途 | 提醒当前 Agent 接下来做什么 | 管理可以独立认领和追踪的任务 |
| 保存位置 | 进程内存 | `.tasks/*.json` |
| 程序退出后 | 消失 | 仍然存在 |
| 依赖关系 | 没有 | `blockedBy` |
| 负责人 | 没有 | `owner` |
| 是否能判断可执行 | 主要依靠模型理解 | 本地代码检查依赖状态 |
| 更新方式 | 重写整个 todo 列表 | 单独创建、读取、认领、完成任务 |
| 适合的粒度 | 当前工作的执行步骤 | 项目级、可协作、可恢复的工作单元 |

例如，“实现用户登录”可以在 Task System 中作为一个任务；执行它时使用的“读代码、修改接口、补测试”更像当前任务内部的 todo。

二者并不冲突：

```text
Task System：决定现在可以做哪项工作
TodoWrite：记录这项工作内部准备怎么做
```

---

## 三、任务依赖图

本章中的 `blockedBy` 表示一个任务依赖哪些前置任务。

例如：

```text
Setup database schema
        │
        ├──> Create API endpoints
        │          │
        │          └──> Write tests
        │
        └──> Write docs
```

箭头可以理解为“完成左边之后，右边才有可能开始”。

这种结构是一个有向图。理想情况下它应当是 DAG（Directed Acyclic Graph，有向无环图）：

- 有向：依赖有明确方向。
- 无环：不能出现 A 依赖 B，同时 B 又依赖 A。

当前实现能够检查依赖是否存在，但没有显式检测依赖环。因此调用方仍可能通过后续手工修改 JSON 等方式制造循环依赖。一旦存在环，相关任务将一直无法开始。

---

## 四、启动初始化

程序首先加载模块和环境变量：

```python
load_dotenv(override=True)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

这里要注意：

- `WORKDIR` 是启动程序时的当前目录，不一定是 `code.py` 所在目录。
- `.tasks` 目录会创建在 `WORKDIR` 下。
- `MODEL_ID` 不存在时，程序会在启动阶段抛出 `KeyError`。

系统提示词是：

```python
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use task tools to track dependencies and progress."
)
```

它告诉模型应当使用任务工具记录依赖和进度。不过这仍然只是提示词约束；是否调用任务工具，最终取决于模型输出。

---

## 五、任务数据结构

### 1. `Task` dataclass

每个任务都用一个 `Task` 对象表示：

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
```

字段含义：

- `id`：任务唯一标识，例如 `task_76ba0742`。
- `subject`：简短标题。
- `description`：完整任务说明。
- `status`：`pending`、`in_progress` 或 `completed`。
- `owner`：当前任务的负责人，尚未认领时为 `None`。
- `blockedBy`：前置任务 ID 列表。

### 2. 为什么使用 dataclass

`@dataclass` 自动生成初始化等常用方法，让代码可以直接写：

```python
task = Task(
    id="task_76ba0742",
    subject="Setup database schema",
    description="Design and implement the database schema.",
    status="completed",
    owner="agent",
    blockedBy=[],
)
```

写入 JSON 前，通过：

```python
asdict(task)
```

把 dataclass 转换成普通字典。

### 3. 磁盘格式

每个任务单独保存为一个 JSON 文件：

```text
.tasks/task_76ba0742.json
```

示例：

```json
{
  "id": "task_23e97cfd",
  "subject": "Create API endpoints",
  "description": "Implement the API endpoints backed by the database schema.",
  "status": "pending",
  "owner": null,
  "blockedBy": [
    "task_76ba0742"
  ]
}
```

一条任务一个文件的优点是：

- 更新单条任务时不需要重写整张任务表。
- 文件容易查看和调试。
- 程序退出后仍可恢复。
- 后续可以围绕单个任务增加认领、同步等机制。

缺点是跨多个文件的修改不是事务性的，并发写入时也需要更完整的锁或原子替换机制。

---

## 六、`TaskStore`：持久化存储层

`TaskStore` 把路径检查和 JSON 读写集中到一个类中：

```python
TASKS_DIR = WORKDIR / ".tasks"
TASKS = TaskStore(TASKS_DIR)
```

上层函数通过全局 `TASKS` 对象访问任务文件。

### 1. `_root(create=False)`

```python
def _root(self, create: bool = False) -> Path:
    if create:
        self.directory.mkdir(parents=True, exist_ok=True)
    root = self.directory.resolve()
    if not root.is_relative_to(WORKDIR.resolve()):
        raise ValueError("Task store escapes the workspace")
    return root
```

作用：

1. 在需要写入时创建 `.tasks` 目录。
2. 把目录转换成绝对路径。
3. 确认任务目录没有逃出当前工作区。

`create=False` 时只负责解析和检查，不主动创建目录。

### 2. `_path(task_id, create_root=False)`

任务 ID 必须匹配：

```python
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
```

合法示例：

```text
task_76ba0742
task_abcdef12
```

非法示例：

```text
task_123
TASK_76BA0742
../../outside
task_76ba0742.json
```

只有验证通过后，才会生成：

```python
root / f"{task_id}.json"
```

正则检查和 `is_relative_to(root)` 构成两层路径安全检查。

### 3. `exists(task_id)`

```python
return self._path(task_id).is_file()
```

它主要在创建带依赖的任务时使用，确保 `blockedBy` 指向的任务真实存在。

### 4. `create(subject, description, blocked_by)`

创建任务的顺序是：

```text
清理 subject
  ↓
拒绝空标题
  ↓
对 blockedBy 去重并保持原顺序
  ↓
确认每个依赖任务都存在
  ↓
确保 .tasks 目录存在
  ↓
生成随机任务 ID
  ↓
用排他模式创建 JSON 文件
```

依赖去重使用：

```python
dependencies = list(dict.fromkeys(blocked_by or []))
```

因为 Python 字典保持插入顺序，所以它既能去重，又不会打乱依赖顺序。

任务 ID 使用：

```python
id=f"task_{secrets.token_hex(4)}"
```

`token_hex(4)` 生成 4 个随机字节，对应 8 个十六进制字符。

创建文件时使用：

```python
open("x", encoding="utf-8")
```

`x` 是排他创建模式：

- 文件不存在：创建成功。
- 文件已经存在：抛出 `FileExistsError`，不会覆盖旧任务。

代码最多重新生成 100 次 ID。连续失败后抛出 `RuntimeError`。

### 5. `save(task)`

```python
self._path(task.id, create_root=True).write_text(
    json.dumps(asdict(task), indent=2),
    encoding="utf-8",
)
```

认领或完成任务后，通过 `save()` 覆盖写回对应 JSON 文件。

这里没有使用临时文件加原子替换。如果程序恰好在写入中途崩溃，理论上可能留下不完整 JSON；教学代码暂时忽略了这个问题。

### 6. `load(task_id)`

读取顺序：

```text
校验 ID 并定位文件
  ↓
读取 JSON
  ↓
Task(**data) 恢复对象
  ↓
检查文件内容中的 id 是否和请求一致
  ↓
检查 status 是否属于允许集合
```

两个显式校验是：

```python
if task.id != task_id:
    raise ValueError(...)

if task.status not in ("pending", "in_progress", "completed"):
    raise ValueError(...)
```

### 7. `list()`

如果 `.tasks` 不存在，返回空列表。否则：

```python
return [
    self.load(path.stem)
    for path in sorted(root.glob("task_*.json"))
]
```

这意味着：

- 只扫描 `task_*.json`。
- 按文件路径排序。
- 每个文件仍然通过 `load()` 做完整校验。
- 任意一个匹配文件损坏，都可能导致整个 `list()` 失败。

---

## 七、任务服务函数

代码在 `TaskStore` 外又包装了一层函数：

```python
create_task(...)
load_task(task_id)
list_tasks()
get_task(task_id)
```

这一层目前很薄，但它隔离了上层业务逻辑和具体存储实现。以后即使把 JSON 文件换成数据库，上层的 `claim_task()`、`complete_task()` 也不一定需要全部重写。

`get_task()` 返回格式化 JSON 字符串：

```python
def get_task(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)
```

`list_tasks()` 面向内部代码返回 `list[Task]`；`get_task()` 面向工具结果返回字符串。这体现了领域对象和工具协议之间的转换。

---

## 八、依赖判断

### 1. `incomplete_dependencies(task)`

```python
def incomplete_dependencies(task: Task) -> list[str]:
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError):
            incomplete.append(dependency)
    return incomplete
```

它逐个读取 `blockedBy` 中的任务：

- 状态是 `completed`：依赖满足。
- 状态是 `pending`：依赖未满足。
- 状态是 `in_progress`：依赖未满足。
- 文件不存在：依赖未满足。
- ID 或任务状态非法：依赖未满足。

这里采用的是保守策略：无法确认依赖已经完成时，就不允许继续。

### 2. `can_start(task_id)`

```python
def can_start(task_id: str) -> bool:
    return not incomplete_dependencies(load_task(task_id))
```

其逻辑等价于：

```text
所有 blockedBy 都是 completed → True
至少一个 blockedBy 未完成或不可读取 → False
```

没有依赖的任务，其未完成依赖列表为空，因此天然可以开始。

### 3. `blockedBy` 不会被删除

当前置任务完成后，下游任务的 `blockedBy` 字段不会被修改。

例如：

```json
"blockedBy": ["task_76ba0742"]
```

即使 `task_76ba0742` 已经完成，这条依赖关系仍然保留。系统只是通过读取前置任务的状态，动态判断当前任务是否可开始。

这样可以保留完整的任务来源和依赖历史。

---

## 九、认领任务：`claim_task`

认领表示 Agent 正式开始负责这项工作：

```text
pending → in_progress
```

完整检查顺序：

```python
task = load_task(task_id)

if task.status != "pending":
    return ...

dependencies = incomplete_dependencies(task)
if dependencies:
    return ...

task.owner = owner
task.status = "in_progress"
TASKS.save(task)
```

因此只有同时满足以下条件才能认领：

1. 任务当前是 `pending`。
2. 所有前置任务都是 `completed`。

成功后同时更新：

- `owner = "agent"`
- `status = "in_progress"`

包装函数固定使用：

```python
def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")
```

因此这个版本实际上只有一个固定 owner。虽然数据模型已经为多人或多 Agent 协作预留了字段，但当前工具 schema 没有让模型传入不同 owner。

认领失败时只返回错误字符串，不抛异常，也不修改任务文件。模型可以读取该结果，再选择其他可执行任务。

---

## 十、完成任务与解锁下游：`complete_task`

完成任务对应：

```text
in_progress → completed
```

首先检查：

```python
if task.status != "in_progress":
    return ...

if task.owner != owner:
    return ...
```

也就是说：

- 未认领的任务不能直接完成。
- 已完成任务不能再次完成。
- 不是当前 owner 的执行者不能完成任务。

### 1. 为什么需要 `ready_before`

完成任务之前，代码先计算当前已经可以开始的任务：

```python
ready_before = {
    candidate.id
    for candidate in list_tasks()
    if candidate.status == "pending"
    and candidate.blockedBy
    and can_start(candidate.id)
}
```

随后才把当前任务改为 `completed` 并保存。

保存后再次扫描：

```python
unblocked = [
    candidate.subject
    for candidate in list_tasks()
    if candidate.status == "pending"
    and candidate.blockedBy
    and candidate.id not in ready_before
    and can_start(candidate.id)
]
```

这个集合差的含义是：

```text
完成之后可以开始
并且
完成之前还不能开始
```

只有满足这两个条件，才是真正被当前完成动作“刚刚解锁”的任务。

### 2. 为什么不能只扫描完成后的状态

假设 API 和 Docs 早已因为 Schema 完成而可执行，现在完成了另一个完全无关的任务。

如果只在完成后扫描所有 `can_start == True` 的任务，就会错误地说 API 和 Docs 是刚刚被解锁的。

`ready_before` 正是用来排除这些原本就已就绪的任务。

### 3. 一个多依赖例子

假设 Deploy 同时依赖 Tests 和 Docs：

```json
"blockedBy": ["task_tests", "task_docs"]
```

- Tests 完成、Docs 未完成：Deploy 仍不能开始。
- Docs 完成、Tests 未完成：Deploy 仍不能开始。
- 两者都完成：Deploy 才会出现在 `unblocked` 中。

因此这里实现的是 AND 依赖，不是“任意一个依赖完成即可”。

---

## 十一、面向模型的五个任务工具

本章向模型暴露了：

| 工具 | 作用 |
| --- | --- |
| `create_task` | 创建任务，可以指定前置依赖 |
| `list_tasks` | 查看任务标题、状态、owner 和依赖摘要 |
| `get_task` | 查看单个任务的完整 JSON |
| `claim_task` | 认领一个依赖已完成的 pending 任务 |
| `complete_task` | 完成由当前 agent 认领的任务 |

每种工具仍有三层结构：

```text
TOOLS 中的 JSON Schema
  ↓ 告诉模型参数格式
run_create_task 等包装函数
  ↓ 把领域对象转换为文本结果
create_task / TaskStore 等核心逻辑
```

例如：

```text
模型调用 create_task
  ↓
execute_tool 根据 TOOL_HANDLERS 找到 run_create_task
  ↓
run_create_task 调用 create_task
  ↓
create_task 调用 TASKS.create
  ↓
TaskStore 创建 JSON 文件
  ↓
结果作为 tool_result 返回模型
```

基础文件工具、Hook 和 `execute_tool()` 与前几章基本相同。本章只需要记住：任务工具也通过同一条 Agent 工具执行管线运行。

---

## 十二、当前 `.tasks` 的真实状态

当前项目里有四个任务：

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| `task_76ba0742` | Setup database schema | completed | 无 |
| `task_23e97cfd` | Create API endpoints | pending | `task_76ba0742` |
| `task_3658651a` | Write docs | pending | `task_76ba0742` |
| `task_bfbeb9ff` | Write tests | pending | `task_23e97cfd` |

对应依赖图：

```text
task_76ba0742: Setup database schema [completed]
        │
        ├──> task_23e97cfd: Create API endpoints [pending, ready]
        │          │
        │          └──> task_bfbeb9ff: Write tests [pending, blocked]
        │
        └──> task_3658651a: Write docs [pending, ready]
```

由此可以得到：

```python
can_start("task_23e97cfd") is True
can_start("task_3658651a") is True
can_start("task_bfbeb9ff") is False
```

注意：`pending` 不等于 `blocked`。

- API 是 `pending`，但可以认领。
- Docs 是 `pending`，也可以认领。
- Tests 是 `pending`，但不能认领。

任务是否可执行，需要同时查看 `status` 和依赖状态。

---

# 用一条 Prompt 模拟完整任务流转

## 一、示例 Prompt

下面这条 Prompt 重点覆盖任务规划，不要求为了演示而修改已有四个任务，而是创建一组独立的演示任务：

```text
请使用任务工具完整演示一次带依赖的任务规划，并严格按以下顺序执行，不要只描述：

1. 先调用 list_tasks 查看现有任务。
2. 创建任务“FLOW-DEMO: prepare”，描述为“Prepare the demo environment”，不设置依赖；记住返回的任务 ID，称为 T1。
3. 创建任务“FLOW-DEMO: verify”，描述为“Verify the demo result”，设置 blockedBy=[T1]；记住返回的任务 ID，称为 T2。
4. 调用 get_task 查看 T2 的完整内容。
5. 在 T1 尚未完成时尝试 claim_task(T2)，确认它被 T1 阻塞。
6. 调用 claim_task(T1)，再调用 complete_task(T1)，观察 T2 是否被报告为刚刚解锁。
7. 调用 claim_task(T2)，再调用 complete_task(T2)。
8. 最后调用 list_tasks，确认 T1 和 T2 都是 completed。
9. 用文字总结 T1、T2 的真实 ID、依赖关系和状态变化，不要创建其他任务。
```

模型输出具有不确定性。这条 Prompt 强烈约束了调用顺序，但不能像普通 Python 测试一样百分之百保证模型严格执行。下面假设模型完全遵循要求。

---

## 二、接收用户输入

主程序读取 Prompt 后执行：

```python
trigger_hooks("UserPromptSubmit", query)
history.append({"role": "user", "content": query})
agent_loop(history)
```

此时消息历史大致是：

```python
[
    {
        "role": "user",
        "content": "请使用任务工具完整演示一次……",
    }
]
```

进入 `agent_loop()` 后，程序把这些内容发给模型：

- `SYSTEM`：告诉模型当前工作区及任务管理要求。
- `messages`：包含用户的完整 Prompt。
- `TOOLS`：包含基础工具和五个任务工具的 Schema。

---

## 三、第 1 次工具调用：查看已有任务

模型返回：

```json
{
  "name": "list_tasks",
  "input": {}
}
```

调用链：

```text
execute_tool
  ↓
run_list_tasks
  ↓
list_tasks
  ↓
TASKS.list
  ↓
逐个 TASKS.load
```

模型会看到现有四个任务。其中：

- API 和 Docs 已经就绪。
- Tests 仍被 API 阻塞。

这一步让 Agent 在创建新计划前先恢复已有项目状态，也是持久化任务系统相对于内存 todo 的重要优势。

---

## 四、第 2 次工具调用：创建根任务 T1

模型请求：

```json
{
  "name": "create_task",
  "input": {
    "subject": "FLOW-DEMO: prepare",
    "description": "Prepare the demo environment"
  }
}
```

执行路径：

```text
run_create_task
  ↓
create_task
  ↓
TASKS.create
  ↓
生成随机 ID 并写入 JSON
```

假设本次生成：

```text
T1 = task_aabbcc11
```

对应文件初始内容：

```json
{
  "id": "task_aabbcc11",
  "subject": "FLOW-DEMO: prepare",
  "description": "Prepare the demo environment",
  "status": "pending",
  "owner": null,
  "blockedBy": []
}
```

工具返回：

```text
Created task_aabbcc11: FLOW-DEMO: prepare
```

模型只有收到这次工具结果后，才知道真实的 T1 ID。因此依赖 T1 的任务通常必须在后续模型轮次中创建。

---

## 五、第 3 次工具调用：创建依赖任务 T2

模型使用刚才返回的 ID：

```json
{
  "name": "create_task",
  "input": {
    "subject": "FLOW-DEMO: verify",
    "description": "Verify the demo result",
    "blockedBy": ["task_aabbcc11"]
  }
}
```

`TaskStore.create()` 首先执行：

```python
self.exists("task_aabbcc11")
```

确认依赖文件存在后才创建 T2。

假设生成：

```text
T2 = task_ddeeff22
```

对应 JSON：

```json
{
  "id": "task_ddeeff22",
  "subject": "FLOW-DEMO: verify",
  "description": "Verify the demo result",
  "status": "pending",
  "owner": null,
  "blockedBy": [
    "task_aabbcc11"
  ]
}
```

此时依赖图是：

```text
T1 prepare [pending]
  ↓
T2 verify  [pending, blocked]
```

---

## 六、第 4 次工具调用：读取 T2

模型调用：

```json
{
  "name": "get_task",
  "input": {
    "task_id": "task_ddeeff22"
  }
}
```

调用链：

```text
run_get_task
  ↓
get_task
  ↓
load_task
  ↓
TASKS.load
```

这一步让模型获得 T2 的完整描述、当前状态和依赖，而不是只依靠 `list_tasks()` 的单行摘要。

---

## 七、第 5 次工具调用：提前认领 T2

模型调用：

```text
claim_task(task_ddeeff22)
```

内部执行：

```text
load_task(T2)
  ↓
确认 T2.status == pending
  ↓
incomplete_dependencies(T2)
  ↓
load_task(T1)
  ↓
发现 T1.status == pending
```

因此：

```python
incomplete_dependencies(T2) == ["task_aabbcc11"]
```

返回：

```text
Blocked by: ['task_aabbcc11']
```

认领失败不会抛异常，也不会修改 T2：

```text
T2: pending → pending
owner: None → None
```

这里体现了一个重要设计：模型可以提出动作，但任务系统在本地代码中强制执行依赖规则。模型不能仅凭一句“我要开始 T2”绕过前置任务。

---

## 八、第 6 次工具调用：认领 T1

模型调用：

```text
claim_task(task_aabbcc11)
```

T1 的 `blockedBy` 为空，因此：

```python
incomplete_dependencies(T1) == []
```

代码更新：

```python
task.owner = "agent"
task.status = "in_progress"
TASKS.save(task)
```

状态变化：

```text
T1: pending → in_progress
owner: None → agent
```

此时磁盘 JSON 已经同步更新。即使程序现在退出，下次启动后也能看出 T1 正在由 `agent` 执行。

---

## 九、第 7 次工具调用：完成 T1 并解锁 T2

模型调用：

```text
complete_task(task_aabbcc11)
```

### 1. 完成前检查

程序确认：

```text
T1.status == in_progress
T1.owner == agent
```

### 2. 记录 `ready_before`

程序扫描所有 pending 且有依赖的任务。

在当前真实任务数据中：

- API 已经因为 Schema 完成而就绪。
- Docs 已经因为 Schema 完成而就绪。
- Tests 仍被 API 阻塞。
- T2 仍被 T1 阻塞。

所以完成 T1 前，集合大致是：

```python
ready_before = {
    "task_23e97cfd",
    "task_3658651a",
}
```

### 3. 保存 T1 的完成状态

```text
T1: in_progress → completed
```

### 4. 再次扫描并计算 `unblocked`

此时 T2 的唯一依赖已经完成：

```python
can_start(T2) is True
```

而 T2 不在 `ready_before` 中，所以它是刚刚解锁的任务。

返回：

```text
Completed task_aabbcc11 (FLOW-DEMO: prepare)
Unblocked: FLOW-DEMO: verify
```

API 和 Docs 不会再次被报告为解锁，因为它们在 T1 完成前就已经就绪。

依赖图变为：

```text
T1 prepare [completed]
  ↓
T2 verify  [pending, ready]
```

---

## 十、第 8、9 次工具调用：认领并完成 T2

再次认领 T2 时：

```python
load_task(T1).status == "completed"
```

因此依赖检查通过：

```text
T2: pending → in_progress
owner: None → agent
```

随后调用 `complete_task(T2)`：

```text
T2: in_progress → completed
```

如果没有其他任务依赖 T2，那么这次返回中不会有 `Unblocked:`。

最终演示任务图：

```text
T1 prepare [completed, owner=agent]
  ↓
T2 verify  [completed, owner=agent]
```

---

## 十一、第 10 次工具调用：最终检查

模型再次调用 `list_tasks()`。

新增任务应显示为：

```text
[x] task_aabbcc11: FLOW-DEMO: prepare [completed] [agent]
[x] task_ddeeff22: FLOW-DEMO: verify [completed] [agent]
    (blockedBy: task_aabbcc11)
```

注意 T2 即使已经完成，仍然保存 `blockedBy: task_aabbcc11`。完成任务不会破坏原来的依赖图。

模型确认状态后，不再请求工具，而是输出最终总结。`agent_loop()` 随之结束，主程序打印最终文本并等待下一条用户输入。

---

## 十二、这次模拟中的状态变化总表

| 时刻 | T1 状态 | T2 状态 | T2 是否可开始 | 发生的动作 |
| --- | --- | --- | --- | --- |
| 创建后 | pending | pending | 否 | T2 依赖 T1 |
| 提前认领 T2 | pending | pending | 否 | 返回 `Blocked by` |
| 认领 T1 后 | in_progress | pending | 否 | T1 owner 设为 agent |
| 完成 T1 后 | completed | pending | 是 | T2 被解锁 |
| 认领 T2 后 | completed | in_progress | 已开始 | T2 owner 设为 agent |
| 完成 T2 后 | completed | completed | 已完成 | 整条任务链结束 |

---

## 十三、这套设计保证了什么

### 1. 任务可以跨会话恢复

状态写在 `.tasks/*.json` 中，而不是只保存在 `history` 或全局列表里。

### 2. 依赖规则由代码执行

模型负责提出 `claim_task`，但能否认领由 `incomplete_dependencies()` 决定。

### 3. 状态迁移有顺序

正常路径只能是：

```text
pending → in_progress → completed
```

不能直接从 pending 跳到 completed。

### 4. owner 提供基本所有权检查

只有认领任务的 owner 才能完成它。

### 5. 可以识别真正新解锁的任务

通过比较完成前后的就绪集合，避免把原本就能开始的任务重复报告为“刚解锁”。

---

## 十四、当前实现没有完全解决的问题

这份代码适合展示 Task System 的核心概念，但还不是完整的生产级调度器。

### 1. 没有依赖环检测

如果任务依赖形成环，任务可能永久阻塞。

### 2. 认领不是原子操作

`claim_task()` 的过程是“读取 → 检查 → 修改 → 写入”。两个进程同时认领同一任务时，可能都先读到 `pending`，随后相互覆盖。

生产实现通常需要文件锁、数据库事务或 compare-and-swap。

### 3. 保存不是原子替换

`save()` 直接覆盖原文件。更稳妥的做法是先写临时文件，再原子替换。

### 4. owner 实际固定为 `agent`

虽然任务结构支持 owner，但任务工具没有暴露 owner 参数，因此暂时不能区分多个 Agent。

### 5. 没有失败、取消和重新打开状态

当前只有：

```text
pending / in_progress / completed
```

实际系统可能还需要：

```text
failed / cancelled / blocked / paused
```

### 6. 完成任务依赖 Agent 自觉

`complete_task()` 只检查状态和 owner，不验证真实代码、测试或交付物是否已经完成。

也就是说，“任务完成”的真实性仍然依赖 Agent 的执行质量和验证流程。

### 7. 每次依赖检查都重新读取文件

`complete_task()` 会多次调用 `list_tasks()` 和 `can_start()`。任务数量较大时，会重复读取很多 JSON 文件。

教学项目中逻辑清晰比性能更重要；规模扩大后可以考虑建立内存索引或使用数据库查询。

---

## 十五、最值得记住的设计思想

### 1. 规划信息需要成为结构化状态

如果计划只存在于模型自然语言里，程序无法可靠判断依赖、负责人和状态迁移。

### 2. 模型负责决策，代码负责约束

模型决定尝试执行哪个任务；Python 代码负责判断该动作是否合法。

### 3. 持久化使 Agent 可以恢复工作

对话可能结束、上下文可能压缩、进程可能重启，但任务文件仍然存在。

### 4. 依赖图比线性清单更接近真实项目

真实工作往往不是严格的一条直线：Schema 完成后，API 和 Docs 可以分别开始；Tests 则继续等待 API。

### 5. “pending”和“blocked”是两个维度

`pending` 描述生命周期状态；是否 blocked 则由依赖的实时状态推导出来。

### 6. `blockedBy` 保存关系，`can_start` 计算当前事实

依赖关系不会因为前置任务完成而消失。系统保留结构，再根据任务状态动态判断是否可执行。

---

## 最终总结

`s10_task_system` 建立了一条完整的任务生命周期：

```text
把目标拆成任务
  ↓
为任务声明 blockedBy 依赖
  ↓
把每个任务保存到独立 JSON 文件
  ↓
读取任务图并寻找依赖已完成的任务
  ↓
claim：pending → in_progress，并记录 owner
  ↓
执行和验证实际工作
  ↓
complete：in_progress → completed
  ↓
重新计算并报告刚刚解锁的下游任务
```

本章最核心的提升不是多了五个工具，而是把“Agent 心里的一份计划”变成了“程序能够读取、验证、持久化和推进的任务状态”。
