# s03 Permission 学习笔记

## 1. 这一节在解决什么问题

s03 的核心目标是：

- 保留 s02 的多工具 Agent 结构
- 但在真正执行工具之前，先做权限判断

也就是说，这一节不是重写 Agent，而是在“工具执行”前面插入一个权限检查层，避免模型直接执行高风险操作。

一句话理解：

`模型先决定要不要用工具，程序再决定这个工具能不能真的执行。`


## 2. 代码整体结构

`code.py` 从上到下可以分成 5 个部分：

1. 初始化运行环境和模型客户端
2. 定义工具函数
3. 定义工具描述和分发表
4. 定义权限三道门
5. 定义主循环并进入交互

可以把它看成下面这条主线：

`用户输入 -> 模型决定是否调用工具 -> 权限检查 -> 执行工具 -> 结果回给模型 -> 模型继续回答`


## 3. 按顺序理解 code.py

### 3.1 环境初始化

文件开头主要做这些事：

- 导入 `os`、`subprocess`、`Path`
- 尝试导入 `readline`，改善终端输入体验
- 导入 `Anthropic` 和 `load_dotenv`
- 读取 `.env`
- 设置当前工作目录 `WORKDIR = Path.cwd()`
- 创建模型客户端 `client = Anthropic(...)`
- 从环境变量读取模型名 `MODEL`
- 设置系统提示词 `SYSTEM`

这里最关键的两个变量是：

- `WORKDIR`
  表示 Agent 工作在哪个目录
- `SYSTEM`
  告诉模型：你是一个 coding agent，而且破坏性操作需要用户批准


### 3.2 工具实现

这一部分定义了 5 个真正做事的函数。

`run_bash(command)`

- 执行 shell 命令
- 工作目录固定在 `WORKDIR`
- 最长运行 120 秒
- 返回标准输出和错误输出

`run_read(path, limit=None)`

- 读取文件内容
- 如果指定 `limit`，只返回前几行

`run_write(path, content)`

- 创建父目录
- 写入文件内容

`run_edit(path, old_text, new_text)`

- 读取整个文件
- 找到 `old_text`
- 只替换第一次出现的内容

`run_glob(pattern)`

- 按 glob 模式查找文件
- 只保留工作区内的结果

这一层的意义是：

`模型不能直接操作系统，它只能调用我们提前定义好的这些工具。`


### 3.3 TOOLS 和 TOOL_HANDLERS

这部分分成两个层面。

`TOOLS`

- 给模型看的“工具菜单”
- 告诉模型有哪些工具可用
- 每个工具都说明了名字、作用、输入参数格式

`TOOL_HANDLERS`

- 给 Python 程序自己用的分发表
- 把工具名映射到真正执行的函数

例如：

- 模型请求调用 `write_file`
- 程序会到 `TOOL_HANDLERS` 里找到 `run_write`
- 然后真正执行 `run_write(...)`

所以这两者的区别是：

- `TOOLS` 解决“模型知不知道能用什么工具”
- `TOOL_HANDLERS` 解决“程序实际该调用哪个函数”


## 4. s03 的重点：权限三道门

这一节是相对 s02 的核心新增内容。

### 4.1 第一道门：硬拒绝

`DENY_LIST` 里放的是绝对不允许执行的危险命令片段，例如：

- `rm -rf /`
- `sudo`
- `shutdown`
- `reboot`

`check_deny_list(command)` 的逻辑很简单：

- 遍历 `DENY_LIST`
- 只要某个危险字符串出现在命令里，就直接返回阻止原因
- 如果都没命中，就返回 `None`

它只对 `bash` 工具生效。

理解成一句话就是：

`特别危险的命令，程序直接拦住，连问都不问。`


### 4.2 第二道门：规则匹配

`PERMISSION_RULES` 处理的是“不是绝对禁止，但需要提高警惕”的情况。

当前代码里有两类规则。

第一类：文件路径越界

- 适用工具：`read_file`、`write_file`、`edit_file`
- 如果目标路径解析后不在 `WORKDIR` 内，就命中规则

第二类：潜在破坏性 bash

- 适用工具：`bash`
- 如果命令里包含 `rm `、`> /etc/`、`chmod 777`
- 就命中规则

`check_rules(tool_name, args)` 的逻辑是：

- 逐条遍历规则
- 如果当前工具匹配某条规则，并且该规则的检查函数返回 `True`
- 就返回规则对应的提示消息
- 否则返回 `None`

这一层的本质是：

`不是直接拒绝，而是标记成“这个操作需要人工确认”。`


### 4.3 第三道门：用户审批

`ask_user(tool_name, args, reason)` 会：

- 在终端打印警告信息
- 显示当前准备执行的工具和参数
- 询问用户是否允许

如果用户输入：

- `y` 或 `yes`，返回 `allow`
- 其他输入，返回 `deny`

这一层就是把决定权交还给人。


### 4.4 总入口：check_permission

`check_permission(block)` 把三道门串起来，执行顺序是固定的：

1. 如果是 `bash`，先检查硬拒绝名单
2. 如果命中拒绝名单，直接返回 `False`
3. 再检查规则匹配
4. 如果规则命中，询问用户
5. 如果用户拒绝，返回 `False`
6. 否则返回 `True`

所以最终有三种结果：

- 高危命令：直接拒绝
- 有风险命令：询问用户
- 普通命令：直接放行


## 5. agent_loop 是怎么工作的

`agent_loop(messages)` 是整个 Agent 的主调度器。

每一轮循环都做这几件事：

