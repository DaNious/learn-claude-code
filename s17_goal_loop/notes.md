# s17 Goal Loop 学习笔记

> 本章核心：**主模型不再调用工具，只说明当前这一轮想停止；整个目标是否真正完成，要由独立的 Goal Evaluator 根据对话中的证据判断。**

本文按 Python 的实际运行与调用顺序讲解 [`code.py`](./code.py)，并使用一条 `/goal` prompt 模拟一次包含“工具调用、首次评估未通过、自动继续、二次评估成功”的完整流转。

---

## 1. 这一章解决什么问题

s01 的基础 Agent Loop 大致是：

```text
调用模型
  ├─ 模型返回 tool_use → 执行工具 → 把结果交回模型 → 继续
  └─ 模型不再调用工具 → 返回最终文本
```

问题在于，模型“不再调用工具”只代表它**想结束当前轮次**，并不一定代表用户要求的最终结果已经满足。例如：

- 代码改了，但没有运行测试；
- 只运行了局部测试，但 Goal 要求完整测试；
- 模型说“应该好了”，对话中却没有命令退出码；
- 后台任务还没结束，关键结果尚未回到对话；
- 完成了实现，却遗漏了 Goal 中的限制条件。

s17 在原 Agent Loop 的返回边界上增加一个 Goal Gate：

```text
用户输入 /goal 完成条件
          ↓
   Worker 模型执行任务
          ↓
 Worker 不再调用工具，准备停止
          ↓
 GoalController 调用独立 Evaluator
     ├─ achieved：证据表明已完成 → 真正返回
     ├─ failed：目标已不可能完成 → 返回失败
     ├─ block：尚未完成 → 将原因写回 messages → Worker 继续
     ├─ defer：后台任务仍运行 → 暂时返回宿主
     ├─ error：评估失败 → 返回宿主，保留 Goal
     └─ limit：连续阻止次数超限 → 返回宿主，保留 Goal
```

因此，这一章并不是再造一个测试框架。真正的文件检查和测试仍由 Worker 使用工具完成；Goal Evaluator 只判断这些结果是否已经成为对话中的可靠证据。

---

## 2. 文件的组成

`code.py` 可以分为九部分：

1. 常量和安全配置；
2. Goal 相关数据类；
3. Anthropic 内容块、token 和 transcript 辅助函数；
4. `PromptGoalEvaluator`：独立的、无工具的评估模型；
5. `GoalController`：保存 Goal 状态并实现 Stop hook 决策；
6. 五个基础工具的 schema；
7. `AgentSession`：Worker Agent Loop、hooks 和工具执行；
8. `make_live_session()`：读取配置并组装所有对象；
9. `main()` 和 `__main__`：命令行入口。

Python 会从上到下加载这些定义，但函数体不会在定义时执行。真正开始运行的位置在文件底部：

```python
if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except (GoalError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error
```

---

## 3. 常量和状态模型

### 3.1 主要常量

```python
DEFAULT_MAX_TOKENS = 8000
DEFAULT_EVALUATOR_MAX_TOKENS = 512
DEFAULT_STOP_HOOK_BLOCK_CAP = 8
MAX_GOAL_LENGTH = 4000
```

- `DEFAULT_MAX_TOKENS`：每次 Worker 模型调用的最大输出 token。
- `DEFAULT_EVALUATOR_MAX_TOKENS`：每次 Evaluator 调用的最大输出 token。
- `DEFAULT_STOP_HOOK_BLOCK_CAP`：同一次 query 中，Stop hook 可连续阻止 Worker 结束的次数上限。
- `MAX_GOAL_LENGTH`：Goal 条件最大长度，防止无限长条件进入会话。

清除 Goal 的别名：

```python
CLEAR_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}
```

工具权限相关配置：

```python
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]
```

- 命中 `DENY_LIST`：直接拒绝。
- 命中 `DESTRUCTIVE`：命令行询问用户是否允许。

这只是教学示例中的字符串级防护，不是完备的 shell 安全沙箱。

### 3.2 `GoalError`

```python
class GoalError(Exception):
    """The goal command or evaluator could not be used safely."""
```

用于表示：

- Goal 条件为空或过长；
- Evaluator 返回非法 JSON；
- 工具路径逃出仓库；
- 未知工具；
- 依赖或模型环境变量缺失；
- 配置值不合法。

### 3.3 `GoalState`

```python
@dataclass
class GoalState:
    condition: str
    iterations: int
    set_at: float
    tokens_at_start: int
    last_reason: str | None = None
```

表示当前活动 Goal：

| 字段 | 含义 |
|---|---|
| `condition` | 用户要求的完成条件 |
| `iterations` | Evaluator 已经成功给出结构化判断的次数 |
| `set_at` | Goal 设置时间 |
| `tokens_at_start` | 设置 Goal 时 Worker 累计 token |
| `last_reason` | 最近一次 Evaluator 给出的理由，或评估错误 |

