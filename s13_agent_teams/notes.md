# s13 Agent Teams 学习笔记

本文完整记录 `s13_agent_teams/code.py` 的代码结构、各组成部分的职责，以及一次覆盖任务系统、Git worktree、消息总线、计划审批、常驻队友和关闭协议的详细程序流转。

这份代码实现了一个“主 Agent 带领多个常驻队友”的命令行系统。核心结构可以先表示为：

```text
用户输入
   ↓
Lead 主 Agent
   ├── 创建/认领任务 → .tasks/
   ├── 启动队友线程 → TeammateRuntime
   ├── 发送消息     → .mailboxes/
   └── 创建工作区   → .worktrees/

队友：WORK → 完成任务 → IDLE → 自动认领新任务 → WORK
```

# 第一部分：完整代码说明

下面按“文件从上到下 + 实际执行顺序”理解代码。

## 1. 初始化运行环境

位置：`code.py:1`

程序首先：

- 导入线程、文件、Git、JSON、Anthropic SDK 等模块。
- `load_dotenv(override=True)` 加载 `.env`。
- 创建 Anthropic 客户端。
- 从 `MODEL_ID` 环境变量读取模型。
- 把启动程序时的当前目录保存为 `WORKDIR`。

```python
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

因此最好从仓库根目录执行：

```bash
python s13_agent_teams/code.py
```

否则 `WORKDIR`、`.tasks`、`.mailboxes` 和 `.worktrees` 都会建立在错误位置。

如果设置了 `ANTHROPIC_BASE_URL`，代码还会删除环境变量 `ANTHROPIC_AUTH_TOKEN`：

```python
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
```

这是为了在使用自定义兼容端点时，避免另一种鉴权变量干扰 Anthropic 客户端的认证方式。

## 2. 任务系统

位置：`code.py:55`

一个任务由 `Task` 表示：

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None
```

任务实际保存在：

```text
.tasks/task_xxxxxxxx.json
```

任务状态只有三种：

```text
pending → in_progress → completed
```

关键函数按顺序是：

- `create_task()`：创建任务文件。
- `save_task()`：先写临时文件，再用 `os.replace()` 原子替换。
- `load_task()`：读取并校验任务。
- `list_tasks()`：列出全部任务。
- `get_task()`：返回单个任务的完整 JSON。
- `can_start()`：判断依赖任务是否全部完成。
- `_owner_in_progress()`：查找某个 owner 当前进行中的任务。
- `_incomplete_dependencies()`：列出尚未完成的依赖。
- `claim_task()`：认领任务。
- `complete_task()`：完成任务。

`blockedBy` 实现任务依赖。例如：

```text
task_A: 写后端
task_B: 写测试，blockedBy=[task_A]
```

只有 `task_A` 完成，`task_B` 才能被认领。

### 2.1 任务 ID 和路径安全

任务 ID 必须匹配：

```python
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
```

`_task_path()` 不只是拼接路径，还会验证解析后的路径仍位于 `.tasks/` 内：

```python
path = (TASKS_DIR / f"{task_id}.json").resolve()
if (not TASKS_ROOT.is_relative_to(WORKDIR.resolve())
        or not path.is_relative_to(TASKS_ROOT)):
    raise ValueError(...)
```

这样可以防止通过伪造任务 ID 实现路径穿越。

### 2.2 为什么有两层锁

```python
task_lock = threading.RLock()
fcntl.flock(...)
```

- `threading.RLock`：防止当前进程中的多个队友线程同时修改任务。
- `fcntl.flock`：防止多个 Python 进程同时修改 `.tasks`。

`RLock` 是可重入锁，因为 `claim_task()` 持锁时还会调用 `load_task()`、`save_task()` 等也需要锁的函数。

`task_store_lock()` 使用线程局部变量 `_task_store_state.depth` 记录嵌套深度。只有最外层进入时才打开 `.tasks/.lock` 并获取操作系统文件锁，只有最外层退出时才释放文件锁并关闭文件。这避免同一线程嵌套调用时重复获取和释放跨进程锁。

### 2.3 创建和保存任务

`create_task()` 会先清理任务主题并去重依赖：

```python
subject = subject.strip()
dependencies = list(dict.fromkeys(blockedBy or []))
```

它会确认每一个依赖任务文件都存在，然后最多尝试 100 次随机任务 ID：

```python
id=f"task_{secrets.token_hex(4)}"
```

任务文件以 `"x"` 模式创建，只有文件不存在时才能成功，因此即使随机 ID 碰撞也不会覆盖原有任务。

`save_task()` 先写唯一的临时文件，再调用：

```python
os.replace(temporary, path)
```

这样其他线程或进程不会读到只写了一半的 JSON 文件。

### 2.4 任务认领

`claim_task()` 会依次检查：

1. 任务必须是 `pending`。
2. 任务不能已有 owner。
3. 当前 owner 不能在 `teammate_assignments` 中占用另一个任务。
4. 当前 owner 不能还有另一个 `in_progress` 任务。
5. 所有依赖必须完成。
6. 绑定的 worktree 必须可用。
7. 把任务改成 `in_progress` 并记录 owner。
8. 在 `teammate_assignments` 中记录任务 ID 和工作目录。
9. 调用 `advance_assignment_version()` 让旧审批失效。

所以一个队友同一时间只能处理一个任务。

### 2.5 完成任务

`complete_task()` 检查：

1. 任务必须是 `in_progress`。
2. 调用者必须是任务 owner。
3. 当前计划状态不能是 `required`、`pending` 或 `rejected`。
4. 当前 assignment 必须能恢复并解析出有效工作目录。

任务完成后，它会扫描全部 pending 任务，找出刚刚因为依赖完成而变得可执行的任务，并打印 `[unblocked]`。

这里的“解除阻塞”不是给任务写入一种新状态，而是下一次调用 `can_start()` 时从 `False` 变为 `True`。任务本身仍然是 `pending`。

## 3. Task 绑定 Git worktree

位置：`code.py:297`

worktree 的目的，是让不同队友在不同目录和分支中工作，减少文件冲突。

```text
主仓库
├── .worktrees/frontend → wt/frontend
└── .worktrees/backend  → wt/backend
```

`validate_worktree_name()` 要求名称长度为 1 到 64，只包含字母、数字、点、下划线或横线，以字母或数字开头，并且不能包含 `..`。

`_worktree_path()` 再次验证解析后的路径位于 `.worktrees/` 内，且不能正好等于 `.worktrees/` 根目录。

### 3.1 读取 Git worktree 注册表

`_registered_worktrees()` 执行：

```bash
git worktree list --porcelain
```

然后解析每个 worktree 的路径、HEAD 和 branch 信息。

