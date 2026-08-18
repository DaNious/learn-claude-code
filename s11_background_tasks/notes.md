# s11 Background Tasks 学习笔记

## 一、本章解决什么问题

前几章的工具调用都是同步的：模型调用 Bash 后，主线程必须等命令执行完，才能把 `tool_result` 返回给模型。读取小文件或执行 `git status` 时问题不大，但安装依赖、构建项目、运行完整测试往往需要很长时间。

`s11_background_tasks` 增加了一条后台执行路径：

```text
模型调用 bash(run_in_background=True)
  → 主线程创建后台线程
  → 后台线程创建 Shell 子进程
  → 主线程立即返回 bg_id 占位结果
  → Agent Loop 可以继续处理其他工具
  → 后台线程保存最终结果
  → 后续 Agent Loop 在调用模型前收集结果
  → 以 <task_notification> 注入对话
```

这一章最重要的不是“多了一个布尔参数”，而是同时管理两类状态：

1. `_shell_processes`：当前仍需要管理和清理的操作系统子进程。
2. `BackgroundManager`：后台任务从运行、完成到被模型消费的逻辑状态。

必须区分这两类状态。一个 Shell 进程结束后，会立刻从 `_shell_processes` 删除；但它对应的后台任务结果仍会保留在 `BackgroundManager` 中，直到主线程下一次收集。

---

## 二、线程、进程与 Agent Loop 的关系

### 1. 三层执行结构

```text
Python 主线程
├─ 读取用户输入
├─ 调用 LLM
├─ 执行普通文件工具
├─ 执行同步 Bash，并等待其 Shell 进程
└─ 启动后台 Python 线程
   └─ 后台线程执行 _run()
      └─ 创建并等待一个 Shell 子进程
```

因此，后台任务并不是“在线程里直接执行 Bash 代码”，而是：

```text
一个后台任务
  = 一个 daemon Python 线程
  + 该线程启动并管理的一个 Shell 进程组
```

如果同时启动两个后台 Bash，典型结构是：

```text
Python 进程
├─ 主线程
├─ 后台线程 A ──> Shell 进程组 A
└─ 后台线程 B ──> Shell 进程组 B
```

如果这时主线程又执行一个同步 Bash，会短暂变为：

```text
Python 进程
├─ 主线程     ──> 同步 Shell 进程组
├─ 后台线程 A ──> Shell 进程组 A
└─ 后台线程 B ──> Shell 进程组 B
```

### 2. “后台”不等于“主动通知”

后台线程完成时只会写入共享内存：

```python
self.results[task_id] = result
self._ready.append(task_id)
```

它不会直接调用模型，也不会主动唤醒已经结束的 `agent_loop()`。

真正的通知发生在 Agent Loop 下一轮开头：

```python
while True:
    inject_background_results(messages)
    response = client.messages.create(...)
```

所以准确说法是：

> 后台线程负责完成工作并登记结果，主线程负责在后续轮次轮询、收集并通知模型。

---

## 三、启动阶段

### 1. 环境与客户端

```python
load_dotenv(override=True)
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

- `WORKDIR` 是启动程序时的当前目录，不一定是 `code.py` 所在目录。
- 所有 Shell 命令都在 `WORKDIR` 下运行。
- 文件工具也以 `WORKDIR` 为基准解析路径。
- `MODEL_ID` 不存在时，程序会在启动阶段抛出 `KeyError`。

系统提示词要求模型只把独立命令放到后台：

```python
"Set run_in_background to true only for independent Bash commands."
```

这是提示词约束，不是程序能够自动证明的依赖关系。最终仍由模型决定是否传入 `run_in_background=True`。

### 2. 全局 Shell 进程注册表

```python
_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()
```

`_shell_processes` 保存当前由程序启动、仍需要生命周期管理的 `Popen` 对象。它同时包含：

- 主线程启动的同步 Bash 进程；
- 后台线程启动的后台 Bash 进程。

它不区分任务 ID，也不保存命令结果，职责只有一个：

> 跟踪仍需清理的操作系统进程。

使用 `RLock` 是因为多个线程可能同时向集合添加或删除进程，程序退出时主线程也可能同时读取集合。

### 3. 退出清理注册

```python
atexit.register(_stop_all_shell_processes)
signal.signal(signal.SIGTERM, _handle_termination_signal)
```

存在两条清理路径：

```text
正常退出
  → atexit
  → _stop_all_shell_processes()

