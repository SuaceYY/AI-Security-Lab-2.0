# 双 AI 协调器用户指南

本指南面向使用本仓库双 AI 协调工具的用户，说明当前实现能做什么、不能做什么、如何执行安全的只读检查，以及为什么合并必须由人工完成。

本文只描述 `tools/dual_ai/` 中已经存在的实现，不把路线图中的计划能力写成现有功能。当前角色划分为 GLM（规划与审查）和 DeepSeek（执行），配置位于 `tools/dual_ai/model_config.json`。该配置要求两个角色使用不同模型、使用独立会话，并明确设置 `manual_merge_required: true`、`auto_merge: false` 和最多 3 轮审查。

## 1. 当前局限性

### 1.1 不是常驻的自动闭环

当前 `tools/dual_ai/` 是一组需要手动调用的脚本，不是后台守护进程。它没有实现自动创建 Issue、自动排期、自动修复和自动合并的完整循环。

- `dispatch_review.py` 是一次性调度：读取 GitHub 上打开且非草稿的 PR，最多处理一个待审查的 head。
- `review_pr.py` 只对指定 PR 做只读审查，不发布评论、不创建 Issue、不合并。
- `review_feedback.py` 只有在显式使用 `--post` 时才会写入一条 PR 评论；即使写入，也不会合并 PR。
- 审查结论不会自动触发 DeepSeek 修复任务；当前代码中没有自动创建修复任务或自动推进修复轮次的实现。

### 1.2 会话必须分离，且不会跨角色恢复

`live_runner.py` 仅用于手动调用，一次只运行一个角色。会话 ID 必须来自真实 OpenCode 事件流；GLM 与 DeepSeek 不得共用同一个 OpenCode 会话，也不能跨角色恢复会话。会话记录保存在本地 `data/dual_ai/sessions.json`。当模型配置发生变化时，现有会话不会被静默复用，而是要求显式重置。

### 1.3 审查依赖真实证据

审查输入来自 GitHub 的 PR 元数据、文件列表、实际 diff 和状态检查，而不是 PR 描述中的自述。为避免超范围或不可控的审查，以下情况会直接拒绝自动审查：

- PR 文件数超过 100 个；
- diff 超过 150000 个字符；
- 无法读取 PR 或 diff；
- 缺少 CI 或完整测试证据。

缺少证据时，裁决必须是 `needs_evidence`。`needs_evidence` 表示证据不足，不应被当作代码缺陷处理。

### 1.4 去重游标不等于批准

`dispatch_review.py` 使用 `data/dual_ai/review_cursor.json` 按 PR head SHA 去重。每个 head 只审查一次；审查失败不会推进游标；head 在读取过程中变化时会跳过。游标只表示“该 head 的审查运行已完成”，绝不表示 PR 已批准。

### 1.5 裁决是失败关闭的

OpenCode 进程成功退出不等于 PR 通过。`review_feedback.py` 要求输出恰好一条结构化裁决，且状态只能是 `needs_evidence`、`changes_requested` 或 `ready_for_human`。裁决缺失、重复、格式非法或理由不合法时，会失败关闭，并且不会写入 GitHub。

### 1.6 运行前提与安全边界

- 需要本地可用的 OpenCode、已认证的 GitHub CLI，以及 Python 运行环境。
- 模型 ID 必须来自实际的 OpenCode 模型列表；预检只能确认模型存在且不同，不能证明两个会话已经完全隔离，也不能证明模型输出可信。
- 该工具不是操作系统级沙箱，OpenCode 进程仍可能访问本机资源；只应在可信仓库和人工监督下使用。
- 历史失败 Issue #2、#6、#9 不属于本协调器的自动处理范围，不应被自动重试或自动改写。
- `data/dual_ai/` 是本地运行数据目录，其中的会话记录和游标不应提交到仓库。

## 2. 安全只读检查

只读检查的目标是确认模型、PR 证据和仓库状态，而不修改代码、不写入 GitHub、不合并 PR。建议在仓库根目录、工作区干净且仅处理可信仓库时进行。

本指南不提供可直接执行的命令行片段；请根据入口文件、参数和副作用说明，在本地以只读方式使用。

