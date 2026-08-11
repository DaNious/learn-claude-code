# s04 Hooks 学习笔记

## 这节在讲什么

这一节的核心不是“又实现了一个 agent”，而是把扩展逻辑从主循环里拆出来，挂到 hooks 上。

也就是说：

- 主循环 `agent_loop()` 只负责对话和工具调度
- 权限检查、日志、输出检查、结束统计这些横切逻辑，不再硬编码进循环
- 这些逻辑通过 hook 注册到不同事件点上

一句话总结：

> 让循环保持稳定，让扩展逻辑挂在循环外面。

---

## 按代码顺序理解 `code.py`

### 1. 启动和环境准备

文件开头先做基础准备：

- 导入 `os`、`subprocess`、`Path`
- 尝试配置 `readline`，改善终端输入体验
- 加载 `.env`
- 初始化 `Anthropic` client
- 读取 `MODEL_ID`
- 定义系统提示词 `SYSTEM`

关键变量：

- `WORKDIR = Path.cwd()`
- `client = Anthropic(...)`
- `MODEL = os.environ["MODEL_ID"]`

`SYSTEM` 告诉模型：你是一个 coding agent，可以用工具，而且破坏性操作需要用户批准。

---

### 2. 定义工具实现

这一段定义了 5 个工具函数：

- `run_bash(command)`
- `run_read(path, limit=None)`
- `run_write(path, content)`
- `run_edit(path, old_text, new_text)`
- `run_glob(pattern)`

它们都是 Python 里真正执行动作的函数。

接着代码又定义了两层映射：

- `TOOLS`：给 LLM 看的工具说明
- `TOOL_HANDLERS`：工具名到 Python 函数的映射

可以这样理解：

- `TOOLS` 负责告诉模型“你能调用什么”
- `TOOL_HANDLERS` 负责在模型真的调用后“由谁来执行”

---

### 3. 定义 Hook 系统

这一节最重要的抽象是：

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
```

它表示：

- 每个事件名对应一个回调列表
- 一个事件可以挂多个 hook

配套有两个函数：

- `register_hook(event, callback)`：把回调挂到某个事件下
- `trigger_hooks(event, *args)`：依次触发该事件下的所有回调

`trigger_hooks()` 的规则很关键：

- 如果某个 hook 返回 `None`，表示继续
- 如果某个 hook 返回非 `None`，就提前停止后续 hook，并把这个值返回出去

这是这份教学代码里最重要的控制方式。

---

### 4. 定义具体 hooks

这份代码定义了 5 个具体 hook 函数：

#### `permission_hook(block)`

绑定在 `PreToolUse` 上。

作用：

- 检查 `bash` 命令是否命中 deny list
- 检查 `bash` 是否是潜在破坏性命令
- 检查读写编辑文件是否访问了工作目录外的路径

如果不允许，它会返回：

- `"Permission denied by deny list"`
- `"Permission denied by user"`

只要返回了非 `None`，这次工具调用就会被拦截。

#### `log_hook(block)`

也绑定在 `PreToolUse` 上。

作用：

- 打印工具调用日志

注意它在 `permission_hook` 后面注册，所以只有没被前一个 hook 拦住时，它才会执行。

#### `large_output_hook(block, output)`

绑定在 `PostToolUse` 上。

作用：

- 如果工具输出特别大，就打印提示

#### `context_inject_hook(query)`

绑定在 `UserPromptSubmit` 上。

作用：

- 在用户输入送进 LLM 之前打印当前工作目录

现在它只是做日志演示，没有真正修改 prompt。

#### `summary_hook(messages)`

绑定在 `Stop` 上。

作用：

- 在本轮 agent 即将停止前，统计这次会话用了多少次工具

---

### 5. 注册 hooks

代码把这些 hook 注册到不同事件：

```python
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
```

这里要特别注意顺序：

- `PreToolUse` 先跑 `permission_hook`
- 再跑 `log_hook`

也就是说，如果权限检查已经返回了非 `None`，后面的日志 hook 就不会执行。

---

### 6. `agent_loop()` 是怎么工作的

`agent_loop(messages)` 是主循环，但现在它已经被清理得很干净。

每轮主要做四件事：

1. 调用 LLM
2. 看模型是否要用工具
3. 如果要用工具，就先过 hooks，再执行工具
4. 如果这轮不再用工具，就触发 `Stop` hook 后退出

伪代码可以理解为：

```python
while True:
    response = LLM(...)
    append assistant response

    if not tool_use:
        trigger Stop hooks
        return

    for each tool block:
        trigger PreToolUse hooks
        if blocked:
            return blocked result to model
            continue

        execute tool
        trigger PostToolUse hooks
        append tool_result
