# kvm — KVM / Libvirt Compute Spoke (Lab Manager Module)

`kvm` is the Lab Manager hypervisor integration spoke for standalone Linux KVM hosts running `libvirtd` and QEMU. It provides full virtual machine lifecycle control, telemetry aggregation, and resource allocation through the Lab Manager unified WebUI and REST APIs, offering parity with the `pxmx` (Proxmox) module.

---

## Architecture

The `kvm` module implements the standard Lab Manager hub-and-spoke model under `module_type = "hypervisor"`:

```
┌─────────────────┐             WebSocket / TLS (:443)             ┌─────────────────┐
│     LM Hub      │ ◄────────────────────────────────────────────► │    kvm Spoke    │
│  Control Plane  │                                                │ (KVMSpoke / CP) │
└─────────────────┘                                                └────────┬────────┘
                                                                            │
                                                WebSocket / TLS (:443 / :8443)
                                                                            │
                                                       ┌────────────────────┴────────────────────┐
                                                       ▼                                         ▼
                                            ┌─────────────────────┐                   ┌─────────────────────┐
                                            │   kvm Host Agent    │                   │   kvm Host Agent    │
                                            │     (Host Node 1)   │                   │     (Host Node 2)   │
                                            └──────────┬──────────┘                   └──────────┬──────────┘
                                                       │                                         │
                                            ┌──────────┴──────────┐                   ┌──────────┴──────────┐
                                            │   libvirt daemon    │                   │   libvirt daemon    │
                                            │   (qemu:///system)  │                   │   (qemu:///system)  │
                                            └─────────────────────┘                   └─────────────────────┘
```

1. **KVM Spoke Coordinator (`src/kvm_spoke.py`, `src/control_plane.py`):**
   - Dials outbound to the LM Hub control plane (`/ws/spoke` on port 443).
   - Manages connected KVM host agents, serving an agent listener on port 443 or loopback.
   - Caches agent telemetry frames (`_TELEMETRY_FRESH_SECS = 60.0`) for fast dashboard responses, and invalidates cache on mutation commands (`CREATE_VM`, `DELETE_VM`).
   - Normalizes guest domains into canonical LM VM objects:
     - `unique_id`: `<hostname>/<domain_name>`
     - `vmid`: libvirt domain UUID
     - `cluster`: host hostname
     - `type`: `kvm`

2. **Libvirt Operations Engine (`src/kvm_engine.py`):**
   - Connects to local or remote `libvirtd` daemons via `qemu:///system`.
   - Generates and parses Libvirt XML domain definitions (`generate_domain_xml`, `parse_domain_xml`).
   - Executes domain lifecycle primitives: define, start, destroy, and undefine.
   - Configures virtual disk backing (qcow2 images) and virtual network interface bridges (`virbr0` or physical bridge adapters).

---

## Features

- **Domain Lifecycle Management:** Create, define, start, stop/destroy, and undefine KVM/QEMU guest virtual machines.
- **Node Resource Telemetry:** Collects CPU load, memory utilization, and active domain counts across host nodes.
- **Telemetry Caching & Live Queries:** High-speed cached inventory serving with automated fallback to live node queries on stale or mutating states.
- **Scoped VM Search:** Global multi-tenant search filtering domains by name, hostname, UUID, IP address, and MAC address.
- **Virtual Network Bridging:** Bridges domain network interfaces onto host virtual switches (`virbr0`, Open vSwitch, or standard Linux bridges).
- **Virtual Disk Management:** Allocates and manages qcow2 disk images on local host storage pools.

---

## Spoke Commands Reference Table

Commands processed by `src/kvm_spoke.py`:

| Command | Direction | Description |
| :--- | :--- | :--- |
| `GET_VERSION` | Hub → Spoke | Returns current version string from `VERSION`. |
| `UPDATE_CONFIG` | Hub → Spoke | Updates spoke runtime configuration. |
| `GET_AGENTS` | Hub → Spoke | Returns connected KVM host agents with hostname and VM count. |
| `GET_NODE_STATS` | Hub → Spoke → Agents | Broadcasts request for node CPU and memory statistics. |
| `PXMX_LIST_VMS` | Hub → Spoke | Returns aggregated VM domain list across all connected agents. |
| `GET_VM_LIST` | Hub → Spoke | Alias for `PXMX_LIST_VMS`. |
| `SEARCH_VMS` | Hub → Spoke | Multi-criteria search for guest domains with tenant-scoping enforcement. |
| `GET_VM_INFO` | Hub → Spoke → Agent | Queries detailed domain runtime status and XML definition. |
| `CREATE_VM` | Hub → Spoke → Agent | Relays domain creation request to target agent; forces live query on next list. |
| `DELETE_VM` | Hub → Spoke → Agent | Relays domain deletion/undefine request to target agent; forces live query on next list. |

---

## Installation & Setup

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
  | sudo bash -s -- --hub wss://lm-hub.example.com:443
```

Ensure `/etc/lm-kvm/config.json` contains a shared `agent_secret` before starting the agent listener:

```json
{
  "agent_secret": "<shared-random-secret>"
}
```
