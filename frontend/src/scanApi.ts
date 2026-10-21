export interface ScanFileInput {
  file_path: string
  content: string
}

export interface ScanRequest {
  files: ScanFileInput[]
}

export type ScanSeverity = 'high' | 'medium' | 'low' | 'info'

export interface ScanFinding {
  rule_id: string
  severity: ScanSeverity
  file_path: string
  line: number
  evidence: string
  explanation: string
  recommendation: string
}

export interface ScannedFile {
  file_path: string
  status: 'scanned' | 'skipped'
  finding_count: number
  reason: string | null
}

export interface ScanSummary {
  file_count: number
  scanned_count: number
  skipped_count: number
  finding_count: number
}

export interface ScanResponse {
  task_id: string
  status: 'queued' | 'running' | 'done' | 'failed'
  summary: ScanSummary
  files: ScannedFile[]
  findings: ScanFinding[]
}

export interface AnalysisRequest {
  findings: ScanFinding[]
}

export type FindingPriorityLevel = 'critical' | 'high' | 'medium' | 'low'

export interface FindingPriority {
  finding_index: number
  priority: FindingPriorityLevel
  reason: string
}

export interface AnalysisResponse {
  status: 'done'
  model: string
  total_count: number
  analyzed_count: number
  summary: string
  priorities: FindingPriority[]
}

export interface TextFileLike {
  text(): Promise<string>
}

const TASK_STATUSES: readonly ScanResponse['status'][] = ['queued', 'running', 'done', 'failed']
const FILE_STATUSES: readonly ScannedFile['status'][] = ['scanned', 'skipped']
const SEVERITIES: readonly ScanSeverity[] = ['high', 'medium', 'low', 'info']
const FINDING_PRIORITIES: readonly FindingPriorityLevel[] = ['critical', 'high', 'medium', 'low']
export const MAX_ANALYSIS_FINDINGS = 50

const TASK_STATUS_LABELS: Record<ScanResponse['status'], string> = {
  queued: '排队中',
  running: '扫描中',
  done: '已完成',
  failed: '失败',
}

const SCAN_ERROR_MESSAGES: Record<string, string> = {
  empty_file_list: '没有可扫描的文件，请重新选择。',
  too_many_files: '文件数量超过服务端上限，请减少文件后重试。',
  file_too_large: '存在超过单文件大小上限的文件，请减少文件后重试。',
  total_size_too_large: '提交文本总量超过服务端上限，请减少文件后重试。',
  too_many_findings: '扫描结果数量超过服务端上限，请减少文件后重试。',
  scan_output_too_large: '扫描结果体积超过服务端上限，请减少文件后重试。',
  invalid_file_path: '文件路径不合法，服务端已拒绝请求。',
  sensitive_file: '文件包含敏感路径或凭据，服务端已拒绝请求。',
  duplicate_file_path: '提交的文件路径重复，请重新选择。',
  invalid_file_content: '文件内容必须是文本。',
  invalid_text_encoding: '文件内容不是合法的 UTF-8 文本。',
  invalid_request: '请求格式不合法，请重新选择文件后重试。',
}

const ANALYSIS_ERROR_MESSAGES: Record<string, string> = {
  invalid_model: '本地 AI 模型配置无效，请联系管理员检查模型配置。',
  model_endpoint_invalid: '本地 AI 服务地址配置无效，请联系管理员检查配置。',
  model_unavailable: '无法连接本地 AI 服务，请确认 Ollama 已启动后重试。',
  model_timeout: '本地 AI 分析超时，请稍后重试。',
  model_http_error: '本地 AI 服务返回异常状态，请稍后重试。',
  model_response_too_large: '本地 AI 返回内容过大，请稍后重试。',
  model_invalid_response: '本地 AI 返回了无效的分析结果，请稍后重试。',
  analysis_request_too_large: '提交的分析请求超过服务端上限，请减少 Finding 后重试。',
  request_too_large: '请求体超过服务端上限，请减少 Finding 后重试。',
  invalid_request: '请求格式不合法，请重新扫描后重试。',
  invalid_finding: 'Finding 格式不合法，请重新扫描后重试。',
}

