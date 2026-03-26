"""Artifact storage — save execution outputs to files and return URLs."""

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol for saving execution artifacts and returning URLs."""

    async def save(
        self, session_id: str, filename: str, data: bytes, content_type: str,
    ) -> str:
        """Save artifact data and return its URL.

        The caller (SandboxSession) is responsible for encoding str data to
        bytes (UTF-8) before calling save().  Data is always bytes here.
        """
        ...


class LocalArtifactStore:
    """Saves artifacts to the local filesystem."""

    def __init__(self, base_dir: str, url_prefix: str):
        self._base_dir = base_dir
        self._url_prefix = url_prefix.rstrip("/")

    async def save(
        self, session_id: str, filename: str, data: bytes, content_type: str,
    ) -> str:
        """Write data to {base_dir}/{session_id}/{filename} and return URL."""
        dir_path = os.path.join(self._base_dir, session_id)
        os.makedirs(dir_path, exist_ok=True)

        file_path = os.path.join(dir_path, filename)
        with open(file_path, "wb") as f:
            f.write(data)

        return f"{self._url_prefix}/{session_id}/{filename}"