`_registered_worktree(name)` 不仅要求目录存在，还要求 Git 注册表中的分支精确等于：

```text
refs/heads/wt/<name>
```

这避免一个目录碰巧存在，但并不是当前程序按约定创建的 worktree。

### 3.2 创建 worktree

`create_worktree()` 的执行顺序：

1. 校验 worktree 名称。
2. 校验任务 ID。
3. 要求任务仍然是 `pending` 且无人认领。
4. 要求任务尚未绑定 worktree。
5. 检查名称是否已经被其他任务使用。
6. 检查目标目录是否存在。
7. 确认 `WORKDIR` 是当前 Git 仓库根目录。
8. 使用 `git check-ref-format` 校验分支名称。
9. 检查分支是否已经存在。
10. 检查 Git worktree 注册表是否存在路径冲突。
11. 执行 `git worktree add`。
12. 把 `task.worktree` 更新为对应名称。

实际命令是：

```bash
git worktree add -b wt/<name> <path> HEAD
```

任务被认领后，文件工具的默认目录就会变成该 worktree。

如果 Git 报错但已经留下目录、注册表记录或分支，代码不会擅自清理这些数据。它会返回 `Partial operation`，要求用户检查并手动处理，以免误删可能已经产生的工作。

### 3.3 解析 assignment 工作目录

`assignment_cwd(owner)` 会综合检查：

- owner 当前是否有 `in_progress` 任务；
- 内存 assignment 是否与任务文件一致；
- 任务的 owner 是否正确；
- worktree 是否仍被 Git 注册；
- assignment 中缓存的 cwd 是否发生变化。

如果没有 assignment，也没有进行中的任务，则返回主工作目录 `WORKDIR`。

### 3.4 释放 assignment

`release_completed_assignment(owner)` 只在模型回合边界释放已经完成的任务工作目录租约。这样模型调用 `complete_task` 后，本轮剩余工具仍然保持一致的工作目录，不会在同一轮中突然从 worktree 切回主仓库。

`release_teammate_assignment(owner)` 用于队友线程异常或退出。如果队友仍有 `in_progress` 任务，它会把任务恢复为：

```text
pending + owner=None
```

这样队友崩溃不会永久占住任务。

### 3.5 删除 worktree

`remove_worktree()` 只有在以下条件全部满足时才删除 checkout：

- worktree 是合法且已注册的；
- worktree 已绑定某个任务；
- 所有绑定任务都已完成；
- 没有队友仍在使用；
- 没有未提交修改，或者显式要求丢弃修改。

它会保留 Git 分支。不过这个函数目前没有加入模型的 `TOOLS` 和 `TOOL_HANDLERS`，所以 Agent 不能直接调用它，只能由宿主代码或用户在程序之外处理。

## 4. 主 Agent 的系统提示词

位置：`code.py:577`

`PROMPT_SECTIONS` 告诉 Lead：

- 它是一个编码 Agent。
- 可以使用哪些工具。
- 创建队伍前要先征得用户确认。
- 应先创建任务，再把任务交给队友。
- 只有单独工作目录能够避免冲突时才创建 task-bound worktree。
- 一个队友必须完成当前任务后才能认领另一个任务。
- worktree 只改变工具的默认 cwd，并不是安全沙箱。
- 启动队友后不要轮询，等待消息事件自动唤醒。
- 协作结束后关闭队友。

最后拼成：

```python
SYSTEM = "\n\n".join(PROMPT_SECTIONS.values())
```

这个 `SYSTEM` 只给 Lead 使用；队友有自己单独的 system prompt。

## 5. 文件和 Shell 工具

位置：`code.py:606`

基础工具包括：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`

### 5.1 路径限制

`safe_path()` 防止文件路径通过 `../` 逃出当前工作目录：

```python
base = (cwd or WORKDIR).resolve()
path = (base / p).resolve()
if not path.is_relative_to(base):
    raise ValueError(f"Path escapes workspace: {p}")
```

当任务绑定 worktree 时，这里的 `base` 是 worktree，而不是主仓库。因此队友不能通过文件工具访问 worktree 之外的主仓库文件。

### 5.2 Shell 工具

`run_bash()` 使用：

```python
subprocess.run(
    command,
    shell=True,
    cwd=cwd or WORKDIR,
    capture_output=True,
    text=True,
    timeout=120,
)
```

它限制单次执行最多 120 秒，输出最多返回 50000 个字符。这里使用 `shell=True`，因此真正的安全控制主要依赖后面的权限 Hook；worktree 只是 cwd，不是 shell 沙箱。

### 5.3 文件工具

- `run_read()`：读取文本，支持按行数截断。
- `run_write()`：创建父目录并覆盖写入内容。
- `run_edit()`：要求 `old_text` 在文件中精确出现一次，然后替换。
- `run_glob()`：按 glob 模式寻找文件，最多返回 200 个结果。

### 5.4 Lead 的工具包装层

`run_agent_*` 是 Lead 使用的包装层。它先调用：

```python
assignment_cwd("agent")
```

如果 Lead 认领了绑定 worktree 的任务，所有文件操作就自动落在那个 worktree 中；否则使用主仓库。

调用链例如：

```text
run_agent_write
  → _agent_cwd
  → assignment_cwd("agent")
  → run_write(path, content, cwd)
