# AGENTS.md — `kvm`

**KVM hypervisor module.** An alternative hypervisor backend to `pxmx`.

- **Repo:** `github.com/lbockenstedt/kvm`
- **Module type:** `module_type = "hypervisor"`
- **Canonical docs:** [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Layout

`src/kvm_spoke.py` (spoke) + `src/control_plane.py` (hub link). `install_kvm.sh`.

## kvm-specific gotchas

- **Earliest-stage repo in the fleet** (`VERSION 0.03`) — thin compared to its siblings, and there is no dedicated `lm/docs/kvm.md` yet. Expect gaps.
- **Shares `module_type = "hypervisor"` with `pxmx`.** Cross-check hub-side hypervisor handling before changing anything the hub dispatches on.
- **`--hub` needs a full `ws://`/`wss://` URL here** — unlike most module installers, this one does **not** normalise a bare hostname.
- `--admin-token` is deprecated: accepted and ignored.
- The control plane authenticates agents with a shared secret and **fails closed** — see the README's "Required control-plane config".
- No `docs/` or `tests/` directory yet.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
