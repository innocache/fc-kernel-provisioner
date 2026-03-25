import pytest
from sandbox_client import SandboxSession


class TestSandboxClient:
    async def test_hello_world(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("print('hello')")
        assert result.success is True
        assert result.stdout.strip() == "hello"

    async def test_state_persists(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            await session.execute("x = 42")
            result = await session.execute("print(x)")
        assert result.stdout.strip() == "42"

    async def test_error_handling(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("1/0")
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "ZeroDivisionError"

    async def test_stderr_captured(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("import sys; print('warn', file=sys.stderr)")
        assert "warn" in result.stderr

    async def test_execution_count_increments(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            r1 = await session.execute("1")
            r2 = await session.execute("2")
        assert r2.execution_count > r1.execution_count

    async def test_explicit_lifecycle(self, fake_kg):
        session = SandboxSession(fake_kg)
        await session.start()
        result = await session.execute("print('explicit')")
        assert result.stdout.strip() == "explicit"
        await session.stop()

    async def test_error_recovery(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            r1 = await session.execute("1/0")
            assert r1.success is False
            r2 = await session.execute("print('ok')")
            assert r2.success is True

    async def test_stdout_before_error(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("print('before')\nraise RuntimeError('boom')")
        assert "before" in result.stdout
        assert result.error.name == "RuntimeError"

    async def test_multiline_output(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("for i in range(3): print(i)")
        assert result.stdout.strip() == "0\n1\n2"

    async def test_import_works(self, fake_kg):
        async with SandboxSession(fake_kg) as session:
            result = await session.execute("import json; print(json.dumps({'a': 1}))")
        assert result.success is True
        assert '"a"' in result.stdout