收到 SIGTERM
  → _handle_termination_signal()
  → _stop_all_shell_processes()
  → SystemExit
  → atexit 再次执行清理
```

重复清理通常是安全的，因为进程不存在时 `_stop_process_group()` 会直接返回。

---

## 四、Shell 进程的完整生命周期

核心函数是 `_run_bash_process(command)`。

### 1. 创建进程

```python
process = subprocess.Popen(
    command,
    shell=True,
    cwd=WORKDIR,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    start_new_session=True,
)
```

关键参数：

- `shell=True`：通过系统 Shell 执行命令。
- `cwd=WORKDIR`：把工作目录固定为 Agent 工作区。
- `stdout/stderr=PIPE`：捕获命令输出。
- `text=True`：以字符串而不是字节读取输出。
- `start_new_session=True`：让命令进入新的 session/进程组，便于后续整体停止。

### 2. 登记到 `_shell_processes`

```python
with _shell_process_lock:
    _shell_processes.add(process)
```

状态变化：

```text
创建前：_shell_processes = {...其他仍在运行的进程...}
创建后：_shell_processes = {...其他进程..., 当前 process}
```

只有完成登记后，退出清理逻辑才能发现这个进程。

### 3. 等待与读取输出

```python
stdout, stderr = process.communicate(timeout=120)
```

调用它的线程会等待这个进程：

- 同步 Bash：阻塞主线程；
- 后台 Bash：只阻塞对应后台线程，不阻塞主线程。

输出随后被合并并截断：

```python
output = (stdout + stderr).strip()
return (output[:50000] if output else "(no output)"), process.returncode
```

### 4. 超时或启动异常

```python
except subprocess.TimeoutExpired:
    return "Error: Timeout (120s)", None
except OSError as error:
    return f"Error: {type(error).__name__}: {error}", None
```

后台管理器用退出码判断状态：

```python
status = "completed" if exit_code == 0 else "failed"
```

因此超时或 `OSError` 返回的 `exit_code=None` 会被后台管理器标记为 `failed`。

### 5. `finally` 中移除进程

不论成功、失败还是超时，最终都会执行：

```python
finally:
    if process is not None:
        _stop_process_group(process)
        ...
        with _shell_process_lock:
            _shell_processes.discard(process)
```

这里先尝试清理原进程组中可能残留的子进程，再把 `Popen` 对象从集合删除。

状态变化：

```text
命令运行时：_shell_processes = {..., process}
finally 后： _shell_processes = {...}
```

`discard()` 而不是 `remove()` 的好处是：即使集合中已经没有这个对象，也不会抛出 `KeyError`。

### 6. `_shell_processes` 不保存历史

进程结束后就会被删除。因此不能通过 `_shell_processes` 查询：

- 某个命令以前是否运行过；
- 某个后台任务成功还是失败；
- 某个后台任务的输出是什么。

这些信息由 `BackgroundManager` 保存。

---

## 五、基础工具与工具协议

程序向模型暴露五个工具：

| 工具 | 作用 | 是否能后台执行 |
| --- | --- | --- |
| `bash` | 执行 Shell 命令 | 可以 |
| `read_file` | 读取文件 | 不可以 |
| `write_file` | 写文件 | 不可以 |
| `edit_file` | 替换一次精确文本 | 不可以 |
| `glob` | 搜索工作区文件 | 不可以 |

`bash` schema 新增：

```python
"run_in_background": {"type": "boolean"}
```

`run_bash()` 虽然接收这个参数：

```python
def run_bash(command: str, run_in_background: bool = False) -> str:
```

但它自己不进行后台调度。真正的路由发生在 `execute_tool()` 中。保留该参数是为了让 `call_tool()` 使用 `handler(**block.input)` 时参数能够匹配。

---

## 六、Hooks 与权限检查

启动时注册：

```python
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
```

后台任务同样先经过 `PreToolUse`：

```text
execute_tool(block)
  → permission_hook(block)
  → log_hook(block)
  → 判断同步或后台
