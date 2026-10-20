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
python -m tools.dual_ai.web_batch --batch-id web-s1-b001 --check
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

## 已发布开发报告的受限恢复

报告传输使用独立 Base64 分块及 SHA-256 校验，保留可读 JSON 报告，避免 CLI 的终端控制字符转换破坏嵌套 JSON。解码不会执行报告中的命令，不会猜测修复无效转义。

只有主审已核对的以下暂停点可以恢复：原状态为 needs_human、本轮开发报告已发布且固定验证通过、GLM review_started 尚未登记、Issue/PR/提交/计划和原计数全部一致、两个会话空闲、工作区干净。恢复需显式 --resume-from-report 原开发报告评论ID --write；每批最多一次此类恢复，不重做已完成开发，不创建新 Issue/PR，不清零计数。若投递可能已发生或源码改变，则拒绝恢复并保留原状态。三轮上限不变。

本次 Issue #33 的恢复报告为 6298174688，主审修复并验证交接数据后，才可运行：
```powershell
python -m tools.dual_ai.web_batch --batch-id web-s1-b001 --resume-from-report 6298174688 --write
```


## 24KB 评论与小任务约束

每条新发布评论（含标题、标记、代码围栏与清单）按 UTF-8 实际字节限制为 24000，发布前检查。完整报告先保存本地，较大报告自动拆成可读分页；机器交接仍校验全部分块，兼容读取历史42KB分块。单份完整交接上限192000字节，超过则保留现场交由主审核对。拆分评论不计循环次数。

每次只派发一个最小可验收目标，尽量限定2到3个源码/测试文件。首批第3轮缩小为已过滤文件提交API并展示基础Finding及最小加载/错误反馈，JSON导出与复杂看板留待下一批评估；原大纲与批次计数不变。

固定后端验证优先探测项目已有 .venv、backend/.venv、venv、backend/venv 中的 Python，再检查当前 Python。只复用能够导入 FastAPI 的解释器，并在验证记录中保存解释器路径和版本。不自动安装依赖；若没有现成后端环境则停下询问。

## 第二轮未发布现场恢复

Issue #33 第2轮在系统 Python 缺 FastAPI、后续大报告发布超限后暂停。代码仍在本地 backend 四个文件中。必须先用 git stash push -u 备份已知代码改动，保留完整 stash SHA，再切换并更新工具分支；禁止 reset、clean、丢弃工作区或删除批次状态。

tools.dual_ai.recover_local --batch-id web-s1-b001 --stash <完整SHA> --write 仅接受：原状态为开发阶段报告超预算暂停、完整循环计数为当前轮减一、原 Issue/PR/分支/提交未变化、本轮未发布开发报告或投递GLM、备份基于原提交且不含越界/暂存改动、绑定会话和唯一已完成开发消息一致。

恢复时保留 stash，回到原分支恢复文件，重新运行固定验证；读取原 DeepSeek 消息而不重发开发提示，再将完整证据分段发布后交给 GLM。每批仅允许一次此类显式恢复；没有自动重试或清零计数。恢复失败保留备份和现场。


## 结果格式自恢复与原审查回复续接

普通结果格式问题先在本地解析：兼容粗体、行内标记、全角冒号、JSON代码块和纯JSON，保留原字段与结论；OpenCode不同text part之间保留换行。原回复先落盘。仍无法解析时，每轮每阶段最多向同一可见会话发送一次“仅整理格式”请求，plan模式且全部工具禁用，记录原文摘要、整理回复及次数，并核对仓库未改变。这不是重新执行开发/审查，不增加循环次数，也不重试投递状态不明的请求。多个冲突结果、明确needs_human、权限/费用/额度/连接/越界等问题仍停止。

对于旧版本已经因结果格式暂停、但GLM确实完成回复的情况，可以在更新后的工具分支运行：

```powershell
python -m tools.dual_ai.web_batch --batch-id web-s1-b001 --resume-completed-review --write
```

此入口只接受：原计数保留、本轮已发布开发报告且验证通过、唯一review_started、尚无review发布或repair_started、原Issue/PR/本地与远端提交和源码摘要一致、原绑定会话空闲、可唯一定位并GET原GLM消息及模型来源。每批最多一次。不会重新发送审查任务。将原意见发布GitHub后重新读取，交给DeepSeek完成当轮整改/核对，再按原三轮上限停止。无第四轮、自动合并或重置状态。

Issue #33 在提交4975de7a的第三轮开发与固定验证已通过；已完整完成两轮，第三轮旧GLM回复格式待恢复。只有最后整改报告发布及round_complete后才计满三轮，不能把屏幕出现pass当作批次完成。
