"""HTTP API for the mail thread reconstruction service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_schema()
    yield
    db.close_pool()


app = FastAPI(
    title="Mail Thread Reconstruction API",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class ImportRequest(BaseModel):
    mailbox: str = Field(min_length=1, max_length=200)
    messages: list[str]


class MergeMap(BaseModel):
    old_thread_id: str
    new_thread_id: str


class ImportResponse(BaseModel):
    mailbox: str
    revision: int
    created: bool
    imported: list[str]
    duplicates: list[str]
    merges: list[MergeMap]
    missing: list[str]


class ThreadNode(BaseModel):
    message_id: str
    present: bool
    children: list["ThreadNode"]


class Thread(BaseModel):
    thread_id: str
    root: ThreadNode


class ThreadsResponse(BaseModel):
    mailbox: str
    revision: int
    head_revision: int
    threads: list[Thread]
    missing: list[str]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    with db.get_pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/imports", response_model=ImportResponse)
def import_messages(req: ImportRequest):
    mailbox = req.mailbox.strip()
    if not mailbox:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_mailbox", "message": "mailbox must not be blank"},
        )
    raws = [m.encode("utf-8", "surrogateescape") for m in req.messages]
    try:
        with db.get_pool().connection() as conn:
            return service.import_batch(conn, mailbox, raws)
    except service.Reject as rej:
        raise HTTPException(status_code=rej.status, detail=rej.detail) from rej


@app.get("/threads", response_model=ThreadsResponse)
def get_threads(
    mailbox: str = Query(min_length=1),
    revision: int | None = Query(default=None, ge=0),
):
    try:
        with db.get_pool().connection() as conn:
            return service.get_threads(conn, mailbox, revision)
    except service.Reject as rej:
        raise HTTPException(status_code=rej.status, detail=rej.detail) from rej
