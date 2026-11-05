"""Command handler for the KVM hypervisor spoke.

Implements the same hub command contract as the Proxmox spoke (VM listing,
search, info, create, delete, node stats) by relaying commands to libvirt
agents through the KVM control plane and normalising their VM records.
"""
# logging reports problems; time decides whether cached telemetry is fresh.
import logging
import time
# Type hints only.
from typing import Any, Dict, List

# Shared spoke base class; the import path differs between flat and repo layouts.
try:
    from base_spoke import BaseSpoke
except ImportError:
    from core.src.base_spoke import BaseSpoke

# One named logger for this spoke.
logger = logging.getLogger("KVMSpoke")

# Serve an agent's telemetry-cached VM list only while its last telemetry frame
# is this fresh; beyond it, live-query that agent instead of serving a snapshot
# from a stalled agent forever. Sized at ~2× the agent telemetry interval so a
# single dropped frame doesn't force a live query, but a genuinely stalled agent
# does. The agent owns the actual cadence (not defined in this repo).
_TELEMETRY_FRESH_SECS = 60.0


def _norm_mac(mac: str) -> str:
    """Normalize a MAC to lower-colon form (aa:bb:cc:dd:ee:ff).

    The hub normalizes the query `q` to this form before dispatch, so we do
    the same to VM MACs before comparing. Tolerates :, -, ., space and bare
    hex; returns the stripped/lowered input unchanged if it isn't a MAC.
    """
    # Empty input has no MAC.
    if not mac:
        return ""
    # Lower-case and strip every common separator to get bare hex digits.
    raw = mac.strip().lower()
    hexonly = raw.replace(":", "").replace("-", "").replace(".", "").replace(" ", "")
    # Twelve hex digits form a real MAC: re-insert colons every two characters.
    if len(hexonly) == 12 and all(c in "0123456789abcdef" for c in hexonly):
        return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))
    # Not a MAC: return the cleaned input unchanged.
    return raw


def _vm_ips(vm: Dict[str, Any]) -> List[str]:
    """Lowercased IP strings from a VM record's `ips` list (strings or dicts)."""
    # Collected IPs from every entry in the VM's ips list.
    out: List[str] = []
    for entry in (vm.get("ips") or []):
        # Plain strings are IPs already.
        if isinstance(entry, str):
            out.append(entry.lower())
        # Dicts carry the IP under one of several key names, depending on the agent.
        elif isinstance(entry, dict):
            addr = entry.get("address") or entry.get("ip") or entry.get("addr")
            if addr:
                out.append(str(addr).lower())
    # Return all IPs found.
    return out