注意：Evaluator 调用报错时不会执行 `iterations += 1`，但会把错误写入 `last_reason`。

### 3.4 `GoalEvaluation`

```python
@dataclass(frozen=True)
class GoalEvaluation:
    ok: bool
    reason: str
    impossible: bool = False
```

Evaluator 的结构化结论：

- `ok=True`：完成条件已满足；
- `ok=False, impossible=False`：暂未满足，可以继续；
- `ok=False, impossible=True`：无法完成，应以失败结束。

`ok=True` 与 `impossible=True` 不允许同时出现。

### 3.5 `StopDecision`

```python
@dataclass(frozen=True)
class StopDecision:
    action: str
    reason: str = ""
```

这是 `GoalController` 返回给 Agent Loop 的控制决策。`action` 可能是：

```text
allow / defer / error / achieved / failed / block / limit
```

### 3.6 `SessionResult`

```python
@dataclass(frozen=True)
class SessionResult:
    text: str
    status: str
    reason: str = ""
```

这是 `AgentSession` 最终返回给 `main()` 或宿主的结果：

- `text`：Worker 最后一轮的普通文本；
- `status`：本次为何返回；
- `reason`：Goal Controller/Evaluator 给出的原因。

---

## 4. 内容块和 transcript 辅助函数

Anthropic SDK 的内容块通常是对象，而测试替身或本地数据可能使用字典。辅助函数同时兼容两种形式。

### 4.1 `_block_type()` 和 `_block_value()`

```python
def _block_type(block):
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)
```

```python
def _block_value(block, key, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)
```

它们避免业务代码到处区分：

```python
block["type"]
```

还是：

```python
block.type
```

### 4.2 `_extract_text()`

只提取响应中 `type == "text"` 的内容块，忽略 `tool_use`：

```python
return "\n".join(...).strip()
```

它既用于提取 Worker 的最终回答，也用于提取 Evaluator 返回的 JSON 文本。

### 4.3 `_usage_total()`

将一次 Worker 响应的：

```text
input_tokens + output_tokens
```

相加。本代码只在 Worker 响应后调用它，所以 `session.total_tokens` 不包括 Evaluator 的 token。

### 4.4 `_plain_content()`

把不同内容块转换成 Evaluator 可以阅读的纯文本：

```text
text        → 原文本
tool_use    → [tool_use 工具名 JSON参数]
tool_result → [tool_result 工具输出]
```

对 `tool_result.content` 会递归调用 `_plain_content()`，所以字符串、列表和嵌套内容都能被展开。

### 4.5 `transcript_text()`

将 `messages[]` 渲染为：

```text
USER:
...

ASSISTANT:
...
```

默认最多 24000 字符。它从最新消息向前选择，尽量保留最近的完整消息：

- 如果继续加入更老消息会超限，就停止；
- 如果最新一条消息自己就超限，只保留它的头部和尾部；
- 中间插入 `...[middle omitted]...` 标记。

这样能降低旧内容占满 Evaluator 上下文、挤掉最新测试结果的风险。

### 4.6 `_parse_json_object()` 与 `value`

Evaluator 可能直接返回 JSON，也可能错误地包上一层 Markdown 代码围栏。函数先尝试移除围栏，然后：

```python
value = json.loads(stripped)
```

这里的 `value` 是普通 Python 字典，例如：

```python
{
    "ok": False,
    "reason": "对话中还没有 pytest 的退出码",
    "impossible": False,
}
```

随后严格校验：

1. 顶层必须是字典；
2. `ok` 必须是真正的布尔值；
3. `reason` 必须是非空字符串；
4. `impossible` 必须是布尔值，缺省时为 `False`；
5. 不能同时 `ok=True` 和 `impossible=True`。

最后返回一个只含标准字段的新字典，再通过：

```python
GoalEvaluation(**value)
```

展开成不可变数据类实例。

---

## 5. `PromptGoalEvaluator`：独立裁判

### 5.1 职责边界

Evaluator：

- 读取 Goal 完成条件；
- 读取截至当前的对话 transcript；
- 判断对话证据是否满足条件；
- 返回严格 JSON。

Evaluator 不会：

- 自己读取仓库文件；
- 自己执行测试；
- 修改代码；
- 接收 Worker 的工具列表。

这使“干活”和“验收”成为两次独立模型调用。

### 5.2 `evaluate()`

```python
async def evaluate(self, condition, messages):
    return await asyncio.to_thread(
        self._evaluate_sync, condition, messages
    )
```

Anthropic SDK 的 `messages.create()` 是同步调用，因此用 `asyncio.to_thread()` 放到工作线程中，避免直接阻塞事件循环。