```

## 6. Task 工具包装函数

位置：`code.py:717`

这部分把内部任务函数包装成适合模型调用并返回文本的 handler：

- `run_create_task()`
- `run_list_tasks()`
- `run_get_task()`
- `run_claim_task()`
- `run_complete_task()`

Lead 的 owner 固定使用字符串 `"agent"`。队友则在自己的 runtime 中把 owner 替换为队友名称。

`run_list_tasks()` 使用图标展示状态：

```text
[ ] pending
[~] in_progress
[x] completed
```

并同时展示 owner、依赖和 worktree。

## 7. 消息总线

位置：`code.py:770`

每个 Agent 都有一个 JSONL 邮箱：

```text
.mailboxes/lead.jsonl
.mailboxes/frontend.jsonl
.mailboxes/tester.jsonl
```

队友名称必须匹配：

```python
^[A-Za-z0-9_-]{1,64}$
```

`lead` 和 `agent` 是运行时保留名称，不能用作队友名称。

### 7.1 发送消息

`MessageBus.send()` 构造消息：

```python
{
    "from": from_agent,
    "to": to_agent,
    "content": content,
    "type": msg_type,
    "ts": time.time(),
    "metadata": metadata or {},
}
```

它把消息追加到目标邮箱，并通过 `threading.Condition` 唤醒等待中的队友线程。

### 7.2 读取消息

`read_inbox()` 是“破坏性读取”：

```python
msgs = [json.loads(line) ...]
inbox.unlink()
```

也就是读取后立即删除邮箱文件，避免同一消息被重复处理。

`peek()` 只检查是否存在非空邮箱，不消费消息。

`wait_for_messages()` 使用 Condition 阻塞，直到收到消息或超时。队友用它避免持续高频轮询。

### 7.3 消息类型

消息除了普通文本，还有协议消息：

- `plan_approval_request`
- `plan_approval_response`
- `shutdown_request`
- `shutdown_response`
- `result`
- `idle_notification`
- `error`

`ProtocolState` 用来追踪一次计划审批或关闭请求，`request_id` 用于防止响应串线。

## 8. 团队协议状态

位置：`code.py:852`

队友状态由 `active_teammates` 保存：

```text
working
waiting_approval
idle
stopping
```

`ProtocolState` 保存：

```python
request_id
type
sender
target
status
payload
work_version
task_id
created_at
```

`new_request_id()` 生成形如 `req_123456` 的 ID，并确保不与当前 `pending_requests` 冲突。

`match_response()` 验证响应类型、发送者、接收者和状态都与请求一致，然后把请求改成 `approved` 或 `rejected`。

`consume_lead_inbox()` 在把团队事件交给 Lead 模型之前，先处理所有以 `_response` 结尾的协议响应。这样模型看到事件时，内部协议状态已经同步。

`format_team_events()` 将邮箱消息转成：

```text
[Team events]
[消息类型 request_id=...] 发送者: 内容
```

然后作为一条新的 user message 交给 Lead。

## 9. 计划审批机制

位置：`code.py:942`

如果启动队友时指定：

```python
require_plan=True
```

队友必须先调用 `submit_plan`。

状态大致是：

```text
required → pending → approved
                   ↘ rejected → 重新提交
```

### 9.1 提交计划

`_teammate_submit_plan()` 会把计划绑定到当前：

- teammate 名称；
- assignment version；
- task ID。

然后把 `plan_gates[name]` 设置为 `pending`，把队友状态设置为 `waiting_approval`，并向 Lead 发送 `plan_approval_request`。

### 9.2 计划门禁

计划未批准时，以下工具会被 `_run_teammate_tool()` 拦截：

- `bash`
- `write_file`
- `edit_file`

`read_file` 和 `glob` 不属于修改型工具，因此不受这个门禁限制。但没有任务 assignment 时，队友仍会因为没有有效工作目录而无法使用工作区工具。

### 9.3 assignment version

`assignment_versions` 会在认领任务、释放任务或放弃任务时递增。

如果 assignment 改变，`advance_assignment_version()` 会：

```python
assignment_versions[owner] += 1
```

并把非 `not_required` 的计划门禁重置为 `required`，删除当前计划请求 ID。这样旧任务的计划批准不能被拿来授权新任务，避免“过期审批”问题。

### 9.4 Lead 审批

`run_review_plan()` 会验证：

- 请求是否存在；
- 请求是否确实是计划；
- 状态是否仍是 pending；
- 请求的 work version 是否与当前一致；
- 请求的 task ID 是否与当前一致；
- 它是否仍是该队友当前的计划请求。

批准或拒绝后，Lead 向队友发送 `plan_approval_response`。队友通过 `apply_plan_response()` 再次核对协议身份，只有匹配当前工作身份的响应才会生效。

## 10. 空闲任务发现

位置：`code.py:1051`

`scan_unclaimed_tasks()` 返回满足以下条件的任务：

- `status == "pending"`；
- `owner is None`；
- 所有依赖完成；
- 绑定的 worktree 可以正常解析。

`claim_next_task(name)` 先确保队友没有当前 assignment 和进行中任务，然后按 `list_tasks()` 返回的顺序尝试认领第一个仍可用的任务。

这里没有按队友角色、任务描述或创建批次过滤任务。只要任务已经 ready，任何空闲队友都可能认领。

## 11. 队友的 WORK / IDLE 循环

位置：`code.py:1085`

每个队友对应一个 `TeammateRuntime` 和一个后台线程。

核心状态循环在 `run()`：

```python
state = "continue"
while state != "stop":
    if state == "idle" and not self.wait_for_work():
        break
    state = self.work()
```

### 11.1 初始化队友

`TeammateRuntime.__init__()` 创建队友自己的 system prompt，强调：

- 使用工具完成分配的任务；
- 完成后必须调用 `complete_task`；
- 初始任务已经被 runtime 认领时不要重复认领；
- 要求计划时必须先提交并等待批准；
- 文件工具使用任务工作目录；
- 最终文本由 runtime 自动发送给 Lead；
- `send_message` 只用于中间协调。

如果传入初始 `task_id`，首条消息会追加任务信息和工作目录。如果 `require_plan=True`，还会追加计划要求。

### 11.2 队友工具

队友 handler 包括：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`
- `send_message`
- `submit_plan`
- `list_tasks`
- `claim_task`
- `complete_task`

队友不能调用 `spawn_teammate`、`review_plan`、`request_shutdown` 等 Lead 工具。

### 11.3 `work()` 的顺序

1. 读取自己的邮箱。
2. 处理关闭、计划审批、计划要求和普通消息。
3. 将有效消息追加到自己的对话历史。
4. 把状态设为 `working`。
5. 调用 Anthropic 模型。
6. 如果模型返回工具调用，执行工具并把结果加入历史。
7. 返回 `continue`，开始下一个模型回合。
8. 如果模型没有调用工具，提取最终文本。
9. 如果计划不在 pending 状态，把最终文本作为 `result` 发给 Lead。
10. 如果仍在等待审批，状态改为 `waiting_approval`。
11. 否则释放已经完成的 assignment，状态改成 `idle`。
12. 向 Lead 发送 `idle_notification`。

### 11.4 `wait_for_work()` 的顺序

队友空闲后会：

1. 每次最多等待邮箱消息 2 秒。
2. 收到新消息就处理。
3. 如果消息在对话中产生了新工作内容，就恢复 `WORK`。
4. 没收到消息就扫描 `.tasks`。
5. 找到可执行的无人任务时自动认领。
6. 把 `[Auto-claimed task ...]` 追加到模型历史。
7. 返回 `WORK` 状态继续调用模型。

所以队友不是“一次性 subagent”，而是常驻 worker：

```text
启动 → 工作 → 空闲 → 自动找任务 → 再工作 → 收到关闭请求 → 退出
```

### 11.5 队友退出清理

无论正常关闭还是异常退出，`run()` 的 `finally` 都会：

1. 调用 `release_teammate_assignment()`。
2. 如果还有未完成任务，将任务恢复到 pending 且清空 owner。
3. 删除 active teammate 状态。
4. 删除计划门禁和当前计划请求 ID。
5. 删除线程注册。

