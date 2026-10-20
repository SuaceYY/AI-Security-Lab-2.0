import { useEffect, useState } from 'react'
import { filterFiles, type FilterResult } from './fileFilter.ts'

function App() {
  const [status, setStatus] = useState('检查中...')
  const [result, setResult] = useState<FilterResult<File> | null>(null)

  useEffect(() => {
    fetch('/api/health')
      .then(response => response.json())
      .then(data => setStatus(data.status === 'ok' ? '后端连接正常' : '后端状态异常'))
      .catch(() => setStatus('后端连接失败'))
  }, [])

  return (
    <main style={{ padding: 40, maxWidth: 800, margin: 'auto' }}>
      <h1>AI-Security-Lab 2.0</h1>
      <p>本地 AI 代码安全审计平台</p>
      <p>系统状态：{status}</p>

      <hr />

      <h2>选择审计项目</h2>
      <input
        type="file"
        multiple
        {...{ webkitdirectory: '' }}
        onChange={event => {
          const selected = Array.from(event.target.files || [])
          setResult(filterFiles(selected))
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

          <p>文件过滤仅在浏览器本地进行，不会上传或执行任何文件。</p>
        </>
      )}
    </main>
  )
}

export default App
