"""Tests for the artifact store."""

import os

import pytest

from sandbox_client.artifact_store import ArtifactStore, LocalArtifactStore


class TestLocalArtifactStore:
    @pytest.fixture
    def store(self, tmp_path):
        return LocalArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            url_prefix="http://localhost:8080/artifacts",
        )

    async def test_save_creates_file(self, store, tmp_path):
        url = await store.save("session-1", "output_0.png", b"fake-png", "image/png")
        path = tmp_path / "artifacts" / "session-1" / "output_0.png"
        assert path.exists()
        assert path.read_bytes() == b"fake-png"

    async def test_save_returns_url(self, store):
        url = await store.save("session-1", "output_0.png", b"data", "image/png")
        assert url == "http://localhost:8080/artifacts/session-1/output_0.png"

    async def test_save_creates_directories(self, store, tmp_path):
        """Directories are created automatically."""
        await store.save("new-session", "chart.html", b"<html>", "text/html")
        path = tmp_path / "artifacts" / "new-session" / "chart.html"
        assert path.exists()

    async def test_save_multiple_files_same_session(self, store, tmp_path):
        await store.save("s1", "output_0.png", b"img1", "image/png")
        await store.save("s1", "output_1.html", b"<p>hi</p>", "text/html")
        assert (tmp_path / "artifacts" / "s1" / "output_0.png").exists()
        assert (tmp_path / "artifacts" / "s1" / "output_1.html").exists()

    async def test_save_overwrites_existing(self, store, tmp_path):
        await store.save("s1", "output_0.png", b"v1", "image/png")
        await store.save("s1", "output_0.png", b"v2", "image/png")
        path = tmp_path / "artifacts" / "s1" / "output_0.png"
        assert path.read_bytes() == b"v2"

    async def test_url_prefix_no_trailing_slash(self):
        store = LocalArtifactStore(base_dir="/tmp/art", url_prefix="http://host/art/")
        url = await store.save("s", "f.png", b"x", "image/png")
        assert url == "http://host/art/s/f.png"

    def test_implements_protocol(self):
        """LocalArtifactStore satisfies the ArtifactStore protocol."""
        store = LocalArtifactStore(base_dir="/tmp", url_prefix="http://x")
        assert isinstance(store, ArtifactStore)
