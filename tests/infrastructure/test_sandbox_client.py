import os

import pytest

from sandbox_client import SandboxSession, LocalArtifactStore

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")


class TestSandboxClient:
    async def test_sandbox_hello_world(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute("print('hello')")
        assert result.success is True
        assert result.stdout.strip() == "hello"
        assert result.error is None

    async def test_sandbox_state_persists(self):
        async with SandboxSession(GATEWAY_URL) as session:
            await session.execute("x = 42")
            result = await session.execute("print(x)")
        assert result.stdout.strip() == "42"

    async def test_sandbox_error_handling(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute("1/0")
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "ZeroDivisionError"
        assert result.error.value == "division by zero"
        assert len(result.error.traceback) > 0

    async def test_sandbox_rich_output(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3])\n"
                "plt.show()"
            )
        assert result.success is True
        png_outputs = [o for o in result.outputs if o.mime_type == "image/png"]
        assert len(png_outputs) >= 1
        assert isinstance(png_outputs[0].data, bytes)
        assert len(png_outputs[0].data) > 100

    async def test_sandbox_html_output(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import pandas as pd\n"
                "df = pd.DataFrame({'a': [1, 2], 'b': [3, 4]})\n"
                "display(df)"
            )
        assert result.success is True
        html_outputs = [o for o in result.outputs if o.mime_type == "text/html"]
        assert len(html_outputs) >= 1
        assert "<table" in html_outputs[0].data.lower() or "<div" in html_outputs[0].data.lower()

    async def test_sandbox_timeout(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import time; time.sleep(60)", timeout=3,
            )
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "TimeoutError"

    async def test_sandbox_artifact_store(self, tmp_path):
        store = LocalArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            url_prefix="http://localhost:8080/artifacts",
        )
        async with SandboxSession(GATEWAY_URL, artifact_store=store) as session:
            result = await session.execute(
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3])\n"
                "plt.show()"
            )
        png_outputs = [o for o in result.outputs if o.mime_type == "image/png"]
        assert len(png_outputs) >= 1
        assert png_outputs[0].url is not None
        artifact_dir = tmp_path / "artifacts"
        assert artifact_dir.exists()
        files = list(artifact_dir.rglob("*.png"))
        assert len(files) >= 1

    async def test_sandbox_explicit_lifecycle(self):
        session = SandboxSession(GATEWAY_URL)
        await session.start()
        try:
            result = await session.execute("print('explicit')")
            assert result.success is True
            assert result.stdout.strip() == "explicit"
        finally:
            await session.stop()

    async def test_sandbox_error_recovery(self):
        async with SandboxSession(GATEWAY_URL) as session:
            r1 = await session.execute("1/0")
            assert r1.success is False
            r2 = await session.execute("print('recovered')")
            assert r2.success is True
            assert r2.stdout.strip() == "recovered"

    async def test_sandbox_execution_count_increments(self):
        async with SandboxSession(GATEWAY_URL) as session:
            r1 = await session.execute("1 + 1")
            r2 = await session.execute("2 + 2")
            assert r2.execution_count > r1.execution_count

    async def test_sandbox_stderr_captured(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import sys; print('warning', file=sys.stderr)"
            )
        assert result.success is True
        assert "warning" in result.stderr

    async def test_sandbox_stdout_before_error(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "print('before')\n"
                "raise RuntimeError('boom')"
            )
        assert result.success is False
        assert "before" in result.stdout
        assert result.error.name == "RuntimeError"
