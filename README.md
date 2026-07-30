# kvm — KVM hypervisor (LM module)

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### KVM spoke — `install_kvm.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
  | sudo bash -s -- --hub ws://LM_HUB_IP:8765
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL. **Pass a full `ws://`/`wss://` URL** — unlike the other module installers this one does *not* normalize a bare hostname. |
| `--admin-token` | Deprecated, accepted and ignored. |

This installer takes no environment overrides.
<!-- INSTALLERS:END -->
