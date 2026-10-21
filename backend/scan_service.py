"""扫描任务编排与服务端独立输入校验。

服务端不信任浏览器过滤结果：文件数量、相对路径、文本体积和敏感文件名
均在此重新校验。所有待扫描内容都来自请求体，本模块不读取本地路径，也不
执行被扫描文本。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Iterable

try:
    from .static_scan import (
        SUPPORTED_EXTENSIONS,
        Finding,
        FindingLimitExceeded,
        normalize_relative_path,
        scan_text,
    )
except ImportError:
    from static_scan import (  # type: ignore[no-redef]
        SUPPORTED_EXTENSIONS,
        Finding,
        FindingLimitExceeded,
        normalize_relative_path,
        scan_text,
    )

MAX_FILES = 100
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_FINDINGS = 1000

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "out",
        "coverage",
        "htmlcov",
        "__pycache__",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".turbo",
        ".parcel-cache",
        ".yarn",
        ".pnpm-store",
        "bower_components",
        ".idea",
        ".vscode",
    }
)

SENSITIVE_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".docker",
        ".kube",
        ".azure",
    }
)

SENSITIVE_EXTENSIONS = frozenset(
    {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".ppk",
        ".kdbx",
        ".ovpn",
        ".crt",
        ".cer",
        ".der",
        ".csr",
        ".tfvars",
        ".tfstate",
        ".pgpass",
    }
)

SENSITIVE_FILE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".git-credentials",
        ".htpasswd",
        ".dockercfg",
        ".my.cnf",
        "credentials",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secret.json",
        "secret.yaml",
        "secret.yml",
        "settings.xml",
        "auth.json",
        "token.json",
        "tokens.json",
    }
)

_PRIVATE_KEY_NAME = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$")
_SERVICE_ACCOUNT_NAME = re.compile(r"^service[-_]?account.*\.json$")


class ScanValidationError(ValueError):
    """表示服务端拒绝请求，并携带稳定的错误码与 HTTP 状态。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ScannedFile:
    file_path: str
    status: str
    finding_count: int
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "file_path": self.file_path,
            "status": self.status,
            "finding_count": self.finding_count,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScanSummary:
    file_count: int
    scanned_count: int
    skipped_count: int
    finding_count: int

    def to_dict(self) -> dict[str, int]:
        return {
            "file_count": self.file_count,
            "scanned_count": self.scanned_count,
            "skipped_count": self.skipped_count,
            "finding_count": self.finding_count,
        }


@dataclass(frozen=True)
class ScanResult:
    task_id: str
    status: str
    summary: ScanSummary
    files: tuple[ScannedFile, ...]
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _extension_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot_index = name.rfind(".")
    return name[dot_index:].lower() if dot_index >= 0 else ""


def _sensitive_reason(path: str) -> str | None:
    segments = path.split("/")
    file_name = segments[-1].lower()

    for segment in segments[:-1]:
        if segment.lower() in SENSITIVE_DIRECTORIES:
            return f"位于敏感目录：{segment}"

    if (
        file_name == ".env"
        or file_name == ".envrc"
        or file_name.startswith(".env.")
        or file_name.endswith(".env")
    ):
        return "敏感文件：环境变量配置"

    if _PRIVATE_KEY_NAME.fullmatch(file_name):
        return "敏感文件：私钥"

    if _extension_of(file_name) in SENSITIVE_EXTENSIONS:
        return "敏感文件：证书或私钥"

    if file_name in SENSITIVE_FILE_NAMES:
        return "敏感文件：凭据或敏感配置"

    if (
        file_name.endswith(".secret")
        or file_name.endswith(".secrets")
        or _SERVICE_ACCOUNT_NAME.fullmatch(file_name)
    ):
        return "敏感文件：凭据或敏感配置"

    return None


def _excluded_directory_reason(path: str) -> str | None:
    segments = path.split("/")
    for segment in segments[:-1]:
        if segment.lower() in EXCLUDED_DIRECTORIES:
            return f"位于排除目录：{segment}"
    return None


