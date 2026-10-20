import asyncio
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from taskbot_api.attachment_routes import AttachmentStorage, validated_file
from taskbot_api.config import Settings
from taskbot_api.models import AuditLog, TaskAttachment
from test_tasks import TOKEN, fixture, headers, login, new_task


def test_attachment_validation_and_private_storage_access():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        app.state.settings = Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                                      supabase_url="https://example.supabase.co",
                                      supabase_secret_key=SecretStr("sb_secret_test"),
                                      attachment_bucket="private-task-files",
                                      web_origin="http://localhost:3000",
                                      session_cookie_secure=False)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin:
                    author_csrf = await login(author, 703)
                    outsider_csrf = await login(outsider, 706)
                    admin_csrf = await login(admin, 701)
                    task = await new_task(author, author_csrf, project, users[704])
                    task_id = task["id"]
                    assert validated_file("report.pdf", "application/pdf", b"%PDF-1.4\n") == "report.pdf"
                    with patch.object(AttachmentStorage, "upload", new_callable=AsyncMock) as upload, \
                         patch.object(AttachmentStorage, "signed_url", new_callable=AsyncMock) as signed, \
                         patch.object(AttachmentStorage, "delete", new_callable=AsyncMock) as delete:
                        signed.return_value = "https://example.supabase.co/storage/v1/object/sign/private-task-files/x?token=test"
                        key = "attachment-replay-key"
                        upload_headers = {**headers(author_csrf, key), "Content-Type": "application/pdf"}
                        bad = await author.post(f"/api/v1/tasks/{task_id}/attachments?filename=wrong.pdf",
                                                headers=upload_headers, content=b"not a PDF")
                        assert bad.status_code == 422
                        denied = await outsider.post(f"/api/v1/tasks/{task_id}/attachments?filename=report.pdf",
                                                    headers={**headers(outsider_csrf), "Content-Type": "application/pdf"},
                                                    content=b"%PDF-1.4\n")
                        assert denied.status_code == 404
                        created = await author.post(f"/api/v1/tasks/{task_id}/attachments?filename=report.pdf",
                                                    headers=upload_headers, content=b"%PDF-1.4\n")
                        assert created.status_code == 201, created.text
                        attachment_id = created.json()["id"]
                        replay = await author.post(f"/api/v1/tasks/{task_id}/attachments?filename=report.pdf",
                                                   headers=upload_headers, content=b"%PDF-1.4\n")
                        assert replay.status_code == 201 and replay.json()["id"] == attachment_id
                        assert upload.await_count == 1
                        changed = await author.post(f"/api/v1/tasks/{task_id}/attachments?filename=report.pdf",
                                                    headers=upload_headers, content=b"%PDF-1.5\n")
                        assert changed.status_code == 409
                        assert (await outsider.get(f"/api/v1/tasks/{task_id}/attachments")).status_code == 404
                        assert len((await author.get(f"/api/v1/tasks/{task_id}/attachments")).json()) == 1
                        assert (await outsider.get(f"/api/v1/tasks/{task_id}/attachments/{attachment_id}/download")).status_code == 404
                        download = await author.get(f"/api/v1/tasks/{task_id}/attachments/{attachment_id}/download")
                        assert download.status_code == 200 and download.json()["expires_in"] == 60
                        assert signed.await_count == 1
                        forbidden = await outsider.delete(f"/api/v1/tasks/{task_id}/attachments/{attachment_id}",
                                                          headers=headers(outsider_csrf))
                        assert forbidden.status_code == 404
                        removed = await admin.delete(f"/api/v1/tasks/{task_id}/attachments/{attachment_id}",
                                                     headers=headers(admin_csrf))
                        assert removed.status_code == 200
                        assert delete.await_count == 1
                        assert (await author.get(f"/api/v1/tasks/{task_id}/attachments")).json() == []
                    async with factory() as db:
                        row = await db.get(TaskAttachment, UUID(attachment_id))
                        assert row.deleted_at is not None
                        actions = (await db.scalars(select(AuditLog.action).where(
                            AuditLog.target_id == attachment_id))).all()
                        assert actions == ["UPLOAD_ATTACHMENT", "DELETE_ATTACHMENT"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_storage_rejects_public_bucket_and_unexpected_signed_url():
    async def scenario():
        settings = Settings(_env_file=None, supabase_url="https://example.supabase.co",
                            supabase_secret_key=SecretStr("sb_secret_test"),
                            attachment_bucket="private-files")
        storage = AttachmentStorage(settings)
        assert "Authorization" not in storage.headers()
        with patch.object(storage, "_request", new_callable=AsyncMock) as request:
            request.return_value = httpx.Response(200, json={"public": True})
            try:
                await storage.require_private_bucket()
                assert False, "public bucket must be rejected"
            except Exception as error:
                assert getattr(error, "detail", None) == "ATTACHMENT_BUCKET_MUST_BE_PRIVATE"
            request.side_effect = [httpx.Response(200, json={"public": False}),
                                   httpx.Response(200, json={"signedURL": "https://evil.example/a"})]
            try:
                await storage.signed_url("tasks/one/file")
                assert False, "unexpected URL must be rejected"
            except Exception as error:
                assert getattr(error, "detail", None) == "ATTACHMENT_STORAGE_UNAVAILABLE"
            request.side_effect = [httpx.Response(200, json={"public": False}),
                                   httpx.Response(200, json={"signedURL":
                                       "/object/sign/private-files/tasks/one/file?token=short"})]
            url = await storage.signed_url("tasks/one/file")
            assert url == ("https://example.supabase.co/storage/v1/object/sign/"
                           "private-files/tasks/one/file?token=short")

    asyncio.run(scenario())