def _vm_macs(vm: Dict[str, Any]) -> List[str]:
    """Lower-colon MACs from a VM record — top-level `mac`, `interfaces`, and
    any `ips` entry carrying a `mac` (agent shape varies)."""
    # Collected MACs from every place an agent might report one.
    macs: List[str] = []
    # Top-level mac field.
    if vm.get("mac"):
        macs.append(_norm_mac(vm.get("mac")))
    # Interface entries may be dicts with a mac key or bare MAC strings.
    for iface in (vm.get("interfaces") or []):
        if isinstance(iface, dict):
            if iface.get("mac"):
                macs.append(_norm_mac(iface.get("mac")))
        elif isinstance(iface, str):
            macs.append(_norm_mac(iface))
    # Some agents attach a mac to entries in the ips list.
    for entry in (vm.get("ips") or []):
        if isinstance(entry, dict) and entry.get("mac"):
            macs.append(_norm_mac(entry.get("mac")))
    # Return all MACs found.
    return macs


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
        """Initialize KVMSpoke with spoke identity, configuration, and control plane."""
        # Base class stores spoke id and config; keep the control plane for agent access.
        super().__init__(spoke_id, config)
        self.control_plane = control_plane
        # Set after a CREATE_VM/DELETE_VM so the very next _list_vms bypasses the
        # telemetry cache and live-queries — the mutation won't be reflected in
        # the cache until the agent's next telemetry frame arrives.
        self._force_live_query = False

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Route incoming Hub command to the appropriate KVM handler method."""
        # Commands are matched case-insensitively.
        cmd = command_type.upper()

        # GET_VERSION: report this spoke's version.
        if cmd == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        # UPDATE_CONFIG: replace the stored config.
        if cmd == "UPDATE_CONFIG":
            self.config = data
            return {"status": "SUCCESS", "message": "Config updated"}

        # GET_AGENTS: list connected hypervisor agents.
        if cmd == "GET_AGENTS":
            return self._get_agents()

        # GET_NODE_STATS: CPU/RAM stats from every agent.
        if cmd == "GET_NODE_STATS":
            return await self._get_node_stats(data)

        # VM listing (both the Proxmox-compatible and generic command names).
        if cmd in ("PXMX_LIST_VMS", "GET_VM_LIST"):
            return await self._list_vms(data)

        # SEARCH_VMS: tenant-scoped VM search.
        if cmd == "SEARCH_VMS":
            return await self._search_vms(data)

        # GET_VM_INFO: details of one VM.
        if cmd == "GET_VM_INFO":
            return await self._get_vm_info(data)

        # CREATE_VM / DELETE_VM: relay the mutation to an agent.
        if cmd == "CREATE_VM":
            return await self._create_vm(data)

        if cmd == "DELETE_VM":
            return await self._delete_vm(data)

        # Anything else is not supported.
        return {"status": "ERROR", "error": f"Unknown command: {command_type}"}

    def _get_agents(self) -> Dict[str, Any]:
        """Return list of connected KVM hypervisor agents with hostname and VM count."""
        # Without a control plane there are no agents.
        if not self.control_plane:
            return {"status": "SUCCESS", "agents": []}
        # Summarise each connected agent; KVM has no clusters, so the hostname stands in.
        agents = [
            {"agent_id": aid, "hostname": info.get("hostname", aid),
             "cluster_name": info.get("hostname", aid), "vm_count": len(info.get("vms", []))}
            for aid, info in self.control_plane.connected_agents.items()
        ]
        # Return the agent list.
        return {"status": "SUCCESS", "agents": agents}

    async def _get_node_stats(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Broadcast request for node statistics across all connected KVM agents."""
        # No agents connected means no nodes.
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "nodes": []}
        # Ask every agent for its node stats in parallel.
        results = await self.control_plane.broadcast_to_agents("GET_NODE_STATS", {})
        # Flatten the per-agent node lists, tagging each node with its agent id.
        nodes: List[Dict] = []
        for res in results:
            for node in res.get("nodes", []):
                nodes.append({**node, "agent_id": res.get("agent_id", "")})
        # Return the combined node list.
        return {"status": "SUCCESS", "nodes": nodes}

    @staticmethod
    def _shape_vm(vm: Dict[str, Any], aid: str, hostname: str) -> Dict[str, Any]:
        """Normalize VM record dictionary into canonical Lab Manager shape."""
        # The domain name is used to build the default unique id.
        name = vm.get("name", "")
        # Keep every original field and fill in agent id, cluster, unique id and type.
        return {
            **vm,
            "agent_id":  aid,
            "cluster":   vm.get("cluster", hostname),
            "unique_id": vm.get("unique_id", f"{hostname}/{name}"),
            "type":      vm.get("type", "kvm"),
        }

    async def _list_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate VM domains from connected agents using telemetry cache or live query."""
        # No agents connected means no VMs.
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "SUCCESS", "vms": [], "agent_count": 0}

        # Optional tenant tag to filter on (lower-cased; None when absent).
        tag_filter = (data.get("tag_filter") or "").lower() or None

        # A CREATE_VM/DELETE_VM just landed → the cache is one telemetry frame
        # behind; live-query every agent this once so the mutation shows.
        force_live = self._force_live_query
        self._force_live_query = False

        # Current time, the collected VMs and the agents that need a live query.
        now = time.time()
        vms: List[Dict] = []
        stale_agents: List[str] = []

        # Decide per agent whether its cached telemetry can be used.
        for aid, info in self.control_plane.connected_agents.items():
            # Hostname, freshness of its last telemetry and its cached VM list.
            hostname = info.get("hostname", aid)
            fresh = (now - info.get("last_seen", 0)) < _TELEMETRY_FRESH_SECS
            cached_vms = info.get("vms", [])
            # Serve the telemetry cache only for a fresh agent (and not when a
            # mutation forced a refresh). A stalled agent — or one with no cache
            # yet — is live-queried so we never serve a frozen snapshot forever.
            if cached_vms and fresh and not force_live:
                for vm in cached_vms:
                    vms.append(self._shape_vm(vm, aid, hostname))
            # Otherwise mark the agent for a live query.
            else:
                stale_agents.append(aid)

        # Live-query the stale / forced agents individually and merge.
        for aid in stale_agents:
            # Look up the agent's hostname for shaping its VMs.
            info = self.control_plane.connected_agents.get(aid, {})
            hostname = info.get("hostname", aid)
            # Ask the agent directly; skip it (with a warning) if the request fails.
            try:
                res = await self.control_plane.send_to_agent("GET_VM_LIST", {}, agent_id=aid)
            except Exception as e:
                logger.warning("Live VM query to agent %s failed: %s", aid, e)
                continue
            # Add the live VMs in the canonical shape.
            for vm in res.get("vms", []):
                vms.append(self._shape_vm(vm, aid, hostname))

        # Keep only VMs carrying the requested tenant tag.
        if tag_filter:
            vms = [v for v in vms
                   if tag_filter in [t.lower() for t in (v.get("tags") or [])]]

        # Report where the data came from along with the VMs.
        source = "live_query" if stale_agents else "telemetry_cache"
        return {"status": "SUCCESS", "vms": vms, "source": source,
                "agent_count": len(self.control_plane.connected_agents)}

    async def _search_vms(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Search KVM guest domains by name, hostname, unique ID, IP, or MAC."""
        # Normalise the query and read the tenant scope from the request.
        try:
            q = (data.get("q") or "").strip().lower()
            tenant_tag = (data.get("proxmox_tag") or "").strip()
            is_admin = bool(data.get("is_admin"))

            # Spoke-side scoping rule:
            #   tenant tag present  → filter to that tenant
            #   tag absent + admin  → unscoped (all VMs)
            #   tag absent + !admin → EMPTY (never leak another tenant's VMs)
            if not tenant_tag and not is_admin:
                return {"status": "SUCCESS", "results": [], "count": 0}

            # Restrict the VM list to the tenant tag when one is given.
            list_args: Dict[str, Any] = {}
            if tenant_tag:
                list_args["tag_filter"] = tenant_tag
            # Get the (possibly filtered) VM list.
            all_r = await self._list_vms(list_args)
            vms = all_r.get("vms", [])

            # Match each VM against the query and build normalised search results.
            results: List[Dict[str, Any]] = []
            for vm in vms:
                # Searchable fields: name, hostname/node, IPs and MACs.
                name = (vm.get("name") or "").lower()
                hostname = (vm.get("hostname") or vm.get("node") or "").lower()
                ips = _vm_ips(vm)
                macs = _vm_macs(vm)
                # With a query, skip VMs that match none of the searchable fields.
                if q and not (
                    q in name
                    or q in hostname
                    or q in (vm.get("unique_id") or "").lower()
                    or any(q in ip for ip in ips)
                    or any(q in m for m in macs)
                ):
                    continue
                # Emit the VM as a search result.
                results.append({
                    "source":    "kvm",
                    "type":      "vm",
                    "id":        vm.get("vmid") or vm.get("unique_id") or "",
                    "name":      vm.get("name") or "",
                    "ip":        ips[0] if ips else "",
                    "mac":       macs[0] if macs else "",
                    "cluster":   vm.get("cluster") or vm.get("node") or "",
                    "status":    vm.get("status") or "",
                    "unique_id": vm.get("unique_id") or "",
                    "agent_id":  vm.get("agent_id") or "",
                })
            # Return the results and their count.
            return {"status": "SUCCESS", "results": results, "count": len(results)}
        # Any failure: log the traceback and return an ERROR with no results.
        except Exception as e:
            logger.exception("SEARCH_VMS failed")
            return {"status": "ERROR", "message": str(e), "results": []}

    async def _get_vm_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch detailed runtime and XML configuration for a specific VM domain."""
        # Commands need the control plane to reach agents.
        if not self.control_plane:
            return {"status": "ERROR", "message": "No control plane"}
        # Use the requested agent, or the first connected one.
        agent_id = data.get("agent_id")
        if not agent_id and self.control_plane.connected_agents:
            agent_id = next(iter(self.control_plane.connected_agents))
        # No agent available to ask.
        if not agent_id:
            return {"status": "ERROR", "message": "No agents connected"}
        # Relay the request and return the agent's reply.
        return await self.control_plane.send_to_agent("GET_VM_INFO", data, agent_id=agent_id)

    async def _create_vm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Relay VM domain creation command to target KVM agent."""
        # Commands need at least one connected agent.
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "ERROR", "message": "No agents connected"}
        # Use the requested agent, or the first connected one, and relay the create.
        agent_id = data.get("agent_id") or next(iter(self.control_plane.connected_agents))
        result = await self.control_plane.send_to_agent("CREATE_VM", data, agent_id=agent_id)
        # Cache is now stale for this mutation — force the next list to live-query.
        self._force_live_query = True
        return result

    async def _delete_vm(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Relay VM domain destruction/undefinition command to target KVM agent."""
        # Commands need at least one connected agent.
        if not self.control_plane or not self.control_plane.connected_agents:
            return {"status": "ERROR", "message": "No agents connected"}
        # Use the requested agent, or the first connected one, and relay the delete.
        agent_id = data.get("agent_id") or next(iter(self.control_plane.connected_agents))
        result = await self.control_plane.send_to_agent("DELETE_VM", data, agent_id=agent_id)
        # Cache is now stale for this mutation — force the next list to live-query.
        self._force_live_query = True
        return result

    async def get_status(self) -> Dict[str, Any]:
        """Report spoke operational status and connected agent count."""
        # Count connected agents (zero without a control plane).
        agent_count = len(self.control_plane.connected_agents) if self.control_plane else 0
        # HEALTHY when at least one agent is connected.
        return {
            "spoke_id":    self.spoke_id,
            "module":      "kvm",
            "agent_count": agent_count,
            "status":      "HEALTHY" if agent_count > 0 else "NO_AGENTS",
        }

    def get_version(self) -> str:
        """Read and return version string from VERSION file."""
        # Path builds the location of the VERSION file at the repo root.
        from pathlib import Path
        # Read the version; any read error means "unknown".
        try:
            return (Path(__file__).parent.parent / "VERSION").read_text().strip()
        except Exception:
            return "unknown"