def _content_size(content: str) -> int:
    try:
        return len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ScanValidationError(
            "invalid_text_encoding",
            "文件内容不是合法的 UTF-8 文本。",
        ) from exc


def _prepare_files(
    items: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if not items:
        raise ScanValidationError("empty_file_list", "至少需要提交一个文件。")

    if len(items) > MAX_FILES:
        raise ScanValidationError(
            "too_many_files",
            f"提交文件数量超过上限 {MAX_FILES}。",
            status_code=413,
        )

    prepared: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    total_bytes = 0

    for file_path, content in items:
        if not isinstance(content, str):
            raise ScanValidationError("invalid_file_content", "文件内容必须是文本。")

        try:
            normalized_path = normalize_relative_path(file_path)
        except (TypeError, ValueError) as exc:
            raise ScanValidationError(
                "invalid_file_path",
                f"文件路径不合法：{exc}",
            ) from exc

        if normalized_path in seen_paths:
            raise ScanValidationError(
                "duplicate_file_path",
                f"文件路径重复：{normalized_path}",
            )
        seen_paths.add(normalized_path)

        sensitive_reason = _sensitive_reason(normalized_path)
        if sensitive_reason:
            raise ScanValidationError(
                "sensitive_file",
                f"{sensitive_reason}：{normalized_path}",
            )

        size = _content_size(content)
        if size > MAX_FILE_BYTES:
            raise ScanValidationError(
                "file_too_large",
                f"文件超过单文件上限 {MAX_FILE_BYTES} 字节：{normalized_path}",
                status_code=413,
            )
        total_bytes += size
        prepared.append((normalized_path, content))

    if total_bytes > MAX_TOTAL_BYTES:
        raise ScanValidationError(
            "total_size_too_large",
            f"提交文本总量超过上限 {MAX_TOTAL_BYTES} 字节。",
            status_code=413,
        )

    return prepared


def run_scan(
    files: Iterable[tuple[str, str]],
    task_id: str | None = None,
) -> ScanResult:
    """校验并同步执行一次扫描。

    扫描在请求内完成，成功时任务状态为 done；校验失败由调用方转换为
    4xx 响应，并在响应中返回 status=failed。queued/running 为后续异步
    任务保留，不在本函数中伪造。
    """

    prepared = _prepare_files(list(files))
    file_results: list[ScannedFile] = []
    findings: list[Finding] = []

    for file_path, content in prepared:
        excluded_reason = _excluded_directory_reason(file_path)
        if excluded_reason:
            file_results.append(
                ScannedFile(
                    file_path=file_path,
                    status="skipped",
                    finding_count=0,
                    reason=excluded_reason,
                )
            )
            continue

        if _extension_of(file_path) not in SUPPORTED_EXTENSIONS:
            file_results.append(
                ScannedFile(
                    file_path=file_path,
                    status="skipped",
                    finding_count=0,
                    reason="不支持的文件类型",
                )
            )
            continue

        try:
            file_findings = scan_text(
                file_path,
                content,
                max_findings=MAX_FINDINGS - len(findings),
            )
        except FindingLimitExceeded as exc:
            raise ScanValidationError(
                "too_many_findings",
                f"扫描结果 Finding 数量超过上限 {MAX_FINDINGS}。",
                status_code=413,
            ) from exc
        findings.extend(file_findings)
        file_results.append(
            ScannedFile(
                file_path=file_path,
                status="scanned",
                finding_count=len(file_findings),
            )
        )

    scanned_count = sum(1 for item in file_results if item.status == "scanned")
    summary = ScanSummary(
        file_count=len(file_results),
        scanned_count=scanned_count,
        skipped_count=len(file_results) - scanned_count,
        finding_count=len(findings),
    )

    return ScanResult(
        task_id=task_id or uuid.uuid4().hex,
        status="done",
        summary=summary,
        files=tuple(file_results),
        findings=tuple(findings),
    )
