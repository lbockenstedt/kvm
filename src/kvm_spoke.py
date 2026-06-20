import logging
from typing import Any, Dict, List

try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke

logger = logging.getLogger("KVMSpoke")


class KVMSpoke(BaseSpoke):
    """
    KVM/libvirt hypervisor integration spoke.

    Implements the same command contract as ProxmoxSpoke (module_type="hypervisor")
    so the hub and WebUI need zero changes to switch from Proxmox to KVM.

    Required commands (same interface as pxmx):
      GET_NODE_STATS   → node CPU/RAM stats
      PXMX_LIST_VMS   → aggregated VM list with unique_id "<host>/<domain>"
      SEARCH_VMS       → filter VMs by name/ID fragment
      GET_VM_INFO      → details for a specific VM
      CREATE_VM        → define a new domain
      DELETE_VM        → undefine and optionally delete disk

    Each VM record includes:
      unique_id  — "<hostname>/<domain_name>"
      cluster    — hostname (KVM has no cluster concept natively)
      node       — hostname
      vmid       — libvirt domain UUID
      type       — "kvm"
      name, status, cpu, mem_bytes
    """

    def __init__(self, spoke_id: str, config: Dict[str, Any], control_plane=None):
        super().__init__(spoke_id, config)
        self.control_plane = control_plane

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        cmd = command_type.upper()

        if cmd == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if cmd == "UPDATE_CONFIG":
            self.config = data
            return {"status": "SUCCESS", "message": "Config updated"}

        if cmd == "GET_AGENTS":
            return self._get_agents()

        if cmd == "GET_NODE_STATS":
            return await self._get_node_stats(data)

        if cmd in ("PXMX_LIST_VMS", "GET_VM_LIST"):
            return await self._list_vms(data)

        if cmd == "SEARCH_VMS":
            return await self._search_vms(data)

        if cmd == "GET_VM_INFO":
            return await self._get_vm_info(data)

        if cmd == "CREATE_VM":
            return await self._create_vm(data)

        if cmd == "DELETE_VM":
            return await self._delete_vm(data)

        return {"status": "ERROR", "error": f"Unknown command: {command_type}"}

    def _get_agents(self) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "SUCCESS", "agents": []}
        agents = [
            {"agent_id": aid, "hostname": info.get("hostname", aid),
             "cluster_name": info.get("hostname", aid), "vm_count": len(info.get("vms", []))}
            for aid, info in self.control_plane.connected_agents.items()
        ]
        return {"status": "SUCCESS", "agents": agents}

    async def _get_node_stats(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "nodes": []}
        results = await self.control_plane.broadcast_to_agents("GET_NODE_STATS", {})
        nodes: List[Dict] = []
        for res in results:
            for node in res.get("nodes", []):
                nodes.append({**node, "agent_id": res.get("agent_id", "")})
        return {"status": "SUCCESS", "nodes": nodes}

    async def _list_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "vms": [], "agent_count": 0}

        tag_filter = (data.get("tag_filter") or "").lower() or None

        # Serve from telemetry cache (fast path)
        cached: List[Dict] = []
        for aid, info in self.control_plane.connected_agents.items():
            hostname = info.get("hostname", aid)
            for vm in info.get("vms", []):
                name = vm.get("name", "")
                cached.append({
                    **vm,
                    "agent_id":  aid,
                    "cluster":   vm.get("cluster", hostname),
                    "unique_id": vm.get("unique_id", f"{hostname}/{name}"),
                    "type":      vm.get("type", "kvm"),
                })

        if tag_filter:
            cached = [v for v in cached
                      if tag_filter in [t.lower() for t in (v.get("tags") or [])]]

        if cached:
            return {"status": "SUCCESS", "vms": cached,
                    "source": "telemetry_cache",
                    "agent_count": len(self.control_plane.connected_agents)}

        # Live query
        results = await self.control_plane.broadcast_to_agents("GET_VM_LIST", {})
        all_vms: List[Dict] = []
        for res in results:
            aid = res.get("agent_id", "")
            for vm in res.get("vms", []):
                all_vms.append({**vm, "agent_id": aid, "type": vm.get("type", "kvm")})
        return {"status": "SUCCESS", "vms": all_vms, "source": "live_query",
                "agent_count": len(self.control_plane.connected_agents)}

    async def _search_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        q = (data.get("q") or "").strip().lower()
        all_r = await self._list_vms({})
        return {
            "status":  "SUCCESS",
            "results": [
                {"source": "kvm", "type": vm.get("type", "kvm"), **vm}
                for vm in all_r.get("vms", [])
                if q in (vm.get("name") or "").lower()
                or q in (vm.get("unique_id") or "").lower()
            ],
        }

    async def _get_vm_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane:
            return {"status": "ERROR", "message": "No control plane"}
        agent_id = data.get("agent_id")
        if not agent_id and self.control_plane.connected_agents:
            agent_id = next(iter(self.control_plane.connected_agents))
        if not agent_id:
            return {"status": "ERROR", "message": "No agents connected"}
        return await self.control_plane.send_to_agent("GET_VM_INFO", data, agent_id=agent_id)

    async def _create_vm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "ERROR", "message": "No agents connected"}
        agent_id = data.get("agent_id") or next(iter(self.control_plane.connected_agents))
        return await self.control_plane.send_to_agent("CREATE_VM", data, agent_id=agent_id)

    async def _delete_vm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "ERROR", "message": "No agents connected"}
        agent_id = data.get("agent_id") or next(iter(self.control_plane.connected_agents))
        return await self.control_plane.send_to_agent("DELETE_VM", data, agent_id=agent_id)

    async def get_status(self) -> Dict[str, Any]:
        agent_count = len(self.control_plane.connected_agents) if self.control_plane else 0
        return {
            "spoke_id":    self.spoke_id,
            "module":      "kvm",
            "agent_count": agent_count,
            "status":      "HEALTHY" if agent_count > 0 else "NO_AGENTS",
        }

    def get_version(self) -> str:
        from pathlib import Path
        try:
            return (Path(__file__).parent.parent / "VERSION").read_text().strip()
        except Exception:
            return "unknown"
