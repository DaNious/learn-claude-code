# s12 Cron Scheduler 学习笔记

# 第一部分：代码说明

## 1. 程序要解决的问题

普通 Agent Loop 只能处理当前收到的用户消息。s12 在它外面增加了一个进程内 Cron 调度器，用来记录“什么时间，把什么 prompt 交给 Agent”。

它并不是提前保存一条 shell 命令并到点直接运行，而是把到期任务转换为一条新的用户消息：

```text
Cron 表达式 + prompt
        ↓ 时间匹配
cron_queue
        ↓ Agent 空闲
[Scheduled] prompt
        ↓
Agent Loop
        ↓
模型自行判断需要调用哪些工具
```

因此定时任务仍然拥有普通 Agent 回合的能力，可以读取文件、修改代码或执行命令。

## 2. 初始化与全局配置

程序首先加载 `.env` 并创建 Anthropic 客户端：

```python
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
```

这里有三个需要注意的点：

1. `WORKDIR` 是启动程序时的当前目录，不一定是 `code.py` 所在目录。
2. 持久化任务保存在 `WORKDIR/.scheduled_tasks.json`。
3. `MODEL_ID` 必须存在，否则程序会在初始化阶段报错。

系统提示中专门告诉模型：

```python
"Use schedule_cron for work that should start at a future local time."
```

这使模型知道未来执行的工作应通过 `schedule_cron` 注册。

## 3. 基础工具和 Hook

前半部分保留了前面章节实现的五个基础工具：

```text
bash        执行 shell 命令
read_file   读取文件
write_file  写入文件
edit_file   替换文件中的指定文本
glob        按模式查找文件
```

`TOOLS` 是提供给模型的工具描述，`TOOL_HANDLERS` 是工具名到 Python 函数的映射：

```python
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}
```

工具统一经过 `execute_tool()`：

```text
模型返回 tool_use
  ↓
触发 PreToolUse Hook
  ↓
从 TOOL_HANDLERS 找到实现
  ↓
执行工具
  ↓
触发 PostToolUse Hook
  ↓
把 tool_result 返回模型
```

Hook 不是本章重点，但有一条规则直接影响定时任务：

```python
if threading.current_thread() is not threading.main_thread():
    return "Permission denied: scheduled turns cannot request interactive approval"
```

定时 Agent 回合运行在后台队列线程中。为了避免后台线程和主线程同时调用 `input()`，后台定时任务不能请求交互式权限确认。

## 4. 定时任务的数据模型

```python
@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None
```

字段含义如下：

| 字段 | 含义 |
|---|---|
| `id` | 随机生成的任务 ID，例如 `cron_a1b2c3d4` |
| `cron` | 五段 Cron 表达式 |
| `prompt` | 到期后交给 Agent 的任务说明 |
| `recurring` | 是否周期执行；`False` 表示一次性任务 |
| `durable` | 是否写入 `.scheduled_tasks.json` |
| `pending_delivery` | 已到期，但尚未被模型成功接收 |
| `last_fired` | 最近一次入队的分钟，用于防止重复入队 |

其中最容易混淆的是：

```text
recurring 决定成功交付后是否继续保留
durable   决定进程重启后是否能够恢复
```

两者互相独立，所以可以组合为：

| 类型 | `recurring` | `durable` | 行为 |
|---|---:|---:|---|
| 周期持久化任务 | `True` | `True` | 周期执行，重启后恢复 |
| 周期会话任务 | `True` | `False` | 周期执行，退出进程后消失 |
| 单次持久化任务 | `False` | `True` | 成功交付前可以跨重启恢复 |
| 单次会话任务 | `False` | `False` | 只在当前进程中等待一次交付 |

## 5. 三个核心容器与两把锁

```python
scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.RLock()
```

加上后面定义的 `session_history`，程序有三个关键容器：

| 容器 | 职责 |
|---|---|
| `scheduled_jobs` | 保存所有仍然有效的任务定义 |
| `cron_queue` | 保存已经到期、正在等待交付的任务 |
| `session_history` | 保存普通用户消息、定时消息、模型响应和工具结果 |

