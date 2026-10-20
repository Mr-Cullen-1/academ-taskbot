"""Task-scoped private attachments; bytes never pass through a public bucket."""

import hashlib
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from taskbot_api.attachment_storage import AttachmentStorage
from taskbot_api.models import ProjectMember, TaskAttachment, now, utc
from taskbot_api.routes import audit, mutation
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import visible_task

router = APIRouter(prefix="/api/v1/tasks", tags=["attachments"])
MAX_BYTES = 10 * 1024 * 1024
TYPES = {"application/pdf": ".pdf", "image/png": ".png",
         "image/jpeg": (".jpg", ".jpeg"), "text/plain": ".txt"}


class UploadFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    content_type: str
    sha256: str


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def metadata(item: TaskAttachment) -> dict:
    return {"id": str(item.id), "task_id": str(item.task_id),
            "uploader_user_id": str(item.uploader_user_id), "filename": item.filename,
            "content_type": item.content_type, "size_bytes": item.size_bytes,
            "created_at": utc(item.created_at).isoformat()}


def validated_file(filename: str, content_type: str, data: bytes) -> str:
    name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    media = content_type.split(";", 1)[0].strip().lower()
    if (not name or len(name) > 200 or any(ord(char) < 32 for char in name) or
            media not in TYPES or not name.lower().endswith(TYPES[media]) or
            not data or len(data) > MAX_BYTES):
        raise HTTPException(422, "ATTACHMENT_INVALID")
    if media == "application/pdf" and not data.startswith(b"%PDF-"):
        raise HTTPException(422, "ATTACHMENT_INVALID")
    if media == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "ATTACHMENT_INVALID")
    if media == "image/jpeg" and not (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")):
        raise HTTPException(422, "ATTACHMENT_INVALID")
    if media == "text/plain":
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "ATTACHMENT_INVALID") from None
        if any(ord(char) < 32 and char not in "\n\r\t" for char in decoded):
            raise HTTPException(422, "ATTACHMENT_INVALID")
    return name


@router.get("/{task_id}/attachments")
async def list_attachments(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    items = (await db.scalars(select(TaskAttachment).where(
        TaskAttachment.task_id == task_id, TaskAttachment.deleted_at.is_(None))
        .order_by(TaskAttachment.created_at, TaskAttachment.id))).all()
    return [metadata(item) for item in items]


@router.post("/{task_id}/attachments")
async def upload_attachment(task_id: UUID, filename: str, request: Request, db: Db,
                            actor: CurrentUser) -> Response:
    content_type = request.headers.get("Content-Type", "")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, "ATTACHMENT_TOO_LARGE")
        chunks.append(chunk)
    data = b"".join(chunks)
    name = validated_file(filename, content_type, data)
    media = content_type.split(";", 1)[0].strip().lower()
    fingerprint = UploadFingerprint(filename=name, content_type=media,
                                     sha256=hashlib.sha256(data).hexdigest())

    async def perform() -> tuple[int, dict]:
        task = await visible_task(db, actor, task_id)
        if await db.get(ProjectMember, (task.project_id, actor.id)) is None:
            raise HTTPException(403, "PROJECT_MEMBERSHIP_REQUIRED")
        key = request.headers["Idempotency-Key"]
        object_key = f"tasks/{task_id}/{hashlib.sha256(f'{actor.id}:{key}'.encode()).hexdigest()}"
        storage = AttachmentStorage(request.app.state.settings)
        await storage.upload(object_key, data, media)
        item = TaskAttachment(id=uuid4(), task_id=task_id, uploader_user_id=actor.id,
                              object_key=object_key, filename=name, content_type=media,
                              size_bytes=len(data), sha256=fingerprint.sha256, created_at=now())
        db.add(item)
        audit(db, actor, "UPLOAD_ATTACHMENT", "task_attachment", str(item.id),
              {"task_id": str(task_id), "size_bytes": len(data)})
        return 201, metadata(item)

    return await mutation(request, db, actor, fingerprint, f"task:attachment:{task_id}", perform)


@router.get("/{task_id}/attachments/{attachment_id}/download")
async def download_attachment(task_id: UUID, attachment_id: UUID, request: Request,
                              db: Db, actor: CurrentUser) -> dict:
    await visible_task(db, actor, task_id)
    item = await db.get(TaskAttachment, attachment_id)
    if item is None or item.task_id != task_id or item.deleted_at is not None:
        raise HTTPException(404, "NOT_FOUND")
    url = await AttachmentStorage(request.app.state.settings).signed_url(item.object_key)
    return {"url": url, "expires_in": 60}


@router.delete("/{task_id}/attachments/{attachment_id}")
async def delete_attachment(task_id: UUID, attachment_id: UUID, request: Request,
                            db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        await visible_task(db, actor, task_id)
        item = await db.get(TaskAttachment, attachment_id, with_for_update=True)
        if item is None or item.task_id != task_id or item.deleted_at is not None:
            raise HTTPException(404, "NOT_FOUND")
        if item.uploader_user_id != actor.id and "SUPER_ADMIN" not in await role_codes(db, actor.id):
            raise HTTPException(403, "FORBIDDEN")
        await AttachmentStorage(request.app.state.settings).delete(item.object_key)
        item.deleted_at = now()
        audit(db, actor, "DELETE_ATTACHMENT", "task_attachment", str(item.id),
              {"task_id": str(task_id)})
        return 200, {"deleted": True}

    return await mutation(request, db, actor, EmptyBody(),
                          f"task:attachment:delete:{task_id}:{attachment_id}", perform)