异常信息会尽量作为 `error` 消息发送给 Lead。

## 12. Lead 如何启动和控制队友

位置：`code.py:1315`

`spawn_teammate_thread()` 会：

1. 校验队友名称。
2. 防止使用保留名称 `lead` 和 `agent`。
3. 防止同名队友重复启动，名称比较不区分大小写。
4. 注册队友状态和计划门禁。
5. 如果传了 `task_id`，先替队友认领任务。
6. 如果认领失败，回滚活动队友注册。
7. 创建 `TeammateRuntime`。
8. 创建 daemon 线程并启动。

```python
thread = threading.Thread(target=runtime.run, daemon=True)
thread.start()
```

因此 Lead 和所有队友共享同一个 Python 进程，但各自在独立线程中调用模型。

Lead 团队工具包括：

- `run_spawn_teammate()`：启动队友。
- `run_list_teammates()`：查看活动队友及状态。
- `run_send_message()`：向活动队友发送普通消息。
- `run_request_shutdown()`：创建关闭协议并请求队友关闭。
- `run_request_plan()`：将队友计划状态设为 required 并发送计划请求。
- `run_review_plan()`：批准或拒绝计划。
- `run_create_worktree()`：创建 task-bound worktree。

## 13. 工具定义与分发

位置：`code.py:1439`

代码将工具分成三组：

- `BASE_TOOLS`：Shell 和文件操作。
- `TASK_TOOLS`：任务管理。
- `TEAM_TOOLS`：队友、消息、计划和 worktree 管理。

Lead 使用全部工具：

```python
TOOLS = [*BASE_TOOLS, *TASK_TOOLS, *TEAM_TOOLS]
```

队友使用受限的 `TEAMMATE_TOOLS`，例如队友不能自行启动新的队友。

`TOOL_HANDLERS` 将模型返回的工具名映射到 Python 函数：

```python
"create_task": run_create_task
"spawn_teammate": run_spawn_teammate
"review_plan": run_review_plan
```

工具定义解决“告诉模型有哪些工具以及参数结构”的问题，handler 映射解决“模型选择工具后实际执行哪个 Python 函数”的问题。

## 14. Hook 和权限检查

位置：`code.py:1586`

支持四类 Hook：

```text
UserPromptSubmit
PreToolUse
PostToolUse
Stop
```

默认注册了：

- 用户输入时显示当前工作目录。
- 工具执行前进行权限检查和日志记录。
- 工具执行后检查超大输出。
- 会话停止时统计工具调用次数。

Shell 命令分成两类：

- `DENY_LIST`：直接拒绝，例如 `sudo`、`shutdown`、`rm -rf /`。
- `DESTRUCTIVE`：Lead 执行时询问用户；队友不能弹出终端询问，因此直接要求交给 Lead。

文件工具访问 `WORKDIR` 之外路径时，Lead 可以收到交互式授权询问；队友因为运行在后台线程中不能进行交互式确认，所以会直接收到权限错误。

`_run_teammate_tool()` 调用 Hook 时使用：

```python
trigger_hooks("PreToolUse", block, skip_permission=True)
```

因为队友权限已经由 `check_permission(..., prompt_user=False)` 单独检查，随后只运行日志等其他 PreToolUse Hook，避免重复询问。

## 15. Lead 模型循环

位置：`code.py:1691`

`agent_loop()` 是标准的工具调用循环：

```text
发送 messages 给模型
        ↓
模型是否返回 tool_use？
  ├── 是：执行工具 → 把 tool_result 加回 messages → 再调用模型
  └── 否：结束本轮
```

关键点是：模型本身不会直接执行 Python 函数。模型只返回“我要调用哪个工具及参数”，然后 `execute_tool()` 才真正执行。

如果模型 API 调用抛出异常，代码会把错误作为 assistant 文本加入历史，释放 Lead 已完成的 assignment，触发 Stop Hook，然后结束本轮。

当模型不再请求工具时，代码同样会释放 Lead 已完成的 assignment，触发 Stop Hook，并退出 `agent_loop()`。

多个 tool call 出现在一次模型响应中时，当前实现按响应中的顺序逐个执行，并把所有结果组合成一条 user tool-result 消息再发回模型。

## 16. 程序真正的入口

位置：`code.py:1763`

启动后进入无限循环：

```python
while True:
    kind, payload = wait_for_cli_event()
```

`wait_for_cli_event()` 同时监听：

- 用户的终端输入；
- Lead 邮箱是否出现队友消息。

### 16.1 用户输入时

```text
用户输入
→ UserPromptSubmit hook
→ 加入 history
→ agent_loop(history)
→ 打印模型最终回复
```

输入 `q`、`exit` 或空字符串时退出程序。

### 16.2 队友发消息时

```text
队友写入 lead 邮箱
→ CLI 检测到邮箱
→ consume_lead_inbox()
→ 格式化成 [Team events]
→ 作为一条新的 user message 加入 history
→ 自动唤醒 Lead 模型
```

这就是注释里“不要轮询队友”的实现：队友有事件时，消息总线会让 Lead 自动开始新一轮。

### 16.3 打印最终回答与团队状态

每次 `agent_loop()` 结束后，`print_last_assistant_message()` 打印历史中最后一条 assistant 文本。

如果曾经有活动队友，而现在已经全部退出且 Lead 邮箱没有剩余消息，程序打印：

```text
[all teammates shut down]
```

最后用一句话概括整份代码：

> Lead 负责和用户交互及调度；Task 文件保存共享工作状态；worktree 隔离代码目录；MessageBus 负责异步通信；TeammateRuntime 让后台队友在工作与空闲之间持续循环。

# 第二部分：完整模拟程序流转

下面按照集成演练 Prompt 的理想执行路径，详细模拟一次程序流转。模型决定具体调用哪些工具，队友线程并发运行，所以细节时序可能变化，但运行时机制基本固定。

本次模拟包含三个任务：

- 任务 A：由 Lead 认领，使用基础文件工具创建和编辑测试文件。
- 任务 B：依赖 A，绑定独立 Git worktree，交给要求计划审批的 analyst。
- 任务 C：依赖 B，由空闲 runner 自动扫描并认领，验证 assignment 改变后旧计划失效。

整体时序如下：

