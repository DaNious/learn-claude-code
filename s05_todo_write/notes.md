# s05 TodoWrite 学习笔记

## 1. 这一节在讲什么

`s05_todo_write` 的核心目标，是在 `s04` 的基础上给 Agent 增加“先规划、再执行”的能力。

它新增了一个 `todo_write` 工具，但这个工具本身不读文件、不写文件、不执行命令，只负责：

- 保存当前任务列表
- 展示任务状态
- 提醒模型不要忘记更新计划

这一节最重要的一句话是：

> `todo_write` 增加的不是执行能力，而是规划能力。

---

## 2. 整体结构概览

`code.py` 可以分成 5 个部分：

1. 启动初始化
2. 基础工具实现
3. `todo_write` 工具实现
4. Hook 系统
5. `agent_loop` 主循环

整个程序的执行关系可以理解成：

`用户输入 -> LLM 决定是否调工具 -> Python 执行工具 -> 工具结果返回给 LLM -> LLM 继续下一步`

---

## 3. 按顺序理解 `code.py`

### 3.1 启动初始化

程序开始时会先做这些事：

- 导入 `ast`、`json`、`os`、`subprocess`、`Path`
- 导入 `Anthropic` 和 `load_dotenv`
- 加载 `.env`
- 读取当前工作目录 `WORKDIR = Path.cwd()`
- 创建大模型客户端 `client = Anthropic(...)`
- 从环境变量中拿模型名 `MODEL = os.environ["MODEL_ID"]`
- 初始化一个全局变量 `CURRENT_TODOS = []`

这里的 `CURRENT_TODOS` 很重要，它表示当前任务清单保存在内存里。程序退出后，todo 就没了。

### 3.2 SYSTEM 提示词

`SYSTEM` 会告诉模型：

- 你是一个 coding agent
- 遇到多步骤任务时，要先调用 `todo_write`
- 在执行过程中要持续更新状态

这一步相当于从提示词层面约束模型的行为。

### 3.3 基础工具函数

这部分主要来自前几节：

- `safe_path(p)`
  - 把路径限制在工作区内，防止访问工作区外文件
- `run_bash(command)`
  - 执行 shell 命令
- `run_read(path, limit=None)`
  - 读取文件内容
- `run_write(path, content)`
  - 写文件，如果父目录不存在会自动创建
- `run_edit(path, old_text, new_text)`
  - 在文件中做一次精确替换
- `run_glob(pattern)`
  - 按 glob 模式找文件

这些函数是真正“干活”的执行工具。

### 3.4 新增的 `todo_write`

这是 `s05` 的核心新增内容。

#### `_normalize_todos(todos)`

这个函数负责校验模型传入的 todo 参数：

- 如果是字符串，先尝试按 JSON 解析
- JSON 不行，再尝试 `ast.literal_eval`
- 最后确认它是不是一个列表
- 列表中的每一项必须是字典
- 每一项都必须有：
  - `content`
  - `status`
- `status` 只能是：
  - `pending`
  - `in_progress`
  - `completed`

它的作用是避免模型传错格式。

#### `run_todo_write(todos)`

这个函数的行为很简单：

1. 调用 `_normalize_todos` 校验输入
2. 更新全局变量 `CURRENT_TODOS`
3. 把当前任务列表打印到终端
4. 返回 `Updated N tasks`

所以它不会真正创建文件，也不会修改代码，只负责更新“任务视图”。

### 3.5 TOOLS 和 TOOL_HANDLERS

`TOOLS` 是暴露给模型看的工具定义，告诉模型：

- 可用工具有哪些
- 每个工具的参数长什么样

这一节一共有 6 个工具：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`
- `todo_write`

`TOOL_HANDLERS` 则是工具名到 Python 函数的映射，比如：

- `bash -> run_bash`
- `todo_write -> run_todo_write`

模型产生工具调用后，Python 端就是通过这张表找到真正执行函数的。

### 3.6 Hook 系统

Hook 系统是上一节留下来的扩展点，这一节继续保留。

定义了 4 类事件：

- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`

相关函数：

- `register_hook(event, callback)`
  - 注册 hook