```

这很重要：命令不会因为被放进后台就绕过权限检查。

对于后台任务，`PostToolUse` 收到的是启动占位结果：

```text
[Background task bg_0001 started]
```

后台线程稍后完成时不会再次触发 `PostToolUse`。

---

## 七、`BackgroundManager` 的四份核心状态

初始化代码：

```python
class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._ready: list[str] = []
        self._counter = 0
        self._lock = threading.Lock()
```

### 1. `tasks`

保存后台任务元数据和逻辑状态：

```python
{
    "bg_0001": {
        "tool_use_id": "toolu_...",
        "command": "python ...",
        "status": "running",
    }
}
```

生命周期：

```text
不存在
  → start() 登记为 running
  → _run() 更新为 completed 或 failed
  → collect() 删除
```

### 2. `results`

只保存已经结束、尚未收集的最终输出：

```python
{
    "bg_0001": "A:start\nA:done"
}
```

生命周期：

```text
任务运行时不存在
  → _run() 完成后写入
  → collect() 取出并删除
```

### 3. `_ready`

保存“已经完成、等待主线程收集”的任务 ID：

```python
["bg_0002", "bg_0001"]
```

它是按完成顺序追加的，不一定等于启动顺序。

生命周期：

```text
任务运行时不在列表中
  → _run() 完成后 append(task_id)
  → collect() 统一 clear()
```

### 4. `_counter`

每次启动任务时加一：

```text
0 → bg_0001
1 → bg_0002
2 → bg_0003
```

收集任务不会回退计数器，所以同一进程内不会重复使用已经生成过的 ID。

### 5. `BackgroundManager._lock`

它保护的是：

- `tasks`
- `results`
- `_ready`
- `_counter`

它和 `_shell_process_lock` 职责不同：

| 锁 | 保护对象 | 所属层次 |
| --- | --- | --- |
| `_shell_process_lock` | `_shell_processes` | 操作系统进程生命周期 |
| `BackgroundManager._lock` | 任务、结果、就绪队列和计数器 | 后台任务逻辑状态 |

两把锁分开，使进程管理和任务状态管理互不混杂。

---

## 八、`BackgroundManager.start()`：登记并启动后台线程

调用前先验证：

```python
if block.name != "bash":
    raise ValueError(...)

if not isinstance(command, str) or not command.strip():
    raise ValueError(...)
```

随后在锁内生成任务 ID 并登记：

```python
with self._lock:
    self._counter += 1
    task_id = f"bg_{self._counter:04d}"
    self.tasks[task_id] = {
        "tool_use_id": block.id,
        "command": command,
        "status": "running",
    }
```

必须先登记、再启动线程。这样线程即使非常快地执行完，也能在 `tasks` 中找到自己的任务记录。

接着创建线程：

```python
thread = threading.Thread(
    target=self._run,
    args=(task_id, command),
    daemon=True,
)
thread.start()
```

`daemon=True` 表示这个线程不会阻止 Python 解释器退出，但这并不代表不需要清理 Shell 子进程，所以代码仍然维护 `_shell_processes` 和 `atexit` 清理逻辑。

如果 `thread.start()` 本身失败，代码会回滚刚才写入的 `tasks`：

```python
with self._lock:
    self.tasks.pop(task_id, None)
```

---

## 九、`BackgroundManager._run()`：后台工作线程

线程入口执行：

```python
output, exit_code = _run_bash_process(command)
result = _format_bash_result(output, exit_code)
status = "completed" if exit_code == 0 else "failed"
```

然后在锁内提交完成状态：

```python
with self._lock:
    task = self.tasks.get(task_id)
    if task is None:
        return
    task["status"] = status
    self.results[task_id] = result
    self._ready.append(task_id)
