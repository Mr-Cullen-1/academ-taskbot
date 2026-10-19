"""Validate and normalize private profile images before database storage."""

from io import BytesIO

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 512 * 1024
MAX_DIMENSION = 4096
MAX_STORED_BYTES = 256 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def normalize_avatar(encoded: bytes) -> bytes:
    if not encoded or len(encoded) > MAX_UPLOAD_BYTES:
        raise HTTPException(422, "AVATAR_SIZE_INVALID")
    try:
        with Image.open(BytesIO(encoded)) as probe:
            if probe.format not in ALLOWED_FORMATS or max(probe.size) > MAX_DIMENSION or min(probe.size) < 1:
                raise HTTPException(422, "AVATAR_FORMAT_INVALID")
            probe.verify()
        with Image.open(BytesIO(encoded)) as image:
            image.load()
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((512, 512), Image.Resampling.LANCZOS)
            result = BytesIO()
            normalized.save(result, format="JPEG", quality=82, optimize=True)
            value = result.getvalue()
            if len(value) > MAX_STORED_BYTES:
                raise HTTPException(422, "AVATAR_SIZE_INVALID")
            return value
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "AVATAR_FORMAT_INVALID") from exc
