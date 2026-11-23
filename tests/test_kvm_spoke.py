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

    # Create VM relay: the target agent must be named, and only an admin or a tenant may create.
    res = await spoke.handle_command("CREATE_VM", {"name": "new-vm", "agent_id": "agent-1", "is_admin": True})
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


def _two_agent_cp():
    """A control plane mock with two KVM hosts, each running one tagged VM."""
    cp = MagicMock()
    cp.connected_agents = {
        "agent-1": {"hostname": "node1", "last_seen": 9999999999,
                    "vms": [{"name": "vm-a", "tags": ["tenant-a"]}]},
        "agent-2": {"hostname": "node2", "last_seen": 9999999999,
                    "vms": [{"name": "vm-b", "tags": ["tenant-b"]}]},
    }
    cp.send_to_agent = AsyncMock(return_value={"status": "SUCCESS"})
    return cp


@pytest.mark.asyncio
async def test_vm_commands_never_fall_back_to_first_agent():
    cp = _two_agent_cp()
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    # No agent_id and no unique_id: refused, nothing sent.
    for cmd in ("GET_VM_INFO", "CREATE_VM", "DELETE_VM"):
        res = await spoke.handle_command(cmd, {"name": "vm-b", "is_admin": True})
        assert res["status"] == "ERROR"
    # Unknown agent: refused.
    res = await spoke.handle_command("DELETE_VM", {"agent_id": "ghost", "name": "vm-b", "is_admin": True})
    assert res["status"] == "ERROR"
    cp.send_to_agent.assert_not_called()
    # unique_id resolves to the agent whose hostname matches.
    res = await spoke.handle_command("DELETE_VM", {"unique_id": "node2/vm-b", "is_admin": True})
    assert res["status"] == "SUCCESS"
    assert cp.send_to_agent.call_args.kwargs["agent_id"] == "agent-2"


@pytest.mark.asyncio
async def test_vm_commands_are_tenant_scoped():
    cp = _two_agent_cp()
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    # Another tenant's VM: refused as not found.
    res = await spoke.handle_command("DELETE_VM", {"unique_id": "node2/vm-b", "proxmox_tag": "tenant-a"})
    assert res["status"] == "ERROR"
    res = await spoke.handle_command("GET_VM_INFO", {"unique_id": "node2/vm-b", "proxmox_tag": "tenant-a"})
    assert res["status"] == "ERROR"
    # No tenant tag and not admin: refused.
    res = await spoke.handle_command("DELETE_VM", {"unique_id": "node1/vm-a"})
    assert res["status"] == "ERROR"
    res = await spoke.handle_command("CREATE_VM", {"agent_id": "agent-1", "name": "x"})
    assert res["status"] == "ERROR"
    cp.send_to_agent.assert_not_called()
    # Own VM: allowed.
    res = await spoke.handle_command("DELETE_VM", {"unique_id": "node1/vm-a", "proxmox_tag": "Tenant-A"})
    assert res["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_failed_mutation_does_not_force_live_query():
    cp = _two_agent_cp()
    cp.send_to_agent = AsyncMock(return_value={"status": "ERROR", "message": "Agent response timeout"})
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    res = await spoke.handle_command("DELETE_VM", {"agent_id": "agent-1", "name": "vm-a", "is_admin": True})
    assert res["status"] == "ERROR"
    assert spoke._force_live_query is False


@pytest.mark.asyncio
async def test_agent_failures_are_reported_not_hidden():
    cp = _two_agent_cp()
    cp.connected_agents["agent-2"]["last_seen"] = 0  # stale: forces a live query
    cp.send_to_agent = AsyncMock(return_value={"status": "ERROR", "message": "Agent response timeout"})
    cp.broadcast_to_agents = AsyncMock(return_value=[
        {"agent_id": "agent-1", "nodes": [{"name": "node1"}]},
        {"agent_id": "agent-2", "status": "ERROR", "message": "Agent response timeout"},
    ])
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    res = await spoke.handle_command("PXMX_LIST_VMS", {})
    assert [v["name"] for v in res["vms"]] == ["vm-a"]
    assert res["errors"] == [{"agent_id": "agent-2", "message": "Agent response timeout"}]
    res = await spoke.handle_command("GET_NODE_STATS", {})
    assert len(res["nodes"]) == 1
    assert res["errors"][0]["agent_id"] == "agent-2"


def test_domain_xml_escapes_values_and_keeps_vnc_local():
    xml = generate_domain_xml("x</name><devices><hostdev/></devices><name>y", 512, 1,
                              "/d.qcow2'/><source file='/etc/shadow", "br0' x='1", "aa:bb'")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    # The hostile values stay literal text: no injected element, one disk, one source.
    assert root.findtext("name") == "x</name><devices><hostdev/></devices><name>y"
    assert root.find(".//hostdev") is None
    assert [d.find("source").get("file") for d in root.iter("disk")] == \
        ["/d.qcow2'/><source file='/etc/shadow"]
    assert root.find(".//interface/source").get("bridge") == "br0' x='1"
    assert "listen='127.0.0.1'" in xml and "0.0.0.0" not in xml
    with pytest.raises(ValueError):
        generate_domain_xml("vm", "512; rm", 1, "/d.qcow2")


@pytest.mark.asyncio
async def test_get_vm_info_accepts_the_hub_payload_shapes():
    """lm sends {vm_id: unique_id|"all"|vmid, vmid, node} with no tenant fields
    and authorizes from the tags in the reply (routes/pxmx.py, pxmx_vm.py)."""
    cp = _two_agent_cp()
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    # VM detail / console gate: vm_id is the unique id.
    res = await spoke.handle_command("GET_VM_INFO", {"vm_id": "node2/vm-b", "vmid": None, "node": None})
    assert res["status"] == "SUCCESS"
    assert cp.send_to_agent.call_args.kwargs["agent_id"] == "agent-2"
    assert cp.send_to_agent.call_args.args[1]["name"] == "vm-b"
    # VM control gate without a unique id: bare name plus node.
    res = await spoke.handle_command("GET_VM_INFO", {"vm_id": "vm-a", "vmid": None, "node": "node1"})
    assert res["status"] == "SUCCESS"
    assert cp.send_to_agent.call_args.kwargs["agent_id"] == "agent-1"
    # Admin inventory: every agent's VMs, not one agent's.
    cp.send_to_agent.reset_mock()
    res = await spoke.handle_command("GET_VM_INFO", {"vm_id": "all"})
    assert res["status"] == "SUCCESS"
    assert {v["name"] for v in res["vms"]} == {"vm-a", "vm-b"}


@pytest.mark.asyncio
async def test_vm_command_acts_on_the_domain_that_was_checked():
    cp = _two_agent_cp()
    spoke = KVMSpoke("kvm-spoke-1", {}, control_plane=cp)
    # A stray "name" can't redirect a tenant-checked command to another domain.
    res = await spoke.handle_command("DELETE_VM", {"unique_id": "node1/vm-a", "name": "vm-other",
                                                   "proxmox_tag": "tenant-a"})
    assert res["status"] == "SUCCESS"
    assert cp.send_to_agent.call_args.args[1]["name"] == "vm-a"
    # An agent_id that disagrees with the unique id's host is refused.
    cp.send_to_agent.reset_mock()
    res = await spoke.handle_command("DELETE_VM", {"agent_id": "agent-1", "unique_id": "node2/vm-b",
                                                   "is_admin": True})
    assert res["status"] == "ERROR"
    cp.send_to_agent.assert_not_called()