| 检查方式 | 读取内容 | 副作用 | 适用场景 |
| --- | --- | --- | --- |
| `preflight.py` 的模型预检，使用 `--list-models` 或同时传入 `--glm`、`--deepseek` | OpenCode 模型列表 | 不启动 AI、不创建会话、不写状态、不访问 GitHub | 确认模型存在且两个角色使用不同模型 |
| `dispatch_review.py` 的 `--dry-run` | GitHub 上打开且非草稿的 PR 列表 | 不调用 AI、不写游标、不写 GitHub | 确认下一个会被审查的 PR |
| `github_evidence.py` 的证据读取 | PR 元数据、文件列表、实际 diff、状态检查 | 只读 GitHub，不评论、不建 Issue、不合并 | 人工核对审查依据 |
| `review_pr.py` 的只读审查 | 真实 PR 证据 | 调用 GLM 模型，并可能在本地记录会话；不写 GitHub | 预览独立审查结果 |
| `review_feedback.py` 不带 `--post` | 真实 PR 证据 | 调用 GLM 模型，并可能在本地记录会话；不写 GitHub | 预览结构化裁决和拟发布评论 |
| Git 的只读状态查询（状态、变更文件列表、分支列表） | 本地工作区信息 | 只读，不改变文件和历史 | 确认改动范围和当前分支 |

补充说明：

- 模型预检在模型缺失、命令失败或两个模型相同时会返回失败；它不会自动安装模型或修改配置。
- `dispatch_review.py` 的 `--dry-run` 只报告待审查的 PR，不获取 diff，也不调用模型。
- `review_pr.py` 明确只读：目标是让审查过程可见，但不发布、不合并、不创建后续 Issue。
- `review_feedback.py` 默认只预览；只有显式添加 `--post` 才会评论。写入前会重新获取 PR，如果 head 或状态已变化，会取消写入以避免发布过期裁决。
- 证据读取有文件数和 diff 大小上限；超过上限时应由人工检查，而不是绕过限制。
- 如果只读检查失败，应停止并检查原因，不要通过写入、强制推送、删除状态文件或添加 `--post` 来“修复”。

## 3. 人工合并闸门

### 3.1 三种裁决的含义

- `needs_evidence`：缺少固定验证、CI 或其他必要证据，当前无法确认通过。
- `changes_requested`：发现问题，需要修改后重新审查。
- `ready_for_human`：仅表示可以提交人工审查，绝不表示已批准或可以自动合并。

### 3.2 人工必须完成的检查

在合并前，人工审查者应至少确认：

1. PR 只修改了任务允许的文件范围，没有无关改动。
2. 实际 diff 与 Issue 要求一致，没有隐藏的越权修改。
3. 固定验证和 CI 证据充分，失败项已得到解释或修复。
4. 没有提交凭据、密钥、令牌或其他敏感信息。
5. 文档表述与仓库现有实现一致，没有把计划能力写成现有功能。
6. 审查针对的 head SHA 仍是当前 PR 的 head；如果 head 已变化，应重新审查。
7. 不使用强制推送覆盖历史，不删除本地状态来绕过闸门。

### 3.3 合并规则

- AI 审查意见仅为建议，不能替代人工批准。
- 任何情况下都不自动合并、不自动批准、不自动创建后续依赖任务。
- PR 创建后应保持打开，等待人工审核和人工合并。
- 只有人工确认并完成合并后，才应考虑依赖该 PR 的后续工作。
- 如果 PR 在审查后发生了新的提交，之前的审查结论不再适用于新的 head，必须重新执行只读审查。

## 4. 事实来源

本指南依据以下现有文件编写：

- `tools/dual_ai/model_config.json`
- `tools/dual_ai/preflight.py`
- `tools/dual_ai/live_runner.py`
- `tools/dual_ai/session_registry.py`
- `tools/dual_ai/github_evidence.py`
- `tools/dual_ai/review_pr.py`
- `tools/dual_ai/dispatch_review.py`
- `tools/dual_ai/review_feedback.py`
- `tools/dual_ai/opencode_commands.py`

如需扩展为自动修复或自动排期，应先通过独立评审的代码变更实现，并继续保留人工合并闸门。
