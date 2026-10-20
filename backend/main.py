from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from .scan_service import ScanValidationError, run_scan
except ImportError:
    from scan_service import ScanValidationError, run_scan  # type: ignore[no-redef]

app = FastAPI(title="AI-Security-Lab 2.0", version="0.1.0")


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
