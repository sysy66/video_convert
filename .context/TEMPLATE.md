# Task Spec Template
# 使用方法：复制本文件为 TASK_ACTIVE.md，填写所有字段，状态改为 READY 后交给 Spoke。
# Usage: copy to TASK_ACTIVE.md, fill in all fields, set Status to READY before handing to Spoke.

---

## Meta
- Task-ID:   # 格式：v{版本}-{模块}-{关键词}，如 v1.7-vmaf-interrupt
- Branch:    # Spoke 工作分支，如 feat/v1.7-vmaf-interrupt
- Status:    # DRAFT | READY | IN_PROGRESS | BLOCKED | REVIEW | DONE
- Owner:     # Spoke 认领后填写
- Created:   # Hub 创建日期 YYYY-MM-DD
- Updated:   # 最后更新日期（谁改谁填）

---

## Context
<!--
一段话：为什么做这个任务，它解决什么问题，属于哪个里程碑。
包含：问题现象、影响范围、触发原因。
-->

## Goal
<!--
一句话：做完后系统行为有什么具体变化（面向用户/运维可观测的变化）。
-->

---

## Current State

### 问题代码定位
<!--
文件路径:行号 — 关键代码片段（≤20行）— 一句话说明为何有问题
-->

### 相关现有实现（可复用）
<!--
Spoke 应优先复用而非重新发明的函数/模式。
格式：`文件路径:函数名` — 一句话说明用途
-->

### 数据流 / 调用链
<!--
Spoke 需要理解的最短调用路径，用箭头表示。
-->

---

## Target State

### 方案描述
<!--
推荐实现方案（伪代码或文字描述）。Hub 已评估可行性，Spoke 直接实现。
-->

### 明确禁止（Out of Scope）
<!--
不能动的文件/接口/行为，防止 Spoke 过度修改。
格式：❌ 禁止 XXX，原因：YYY
-->

### 接受标准（Acceptance Criteria）
<!--
可机器验证的条件，每条一个 checkbox。
-->
- [ ] AC1:
- [ ] AC2:
- [ ] AC3:

---

## Execution Protocol

### 必读文件清单（按顺序）
<!--
Spoke 开工前必须读的文件，Hub 已筛选，避免无效探索。
格式：N. `path/to/file.py` — 关注哪个函数/区块
-->
1.
2.
3.

### 验收命令
<!--
精确命令，Spoke 执行后把输出贴在"Spoke 输出"区。
-->
```bash
# 命令填写在此
```

**Spoke 输出：**
```
（执行后在此粘贴输出）
```

### 交付物清单
- [ ] 修改的文件（列出路径）
- [ ] 新增的测试文件（列出路径）
- [ ] 更新的文档（如有）
- [ ] TASK_ACTIVE.md Status 改为 REVIEW

### Blockers（Spoke 专属写入区）
<!--
遇到阻碍在此描述，Hub 收到后更新规范或给出解答。
格式：[YYYY-MM-DD] 问题描述
Hub 回复：...
-->
