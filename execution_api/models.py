"""Pydantic request/response models for the Execution API."""

from datetime import datetime

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    execution_timeout: int | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    created_at: datetime


class ExecuteRequest(BaseModel):
    code: str


class OneShotRequest(BaseModel):
    code: str
    timeout: int | None = None


class ErrorDetail(BaseModel):
    name: str
    value: str
    traceback: list[str]


class OutputItem(BaseModel):
    mime_type: str
    data: str | None = None
    data_b64: str | None = None
    url: str | None = None


class ExecuteResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    error: ErrorDetail | None = None
    outputs: list[OutputItem]
    execution_count: int


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_active: datetime


class DeleteResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    error: str
