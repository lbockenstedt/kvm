# kvm — KVM / Libvirt Compute Spoke (Lab Manager Module)

The `kvm` spoke module provides hypervisor management for standalone Linux KVM hosts running `libvirtd` and QEMU. Operating under `module_type = "hypervisor"`, it implements the same command surface and telemetry contracts as `pxmx` (Proxmox), allowing the Lab Manager (LM) WebUI and orchestration pipelines to manage KVM virtual machines with zero control-plane friction.

---

## Repository layout

| Path | What it holds |
| :--- | :--- |
| `.claude/` | Fleet-owned skills, synced from lm; edit them in lm. |
| `.github/` | CI (`workflows/ci.yml`), the thin `promote`/`backmerge` callers, promotion scripts and fleet-owned agents. |
| `docs/` | This module's docs (copies of the canonical pages in `lm/docs`). |
| `src/` | The spoke's Python source (entry point, engine, control plane). |
| `tests/` | pytest suite; CI runs it in the `test` job. |
| `AGENTS.md` | Orientation for people and AI agents; the fleet rules block is synced from lm. |
| `install_kvm.sh` | Installer (idempotent; safe to re-run). |
| `README.md` | This file. |
| `VERSION` | Bot-owned version; promotion bumps it. Never edit by hand. |

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

`GET_VM_INFO`, `CREATE_VM` and `DELETE_VM` must name their host: a connected `agent_id`, a `unique_id` or `vm_id` of `<hostname>/<domain>`, or a bare domain name plus `node` (the hostname). The hostname must match exactly one agent, and an `agent_id` must agree with it. There is no "first agent" fallback. The agent is sent the resolved domain name. With `proxmox_tag`, the domain must carry that tag (otherwise "VM not found"). Without it, `DELETE_VM` needs `is_admin` and `CREATE_VM` needs `is_admin` or a tag. `GET_VM_INFO` with no tag is answered, because the hub authorizes it from the `tags` in the reply. `GET_VM_INFO` with `vm_id: "all"` returns every agent's VMs. The live-refresh flag is set only when the agent reports success. VM lists and node stats include an `errors` list naming any agent that failed to answer.

---

## Installation & Setup Instructions

<!-- INSTALLERS:START -->
Installers are idempotent — re-running updates code while preserving credentials.

### KVM Spoke Installation (`install_kvm.sh`)

Run on the spoke coordinator host or container:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
  | sudo bash -s -- --hub lm-hub.example.com
```

| Flag | Description |
| :--- | :--- |
| `--hub HOST\|URL` | LM hub. A bare hostname becomes `wss://<host>:443`. `ws://` is refused. |
| `--id`, `--name` | Unique spoke identifier (default `lm-kvm-<short hostname>`; a re-run keeps the saved one). |
| `--secret` | Pre-shared key for authenticating with the hub (empty: await approval in the WebUI). |
| `--hub-secret` | Optional hub secret for mutual authentication. |

The installer clones the shared LM core into `/opt/lm-kvm/lm-core` and installs
`websockets` and core's requirements into the venv. Secrets are written only to
`/opt/lm-kvm/.env` (mode 600, the unit's `EnvironmentFile`); the service reads
`SPOKE_SECRET` / `HUB_SECRET` from its environment, never from its command line.

### Control Plane Configuration

`/etc/lm-kvm/config.json` (mode 600) holds the agent-listener settings. The
installer creates any missing value and keeps existing ones on a re-run:

```json
{
  "agent_secret": "<64 hex characters, generated once>",
  "agent_tls_cert": "/etc/lm-kvm/agent-tls.crt",
  "agent_tls_key": "/etc/lm-kvm/agent-tls.key"
}
```

The agent listener on `:8767` serves **TLS only** and **fails closed**: with no
loadable cert/key it does not start (agents send `agent_secret` in their first
frame). The installer makes a self-signed cert when none exists at those paths;
put your own cert and key there, or point the two keys elsewhere (a re-run keeps
them). An agent must authenticate within 10 seconds of connecting.

Agent ids are claimed, not proven: every agent shares the one `agent_secret`, so
anything holding it can connect under any id that is **not currently
connected**. While an id's connection is open, a second connection with that id
is refused (close code 1008, "agent_id in use") and logged as a WARNING with
both peer addresses; once the old connection has closed, a reconnect takes its
place. Pings every 20 s close a dead connection within about 40 s, so an agent
whose old socket went half-open may be refused until then. Replies and log
lines are accepted only from the connection a command was sent on. Per-agent
credentials (or mTLS) are a planned follow-up; until then, treat the secret as
granting control of every agent id.
<!-- INSTALLERS:END -->

---

## Testing & Verification

Run the test suite using `pytest`:

```bash
pytest tests
```
