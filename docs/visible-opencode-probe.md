# 原生双 TUI：第一步会话连接验证

当前事实：PR #16 已人工合并；旧后台 Worker 已停。两个 OpenCode 窗口自动交接尚未实现。
本工具只验证原生 TUI 与服务器会话的接入方式，不宣布双 AI 闭环完成。

## 本次验证范围

`tools/dual_ai/visible_probe.py --launch` 在 Windows 中：

1. 确认 41961、41962 两个本地端口空闲，启动两个属于本次验证的 OpenCode serve 后端。
2. 读取真实版本和项目路径，为两个角色各创建一个全新且不同的会话。
3. 打开两个真正的 OpenCode TUI，使用 `attach URL --dir PATH --session ID` 精确绑定。
4. 用 `POST /session/ID/message` 和 `noReply: true` 写入明确标为“连接验证”的上下文标记。
5. 从相同会话回读标记，检查没有 assistant 消息；将原始 session ID、message ID 和版本存入本地记录。

这里的后台进程只是与原生 TUI 连接的 OpenCode 服务端，不是先执行任务再回放日志的 Worker。
标记是测试输入，不是模拟 AI 回答。此工具不发送需要模型响应的提示、不执行开发任务、不访问 GitHub、不自动合并。
模型字段用于绑定角色配置；**没有验证模型推理、工具调用或交接**。

两个窗口都必须实际显示本次编号的各自标记。HTTP 回读成功仅证明服务器会话接入；
它不能证明窗口已渲染、前台持续可见或模型执行已经完成。因此记录始终保留
`visual_verification: pending` 和 `workflow_verified: false`。

## Windows 运行

先正常退出旧的两个空闲 OpenCode TUI。保留已停止的旧 Worker，不要重新启动 claimer 或 coordinator。
在开发分支已更新、包含本工具的仓库根目录运行一次：

```powershell
python tools/dual_ai/visible_probe.py --launch
```

本次只需要核对两个窗口中的 `连接验证/planner/编号` 和 `连接验证/executor/编号`，
以及启动终端的回读结果。可以截图反馈，或粘贴启动终端的输出；无需复制任何任务给模型。
若端口占用、版本接口不兼容、会话回读失败或任一进程退出，立即停止，不重发消息。
`Ctrl+C` 关闭本次创建的进程；默认十分钟后也会关闭。不会终止其他 Worker，
不会改写 `sessions.json`、协调器游标或既有任务状态。服务端创建的测试会话保留供核对。

## 验证后才能推进

核对真实可见会话后，下一项才是让现有调度器把一次必要任务发送到已绑定会话，
并以真实工具事件、测试结果、GitHub Issue/PR 和 GLM 审查证明实际交接。
仍须保留历史任务 #2/#6/#9/#10/#15 禁止重做、有限重试、人工合并闸门。
本工具没有接入现有协调器，因此不会触发旧路线图中已完成的文档任务。

## 验证依据与限制

- [OpenCode CLI：attach 与 session 参数](https://opencode.ai/docs/cli/)
- [OpenCode Server：会话、消息与健康接口](https://opencode.ai/docs/server/)
- [OpenCode SDK：noReply 上下文注入](https://opencode.ai/docs/sdk/)

自动测试采用模拟 HTTP 服务，只验证协议、参数、角色隔离与失败停止。
它们不是 Windows/OpenCode 的现场验证，也不是两个 AI 已经执行的证据。