```

这里和 s03 最大的区别是：

- s03：主循环里直接写 `check_permission(block)`
- s04：改成 `trigger_hooks("PreToolUse", block)`

这就把“权限检查”从循环逻辑里拆出去了。

---

## 主流程总结

从用户输入到程序结束，一次完整流程是：

1. 用户输入 prompt
2. 触发 `UserPromptSubmit`
3. 用户消息加入 `history`
4. 调用 LLM
5. 如果 LLM 请求工具：
6. 触发 `PreToolUse`
7. 若未拦截，执行工具
8. 触发 `PostToolUse`
9. 把工具结果喂回 LLM
10. LLM 生成最终文本
11. 触发 `Stop`
12. 打印最后回复

这 4 个事件覆盖了一个 agent cycle 的关键节点：

- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`

---

## 用“删除 test.txt”模拟一次流转

假设用户输入：

```text
删除 test.txt
```

### 第 1 步：主程序读取输入

程序在命令行中读到这句话：

```python
query = input("s04 >> ")
```

---

### 第 2 步：触发 `UserPromptSubmit`

调用：

```python
trigger_hooks("UserPromptSubmit", query)
```

这时会执行 `context_inject_hook(query)`。

它当前只做一件事：

- 打印当前工作目录日志

它返回 `None`，表示不拦截、不修改，继续往下执行。

---

### 第 3 步：把用户消息加入历史

程序执行：

```python
history.append({"role": "user", "content": query})
```

现在 `history` 里多了一条用户消息：

```python
{"role": "user", "content": "删除 test.txt"}
```

---

### 第 4 步：进入 `agent_loop()`

程序把当前 `history` 发给 LLM：

```python
response = client.messages.create(...)
```

模型能看到：

- 系统提示词
- 用户说“删除 test.txt”
- 可用工具列表

因为这里没有专门的 `delete_file` 工具，所以模型大概率会选 `bash`，产生一个 `tool_use`，类似：

```python
{
    "type": "tool_use",
    "name": "bash",
    "input": {"command": "rm test.txt"}
}
```

注意：

- 这是模型做的工具选择
- Python 代码本身并没有写死“删除文件一定用 bash”

---

### 第 5 步：发现这是一次工具调用

如果 `response.stop_reason == "tool_use"`，程序就不会退出，而是开始处理工具调用块。

它会遍历 `response.content`，找到这个 `bash` 工具块。

---

### 第 6 步：先触发 `PreToolUse`

程序执行：

```python
blocked = trigger_hooks("PreToolUse", block)
```

当前注册在 `PreToolUse` 上的有两个 hook：

1. `permission_hook`
2. `log_hook`

按注册顺序先执行 `permission_hook`。

---

### 第 7 步：`permission_hook` 检查命令

`permission_hook` 发现：

- 工具名是 `bash`
- 命令是 `rm test.txt`

它先检查 deny list：

- `rm -rf /`
- `sudo`
- `shutdown`
- `reboot`
- `mkfs`
- `dd if=`

`rm test.txt` 没命中这些绝对禁止项。

然后检查潜在破坏性关键字：

- `"rm "`
- `"> /etc/"`
- `"chmod 777"`

这里 `rm test.txt` 命中了 `"rm "`，所以它会提示用户确认。

终端会出现类似：

```text
Potentially destructive command
Tool: bash({'command': 'rm test.txt'})
Allow? [y/N]
```

---

### 第 8 步：如果用户拒绝

如果用户输入 `n`，或者直接回车：

