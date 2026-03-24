from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_api.caddy_client import CaddyClient


def _mock_response(status: int, text: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    return resp


class TestCaddyClient:
    @pytest.fixture
    def client(self):
        return CaddyClient(admin_url="http://localhost:2019")

    async def test_add_route_put_success(self, client):
        put_resp = _mock_response(200)
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.add_route("sess1", "172.16.0.2:5006")
        session.put.assert_awaited_once()

    async def test_add_route_fallback_to_post_on_404(self, client):
        put_resp = _mock_response(404)
        post_resp = _mock_response(201)
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.post = AsyncMock(return_value=post_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.add_route("sess1", "172.16.0.2:5006")
        session.put.assert_awaited_once()
        session.post.assert_awaited_once()

    async def test_add_route_server_error_raises(self, client):
        put_resp = _mock_response(500, "boom")
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="Caddy add_route failed"):
                await client.add_route("sess1", "172.16.0.2:5006")

    async def test_remove_route_success(self, client):
        del_resp = _mock_response(204)
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.remove_route("sess1")
        session.delete.assert_awaited_once()

    async def test_remove_route_404_is_ok(self, client):
        del_resp = _mock_response(404)
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.remove_route("sess1")

    async def test_remove_route_server_error_raises(self, client):
        del_resp = _mock_response(500, "boom")
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="Caddy remove_route failed"):
                await client.remove_route("sess1")

    def test_route_id_format(self, client):
        assert client._route_id("abc") == "dashboard_abc"

    def test_build_route_structure(self, client):
        route = client._build_route("sess1", "172.16.0.2:5006")
        assert route["@id"] == "dashboard_sess1"
        assert route["match"] == [{"path": ["/dash/sess1/*"]}]
        rp = route["handle"][0]
        assert rp["handler"] == "reverse_proxy"
        assert rp["upstreams"] == [{"dial": "172.16.0.2:5006"}]
        assert rp["flush_interval"] == -1
