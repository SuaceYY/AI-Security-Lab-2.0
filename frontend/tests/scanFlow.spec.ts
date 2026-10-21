import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { createServer, type ViteDevServer } from 'vite'

test.use({ channel: 'msedge' })

const FINDING_CONTENT = 'value = eval(user_input)\n'
const MIXED_ALLOWED_CONTENT = 'const allowed = true\n'
const SENSITIVE_ENV_CONTENT = 'SECRET_TOKEN=e2e-sensitive-sentinel\n'
const UNSUPPORTED_CONTENT = 'e2e-unsupported-sentinel\n'

const SCAN_FAILURE_CASES = [
  {
    code: 'too_many_findings',
    serverMessage: '扫描结果 Finding 数量超过上限 1000。',
    visibleMessage: '扫描结果数量超过服务端上限，请减少文件后重试。',
  },
  {
    code: 'scan_output_too_large',
    serverMessage: '扫描响应超过上限 2097152 字节。',
    visibleMessage: '扫描结果体积超过服务端上限，请减少文件后重试。',
  },
] as const

let server: ViteDevServer | undefined
let fixtureDir: string | undefined
let baseURL = ''

test.beforeAll(async () => {
  server = await createServer({
    root: process.cwd(),
    logLevel: 'error',
    server: { host: '127.0.0.1', port: 0 },
  })
  await server.listen()

  const localURL = server.resolvedUrls?.local[0]
  if (!localURL) {
    throw new Error('Vite 开发服务器未提供本地访问地址')
  }

  baseURL = localURL.replace(/\/$/, '')
})

test.afterEach(async () => {
  if (fixtureDir) {
    await rm(fixtureDir, { recursive: true, force: true })
    fixtureDir = undefined
  }
})

test.afterAll(async () => {
  await server?.close()
})

async function createFixture(files: Record<string, string>): Promise<string> {
  fixtureDir = await mkdtemp(join(tmpdir(), 'ai-security-lab-e2e-'))
  for (const [relativePath, content] of Object.entries(files)) {
    const targetPath = join(fixtureDir, relativePath)
    await mkdir(dirname(targetPath), { recursive: true })
    await writeFile(targetPath, content, 'utf8')
  }
  return fixtureDir
}

async function mockHealth(page: Page): Promise<void> {
  await page.route('**/api/health', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok' }),
    })
  })
}

async function mockScanRoute(
  page: Page,
  respond: (body: unknown) => { status: number; body: unknown },
): Promise<void> {
  await page.route('**/api/scans', async route => {
    const reply = respond(route.request().postDataJSON())
    await route.fulfill({
      status: reply.status,
      contentType: 'application/json',
      body: JSON.stringify(reply.body),
    })
  })
}

async function mockAPI(
  page: Page,
  filePath: string,
  captureRequest: (body: unknown) => void,
): Promise<void> {
  await mockHealth(page)
  await mockScanRoute(page, body => {
    captureRequest(body)
    return {
      status: 200,
      body: {
        task_id: 'e2e-task-1',
        status: 'done',
        summary: {
          file_count: 1,
          scanned_count: 1,
          skipped_count: 0,
          finding_count: 1,
        },
        files: [
          {
            file_path: filePath,
            status: 'scanned',
            finding_count: 1,
            reason: null,
          },
        ],
        findings: [
          {
            rule_id: 'PY-EVAL-EXEC',
            severity: 'high',
            file_path: filePath,
            line: 1,
            evidence: 'value = eval(user_input)',
            explanation: '检测到动态执行字符串的调用模式。',
            recommendation: '避免使用 eval，改用显式解析。',
          },
        ],
      },
    }
  })
}

async function mockScanFailure(
  page: Page,
  code: string,
  message: string,
  captureRequest: (body: unknown) => void,
): Promise<void> {
  await mockHealth(page)
  await mockScanRoute(page, body => {
    captureRequest(body)
    return {
      status: 413,
      body: {
        detail: {
          status: 'failed',
          code,
          message,
        },
      },
    }
  })
}

