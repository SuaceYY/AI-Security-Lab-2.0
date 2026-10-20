# Issue Claimer 自动化 E2E 冒烟测试（第二轮）

## 测试目标与范围

本次测试用于验证 GitHub Issue → 本地 Issue Claimer Worker → OpenCode → 固定验证 → Git push → 自动创建 PR 的端到端链路是否正常工作。

测试范围仅限自动化流程本身，不涉及程序源码、配置文件、`.github/` 目录或 `tools/issue_claimer/` 的任何修改。本次测试只创建 PR，禁止自动合并。

## 端到端步骤说明

1. 在 GitHub 创建测试 Issue，并由本地 Issue Claimer Worker 领取。
2. Worker 调用 OpenCode 处理 Issue 要求，仅新增指定文档文件。
3. Worker 执行预设固定验证，检查文件范围、编码与内容。
4. 固定验证通过后，Worker 执行 Git push 并自动创建 PR。
5. PR 创建完成后等待人工审核，不进行自动合并。

## 验收标准

- 固定验证通过：Git diff 中只有新增的 `docs/automation-e2e-smoke-v2.md`，文档为 UTF-8 且非空，内容符合要求。
- PR 已创建：Worker 成功推送分支并创建 Pull Request。
- PR 未自动合并：PR 保持打开状态，等待人工审核。
- 固定验证失败时 Worker 立即停止，不提交 PR。
- 不绕过受保护路径检查，不重试历史失败 Issue #2/#6/#9。

## 安全说明

本文档仅用于自动化链路冒烟测试，不包含任何真实凭据、令牌、密钥或其他敏感信息。测试过程不执行漏洞扫描、外部攻击或任何未经授权的安全测试。
