"""Tests for IP allocation and TAP device naming."""

import pytest
from fc_pool_manager.network import IPAllocator, NetworkManager


class TestIPAllocator:
    def test_allocate_first_ip(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip = alloc.allocate()
        assert ip == "172.16.0.2"

    def test_allocate_sequential(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip1 = alloc.allocate()
        ip2 = alloc.allocate()
        assert ip1 != ip2
        assert ip1 == "172.16.0.2"
        assert ip2 == "172.16.0.3"

    def test_release_and_reuse(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip = alloc.allocate()
        alloc.release(ip)
        ip2 = alloc.allocate()
        assert ip2 == ip

    def test_exhaustion_raises(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=3)
        alloc.allocate()
        alloc.allocate()
        with pytest.raises(RuntimeError, match="exhausted"):
            alloc.allocate()

    def test_available_count(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        assert alloc.available == 253
        alloc.allocate()
        assert alloc.available == 252


class TestNetworkManager:
    @pytest.fixture
    def nm(self):
        return NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)

    def test_tap_name_within_ifnamsiz(self, nm):
        name = nm._tap_name("a1b2c3d4")
        assert name == "tap-a1b2c3d4"
        assert len(name) <= 15

    def test_mac_from_ip(self, nm):
        mac = nm._mac_from_ip("172.16.0.2")
        assert mac == "AA:FC:00:00:00:02"
        mac = nm._mac_from_ip("172.16.0.255")
        assert mac == "AA:FC:00:00:00:FF"

    def test_allocate_and_release_ip(self, nm):
        ip = nm.allocate_ip()
        assert ip == "172.16.0.2"
        nm.release_ip(ip)
        ip2 = nm.allocate_ip()
        assert ip2 == ip
