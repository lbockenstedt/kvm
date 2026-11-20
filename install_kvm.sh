#!/usr/bin/env bash
# Lab Manager — KVM Spoke Installer v0.01
# Single-line install:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
#     | sudo bash -s -- --hub LM_HUB_HOST [--id ID] [--secret S] [--hub-secret S]
# A bare hub hostname becomes wss://HOST:443. Plain ws:// is refused: the spoke
# secret travels over this link, so it must be encrypted.
set -euo pipefail

REPO_URL="https://github.com/lbockenstedt/kvm.git"
INSTALL_DIR="/opt/lm-kvm"
SERVICE_NAME="lm-kvm"
ENV_FILE="$INSTALL_DIR/.env"
LOG_FILE="/var/log/lm-kvm-install.log"
# Shared LM core (BaseControlPlane, MessageSigner) the spoke imports.
CORE_DIR="$INSTALL_DIR/lm-core"
# Agent-listener config read by control_plane.py, and its TLS cert/key.
CONFIG_DIR="/etc/lm-kvm"
CONFIG_FILE="$CONFIG_DIR/config.json"
AGENT_TLS_CERT="$CONFIG_DIR/agent-tls.crt"
AGENT_TLS_KEY="$CONFIG_DIR/agent-tls.key"

HUB_URL=""
ARG_ID=""
ARG_SECRET=""
ARG_HUB_SECRET=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --hub)         HUB_URL="$2"; shift ;;
        --id|--name)   ARG_ID="$2"; shift ;;
        --secret)      ARG_SECRET="$2"; shift ;;
        --hub-secret)  ARG_HUB_SECRET="$2"; shift ;;
        --admin-token) ;;  # deprecated
        *)             echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "$HUB_URL" ]] && { echo "Usage: $0 --hub <HUB_HOST | wss://HUB:443> [--id ID] [--secret S] [--hub-secret S]"; exit 1; }

# Normalise the hub URL: a bare host means wss on 443; ws:// is refused.
case "$HUB_URL" in
    wss://*) ;;
    ws://*)  echo "❌ Refusing plaintext hub URL $HUB_URL — use wss:// (or a bare hostname)."; exit 1 ;;
    *://*)   echo "❌ Unsupported hub URL scheme: $HUB_URL"; exit 1 ;;
    *:[0-9]*) HUB_URL="wss://${HUB_URL}" ;;
    *)       HUB_URL="wss://${HUB_URL}:443" ;;
esac

echo "$(date) — KVM spoke install started" | tee -a "$LOG_FILE"

# ── Dependencies ──────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip libvirt-clients openssl

# ── Clone / update ────────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "Updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --rebase --autostash
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Shared LM core ────────────────────────────────────────────────────────────
# control_plane.py imports BaseControlPlane / MessageSigner from lm's core;
# without it the service crash-loops on import. Clone once, update on re-run.
if [[ -d "$CORE_DIR/.git" ]]; then
    git -C "$CORE_DIR" pull --ff-only -q || echo "⚠️  Could not update $CORE_DIR; keeping the current copy."
else
    git clone --depth 1 -q https://github.com/lbockenstedt/lm.git "$CORE_DIR"
fi
[[ -f "$CORE_DIR/core/src/messaging/control_plane.py" ]] || { echo "❌ LM core missing at $CORE_DIR."; exit 1; }

# ── Virtual env ───────────────────────────────────────────────────────────────
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
[[ -f requirements.txt ]] && ./venv/bin/pip install -r requirements.txt -q
# websockets serves the agent listener; core's own requirements back its imports.
./venv/bin/pip install -q websockets
[[ -f "$CORE_DIR/core/requirements.txt" ]] && ./venv/bin/pip install -r "$CORE_DIR/core/requirements.txt" -q

# ── Secrets and identity: flags win, else keep what a previous run saved ──────
# Read one KEY= value from the existing env file (empty when absent).
env_value() { [[ -f "$ENV_FILE" ]] && grep "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }

SPOKE_SECRET="${ARG_SECRET:-$(env_value SPOKE_SECRET)}"
HUB_SECRET="${ARG_HUB_SECRET:-$(env_value HUB_SECRET)}"
SPOKE_ID="${ARG_ID:-$(env_value SPOKE_ID)}"
SPOKE_ID="${SPOKE_ID:-${SERVICE_NAME}-$(hostname -s)}"
[[ -z "$SPOKE_SECRET" ]] && echo "ℹ️  No pre-shared secret — spoke will await admin approval in the LM WebUI."

# Secrets live only in this root-only file (the unit's EnvironmentFile); the
# spoke reads them from the environment, never from its command line.
(umask 077; cat > "$ENV_FILE" <<EOF
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
HUB_URL=$HUB_URL
EOF
)
chmod 600 "$ENV_FILE"

# ── Agent listener: TLS cert and agent secret (created once, then kept) ──────
# The listener refuses to start without TLS, since agents send the agent secret
# in their first frame. A self-signed cert is made only when none exists, so an
# operator-supplied cert at the same path survives a reinstall.
mkdir -p "$CONFIG_DIR"; chmod 700 "$CONFIG_DIR"
if [[ ! -s "$AGENT_TLS_CERT" || ! -s "$AGENT_TLS_KEY" ]]; then
    (umask 077; openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$(hostname -f)" \
        -keyout "$AGENT_TLS_KEY" -out "$AGENT_TLS_CERT" 2>/dev/null)
fi
chmod 600 "$AGENT_TLS_KEY"
# Merge the cert paths into config.json and add an agent secret if there is none;
# every other key (and an existing secret) is kept as it is.
CONFIG_FILE="$CONFIG_FILE" AGENT_TLS_CERT="$AGENT_TLS_CERT" AGENT_TLS_KEY="$AGENT_TLS_KEY" python3 - <<'PY'
import json, os, secrets
path = os.environ["CONFIG_FILE"]
try:
    with open(path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
# Fill only missing or empty values: a re-run keeps a custom cert path and the
# existing secret (agents already hold it), but repairs a blank one.
defaults = {"agent_tls_cert": os.environ["AGENT_TLS_CERT"],
            "agent_tls_key": os.environ["AGENT_TLS_KEY"],
            "agent_secret": secrets.token_hex(32)}
for key, value in defaults.items():
    if not str(cfg.get(key) or "").strip():
        cfg[key] = value
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
PY

# ── systemd service ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Lab Manager KVM Spoke
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=$ENV_FILE
Environment="PYTHONPATH=$CORE_DIR:$CORE_DIR/core/src:$INSTALL_DIR/src"
WorkingDirectory=$INSTALL_DIR/src
# Secrets come from EnvironmentFile (SPOKE_SECRET / HUB_SECRET), not argv.
ExecStart=$INSTALL_DIR/venv/bin/python3 control_plane.py --id \$SPOKE_ID --hub \$HUB_URL
Restart=always
RestartSec=10
# The app now owns the canonical /var/log/lm/kvm.log via a Python FileHandler
# (control_plane.py _resolve_log_file — the location the hub scans); stderr goes
# to journald. No redundant append-redirect to /var/log/lm-kvm.log.

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
# restart (not just start) so a re-run picks up new code and config.
systemctl restart "$SERVICE_NAME"

echo "$(date) — KVM spoke installed and started." | tee -a "$LOG_FILE"
echo "Spoke ID: $SPOKE_ID → Hub: $HUB_URL"
