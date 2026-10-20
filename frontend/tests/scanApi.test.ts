import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildScanRequest,
  describeScanResult,
  formatScanError,
  formatTaskStatus,
  parseScanError,
  readAllowedFiles,
  type ScanResponse,
} from '../src/scanApi.ts'

test('构造扫描请求时保留路径与文本', () => {
  const files = [
    { file_path: 'project/src/app.py', content: 'value = eval(user_input)\n' },
    { file_path: 'project/web/view.tsx', content: '<div />\n' },
  ]
  const payload = buildScanRequest(files)

  assert.deepEqual(payload, { files })
  assert.notEqual(payload.files, files)
})

test('只读取传入的允许文件并保留相对路径', async () => {
  const read: string[] = []
  const makeFile = (name: string, content: string) => ({
    text: async () => {
      read.push(name)
      return content
    },
  })

  const inputs = await readAllowedFiles([
    {
      file: makeFile('app.py', 'value = eval(user_input)\n'),
      path: 'project/src/app.py',
    },
  ])

  assert.deepEqual(read, ['app.py'])
  assert.deepEqual(inputs, [
    { file_path: 'project/src/app.py', content: 'value = eval(user_input)\n' },
  ])
})

test('将服务端错误码映射为可读提示', () => {
  const sensitive = parseScanError(400, {
    detail: {
      status: 'failed',
      code: 'sensitive_file',
      message: '敏感文件：环境变量配置：.env',
    },
  })
  const invalidRequest = parseScanError(422, {
    detail: {
      status: 'failed',
      code: 'invalid_request',
      message: '请求格式不合法。',
    },
  })

  assert.equal(formatScanError(sensitive), '文件包含敏感路径或凭据，服务端已拒绝请求。')
  assert.equal(formatScanError(invalidRequest), '请求格式不合法，请重新选择文件后重试。')
  assert.match(formatScanError(parseScanError(500, null)), /HTTP 500/)
})

test('扫描空结果与任务状态可读', () => {
  const result: ScanResponse = {
    task_id: 'task-1',
    status: 'done',
    summary: {
      file_count: 1,
      scanned_count: 1,
      skipped_count: 0,
      finding_count: 0,
    },
    files: [],
    findings: [],
  }

  assert.equal(formatTaskStatus(result.status), '已完成')
  assert.equal(describeScanResult(result), '扫描完成，未发现匹配的静态规则。')

  assert.equal(
    describeScanResult({
      status: 'done',
      findings: [
        {
          rule_id: 'PY-EVAL-EXEC',
          severity: 'high',
          file_path: 'project/src/app.py',
          line: 1,
          evidence: 'value = eval(user_input)',
          explanation: '检测到动态执行字符串的调用模式。',
          recommendation: '避免使用 eval。',
        },
      ],
    }),
    '扫描完成，发现 1 个疑似问题。',
  )
})