两个锁分别解决不同问题：

```text
cron_lock
  └─ 保护 scheduled_jobs、cron_queue 和相关持久化操作

agent_lock
  └─ 防止普通用户回合与定时回合同时修改 session_history
```

`cron_lock` 使用 `RLock`，是因为某些已经持锁的任务管理函数还会调用同样需要该锁的 `save_durable_jobs()`。

## 6. Cron 表达式匹配

本章使用标准五段格式：

```text
分钟  小时  日期  月份  星期
```

支持的字段形式包括：

```text
*        任意值
*/5      每 5 个单位
3        精确匹配 3
1-5      匹配 1 到 5
1,3,5    匹配多个离散值
```

### 6.1 单字段匹配

`_cron_field_matches()` 按以下顺序处理字段：

```text
是否为 *
  ↓
是否为 */N
  ↓
是否为逗号列表
  ↓
是否为范围
  ↓
按精确整数匹配
```

列表匹配会递归调用 `_cron_field_matches()`，所以列表中的每一项也可以继续按普通字段处理。

### 6.2 完整表达式匹配

`cron_matches()` 先检查分钟、小时和月份。任意一个不匹配就直接返回 `False`。

Python 的 `datetime.weekday()` 以周一为 `0`，这里转换成 Cron 使用的周日为 `0`：

```python
cron_weekday = (moment.weekday() + 1) % 7
```

日期和星期的组合规则是：

```text
日期=*，星期=*     → 匹配
日期=*             → 只检查星期
星期=*             → 只检查日期
两者都不是 *       → 日期或星期任意一个匹配即可
```

### 6.3 创建前验证

`validate_cron()` 确保表达式正好有五个字段，并按下面的范围检查：

```text
minute       0-59
hour         0-23
day-of-month 1-31
month        1-12
day-of-week  0-6
```

验证函数只负责判断表达式是否合法，真正判断某个时间是否到期的是 `cron_matches()`。

## 7. 持久化机制

`save_durable_jobs()` 只保存：

```python
if job.durable
```

的任务。保存时不会直接覆盖正式文件，而是：

```text
把任务转成 JSON
  ↓
写入带进程和线程 ID 的临时文件
  ↓
os.replace() 原子替换 .scheduled_tasks.json
  ↓
清理残留临时文件
```

`load_durable_jobs()` 在程序启动时读取该文件，并重新验证每个任务。损坏的数据会输出错误或被跳过，不会静默载入。

如果恢复出的任务具有：

```python
pending_delivery=True
```

说明它上次已经到期但还没完成交付确认，因此除了进入 `scheduled_jobs`，还会重新进入 `cron_queue`。

## 8. 创建、列出和取消任务

### 8.1 创建任务

模型调用的工具入口是：

```python
run_schedule_cron()
```

实际创建由 `schedule_job()` 完成：

```text
验证 Cron
  ↓
验证 prompt
  ↓
生成唯一 ID
  ↓
构造 CronJob
  ↓
放入 scheduled_jobs
  ↓
durable=True 时持久化
```

如果持久化失败，刚放入 `scheduled_jobs` 的任务会被删除，避免工具报告失败后任务仍在内存中生效。

### 8.2 列出任务

`run_list_crons()` 读取 `scheduled_jobs`，显示：

```text
任务 ID
Cron 表达式
prompt 摘要
recurring / one-shot
durable / session
```

它列出的是仍然有效的任务，而不是只列出队列中已经到期的任务。

### 8.3 取消任务

`cancel_job()` 会同时清理：

```text
scheduled_jobs 中的任务定义
cron_queue 中相同 ID 的待交付项
持久化文件中的 durable 任务
```

取消前会备份队列。如果持久化失败，任务表和队列都会恢复原状，防止只取消了一半。

## 9. 到期检测与入队

调度线程每秒执行：

```python
poll_due_jobs(datetime.now())
```

它为当前时间生成分钟标记：

