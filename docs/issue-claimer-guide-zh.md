# Issue Claimer 中文使用指南

本指南面向 Windows PowerShell 用户，说明如何在本仓库中使用本地 Issue Claimer。
该工具会轮询 GitHub 上带有 `ai-ready` 标签的 Issue，调用 OpenCode 完成修改，
执行固定验证矩阵，然后提交分支并创建 Pull Request。

> 仅用于处理可信 Issue，并且必须有人工监督。该工具不是操作系统级沙箱，不会把
> OpenCode 与机器上的其他文件隔离。它不支持 OpenCode 自动审批，也不支持自定义验证命令。

## 1. 前置条件

在仓库根目录执行以下检查：

```powershell
python --version
git --version
gh --version
gh auth status --hostname github.com
opencode --version
```

需要满足：

- Python 3.10 或更高版本。
- Git 可用，且当前目录是仓库根目录。
- GitHub CLI 已登录，并有权读取 Issue、推送分支和创建 PR。
- OpenCode 已安装并完成模型配置。
- `origin` 必须指向 `SuaceYY/AI-Security-Lab-2.0`，否则启动前的预检会拒绝运行。
- Git 工作区必须干净，不能有未提交或未跟踪的文件。

检查工作区：

```powershell
git status --porcelain=v1 --untracked-files=all
```

输出为空才表示干净。

## 2. 常用命令

以下命令均在仓库根目录运行。若系统使用 `py` 启动 Python，可将 `python` 替换为 `py`。

### `status`

查看运行状态、PID、状态文件位置和所有已记录任务：

```powershell
python tools/issue_claimer/claimer.py status
python tools/issue_claimer/claimer.py status --json
```

`--json` 适合脚本处理。状态数据来自 `data/issue_claimer/state.json`。

### `dry-run`

使用与后台 worker 完全相同的规则，检查下一个可领取的 Issue，但不会创建分支、
不会写入状态，也不会调用 AI：

```powershell
python tools/issue_claimer/claimer.py dry-run
```

输出中的 `claimable: yes` 表示当前有可领取任务；`claimable: no` 时请查看
`reason` 字段。退出码为 `0` 表示可领取，`2` 表示没有可领取任务。

### `start`

启动后台 worker。启动前会执行预检（`git`、`gh`、`opencode`、仓库 origin、
GitHub 登录）并再次检查工作区是否干净：

```powershell
python tools/issue_claimer/claimer.py start
```

启动成功后会输出 PID、日志文件和状态文件位置。worker 在后台运行，日志写入
`data/issue_claimer/claimer.log`，可使用以下命令持续查看：

```powershell
Get-Content data/issue_claimer/claimer.log -Wait
```

### `stop`

请求后台 worker 停止：

```powershell
python tools/issue_claimer/claimer.py stop
```

工具会先写入停止标记并等待约 30 秒；若仍未退出，会终止 worker 进程树。
如果当前没有运行中的实例，命令会直接返回。

### `worker`

在前台运行 worker，通常由 `start` 自动调用，不建议手动启动：

```powershell
python tools/issue_claimer/claimer.py worker
```

前台运行时可直接在终端查看日志；按 `Ctrl+C` 会中断当前任务并记录为需要人工恢复。

### `recover`

仅用于 `stage` 为 `recovery_required` 的任务，例如 worker 被杀掉或机器重启后：

```powershell
python tools/issue_claimer/claimer.py recover --issue 6 --action retry --confirm --note "已检查工作区"
python tools/issue_claimer/claimer.py recover --issue 6 --action close --confirm --note "放弃该任务"
```

- 不带 `--confirm` 时只打印恢复计划，不写入任何内容。
- `--action retry`：授权该 Issue 再被领取一次。worker 仍会先做非破坏性预检；
  工作区不干净、已有本地分支、远端分支或 PR 时会保留授权并停止。
- `--action close`：把任务标记为 `abandoned`，保留失败记录，之后不会再被领取。
- `--note` 会写入审计记录。

恢复操作不能删除或重置历史状态，也不能绕过预检。

## 3. 选择模型

通过环境变量让 OpenCode 使用指定模型：

```powershell
$env:ISSUE_CLAIMER_OPENCODE_MODEL = "opencode/deepseek-v4.1-flash-free"
python tools/issue_claimer/claimer.py start
```

环境变量只在当前 PowerShell 会话中有效。可在同一会话中先设置变量再执行
`dry-run` 或 `start`。其他可选变量：