```

一次成功任务的状态迁移：

```text
tasks[id].status: running → completed
results[id]:        不存在 → 命令输出
_ready:             [...] → [..., id]
```

一次失败任务的状态迁移：

```text
tasks[id].status: running → failed
results[id]:        不存在 → 格式化错误
_ready:             [...] → [..., id]
```

注意：进入这段提交逻辑之前，`_run_bash_process()` 的 `finally` 已经把对应 `Popen` 从 `_shell_processes` 删除。因此经常会出现：

```text
_shell_processes 中已经没有该进程
但 BackgroundManager 中仍保留 completed/failed 任务及结果
```

这是正常且必要的状态。

---

## 十、`collect()`：消费已完成任务

`collect()` 在锁内取出所有就绪任务：

```python
for task_id in self._ready:
    task = self.tasks.pop(task_id, None)
    result = self.results.pop(task_id, "")
    ...
self._ready.clear()
```

这里的语义是“消费”，不是“只读查询”：

```text
收集前：任务和结果仍在 BackgroundManager 中
收集后：tasks/results/_ready 中都不再保存该任务
```

随后生成通知：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>...</command>
  <summary>...</summary>
</task_notification>
```

通知中的结果只保留前 500 个字符：

```python
result[:500]
```

而 `collect()` 同时删除了 `results` 中的完整结果。因此超过 500 字符的内容在收集后不会继续保留。

---

## 十一、同步与后台的路由

判断函数：

```python
def should_run_background(tool_name: str, tool_input: dict) -> bool:
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )
```

必须同时满足：

1. 工具名严格等于 `bash`；
2. `run_in_background` 严格为布尔值 `True`。

`execute_tool()` 的完整顺序：

```text
PreToolUse hooks
  → 被拒绝：直接返回拒绝原因
  → 被允许：should_run_background()
      ├─ True  → start_background_task() → 立即返回 bg_id 占位结果
      └─ False → call_tool() → 同步等待真实结果
  → PostToolUse hooks
```

后台任务的原始 `tool_use` 必须马上对应一个 `tool_result`，所以程序返回占位结果：

```text
[Background task bg_0001 started]
The result will be collected on a later turn.
```

最终结果不能再次复用原来的 `tool_use_id`，否则一个 `tool_use` 会对应两个 `tool_result`，破坏工具协议。因此最终结果作为普通文本 `<task_notification>` 注入。

虽然 `tasks` 中保存了 `tool_use_id`，当前实现收集时并没有使用它。

---

## 十二、Agent Loop 如何收集通知

```python
def agent_loop(messages: list):
    while True:
        inject_background_results(messages)
        response = client.messages.create(...)
        ...
```

每次请求模型之前都先检查 `_ready`。

如果上一条消息已经是用户消息，通知就追加到它的内容列表：

```python
content.extend(blocks)
```

否则创建新的用户消息：

```python
messages.append({"role": "user", "content": blocks})
```

如果模型不再调用工具，`agent_loop()` 会结束。此后即使后台任务完成，也不会主动触发新的模型调用；要等用户下一次提交 prompt，新的 `agent_loop()` 才会收集它。

---

# 用一条 Prompt 演示完整程序流转

## 十三、示例 Prompt

下面的 prompt 覆盖：

- 两个后台 Bash；
- 一个成功任务和一个失败任务；
- 精简的文件读写工具；
- 一个同步 Bash；
- 后台结果收集和最终总结。