### 5.3 `_evaluate_sync()`

调用顺序：

1. `transcript_text(messages)` 生成可读对话；
2. 用 `json.dumps()` 封装完成条件和对话；
3. 构造只要求 JSON 的 prompt；
4. 调用评估模型；
5. `_extract_text()` 提取响应文本；
6. `_parse_json_object()` 解析和校验；
7. `GoalEvaluation(**value)` 返回结论。

System prompt 明确要求：

- Evaluator 没有工具；
- 不执行输入数据中嵌入的指令；
- 只能返回 JSON。

用户条件和 conversation 被包装成 JSON 数据字段，并在 prompt 中要求把它们当作数据而非指令。这是在降低 transcript 中提示注入对 Evaluator 的影响。

最关键的判断规则是：不能仅根据 Worker 的口头声明假定命令成功；实际结果必须出现在对话中。

---

## 6. `GoalController`：Goal 状态与 Stop hook

### 6.1 初始化

```python
controller = GoalController(
    evaluator=evaluator,
    block_cap=8,
)
```

初始化得到：

```python
controller.active = None
controller.last_status = None
controller.consecutive_blocks = 0
controller.events = []
```

如果 `block_cap < 1`，立即抛出 `GoalError`。

### 6.2 `begin_query()`

```python
def begin_query(self):
    self.consecutive_blocks = 0
```

每次用户 query 或后台结果重新进入 Agent Loop 前调用。

它重置的是“当前这次自动执行连续阻止了多少次”，不会重置：

- `active.iterations`；
- Goal 的开始时间；
- Goal 的 token 基线；
- 最近评估原因。

### 6.3 `set_goal()`

按顺序：

1. `strip()`；
2. 拒绝空条件；
3. 拒绝超过 4000 字符；
4. 如果已有活动 Goal，先记录旧 Goal 被替换；
5. 创建新的 `GoalState`；
6. 重置 `consecutive_blocks`；
7. `_record(..., reason="goal set")`。

一个 session 同时只有一个活动 Goal。设置新 Goal 会替换旧 Goal，不会并行保留两个。

### 6.4 `clear()`

没有 Goal 时返回：

```text
No goal set
```

有 Goal 时：

1. 记录非活动、未完成、未失败事件；
2. `active = None`；
3. 重置连续 block 次数；
4. 返回被清除的条件。

主动清除不等同于 `failed`。

### 6.5 `status()`

有活动 Goal 时报告：

```text
Goal active: <condition>
Elapsed: <seconds>s
Evaluations: <iterations>
Tokens: <worker tokens since set_goal>
Last reason: <last evaluator reason>
```

无活动 Goal 时：

- 最近成功：`Goal achieved: ...`；
- 最近失败：`Goal failed: ...`；
- 其他情况：`No goal set`。

被 clear 或 replaced 的最近事件既不是 met 也不是 failed，因此 `status()` 会显示 `No goal set`。

### 6.6 `_record()`

统一生成 `goal_status` 事件：

```python
{
    "type": "goal_status",
    "condition": ...,
    "active": ...,
    "met": ...,
    "failed": ...,
    "reason": ...,
    "iterations": ...,
    "duration": ...,
}
```

同一个事件同时写入：

```python
self.events
self.last_status
```

### 6.7 `evaluate_after_turn()`

这是 Goal Gate 的核心，按以下顺序执行：

#### A. 没有活动 Goal

```python
if self.active is None:
    return StopDecision("allow")
```

行为退化为 s01：Worker 不再调用工具时可以直接结束。

#### B. 后台任务仍在运行

```python
if background_running:
    return StopDecision("defer", ...)
```

此时：

- 不调用 Evaluator；
- 不增加 `iterations`；
- 不清除 Goal；
- AgentSession 把控制权返回宿主。

#### C. 调用 Evaluator 时出错

捕获所有异常：

```python
except Exception as error:
```

随后：

- 将异常类型和文本写入 `last_reason`；
- 记录活动事件；
- 返回 `StopDecision("error", reason)`；
- 保留 Goal；
- 不增加 `iterations`。

无法判断时绝不能假装成功。

#### D. Evaluator 正常返回

先执行：

```python
state.iterations += 1
state.last_reason = evaluation.reason
```

然后处理三个互斥结果。

**完成：**

```python
if evaluation.ok:
```

- 记录 `met=True`；
- 清除活动 Goal；
- 重置连续 blocks；
- 返回 `achieved`。

**不可能：**

```python
if evaluation.impossible:
```

- 记录 `failed=True`；
- 清除活动 Goal；
- 重置连续 blocks；
- 返回 `failed`。

**尚未完成：**