```text
用户
 │ 输入集成测试 Prompt
 ▼
Lead CLI 主循环
 │
 ├─ Lead 完成任务 A
 │
 ├─ 创建任务 B、C
 │    B blockedBy A，并绑定 worktree
 │    C blockedBy B
 │
 ├─ 启动 analyst ───────────────┐
 │                              │
 ├─ 启动 runner ─────────────┐  │
 │                           │  │
 │◀──── analyst 计划请求 ────┘  │
 ├─ 批准计划                    │
 │                              ▼
 │                         analyst 执行 B
 │                              │
 │◀──────── B 完成事件 ─────────┘
 │
 │                    C 解除阻塞
 │                           │
 │                    runner 自动认领 C
 │                           │
 │◀──── runner 新计划请求 ───┘
 ├─ 批准新计划
 │
 │                    runner 执行 C
 │                           │
 │◀──────── C 完成事件 ──────┘
 │
 ├─ 检查任务状态
 ├─ 请求关闭 analyst、runner
 │◀──── shutdown_response
 ▼
最终汇总
```

## 1. 程序启动阶段

入口位于 `code.py:1763`。

运行：

```bash
python s13_agent_teams/code.py
```

Python 会先从上到下执行模块级代码。

### 1.1 初始化环境

执行：

```python
load_dotenv(override=True)
WORKDIR = Path.cwd()
client = Anthropic(...)
MODEL = os.environ["MODEL_ID"]
```

此时确定：

- 仓库工作目录 `WORKDIR`；
- Anthropic 客户端；
- 使用的模型；
- `.tasks/`、`.mailboxes/`、`.worktrees/` 所在位置。

### 1.2 创建全局运行状态

主要包括：

```python
teammate_assignments = {}
assignment_versions = {}

BUS = MessageBus()

active_teammates = {}
plan_gates = {}
plan_request_ids = {}
pending_requests = {}
teammate_threads = {}
```

刚启动时还没有活动队友。

### 1.3 注册 Hooks

位置：`code.py:1667`

注册：

```text
UserPromptSubmit → context_hook
PreToolUse       → permission_hook
PreToolUse       → log_hook
PostToolUse      → large_output_hook
Stop             → summary_hook
```

### 1.4 进入 CLI 主循环

```python
while True:
    kind, payload = wait_for_cli_event()
```

`wait_for_cli_event()` 每隔 0.25 秒同时检查：

- 用户有没有输入；
- `.mailboxes/lead.jsonl` 有没有队友消息。

终端显示：

```text
s13 >>
```

## 2. 用户提交 Prompt

用户粘贴集成测试 Prompt 并回车。

`wait_for_cli_event()` 返回：

```python
("user", 用户输入)
```

随后执行：

```python
trigger_hooks("UserPromptSubmit", payload)
history.append({"role": "user", "content": payload})
agent_loop(history)
```

`context_hook()` 打印：

```text
[hook] UserPromptSubmit: working in ...
```

然后进入 Lead 的模型循环。

## 3. Lead 模型工具循环

位置：`code.py:1691`

Lead 请求模型：

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM,
    messages=messages,
    tools=TOOLS,
)
```

如果模型返回工具调用，例如：

```text
glob
read_file
bash
create_task
```

运行时依次执行每个调用：

```python
execute_tool(block)
```

完整调用链是：

```text
agent_loop
  → execute_tool
      → trigger_hooks("PreToolUse")
          → permission_hook
          → log_hook
      → TOOL_HANDLERS[工具名]
      → trigger_hooks("PostToolUse")
          → large_output_hook
  → 构造 tool_result
  → 重新请求模型
```

只要模型继续返回工具调用，这个循环就不会结束。

## 4. 阶段一：Lead 完成任务 A

### 4.1 查看代码文件

模型调用：

```text
glob(pattern="s13_agent_teams/*.py")
```

执行路径：

```text
execute_tool
→ run_agent_glob
→ _agent_cwd
→ assignment_cwd("agent")
→ run_glob
```

Lead 此时没有认领任务，所以：

```python
assignment_cwd("agent") == WORKDIR
```

`glob` 返回类似：

```text
s13_agent_teams/code.py
```

### 4.2 阅读代码

调用：

```text
read_file(
  path="s13_agent_teams/code.py",
  limit=80
)
```

执行：

```text
run_agent_read
→ _agent_cwd
→ run_read
→ safe_path
→ Path.read_text
```

`safe_path()` 确保路径没有逃出当前工作目录。

### 4.3 检查 Git 状态

调用：

```text
bash(command="git status --short")
```

执行：

```text
run_agent_bash
→ _agent_cwd
→ run_bash
→ subprocess.run(...)
```

权限 Hook 检查命令不在危险列表中，然后放行。

### 4.4 创建任务 A

模型调用：

```text
create_task(
  subject="Lead baseline exercise",
  description="..."
)
```

调用链：

```text
run_create_task
→ create_task
→ task_store_lock
→ _task_path
→ 创建 .tasks/task_xxxxxxxx.json
```

任务文件内容类似：

```json
{
  "id": "task_a1b2c3d4",
  "subject": "Lead baseline exercise",
  "description": "...",
  "status": "pending",
  "owner": null,
  "blockedBy": [],
  "worktree": null
}
```

`task_store_lock()` 同时使用：

- `threading.RLock`；
- `.tasks/.lock` 上的 `fcntl.flock`。

保证线程和进程之间不会同时修改任务。

### 4.5 查看任务

调用：

```text
get_task(task_id="task_a1b2c3d4")
```

执行：

```text
run_get_task
→ get_task
→ load_task
→ 读取 JSON
→ Task(**data)
```

然后调用：

```text
list_tasks()
```

返回：

```text
[ ] task_a1b2c3d4: Lead baseline exercise [pending]
```

### 4.6 Lead 认领任务 A

调用：

```text
claim_task(task_id="task_a1b2c3d4")
```

执行路径：

```text
run_claim_task
→ claim_task(owner="agent")
```

`claim_task()` 检查：

1. 状态是不是 `pending`；
2. owner 是否为空；
3. Lead 是否已经有其他任务；
4. 依赖是否完成；
5. worktree 是否有效。

任务 A 没有 worktree，因此：

```python
cwd = WORKDIR
```

状态变成：

```json
{
  "status": "in_progress",
  "owner": "agent"
}
```

内存中增加：

```python
teammate_assignments["agent"] = {
    "task_id": "task_a1b2c3d4",
    "cwd": WORKDIR
}
```

`advance_assignment_version("agent")` 把 Lead 的工作版本递增。

### 4.7 写入测试文件

调用：

```text
write_file(
  path=".agent_team_demo/lead.txt",
  content="PLACEHOLDER"
)
```

因为 Lead 已经认领任务，文件工具先找到任务工作目录：

```text
run_agent_write
→ assignment_cwd("agent")
→ WORKDIR
→ run_write
→ safe_path
```

`run_write()` 自动创建父目录：

```python
fp.parent.mkdir(parents=True, exist_ok=True)
```

最终产生：

```text
.agent_team_demo/lead.txt
```

### 4.8 精确编辑文件

调用：

```text
edit_file(
  path=".agent_team_demo/lead.txt",
  old_text="PLACEHOLDER",
  new_text="LEAD_BASELINE_COMPLETE"
)
```

`run_edit()` 要求旧文本只出现一次：

```python
count = content.count(old_text)
if count != 1:
    return "Error"
