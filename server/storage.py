"""Document storage backends for local development and Supabase Storage."""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import HTTPException, UploadFile


class LocalStorage:
    def __init__(self, root: Path, max_upload_bytes: int = 25 * 1024 * 1024):
        self.root = root
        self.max_upload_bytes = max(0, int(max_upload_bytes))

    async def save(self, company_id: str, document_id: str, name: str,
                   file: UploadFile) -> tuple[str, int]:
        directory = self.root / company_id / document_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        size = 0
        with destination.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if self.max_upload_bytes and size > self.max_upload_bytes:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(413, "Document exceeds the configured upload limit")
                handle.write(chunk)
        return str(destination), size

    def resolve(self, location: str) -> str:
        return location

    def delete(self, location: str) -> None:
        Path(location).unlink(missing_ok=True)


class SupabaseStorage:
    BUCKET = "interfaze-documents"

    def __init__(self, url: str, service_key: str,
                 max_upload_bytes: int = 25 * 1024 * 1024):
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.max_upload_bytes = max(0, int(max_upload_bytes))

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.service_key}", "apikey": self.service_key}

    async def save(self, company_id: str, document_id: str, name: str,
                   file: UploadFile) -> tuple[str, int]:
        chunks, size = [], 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if self.max_upload_bytes and size > self.max_upload_bytes:
                raise HTTPException(413, "Document exceeds the configured upload limit")
            chunks.append(chunk)
        key = f"{company_id}/{document_id}/{name}"
        headers = {**self.headers, "Content-Type": file.content_type or "application/octet-stream",
                   "x-upsert": "false"}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.url}/storage/v1/object/{self.BUCKET}/{key}",
                headers=headers, content=b"".join(chunks),
            )
        if response.status_code >= 400:
            raise HTTPException(502, f"Supabase Storage upload failed: {response.text[:300]}")
        return f"supabase://{self.BUCKET}/{key}", size

    def resolve(self, location: str) -> str:
        prefix = f"supabase://{self.BUCKET}/"
        if not location.startswith(prefix):
            raise ValueError("Invalid Supabase storage location")
        key = location[len(prefix):]
        response = httpx.post(
            f"{self.url}/storage/v1/object/sign/{self.BUCKET}/{key}",
            headers=self.headers, json={"expiresIn": 900}, timeout=15,
        )
        response.raise_for_status()
        signed = response.json()["signedURL"]
        return signed if signed.startswith("http") else f"{self.url}/storage/v1{signed}"

    def delete(self, location: str) -> None:
        prefix = f"supabase://{self.BUCKET}/"
        if not location.startswith(prefix):
            return
        key = location[len(prefix):]
        response = httpx.delete(
            f"{self.url}/storage/v1/object/{self.BUCKET}/{key}",
            headers=self.headers, timeout=15,
        )
        response.raise_for_status()


def create_storage(settings):
    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseStorage(
            settings.supabase_url,
            settings.supabase_service_role_key,
            settings.max_upload_bytes,
        )
    return LocalStorage(settings.upload_dir, settings.max_upload_bytes)

