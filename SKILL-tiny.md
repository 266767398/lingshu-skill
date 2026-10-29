---
name: lingshu-tiny
title: 灵枢-tiny
version: 2.14
description: 灵枢记忆系统的最小试用版说明书。只剩三件事：startup 带回上次的记忆、remember 记下值得记的、recall 想不起来时捞回来。给第一次接触灵枢的人或最小模型：先跑通这三条拿到真实收益，再决定要不要升级完整版。当用户提到"记住这个""你还记得吗"且你不想背整套机制时使用。
---

# 灵枢 · 最小试用版

> 先看这一条：**必须先把命令的结果讲给用户，再说话。** 所有命令跑完会直接输出该讲的内容——你负责念出来，不要自己脑补。

## 就三条命令

```bash
# 1. 新会话第一轮必跑：把上次攒的记忆带回来
python scripts/brain.py startup --query "本轮主题"

# 2. 用户说了值得记的事，记下来
python scripts/brain.py remember "<原话>" --importance 6

# 3. 想不起来 / 用户问"你还记得吗"，捞回来
python scripts/brain.py recall "<关键词或一句话>"
```

- 跑 `startup` 后，**把它的输出原样放在回复最前面**。
- 用户心情不好的高点、喜欢 / 讨厌什么、定了什么约定——都值得 `remember`。
- 拿不准重要性就写 6。

## 试用够 20 分钟，觉得有用

去读 [SKILL-lite.md](SKILL-lite.md)（小模型精简版）或 [SKILL.md](SKILL.md)（完整版），补上工作记忆、做梦整理、人格初识那些能力。

## 注意

- 目录默认 `~/.brain-memory/`，只读降级时 `startup` / `recall` 仍可用。
- 这版是"感受一下",想要跨会话的人格、情绪、知识库，升级完整版再看。