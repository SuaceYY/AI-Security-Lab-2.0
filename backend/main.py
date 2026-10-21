from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    from .local_ai import LocalAIError, analyze_findings
    from .scan_service import ScanValidationError, run_scan
except ImportError:
    from local_ai import LocalAIError, analyze_findings  # type: ignore[no-redef]
    from scan_service import ScanValidationError, run_scan  # type: ignore[no-redef]

MAX_SCAN_REQUEST_BODY_BYTES = 8 * 1024 * 1024
REQUEST_TOO_LARGE_MESSAGE = "请求体过大，最大允许 8 MiB。"
LIMITED_REQUEST_PATHS = frozenset({"/scans", "/analyses"})


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in LIMITED_REQUEST_PATHS
        ):
            await self.app(scope, receive, send)
            return

        if self._content_length_exceeds_limit(scope):
            await self._send_too_large(scope, receive, send)
            return

        body = bytearray()
        disconnected = False
        received_bytes = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue

            received_bytes += len(message.get("body", b""))
            if received_bytes > MAX_SCAN_REQUEST_BODY_BYTES:
                await self._send_too_large(scope, receive, send)
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        # ASGI transport fragments can be arbitrarily small. Retain at most
        # one body plus a disconnect, rather than one dictionary per fragment.
        messages: list[Message] = []
        if body or not disconnected:
            messages.append({
                "type": "http.request",
                "body": bytes(body),
                "more_body": disconnected,
            })
        if disconnected:
            messages.append({"type": "http.disconnect"})
        del body
        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _content_length_exceeds_limit(scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                return int(value) > MAX_SCAN_REQUEST_BODY_BYTES
            except ValueError:
                return False
        return False

    async def _send_too_large(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "status": "failed",
                    "code": "request_too_large",
                    "message": REQUEST_TOO_LARGE_MESSAGE,
                }
            },
        )
        await response(scope, receive, send)


app = FastAPI(title="AI-Security-Lab 2.0", version="0.1.0")
app.add_middleware(RequestBodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "status": "failed",
                "code": "invalid_request",
                "message": "请求格式不合法。",
                "errors": jsonable_encoder(exc.errors()),
            }
        },
    )


class ScanFileRequest(BaseModel):
    file_path: str
    content: str


class ScanRequest(BaseModel):
    files: list[ScanFileRequest]


class FindingModel(BaseModel):
    rule_id: str
    severity: str
    file_path: str
    line: int
    evidence: str
    explanation: str
    recommendation: str


class ScannedFileModel(BaseModel):
    file_path: str
    status: Literal["scanned", "skipped"]
    finding_count: int
    reason: str | None = None


class ScanSummaryModel(BaseModel):
    file_count: int
    scanned_count: int
    skipped_count: int
    finding_count: int


class ScanResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "done", "failed"]
    summary: ScanSummaryModel
    files: list[ScannedFileModel]
    findings: list[FindingModel]


class AnalysisRequest(BaseModel):
    findings: list[FindingModel]


class FindingPriorityResponse(BaseModel):
    finding_index: int
    priority: Literal["critical", "high", "medium", "low"]
    reason: str


class AnalysisResponse(BaseModel):
    status: Literal["done"]
    model: str
    total_count: int
    analyzed_count: int
    summary: str
    priorities: list[FindingPriorityResponse]


@app.get("/")
def home():
    return {"message": "AI-Security-Lab 2.0 API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scans", response_model=ScanResponse)
def create_scan(payload: ScanRequest):
    try:
        result = run_scan(
            (item.file_path, item.content) for item in payload.files
        )
    except ScanValidationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "failed",
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc

    return result.to_dict()


@app.post("/analyses", response_model=AnalysisResponse)
def create_analysis(payload: AnalysisRequest):
    try:
        result = analyze_findings(
            item.model_dump() for item in payload.findings
        )
    except LocalAIError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "failed",
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc

    return result.to_dict()
