# s07 学习笔记

## 主题

这一章的核心是 **Skill Loading（按需加载技能）**。

目标不是把所有规范、说明文档、风格指南一次性塞进 `SYSTEM prompt`，而是分成两层：

1. 第一层：启动时只注入技能目录
2. 第二层：运行时按需加载某个技能的完整内容

这样做的好处是：

- 平时上下文更轻
- 只有真正需要某个技能时才消耗更多 token
- 扩展技能时只需要往 `skills/` 目录里新增内容

---

## README 在讲什么

`README.md` 的核心思想是：

- 不要把所有知识都堆到 `system prompt`
- 可以只把“有哪些技能可用”告诉模型
- 当模型判断当前任务需要某个技能时，再调用 `load_skill`
- `load_skill` 返回该技能的完整 `SKILL.md`

也就是说，这里设计的是两级知识注入：

### 第一级：Catalog

启动时扫描 `skills/`，把每个技能的：

- 名称
- 一行描述

拼进 `SYSTEM` prompt。

这一级很便宜，因为只放简介，不放全文。

### 第二级：Full Content

当模型真的需要某个技能，比如 `code-review`，就调用：

```python
load_skill("code-review")
```

程序再把 `code-review/SKILL.md` 的完整内容通过 `tool_result` 注入进当前对话。

这一级比较贵，但只在需要时发生。

---

## code.py 按顺序理解

下面按程序从上到下、从启动到运行的顺序理解。

### 1. 环境初始化

文件开头先完成基础准备，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:29)。

主要做了这些事：

- 导入标准库和第三方库
- 读取 `.env`
- 创建 Anthropic 客户端
- 获取模型名 `MODEL_ID`
- 设置当前工作目录 `WORKDIR`
- 设置技能目录 `SKILLS_DIR = WORKDIR / "skills"`

这一步的作用是把整个程序运行所需的上下文先准备好。

---

### 2. 解析技能 frontmatter

函数 `_parse_frontmatter()` 负责解析 `SKILL.md` 顶部 YAML frontmatter，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:52)。

如果一个技能文件长这样：

```md
---
name: code-review
description: Review code for bugs, risks, and missing tests
---
```

那么这个函数会把它拆成：

- `meta`：前面的 YAML 元数据
- `body`：正文内容

这一层解析让程序能拿到技能名和技能描述，而不只是把文件当普通文本处理。

---

### 3. 启动时扫描 skills 目录

`SKILL_REGISTRY` 是技能注册表，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:67)。

`_scan_skills()` 会：

- 遍历 `skills/` 下的子目录
- 找每个子目录里的 `SKILL.md`
- 读出文件全文
- 解析 frontmatter
- 提取 `name`、`description`、`content`
- 保存到 `SKILL_REGISTRY`

然后程序在启动阶段立刻执行：

```python
_scan_skills()
```

这意味着技能目录是在程序启动时就建立好的，而不是等模型调用时才临时去找文件。

这里的一个重要设计点是：

- 后面 `load_skill(name)` 通过注册表查技能
- 不直接拼文件路径去读
- 这样更安全，也更清晰

---

### 4. 构造 SYSTEM prompt

`list_skills()` 会把所有技能转成简短目录，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:86)。

`build_system()` 会把这个目录拼进 `SYSTEM`，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:93)。

构造出来的 `SYSTEM` 大致像这样：

```text
You are a coding agent at <WORKDIR>.
Skills available:
- **agent-builder**: ...
- **code-review**: ...
- **mcp-builder**: ...
- **pdf**: ...
Use load_skill to get full details when needed.
```

这一步就是第一层知识注入。

模型从一开始就知道：

- 当前有哪些技能可用
- 如果需要细节，就去调用 `load_skill`

但此时它还没有看到技能全文。

---

### 5. 子 Agent 的 system prompt

`SUB_SYSTEM` 是子 agent 用的系统提示，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:104)。

它和主 agent 的区别是：

- 不包含技能目录
- 不负责技能加载
- 只负责完成被分配的子任务并返回总结

这说明 s07 的技能加载机制主要是给主 agent 准备的。

---

### 6. 基础工具实现

从 `safe_path()` 开始，到 `extract_text()` 为止，是前几章已经有的工具层，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:116)。

主要工具有：