```text
请严格按照下面的顺序完成一次后台任务调度演示：

1. 在同一轮响应中，首先调用 bash 启动后台任务 A，必须设置 run_in_background=true：
   python -c "import time; print('A:start', flush=True); time.sleep(2); print('A:done')"

2. 紧接着调用 bash 启动后台任务 B，也必须设置 run_in_background=true：
   python -c "import sys,time; print('B:start', flush=True); time.sleep(1); print('B:failed', file=sys.stderr); sys.exit(7)"

3. 然后依次调用这些工具：
   - write_file：创建 tmp/s11-flow-demo.txt，内容为 hello background
   - read_file：读取 tmp/s11-flow-demo.txt
   - edit_file：把 hello 替换为 goodbye
   - glob：搜索 tmp/*.txt

4. 最后调用一次同步 bash，必须设置 run_in_background=false：
   python -c "import time; time.sleep(3); print('foreground wait done')"

5. 在收到后台任务 A 和 B 的两个 <task_notification> 之前，不要给出最终回答。

6. 收到通知后，总结：
   - 两个后台任务的 task_id
   - 哪个 completed
   - 哪个 failed
   - 同步命令的输出
   - 文件最终内容

不要添加或改动上述步骤。
```

模型输出具有不确定性，prompt 只能强约束调用顺序，不能像普通单元测试一样百分之百保证模型行为。同步 `sleep(3)` 的目的是确保两个后台任务在 Agent Loop 下一轮开始前已经完成，使状态流转更稳定。

---

## 十四、完整程序流转

### 阶段 0：模块初始化完成

后台状态：

```python
BACKGROUND.tasks = {}
BACKGROUND.results = {}
BACKGROUND._ready = []
BACKGROUND._counter = 0
```

进程状态：

```python
_shell_processes = set()
```

此时没有后台线程和 Shell 子进程。

### 阶段 1：接收用户输入

主程序执行：

```python
trigger_hooks("UserPromptSubmit", query)
history.append({"role": "user", "content": query})
agent_loop(history)
```

`context_inject_hook()` 打印当前工作区，但不修改 prompt。

### 阶段 2：第一次进入 Agent Loop

首先调用：

```text
inject_background_results
  → collect_background_results
  → BACKGROUND.collect
```

由于没有任务，状态不变：

```text
_shell_processes = set()
tasks = {}
results = {}
_ready = []
```

然后第一次调用模型。按照示例约束，模型返回七个 `tool_use`：

```text
1. 后台 Bash A
2. 后台 Bash B
3. write_file
4. read_file
5. edit_file
6. glob
7. 同步 Bash
```

Agent Loop 按列表顺序逐个调用 `execute_tool()`。

### 阶段 3：启动后台任务 A

主线程调用链：

```text
execute_tool(block_A)
  → permission_hook
  → log_hook
  → should_run_background == True
  → start_background_task
  → BACKGROUND.start
```

管理器先登记任务：

```python
BACKGROUND.tasks = {
    "bg_0001": {
        "tool_use_id": "tool_A",
        "command": "python ... A ...",
        "status": "running",
    }
}
BACKGROUND.results = {}
BACKGROUND._ready = []
BACKGROUND._counter = 1
```

随后启动后台线程 A。线程 A 进入 `_run()`，再调用 `_run_bash_process()` 创建 `process_A`。

创建并登记进程后：

```python
_shell_processes = {process_A}
```

此时的结构：

```text
主线程：继续处理工具调用
后台线程 A：阻塞在 process_A.communicate(timeout=120)
process_A：执行任务 A，预计运行 2 秒
```

主线程立即收到占位结果：

```text
[Background task bg_0001 started]
```

### 阶段 4：启动后台任务 B

同样经过权限检查和后台判断，管理器生成 `bg_0002`：

```python
BACKGROUND.tasks = {
    "bg_0001": {"status": "running", ...},
    "bg_0002": {"status": "running", ...},
}
BACKGROUND.results = {}
BACKGROUND._ready = []
BACKGROUND._counter = 2
```

后台线程 B 创建 `process_B` 后：

```python
_shell_processes = {process_A, process_B}
```

此时：

```text
主线程：继续执行文件工具
后台线程 A：等待 process_A
后台线程 B：等待 process_B
```

主线程立即收到第二个占位结果：

```text
[Background task bg_0002 started]
```

### 阶段 5：主线程执行文件工具