1. 调用模型 `client.messages.create(...)`
2. 把系统提示、对话历史、工具列表一起发给模型
3. 模型决定是直接回答，还是发起工具调用
4. 如果模型发起工具调用，程序逐个处理
5. 每个工具调用先经过 `check_permission(block)`
6. 权限通过后，才真正执行工具
7. 把工具执行结果作为 `tool_result` 再发回模型
8. 模型根据结果继续思考，直到不再需要工具

这里最关键的一行就是：

```python
if not check_permission(block):
```

这就是 s03 相比 s02 的核心改动。

可以把它看成一个“执行前拦截器”。


## 6. 一次请求的完整流转示例

这里用一个最典型的请求来模拟：

```text
Delete the file test.txt
```

这个例子适合用来理解 s03，因为它通常会触发 `bash` 工具，并命中“潜在破坏性命令”规则。


### 6.1 用户输入

主程序运行后，用户在终端输入：

```text
Delete the file test.txt
```

代码会把这句话加进 `history`：

```python
history.append({"role": "user", "content": query})
```


### 6.2 进入 agent_loop

程序调用：

```python
agent_loop(history)
```

然后把这些信息一起发给模型：

- 系统提示 `SYSTEM`
- 对话历史 `messages`
- 工具列表 `TOOLS`

模型看到请求后，会自己判断最方便的做法。常见结果是：

```python
{
    "type": "tool_use",
    "name": "bash",
    "input": {"command": "rm test.txt"}
}
```

也就是说，模型决定调用 `bash("rm test.txt")`。


### 6.3 agent_loop 识别到工具调用

程序检查：

```python
if response.stop_reason != "tool_use":
    return
```

因为这次是工具调用，所以不会返回，而是开始遍历 `response.content` 里的 `tool_use` block。

此时拿到的 block 大概是：

- `block.name == "bash"`
- `block.input == {"command": "rm test.txt"}`


### 6.4 执行前先做权限检查

在真正执行工具前，程序会运行：

```python
if not check_permission(block):
```

于是开始进入权限三道门。


### 6.5 第一道门：拒绝名单检查

因为当前工具是 `bash`，程序先调用：

```python
check_deny_list("rm test.txt")
```

它会去检查这个命令是否包含：

- `rm -rf /`
- `sudo`
- `shutdown`
- `reboot`
- `mkfs`
- `dd if=`
- `> /dev/sda`

结果是都没有命中，所以返回 `None`。

说明：

`这条命令不是“绝对禁止”的那类命令。`


### 6.6 第二道门：规则匹配

接着程序调用：

```python
check_rules("bash", {"command": "rm test.txt"})
```

对于 `bash`，规则会检查命令里是否包含：

- `rm `
- `> /etc/`
- `chmod 777`

因为 `"rm test.txt"` 里包含 `"rm "`，所以命中规则，返回：

```text
Potentially destructive command
```

说明：

`这条命令不是直接禁止，但属于高风险操作，需要用户确认。`


### 6.7 第三道门：用户确认

因为第 2 道门命中了规则，程序会调用：

```python
ask_user("bash", {"command": "rm test.txt"}, "Potentially destructive command")
```

终端会显示类似：

```text
Potentially destructive command
Tool: bash({'command': 'rm test.txt'})
Allow? [y/N]
```

程序此时暂停，等待用户输入。


### 6.8 如果用户输入 y

如果用户输入 `y`：

- `ask_user(...)` 返回 `allow`
- `check_permission(block)` 返回 `True`

于是主循环继续执行工具：

```python
handler = TOOL_HANDLERS.get("bash")
output = handler(command="rm test.txt")
```

也就是调用：

```python
run_bash("rm test.txt")
```

如果删除成功，通常输出为空，于是 `run_bash` 会返回：

```text
(no output)
```

然后程序把这个结果包装成 `tool_result`，再送回模型。

模型看到工具已经执行成功，就会生成类似的最终答复：

```text
Deleted test.txt.
```


### 6.9 如果用户拒绝

如果用户没有输入 `y` 或 `yes`，那就算拒绝：

- `ask_user(...)` 返回 `deny`
- `check_permission(block)` 返回 `False`

于是工具不会真的执行。

程序会直接构造一个失败结果：

```python
{
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": "Permission denied. Do not retry this action."
}
```

然后把这个结果发回模型。

模型看到“权限被拒绝”，通常就会改成自然语言回复，例如：

```text
I was not allowed to delete test.txt because permission was denied.
```

这一点很重要，因为它防止模型反复重试同一个危险操作。


## 7. 用这次流转提炼出的理解

对于请求：

```text
Delete the file test.txt
```

完整链路可以记成：

`用户输入 -> 模型请求调用 bash("rm test.txt") -> check_permission -> 第1道门没命中 -> 第2道门命中 -> 第3道门询问用户 -> 用户允许或拒绝 -> 生成 tool_result -> tool_result 回给模型 -> 模型输出最终回答`

这条链路说明了 s03 的本质：

`模型负责提出动作，程序负责决定动作能不能执行。`


## 8. 相比 s02，到底变了什么

s03 没有改变 Agent 的总体思路，只是新增了权限控制层。

新增内容主要是：

- `DENY_LIST`
- `check_deny_list()`
- `PERMISSION_RULES`
- `check_rules()`
- `ask_user()`
- `check_permission()`

主循环只增加了一个关键判断：

```python
if not check_permission(block):
    ...
```

所以可以把 s03 理解为：

`在 s02 的工具执行链路中，插入了一个“先审后执行”的中间层。`


## 9. 学这一节时最该记住的三句话

1. `TOOLS` 是给模型看的，`TOOL_HANDLERS` 是给程序执行用的。
2. `agent_loop` 负责调度模型和工具，`check_permission` 负责拦截风险操作。
3. s03 的关键不是“让模型更安全”，而是“让代码在工具执行前强制做安全判断”。 