```python
minute_marker = moment.strftime("%Y-%m-%d %H:%M")
```

每个任务按照下面的顺序判断：

```text
pending_delivery=True？
  └─ 是：任务已经等待交付，跳过

last_fired 等于当前分钟？
  └─ 是：本分钟已经触发过，跳过

cron_matches()？
  └─ 否：尚未到期，跳过
  └─ 是：调用 _enqueue_due_job()
```

`last_fired` 很重要，因为调度线程每秒运行一次，而 Cron 的最小粒度是一分钟。没有它，同一任务会在一分钟内重复入队约 60 次。

`_enqueue_due_job()` 的顺序是：

```text
pending_delivery=True
  ↓
记录 last_fired
  ↓
先保存持久化状态
  ↓
再 append 到 cron_queue
```

只有磁盘状态保存成功，任务才会暴露给队列处理线程。如果写盘失败，函数会恢复旧的 `pending_delivery` 和 `last_fired`。

## 10. 等待 Agent 空闲并交付

队列线程每 0.2 秒检查：

```python
if not has_cron_queue() or not agent_lock.acquire(blocking=False):
    continue
```

如果用户正在进行普通 Agent 回合，主线程持有 `agent_lock`，队列线程就暂不处理。它不会打断用户，也不会阻塞等待，而是在下一轮继续尝试。

取得锁后，队列线程调用：

```python
run_agent_turn_locked()
```

这里没有 `user_query`，定时消息由 `agent_loop()` 从 `cron_queue` 中获取：

```python
fired = consume_cron_queue()
```

每个到期任务被转换成：

```python
{"role": "user", "content": f"[Scheduled] {job.prompt}"}
```

之后它就和普通用户请求一样进入模型—工具循环。

## 11. 成功确认和失败恢复

从队列取出的任务先保存在：

```python
waiting_for_ack = list(fired)
```

第一次模型 API 调用成功返回后，程序调用：

```python
acknowledge_cron_jobs(waiting_for_ack)
```

不同任务的确认方式如下：

```text
周期任务 recurring=True
  → pending_delivery=False
  → 继续保留，等待下一次 Cron 匹配

一次性任务 recurring=False
  → 从 scheduled_jobs 删除
  → 不再触发
```

这里确认的是“模型已经收到 prompt”，不是“prompt 要求的全部工具操作已经成功完成”。

如果第一次模型调用失败，程序会：

```text
删除刚追加的 [Scheduled] 消息
  ↓
restore_cron_jobs()
  ↓
重新设置 pending_delivery=True
  ↓
重新放回 cron_queue
```

如果确认状态写盘失败，`acknowledge_cron_jobs()` 也会恢复被删除或修改的任务并重新排队。

因此这个实现提供的是“至少一次交付”：尽量不丢任务，但极端情况下可能重复执行。

## 12. 运行边界

1. 调度器使用 Agent 进程所在机器的本地时间。
2. 后台线程只在 CLI 程序运行时存在；导入 `code.py` 不会自动启动线程。
3. `durable=True` 只保证任务定义和待交付状态能够恢复，不会让关闭的程序自动运行。
4. 程序不会补跑停机期间错过的普通触发时间。
5. 后台定时回合不能进行交互式权限确认。
6. 交付语义是 at-least-once，而不是 exactly-once。
7. 需要真正脱离 Agent 进程运行时，应使用 crontab、systemd timer 或外部调度服务。

---

# 第二部分：使用一条 Prompt 模拟完整程序流转

可以用一条“自清理”的综合 prompt，让定时任务经历：

```text
创建 → 持久化 → 列出 → 取消 → 到期 → 入队
→ 等待 Agent 空闲 → 交付 → 确认 → 周期任务自我取消
```

不过要先说明：`start_runtime_threads()`、`load_durable_jobs()` 发生在用户输入 prompt 之前，`stop_runtime_threads()` 发生在输入 `q` 之后；`restore_cron_jobs()` 只在 API 调用失败时触发。因此一条正常 prompt 能覆盖所有正常路径，但不能自然覆盖启动、退出和故障恢复路径。

