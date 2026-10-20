# 网页开发：每批三个完整循环

本入口承接原网页大纲：基础结构 → FastAPI → React → 前后端通信 → 本地代码接入 → 文件过滤 → 静态扫描 → 任务状态与 JSON Findings。文件过滤已有实现及测试。后续仍按既定五阶段推进，不用 bootstrap 文档任务代替产品开发。

## 一个循环的精确定义

1. DeepSeek 在用户绑定的可见 OpenCode TUI 会话开发当前网页子步骤。
2. 调度器运行固定验证，把实际报告、命令输出、源码内容/提交和建议发到 GitHub Issue；验证通过才提交推送并创建/更新草稿 PR。
3. GLM 在自己的可见 TUI 会话读取从 GitHub 重新取得的报告、完整源码和验证结果，给出 pass / changes_requested / needs_evidence / needs_human 及开发意见。
4. 调度器把 GLM 意见发到同一 GitHub Issue，再从 GitHub 读取交给 DeepSeek 落实并发布整改结果。至此计一轮。

恰好完成三轮后调度进程退出，不自动第四轮，不合并，不推进下一批。普通缺陷在这些有限调用中整改，无无限重试。前序验证不通过时先修复，不盲目推进后续子步骤。三轮只表示三个流程完成，不表示产品验收通过；最终整改必须由主审助手/用户独立复核。

## 首批计划

tools/dual_ai/web_batch_plan.json 是本批已批准范围：静态规则与 Finding/反例测试 → FastAPI/任务状态/服务端输入校验 → React 提交与结果展示。保留现有脚本与过滤功能，不增加依赖，不执行被审查代码。

一个批次使用一条开发分支和一个草稿 PR，三个子步骤累计开发，批次结束后最终人工审核合并。模型无权改变 main、工具、Git 配置、依赖、工作流或预算。范围检查、秘密格式检查、分支与 HEAD 检查复用现有保护；这些检查不是操作系统沙箱。GLM 本次消息工具仅启用只读工具。

## 启动前

保留两个 OpenCode TUI 及其 serve/attach 服务。分别在旧 coordinator 和旧 claimer 的普通 PowerShell 中 Ctrl+C 停止旧调度，避免它们继续处理文档任务。本入口持有旧 Worker 锁，但不能替用户确认旧 coordinator 已停止。不要重新启动旧 --write 循环。

本入口读取现有 data/dual_ai/active_visible_sessions.json；不创建新模型会话或重新做探针任务。确保两个绑定会话空闲、无待回答问题，并且 Node.js/npm/Python/git/gh 已安装、GitHub 已登录、工作区干净。只有启动本入口的 PowerShell 运行一条命令；不要分别往两个 TUI 粘贴任务。

在已更新到本 PR 的工具分支、仓库根目录运行：

```powershell
python -m unittest discover -s tools/dual_ai -p "test_*.py" -v
python -m unittest discover -s tools/issue_claimer/tests -p "test_*.py" -v
python -m tools.dual_ai.web_batch --plan tools/dual_ai/web_batch_plan.json --batch-id web-s1-b001 --write
```

模型执行实时发生在这两个绑定 TUI；PowerShell 的结果输出仅作辅助核对。模型请求只投递一次，不后台另起模型，不自动批准权限。GitHub 写入由调度器执行，模型输出不能直接当 shell 命令执行。

计划路径在启动时读入内存；批次产品分支从 origin/main 创建，因此工具文件在运行期间可能不出现在该分支。干净完成后自动切回原工具分支；若暂停时有未提交修改则保留现场，不自动丢弃。

## 上下文、额度与人工暂停

每次调用核对上下文：优先使用最新输入及缓存 token，缺数据时明确使用估算，保守软预算 60000 token 并受模型实际 context 限额约束，预留输出空间。接近 75% 时先把计划、分支、提交、上一报告 ID 和工作文件列表写入 GitHub，再调用绑定会话的 summarize；不删除窗口或历史。压缩失败则暂停。供应商剩余额度无法由普通 token 统计精确推算；遇到额度/费用/模型错误就停，不切付费模型、不重试。

连接/权限/问题请求、缺凭据或运行环境、超时、投递不明确、上下文摘要失败、越界文件、来源不一致、证据超出有界预算，以及模型明确需要人工决定时，记录 needs_human 并退出。请看绑定 TUI 和 Issue 的问题；没有自动代答。Ctrl+C 会尝试中止当前绑定模型请求；若中止未确认，需在 TUI 手动停止。后台 HTTP 工作线程只是监控同一可见会话，不能当作模型已经停止的证明。

本地状态是 data/dual_ai/web_batches/<batch-id>.json。可读取：

```powershell
python -m tools.dual_ai.web_batch --batch-id web-s1-b001 --status
```

同 batch-id 不能重放，不能删状态“清零”。相同计划摘要也不能再次开启批次。人工异常恢复需要先核对现场与 GitHub 投递标记，再制定新的恢复计划，不能直接换 ID 重复执行。

## 三轮结束后

把 Issue/PR 链接或最终 PowerShell/TUI 截图发给主审助手。主审读取最终源码、最后一次整改后的真实验证及 GitHub 交接，说明当前进度、可行度、未完成项与下一步。得到人工合并批准后合并。下一批必须按已合并成果更新计划 revision/三项 focus 和新 batch-id，再给出明确启动代码；不自动在未合并成果上推进新批次。

首次实际运行前仍须验证用户本机 OpenCode 的 permission/question/summarize API 兼容性与可见会话行为；单元测试中的假客户端不是双窗口实测证据。
