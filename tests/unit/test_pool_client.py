"""Tests for the pool client."""

import pytest
from fc_provisioner.pool_client import PoolClient


class TestPoolClient:
    @pytest.fixture
    def client(self):
        return PoolClient(socket_path="/tmp/test.sock")

    def test_init(self, client):
        assert client.socket_path == "/tmp/test.sock"

    def test_base_url(self, client):
        assert "localhost" in client._base_url