## 推荐测试 Prompt

程序启动后一次性输入：

```text
请严格按下面顺序完成一次 Cron 调度器端到端测试：

1. 创建任务 A：
   - cron: */1 * * * *
   - recurring: true
   - durable: true
   - prompt:
     DEMO_A_RECURRING_SELF_CANCEL：这是一个周期任务。触发后先调用
     list_crons，找到 prompt 以 DEMO_A_RECURRING_SELF_CANCEL 开头的任务，
     使用 cancel_cron 取消它自己，然后说明任务已经执行并自我取消。

2. 创建任务 B：
   - cron: */1 * * * *
   - recurring: false
   - durable: true
   - prompt:
     DEMO_B_ONE_SHOT：这是一个一次性任务。触发后回复任务已经执行，
     并调用 list_crons 查看当前剩余任务。

3. 创建任务 C：
   - cron: 0 0 1 1 *
   - recurring: true
   - durable: true
   - prompt: DEMO_C_CANCEL_BEFORE_FIRE

4. 创建任务 D：
   - cron: 0 0 1 1 *
   - recurring: true
   - durable: false
   - prompt: DEMO_D_SESSION_CANCEL_BEFORE_FIRE

5. 四个任务全部创建后，调用 list_crons，确认它们都存在。

6. 根据创建任务时返回的任务 ID，立即取消任务 C 和任务 D。

7. 再调用一次 list_crons，确认现在只剩任务 A 和任务 B。

8. 不要使用 bash 等待，不要重复创建任务。完成上述操作后直接结束当前回合，
   让后台 Cron 调度线程处理 A 和 B。
```

`*/1 * * * *` 每分钟都匹配。因为任务刚创建时 `last_fired=None`，所以调度线程下一次轮询时通常就会把 A、B 放入队列，不一定需要等到下一分钟。

---

# 完整流转模拟

## 阶段一：Prompt 输入之前

程序从入口开始：

```python
if __name__ == "__main__":
    start_runtime_threads()
```

见 `code.py:754`。

### 1. 加载持久化任务

```python
load_durable_jobs()
```

它读取：

```text
.scheduled_tasks.json
```

对于每条任务：

```text
JSON 字典
  → CronJob(**item)
  → validate_cron()
  → 放入 scheduled_jobs
```

如果保存的任务带有：

```python
pending_delivery=True
```

说明上次已经到期，但还没确认交付，于是还会放入：

```python
cron_queue
```

### 2. 启动两个后台线程

```text
cron-scheduler
    每秒调用 poll_due_jobs()

cron-queue-processor
    每 0.2 秒检查 cron_queue
```

此时主线程进入 `input()`，等待上面的综合 prompt。

---

# 阶段二：主线程收到 Prompt

主线程先获得：

```python
agent_lock
```

然后调用：

```python
run_agent_turn_locked(query)
```

见 `code.py:766`。

调用链：

```text
run_agent_turn_locked(prompt)
  → prompt 加入 session_history
  → agent_loop(session_history)
  → client.messages.create(...)
```

此时 `cron_queue` 一般还是空的，所以：

```python
fired = consume_cron_queue()
```

返回空列表。

模型看到综合 prompt 后，开始调用 `schedule_cron`、`list_crons` 和 `cancel_cron`。

---

# 阶段三：创建任务 A

模型调用：

```python
schedule_cron(
    cron="*/1 * * * *",
    prompt="DEMO_A_RECURRING_SELF_CANCEL：...",
    recurring=True,
    durable=True,
)
```

完整调用链：

```text
execute_tool()
  → PreToolUse Hook
  → run_schedule_cron()
  → schedule_job()
  → validate_cron()
  → _validate_cron_field()
  → new_cron_id()
  → CronJob(...)
  → scheduled_jobs[job.id] = job
  → save_durable_jobs()
```

任务 A 创建后类似：

