import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AnalysisRequestError,
  buildAnalysisRequest,
  buildScanRequest,
  describeScanResult,
  formatAnalysisError,
  formatScanError,
  formatTaskStatus,
  MAX_ANALYSIS_FINDINGS,
  parseAnalysisError,
  parseScanError,
  readAllowedFiles,
  ScanRequestError,
  submitAnalysis,
  submitScan,
  type AnalysisResponse,
  type ScanFinding,
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

const VALID_FINDING: ScanFinding = {
  rule_id: 'PY-EVAL-EXEC',
  severity: 'high',
  file_path: 'project/src/app.py',
  line: 1,
  evidence: 'value = eval(user_input)',
  explanation: '检测到动态执行字符串的调用模式。',
  recommendation: '避免使用 eval。',
}

const VALID_ANALYSIS_RESPONSE: AnalysisResponse = {
  status: 'done',
  model: 'qwen2.5:7b-instruct',
  total_count: 1,
  analyzed_count: 1,
  summary: '发现 1 个高优先级问题。',
  priorities: [
    {
      finding_index: 0,
      priority: 'high',
      reason: '动态执行用户输入可能导致代码注入。',
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

function capturingFetch(payload: unknown): {
  calls: Array<{ url: string; init?: RequestInit }>
  fetchImpl: typeof fetch
} {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init })
    return {
      ok: true,
      status: 200,
      json: async () => payload,
    }
  }) as typeof fetch

  return { calls, fetchImpl }
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

async function expectInvalidAnalysisResponse(
  label: string,
  payload: unknown,
  findings: readonly ScanFinding[] = [VALID_FINDING],
): Promise<void> {
  await assert.rejects(
    submitAnalysis(findings, successfulFetch(payload)),
    (error: unknown) => {
      assert.ok(error instanceof AnalysisRequestError, label)
      assert.equal(error.status, 200, label)
      assert.equal(
        formatAnalysisError(error),
        '分析失败：服务端返回了无法识别的分析结果。',
        label,
      )
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

test('构造分析请求只序列化七个 Finding 字段', () => {
  let contentRead = false
  const contaminated = {
    ...VALID_FINDING,
    get content() {
      contentRead = true
      return '不应发送的源码内容'
    },
    file: new File(['不应读取'], 'app.py'),
  } as unknown as ScanFinding

  const request = buildAnalysisRequest([contaminated])

  assert.deepEqual(request, { findings: [VALID_FINDING] })
  assert.equal(contentRead, false)
  assert.equal('content' in request.findings[0], false)
  assert.deepEqual(Object.keys(request.findings[0]).sort(), [
    'evidence',
    'explanation',
    'file_path',
    'line',
    'recommendation',
    'rule_id',
    'severity',
  ])
})

test('提交分析请求使用固定路径并返回严格校验的成功结果', async () => {
  const { calls, fetchImpl } = capturingFetch(VALID_ANALYSIS_RESPONSE)

  const result = await submitAnalysis([VALID_FINDING], fetchImpl)

  assert.deepEqual(result, VALID_ANALYSIS_RESPONSE)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/analyses')
  assert.equal(calls[0].init?.method, 'POST')
  assert.deepEqual(calls[0].init?.headers, { 'Content-Type': 'application/json' })
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    findings: [VALID_FINDING],
  })
})

test('拒绝畸形分析 HTTP 200 响应', async () => {
  const malformedResponses: Array<[string, unknown]> = [
    ['顶层为 null', null],
    ['顶层为数组', []],
    ['状态非法', { ...VALID_ANALYSIS_RESPONSE, status: 'queued' }],
    ['模型为空', { ...VALID_ANALYSIS_RESPONSE, model: '' }],
    ['模型为空白', { ...VALID_ANALYSIS_RESPONSE, model: '   ' }],
    ['摘要为空', { ...VALID_ANALYSIS_RESPONSE, summary: '' }],
    ['摘要为空白', { ...VALID_ANALYSIS_RESPONSE, summary: ' \n ' }],
    ['总数为负数', { ...VALID_ANALYSIS_RESPONSE, total_count: -1 }],
    ['总数非整数', { ...VALID_ANALYSIS_RESPONSE, total_count: 1.5 }],
    [
      '总数非安全整数',
      { ...VALID_ANALYSIS_RESPONSE, total_count: Number.MAX_SAFE_INTEGER + 1 },
    ],
    ['总数为字符串', { ...VALID_ANALYSIS_RESPONSE, total_count: '1' }],
    ['分析数为负数', { ...VALID_ANALYSIS_RESPONSE, analyzed_count: -1 }],
    ['分析数非整数', { ...VALID_ANALYSIS_RESPONSE, analyzed_count: 0.5 }],
    [
      '分析数非安全整数',
      { ...VALID_ANALYSIS_RESPONSE, analyzed_count: Number.MAX_SAFE_INTEGER + 1 },
    ],
    ['总数与实际提交不一致', { ...VALID_ANALYSIS_RESPONSE, total_count: 2 }],
    [
      '分析数大于总数',
      {
        ...VALID_ANALYSIS_RESPONSE,
        total_count: 1,
        analyzed_count: 2,
        priorities: [
          ...VALID_ANALYSIS_RESPONSE.priorities,
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], finding_index: 0 },
        ],
      },
    ],
    ['priorities 为空对象', { ...VALID_ANALYSIS_RESPONSE, priorities: {} }],
    ['priorities 数量与计数不符', { ...VALID_ANALYSIS_RESPONSE, priorities: [] }],
    [
      '优先级索引越界',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], finding_index: 1 },
        ],
      },
    ],
    [
      '优先级索引为负数',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], finding_index: -1 },
        ],
      },
    ],
    [
      '优先级索引非整数',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], finding_index: 0.5 },
        ],
      },
    ],
    [
      '优先级等级非法',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], priority: 'urgent' },
        ],
      },
    ],
    [
      '优先级理由为空',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [
          { ...VALID_ANALYSIS_RESPONSE.priorities[0], reason: '   ' },
        ],
      },
    ],
    [
      '缺少优先级索引',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [withoutField(VALID_ANALYSIS_RESPONSE.priorities[0], 'finding_index')],
      },
    ],
    [
      '缺少优先级理由',
      {
        ...VALID_ANALYSIS_RESPONSE,
        priorities: [withoutField(VALID_ANALYSIS_RESPONSE.priorities[0], 'reason')],
      },
    ],
  ]

  for (const [label, payload] of malformedResponses) {
    await expectInvalidAnalysisResponse(label, payload)
  }

  const duplicateIndex = {
    ...VALID_ANALYSIS_RESPONSE,
    total_count: 2,
    analyzed_count: 2,
    priorities: [
      VALID_ANALYSIS_RESPONSE.priorities[0],
      { ...VALID_ANALYSIS_RESPONSE.priorities[0] },
    ],
  }
  await expectInvalidAnalysisResponse(
    '优先级索引重复',
    duplicateIndex,
    [VALID_FINDING, VALID_FINDING],
  )

  await expectInvalidAnalysisResponse(
    '缺少分析状态',
    withoutField(VALID_ANALYSIS_RESPONSE, 'status'),
  )
  await expectInvalidAnalysisResponse(
    '缺少本地模型',
    withoutField(VALID_ANALYSIS_RESPONSE, 'model'),
  )
})