`write_file`、`read_file`、`edit_file` 和 `glob` 都会经过：

```text
execute_tool
  → PreToolUse
  → should_run_background == False
  → call_tool
  → 对应 handler
  → PostToolUse
```

文件内容变化：

```text
write_file 后：hello background
read_file 返回：hello background
edit_file 后：goodbye background
glob 返回：tmp/s11-flow-demo.txt
```

这些操作很快，执行期间后台任务 A、B 继续运行。

### 阶段 6：主线程执行同步 Bash

同步 Bash 显式设置：

```python
run_in_background = False
```

因此调用链为：

```text
execute_tool
  → should_run_background == False
  → call_tool
  → run_bash
  → _run_bash_process
```

主线程创建 `process_F` 并阻塞等待三秒。

某个短暂时刻，进程集合为：

```python
_shell_processes = {
    process_A,
    process_B,
    process_F,
}
```

但 `BackgroundManager` 只管理 A、B：

```python
BACKGROUND.tasks = {
    "bg_0001": {"status": "running", ...},
    "bg_0002": {"status": "running", ...},
}
```

同步进程 `process_F` 不属于后台任务，所以不会出现在 `BACKGROUND.tasks` 中。这正是两套状态不可混为一谈的例子。

### 阶段 7：约 1 秒时，后台任务 B 失败

任务 B 执行 `sys.exit(7)`。

后台线程 B 得到：

```python
exit_code = 7
result = (
    "Error: command exited with status 7\n"
    "B:start\n"
    "B:failed"
)
status = "failed"
```

`_run_bash_process()` 的 `finally` 先移除进程：

```python
_shell_processes = {process_A, process_F}
```

随后 `_run()` 提交后台状态：

```python
BACKGROUND.tasks = {
    "bg_0001": {"status": "running", ...},
    "bg_0002": {"status": "failed", ...},
}

BACKGROUND.results = {
    "bg_0002": "Error: command exited with status 7\n...",
}

BACKGROUND._ready = ["bg_0002"]
```

关键观察：

```text
process_B 已不存在于 _shell_processes
bg_0002 仍存在于 BackgroundManager
```

这是因为进程生命周期已经结束，但任务结果还没有被 Agent Loop 消费。

### 阶段 8：约 2 秒时，后台任务 A 成功

后台线程 A 得到：

```python
exit_code = 0
result = "A:start\nA:done"
status = "completed"
```

进程状态先变为：

```python
_shell_processes = {process_F}
```

然后后台状态变为：

```python
BACKGROUND.tasks = {
    "bg_0001": {"status": "completed", ...},
    "bg_0002": {"status": "failed", ...},
}

BACKGROUND.results = {
    "bg_0002": "Error: command exited with status 7\n...",
    "bg_0001": "A:start\nA:done",
}

BACKGROUND._ready = ["bg_0002", "bg_0001"]
```

B 虽然后启动，却先完成，所以在 `_ready` 中排在 A 前面。

### 阶段 9：约 3 秒时，同步 Bash 完成

主线程得到：

```text
foreground wait done
```

同步进程从集合删除：

```python
_shell_processes = set()
```

后台管理器仍保留结果：

```python
tasks = {
    "bg_0001": completed,
    "bg_0002": failed,
}
results = {
    "bg_0001": ...,
    "bg_0002": ...,
}
_ready = ["bg_0002", "bg_0001"]
```

此时已经没有运行中的 Shell 进程，但仍有两个尚未通知模型的后台任务结果。

### 阶段 10：第一批 `tool_result` 加入消息历史

七个工具调用全部返回后，Agent Loop 追加一条用户消息：

```python
messages.append({
    "role": "user",
    "content": [
        bg_0001_started_tool_result,
        bg_0002_started_tool_result,
        write_result,
        read_result,
        edit_result,
        glob_result,
        foreground_bash_result,
    ],
})
```

前两个 `tool_result` 只是任务已启动的占位结果，不包含最终输出。

### 阶段 11：第二次 Agent Loop 收集后台结果

