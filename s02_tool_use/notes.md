# s02 Learning Notes

## 1. s02 在讲什么

`s02` 的主题是：在 `s01` 只有一个 `bash` 工具的基础上，扩展成一个支持多个工具的 agent。

它想说明的核心思想是：

- 主循环基本不变
- 新增工具时，不要改一大堆逻辑
- 只需要“定义工具”加“注册处理函数”两步

也就是：

`加一个工具 = 在 TOOLS 里加定义 + 在 TOOL_HANDLERS 里加映射`

## 2. s02 相比 s01 新增了什么

[code.py](C:\XS\CodesToGithub\learn-claude-code\s02_tool_use\code.py) 里主要新增了三块：

1. 4 个新工具实现
2. 工具定义列表 `TOOLS`
3. 工具分发表 `TOOL_HANDLERS`

原来 s01 只有：

- `bash`

s02 扩展成了 5 个工具：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `glob`

## 3. 按代码顺序理解 s02

### 3.1 初始化阶段

程序开头先做环境准备：

- 导入模块
- 加载 `.env`
- 创建 `Anthropic` 客户端
- 读取 `MODEL_ID`
- 记录当前工作目录 `WORKDIR`

`WORKDIR = Path.cwd()` 很重要，因为后面的文件工具都默认只在当前工作目录内操作。

### 3.2 SYSTEM 提示词

`SYSTEM` 告诉模型：

- 你是一个 coding agent
- 当前在 Windows 环境
- 优先使用工具解决任务
- 少解释，多执行

这相当于给模型设定工作方式。

### 3.3 旧工具 `run_bash`

`run_bash(command)` 是从 s01 延续下来的工具。

它的作用是：

- 接收一条 shell 命令
- 在 `WORKDIR` 下执行
- 返回标准输出和错误输出

它还做了两件保护：

- 简单拦截明显危险命令
- 设置 `120s` 超时

### 3.4 新增的 4 个文件工具

s02 新增了：

- `run_read(path, limit=None)`
- `run_write(path, content)`
- `run_edit(path, old_text, new_text)`
- `run_glob(pattern)`

它们分别负责：

- 读取文件
- 写入文件
- 替换文件中的一段文本
- 按 glob 模式查找文件

### 3.5 `safe_path` 的作用

`safe_path(p)` 是文件工具共用的安全检查。

它会：

1. 把相对路径解析成绝对路径
2. 检查这个路径是否仍然在 `WORKDIR` 内

如果路径逃出了工作目录，就抛错。

所以文件工具虽然能读写文件，但只能操作当前 workspace 内的内容。

### 3.6 `TOOLS` 是给模型看的

`TOOLS` 不是执行逻辑，而是“工具说明书”。

它告诉模型：

- 工具有哪些
- 每个工具叫什么
- 每个工具是干什么的
- 调用时需要什么参数

比如：

- `read_file` 需要 `path`
- `write_file` 需要 `path` 和 `content`
- `glob` 需要 `pattern`

模型正是根据这里的定义，决定自己要调用哪个工具。

### 3.7 `TOOL_HANDLERS` 是给 Python 查表用的

`TOOL_HANDLERS` 是 s02 最关键的结构：

- 工具名 -> Python 函数

例如：

- `"bash"` -> `run_bash`
- `"read_file"` -> `run_read`
- `"write_file"` -> `run_write`

这就是“分发”或“查表调用”。

以前的思路像是：

`如果模型用了 bash，就手写调用 run_bash`

现在变成：

`模型说自己要哪个工具，就按工具名去表里找对应函数`

所以新增工具时，不需要改主循环结构，只要往表里注册新函数。

## 4. agent_loop 是怎么工作的

`agent_loop(messages)` 是整个 agent 的核心循环。

它每轮都做 4 件事：

1. 把消息历史和工具列表发给模型
2. 看模型是要继续调工具，还是直接回答
3. 如果模型请求工具，就执行工具
4. 把工具结果塞回消息历史，再进入下一轮

关键分界点是：

- `response.stop_reason == "tool_use"`：模型要调用工具
- 否则：模型已经给出最终文本答案

### 4.1 工具执行的关键一行

s02 的核心变化在这行：

`output = handler(**block.input)`

展开理解就是：

1. 先用 `block.name` 找到 handler
2. 再把 `block.input` 里的参数展开传进去

例如模型返回：

```python
{
  "name": "read_file",
  "input": {"path": "README.md"}
}
```

程序就会执行：

```python
run_read(path="README.md")
```

## 5. 用 “Read README.md” 这个例子看程序流转

假设你输入：

```text
Read README.md and tell me what this project is about
```

### 第 1 步：用户输入进入历史消息

主程序会先把这句话加入 `history`：

```python
history.append({"role": "user", "content": query})
```

然后调用：

```python
agent_loop(history)
```

### 第 2 步：把问题和工具一起发给模型

`agent_loop()` 调用 `client.messages.create(...)`，把下面几样东西发给模型：

- `system`
- `messages`
- `tools`

此时模型知道：

- 用户想读 `README.md`
- 自己可以使用 `read_file`

### 第 3 步：模型先返回 `tool_use`

模型不会立刻回答项目是什么，而是通常先请求工具，大概像这样：

```python
{
  "type": "tool_use",
  "name": "read_file",
  "input": {"path": "README.md"}
}
```

这时 `stop_reason` 会是 `"tool_use"`。

### 第 4 步：程序按工具名执行对应函数

程序遍历 `response.content`，找到这条工具调用：

1. 读取 `block.name`
2. 去 `TOOL_HANDLERS` 查
3. 找到 `run_read`
4. 执行 `run_read(path="README.md")`

