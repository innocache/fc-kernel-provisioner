import pytest
from aiohttp import web

from fc_pool_manager.server import create_app
from fc_pool_manager.vm import VMInstance, VMState


class DummyManager:
    def __init__(self):
        self._vms = {}
        self._kernel_to_vm = {}

    def bind_kernel(self, vm_id, kernel_id):
        self._kernel_to_vm[kernel_id] = vm_id

    def vm_by_kernel(self, kernel_id):
        vm_id = self._kernel_to_vm.get(kernel_id)
        if vm_id is None:
            return None
        vm = self._vms.get(vm_id)
        if vm is None or vm.state != VMState.ASSIGNED:
            return None
        return {"vm_id": vm.vm_id, "ip": vm.ip, "vsock_path": vm.vsock_path}


@pytest.fixture
async def client(aiohttp_client):
    m = DummyManager()
    vm = VMInstance(
        vm_id="vm-1", short_id="1", ip="172.16.0.2", cid=3,
        tap_name="tap-1", mac="aa:bb:cc:dd:ee:ff",
        jail_path="/tmp", vsock_path="/tmp/v.sock",
    )
    vm.transition_to(VMState.IDLE)
    vm.transition_to(VMState.ASSIGNED)
    m._vms[vm.vm_id] = vm
    app = create_app(m)
    return await aiohttp_client(app)


class TestPoolServerBindLookup:
    async def test_bind_then_lookup_success(self, client):
        resp = await client.post(
            "/api/vms/vm-1/bind-kernel",
            json={"kernel_id": "kid1"},
        )
        assert resp.status == 200
        resp = await client.get("/api/vms/by-kernel/kid1")
        assert resp.status == 200
        data = await resp.json()
        assert data["vm_id"] == "vm-1"
        assert data["ip"] == "172.16.0.2"
        assert data["vsock_path"] == "/tmp/v.sock"

    async def test_lookup_unknown_kernel_404(self, client):
        resp = await client.get("/api/vms/by-kernel/missing")
        assert resp.status == 404

    async def test_bind_missing_kernel_id_400(self, client):
        resp = await client.post(
            "/api/vms/vm-1/bind-kernel",
            json={},
        )
        assert resp.status == 400

    async def test_bind_unknown_vm_404(self, client):
        resp = await client.post(
            "/api/vms/vm-nonexistent/bind-kernel",
            json={"kernel_id": "kid2"},
        )
        assert resp.status == 404

    async def test_bind_replaces_previous(self, client):
        await client.post(
            "/api/vms/vm-1/bind-kernel",
            json={"kernel_id": "kid-old"},
        )
        await client.post(
            "/api/vms/vm-1/bind-kernel",
            json={"kernel_id": "kid-new"},
        )
        resp = await client.get("/api/vms/by-kernel/kid-new")
        assert resp.status == 200
        resp_old = await client.get("/api/vms/by-kernel/kid-old")
        assert resp_old.status == 200
