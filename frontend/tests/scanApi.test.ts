import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildScanRequest,
  describeScanResult,
  formatScanError,
  formatTaskStatus,
  parseScanError,
  readAllowedFiles,
  ScanRequestError,
  submitScan,
  type ScanResponse,
} from '../src/scanApi.ts'

const VALID_RESPONSE: ScanResponse = {
  task_id: 'task-1',
  status: 'done',
  summary: {
    file_count: 1,
    scanned_count: 1,
    skipped_count: 0,
    finding_count: 1,
  },
  files: [
    {
      file_path: 'project/src/app.py',
      status: 'scanned',
      finding_count: 1,
      reason: null,
    },
  ],
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
}

function successfulFetch(payload: unknown): typeof fetch {
  return (async () => ({
    ok: true,
    status: 200,
    json: async () => payload,
  })) as unknown as typeof fetch
}

async function expectInvalidResponse(label: string, payload: unknown): Promise<void> {
  await assert.rejects(
    submitScan([], successfulFetch(payload)),
    (error: unknown) => {
      assert.ok(error instanceof ScanRequestError, label)
      assert.equal(error.status, 200, label)
      assert.equal(formatScanError(error), '扫描失败：服务端返回了无法识别的结果。', label)
      return true
    },
    label,
  )
}

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

test('接受结构完整的最小扫描响应', async () => {
  const result = await submitScan([], successfulFetch(VALID_RESPONSE))
  assert.deepEqual(result, VALID_RESPONSE)

  const emptyResult = await submitScan(
    [],
    successfulFetch({
      ...VALID_RESPONSE,
      summary: {
        file_count: 0,
        scanned_count: 0,
        skipped_count: 0,
        finding_count: 0,
      },
      files: [],
      findings: [],
    }),
  )
  assert.deepEqual(emptyResult.files, [])
  assert.deepEqual(emptyResult.findings, [])
})

test('拒绝畸形 HTTP 200 响应', async () => {
  const malformedResponses: Array<[string, unknown]> = [
    ['顶层为 null', null],
    ['顶层为数组', []],
    ['任务 ID 为空', { ...VALID_RESPONSE, task_id: '' }],
    ['任务 ID 为空白', { ...VALID_RESPONSE, task_id: '   ' }],
    ['任务状态非法', { ...VALID_RESPONSE, status: 'pending' }],
    ['summary 为空对象', { ...VALID_RESPONSE, summary: {} }],
    [
      'summary 计数为负数',
      { ...VALID_RESPONSE, summary: { ...VALID_RESPONSE.summary, file_count: -1 } },
    ],
    [
      'summary 计数非整数',
      { ...VALID_RESPONSE, summary: { ...VALID_RESPONSE.summary, scanned_count: 1.5 } },
    ],
    ['summary 为数组', { ...VALID_RESPONSE, summary: [] }],
    ['files 含 null', { ...VALID_RESPONSE, files: [null] }],
    [
      'files 状态非法',
      {
        ...VALID_RESPONSE,
        files: [{ ...VALID_RESPONSE.files[0], status: 'pending' }],
      },
    ],
    [
      'files 缺少 reason',
      {
        ...VALID_RESPONSE,
        files: [{ file_path: 'project/src/app.py', status: 'scanned', finding_count: 1 }],
      },
    ],
    ['findings 含 null', { ...VALID_RESPONSE, findings: [null] }],
    [
      'findings 严重度非法',
      {
        ...VALID_RESPONSE,
        findings: [{ ...VALID_RESPONSE.findings[0], severity: 'critical' }],
      },
    ],
    [
      'findings 行号为 0',
      {
        ...VALID_RESPONSE,
        findings: [{ ...VALID_RESPONSE.findings[0], line: 0 }],
      },
    ],
    [
      'findings 行号非整数',
      {
        ...VALID_RESPONSE,
        findings: [{ ...VALID_RESPONSE.findings[0], line: 1.5 }],
      },
    ],
    [
      'findings 缺少修复建议',
      {
        ...VALID_RESPONSE,
        findings: [
          {
            rule_id: 'PY-EVAL-EXEC',
            severity: 'high',
            file_path: 'project/src/app.py',
            line: 1,
            evidence: 'value = eval(user_input)',
            explanation: '检测到动态执行字符串的调用模式。',
          },
        ],
      },
    ],
  ]

  for (const [label, payload] of malformedResponses) {
    await expectInvalidResponse(label, payload)
  }
})