```

替换后再调用 `read_file` 验证。

### 4.9 完成任务 A

调用：

```text
complete_task(task_id="task_a1b2c3d4")
```

执行：

```text
run_complete_task
→ complete_task(owner="agent")
```

检查：

- 状态必须是 `in_progress`；
- owner 必须是 `agent`；
- Lead 没有未通过的计划门禁。

然后写入：

```json
{
  "status": "completed",
  "owner": "agent"
}
```

注意：`complete_task()` 不会立即删除 `teammate_assignments["agent"]`。

只有当模型结束这一轮、不再返回工具调用时，`agent_loop()` 才执行：

```python
release_completed_assignment("agent")
```

这是“在模型轮次边界释放工作目录租约”。如果 Lead 在释放前尝试认领另一个任务，会因为现有 assignment 仍存在而被拒绝。

## 5. 阶段二：创建任务 B 和 C

### 5.1 创建任务 B

调用：

```text
create_task(
  subject="Worktree analyst exercise",
  blockedBy=["task_a1b2c3d4"]
)
```

因为任务 A 已完成，B 虽然初始状态是 `pending`，但已经可以开始。

状态：

```text
A completed
B pending, ready
```

### 5.2 给 B 创建 worktree

调用：

```text
create_worktree(
  name="flowdemo-xxxxxxxx",
  task_id="task_bbbbbbbb"
)
```

执行：

```text
run_create_worktree
→ create_worktree
```

内部检查：

1. worktree 名称是否合法；
2. B 是否存在；
3. B 是否仍为 `pending`；
4. B 是否还没有 owner；
5. 名称是否绑定其他任务；
6. 路径是否已存在；
7. 当前目录是不是 Git 仓库根；
8. 分支是否已经存在；
9. Git worktree 注册表中是否存在冲突。

然后执行：

```bash
git worktree add -b wt/flowdemo-xxxxxxxx \
  .worktrees/flowdemo-xxxxxxxx HEAD
```

成功后更新任务 B：

```json
{
  "worktree": "flowdemo-xxxxxxxx"
}
```

之后认领 B 的队友，其文件和 Shell 工具默认目录会变成：

```text
.worktrees/flowdemo-xxxxxxxx
```

注意，这个 worktree 基于当前 `HEAD` 创建。任务 A 创建但未提交的 `lead.txt` 不会自动出现在这个 worktree 中。

### 5.3 创建任务 C

调用：

```text
create_task(
  subject="Auto claim verification exercise",
  blockedBy=["task_bbbbbbbb"]
)
```

此时 B 尚未完成，所以：

```python
can_start(task_C) == False
```

任务状态快照：

| 任务 | 状态 | Owner | 依赖 | Worktree |
|---|---|---|---|---|
| A | completed | agent | 无 | 无 |
| B | pending | 无 | A | flowdemo-… |
| C | pending | 无 | B | 无 |

## 6. 阶段三：启动 analyst

Lead 调用：

```text
spawn_teammate(
  name="analyst",
  role="repository analyst",
  prompt="...",
  task_id="task_bbbbbbbb",
  require_plan=true
)
```

调用链：

```text
run_spawn_teammate
→ spawn_teammate_thread
```

### 6.1 注册队友

写入：

```python
active_teammates["analyst"] = "working"
plan_gates["analyst"] = "required"
assignment_versions["analyst"] = 0
```

### 6.2 认领任务 B

因为提供了 `task_id`，启动线程前先执行：

```python
claim_task(task_B, owner="analyst")
```

任务 B 变成：

```json
{
  "status": "in_progress",
  "owner": "analyst"
}
```

任务工作目录解析为：

```text
.worktrees/flowdemo-xxxxxxxx
```

内存中：

```python
teammate_assignments["analyst"] = {
    "task_id": "task_bbbbbbbb",
    "cwd": WORKTREE_PATH
}
```

### 6.3 创建队友运行时

创建：

```python
runtime = TeammateRuntime(...)
```

它拥有独立的：

- system prompt；
- 对话历史 `self.messages`；
- 工具 handler；
- 任务工作目录。

首条消息会自动追加：

```text
[Assigned task task_bbbbbbbb] Worktree analyst exercise
...
Work directory: .../.worktrees/flowdemo-xxxxxxxx

[Plan required] Submit a plan and wait for Lead approval...
```

### 6.4 启动线程

```python
thread = threading.Thread(
    target=runtime.run,
    daemon=True
)
thread.start()
```

随后 analyst 和 Lead 并发运行。

`spawn_teammate_thread()` 返回文本要求 Lead 结束当前回合，让后续团队事件唤醒 Lead，而不是在当前回合中轮询队友。

## 7. analyst 提交计划

analyst 线程进入：

```python
TeammateRuntime.run()
```

初始：

```python
state = "continue"
```

因此直接执行：

```python
state = self.work()
```

`work()` 首先读取邮箱，然后请求模型。

模型看到 `[Plan required]`，调用：

```text
submit_plan(plan="...")
```

执行路径：

```text
_run_teammate_tool
→ _teammate_submit_plan
```

创建协议状态：

```python
pending_requests["req_123456"] = ProtocolState(
    type="plan_approval",
    sender="analyst",
    target="lead",
    status="pending",
    work_version=当前版本,
    task_id="task_bbbbbbbb"
)
```

并更新：

```python
plan_gates["analyst"] = "pending"
plan_request_ids["analyst"] = "req_123456"
active_teammates["analyst"] = "waiting_approval"
```

然后发送消息：

```python
BUS.send(
    "analyst",
    "lead",
    plan,
    "plan_approval_request",
    {"request_id": "req_123456"}
)
```

写入：

```text
.mailboxes/lead.jsonl
```

如果 analyst 在批准前尝试 `bash`、`write_file` 或 `edit_file`，`_run_teammate_tool()` 会返回计划状态为 pending 的阻止信息，不会实际修改工作区。

## 8. Lead 被队友事件唤醒

Lead 此时已经结束上一轮，重新回到：

```python
wait_for_cli_event()
```

它发现：

```python
BUS.peek("lead") == True
```

于是返回：

```python
("wake", None)
```

主循环执行：

```python
inbox = consume_lead_inbox()
```

读取并删除 `.mailboxes/lead.jsonl`，然后将消息格式化为：

```text
[Team events]
[plan_approval_request request_id=req_123456]
analyst: ...
```

这段事件作为新的 `user` 消息追加到 Lead 的 `history`，随后再次调用：

```python
agent_loop(history)
```

所以队友事件在模型看来相当于“系统自动替用户发来了一条新消息”。

## 9. Lead 批准 analyst 计划

Lead 调用：

```text
review_plan(
  request_id="req_123456",
  approve=true,
  feedback="批准，只能修改 .agent_team_demo/analyst.txt"
)
```

执行：

```text
run_review_plan
```

它校验：

1. `request_id` 是否存在；
2. 类型是否为 `plan_approval`；
3. 状态是否仍为 `pending`；
4. 计划是否属于 analyst 当前任务；
5. `work_version` 是否仍然一致；
6. 是否仍是 analyst 当前计划。

校验通过后：

```python
state.status = "approved"
```

向 analyst 发出：

```text
plan_approval_response
```

analyst 的 `handle_inbox()` 收到后调用：

```python
apply_plan_response("analyst", msg)
```

最终：

```python
plan_gates["analyst"] = "approved"
active_teammates["analyst"] = "working"
```

现在 analyst 才能执行：

- `bash`；
- `write_file`；
- `edit_file`。

## 10. analyst 执行任务 B

analyst 调用文件工具时走的是自己的方法：

```text
self.glob
self.read
self.bash
self.write
self.edit
```

例如：

```text
write_file(".agent_team_demo/analyst.txt", ...)
```

调用链：

```text
_run_teammate_tool
→ 检查 plan_gates["analyst"] == "approved"
→ self.write
→ current_cwd
→ assignment_cwd("analyst")
→ worktree 路径
→ run_write
```

所以实际写入：

```text
.worktrees/flowdemo-xxxxxxxx/
└── .agent_team_demo/
    └── analyst.txt