test('选择允许文件后完成真实浏览器扫描并展示 Finding', async ({ page }) => {
  const fixture = await createFixture({
    'src/app.py': FINDING_CONTENT,
  })
  const filePath = `${basename(fixture)}/src/app.py`

  let scanRequestBody: unknown

  await mockAPI(page, filePath, body => {
    scanRequestBody = body
  })
  await page.goto(baseURL)

  expect(await page.evaluate(() => navigator.userAgent)).toContain('Edg/')
  await expect(page.getByText('系统状态：后端连接正常')).toBeVisible()

  await page.locator('input[type="file"]').setInputFiles(fixture)

  await expect(page.getByText('允许文件数量：1')).toBeVisible()
  await page.getByRole('button', { name: '开始静态扫描' }).click()

  await expect(page.getByText('任务状态：已完成（done）')).toBeVisible()
  await expect(page.getByText(/问题数：\s*1/)).toBeVisible()

  const finding = page.locator('li').filter({ hasText: 'PY-EVAL-EXEC' })
  await expect(finding).toHaveCount(1)
  await expect(finding).toContainText(`${filePath}:1`)
  await expect(finding).toContainText('证据：value = eval(user_input)')
  await expect(finding).toContainText('检测到动态执行字符串的调用模式。')
  await expect(finding).toContainText('建议：避免使用 eval，改用显式解析。')

  expect(scanRequestBody).toEqual({
    files: [{ file_path: filePath, content: FINDING_CONTENT }],
  })
})

test('混合选择仅提交允许文件并在页面显示排除原因', async ({ page }) => {
  const fixture = await createFixture({
    'src/app.ts': MIXED_ALLOWED_CONTENT,
    '.env': SENSITIVE_ENV_CONTENT,
    'notes.txt': UNSUPPORTED_CONTENT,
  })
  const fixtureName = basename(fixture)
  const allowedPath = `${fixtureName}/src/app.ts`
  const envPath = `${fixtureName}/.env`
  const unsupportedPath = `${fixtureName}/notes.txt`

  let scanRequestBody: unknown

  await mockAPI(page, allowedPath, body => {
    scanRequestBody = body
  })
  await page.goto(baseURL)

  await page.locator('input[type="file"]').setInputFiles(fixture)

  await expect(page.getByText('选择文件总数：3')).toBeVisible()
  await expect(page.getByText('允许文件数量：1')).toBeVisible()
  await expect(page.getByText('排除文件数量：2')).toBeVisible()

  const sensitiveItem = page.locator('li').filter({ hasText: envPath })
  await expect(sensitiveItem).toHaveCount(1)
  await expect(sensitiveItem).toContainText('敏感文件：环境变量配置')

  const unsupportedItem = page.locator('li').filter({ hasText: unsupportedPath })
  await expect(unsupportedItem).toHaveCount(1)
  await expect(unsupportedItem).toContainText('不支持的文件类型：.txt')

  await page.getByRole('button', { name: '开始静态扫描' }).click()

  await expect.poll(() => scanRequestBody).toEqual({
    files: [{ file_path: allowedPath, content: MIXED_ALLOWED_CONTENT }],
  })

  const serializedRequest = JSON.stringify(scanRequestBody)
  expect(serializedRequest).not.toContain(envPath)
  expect(serializedRequest).not.toContain(unsupportedPath)
  expect(serializedRequest).not.toContain('SECRET_TOKEN=e2e-sensitive-sentinel')
  expect(serializedRequest).not.toContain('e2e-unsupported-sentinel')
})

for (const failure of SCAN_FAILURE_CASES) {
  test(`413 ${failure.code} 显示可读错误且不展示成功结果`, async ({ page }) => {
    const fixture = await createFixture({
      'src/safe.py': 'print("safe")\n',
    })
    const filePath = `${basename(fixture)}/src/safe.py`

    let scanRequestBody: unknown

    await mockScanFailure(page, failure.code, failure.serverMessage, body => {
      scanRequestBody = body
    })
    await page.goto(baseURL)

    await page.locator('input[type="file"]').setInputFiles(fixture)
    await expect(page.getByText('允许文件数量：1')).toBeVisible()
    await page.getByRole('button', { name: '开始静态扫描' }).click()

    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    await expect(alert).toContainText(`错误：${failure.visibleMessage}`)

    await expect(page.getByRole('heading', { name: '扫描结果' })).toHaveCount(0)
    await expect(page.getByText('任务状态：已完成（done）')).toHaveCount(0)

    expect(scanRequestBody).toEqual({
      files: [{ file_path: filePath, content: 'print("safe")\n' }],
    })
  })
}
