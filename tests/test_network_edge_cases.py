"""Edge case tests for IP allocation and network management."""

import pytest
from fc_pool_manager.network import IPAllocator, NetworkManager


class TestIPAllocatorEdgeCases:
    def test_exhaust_pool_raises(self):
        """Allocating beyond the range should raise RuntimeError."""
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=3)
        alloc.allocate()  # .2
        alloc.allocate()  # .3
        with pytest.raises(RuntimeError, match="exhausted"):
            alloc.allocate()

    def test_single_ip_range(self):
        """start == end should give exactly one IP."""
        alloc = IPAllocator(gateway="172.16.0.1", start=100, end=100)
        ip = alloc.allocate()
        assert ip == "172.16.0.100"
        with pytest.raises(RuntimeError, match="exhausted"):
            alloc.allocate()

    def test_release_unknown_ip_is_noop(self):
        """Releasing an IP not in the allocated set should not crash."""
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=10)
        alloc.release("172.16.0.50")  # Never allocated
        assert alloc.available == 9  # Unchanged

    def test_double_release_same_ip(self):
        """Releasing the same IP twice should only add it once to free list."""
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=10)
        ip = alloc.allocate()
        alloc.release(ip)
        alloc.release(ip)  # Second release — not in _allocated anymore
        # Should be available once, not twice
        count = alloc._free.count(int(ip.rsplit(".", 1)[1]))
        assert count == 1

    def test_released_ip_reused_first(self):
        """Released IPs should be reused before new ones."""
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=10)
        ip1 = alloc.allocate()  # .2
        alloc.allocate()  # .3
        alloc.release(ip1)  # .2 goes back to front
        ip3 = alloc.allocate()  # should get .2 again
        assert ip3 == ip1

    def test_available_count(self):
        """available property should reflect current state."""
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=5)
        assert alloc.available == 4
        alloc.allocate()
        assert alloc.available == 3
        ip = alloc.allocate()
        assert alloc.available == 2
        alloc.release(ip)
        assert alloc.available == 3

    def test_gateway_prefix_extraction(self):
        """Different subnet prefixes should work correctly."""
        alloc = IPAllocator(gateway="10.0.5.1", start=2, end=5)
        ip = alloc.allocate()
        assert ip.startswith("10.0.5.")

    def test_allocate_returns_sequential_ips(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=10)
        ips = [alloc.allocate() for _ in range(3)]
        assert ips == ["172.16.0.2", "172.16.0.3", "172.16.0.4"]


class TestNetworkManagerEdgeCases:
    def test_mac_from_ip_low_octet(self):
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
        assert nm._mac_from_ip("172.16.0.2") == "AA:FC:00:00:00:02"

    def test_mac_from_ip_high_octet(self):
        """Octet 255 should produce FF."""
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
        assert nm._mac_from_ip("172.16.0.255") == "AA:FC:00:00:00:FF"

    def test_mac_from_ip_hex_boundary(self):
        """Octet 16 should produce 10 (hex)."""
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
        assert nm._mac_from_ip("172.16.0.16") == "AA:FC:00:00:00:10"

    def test_tap_name_format(self):
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
        assert nm._tap_name("a1b2c3d4") == "tap-a1b2c3d4"

    def test_tap_name_max_length(self):
        """Linux TAP names are limited to 15 characters."""
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
        tap = nm._tap_name("a1b2c3d4")
        # tap-<8chars> = 12 chars, well within 15
        assert len(tap) <= 15

    def test_ip_allocator_uses_configured_range(self):
        nm = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=10)
        ip = nm.allocate_ip()
        assert ip == "172.16.0.10"
