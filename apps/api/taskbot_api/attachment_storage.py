"""Server-only adapter for a private Supabase Storage bucket."""

import httpx
from fastapi import HTTPException

from taskbot_api.config import Settings


class AttachmentStorage:
    def __init__(self, settings: Settings):
        if not (settings.supabase_url and settings.supabase_secret_key and settings.attachment_bucket):
            raise HTTPException(503, "ATTACHMENT_STORAGE_NOT_CONFIGURED")
        self.origin = settings.supabase_url.rstrip("/")
        self.bucket = settings.attachment_bucket
        self.key = settings.supabase_secret_key.get_secret_value()

    def headers(self) -> dict[str, str]:
        headers = {"apikey": self.key}
        # Legacy service-role JWTs also require bearer auth. New sb_secret keys do not.
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    async def _request(self, method: str, suffix: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.request(method, f"{self.origin}/storage/v1/{suffix}",
                                                headers={**self.headers(), **kwargs.pop("headers", {})},
                                                **kwargs)
        except httpx.HTTPError:
            raise HTTPException(503, "ATTACHMENT_STORAGE_UNAVAILABLE") from None
        if response.status_code >= 400:
            # Supabase error bodies may contain paths or secrets; never forward them.
            raise HTTPException(503, "ATTACHMENT_STORAGE_UNAVAILABLE")
        return response

    async def require_private_bucket(self) -> None:
        response = await self._request("GET", f"bucket/{self.bucket}")
        try:
            if response.json().get("public") is not False:
                raise HTTPException(503, "ATTACHMENT_BUCKET_MUST_BE_PRIVATE")
        except ValueError:
            raise HTTPException(503, "ATTACHMENT_STORAGE_UNAVAILABLE") from None

    async def upload(self, object_key: str, contents: bytes, content_type: str) -> None:
        await self.require_private_bucket()
        await self._request("POST", f"object/{self.bucket}/{object_key}",
                            content=contents,
                            headers={"Content-Type": content_type, "x-upsert": "true"})

    async def signed_url(self, object_key: str) -> str:
        await self.require_private_bucket()
        response = await self._request("POST", f"object/sign/{self.bucket}/{object_key}",
                                       json={"expiresIn": 60})
        try:
            path = response.json()["signedURL"]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(503, "ATTACHMENT_STORAGE_UNAVAILABLE") from None
        prefix = f"/object/sign/{self.bucket}/{object_key}?"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise HTTPException(503, "ATTACHMENT_STORAGE_UNAVAILABLE")
        return f"{self.origin}/storage/v1{path}"

    async def delete(self, object_key: str) -> None:
        await self.require_private_bucket()
        await self._request("DELETE", f"object/{self.bucket}", json={"prefixes": [object_key]})