循环回到顶部：

```python
inject_background_results(messages)
```

`collect()` 按 `_ready` 顺序消费任务：

```text
先收集 bg_0002（failed）
再收集 bg_0001（completed）
```

生成两个通知后，管理器变为空：

```python
BACKGROUND.tasks = {}
BACKGROUND.results = {}
BACKGROUND._ready = []
BACKGROUND._counter = 2
```

注意 `_counter` 不会清零。

通知被追加到最后一条用户消息中：

```xml
<task_notification>
  <task_id>bg_0002</task_id>
  <status>failed</status>
  ...
</task_notification>

<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  ...
</task_notification>
```

随后模型第二次被调用。它同时看到：

- 七个原始 `tool_result`；
- 两个后台完成通知。

### 阶段 12：模型给出最终回答

模型不再调用工具，而是总结：

```text
bg_0001 completed
bg_0002 failed
同步命令输出 foreground wait done
文件最终内容 goodbye background
```

因为响应中没有 `tool_use`，Agent Loop 触发 `Stop` Hook。

本例共有七个原始 `tool_result`，所以 `summary_hook()` 会统计出七次工具调用。两个 `<task_notification>` 是普通文本，不算新的工具调用。

然后 `agent_loop()` 返回，主程序打印模型文本并等待下一次用户输入。

### 阶段 13：用户退出

用户输入 `q` 后主循环结束，`atexit` 调用：

```python
_stop_all_shell_processes()
```

本例此时：

```python
_shell_processes = set()
```

因此没有实际需要终止的进程。

如果用户在任务运行期间退出，清理函数会复制当前进程集合，再逐个调用 `_stop_process_group()`。

---

## 十五、完整状态迁移总表

| 时刻 | `_shell_processes` | `tasks` | `results` | `_ready` |
| --- | --- | --- | --- | --- |
| 模块初始化后 | 空 | 空 | 空 | 空 |
| A 已启动 | A | A=`running` | 空 | 空 |
| B 已启动 | A、B | A/B=`running` | 空 | 空 |
| 同步 Bash 已启动 | A、B、F | A/B=`running` | 空 | 空 |
| B 失败 | A、F | A=`running`、B=`failed` | B 的错误 | B |
| A 成功 | F | A=`completed`、B=`failed` | A/B 结果 | B、A |
| 同步 Bash 完成 | 空 | A=`completed`、B=`failed` | A/B 结果 | B、A |
| `collect()` 完成 | 空 | 空 | 空 | 空 |
| 程序退出 | 空 | 空 | 空 | 空 |

最值得记住的两次“不同步”是：

```text
1. Shell 进程已经结束
   但 BackgroundManager 仍保存任务结果，等待模型消费。

2. 后台任务已经完成
   但模型还不知道，直到 Agent Loop 下一次调用 collect()。
```

---

## 十六、并发安全与两把锁

### 1. 为什么 `_shell_processes` 需要锁

下面这些动作可能同时发生：

```text
后台线程 A 添加或删除 process_A
后台线程 B 添加或删除 process_B
主线程添加或删除同步 process_F
退出清理读取全部 process
```

所以集合访问必须放在 `_shell_process_lock` 中。

### 2. 为什么 `BackgroundManager` 需要自己的锁

主线程可能执行：

```text
start()：添加任务
collect()：删除已完成任务
```

后台线程可能同时执行：

```text
_run()：修改 status、写入结果、追加 _ready
```

没有锁时可能出现：

- `collect()` 正在删除任务，后台线程同时写结果；
- 两个线程同时修改 `_ready`；
- 多个任务同时读取和增加 `_counter`，生成重复 ID。

### 3. 为什么不使用同一把锁

两组状态属于不同抽象层：

```text
_shell_processes：进程是否还需要清理
BackgroundManager：后台结果是否已经完成、是否已通知模型
```

分开加锁能缩小临界区，也让职责更清晰。

---

## 十七、重要边界与局限

### 1. 后台完成不会主动唤醒 Agent

