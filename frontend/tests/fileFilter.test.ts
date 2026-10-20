import assert from 'node:assert/strict'
import { statSync } from 'node:fs'
import { basename, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  filterFiles,
  MAX_FILE_SIZE_BYTES,
  type AuditFileLike,
  type FilterResult,
} from '../src/fileFilter.ts'

const fixtureRoot = fileURLToPath(new URL('./fixtures/project/', import.meta.url))

const fixturePaths = [
  'src/app.ts',
  'src/main.py',
  'web/component.tsx',
  'web/index.jsx',
  'web/util.js',
  'README.md',
  'node_modules/pkg/index.js',
  '.venv/lib/module.py',
  'dist/bundle.js',
  'generated/exact.ts',
  'generated/huge.ts',
  '.env',
  'certs/server.pem',
  'keys/id_rsa',
  'config/credentials.json',
  '.npmrc',
]

function loadFixture(relativePath: string): AuditFileLike {
  const stats = statSync(join(fixtureRoot, relativePath))
  return {
    name: basename(relativePath),
    size: stats.size,
    webkitRelativePath: `project/${relativePath.replace(/\\/g, '/')}`,
  }
}

function filterFixtures(paths: readonly string[]): FilterResult<AuditFileLike> {
  return filterFiles(paths.map(loadFixture))
}

function getExcludedReason(result: FilterResult<AuditFileLike>, pathSuffix: string): string {
  const excluded = result.excluded.find(item => item.path.endsWith(pathSuffix))
  assert.ok(excluded, `expected ${pathSuffix} to be excluded`)
  return excluded.reason
}

test('允许受支持的代码文件类型', () => {
  const result = filterFixtures([
    'src/app.ts',
    'src/main.py',
    'web/component.tsx',
    'web/index.jsx',
    'web/util.js',
  ])

  assert.equal(result.total, 5)
  assert.equal(result.allowed.length, 5)
  assert.equal(result.excluded.length, 0)
})

test('排除不支持的文件类型', () => {
  const result = filterFixtures(['README.md'])

  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, 'README.md'), /不支持的文件类型/)
})

test('排除超过 1 MiB 的文件', () => {
  const result = filterFixtures(['generated/huge.ts'])
  const hugeFileSize = loadFixture('generated/huge.ts').size

  assert.ok(hugeFileSize > MAX_FILE_SIZE_BYTES)
  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, 'generated/huge.ts'), /文件过大/)
})

test('允许恰好 1 MiB 的文件', () => {
  const result = filterFixtures(['generated/exact.ts'])

  assert.equal(loadFixture('generated/exact.ts').size, MAX_FILE_SIZE_BYTES)
  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 0)
})

test('排除 node_modules、.venv、dist 目录中的文件', () => {
  const result = filterFixtures([
    'node_modules/pkg/index.js',
    '.venv/lib/module.py',
    'dist/bundle.js',
  ])

  assert.equal(result.allowed.length, 0)
  assert.equal(result.excluded.length, 3)
  assert.match(getExcludedReason(result, 'node_modules/pkg/index.js'), /位于排除目录：node_modules/)
  assert.match(getExcludedReason(result, '.venv/lib/module.py'), /位于排除目录：\.venv/)
  assert.match(getExcludedReason(result, 'dist/bundle.js'), /位于排除目录：dist/)
})

test('排除 .git 目录中的文件（包含受支持的扩展名）', () => {
  const result = filterFiles([
    {
      name: 'index.ts',
      size: 120,
      webkitRelativePath: 'project/.git/hooks/index.ts',
    },
  ])

  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, '.git/hooks/index.ts'), /位于排除目录：\.git/)
})

test('排除 .env、私钥、证书和常见凭据文件', () => {
  const result = filterFixtures([
    '.env',
    'certs/server.pem',
    'keys/id_rsa',
    'config/credentials.json',
    '.npmrc',
  ])

  assert.equal(result.allowed.length, 0)
  assert.equal(result.excluded.length, 5)
  assert.match(getExcludedReason(result, '.env'), /环境变量配置/)
  assert.match(getExcludedReason(result, 'certs/server.pem'), /证书或私钥/)
  assert.match(getExcludedReason(result, 'keys/id_rsa'), /私钥/)
  assert.match(getExcludedReason(result, 'config/credentials.json'), /凭据或敏感配置/)
  assert.match(getExcludedReason(result, '.npmrc'), /凭据或敏感配置/)
})

test('兼容 Windows 反斜杠相对路径', () => {
  const result = filterFiles([
    {
      name: 'index.ts',
      size: 120,
      webkitRelativePath: 'project\\node_modules\\pkg\\index.ts',
    },
    {
      name: 'app.ts',
      size: 120,
      webkitRelativePath: 'project\\src\\app.ts',
    },
  ])

  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 1)
  assert.match(result.excluded[0].reason, /位于排除目录：node_modules/)
})

test('扩展名匹配不区分大小写', () => {
  const result = filterFiles([
    {
      name: 'APP.TS',
      size: 100,
      webkitRelativePath: 'project/APP.TS',
    },
  ])

  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 0)
})

test('总数、允许数、排除数保持一致', () => {
  const result = filterFixtures(fixturePaths)

  assert.equal(result.total, fixturePaths.length)
  assert.equal(result.allowed.length, 6)
  assert.equal(result.excluded.length, 10)
  assert.equal(result.allowed.length + result.excluded.length, result.total)
})
