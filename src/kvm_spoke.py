import logging
import time
from typing import Any, Dict, List

try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke

logger = logging.getLogger("KVMSpoke")

# Serve an agent's telemetry-cached VM list only while its last telemetry frame
# is this fresh; beyond it, live-query that agent instead of serving a snapshot
# from a stalled agent forever. Sized at ~2× the agent telemetry interval so a
# single dropped frame doesn't force a live query, but a genuinely stalled agent
# does. The agent owns the actual cadence (not defined in this repo).
_TELEMETRY_FRESH_SECS = 60.0


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
        # Set after a CREATE_VM/DELETE_VM so the very next _list_vms bypasses the
        # telemetry cache and live-queries — the mutation won't be reflected in
        # the cache until the agent's next telemetry frame arrives.
        self._force_live_query = False

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

    @staticmethod
    def _shape_vm(vm: Dict[str, Any], aid: str, hostname: str) -> Dict[str, Any]:
        name = vm.get("name", "")
        return {
            **vm,
            "agent_id":  aid,
            "cluster":   vm.get("cluster", hostname),
            "unique_id": vm.get("unique_id", f"{hostname}/{name}"),
            "type":      vm.get("type", "kvm"),
        }

    async def _list_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "vms": [], "agent_count": 0}

        tag_filter = (data.get("tag_filter") or "").lower() or None

        # A CREATE_VM/DELETE_VM just landed → the cache is one telemetry frame
        # behind; live-query every agent this once so the mutation shows.
        force_live = self._force_live_query
        self._force_live_query = False

        now = time.time()
        vms: List[Dict] = []
        stale_agents: List[str] = []

        for aid, info in self.control_plane.connected_agents.items():
            hostname = info.get("hostname", aid)
            fresh = (now - info.get("last_seen", 0)) < _TELEMETRY_FRESH_SECS
            cached_vms = info.get("vms", [])
            # Serve the telemetry cache only for a fresh agent (and not when a
            # mutation forced a refresh). A stalled agent — or one with no cache
            # yet — is live-queried so we never serve a frozen snapshot forever.
            if cached_vms and fresh and not force_live:
                for vm in cached_vms:
                    vms.append(self._shape_vm(vm, aid, hostname))
            else:
                stale_agents.append(aid)

        # Live-query the stale / forced agents individually and merge.
        for aid in stale_agents:
            info = self.control_plane.connected_agents.get(aid, {})
            hostname = info.get("hostname", aid)
            try:
                res = await self.control_plane.send_to_agent("GET_VM_LIST", {}, agent_id=aid)
            except Exception as e:
                logger.warning("Live VM query to agent %s failed: %s", aid, e)
                continue
            for vm in res.get("vms", []):
                vms.append(self._shape_vm(vm, aid, hostname))

        if tag_filter:
            vms = [v for v in vms
                   if tag_filter in [t.lower() for t in (v.get("tags") or [])]]

        source = "live_query" if stale_agents else "telemetry_cache"
        return {"status": "SUCCESS", "vms": vms, "source": source,
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
        result = await self.control_plane.send_to_agent("CREATE_VM", data, agent_id=agent_id)
        # Cache is now stale for this mutation — force the next list to live-query.
        self._force_live_query = True
        return result

    async def _delete_vm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "ERROR", "message": "No agents connected"}
        agent_id = data.get("agent_id") or next(iter(self.control_plane.connected_agents))
        result = await self.control_plane.send_to_agent("DELETE_VM", data, agent_id=agent_id)
        # Cache is now stale for this mutation — force the next list to live-query.
        self._force_live_query = True
        return result

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