- `trigger_hooks(event, *args)`
  - 触发 hook

当前注册了几个 hook：

- `permission_hook`
  - 如果 `bash` 命令里出现危险字符串，比如 `rm -rf /`、`shutdown`，就拦截
- `log_hook`
  - 在工具调用前打印日志
- `context_inject_hook`
  - 用户提交 prompt 时打印当前工作目录
- `summary_hook`
  - 结束时统计总共调了多少次工具

### 3.7 主循环 `agent_loop(messages)`

这是整个 Agent 的执行核心。

它维护了一个变量：

- `rounds_since_todo = 0`

表示已经连续多少轮没有更新 todo。

主循环每轮大致这样执行：

1. 如果连续 3 轮没调用 `todo_write`，自动插入：
   - `<reminder>Update your todos.</reminder>`
2. 调用大模型 `client.messages.create(...)`
3. 如果模型没有请求工具，说明它准备直接结束
4. 如果模型请求工具，就遍历每个 `tool_use`
5. 先执行 `PreToolUse` hook
6. 再根据 `TOOL_HANDLERS` 找到实际函数执行
7. 把工具结果包装成 `tool_result` 返回给模型
8. 如果调用的是 `todo_write`，就把 `rounds_since_todo` 清零

这个循环构成了一个完整的 agent 执行闭环。

### 3.8 程序入口

在 `if __name__ == "__main__":` 中，程序会：

- 打印欢迎信息
- 循环读取用户输入
- 输入 `q`、`exit` 或空行时退出
- 每次输入后触发 `UserPromptSubmit`
- 把用户消息放进 `history`
- 调用 `agent_loop(history)`
- 最后把 assistant 返回的文本打印出来

---

## 4. 一句话总结这份代码

这份代码本质上是一个“带工具调用能力的简化版 Agent 框架”，而 `s05` 相比 `s04` 的关键变化只有两个：

1. 增加了 `todo_write`，让模型先列计划再动手
2. 增加了 `rounds_since_todo` 提醒机制，防止模型做着做着忘了更新计划

---

## 5. 用“创建一个 Python package”模拟一次程序流转

这里用这个 prompt 作为例子：

```text
Create a Python package under s05_todo_write/example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py
```

### 5.1 用户输入

在主程序里，用户输入这句 prompt 后：

- 触发 `UserPromptSubmit` hook
- 程序把消息加入 `history`

此时消息历史大致是：

```python
[
    {
        "role": "user",
        "content": "Create a Python package under s05_todo_write/example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py"
    }
]
```

### 5.2 进入 `agent_loop`

`agent_loop(history)` 启动后：

- 先令 `rounds_since_todo = 0`
- 因为还没超过 3 轮，所以不会插入 reminder

### 5.3 第一次请求模型

程序把这些信息一起发给模型：

- `SYSTEM`
- 当前 `messages`
- `TOOLS`

因为系统提示明确要求“多步骤任务先用 `todo_write` 规划”，而创建 package 显然是多步任务，所以模型的理想行为是：

- 第一轮先调用 `todo_write`

### 5.4 模型第一次调用 `todo_write`

模型可能生成类似这样的工具调用：

```python
{
    "type": "tool_use",
    "name": "todo_write",
    "input": {
        "todos": [
            {"content": "Create package directory structure under s05_todo_write/example/demo_pkg", "status": "in_progress"},
            {"content": "Create __init__.py", "status": "pending"},
            {"content": "Create utils.py", "status": "pending"},
            {"content": "Create tests/test_utils.py", "status": "pending"},
            {"content": "Verify files were created correctly", "status": "pending"}
        ]
    }
}
```

程序执行流程是：

1. 触发 `PreToolUse`
2. 因为不是 `bash`，不会被权限拦截
3. 找到 `run_todo_write`
4. 校验并保存 todo
5. 在终端打印当前任务列表
6. 返回 `Updated 5 tasks`

同时：

- 因为调用了 `todo_write`
- 所以 `rounds_since_todo = 0`

### 5.5 工具结果回传给模型

程序会把这次执行结果包装成 `tool_result`，再作为一条消息追加到 `messages` 中。

