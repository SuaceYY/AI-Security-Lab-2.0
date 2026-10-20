export interface AuditFileLike {
  name: string
  size: number
  webkitRelativePath?: string
}

export interface AllowedFile<T extends AuditFileLike> {
  file: T
  path: string
}

export interface ExcludedFile {
  path: string
  reason: string
}

export interface FilterResult<T extends AuditFileLike> {
  total: number
  allowed: AllowedFile<T>[]
  excluded: ExcludedFile[]
}

export const MAX_FILE_SIZE_BYTES = 1024 * 1024

const ALLOWED_EXTENSIONS = new Set(['.py', '.js', '.ts', '.tsx', '.jsx'])

const EXCLUDED_DIRECTORIES = new Set([
  '.git',
  'node_modules',
  '.venv',
  'venv',
  'dist',
  'build',
  'out',
  'coverage',
  'htmlcov',
  '__pycache__',
  '.next',
  '.nuxt',
  '.svelte-kit',
  '.cache',
  '.pytest_cache',
  '.mypy_cache',
  '.ruff_cache',
  '.tox',
  '.turbo',
  '.parcel-cache',
  '.yarn',
  '.pnpm-store',
  'bower_components',
  '.idea',
  '.vscode',
])

const SENSITIVE_DIRECTORIES = new Set([
  '.ssh',
  '.aws',
  '.gnupg',
  '.docker',
  '.kube',
  '.azure',
])

const SENSITIVE_EXTENSIONS: Record<string, string> = {
  '.pem': '证书或私钥',
  '.key': '证书或私钥',
  '.p12': '证书或私钥',
  '.pfx': '证书或私钥',
  '.jks': '证书或私钥',
  '.keystore': '证书或私钥',
  '.ppk': '证书或私钥',
  '.kdbx': '证书或私钥',
  '.ovpn': '证书或私钥',
  '.crt': '证书或私钥',
  '.cer': '证书或私钥',
  '.der': '证书或私钥',
  '.csr': '证书或私钥',
  '.tfvars': '敏感配置',
  '.tfstate': '敏感配置',
  '.pgpass': '数据库凭据',
}

const SENSITIVE_FILE_NAMES = new Set([
  '.npmrc',
  '.pypirc',
  '.netrc',
  '_netrc',
  '.git-credentials',
  '.htpasswd',
  '.dockercfg',
  '.my.cnf',
  'credentials',
  'credentials.json',
  'credentials.yaml',
  'credentials.yml',
  'secrets',
  'secrets.json',
  'secrets.yaml',
  'secrets.yml',
  'secret.json',
  'secret.yaml',
  'secret.yml',
  'settings.xml',
  'auth.json',
  'token.json',
  'tokens.json',
])

function getRelativePath(file: AuditFileLike): string {
  return (file.webkitRelativePath || file.name).replace(/\\/g, '/')
}

function getExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf('.')
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : ''
}

function getSensitiveFileReason(fileName: string): string | null {
  const lowerName = fileName.toLowerCase()

  if (
    lowerName === '.env' ||
    lowerName === '.envrc' ||
    lowerName.startsWith('.env.') ||
    lowerName.endsWith('.env')
  ) {
    return '敏感文件：环境变量配置'
  }

  if (/^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$/.test(lowerName)) {
    return '敏感文件：私钥'
  }

  const extensionReason = SENSITIVE_EXTENSIONS[getExtension(lowerName)]
  if (extensionReason) {
    return `敏感文件：${extensionReason}`
  }

  if (SENSITIVE_FILE_NAMES.has(lowerName)) {
    return '敏感文件：凭据或敏感配置'
  }

  if (
    lowerName.endsWith('.secret') ||
    lowerName.endsWith('.secrets') ||
    /^service[-_]?account.*\.json$/.test(lowerName)
  ) {
    return '敏感文件：凭据或敏感配置'
  }

  return null
}

function getExclusionReason(file: AuditFileLike, relativePath: string): string | null {
  const segments = relativePath.split('/').filter(Boolean)
  const directorySegments = segments.slice(0, -1)
  const fileName = segments.at(-1) || file.name
  const lowerFileName = fileName.toLowerCase()

  for (const segment of directorySegments) {
    const lowerSegment = segment.toLowerCase()
    if (EXCLUDED_DIRECTORIES.has(lowerSegment)) {
      return `位于排除目录：${segment}`
    }
  }

  for (const segment of directorySegments) {
    const lowerSegment = segment.toLowerCase()
    if (SENSITIVE_DIRECTORIES.has(lowerSegment)) {
      return `位于敏感目录：${segment}`
    }
  }

  const sensitiveReason = getSensitiveFileReason(lowerFileName)
  if (sensitiveReason) {
    return sensitiveReason
  }

  const extension = getExtension(lowerFileName)
  if (!ALLOWED_EXTENSIONS.has(extension)) {
    return `不支持的文件类型：${extension || '无扩展名'}`
  }

  if (file.size > MAX_FILE_SIZE_BYTES) {
    return `文件过大：超过 1 MiB（${file.size} 字节）`
  }

  return null
}

export function filterFiles<T extends AuditFileLike>(files: readonly T[]): FilterResult<T> {
  const allowed: AllowedFile<T>[] = []
  const excluded: ExcludedFile[] = []

  for (const file of files) {
    const path = getRelativePath(file)
    const reason = getExclusionReason(file, path)

    if (reason) {
      excluded.push({ path, reason })
    } else {
      allowed.push({ file, path })
    }
  }

  return {
    total: files.length,
    allowed,
    excluded,
  }
}
