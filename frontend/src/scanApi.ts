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

export interface TextFileLike {
  text(): Promise<string>
}

const TASK_STATUSES: readonly ScanResponse['status'][] = ['queued', 'running', 'done', 'failed']
const FILE_STATUSES: readonly ScannedFile['status'][] = ['scanned', 'skipped']
const SEVERITIES: readonly ScanSeverity[] = ['high', 'medium', 'low', 'info']

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
  invalid_file_path: '文件路径不合法，服务端已拒绝请求。',
  sensitive_file: '文件包含敏感路径或凭据，服务端已拒绝请求。',
  duplicate_file_path: '提交的文件路径重复，请重新选择。',
  invalid_file_content: '文件内容必须是文本。',
  invalid_text_encoding: '文件内容不是合法的 UTF-8 文本。',
  invalid_request: '请求格式不合法，请重新选择文件后重试。',
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

export function buildScanRequest(files: readonly ScanFileInput[]): ScanRequest {
  return {
    files: files.map(({ file_path, content }) => ({ file_path, content })),
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