- `run_bash(command)`：执行 shell 命令
- `run_read(path, limit=None)`：读文件
- `run_write(path, content)`：写文件
- `run_edit(path, old_text, new_text)`：精确替换一次
- `run_glob(pattern)`：按 glob 找文件
- `run_todo_write(todos)`：更新 todo 列表

其中 `safe_path()` 很重要，它确保路径不能逃出工作区。

这部分和 s07 的核心主题关系不在“新增能力”，而在“为模型提供可执行动作”。

模型不仅能理解技能，还能在技能指导下继续读文件、写文件、搜索文件。

---

### 7. 子 Agent 执行机制

`spawn_subagent()` 实现了子 agent 的独立循环，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:219)。

运行方式是：

- 主 agent 调用 `task`
- 程序创建一个新的消息循环给子 agent
- 子 agent 只能使用 `SUB_TOOLS`
- 子 agent 做完后只返回最终总结

这部分在 s07 中没有本质变化，属于沿用之前的架构。

---

### 8. s07 新增核心：load_skill

这章最重要的新函数是 `load_skill(name)`，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:274)。

逻辑非常简单：

```python
def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]
```

它做的事是：

- 根据技能名查 `SKILL_REGISTRY`
- 如果找到，就返回完整 `SKILL.md`
- 如果找不到，就返回错误提示

这里就是第二层知识注入发生的地方。

和第一层不同的是：

- 第一层只把技能简介放进 `SYSTEM`
- 第二层把完整技能内容作为一次工具结果注入进消息历史

---

### 9. 工具注册

`TOOLS` 定义主 agent 可调用的所有工具，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:286)。

s07 相比前面章节，多了一个新工具：

```python
{
    "name": "load_skill",
    "description": "Load the full content of a skill by name.",
    ...
}
```

`TOOL_HANDLERS` 再把工具名映射到具体函数：

- `bash -> run_bash`
- `read_file -> run_read`
- `task -> spawn_subagent`
- `load_skill -> load_skill`

这样一来，主循环不需要特殊写死“如果是技能就怎样”，而是统一走工具分发机制。

---

### 10. Hook 系统

`HOOKS`、`register_hook()`、`trigger_hooks()` 组成了一个轻量事件系统，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:315)。

当前注册的 hook 包括：

- `context_inject_hook`
- `permission_hook`
- `log_hook`
- `summary_hook`

作用分别是：

- 用户提交问题时打印当前工作目录
- 工具调用前拦截危险 bash 命令
- 工具调用前打印工具名
- 会话结束时统计用了多少次工具

这些 hook 不改变 s07 的主设计，但让整个流程更容易观测和控制。

---

### 11. 主循环 agent_loop

`agent_loop(messages)` 是整个程序的核心运行循环，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:356)。

它的运行顺序可以概括为：

1. 检查是否该提醒更新 todo
2. 调用模型 `client.messages.create(...)`
3. 如果模型这轮不需要工具，结束
4. 如果模型要调用工具：
5. 对每个工具调用先跑 hook
6. 执行对应 handler
7. 把结果包装成 `tool_result`
8. 追加回消息历史
9. 继续下一轮

所以这不是“一次提问，一次回答”的简单结构，而是：

- 模型可以连续多轮调用工具
- 每次工具结果都会反馈回模型
- 直到模型认为可以输出最终文本为止

---

### 12. 入口 main

程序最底部的 `__main__` 是命令行交互入口，见 [code.py](C:/XS/CodesToGithub/learn-claude-code/s07_skill_loading/code.py:390)。

执行顺序是：

- 打印提示语
- 进入输入循环
- 读取用户问题
- 触发 `UserPromptSubmit` hook
- 把用户输入加入 `history`
- 调用 `agent_loop(history)`
- 最后把模型生成的文本打印到终端

这就是从“用户打一行字”到“模型多轮调用工具再回复”的外层控制流程。

---

## 用“我需要做一次代码审查”模拟一次流转

下面用这个 prompt 模拟：

```text
我需要做一次代码审查
```

---

### 1. 程序启动前置状态

在你输入之前，程序已经完成了这些事情：

- 扫描 `skills/`
- 建好 `SKILL_REGISTRY`
- 生成 `SYSTEM`

假设注册表中已经有：