| 变量 | 作用 |
| --- | --- |
| `ISSUE_CLAIMER_OPENCODE_AGENT` | 指定 OpenCode agent |
| `ISSUE_CLAIMER_POLL_SECONDS` | 轮询间隔，默认 300 秒 |
| `ISSUE_CLAIMER_TASK_TIMEOUT` | 单个任务总超时，默认 1800 秒 |
| `ISSUE_CLAIMER_VERIFY_TIMEOUT` | 验证超时，默认 900 秒 |
| `ISSUE_CLAIMER_STOP_WAIT` | `stop` 等待优雅退出的秒数，默认 30 秒 |
| `ISSUE_CLAIMER_BASE_BRANCH` | PR 目标分支，默认 `main` |
| `ISSUE_CLAIMER_REPO` | GitHub 仓库，默认 `SuaceYY/AI-Security-Lab-2.0` |
| `ISSUE_CLAIMER_DATA_DIR` | 运行数据目录，默认 `data/issue_claimer` |

## 4. 领取规则

worker 按以下规则选择任务：

1. 首次运行只允许领取 Issue #2。只有该任务进入终态后，才会解除首次运行限制。
2. Issue 必须处于打开状态，并带有 `ai-ready` 标签。
3. 本地状态中已记录过的 Issue 不会再次领取。
4. 已存在远端分支 `ai/issue-<编号>` 或对应 PR 时不会领取。
5. 首次运行完成后，按 Issue 编号从小到大扫描。

失败、超时或已关闭的任务不会自动重试；只有人工执行 `recover --action retry`
授权后才会重新领取。

## 5. 执行与验证

领取成功后，worker 会：

1. 从 `origin/main` 创建分支 `ai/issue-<编号>`。
2. 调用 OpenCode 修改仓库文件，最多尝试 2 次。
3. 运行固定验证矩阵：
   - Issue Claimer 单元测试；
   - 涉及 `frontend/` 时运行 `npm ci`、前端测试、构建和 lint；
   - 涉及 `.github/workflows/*.yml` 时做静态工作流检查。
4. 验证通过后，只暂存本次实际修改的文件，检查敏感路径和疑似密钥，然后提交。
5. 推送分支并创建 PR，标题形如 `[AI] Issue #<编号>: <标题>`。

验证失败时不会提交或推送。对于没有固定验证方案的文件路径，工具会直接失败并停止。

## 6. 中断与人工恢复

以下情况会被标记为 `failed`、`timed_out` 或 `recovery_required`：

- OpenCode 超时或被中断；
- 验证失败且重试后仍未通过；
- worker 异常退出，启动时发现残留的 `running` 任务；
- 上下文耗尽，需要人工继续会话。

恢复原则：

```powershell
# 查看任务状态和交接信息
python tools/issue_claimer/claimer.py status
Get-Content data/issue_claimer/handoff.md

# 检查工作区和分支
git status
git branch --show-current
git branch --list "ai/issue-*"

# 必要时人工继续被中断的 OpenCode 会话
opencode run --dir "<仓库根目录>" --continue
```

确认原因并清理冲突后，再使用 `recover`。不要删除 `state.json`、`status.json`
或 `handoff.md` 来强行重试，也不要重置或强制覆盖已有分支。

## 7. 运行数据

运行数据保存在 `data/issue_claimer/`，该目录已被 Git 忽略：

| 文件 | 说明 |
| --- | --- |
| `state.json` | 任务状态与恢复历史 |
| `status.json` | 当前任务的检查点 |
| `handoff.md` | 面向人工的交接摘要 |
| `claimer.log` | 轮转日志 |
| `claimer.pid` | 后台 worker PID |
| `claimer.lock` | 单实例锁 |
| `stop.flag` | 停止请求标记 |

不要手动编辑这些文件，也不要把它们提交到仓库。

## 8. 安全说明

- 只处理可信 Issue；Issue 内容和 PR 都需要人工审查。
- 该工具不是 OS 级沙箱，不能隔离 OpenCode 对整台机器的访问。
- 不启用 OpenCode 自动审批；命令使用 `--pure`，且不会添加 `--auto`。
- 不支持自定义验证命令，验证矩阵固定且失败时关闭（fail closed）。
- 拒绝修改受保护路径、敏感目录、符号链接和疑似包含密钥的文件。
- 调用 OpenCode 前会移除常见 token、密钥和凭据类环境变量。
- PR 创建后不会自动合并，必须由人工审核。
