import assert from 'node:assert/strict'
import test from 'node:test'

import {
  filterFiles,
  MAX_FILE_SIZE_BYTES,
  type AuditFileLike,
  type FilterResult,
} from '../src/fileFilter.ts'

function makeFile(relativePath: string, size = 100): AuditFileLike {
  const segments = relativePath.replace(/\\/g, '/').split('/').filter(Boolean)

  return {
    name: segments.at(-1) ?? relativePath,
    size,
    webkitRelativePath: relativePath,
  }
}

function getExcludedReason(result: FilterResult<AuditFileLike>, pathSuffix: string): string {
  const excluded = result.excluded.find(item => item.path.endsWith(pathSuffix))
  assert.ok(excluded, `expected ${pathSuffix} to be excluded`)
  return excluded.reason
}

test('允许受支持的代码文件类型', () => {
  const result = filterFiles([
    makeFile('project/src/app.ts'),
    makeFile('project/src/main.py'),
    makeFile('project/web/component.tsx'),
    makeFile('project/web/index.jsx'),
    makeFile('project/web/util.js'),
  ])

  assert.equal(result.total, 5)
  assert.equal(result.allowed.length, 5)
  assert.equal(result.excluded.length, 0)
})

test('排除不支持的文件类型', () => {
  const result = filterFiles([makeFile('project/README.md')])

  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, 'README.md'), /不支持的文件类型/)
})

test('排除超过 1 MiB 的文件', () => {
  const hugeFile = makeFile('project/generated/huge.ts', MAX_FILE_SIZE_BYTES + 1)
  const result = filterFiles([hugeFile])

  assert.equal(hugeFile.size, MAX_FILE_SIZE_BYTES + 1)
  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, 'generated/huge.ts'), /文件过大/)
})

test('允许恰好 1 MiB 的文件', () => {
  const exactFile = makeFile('project/generated/exact.ts', MAX_FILE_SIZE_BYTES)
  const result = filterFiles([exactFile])

  assert.equal(exactFile.size, MAX_FILE_SIZE_BYTES)
  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 0)
})

test('排除 node_modules、.venv、dist 目录中的文件', () => {
  const result = filterFiles([
    makeFile('project/node_modules/pkg/index.js'),
    makeFile('project/.venv/lib/module.py'),
    makeFile('project/dist/bundle.js'),
  ])

  assert.equal(result.allowed.length, 0)
  assert.equal(result.excluded.length, 3)
  assert.match(getExcludedReason(result, 'node_modules/pkg/index.js'), /位于排除目录：node_modules/)
  assert.match(getExcludedReason(result, '.venv/lib/module.py'), /位于排除目录：\.venv/)
  assert.match(getExcludedReason(result, 'dist/bundle.js'), /位于排除目录：dist/)
})

test('排除 .git 目录中的文件（包含受支持的扩展名）', () => {
  const result = filterFiles([makeFile('project/.git/hooks/index.ts', 120)])

  assert.equal(result.allowed.length, 0)
  assert.match(getExcludedReason(result, '.git/hooks/index.ts'), /位于排除目录：\.git/)
})

test('排除 .env、私钥、证书和常见凭据文件', () => {
  const result = filterFiles([
    makeFile('project/.env'),
    makeFile('project/certs/server.pem'),
    makeFile('project/keys/id_rsa'),
    makeFile('project/config/credentials.json'),
    makeFile('project/.npmrc'),
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
    makeFile('project\\node_modules\\pkg\\index.ts', 120),
    makeFile('project\\src\\app.ts', 120),
  ])

  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 1)
  assert.match(result.excluded[0].reason, /位于排除目录：node_modules/)
})

test('扩展名匹配不区分大小写', () => {
  const result = filterFiles([makeFile('project/APP.TS')])

  assert.equal(result.allowed.length, 1)
  assert.equal(result.excluded.length, 0)
})

test('总数、允许数、排除数保持一致', () => {
  const files = [
    makeFile('project/src/app.ts'),
    makeFile('project/src/main.py'),
    makeFile('project/web/component.tsx'),
    makeFile('project/web/index.jsx'),
    makeFile('project/web/util.js'),
    makeFile('project/generated/exact.ts', MAX_FILE_SIZE_BYTES),
    makeFile('project/README.md'),
    makeFile('project/node_modules/pkg/index.js'),
    makeFile('project/.venv/lib/module.py'),
    makeFile('project/dist/bundle.js'),
    makeFile('project/generated/huge.ts', MAX_FILE_SIZE_BYTES + 1),
    makeFile('project/.env'),
    makeFile('project/certs/server.pem'),
    makeFile('project/keys/id_rsa'),
    makeFile('project/config/credentials.json'),
    makeFile('project/.npmrc'),
  ]
  const result = filterFiles(files)

  assert.equal(result.total, files.length)
  assert.equal(result.total, 16)
  assert.equal(result.allowed.length, 6)
  assert.equal(result.excluded.length, 10)
  assert.equal(result.allowed.length + result.excluded.length, result.total)
})