```python
CronJob(
    id="cron_a1b2c3d4",
    cron="*/1 * * * *",
    prompt="DEMO_A_RECURRING_SELF_CANCEL：...",
    recurring=True,
    durable=True,
    pending_delivery=False,
    last_fired=None,
)
```

因为 `durable=True`，任务会进入 `.scheduled_tasks.json`。

---

# 阶段四：创建任务 B、C、D

任务 B：

```text
recurring=False
durable=True
```

它是持久化的一次性任务。成功交付后，会从 `scheduled_jobs` 中永久删除。

任务 C：

```text
recurring=True
durable=True
cron="0 0 1 1 *"
```

它用于测试“持久化任务在触发前被取消”。

任务 D：

```text
recurring=True
durable=False
cron="0 0 1 1 *"
```

它用于测试“仅存在于当前进程内存中的任务”。

此时内存状态大致为：

```text
scheduled_jobs
├─ A：周期、持久化
├─ B：单次、持久化
├─ C：周期、持久化
└─ D：周期、会话级
```

磁盘文件只包含：

```text
A、B、C
```

D 因为 `durable=False`，不会写入文件。

---

# 阶段五：第一次列出任务

模型调用：

```python
list_crons()
```

调用链：

```text
execute_tool()
  → run_list_crons()
  → 复制 scheduled_jobs.values()
  → 格式化任务信息
```

输出类似：

```text
cron_a1b2c3d4: */1 * * * * -> DEMO_A... [recurring, durable]
cron_b1b2c3d4: */1 * * * * -> DEMO_B... [one-shot, durable]
cron_c1b2c3d4: 0 0 1 1 * -> DEMO_C... [recurring, durable]
cron_d1b2c3d4: 0 0 1 1 * -> DEMO_D... [recurring, session]
```

---

# 阶段六：取消 C 和 D

取消 C：

```text
run_cancel_cron()
  → cancel_job(C.id)
  → 从 scheduled_jobs 删除 C
  → 从 cron_queue 删除 C
  → save_durable_jobs()
```

因为 C 是持久化任务，所以磁盘文件也会更新。

取消 D：

```text
run_cancel_cron()
  → cancel_job(D.id)
  → 从 scheduled_jobs 删除 D
```

因为 D 不持久化，所以不需要写文件。

此时：

```text
scheduled_jobs = {A, B}
cron_queue     = 可能已经包含 A、B
JSON 文件      = {A, B}
```

为什么说 A、B“可能已经入队”？因为调度线程和主线程同时运行。创建 A、B 后，后台线程可能已经完成了一次时间检查。

---

# 阶段七：调度线程发现 A、B 到期

调度线程每秒执行：

```python
poll_due_jobs(datetime.now())
```

以 A 为例：

```text
poll_due_jobs()
  → pending_delivery 当前为 False
  → last_fired 不是当前分钟
  → cron_matches("*/1 * * * *", now)
  → _cron_field_matches()
  → 匹配成功
  → _enqueue_due_job()
```

`_enqueue_due_job()` 先修改任务：

```python
job.pending_delivery = True
job.last_fired = "2026-08-18 14:35"
```

然后：

```text
先 save_durable_jobs()
再 cron_queue.append(job)
```

顺序非常重要：

```text
持久化“待交付”状态
        ↓
加入内存队列
```

如果保存失败，代码会恢复原来的：

```python
pending_delivery
last_fired
```

并且不会把任务放进队列。

调度完成后的状态：

```text
scheduled_jobs
├─ A：pending_delivery=True
└─ B：pending_delivery=True

cron_queue
├─ A
└─ B
```

---

# 阶段八：为什么定时任务不会马上打断当前 Prompt

队列线程发现 `cron_queue` 非空后执行：

```python
agent_lock.acquire(blocking=False)
```

但是主线程当前仍在处理初始综合 prompt，并持有 `agent_lock`。

所以队列线程拿不到锁：

```text
cron_queue 有任务
       +
agent_lock 被主线程占用
       ↓
暂不处理，0.2 秒后再检查
```

这就是“Agent 空闲后再交付”的实现。