如果模型在后台任务完成前就输出最终答案，`agent_loop()` 会结束。后台结果只能等用户下一次输入时被收集。

### 2. 收集是一次性消费

`collect()` 会从 `tasks` 和 `results` 删除任务。当前实现没有后台任务历史记录。

### 3. 最终通知只保留 500 字符

命令执行层最多保留 50,000 字符，但任务通知只使用 `result[:500]`。收集后完整结果又被删除，所以长输出会永久丢失。

### 4. `tool_use_id` 被保存但没有在收集时使用

这是有意避免返回第二个 `tool_result`，但如果仅从当前功能看，这个字段属于预留信息。

### 5. daemon 线程不会保证任务完成

主程序退出时 daemon 线程不会阻止解释器关闭。真正负责避免遗留 Shell 进程的是 `_shell_processes` 和退出清理函数。

### 6. 进程组清理不是完整沙箱

`start_new_session=True` 和 `killpg()` 可以清理原进程组中的进程，但自行创建新 session 的后代进程仍可能脱离该进程组。

### 7. 平台兼容性

`os.killpg()` 是 Unix 风格接口。这份代码更适合 Linux、macOS 或 WSL 环境；直接使用 Windows Python 时，进程组清理部分可能不兼容。

### 8. 通知内容没有 XML 转义

命令或输出中如果包含 `<`、`>`、`&` 等内容，生成的 `<task_notification>` 可能不是严格有效的 XML。这里把 XML 风格标签当作模型可读的文本协议，而不是严谨 XML 文档。

---

## 十八、正常主干函数覆盖关系

示例 prompt 会覆盖以下主要函数：

```text
用户输入
  → trigger_hooks(UserPromptSubmit)
  → context_inject_hook
  → agent_loop
    → inject_background_results
      → collect_background_results
        → BackgroundManager.collect
    → client.messages.create
    → execute_tool
      → trigger_hooks(PreToolUse)
        → permission_hook
        → log_hook
      → should_run_background
        ├─ 后台：start_background_task
        │          → BackgroundManager.start
        │          → BackgroundManager._run（后台线程）
        │             → _run_bash_process
        │             → _format_bash_result
        └─ 同步：call_tool
                   ├─ run_bash → _run_bash_process
                   ├─ run_read
                   ├─ run_write
                   ├─ run_edit
                   └─ run_glob
      → trigger_hooks(PostToolUse)
        → large_output_hook
    → trigger_hooks(Stop)
      → summary_hook

程序退出
  → _stop_all_shell_processes
  → _stop_process_group（仅当仍有登记的进程）
```

以下函数或分支需要额外外部条件，单靠正常 prompt 不一定触发：

- `_handle_termination_signal()`：需要外部发送 `SIGTERM`；
- Bash 120 秒超时分支；
- `Popen()` 的 `OSError` 分支；
- `thread.start()` 失败和任务登记回滚；
- 权限拒绝或人工确认分支；
- 文件工具异常分支。

---

## 十九、最终总结

本章的完整生命周期可以压缩为：

```text
模型明确请求后台 Bash
  → 主线程在 tasks 中登记 running
  → 创建 daemon 后台线程
  → 后台线程创建 Shell 进程
  → Shell 进程加入 _shell_processes
  → 主线程立即把 bg_id 作为占位 tool_result 返回模型
  → 后台线程等待进程完成
  → finally 清理进程组并从 _shell_processes 删除 Popen
  → 后台线程把任务标为 completed/failed
  → 写入 results，并把 ID 追加到 _ready
  → Agent Loop 下一轮调用 collect()
  → tasks/results/_ready 中的数据被一次性消费
  → 生成 <task_notification> 并加入对话
  → 模型看到最终结果并继续决策
```

最核心的设计思想是：

> `_shell_processes` 管“进程是否还活着、退出时是否需要清理”；`BackgroundManager` 管“任务处于什么逻辑状态、结果是否已被模型消费”。两者生命周期相关，但并不相同。