- `consecutive_blocks += 1`；
- 记录 Goal 仍活动；
- 未超上限则返回 `block`；
- 超上限则返回 `limit`。

这里的判断是：

```python
if self.consecutive_blocks > self.block_cap:
```

不是 `>=`。所以 `block_cap=8` 时，第 1～8 次未通过返回 `block`，第 9 次才返回 `limit`。

达到 `limit` 时，Goal 仍然活动，但当前调用结束，把控制权交还用户或宿主。

### 6.8 `restore()` 与当前选中的 `controller`

`restore()` 是一个类方法：

```python
controller = cls(
    evaluator=evaluator,
    block_cap=block_cap,
    events=list(events),
)
```

这里的 `controller` 是一个新建的 `GoalController` 实例。方法随后从后往前查找最近一条 `goal_status` 事件：

```python
for event in reversed(events):
```

- 找到后复制到 `controller.last_status`；
- 如果最近状态为 `active=True`，创建新的 `GoalState`；
- 随后 `break`，不再检查更老事件；
- 返回这个 `controller`。

恢复活动 Goal 时只继承完成条件，以下运行统计重新开始：

```python
iterations=0
set_at=time.time()
tokens_at_start=0
last_reason=None
```

如果最近事件已经 `active=False`，则不会恢复 Goal。也就是说，已完成、失败、清除或替换后的旧活动状态不会“复活”。

本章 CLI 没有调用 `restore()`；它是为更完整的宿主持久化机制准备的接口。

---

## 7. 工具与 hooks

### 7.1 五个工具

`TOOLS` 告诉 Worker 可调用：

| 工具 | 作用 |
|---|---|
| `bash` | 在当前仓库运行 shell 命令 |
| `read_file` | 读取仓库内 UTF-8 文本 |
| `write_file` | 写入仓库内文本 |
| `edit_file` | 对仓库内文件做一次精确替换 |
| `glob` | 在仓库内按模式查找文件 |

schema 是传给模型的能力说明；真正实现位于 `AgentSession._run_tool()`。

### 7.2 Hook 注册

`AgentSession.__init__()` 注册：

```text
UserPromptSubmit → _context_hook
PreToolUse       → _permission_hook, _log_hook
PostToolUse      → _large_output_hook
Stop             → _summary_hook
```

### 7.3 `trigger_hooks()` 的短路行为

```python
for callback in self.hooks[event]:
    result = callback(*args)
    if result is not None:
        return result
```

某个 hook 返回非 `None` 时，后续同事件 hooks 不再执行。

因此 PreToolUse 中：

- 权限 hook 返回 `None`：继续执行日志 hook；
- 权限 hook 返回拒绝原因：立即短路，日志 hook 和真实工具都不会执行。

### 7.4 权限 hook

`bash`：

- 参数必须是字符串；
- 命中 deny list 直接拒绝；
- 命中 destructive list 请求用户确认。

文件工具：

- `path` 必须是字符串；
- `_safe_path()` 确保解析后的路径仍在 `workdir` 内。

### 7.5 `_safe_path()`

```python
candidate = (self.workdir / path).resolve()
candidate.relative_to(self.workdir)
```

如果 `relative_to()` 抛出 `ValueError`，说明路径逃出了仓库，转换为 `GoalError`。

### 7.6 `_run_tool()`

#### `bash`

通过 `subprocess.run()` 执行，具有：

- 当前工作目录为仓库；
- 捕获 stdout/stderr；
- 120 秒超时；
- `check=False`，非零退出码不会自动抛异常。

返回格式：

```text
exit_code=<returncode>
<stdout + stderr 的尾部>
```

输出只保留最后 29950 字符，但退出码在截断前单独拼到开头，因此不会丢失。

#### `read_file`

- `offset` 最小为 1；
- `limit` 在 1～500 之间；
- 解码错误用替换字符处理。

#### `write_file`

- 自动创建父目录；
- UTF-8 写入；
- 返回写入字节/字符数量和相对路径。

#### `edit_file`

要求 `old_text` 在文件中恰好出现一次：

```python
if count != 1:
    return f"Error: Expected 1 occurrence, found {count}"
```

这可以避免模糊替换意外修改多个位置。

#### `glob`

最多返回 200 条结果，并再次检查每个匹配路径仍在工作目录内。

---

## 8. `AgentSession`：主 Agent Loop

### 8.1 `submit()` 的命令路由

#### 单独输入 `/goal`

```python
if stripped == "/goal":
```

只返回 `goal.status()`，不调用 Worker。

#### 输入 `/goal clear` 等别名

调用 `goal.clear()` 并直接返回，不调用 Worker。

#### 输入 `/goal <condition>`

1. 调用 `goal.set_goal()`；
2. 把去掉 `/goal` 前缀后的条件作为 user message；
3. 本轮立即开始执行，不需要再输入“开始”。