test('拒绝超过 50 条的分析计数', async () => {
  const findings = Array.from({ length: MAX_ANALYSIS_FINDINGS + 1 }, () => VALID_FINDING)
  const analyzed = findings.length
  const priorities = findings.map((_, index) => ({
    finding_index: index,
    priority: 'high',
    reason: `理由 ${index}`,
  }))

  await expectInvalidAnalysisResponse(
    '分析数超过上限',
    {
      ...VALID_ANALYSIS_RESPONSE,
      total_count: findings.length,
      analyzed_count: analyzed,
      priorities,
    },
    findings,
  )
})

test('分析错误码映射为中文且不回显服务端原文', () => {
  const cases: Array<[number, string, string]> = [
    [500, 'invalid_model', '本地 AI 模型配置无效，请联系管理员检查模型配置。'],
    [500, 'model_endpoint_invalid', '本地 AI 服务地址配置无效，请联系管理员检查配置。'],
    [503, 'model_unavailable', '无法连接本地 AI 服务，请确认 Ollama 已启动后重试。'],
    [504, 'model_timeout', '本地 AI 分析超时，请稍后重试。'],
    [502, 'model_http_error', '本地 AI 服务返回异常状态，请稍后重试。'],
    [502, 'model_response_too_large', '本地 AI 返回内容过大，请稍后重试。'],
    [502, 'model_invalid_response', '本地 AI 返回了无效的分析结果，请稍后重试。'],
    [413, 'analysis_request_too_large', '提交的分析请求超过服务端上限，请减少 Finding 后重试。'],
    [413, 'request_too_large', '请求体超过服务端上限，请减少 Finding 后重试。'],
    [422, 'invalid_request', '请求格式不合法，请重新扫描后重试。'],
    [400, 'invalid_finding', 'Finding 格式不合法，请重新扫描后重试。'],
    [502, 'model_future_error', '本地 AI 分析失败，请确认本地模型服务可用后重试。'],
    [500, 'mystery_error', '分析失败（HTTP 500）。'],
  ]

  for (const [status, code, message] of cases) {
    const error = parseAnalysisError(status, {
      detail: {
        status: 'failed',
        code,
        message: 'SERVER_SECRET_SHOULD_NOT_LEAK',
      },
    })

    assert.equal(error.status, status)
    assert.equal(error.code, code)
    assert.equal(formatAnalysisError(error), message)
    assert.doesNotMatch(formatAnalysisError(error), /SERVER_SECRET_SHOULD_NOT_LEAK/)
  }
})

test('分析网络异常与非法 JSON 均安全失败', async () => {
  await assert.rejects(
    submitAnalysis([VALID_FINDING], (async () => {
      throw new Error('SOCKET_SECRET_SHOULD_NOT_LEAK')
    }) as unknown as typeof fetch),
    (error: unknown) => {
      assert.ok(error instanceof AnalysisRequestError)
      assert.equal(error.status, 0)
      assert.equal(formatAnalysisError(error), '无法连接分析服务，请确认后端服务已启动。')
      return true
    },
  )

  const invalidJsonFetch = (async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new Error('不是 JSON')
    },
  })) as unknown as typeof fetch

  await assert.rejects(
    submitAnalysis([VALID_FINDING], invalidJsonFetch),
    (error: unknown) => {
      assert.ok(error instanceof AnalysisRequestError)
      assert.equal(error.status, 200)
      assert.equal(
        formatAnalysisError(error),
        '分析失败：服务端返回了无法识别的分析结果。',
      )
      return true
    },
  )
})
