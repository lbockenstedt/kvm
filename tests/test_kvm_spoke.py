import os
import sys
import types
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock

# Stub core.src.base_spoke so the spoke imports without the lm repo present.
_core = types.ModuleType("core")
_core_src = types.ModuleType("core.src")
_core_base = types.ModuleType("core.src.base_spoke")


class _BaseSpoke:
    def __init__(self, spoke_id, config):
        self.spoke_id = spoke_id
        self.config = config

    async def handle_command(self, command_type, data):
        raise NotImplementedError

    async def get_status(self):
        raise NotImplementedError


_core_base.BaseSpoke = _BaseSpoke
sys.modules["core"] = _core
sys.modules["core.src"] = _core_src
sys.modules["core.src.base_spoke"] = _core_base
sys.modules["base_spoke"] = _core_base

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kvm_spoke import KVMSpoke, _norm_mac, _vm_ips, _vm_macs
from kvm_engine import generate_domain_xml, parse_domain_xml, KVMEngine


def test_norm_mac():
    assert _norm_mac("00:11:22:33:44:55") == "00:11:22:33:44:55"
    assert _norm_mac("00-11-22-33-44-55") == "00:11:22:33:44:55"
    assert _norm_mac("001122334455") == "00:11:22:33:44:55"
    assert _norm_mac("invalid") == "invalid"
    assert _norm_mac("") == ""


def test_vm_ips():
    vm = {"ips": ["192.168.1.50", {"address": "10.0.0.5"}, {"ip": "172.16.0.10"}]}
    ips = _vm_ips(vm)
    assert "192.168.1.50" in ips
    assert "10.0.0.5" in ips
    assert "172.16.0.10" in ips


def test_vm_macs():
    vm = {
        "mac": "00:11:22:33:44:55",
        "interfaces": [{"mac": "00:aa:bb:cc:dd:ee"}],
        "ips": [{"mac": "aa:bb:cc:dd:ee:ff"}]
    }
    macs = _vm_macs(vm)
    assert "00:11:22:33:44:55" in macs
    assert "00:aa:bb:cc:dd:ee" in macs
    assert "aa:bb:cc:dd:ee:ff" in macs


@pytest.mark.asyncio
async def test_kvm_spoke_lifecycle_commands():
    spoke = KVMSpoke("kvm-spoke-1", {})
    
    # Version
    res = await spoke.handle_command("GET_VERSION", {})
    assert res["status"] == "SUCCESS"
    assert "version" in res
    
    # Config
    res = await spoke.handle_command("UPDATE_CONFIG", {"setting": "enabled"})
    assert res["status"] == "SUCCESS"
    
    # Status
    status = await spoke.get_status()
    assert status["module"] == "kvm"
    assert status["status"] == "NO_AGENTS"


@pytest.mark.asyncio
async def test_kvm_spoke_agent_dispatch():
    mock_cp = MagicMock()
    mock_cp.connected_agents = {
        "agent-1": {"hostname": "node1", "last_seen": 1000000000, "vms": [
            {"name": "vm-test-1", "vmid": "uuid-1", "tags": ["prod"]}
        ]}
    }
    mock_cp.broadcast_to_agents = AsyncMock(return_value=[{"nodes": [{"name": "node1"}], "agent_id": "agent-1"}])
    mock_cp.send_to_agent = AsyncMock(return_value={"status": "SUCCESS", "vms": []})

    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=mock_cp)
    
    # Get agents
    res = await spoke.handle_command("GET_AGENTS", {})
    assert res["status"] == "SUCCESS"
    assert len(res["agents"]) == 1
    
    # Node stats
    res = await spoke.handle_command("GET_NODE_STATS", {})
    assert res["status"] == "SUCCESS"
    assert len(res["nodes"]) == 1

    # Create VM relay
    res = await spoke.handle_command("CREATE_VM", {"name": "new-vm"})
    assert res["status"] == "SUCCESS"
    assert spoke._force_live_query is True


@pytest.mark.asyncio
async def test_kvm_spoke_search_scoping():
    mock_cp = MagicMock()
    mock_cp.connected_agents = {
        "agent-1": {"hostname": "node1", "last_seen": 9999999999, "vms": [
            {"name": "vm-prod", "vmid": "uuid-1", "tags": ["tenant-a"], "unique_id": "node1/vm-prod"}
        ]}
    }
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=mock_cp)

    # Scoped: non-admin without tag gets empty
    res = await spoke.handle_command("SEARCH_VMS", {"q": "prod", "is_admin": False})
    assert res["status"] == "SUCCESS"
    assert len(res["results"]) == 0

    # Scoped: admin without tag gets all
    res = await spoke.handle_command("SEARCH_VMS", {"q": "prod", "is_admin": True})
    assert res["status"] == "SUCCESS"
    assert len(res["results"]) == 1


def test_kvm_engine_xml_generation_and_parse():
    xml = generate_domain_xml(
        name="test-vm",
        memory_mb=2048,
        vcpus=2,
        disk_path="/var/lib/libvirt/images/test.qcow2",
        bridge_name="br0",
        mac_address="52:54:00:12:34:56"
    )
    assert "<name>test-vm</name>" in xml
    assert "<memory unit='MiB'>2048</memory>" in xml

    parsed = parse_domain_xml(xml)
    assert parsed["name"] == "test-vm"
    assert parsed["memory_mb"] == 2048
    assert parsed["vcpus"] == 2
    assert len(parsed["disks"]) == 1
    assert parsed["disks"][0]["file"] == "/var/lib/libvirt/images/test.qcow2"
    assert len(parsed["interfaces"]) == 1
    assert parsed["interfaces"][0]["bridge"] == "br0"
    assert parsed["interfaces"][0]["mac"] == "52:54:00:12:34:56"