```

而不是主工作区中的 `.agent_team_demo/analyst.txt`。

完成编辑和验证后，analyst 调用：

```text
complete_task(task_id="task_bbbbbbbb")
```

执行：

```text
self.complete
→ complete_task(owner="analyst")
```

B 状态变为：

```text
completed
```

接着 `complete_task()` 扫描其他任务：

```python
unblocked = [
    t.subject
    for t in list_tasks()
    if t.status == "pending"
    and t.blockedBy
    and can_start(t.id)
]
```

此时发现 C 的依赖 B 已完成，打印：

```text
[unblocked] Auto claim verification exercise
```

注意：C 的 JSON 状态仍然是 `pending`。所谓“解除阻塞”不是写入一个新状态，而是 `can_start(C)` 从 `False` 变成 `True`。

## 11. analyst 返回结果并进入 IDLE

当 analyst 模型不再调用工具时，`work()` 提取最后一段文本：

```python
summary = _last_assistant_text(response.content)
```

然后发送：

```text
analyst → lead: result
```

释放完成任务：

```python
release_completed_assignment("analyst")
```

这会删除：

```python
teammate_assignments["analyst"]
```

并增加 assignment version。

随后状态变为：

```python
active_teammates["analyst"] = "idle"
```

再发送：

```text
analyst → lead: idle_notification
```

`run()` 接下来进入：

```python
wait_for_work()
```

## 12. runner 的启动和计划请求过程

Lead 启动 runner 时不传初始任务：

```text
spawn_teammate(
  name="runner",
  role="verification runner",
  prompt="当前先进入空闲状态……"
)
```

此时：

```python
teammate_assignments["runner"]
```

不存在。

runner 的首次 `work()` 仍会调用模型。因为没有 assignment，它不能成功使用工作区工具。理想情况下它只返回准备就绪的信息，runtime 将结果发送给 Lead，然后发送 `idle_notification` 并进入 `wait_for_work()`。

Lead 在 runner 空闲时调用：

```text
request_plan(
  teammate="runner",
  task="说明任务 C 解锁后的执行计划"
)
```

执行：

```text
run_request_plan
```

更新：

```python
plan_gates["runner"] = "required"
```

并向 runner 发送：

```text
plan_request
```

runner 的 `wait_for_messages()` 被 Condition 唤醒，`handle_inbox()` 将它转换成：

```text
[Plan required] 说明任务 C 解锁后的执行计划
```

runner 调用 `submit_plan`。

此时 runner 尚未认领任务，因此计划身份是：

```python
work_version = 0
task_id = None
```

Lead 收到计划事件后调用 `review_plan` 批准。

状态变为：

```python
plan_gates["runner"] = "approved"
```

## 13. runner 自动认领任务 C

runner 空闲时执行：

```python
wait_for_work()
```

内部循环：

```python
inbox = BUS.wait_for_messages(
    self.name,
    IDLE_SCAN_INTERVAL
)
```

每两秒超时一次。如果没有消息，就执行：

```python
task = claim_next_task("runner")
```

调用链：

```text
claim_next_task
→ 确认 runner 没有当前任务
→ scan_unclaimed_tasks
→ list_tasks
→ 检查 pending、owner、can_start、worktree
→ claim_task(task_C, owner="runner")
```

C 状态变为：

```json
{
  "status": "in_progress",
  "owner": "runner"
}
```

因为 C 没有 worktree，所以工作目录是主仓库：

```python
cwd = WORKDIR
```

随后追加新的模型消息：

```text
[Auto-claimed task task_cccccccc]
Auto claim verification exercise
...
Work directory: 仓库根目录
```

## 14. 为什么 runner 可能需要重新提交计划

认领 C 时，`claim_task()` 调用了：

```python
advance_assignment_version("runner")
```

它会：

1. 增加 runner 的工作版本；
2. 如果计划门禁不是 `not_required`，重置为 `required`；
3. 删除旧的 `plan_request_ids`。

因此原计划：

```text
work_version=0, task_id=None
```

不能授权新任务：

```text
work_version=1, task_id=task_C
```

此时 runner 如果直接调用：

```text
write_file
```

`_run_teammate_tool()` 会返回：

```text
Blocked: plan status is required.
Submit or revise the plan and wait for approval...
```

runner 再调用 `submit_plan`，新计划会绑定：

```python
work_version = 1
task_id = "task_cccccccc"
```

Lead 收到事件后再次调用 `review_plan`。

批准后：

```python
plan_gates["runner"] = "approved"
```

这就验证了“旧计划审批不能跨任务复用”。

## 15. runner 执行任务 C

runner 在主工作区执行：

```text
read_file(".agent_team_demo/lead.txt")
bash("git status --short")
write_file(".agent_team_demo/runner.txt", "RUNNER_PLACEHOLDER")
edit_file(..., "RUNNER_PLACEHOLDER", "RUNNER_COMPLETE")
send_message("lead", "...")
read_file(".agent_team_demo/runner.txt")
complete_task(task_C)
```

中间消息路径：

```text
runner
→ _teammate_send_message
→ BUS.send
→ .mailboxes/lead.jsonl
→ wait_for_cli_event 检测
→ consume_lead_inbox
→ format_team_events
→ Lead agent_loop
```

完成后 C 变为：

```text
completed
```

runner 发送：

```text
result
idle_notification
```

并进入 IDLE。

## 16. 任务最终状态

Lead 调用：

```text
list_tasks
```

理想输出：

```text
[x] task_A: Lead baseline exercise [completed] [agent]
[x] task_B: Worktree analyst exercise [completed] [analyst]
    (worktree: flowdemo-...)