```python
SKILL_REGISTRY["code-review"] = {
    "name": "code-review",
    "description": "...",
    "content": "完整的 SKILL.md"
}
```

此时模型虽然还没看到 `code-review` 全文，但已经知道这个技能存在。

---

### 2. 用户输入 prompt

你输入：

```text
我需要做一次代码审查
```

程序会：

- 触发 `UserPromptSubmit` hook
- 打印当前工作目录日志
- 把这句话加入 `history`

此时消息历史新增：

```python
{"role": "user", "content": "我需要做一次代码审查"}
```

---

### 3. 第一次调用模型

进入 `agent_loop(history)` 后，程序会把下面这些内容发给模型：

- `SYSTEM`
- `history`
- `TOOLS`

也就是模型现在能同时看到：

- 自己是一个 coding agent
- 当前有哪些技能可用
- 用户说“我需要做一次代码审查”
- 自己可以调用哪些工具，包括 `load_skill`

---

### 4. 模型决定先加载 code-review skill

因为 `SYSTEM` 中已经列出了 `code-review`，模型会推断：

- 当前任务和代码审查高度相关
- 只知道技能名字还不够
- 需要先读完整 skill 内容

于是模型大概率不会直接回答，而是先发起工具调用：

```json
{
  "type": "tool_use",
  "name": "load_skill",
  "input": {
    "name": "code-review"
  }
}
```

这一步是 s07 的关键：先靠技能目录发现可用能力，再按需索取全文。

---

### 5. 主循环执行 load_skill

程序检测到模型要调用工具，于是：

- 先执行 `PreToolUse` hooks
- `log_hook` 打印 `[HOOK] load_skill`
- 然后从 `TOOL_HANDLERS` 找到 `load_skill`
- 执行 `load_skill(name="code-review")`

如果找到了，就返回：

- `code-review/SKILL.md` 的完整文本

如果找不到，就返回：

```text
Skill not found: code-review
```

---

### 6. 工具结果注入消息历史

程序会把技能全文包装为 `tool_result`，再追加进消息历史。

于是 `messages` 大致会变成：

1. `user`: 我需要做一次代码审查
2. `assistant`: 调用了 `load_skill`
3. `user`: 返回了 `code-review` 技能全文

注意：

- 技能全文不是写回 `SYSTEM`
- 而是作为当前会话中的工具结果进入上下文

这是 README 里强调的设计点。

---

### 7. 第二次调用模型

主循环继续进入下一轮模型调用。

这时模型已经拥有：

- 原始用户需求
- 技能目录
- `code-review` 的完整说明

所以它接下来就能根据 skill 的要求决定下一步。

通常有两种情况：

- 如果没有具体代码文件，它可能先回复用户，请用户提供文件
- 如果上下文里已经有代码或文件路径，它可能继续调用 `read_file`、`glob` 等工具开始审查

---

### 8. 如果用户没给代码，模型可能这样回答

一种合理情况是，模型读完 `code-review` skill 后，发现代码审查必须先看到目标代码。

这时它可能直接输出类似意思：

```text
我会按 code-review skill 的标准来做审查。请提供要审查的文件或代码片段。
```

这时 `response.stop_reason != "tool_use"`，主循环结束，本次回答输出到终端。

---

### 9. 如果用户已经给了文件，流程会更长

如果上下文里已经有待审查文件，例如：

```text
请审查 src/app.py
```

那么在加载 `code-review` 技能之后，模型很可能继续：

1. 调用 `read_file(path="src/app.py")`
2. 程序返回文件内容
3. 模型结合：
   - 用户目标
   - code-review skill
   - 文件内容
4. 最终输出审查结果

所以 `load_skill` 只是中间一环，不是终点。

---

## 这一章最重要的理解

s07 的重点不是“新增了一个复杂工具”，而是：

- 把知识加载从“一次性全部注入”
- 改成“目录常驻 + 内容按需加载”

可以把它理解成：

- `SYSTEM` 只放导航页
- `load_skill` 才去打开具体章节

这能减少无关任务的上下文浪费，也让 agent 的行为更接近真实工作流。

---

## 一句话总结

对于 s07，可以记住这一句：

**模型先从 `SYSTEM` 里知道有哪些 skill，再通过 `load_skill` 把真正需要的 `SKILL.md` 全文加载进当前对话。**
