import { useEffect, useRef, useState } from 'react'
import { filterFiles, type FilterResult } from './fileFilter.ts'
import {
  describeScanResult,
  formatAnalysisError,
  formatScanError,
  formatTaskStatus,
  matchFindingPriorities,
  readAllowedFiles,
  submitAnalysis,
  submitScan,
  type AnalysisResponse,
  type ScanResponse,
} from './scanApi.ts'

type ScanState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'success'; result: ScanResponse }
  | { kind: 'error'; message: string }

type AnalysisState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'success'; result: AnalysisResponse }
  | { kind: 'error'; message: string }

function App() {
  const [status, setStatus] = useState('检查中...')
  const [result, setResult] = useState<FilterResult<File> | null>(null)
  const [scanState, setScanState] = useState<ScanState>({ kind: 'idle' })
  const [analysisState, setAnalysisState] = useState<AnalysisState>({ kind: 'idle' })
  const analysisRequestId = useRef(0)
  const analysisInFlight = useRef(false)

  useEffect(() => {
    fetch('/api/health')
      .then(response => response.json())
      .then(data => setStatus(data.status === 'ok' ? '后端连接正常' : '后端状态异常'))
      .catch(() => setStatus('后端连接失败'))
  }, [])

  const resetAnalysis = () => {
    analysisRequestId.current += 1
    analysisInFlight.current = false
    setAnalysisState({ kind: 'idle' })
  }

  const handleScan = async () => {
    resetAnalysis()

    if (!result || result.allowed.length === 0) {
      setScanState({ kind: 'error', message: '没有可提交的允许文件，请重新选择。' })
      return
    }

    setScanState({ kind: 'loading' })
    try {
      const files = await readAllowedFiles(result.allowed)
      const scanResult = await submitScan(files)
      setScanState({ kind: 'success', result: scanResult })
    } catch (error) {
      setScanState({ kind: 'error', message: formatScanError(error) })
    }
  }

  const handleAnalysis = async () => {
    if (
      scanState.kind !== 'success' ||
      scanState.result.findings.length === 0 ||
      analysisInFlight.current
    ) {
      return
    }

    const requestId = analysisRequestId.current + 1
    analysisRequestId.current = requestId
    analysisInFlight.current = true
    setAnalysisState({ kind: 'loading' })

    try {
      const analysisResult = await submitAnalysis(scanState.result.findings)
      if (analysisRequestId.current === requestId) {
        setAnalysisState({ kind: 'success', result: analysisResult })
      }
    } catch (error) {
      if (analysisRequestId.current === requestId) {
        setAnalysisState({ kind: 'error', message: formatAnalysisError(error) })
      }
    } finally {
      if (analysisRequestId.current === requestId) {
        analysisInFlight.current = false
      }
    }
  }

  const analysisMatches =
    scanState.kind === 'success' && analysisState.kind === 'success'
      ? matchFindingPriorities(scanState.result.findings, analysisState.result.priorities)
      : []

  return (
    <main style={{ padding: 40, maxWidth: 900, margin: 'auto' }}>
      <h1>AI-Security-Lab 2.0</h1>
      <p>本地 AI 代码安全审计平台</p>
      <p>系统状态：{status}</p>

      <hr />

      <h2>选择审计项目</h2>
      <input
        type="file"
        multiple
        disabled={scanState.kind === 'loading'}
        {...{ webkitdirectory: '' }}
        onChange={event => {
          const selected = Array.from(event.target.files || [])
          setResult(filterFiles(selected))
          setScanState({ kind: 'idle' })
          resetAnalysis()
        }}
      />

      {result && (
        <>
          <p>选择文件总数：{result.total}</p>
          <p>允许文件数量：{result.allowed.length}</p>
          <p>排除文件数量：{result.excluded.length}</p>

          <h3>允许文件</h3>
          <ul>
            {result.allowed.slice(0, 10).map(({ path }) => (
              <li key={path}>{path}</li>
            ))}
          </ul>

          {result.allowed.length > 10 && <p>仅显示前 10 个允许文件</p>}

          <h3>排除文件及原因</h3>
          {result.excluded.length === 0 ? (
            <p>无</p>
          ) : (
            <ul style={{ maxHeight: 320, overflowY: 'auto', textAlign: 'left' }}>
              {result.excluded.map(({ path, reason }) => (
                <li key={path}>
                  <code>{path}</code> - {reason}
                </li>
              ))}
            </ul>
          )}

          <p>
            允许文件只在点击“开始静态扫描”后以文本提交到本地 API；排除文件不会提交，也不会执行任何文件。
          </p>

          <button
            type="button"
            onClick={handleScan}
            disabled={scanState.kind === 'loading' || result.allowed.length === 0}
          >
            {scanState.kind === 'loading' ? '扫描中...' : '开始静态扫描'}
          </button>

          {result.allowed.length === 0 && <p>没有允许文件，无法提交扫描。</p>}

          {scanState.kind === 'loading' && <p>正在提交并扫描允许文件...</p>}

          {scanState.kind === 'error' && <p role="alert">错误：{scanState.message}</p>}

          {scanState.kind === 'success' && (
            <>
              <h3>扫描结果</h3>
              <p>
                任务状态：{formatTaskStatus(scanState.result.status)}（
                {scanState.result.status}）
              </p>
              <p>{describeScanResult(scanState.result)}</p>
              <p>
                扫描文件：{scanState.result.summary.scanned_count}，跳过：
                {scanState.result.summary.skipped_count}，问题数：
                {scanState.result.summary.finding_count}
              </p>

              {scanState.result.findings.length === 0 ? (
                <p>未发现匹配的静态规则。</p>
              ) : (
                <ul style={{ textAlign: 'left', paddingLeft: 20 }}>
                  {scanState.result.findings.map(finding => (
                    <li
                      key={`${finding.file_path}:${finding.line}:${finding.rule_id}`}
                      style={{ marginBottom: 16 }}
                    >
                      <div>
                        <code>
                          {finding.file_path}:{finding.line}
                        </code>{' '}
                        [{finding.severity}] {finding.rule_id}
                      </div>
                      <div>
                        证据：<code>{finding.evidence}</code>
                      </div>
                      <div>{finding.explanation}</div>
                      <div>建议：{finding.recommendation}</div>
                    </li>
                  ))}
                </ul>
              )}

              {scanState.result.findings.length > 0 && (
                <>
                  <p>
                    仅发送上述 Finding 的七个字段，不会再次发送完整源码；AI
                    结果仅供参考，需要人工复核。
                  </p>

                  <button
                    type="button"
                    onClick={handleAnalysis}
                    disabled={analysisState.kind === 'loading'}
                  >
                    {analysisState.kind === 'loading'
                      ? '本地 AI 分析中...'
                      : '使用本地 AI 分析优先级'}
                  </button>

                  {analysisState.kind === 'loading' && <p>正在请求本地 AI 分析...</p>}

                  {analysisState.kind === 'error' && (
                    <p role="alert">分析错误：{analysisState.message}</p>
                  )}

                  {analysisState.kind === 'success' && (
                    <>
                      <h3>本地 AI 分析结果</h3>
                      <p>本地模型：{analysisState.result.model}</p>
                      <p>分析摘要：{analysisState.result.summary}</p>
                      <p>
                        分析数量：{analysisState.result.analyzed_count} /{' '}
                        {analysisState.result.total_count}
                      </p>

                      {analysisState.result.analyzed_count <
                        scanState.result.findings.length && (
                        <p>仅展示模型实际分析的 Finding。</p>
                      )}

                      <ul style={{ textAlign: 'left', paddingLeft: 20 }}>
                        {analysisMatches.map(({ finding, priority }) => (
                          <li
                            key={`${priority.finding_index}:${finding.file_path}:${finding.line}:${finding.rule_id}`}
                            style={{ marginBottom: 12 }}
                          >
                            <div>
                              <code>
                                {finding.file_path}:{finding.line}
                              </code>{' '}
                              [{priority.priority}] {finding.rule_id}
                            </div>
                            <div>理由：{priority.reason}</div>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </>
              )}

              <details style={{ marginTop: 16, textAlign: 'left' }}>
                <summary>查看 JSON 结果</summary>
                <pre
                  style={{
                    maxHeight: 400,
                    overflow: 'auto',
                    padding: 12,
                    background: '#f4f4f4',
                    textAlign: 'left',
                  }}
                >
                  {JSON.stringify(scanState.result, null, 2)}
                </pre>
              </details>
            </>
          )}
        </>
      )}
    </main>
  )
}

export default App