```python
return "Permission denied by user"
```

这会导致：

- `trigger_hooks("PreToolUse", ...)` 立即返回
- 后面的 `log_hook` 不会再执行
- `bash` 工具不会运行
- 文件不会被删除

随后程序把这个拦截结果包装成 `tool_result` 发回给模型：

```python
{
    "type": "tool_result",
    "content": "Permission denied by user"
}
```

模型看到这个结果后，通常会生成类似答复：

```text
我尝试删除 test.txt，但操作被用户拒绝。
```

---

### 第 9 步：如果用户同意

如果用户输入 `y`：

- `permission_hook` 返回 `None`
- 说明权限通过

这时 `trigger_hooks()` 会继续执行下一个 hook，也就是 `log_hook(block)`。

它会打印一条工具日志，例如：

```text
[HOOK] bash(['rm test.txt'])
```

---

### 第 10 步：真正执行工具

程序接着查找工具处理函数：

```python
handler = TOOL_HANDLERS.get(block.name)
```

对 `bash` 来说，对应的是：

```python
run_bash(command="rm test.txt")
```

它内部通过 `subprocess.run(...)` 执行命令。

如果删除成功，终端通常没有输出，所以 `run_bash()` 最后会返回：

```text
(no output)
```

这里的意思不是“没执行”，而是“执行了，但命令本身没有打印内容”。

---

### 第 11 步：触发 `PostToolUse`

工具执行完后，程序调用：

```python
trigger_hooks("PostToolUse", block, output)
```

当前只有 `large_output_hook`。

但这次输出只是 `(no output)`，很短，所以不会发生额外动作。

---

### 第 12 步：把工具结果喂回模型

程序把工具执行结果加入消息历史：

```python
{
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": "(no output)"
}
```

然后再次调用模型。

模型看到：

- 自己请求了 `rm test.txt`
- 工具返回 `(no output)`

通常会推断删除成功，并生成类似文本：

```text
test.txt 已删除。
```

---

### 第 13 步：触发 `Stop`

当模型这一轮不再请求工具时：

```python
if response.stop_reason != "tool_use":
    force = trigger_hooks("Stop", messages)
```

这时会调用 `summary_hook(messages)`。

它会统计本次会话中一共使用了多少次工具，并打印类似：

```text
[HOOK] Stop: session used 1 tool calls
```

然后 `agent_loop()` 结束。

---

### 第 14 步：主程序打印最终文本

最后主程序把 assistant 的文本输出打印到终端。

所以从用户视角看：

- 如果拒绝，就看到“删除被拒绝”
- 如果同意，就看到“test.txt 已删除”

---

## 这个例子最值得记住的点

### 1. 删除逻辑不是写死在 Python 里的

代码没有写：

```python
if user says delete:
    os.remove(...)
```

而是：

- 模型自己决定该用哪个工具
- 这里大概率选 `bash`

---

### 2. 主循环没有直接写权限检查

主循环只写：

```python
blocked = trigger_hooks("PreToolUse", block)
```

至于到底怎么检查权限，是 hook 决定的，不是循环写死的。

---

### 3. hook 的顺序会影响行为

因为 `permission_hook` 注册在前面，所以：

- 先检查能不能做
- 通过后才记录日志

如果前一个 hook 返回了非 `None`，后一个 hook 根本不会运行。

---

### 4. Hook 是“挂点”，不是“主流程”

主流程仍然是：

- 用户输入
- LLM 决策
- 工具执行
- 结果返回

hook 的作用是：

- 在关键节点前后插入额外逻辑

所以 hook 更像“插件”，而不是主循环本身。

---

## 最后总结

s04 最关键的收获是：

- agent 的核心循环应该尽量稳定、简洁
- 权限、日志、收尾、监控这类横切逻辑适合放到 hooks
- hook 的本质是“在生命周期节点上注册回调”

如果以后继续扩展这个 agent，比如加：

- 自动记录每次工具调用
- 工具执行后自动格式化文件
- 停止前做总结

都可以优先考虑“新增 hook”，而不是继续把逻辑堆进 `agent_loop()`。