#### 普通输入

直接作为 user message 加入对话。若此前已有活动 Goal，普通输入会成为同一 Goal 会话的新信息并继续运行。

最后统一执行：

```python
self.trigger_hooks("UserPromptSubmit", text)
self.goal.begin_query()
return await self._run_query()
```

### 8.2 `submit_background_result()`

用于宿主收到后台任务完成通知时恢复 Goal Loop。

它将结果包装成：

```text
[Background task completed]
<result>
```

并作为普通 user message 加入同一个 `messages[]`。

- 没有活动 Goal：返回 `background_result`，不启动 Worker；
- 有活动 Goal：重置本次 block 计数并重新进入 `_run_query()`。

后台通知本身没有特殊可信权限，Evaluator 仍根据通知中的实际内容判断。

### 8.3 `_run_query()` 主循环

可以压缩为下面的伪代码：

```python
while True:
    if max_turns_reached:
        return SessionResult(status="max_turns")

    response = call_worker(messages, tools)
    messages.append(assistant_response)

    tool_results = run_all_tool_uses(response)
    if tool_results:
        messages.append(tool_results)
        continue

    text = extract_text(response)
    decision = await goal.evaluate_after_turn(messages)

    if decision.action == "block":
        messages.append(goal_feedback)
        continue

    trigger_stop_hook()
    return SessionResult(text, decision.action, decision.reason)
```

重要的是，只有 `block` 会在当前 `_run_query()` 内自动 `continue`。其他 action 都会触发 Stop hook 并返回控制权。

### 8.4 全局 `max_turns`

`max_turns` 限制所有 Worker 调用，包括：

- 工具调用轮；
- 普通文本轮；
- Goal block 后的继续轮。

达到上限时：

- 返回 `status="max_turns"`；
- 保留活动 Goal；
- 不调用 Evaluator；
- 触发 Stop hook。

它和 `block_cap` 的区别：

| 限制 | 计数对象 |
|---|---|
| `max_turns` | Worker 模型调用次数 |
| `block_cap` | Evaluator 判断未完成、阻止停止的连续次数 |

---

## 9. 启动和交互入口

### 9.1 `make_live_session()`

按顺序：

1. 导入 `Anthropic` 和 `load_dotenv`；
2. 加载 `.env`；
3. 读取 `MODEL_ID`；
4. 选择 Evaluator 模型；
5. 创建 Anthropic client；
6. 创建 `PromptGoalEvaluator`；
7. 读取 Stop hook block cap；
8. 创建 `GoalController`；
9. 读取全局 `MAX_TURNS`；
10. 创建并返回 `AgentSession`。

如果 `MAX_TURNS=0`，传入 Session 的是 `None`，表示不启用全局轮数限制。

### 9.2 `main()` 两种模式

有命令行参数：

```bash
python s17_goal_loop/code.py "/goal python -m pytest 退出码为 0"
```

拼接所有参数，只提交一次，然后返回。

无命令行参数：进入交互循环。

```text
s17 >>
```

- `q`、`quit`、`exit`：`break` 退出循环；
- `Ctrl+C` 或 EOF：异常分支中的 `break` 退出循环；
- 空输入：`continue`，重新等待输入；
- 其他输入：调用 `session.submit()`。

这里的 `break` 只退出最近的交互 `while True`。循环后没有其他逻辑，所以 `main()` 隐式返回，随后 `asyncio.run()` 和程序结束。

---

## 10. 用一条 Prompt 模拟完整成功流转

为了覆盖 `glob`、`write_file`、`edit_file`、`read_file`、`bash` 以及关键的 Goal block，使用下面这条教学型 prompt：

```text
/goal 创建 goal_demo/result.txt：先写入 DRAFT，再精确修改为 VERIFIED，
重新读取并确认最终内容恰好是 VERIFIED；然后运行
python -m pytest tests/test_goal_loop.py，直到退出码为 0；
不得修改 tests 目录，最终回复必须报告文件内容和测试退出码。
```

下面是假定的模型行为，用来展示确定的代码路径；真实模型每次选择的工具顺序可能不同。

### 10.1 启动对象图

```text
main()
  └─ make_live_session()
       ├─ Anthropic client
       ├─ PromptGoalEvaluator
       ├─ GoalController
       └─ AgentSession
            ├─ messages=[]
            ├─ total_tokens=0
            ├─ TOOLS
            └─ hooks
```

### 10.2 `submit()` 识别 `/goal`

```python
stripped.startswith("/goal ")  # True
argument = stripped[6:].strip()
```

`argument` 不是清除别名，于是调用：

```python
self.goal.set_goal(argument, self.total_tokens)
```

形成：