然后 `run_read()` 内部会：

1. 先调用 `safe_path("README.md")`
2. 确认路径没有跑出工作目录
3. 读取文件内容
4. 返回内容字符串

### 第 5 步：把工具结果再喂回模型

程序把工具输出包装成 `tool_result`：

```python
{
  "type": "tool_result",
  "tool_use_id": block.id,
  "content": output
}
```

然后把这份结果加入消息历史，再发给模型下一轮。

这一步可以理解为：

`模型先要工具 -> Python 真去执行 -> 再把执行结果告诉模型`

### 第 6 步：模型基于文件内容生成最终回答

第二轮时，模型已经真正看到了 `README.md` 内容。

这时它通常不再请求工具，而是直接返回一段文字总结项目内容。

一旦 `response.stop_reason != "tool_use"`，`agent_loop()` 就结束。

最后主程序把文本打印到终端里。

## 6. 这个例子里消息是怎么变化的

可以把消息历史粗略理解成这样：

第一轮前：

```python
[
  {"role": "user", "content": "Read README.md and tell me what this project is about"}
]
```

第一轮后，模型请求工具：

```python
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": [tool_use(read_file)]}
]
```

工具执行完后：

```python
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": [tool_use(read_file)]},
  {"role": "user", "content": [tool_result(...README contents...)]}
]
```

第二轮后，模型给出最终文本：

```python
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": [tool_use(read_file)]},
  {"role": "user", "content": [tool_result(...)]},
  {"role": "assistant", "content": [text("This project is about ...")]}
]
```

## 7. s02 的一句话总结

s02 的本质是：

`让“只有一个 bash 工具的 agent”升级成“支持多个工具、并且按工具名自动分发”的 agent`

最关键的设计点不是多了哪 4 个工具，而是：

`主循环不变，工具执行从硬编码改成查表分发`

## 8. “落盘”是什么意思

“落盘”就是把工具的完整输出写到磁盘文件里，而不是把超长内容直接塞回 LLM 上下文。

常见流程是：

1. 某个工具返回了很长的结果
2. 结果超过 `maxResultSizeChars`
3. 系统把完整结果写入一个临时文件
4. 模型实际看到的是“预览 + 文件路径”

这样做的好处是：

- 节省上下文
- 避免一次塞入太多文本
- 让模型在需要时再决定是否读取完整结果

## 9. 为什么普通工具可以落盘

像 `glob`、搜索工具、日志工具、代码分析工具，经常可能返回很长的结果。

这类工具如果结果太大，可以先落盘，然后让模型看到：

- 一小段预览
- 完整内容保存在某个文件里

如果模型确实想看完整内容，再调用 `read_file(...)` 去读取那个文件。

所以正常链路是：

`普通工具 -> 输出太长 -> 落盘 -> 模型再用 read_file 读取完整内容`

## 10. 为什么 `read_file` 不能再按同样方式落盘

`read_file` 的职责本来就是把文件内容读给模型看。

如果它自己也受 `maxResultSizeChars` 限制，就会出现递归套娃：

1. 模型调用 `read_file("big.txt")`
2. 结果太大
3. 系统把读取结果再落盘成另一个文件
4. 模型看到“完整内容在另一个文件里”
5. 模型再调用 `read_file(...)` 去读那个新文件
6. 新文件内容还是一样大
7. 又再次落盘

于是就会变成：

`读文件 -> 落盘 -> 再读 -> 再落盘 -> ...`

这就是文档里说的无限循环。

## 11. 为什么给 FileRead 设成 Infinity

`FileRead = Infinity` 的意思不是“完全不考虑大文件问题”，而是：

`read_file` 的结果不要再走“超长就落盘成另一个文件”这条机制

它防的是“递归落盘”，不是说系统从此对大文件完全不设防。

## 12. 大文件怎么让 LLM 接受

正确做法通常不是“对 `read_file` 再落盘一次”，而是换别的方式控制输出大小：

1. 限制单次读取范围
2. 分块读取
3. 按上下文预算截断
4. 先搜索定位，再局部读取

常见形式包括：

- `read_file(path, limit=200)`
- `read_file(path, offset=400, limit=200)`
- 先读开头，再读中间，再读结尾
- 先搜索关键词，再读相关片段

关键区别是：

- 不好的方案：`read_file` 太大 -> 落盘成另一个文件 -> 再让 `read_file` 去读
- 更好的方案：`read_file` 太大 -> 只返回当前需要的一段

## 13. 这和 s02 的关系

s02 还没有实现完整的 `maxResultSizeChars` 机制，但它已经有一个接近的思路：

`run_read(path, limit=None)`

这里的 `limit` 表示：

- 不一定一次读完整个文件
- 可以只读前面若干行

所以 s02 对大文件的思路更接近：

`限制读取范围，而不是读出来后再转存成另一个文件`

这和真正工程里的分页读取、分块读取、局部读取，是同一个方向。

## 14. 最后速记

- s02 的主题是“多工具 + 查表分发”
- `TOOLS` 负责告诉模型“有哪些工具可用”
- `TOOL_HANDLERS` 负责让 Python 按工具名找到真正的函数
- `agent_loop` 的模式是：模型要工具 -> Python 执行 -> 结果喂回模型 -> 模型继续
- `safe_path` 用来防止文件工具越界到工作目录之外
- “落盘”是把超长工具输出写入文件
- 普通工具可以落盘，但 `read_file` 不能再递归落盘
- 大文件更合理的处理方式是限制范围、分块读取，而不是反复转存文件