export class ScanRequestError extends Error {
  status: number
  code?: string
  serverMessage?: string

  constructor(status: number, code?: string, serverMessage?: string) {
    super(serverMessage ?? `HTTP ${status}`)
    this.name = 'ScanRequestError'
    this.status = status
    this.code = code
    this.serverMessage = serverMessage
  }
}

export class AnalysisRequestError extends Error {
  status: number
  code?: string

  constructor(status: number, code?: string) {
    super(`分析请求失败（HTTP ${status}）`)
    this.name = 'AnalysisRequestError'
    this.status = status
    this.code = code
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isOneOf<T extends string>(value: unknown, options: readonly T[]): value is T {
  return typeof value === 'string' && (options as readonly string[]).includes(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isScanSummary(value: unknown): value is ScanSummary {
  return (
    isRecord(value) &&
    isNonNegativeSafeInteger(value.file_count) &&
    isNonNegativeSafeInteger(value.scanned_count) &&
    isNonNegativeSafeInteger(value.skipped_count) &&
    isNonNegativeSafeInteger(value.finding_count)
  )
}

function isScannedFile(value: unknown): value is ScannedFile {
  return (
    isRecord(value) &&
    typeof value.file_path === 'string' &&
    isOneOf(value.status, FILE_STATUSES) &&
    isNonNegativeSafeInteger(value.finding_count) &&
    isNullableString(value.reason)
  )
}

function isScanFinding(value: unknown): value is ScanFinding {
  return (
    isRecord(value) &&
    typeof value.rule_id === 'string' &&
    isOneOf(value.severity, SEVERITIES) &&
    typeof value.file_path === 'string' &&
    isPositiveSafeInteger(value.line) &&
    typeof value.evidence === 'string' &&
    typeof value.explanation === 'string' &&
    typeof value.recommendation === 'string'
  )
}

function isScanResponse(value: unknown): value is ScanResponse {
  return (
    isRecord(value) &&
    isNonEmptyString(value.task_id) &&
    isOneOf(value.status, TASK_STATUSES) &&
    isScanSummary(value.summary) &&
    Array.isArray(value.files) &&
    value.files.every(isScannedFile) &&
    Array.isArray(value.findings) &&
    value.findings.every(isScanFinding)
  )
}

function isFindingPriority(value: unknown): value is FindingPriority {
  return (
    isRecord(value) &&
    isNonNegativeSafeInteger(value.finding_index) &&
    isOneOf(value.priority, FINDING_PRIORITIES) &&
    isNonEmptyString(value.reason)
  )
}

function isAnalysisResponse(value: unknown, submittedCount: number): value is AnalysisResponse {
  if (
    !isRecord(value) ||
    value.status !== 'done' ||
    !isNonEmptyString(value.model) ||
    !isNonEmptyString(value.summary) ||
    !isNonNegativeSafeInteger(value.total_count) ||
    !isNonNegativeSafeInteger(value.analyzed_count) ||
    !Array.isArray(value.priorities)
  ) {
    return false
  }

  const totalCount = value.total_count
  const analyzedCount = value.analyzed_count

  if (
    totalCount !== submittedCount ||
    analyzedCount > MAX_ANALYSIS_FINDINGS ||
    analyzedCount > totalCount ||
    value.priorities.length !== analyzedCount
  ) {
    return false
  }

  const seenIndices = new Set<number>()
  for (const item of value.priorities) {
    if (
      !isFindingPriority(item) ||
      item.finding_index >= totalCount ||
      seenIndices.has(item.finding_index)
    ) {
      return false
    }
    seenIndices.add(item.finding_index)
  }

  return true
}

export function buildScanRequest(files: readonly ScanFileInput[]): ScanRequest {
  return {
    files: files.map(({ file_path, content }) => ({ file_path, content })),
  }
}

export function buildAnalysisRequest(findings: readonly ScanFinding[]): AnalysisRequest {
  return {
    findings: findings.map(
      ({ rule_id, severity, file_path, line, evidence, explanation, recommendation }) => ({
        rule_id,
        severity,
        file_path,
        line,
        evidence,
        explanation,
        recommendation,
      }),
    ),
  }
}

export async function readAllowedFiles<T extends TextFileLike>(
  allowed: readonly { file: T; path: string }[],
): Promise<ScanFileInput[]> {
  return Promise.all(
    allowed.map(async ({ file, path }) => ({
      file_path: path,
      content: await file.text(),
    })),
  )
}

export function parseScanError(status: number, payload: unknown): ScanRequestError {
  const detail = isRecord(payload) && isRecord(payload.detail) ? payload.detail : null
  const code = typeof detail?.code === 'string' ? detail.code : undefined
  const message = typeof detail?.message === 'string' ? detail.message : undefined

  return new ScanRequestError(status, code, message)
}

export function formatScanError(error: unknown): string {
  if (error instanceof ScanRequestError) {
    const mapped = error.code ? SCAN_ERROR_MESSAGES[error.code] : undefined
    if (mapped) {
      return mapped
    }

    if (error.status === 0) {
      return error.serverMessage
        ? `无法连接扫描服务：${error.serverMessage}`
        : '无法连接扫描服务。'
    }

    if (error.serverMessage) {
      return `扫描失败：${error.serverMessage}`
    }

    return `扫描失败（HTTP ${error.status}）。`
  }

  if (error instanceof Error) {
    return `扫描失败：${error.message}`
  }

  return '扫描失败：未知错误。'
}

export function parseAnalysisError(status: number, payload: unknown): AnalysisRequestError {
  const detail = isRecord(payload) && isRecord(payload.detail) ? payload.detail : null
  const code = typeof detail?.code === 'string' ? detail.code : undefined

  return new AnalysisRequestError(status, code)
}

export function formatAnalysisError(error: unknown): string {
  if (error instanceof AnalysisRequestError) {
    const mapped = error.code ? ANALYSIS_ERROR_MESSAGES[error.code] : undefined
    if (mapped) {
      return mapped
    }

    if (error.code?.startsWith('model_')) {
      return '本地 AI 分析失败，请确认本地模型服务可用后重试。'
    }

    if (error.status === 0) {
      return '无法连接分析服务，请确认后端服务已启动。'
    }

    if (error.status === 200) {
      return '分析失败：服务端返回了无法识别的分析结果。'
    }

    return `分析失败（HTTP ${error.status}）。`
  }

  if (error instanceof Error) {
    return `分析失败：${error.message}`
  }

  return '分析失败：未知错误。'
}

export function formatTaskStatus(status: ScanResponse['status']): string {
  return TASK_STATUS_LABELS[status]
}

export function describeScanResult(
  result: Pick<ScanResponse, 'status' | 'findings'>,
): string {
  if (result.status === 'failed') {
    return '扫描失败。'
  }

  if (result.findings.length === 0) {
    return '扫描完成，未发现匹配的静态规则。'
  }

  return `扫描完成，发现 ${result.findings.length} 个疑似问题。`
}

export async function submitScan(
  files: readonly ScanFileInput[],
  fetchImpl: typeof fetch = fetch,
): Promise<ScanResponse> {
  let response: Response

  try {
    response = await fetchImpl('/api/scans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildScanRequest(files)),
    })
  } catch (error) {
    const message = error instanceof Error ? error.message : '网络请求失败。'
    throw new ScanRequestError(0, undefined, message)
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw parseScanError(response.status, payload)
  }

  if (!isScanResponse(payload)) {
    throw new ScanRequestError(response.status, undefined, '服务端返回了无法识别的结果。')
  }

  return payload
}

export async function submitAnalysis(
  findings: readonly ScanFinding[],
  fetchImpl: typeof fetch = fetch,
): Promise<AnalysisResponse> {
  let response: Response

  try {
    response = await fetchImpl('/api/analyses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildAnalysisRequest(findings)),
    })
  } catch {
    throw new AnalysisRequestError(0)
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw parseAnalysisError(response.status, payload)
  }

  if (!isAnalysisResponse(payload, findings.length)) {
    throw new AnalysisRequestError(response.status)
  }

  return payload
}