```python
GoalState(
    condition="创建 goal_demo/result.txt ...",
    iterations=0,
    set_at=<当前时间>,
    tokens_at_start=0,
    last_reason=None,
)
```

记录第一个事件：

```python
{
    "type": "goal_status",
    "active": True,
    "met": False,
    "failed": False,
    "reason": "goal set",
    "iterations": 0,
}
```

然后只把条件本身加入消息，不包含 `/goal` 前缀：

```python
messages = [
    {"role": "user", "content": "创建 goal_demo/result.txt ..."}
]
```

触发 `UserPromptSubmit` hook，重置本次连续 block 数，进入 `_run_query()`。

### 10.3 Worker 第 1 轮：`glob`

Worker 返回：

```text
tool_use: glob({"pattern": "goal_demo/*"})
```

流程：

```text
_block_type/_block_value
→ PreToolUse permission hook
→ PreToolUse log hook
→ _run_tool("glob")
→ PostToolUse large-output hook
→ 生成 tool_result
→ messages.append(tool_results)
→ continue
```

假设结果：

```text
(no matches)
```

### 10.4 Worker 第 2 轮：`write_file`

Worker 调用：

```python
write_file(
    path="goal_demo/result.txt",
    content="DRAFT",
)
```

权限 hook 通过 `_safe_path()` 确认路径没有逃出仓库。

工具自动创建目录并写入，返回：

```text
Wrote 5 bytes to goal_demo/result.txt
```

加入对话后 `continue`。

### 10.5 Worker 第 3 轮：`edit_file`

Worker 调用：

```python
edit_file(
    path="goal_demo/result.txt",
    old_text="DRAFT",
    new_text="VERIFIED",
)
```

工具确认 `DRAFT` 恰好出现一次后替换，返回：

```text
Edited goal_demo/result.txt
```

### 10.6 Worker 第 4 轮：`read_file`

Worker 调用：

```python
read_file(path="goal_demo/result.txt")
```

返回：

```text
VERIFIED
```

此时文件相关证据已经进入 `messages[]`。

### 10.7 Worker 第一次想停止

假设 Worker 忘了执行 pytest，直接返回普通文本：

```text
文件已经创建并确认内容为 VERIFIED。
```

响应中没有 `tool_use`，因此：

```python
tool_results == []
text = _extract_text(response.content)
```

此时不直接 return，而是调用：

```python
decision = await self.goal.evaluate_after_turn(
    self.messages,
    background_running=False,
)
```

### 10.8 第一次 Evaluator 调用

Evaluator 通过 `transcript_text()` 看到类似：

```text
USER:
创建 goal_demo/result.txt ... pytest 退出码为 0 ...

ASSISTANT:
[tool_use glob ...]

USER:
[tool_result (no matches)]

ASSISTANT:
[tool_use write_file ...]

USER:
[tool_result Wrote 5 bytes ...]

ASSISTANT:
[tool_use edit_file ...]

USER:
[tool_result Edited goal_demo/result.txt]

ASSISTANT:
[tool_use read_file ...]

USER:
[tool_result VERIFIED]

ASSISTANT:
文件已经创建并确认内容为 VERIFIED。
```

文件证据存在，但没有 pytest 输出。Evaluator 返回：

```json
{
  "ok": false,
  "reason": "文件内容已经确认，但对话中没有 pytest 的输出和退出码。",
  "impossible": false
}
```

`_parse_json_object()` 将 JSON 解析为 `value` 字典，校验后构造 `GoalEvaluation`。

Controller 更新：

```python
active.iterations = 1
active.last_reason = "文件内容已经确认，但对话中没有 pytest 的输出和退出码。"
consecutive_blocks = 1
```

并记录 Goal 仍活动的事件，返回：

```python
StopDecision(
    action="block",
    reason="文件内容已经确认，但对话中没有 pytest 的输出和退出码。",
)
```

### 10.9 `block` 重新进入同一个 Agent Loop

`_run_query()` 追加：

```text
[Goal still active]
Condition: <原 Goal 条件>
Evaluator: 文件内容已经确认，但对话中没有 pytest 的输出和退出码。
Continue working and surface the missing evidence.
```

然后执行：

```python
continue
```

用户不需要再次输入“继续”。Worker 立即开始下一轮，并能看到 Evaluator 指出的缺失证据。

### 10.10 Worker 补跑测试

Worker 调用：

```python
bash({
    "command": "python -m pytest tests/test_goal_loop.py"
})
```

假设工具返回：

```text
exit_code=0
17 passed in 0.42s
```

这个 tool result 被加入同一个 `messages[]`。

### 10.11 Worker 第二次想停止

Worker 返回普通文本：

```text
已完成：
- goal_demo/result.txt 的最终内容是 VERIFIED
- python -m pytest tests/test_goal_loop.py 执行成功
- 退出码为 0
```

