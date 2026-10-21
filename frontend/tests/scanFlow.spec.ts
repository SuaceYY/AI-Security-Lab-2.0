import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { createServer, type ViteDevServer } from 'vite'

test.use({ channel: 'msedge' })

const FINDING_CONTENT = 'value = eval(user_input)\n'

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

test.afterAll(async () => {
  if (fixtureDir) {
    await rm(fixtureDir, { recursive: true, force: true })
  }
  await server?.close()
})

async function mockAPI(
  page: Page,
  filePath: string,
  captureRequest: (body: unknown) => void,
): Promise<void> {
  await page.route('**/api/health', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok' }),
    })
  })

  await page.route('**/api/scans', async route => {
    captureRequest(route.request().postDataJSON())
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
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
      }),
    })
  })
}

test('选择允许文件后完成真实浏览器扫描并展示 Finding', async ({ page }) => {
  fixtureDir = await mkdtemp(join(tmpdir(), 'ai-security-lab-e2e-'))
  await mkdir(join(fixtureDir, 'src'))
  await writeFile(join(fixtureDir, 'src', 'app.py'), FINDING_CONTENT, 'utf8')
  const filePath = `${basename(fixtureDir)}/src/app.py`

  let scanRequestBody: unknown

  await mockAPI(page, filePath, body => {
    scanRequestBody = body
  })
  await page.goto(baseURL)

  expect(await page.evaluate(() => navigator.userAgent)).toContain('Edg/')
  await expect(page.getByText('系统状态：后端连接正常')).toBeVisible()

  await page.locator('input[type="file"]').setInputFiles(fixtureDir)

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
