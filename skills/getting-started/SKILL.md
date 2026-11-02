---
name: getting-started
description: 查看 workspace，记录用户偏好，并生成一份可验证的工作笔记。
---

# 工作笔记

当用户要求整理工作笔记时：

1. 用 `workspace_list` 查看已有文件。需要时用 `workspace_read` 读取用户指定的材料。
2. 用 `time_now` 获取当前时间。
3. 按“目标、已知事实、行动项、待确认事项”整理。
4. 用 `workspace_write` 保存用户指定的文件，默认不覆盖已有文件。
5. 回复保存路径，以及仍需用户确认的事项。

不要仅因笔记里出现个人信息就保存长期记忆。只有用户明确要求“记住”时才调用 `memory_remember`。
本指南不授予任何额外工具权限。
