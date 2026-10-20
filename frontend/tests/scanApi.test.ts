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

function failedFetch(status: number, payload: unknown): typeof fetch {
  return (async () => ({
    ok: false,
    status,
    json: async () => payload,
  })) as unknown as typeof fetch
}

function withoutField<T extends object>(value: T, field: keyof T): unknown {
  const copy: Partial<T> = { ...value }
  delete copy[field]
  return copy
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
    [
      'files 缺少 file_path',
      { ...VALID_RESPONSE, files: [withoutField(VALID_RESPONSE.files[0], 'file_path')] },
    ],
    [
      'files 缺少 finding_count',
      { ...VALID_RESPONSE, files: [withoutField(VALID_RESPONSE.files[0], 'finding_count')] },
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
      'findings 缺少 rule_id',
      { ...VALID_RESPONSE, findings: [withoutField(VALID_RESPONSE.findings[0], 'rule_id')] },
    ],
    [
      'findings 缺少 file_path',
      { ...VALID_RESPONSE, findings: [withoutField(VALID_RESPONSE.findings[0], 'file_path')] },
    ],
    [
      'findings 缺少 evidence',
      { ...VALID_RESPONSE, findings: [withoutField(VALID_RESPONSE.findings[0], 'evidence')] },
    ],
    [
      'findings 缺少 explanation',
      { ...VALID_RESPONSE, findings: [withoutField(VALID_RESPONSE.findings[0], 'explanation')] },
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

test('畸形 HTTP 200 不进入成功状态且错误提示可读', async () => {
  let successResult: ScanResponse | null = null
  let errorMessage: string | null = null

  try {
    successResult = await submitScan(
      [],
      successfulFetch({ ...VALID_RESPONSE, findings: [null] }),
    )
  } catch (error) {
    errorMessage = formatScanError(error)
  }

  const renderedFindings = successResult?.findings.map(finding => finding.rule_id) ?? null

  assert.equal(successResult, null)
  assert.equal(renderedFindings, null)
  assert.equal(errorMessage, '扫描失败：服务端返回了无法识别的结果。')
})

test('保留失败 HTTP 与网络异常的既有行为', async () => {
  const httpFailures: Array<[number, unknown, string | undefined, string]> = [
    [
      400,
      {
        detail: {
          status: 'failed',
          code: 'sensitive_file',
          message: '敏感文件：环境变量配置：.env',
        },
      },
      'sensitive_file',
      '文件包含敏感路径或凭据，服务端已拒绝请求。',
    ],
    [
      422,
      {
        detail: {
          status: 'failed',
          code: 'invalid_request',
          message: '请求格式不合法。',
        },
      },
      'invalid_request',
      '请求格式不合法，请重新选择文件后重试。',
    ],
    [500, null, undefined, '扫描失败（HTTP 500）。'],
  ]

  for (const [status, payload, code, message] of httpFailures) {
    await assert.rejects(
      submitScan([], failedFetch(status, payload)),
      (error: unknown) => {
        assert.ok(error instanceof ScanRequestError)
        assert.equal(error.status, status)
        assert.equal(error.code, code)
        assert.equal(formatScanError(error), message)
        return true
      },
    )
  }

  await assert.rejects(
    submitScan([], (async () => {
      throw new Error('网络不可用。')
    }) as unknown as typeof fetch),
    (error: unknown) => {
      assert.ok(error instanceof ScanRequestError)
      assert.equal(error.status, 0)
      assert.equal(formatScanError(error), '无法连接扫描服务：网络不可用。')
      return true
    },
  )
})