再次没有 `tool_use`，Goal Gate 再次运行。

### 10.12 第二次 Evaluator 调用

这次 transcript 同时包含：

```text
[tool_result VERIFIED]
```

和：

```text
[tool_result exit_code=0
17 passed in 0.42s]
```

Evaluator 返回：

```json
{
  "ok": true,
  "reason": "文件内容已确认为 VERIFIED，pytest 显示 17 passed 且 exit_code=0。",
  "impossible": false
}
```

Controller 更新：

```python
active.iterations = 2
active.last_reason = "文件内容已确认为 VERIFIED，pytest 显示..."
```

随后：

1. `_record(active=False, met=True, failed=False, ...)`；
2. `self.active = None`；
3. `self.consecutive_blocks = 0`；
4. 返回 `StopDecision("achieved", reason)`。

### 10.13 真正离开 `_run_query()`

因为 action 不再是 `block`：

```python
self.trigger_hooks("Stop", self.messages)
```

`_summary_hook()` 统计 `tool_result` 内容块。本例共有：

```text
glob / write_file / edit_file / read_file / bash
```

所以输出类似：

```text
[hook] Stop: session used 5 tool calls
```

最终返回：

```python
SessionResult(
    text="已完成：...",
    status="achieved",
    reason="文件内容已确认为 VERIFIED，pytest 显示...",
)
```

返回链：

```text
GoalController.evaluate_after_turn()
→ AgentSession._run_query()
→ AgentSession.submit()
→ main()
```

`main()` 打印：

```text
已完成：...
[goal] achieved: 文件内容已确认为 VERIFIED，pytest 显示 17 passed 且 exit_code=0。
```

然后交互模式重新等待下一条输入。

### 10.14 本次运行后的状态

```python
session.goal.active is None
session.goal.consecutive_blocks == 0
result.status == "achieved"
```

`last_status` 保存成功事件，`events` 保存完整 Goal 状态历史。

消息大致按下面的顺序增长：

```text
USER       Goal 条件
ASSISTANT  glob tool_use
USER       glob tool_result
ASSISTANT  write_file tool_use
USER       write_file tool_result
ASSISTANT  edit_file tool_use
USER       edit_file tool_result
ASSISTANT  read_file tool_use
USER       read_file tool_result: VERIFIED
ASSISTANT  第一次完成声明
USER       [Goal still active] 缺少 pytest 证据
ASSISTANT  bash tool_use
USER       bash tool_result: exit_code=0
ASSISTANT  最终总结
```

---

## 11. Goal 所有返回分支

一条成功流程不可能同时进入互斥的失败、后台和超限分支。完整分支如下：

| action/status | 触发条件 | 是否调用 Evaluator | Goal 是否保留 | 是否在当前 `_run_query()` 自动继续 |
|---|---|---:|---:|---:|
| `allow` | 没有活动 Goal | 否 | 不适用 | 否 |
| `defer` | 后台任务仍在运行 | 否 | 是 | 否 |
| `error` | Evaluator 调用或 JSON 解析失败 | 尝试过 | 是 | 否 |
| `achieved` | `ok=True` | 是 | 否 | 否 |
| `failed` | `impossible=True` | 是 | 否 | 否 |
| `block` | 尚未完成且未超 block cap | 是 | 是 | 是 |
| `limit` | 连续 block 次数超过上限 | 是 | 是 | 否 |
| `max_turns` | Worker 总轮数达到全局上限 | 否 | 是 | 否 |

只有 `block` 会由当前 Agent Loop 自动继续。`defer`、`error`、`limit` 和 `max_turns` 都是“Goal 仍活动，但当前调用先结束”。

---

## 12. 后台任务完整逻辑

假设 Worker 启动了后台测试，当前轮停止时：

```python
self.background_running() is True
```

GoalController 返回：

```python
StopDecision("defer", "background work is still running")
```

Evaluator 暂时不运行，因为关键结果尚未进入对话。

宿主之后收到后台结果：

```python
await session.submit_background_result(
    "pytest: 12 passed; exit_code=0"
)
```

消息被追加为：

```text
[Background task completed]
pytest: 12 passed; exit_code=0
```

若 Goal 仍活动，则重新进入同一个 `_run_query()`。Worker 可以解释结果，随后 Evaluator 再根据完整 transcript 判断。

`defer` 不是完成，也不是失败；它只是等待外部事件。

---

## 13. Goal 的生命周期

```text
                 set_goal
                    ↓
                 ACTIVE
       ┌────────────┼───────────────┐
       │            │               │
   ok=True    impossible=True     clear/replace
       │            │               │
    ACHIEVED       FAILED          INACTIVE
```

活动期间还可能暂时返回：

