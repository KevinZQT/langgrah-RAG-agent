"""Single-user local API. Run one worker, bound to loopback."""
import logging
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .settings import Settings
from .storage import Catalog
from .loaders import SUPPORTED, load_documents
from .service import RagService, friendly_error

log = logging.getLogger("folio")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("content")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("Empty content")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    document_ids: list[UUID] = Field(min_length=1, max_length=50)
    messages: list[ChatMessage] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_history(self):
        if self.messages[-1].role != "user":
            raise ValueError("The last message must be from the user")
        if sum(len(message.content) for message in self.messages) > 100_000:
            raise ValueError("Conversation too long")
        return self


def create_app(settings=None, service=None, loader=load_documents):
    settings = settings or Settings()
    catalog = Catalog(settings.data_dir)
    service = service or RagService(settings)
    # Serialize parsing/index/delete to protect OCR/PDFium and avoid delete/index races.
    work_lock = RLock()
    index_lock = RLock()

    def remove_file(path):
        target = Path(path).resolve()
        if target.parent != catalog.uploads.resolve():
            raise RuntimeError("Invalid stored path")
        target.unlink(missing_ok=True)

    def cleanup(document_id=None):
        # Failed cleanup remains in SQLite for retry after restart or the next update.
        for version in catalog.versions(document_id, "obsolete"):
            try:
                service.delete(version["document_id"], version=version["version"])
                remove_file(version["path"])
                catalog.forget_version(version["document_id"], version["version"])
            except Exception as error:
                log.warning("Version cleanup failed (%s)", type(error).__name__)

    @asynccontextmanager
    async def lifespan(app):
        catalog.recover_interrupted()
        cleanup()
        yield

    app = FastAPI(title="Folio RAG API", version="0.1.0", lifespan=lifespan)
    app.state.catalog = catalog

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "Invalid request. Check the file, document IDs, and chat messages."}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local-single-user", "dependencies_checked": False}

    @app.get("/documents")
    def list_documents():
        return {"documents": [catalog.public(record) for record in catalog.all()]}

    def process_document(document_id, version):
        with index_lock:
            pending = [row for row in catalog.versions(document_id, "pending") if row["version"] == version]
            if not pending:
                return
            record = pending[0]
            try:
                docs = loader(Path(record["path"]), record["name"], document_id)
                for doc in docs:
                    doc.metadata["version"] = version
                excerpt = "\n\n".join(doc.page_content for doc in docs)[:8000]
                catalog.update(document_id, index_started=1)
                service.index(docs)
            except Exception as error:
                log.error("Document %s failed (%s)", document_id, type(error).__name__)
                with work_lock:
                    catalog.fail(document_id, version, friendly_error(error))
                    cleanup(document_id)
                return
            with work_lock:
                # Chat holds this lock through retrieval, so an in-flight query's
                # version cannot be removed between its snapshot and its search.
                catalog.activate(record, excerpt, datetime.now(timezone.utc).isoformat())
                cleanup(document_id)

    def accept_file(background_tasks, file, document_id=None):
        name = (file.filename or "").replace("\\", "/").split("/")[-1]
        suffix = Path(name).suffix.lower()
        if not name or len(name) > 240 or suffix not in SUPPORTED:
            file.file.close()
            raise HTTPException(415, "Unsupported file format. Upload PDF, DOCX, an image, TXT, or Markdown.")
        is_new = document_id is None
        document_id = document_id or str(uuid4())
        version = uuid4().hex
        path = catalog.uploads / (document_id + "_" + version + suffix)
        size = 0
        digest = hashlib.sha256()
        try:
            with path.open("xb") as destination:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_bytes:
                        raise HTTPException(413, "Files must be no larger than 20 MB.")
                    destination.write(chunk)
                    digest.update(chunk)
            if not size:
                raise HTTPException(400, "Cannot upload an empty file.")
            record = {"id": document_id, "name": name, "kind": suffix[1:].upper(), "size": size,
                      "status": "processing", "addedAt": datetime.now(timezone.utc).isoformat(),
                      "path": str(path), "file_hash": digest.hexdigest()}
            with work_lock:
                existing = catalog.get(document_id)
                if not is_new:
                    if existing is None:
                        raise HTTPException(404, "Document not found.")
                    if existing["updating"] or existing["status"] == "processing":
                        raise HTTPException(409, "This document is being processed. Wait until processing finishes before updating it.")
                candidates = catalog.all() if is_new else [existing]
                for candidate in candidates:
                    if candidate["status"] != "ready" or candidate["updating"]:
                        continue
                    old_hash = candidate["file_hash"]
                    if not old_hash and Path(candidate["path"]).is_file():
                        with Path(candidate["path"]).open("rb") as original:
                            old_hash = hashlib.file_digest(original, "sha256").hexdigest()
                        catalog.update(candidate["id"], file_hash=old_hash)
                    if old_hash == record["file_hash"] and candidate["kind"] == record["kind"]:
                        path.unlink(missing_ok=True)
                        return JSONResponse({"document": catalog.public(candidate), "unchanged": True}, status_code=200)
                # Coalesce concurrent first uploads of identical bytes as well.
                if is_new:
                    for pending in catalog.versions(state="pending"):
                        if pending["file_hash"] == record["file_hash"] and pending["kind"] == record["kind"]:
                            path.unlink(missing_ok=True)
                            return JSONResponse({"document": catalog.public(catalog.get(pending["document_id"])),
                                                 "unchanged": True}, status_code=202)
                catalog.stage(record, version, new=is_new)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            file.file.close()
        response = catalog.public(catalog.get(document_id))
        background_tasks.add_task(process_document, document_id, version)
        return JSONResponse({"document": response, "unchanged": False}, status_code=202)

    @app.post("/documents", status_code=202)
    def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
        return accept_file(background_tasks, file)

    @app.put("/documents/{document_id}/content", status_code=202)
    def update_document(document_id: UUID, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
        return accept_file(background_tasks, file, str(document_id))

    @app.delete("/documents/{document_id}", status_code=204)
    def delete_document(document_id: UUID):
        key = str(document_id)
        record = catalog.get(key)
        if not record:
            raise HTTPException(404, "Document not found.")
        if record["status"] == "processing" or record["updating"]:
            raise HTTPException(409, "This document is being processed. Wait until processing finishes before deleting it.")
        with work_lock:
            record = catalog.get(key)
            if not record:
                raise HTTPException(404, "Document not found.")
            if record["status"] == "processing" or record["updating"]:
                raise HTTPException(409, "This document is being processed. Wait until processing finishes before deleting it.")
            try:
                # Also clean partial writes when indexing failed.
                if record["index_started"]:
                    service.delete(key)
                for version in catalog.versions(key):
                    remove_file(version["path"])
                remove_file(record["path"])
                catalog.delete(key)
            except Exception as error:
                log.error("Delete %s failed (%s)", key, type(error).__name__)
                raise HTTPException(502, friendly_error(error)) from None
        return Response(status_code=204)

    @app.post("/chat")
    def chat(body: ChatRequest):
        with work_lock:
            return chat_locked(body)

    def chat_locked(body):
        document_ids = list(dict.fromkeys(str(value) for value in body.document_ids))
        versions = {}
        for document_id in document_ids:
            record = catalog.get(document_id)
            if not record:
                raise HTTPException(404, "A selected document no longer exists. Refresh the document list.")
            if record["status"] != "ready":
                raise HTTPException(409, "A selected document is not ready yet. Check its processing status.")
            versions[document_id] = record["active_version"]
        try:
            return service.chat(body.messages, document_ids, versions=versions)
        except Exception as error:
            log.error("Chat failed (%s)", type(error).__name__)
            raise HTTPException(502, friendly_error(error)) from None

    return app


app = create_app()
