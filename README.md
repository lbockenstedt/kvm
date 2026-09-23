# kvm — KVM / Libvirt Compute Spoke (Lab Manager Module)

The `kvm` spoke module provides hypervisor management for standalone Linux KVM hosts running `libvirtd` and QEMU. Operating under `module_type = "hypervisor"`, it implements the same command surface and telemetry contracts as `pxmx` (Proxmox), allowing the Lab Manager (LM) WebUI and orchestration pipelines to manage KVM virtual machines with zero control-plane friction.

---

## Architecture

`kvm` uses a coordinator-and-agent model to bridge the LM Hub control plane with physical hypervisors:

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

1. **Spoke Coordinator (`src/kvm_spoke.py`, `src/control_plane.py`):**
   - Connects to the central LM Hub over a persistent WebSocket on port 443.
   - Hosts the agent listener service accepting inbound WebSocket connections from KVM host agents.
   - Implements telemetry caching (`_TELEMETRY_FRESH_SECS = 60.0`), serving instant VM lists when agents are reporting cleanly, and automatically triggers live agent queries on cache misses or immediately following mutation operations (`_force_live_query`).
   - Translates domain structures into canonical LM VM records (`<hostname>/<domain_name>` unique ID, UUID for `vmid`).

2. **Libvirt Operations Engine (`src/kvm_engine.py`):**
   - Connects to the hypervisor's local libvirt socket (`qemu:///system`).
   - Manages programmatic Libvirt domain XML generation (`generate_domain_xml`) and XML parsing (`parse_domain_xml`).
   - Executes domain lifecycle operations: defining domains, starting, forcefully destroying, and undefining configurations.
   - Handles virtual disk allocation (qcow2 images) and virtual network bridging (`virbr0`, Open vSwitch, Linux bridge).

---

## Features

- **Domain Lifecycle Management:** Programmatically define, start, stop/destroy, and undefine QEMU/KVM virtual machines.
- **Libvirt XML Parsing & Generation:** Structured generation and parsing of Libvirt XML definitions, extracting CPU, memory, storage devices, and virtual network adapters.
- **Virtual Disk Allocation:** Automates creation and mapping of virtual disks (qcow2 format) within host filesystem storage pools.
- **Virtual Network Interface Bridging:** Configures virtual network interfaces (virtio) attached to host Linux bridges or libvirt virtual networks.
- **High-Performance Telemetry Caching:** Blends cached agent telemetry with adaptive live querying for low-latency WebUI interaction and immediate mutation visibility.
- **Tenant-Scoped VM Search:** Multi-attribute global search across guest domains by name, hostname, UUID, IP address, or MAC address, with strict tenant isolation.

---

## Spoke Commands Reference Table

Commands routed and executed by `src/kvm_spoke.py`:

| Command | Direction / Scope | Description |
| :--- | :--- | :--- |
| `GET_VERSION` | Hub → Spoke | Returns the module version string from `VERSION`. |
| `UPDATE_CONFIG` | Hub → Spoke | Updates spoke configuration and parameters. |
| `GET_AGENTS` | Hub → Spoke | Returns connected KVM host agents with hostname and VM count. |
| `GET_NODE_STATS` | Hub → Spoke → Agents | Broadcasts request for node CPU load, memory usage, and host stats. |
| `PXMX_LIST_VMS` | Hub → Spoke | Returns aggregated VM domain list across all connected agents. |
| `GET_VM_LIST` | Hub → Spoke | Alias for `PXMX_LIST_VMS`. |
| `SEARCH_VMS` | Hub → Spoke | Searches guest domains by name, unique ID, IP, or MAC with tenant-scoping checks. |
| `GET_VM_INFO` | Hub → Spoke → Agent | Queries detailed domain runtime configuration and XML definition. |
| `CREATE_VM` | Hub → Spoke → Agent | Relays domain creation request to target agent and flags cache for live refresh. |
| `DELETE_VM` | Hub → Spoke → Agent | Relays domain destruction/undefine request and flags cache for live refresh. |

---

## Installation & Setup Instructions

<!-- INSTALLERS:START -->
Installers are idempotent — re-running updates code while preserving credentials.

### KVM Spoke Installation (`install_kvm.sh`)

Run on the spoke coordinator host or container:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
  | sudo bash -s -- --hub wss://lm-hub.example.com:443
```

| Flag | Description |
| :--- | :--- |
| `--hub URL` | Full LM Hub WebSocket URL (`wss://<host>:443` or `ws://<host>:8765`). |
| `--id`, `--name` | Unique spoke identifier (defaults to `<hostname>-kvm`). |
| `--secret` | Pre-shared key for authenticating with the hub. |

### Control Plane Configuration

The KVM control plane authenticates host agents with a shared secret. Before starting the service, configure `/etc/lm-kvm/config.json`:

```json
{
  "agent_secret": "replace-with-a-long-random-secret"
}
```

Ensure file permissions are restricted to the service user (`chmod 0600 /etc/lm-kvm/config.json`).
<!-- INSTALLERS:END -->

---

## Testing & Verification

Run the test suite using `pytest`:

```bash
pytest tests
```