然后 `agent_loop` 继续下一轮，让模型基于“todo 已建立”这一事实继续工作。

### 5.6 第二轮开始真正创建文件

接下来模型就会开始调用真正干活的工具，比如：

- `write_file`
- `glob`
- `read_file`

例如它可能先创建 `__init__.py`：

```python
{
    "type": "tool_use",
    "name": "write_file",
    "input": {
        "path": "s05_todo_write/example/demo_pkg/__init__.py",
        "content": ""
    }
}
```

程序执行 `run_write(...)` 时会：

1. 用 `safe_path` 校验路径
2. 自动创建父目录
3. 把文件写到磁盘
4. 返回写入结果

这里有个很值得注意的点：

- 代码并没有专门的 `mkdir` 工具
- 目录的创建是由 `write_file` 顺手完成的

### 5.7 模型更新 todo 状态

一个比较理想的行为是，模型每完成一部分，就再次调用 `todo_write` 更新状态。

例如：

- 把 “Create package directory structure” 改成 `completed`
- 把 “Create utils.py” 改成 `in_progress`

这样终端中的任务面板就会持续变化，体现出当前执行进度。

### 5.8 创建 `utils.py` 和测试文件

后续模型可能继续调用：

- `write_file(".../utils.py", "...")`
- `write_file(".../tests/test_utils.py", "...")`

每次调用都遵循同一个闭环：

1. 模型请求工具
2. Python 执行工具
3. 返回 `tool_result`
4. 模型决定下一步

### 5.9 验证结果

当文件创建完成后，模型通常还会做验证，比如调用：

- `glob` 查看目录结构
- `read_file` 检查写入内容

这一步对应 todo 里最后那类 “Verify ...” 任务。

### 5.10 如果太久不更新 todo，会被提醒

如果模型连续 3 轮只顾着写文件、读文件、改文件，而没有调用 `todo_write`，程序会在下一轮自动插入：

```text
<reminder>Update your todos.</reminder>
```

这就是 `s05` 新增的 nag reminder 机制，目的是防止模型做着做着忘了最初计划。

### 5.11 最终结束

当模型认为任务已经完成，不再请求工具时：

- `response.stop_reason != "tool_use"`

程序就会进入停止逻辑：

1. 触发 `Stop` hook
2. `summary_hook` 统计总工具调用次数
3. `agent_loop` 返回
4. 主程序把最终文字结果打印出来

最终输出可能类似：

```text
Created the Python package under s05_todo_write/example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py.
```

---

## 6. 这次模拟真正想说明什么

这个例子里最重要的，不是“怎么创建 package”，而是 “`s05` 比 `s04` 多了哪一层能力”：

- `s04`
  - 模型拿到任务后，可能直接开始操作文件
- `s05`
  - 模型先调用 `todo_write` 列计划
  - 执行过程中持续更新状态
  - 如果很久不更新，程序会自动提醒

所以 `s05` 的重点不是新加了一个更强的执行工具，而是给 Agent 增加了一个显式的任务管理层。

---

## 7. 适合记住的几个关键词

- `CURRENT_TODOS`
  - 当前任务列表，保存在内存中
- `todo_write`
  - 负责任务规划与状态更新
- `_normalize_todos`
  - 负责校验 todo 参数是否合法
- `TOOLS`
  - 暴露给模型的工具定义
- `TOOL_HANDLERS`
  - 工具名到 Python 函数的映射
- `HOOKS`
  - 用于插入权限检查、日志、总结等扩展逻辑
- `rounds_since_todo`
  - 用于触发 reminder 的计数器
- `agent_loop`
  - 整个 Agent 的主循环

---

## 8. 最后总结

`s05_todo_write` 这一节想教会我们的，是一个非常实用的 Agent 设计思想：

- 不要只给模型“执行工具”
- 还要给模型“管理任务过程的工具”

因为很多时候，Agent 出问题不是因为不会写代码，而是因为：

- 忘了原始目标
- 做到一半跑偏
- 被新的错误吸走注意力

`todo_write` 正是用来缓解这个问题的。