```text
block / defer / error / limit / max_turns
```

其中：

- `block` 会立即自动继续；
- 其余会将控制权交给用户或宿主；
- 它们都不会把 Goal 伪装成完成。

---

## 14. 容易混淆的关键点

### 14.1 Worker 和 Evaluator 是两个角色

Worker 有工具，负责工作；Evaluator 没有工具，只负责判断 transcript。

它们可以使用同一个 Anthropic client，但模型 ID 可以不同。

### 14.2 Goal Gate 不在主循环外另开一条流程

Goal 检查就在 Worker 原本准备 `return` 的位置：

```python
if tool_results:
    continue

decision = await self.goal.evaluate_after_turn(...)
if decision.action == "block":
    continue

return SessionResult(...)
```

它是主循环的退出闸门，不是第二个并行 Agent Loop。

### 14.3 对话记录就是验收证据

Evaluator 无法自行复查文件或命令。Worker 应把具体工具结果写入同一对话：

```text
命令是什么
退出码是多少
测试通过多少项
文件读取结果是什么
```

### 14.4 `/goal condition` 同时是命令和任务

程序既设置 Goal，也把 `condition` 立即作为 user message 交给 Worker，所以不需要再输入第二条“开始执行”。

### 14.5 Goal 的 block cap 不是总评估预算

它统计的是本次 query 内连续 `block` 次数。`begin_query()` 会重置该计数，但 `active.iterations` 会保留整个 Goal 生命周期内的评估次数。

### 14.6 `iterations` 不等于 Worker turns

- Worker 每次模型调用使局部变量 `turns += 1`；
- 只有 Evaluator 正常返回才使 `GoalState.iterations += 1`；
- 工具调用轮通常不会立即触发 Evaluator；
- `defer` 和 Evaluator 异常不会增加 `iterations`。

### 14.7 `status()` 的 token 只统计 Worker

```python
spent = current_tokens - tokens_at_start
```

`current_tokens` 来自 `session.total_tokens`，而该值只在 Worker 响应后通过 `_usage_total()` 累加。

### 14.8 `defer` 会返回宿主

`_run_query()` 只对 `block` 做 `continue`。所以 `defer` 会触发 Stop hook 并形成 `SessionResult(status="defer")`。之后要由宿主调用 `submit_background_result()` 恢复。

### 14.9 权限拒绝也是工具结果

如果 PreToolUse hook 返回拒绝原因，真实工具不会运行，但这段拒绝文本仍会被包装成 `tool_result` 加入对话。Worker 下一轮可以看到失败原因并调整方案。

### 14.10 Evaluator 错误时保守返回

JSON 非法、API 不可用或字段冲突时：

- Goal 不清除；
- 不宣称成功；
- 将错误原因返回用户；
- 等待后续重试或人工处理。

---

## 15. 与 s01 的最小差异

s01 的退出条件近似：

```python
if not tool_results:
    return text
```

s17 将它改为：

```python
if tool_results:
    messages.append(tool_results)
    continue

decision = await goal.evaluate_after_turn(messages)

if decision.action == "block":
    messages.append(evaluator_feedback)
    continue

return SessionResult(
    text=text,
    status=decision.action,
    reason=decision.reason,
)
```

本章最重要的不是工具系统，而是这几行对“何时允许返回”的重新定义。

---

## 16. 阅读代码时建议抓住的主调用链

正常成功路径：

```text
__main__
→ asyncio.run(main())
→ make_live_session()
→ AgentSession.submit()
→ GoalController.set_goal()
→ GoalController._record()
→ AgentSession._run_query()
→ client.messages.create()                 # Worker
→ _usage_total()
→ _block_type() / _block_value()
→ trigger_hooks("PreToolUse")
→ AgentSession._run_tool()
→ trigger_hooks("PostToolUse")
→ _run_query() continue
→ client.messages.create()                 # Worker 想停止
→ _extract_text()
→ GoalController.evaluate_after_turn()
→ PromptGoalEvaluator.evaluate()
→ PromptGoalEvaluator._evaluate_sync()
→ transcript_text()
→ _plain_content()
→ client.messages.create()                 # Evaluator
→ _extract_text()
→ _parse_json_object()
→ GoalEvaluation(**value)
→ StopDecision("block")
→ messages.append("[Goal still active] ...")
→ _run_query() continue
→ Worker 补充证据
→ Evaluator 再次判断
→ GoalController._record(met=True)
→ StopDecision("achieved")
→ trigger_hooks("Stop")
→ SessionResult(status="achieved")
→ main() 打印结果
```

---

## 17. 一句话总结

```text
没有 tool_use = Worker 想停止；
Goal Evaluator 根据对话证据给出的 StopDecision = 系统是否真正允许停止。
```

