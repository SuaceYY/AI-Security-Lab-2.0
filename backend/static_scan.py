"""离线静态扫描规则与统一 Finding 字段。

本模块只对传入的文本做正则匹配，不读取文件系统、不执行被扫描代码，
也不发起网络请求。规则仅覆盖少量明确代码模式，不构成完整漏洞确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

SUPPORTED_EXTENSIONS = frozenset({".py", ".js", ".ts", ".tsx", ".jsx"})
SEVERITIES = ("high", "medium", "low", "info")
EVIDENCE_LIMIT = 200
FINDING_FIELDS = (
    "rule_id",
    "severity",
    "file_path",
    "line",
    "evidence",
    "explanation",
    "recommendation",
)

_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: str
    extensions: frozenset[str]
    pattern: re.Pattern[str]
    explanation: str
    recommendation: str


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    file_path: str
    line: int
    evidence: str
    explanation: str
    recommendation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "file_path": self.file_path,
            "line": self.line,
            "evidence": self.evidence,
            "explanation": self.explanation,
            "recommendation": self.recommendation,
        }


class FindingLimitExceeded(ValueError):
    """表示本次扫描发现的 Finding 数量超过调用方给定上限。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"本次调用最多允许 {limit} 条 Finding。")
        self.limit = limit


RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="PY-EVAL-EXEC",
        severity="high",
        extensions=frozenset({".py"}),
        pattern=re.compile(r"(?<![\w.])(eval|exec)\s*\("),
        explanation="检测到动态执行字符串的调用模式，可能执行不受信任的输入。",
        recommendation="避免对不受信任的数据使用 eval/exec，改用明确的解析或映射逻辑。",
    ),
    Rule(
        rule_id="PY-OS-SYSTEM",
        severity="high",
        extensions=frozenset({".py"}),
        pattern=re.compile(r"\bos\.system\s*\("),
        explanation="检测到通过系统 shell 执行命令的调用模式。",
        recommendation="改用参数列表形式的子进程调用，并对输入做严格校验。",
    ),
    Rule(
        rule_id="PY-SHELL-TRUE",
        severity="medium",
        extensions=frozenset({".py"}),
        pattern=re.compile(r"\bshell\s*=\s*True\b"),
        explanation="检测到以 shell 方式启动子进程的配置，命令拼接可能被注入。",
        recommendation="优先使用 shell=False，并将命令与参数分离为列表。",
    ),
    Rule(
        rule_id="JS-EVAL",
        severity="high",
        extensions=frozenset({".js", ".ts", ".tsx", ".jsx"}),
        pattern=re.compile(r"(?<![\w.])eval\s*\("),
        explanation="检测到动态执行字符串的调用模式，可能执行不受信任的输入。",
        recommendation="避免使用 eval，改用 JSON 解析或显式分支处理。",
    ),
    Rule(
        rule_id="JS-DANGEROUS-HTML",
        severity="high",
        extensions=frozenset({".js", ".ts", ".tsx", ".jsx"}),
        pattern=re.compile(r"dangerouslySetInnerHTML"),
        explanation="检测到直接注入 HTML 的 React 属性，可能引入跨站脚本风险。",
        recommendation="使用文本渲染或可信的净化处理，避免拼接未净化 HTML。",
    ),
    Rule(
        rule_id="JS-INNER-HTML",
        severity="medium",
        extensions=frozenset({".js", ".ts", ".tsx", ".jsx"}),
        pattern=re.compile(r"\.innerHTML\s*="),
        explanation="检测到直接写入 innerHTML 的赋值模式，可能引入跨站脚本风险。",
        recommendation="改用 textContent，或在写入前使用可信的净化处理。",
    ),
)

_SORTED_RULES = tuple(sorted(RULES, key=lambda rule: rule.rule_id))


def normalize_relative_path(path: str) -> str:
    """规范化相对路径，拒绝绝对路径、上跳路径与 NUL 字符。"""
    if not isinstance(path, str):
        raise TypeError("file_path 必须是字符串")
    if "\x00" in path:
        raise ValueError("file_path 不能包含 NUL 字符")

    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]

    if not normalized:
        raise ValueError("file_path 不能为空")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("file_path 必须是相对路径")

    segments = [segment for segment in normalized.split("/") if segment not in ("", ".")]
    if any(segment == ".." for segment in segments):
        raise ValueError("file_path 不能包含上跳路径")
    if not segments:
        raise ValueError("file_path 不能为空")

    return "/".join(segments)


def _extension_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot_index = name.rfind(".")
    return name[dot_index:].lower() if dot_index >= 0 else ""


def _is_comment_line(stripped: str) -> bool:
    return stripped.startswith(_COMMENT_PREFIXES)


def _make_evidence(line: str) -> str:
    evidence = line.strip()
    if len(evidence) > EVIDENCE_LIMIT:
        return evidence[: EVIDENCE_LIMIT - 3] + "..."
    return evidence


def iter_scan_text(
    file_path: str,
    text: str,
    *,
    max_findings: int | None = None,
) -> Iterator[Finding]:
    """逐个生成 Finding，顺序为行号升序、同一行按 rule_id 升序。

    只支持受支持的代码扩展名；其他扩展名不生成结果。扫描过程不读取
    外部文件，也不执行 text 中的任何内容。给定 max_findings 时，在发现
    下一条超限 Finding 前抛出 FindingLimitExceeded，不生成超限结果。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    if max_findings is not None and max_findings < 0:
        raise ValueError("max_findings 不能为负数")

    normalized_path = normalize_relative_path(file_path)
    extension = _extension_of(normalized_path)
    if extension not in SUPPORTED_EXTENSIONS:
        return

    seen: set[tuple[str, int]] = set()
    finding_count = 0

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or _is_comment_line(stripped):
            continue

        for rule in _SORTED_RULES:
            if extension not in rule.extensions:
                continue
            if not rule.pattern.search(line):
                continue

            key = (rule.rule_id, line_number)
            if key in seen:
                continue
            if max_findings is not None and finding_count >= max_findings:
                raise FindingLimitExceeded(max_findings)
            seen.add(key)
            finding_count += 1
            yield Finding(
                rule_id=rule.rule_id,
                severity=rule.severity,
                file_path=normalized_path,
                line=line_number,
                evidence=_make_evidence(line),
                explanation=rule.explanation,
                recommendation=rule.recommendation,
            )


def scan_text(
    file_path: str,
    text: str,
    *,
    max_findings: int | None = None,
) -> list[Finding]:
    """扫描单个文本，返回按行号和 rule_id 升序排列的 Finding 列表。"""
    return list(iter_scan_text(file_path, text, max_findings=max_findings))


def scan_files(files: Iterable[tuple[str, str]]) -> list[Finding]:
    """扫描多个 (相对路径, 文本) 对，按文件顺序拼接结果。"""
    findings: list[Finding] = []
    for file_path, text in files:
        findings.extend(scan_text(file_path, text))
    return findings
