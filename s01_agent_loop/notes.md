# s01 Agent Loop 学习笔记

## 用 “总结当前目录” 这个 prompt 理解程序流转

假设我在命令行里输入：

```text
总结当前目录
```

当前工作目录是：

```text
C:\XS\CodesToGithub\learn-claude-code
```

这个目录里大致有：

- `.git`、`.github`、`.vscode`
- `agents`、`docs`、`skills`、`tests`、`web`
- `s01_agent_loop` 到 `s20_comprehensive`
- `README.md`、`README-zh.md`、`requirements.txt`、`.env.example`

---

## 1. 用户输入进入 history

主程序先读取用户输入：

```python
query = input("\033[36ms01 >> \033[0m")
```

如果输入的是：

```text
总结当前目录
```

那么程序会把它追加到 `history`：

```python
history.append({"role": "user", "content": query})
```

这时的 `history` 是：

```python
[
    {"role": "user", "content": "总结当前目录"}
]
```

然后程序调用：

```python
agent_loop(history)
```

---

## 2. 第一次调用模型

进入 `agent_loop(messages)` 之后，程序会把下面这些信息一起发给模型：

- `system`
- `messages`
- `tools`

对应代码：

```python
response = client.messages.create(
    model=MODEL,
    system=SYSTEM,
    messages=messages,
    tools=TOOLS,
    max_tokens=8000,
)
```

这时模型知道：

- 它是一个 coding agent
- 当前工作目录是什么
- 它运行在 Windows
- 它可以调用一个 shell 工具
- 用户问题是“总结当前目录”

模型通常不会立刻回答，因为它还不知道目录里有什么。它更可能先调用工具去查看目录内容。

---

## 3. 模型请求调用工具

模型可能返回一个 `tool_use`，类似：

```python
[
    {
        "type": "tool_use",
        "name": "bash",
        "id": "toolu_123",
        "input": {"command": "dir"}
    }
]
```

程序先把这条 assistant 消息加入 `history`：

```python
messages.append({"role": "assistant", "content": response.content})
```

这时 `history` 变成：

```python
[
    {"role": "user", "content": "总结当前目录"},
    {"role": "assistant", "content": [tool_use block]}
]
```

然后程序检查：

```python
if response.stop_reason != "tool_use":
    return
```

因为这次确实是工具调用，所以不会结束，而是进入工具执行阶段。

---

## 4. 程序执行工具命令

程序遍历 `response.content`，找到 `tool_use` 后执行：

```python
output = run_bash(block.input["command"])
```

如果模型给出的命令是：

```text
dir
```

那么 `run_bash("dir")` 会在当前目录执行 `dir`，得到目录列表，比如：

```text
.git
.github
.vscode
agents
docs
s01_agent_loop
s02_tool_use
...
s20_comprehensive
skills
tests
web
.env
.env.example
.gitignore
CONTRIBUTING.md
LICENSE
README-ja.md
README-zh.md
README.md
requirements.txt
```

这一步的关键点是：

- 模型并不是直接“知道”当前目录
- 它是先请求调用工具
- 程序替它执行命令
- 再拿到真实结果

---

## 5. 工具结果回传给模型

程序会把工具输出包装成 `tool_result`：

```python
results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
})
```

然后把它作为下一条消息加回 `history`：

```python
messages.append({"role": "user", "content": results})
```

这时 `history` 大概是：

```python
[
    {"role": "user", "content": "总结当前目录"},
    {"role": "assistant", "content": [tool_use block]},
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_123",
                "content": "...dir 的输出..."
            }
        ]
    }
]
```

这就是 Agent Loop 的核心：把工具结果重新喂回模型。

---

## 6. 第二次调用模型，生成最终总结

程序继续下一轮循环，再次调用模型。

这一次模型已经知道：

- 用户要总结当前目录
- 它刚刚执行了 `dir`
- `dir` 的输出结果是什么

所以这次它通常不再调用工具，而是直接输出文本，比如：

```text
当前目录看起来是一个按章节组织的 AI agent 学习项目。
其中 s01 到 s20 是逐步展开的示例目录，另外还有 README、依赖文件、
测试目录和一些配套模块。
```

这条 assistant 回复也会被加入 `history`。

然后程序判断：

```python
if response.stop_reason != "tool_use":
    return
```

因为这次已经不是工具调用，所以 `agent_loop` 结束。

---

## 7. 主程序打印最终结果

回到主程序后，它会取出最后一条 assistant 消息里的文本内容并打印：

```python
response_content = history[-1]["content"]
if isinstance(response_content, list):
    for block in response_content:
        if getattr(block, "type", None) == "text":
            print(block.text)
```

于是终端里最终看到的，就是模型根据目录内容生成的总结。

---

## 一句话理解整个过程

这个例子可以压缩成下面这条链路：

```text
用户输入“总结当前目录”
-> 模型判断需要先看目录
-> 模型发起 tool_use: dir
-> 程序执行 dir
-> 程序把 tool_result 回传给模型
-> 模型根据结果生成总结
-> 程序打印最终回答
```

---

## 这个例子最值得记住的点

1. 模型不是直接知道环境信息，而是通过工具获取信息。
2. `agent_loop` 的核心不是“回答一次”，而是“调用模型 -> 执行工具 -> 回传结果 -> 再调用模型”的循环。
3. `history` 会持续累积，所以这是一个有上下文记忆的多轮过程。
4. Agent 的关键能力不是单次生成文本，而是基于工具结果继续推理。