[x] task_C: Auto claim verification exercise [completed] [runner]
```

任务的 `owner` 在完成后不会清空，所以仍能看出是谁完成的。

文件分布是：

```text
主工作区
└── .agent_team_demo/
    ├── lead.txt
    └── runner.txt

任务 B 的 worktree
└── .agent_team_demo/
    └── analyst.txt
```

## 17. 关闭队友

Lead 调用：

```text
request_shutdown(teammate="analyst")
```

执行：

```text
run_request_shutdown
```

创建：

```python
ProtocolState(
    type="shutdown",
    sender="lead",
    target="analyst",
    status="pending"
)
```

然后发送：

```text
shutdown_request
```

analyst 当前阻塞在：

```python
BUS.wait_for_messages(...)
```

收到后：

```text
handle_inbox
→ apply_shutdown_request
```

校验请求来自 Lead 且 request ID 有效，然后：

```python
active_teammates["analyst"] = "stopping"
```

analyst 回复：

```text
shutdown_response
```

并让 `handle_inbox()` 返回 `True`，最终跳出 `run()` 循环。

`finally` 清理：

```python
release_teammate_assignment("analyst")
active_teammates.pop("analyst")
plan_gates.pop("analyst")
plan_request_ids.pop("analyst")
teammate_threads.pop("analyst")
```

runner 也走相同流程。

Lead 收到 `shutdown_response` 时：

```text
consume_lead_inbox
→ match_response
```

协议状态从：

```text
pending → approved
```

当所有队友退出后，主循环打印：

```text
[all teammates shut down]
```

## 18. 关键状态变化汇总

| 时刻 | A | B | C | analyst | runner |
|---|---|---|---|---|---|
| 创建后 | pending | 无 | 无 | 无 | 无 |
| Lead 认领 A | in_progress | 无 | 无 | 无 | 无 |
| A 完成 | completed | 无 | 无 | 无 | 无 |
| 创建 B、C | completed | pending | pending/blocked | 无 | 无 |
| 启动 analyst | completed | in_progress | blocked | required | 无 |
| 批准计划 | completed | in_progress | blocked | approved/working | idle |
| B 完成 | completed | completed | pending/ready | idle | idle |
| runner 自动认领 | completed | completed | in_progress | idle | required |
| 新计划批准 | completed | completed | in_progress | idle | approved |
| C 完成 | completed | completed | completed | idle | idle |
| 关闭后 | completed | completed | completed | removed | removed |

## 19. 两个可能影响模拟结果的并发点

### 19.1 analyst 和 runner 竞争任务 C

`analyst` 完成 B 后也会进入 IDLE，也有资格自动认领 C。因此 C 不保证一定被 runner 抢到；两个线程会竞争 `task_lock`，先进入 `claim_task()` 的队友获胜。

如果需要稳定复现 runner 自动认领 C，可以在 analyst 完成 B 后先关闭 analyst，再让 runner 成为唯一的空闲队友。但关闭请求和空闲扫描本身也是并发的，最稳妥的演练方式是调整任务和队友启动顺序，使 C 解除阻塞时只有 runner 处于活动空闲状态。

### 19.2 原有 pending 任务干扰

`.tasks/` 中如果已有其他可执行的 `pending` 任务，空闲队友可能先认领旧任务，因为 `scan_unclaimed_tasks()` 不知道 Prompt 中的角色分工，只会返回所有可执行任务。

若要稳定复现，最好在没有旧 pending 任务的测试仓库运行，或者先确保现有任务都不是 ready 状态。不能仅靠给 runner 的自然语言提示阻止 runtime 自动领取其他 ready 任务，因为自动领取发生在模型调用之外。

# 第三部分：运行和观察时必须注意的条件

## 1. 模型调用具有非确定性

Prompt 可以要求模型按顺序调用工具，但工具选择仍由模型决定。它可能把多个工具放在同一次响应中，也可能拆成多个模型回合，还可能在某一步返回解释文本而没有调用预期工具。因此上述流转描述的是代码支持的完整理想路径，不是运行时硬编码的脚本。

## 2. Lead 和队友是真正并发的线程

Lead CLI、analyst 和 runner 共享内存与文件系统，但分别运行在不同线程中。任务锁只能保证任务状态修改的原子性，不能保证某个队友一定比另一个队友先醒来或先认领任务。

## 3. worktree 是 cwd 隔离，不是完整安全隔离

文件工具受到 `safe_path()` 限制，但 `bash` 使用 `shell=True`，理论上仍可通过绝对路径访问 worktree 之外的位置。代码中的系统提示词和权限 Hook 承担额外约束。因此注释明确说明：worktree changes tool default cwd only; it is not a sandbox。

## 4. worktree 不会自动包含主目录的未提交修改

`git worktree add ... HEAD` 基于 Git 的 `HEAD` 创建新工作目录。主仓库中尚未提交的任务 A 文件不会复制到任务 B 的 worktree。不同 worktree 中产生的修改也不会自动合并回主分支。

## 5. worktree 和分支会保留

`remove_worktree()` 没有暴露给模型，所以集成演练结束后，`.worktrees/flowdemo-...` 和 `wt/flowdemo-...` 分支仍然存在。需要用户检查修改并决定是否提交、合并或手动移除。

## 6. Windows 兼容性

代码直接导入并使用：

```python
import fcntl
```

原生 Windows Python 通常没有 `fcntl`。此外，`select.select([sys.stdin], ...)` 对普通 Windows 控制台标准输入的支持也与 Unix 不同。因此当前实现更适合在 Linux、macOS 或 WSL 中运行。在原生 Windows Python 中，程序可能在读取 Prompt 之前就因 `ModuleNotFoundError: fcntl` 退出。

## 7. 持久化状态会跨程序运行保留

任务文件、邮箱文件和 worktree 都位于磁盘上：

```text
.tasks/
.mailboxes/
.worktrees/
```

程序重新启动后，`.tasks/` 中的任务仍然存在。邮箱如果在上次异常退出时未被消费，也可能在下一次运行中被读取。内存中的 `active_teammates`、`pending_requests` 和 assignment version 不会跨进程恢复，因此异常退出后的磁盘状态需要人工检查。