当初始回合完成后，主线程释放 `agent_lock`。

---

# 阶段九：队列线程交付 A、B

队列线程获得 `agent_lock`，调用：

```python
run_agent_turn_locked()
```

这里没有传 `user_query`，因此不会添加普通用户消息。

随后 `agent_loop()` 取出整个队列：

```python
fired = consume_cron_queue()
```

`consume_cron_queue()`：

```text
复制 cron_queue
清空 cron_queue
返回 [A, B]
```

然后追加两条对话消息：

```python
{"role": "user", "content": "[Scheduled] DEMO_A_RECURRING_SELF_CANCEL：..."}

{"role": "user", "content": "[Scheduled] DEMO_B_ONE_SHOT：..."}
```

现在它们正式成为 Agent 对话的一部分。

---

# 阶段十：模型成功接收后确认任务

只要这次：

```python
client.messages.create(...)
```

成功返回，就会调用：

```python
acknowledge_cron_jobs([A, B])
```

注意，确认发生在模型返回之后、模型请求的工具真正执行之前。

## A 是周期任务

```python
current.pending_delivery = False
```

A 暂时继续留在 `scheduled_jobs`，理论上可以在下一分钟再次触发。

## B 是一次性任务

```python
scheduled_jobs.pop(current.id)
```

B 被直接删除。

确认后的状态：

```text
scheduled_jobs = {A}
cron_queue     = {}
JSON 文件      = {A}
```

所以 B 后面调用 `list_crons` 时看不到自己，这是正常的：它已经被确认为成功交付并删除。

---

# 阶段十一：A 自我取消

A 的定时 prompt 要求：

```text
调用 list_crons
找到 prompt 以 DEMO_A_RECURRING_SELF_CANCEL 开头的任务
取消自己
```

模型首先调用：

```python
list_crons()
```

此时只能看到 A。

然后调用：

```python
cancel_cron(A.id)
```

调用链：

```text
run_cancel_cron()
  → cancel_job()
  → scheduled_jobs.pop(A.id)
  → 清理 cron_queue 中可能存在的 A
  → save_durable_jobs()
```

最终状态：

```text
scheduled_jobs = {}
cron_queue     = {}
.scheduled_tasks.json = []
```

四个测试任务全部被清理：

- A：触发后自我取消
- B：一次性交付后自动删除
- C：持久化任务在触发前取消
- D：会话任务在触发前取消

---

# API 失败时的故障分支

正常 prompt 不会主动触发 `restore_cron_jobs()`。它只在首次交付模型失败时执行：

```python
except Exception:
    del messages[scheduled_start:]
    restore_cron_jobs(waiting_for_ack)
```

假设交付 A、B 时网络失败：

```text
A、B 已从 cron_queue 取出
        ↓
已追加为 [Scheduled] 消息
        ↓
模型 API 调用失败
        ↓
删除这两条 Scheduled 消息
        ↓
restore_cron_jobs([A, B])
        ↓
重新放回 cron_queue
```

这体现了它的核心交付语义：

> 宁可重复，也尽量不丢失，即“至少一次交付”。

如果进程恰好在“模型收到任务”和“确认状态写入文件”之间崩溃，重启后任务可能再次交付。

---

# 最后退出程序

任务完成后，在主终端输入：

```text
q
```

主循环退出并进入：

```python
finally:
    stop_runtime_threads()
```

它设置停止事件，等待两个后台线程结束，然后清空线程列表。

完整测试最终覆盖了定时任务的主要正常路径：

```text
start_runtime_threads
→ load_durable_jobs
→ schedule_job / validate_cron / new_cron_id
→ save_durable_jobs
→ list_crons
→ cancel_job
→ cron_matches
→ poll_due_jobs
→ _enqueue_due_job
→ has_cron_queue
→ consume_cron_queue
→ acknowledge_cron_jobs
→ 周期任务自我取消
→ stop_runtime_threads
```

`restore_cron_jobs()` 则需要额外模拟一次模型 API 失败才能进入。
