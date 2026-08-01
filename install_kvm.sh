#!/usr/bin/env bash
# Lab Manager — KVM Spoke Installer v0.01
# Single-line install:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
#     | sudo bash -s -- --hub ws://LM_HUB_IP:8765
set -euo pipefail

REPO_URL="https://github.com/lbockenstedt/kvm.git"
INSTALL_DIR="/opt/lm-kvm"
SERVICE_NAME="lm-kvm"
ENV_FILE="$INSTALL_DIR/.env"
LOG_FILE="/var/log/lm-kvm-install.log"

HUB_URL=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --hub)         HUB_URL="$2"; shift ;;
        --admin-token) ;;  # deprecated
        *)             echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "$HUB_URL" ]] && { echo "Usage: $0 --hub <ws://HUB_IP:8765>"; exit 1; }

echo "$(date) — KVM spoke install started" | tee -a "$LOG_FILE"

# ── Dependencies ──────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip libvirt-clients

# ── Clone / update ────────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "Updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --rebase --autostash
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Virtual env ───────────────────────────────────────────────────────────────
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
[[ -f requirements.txt ]] && ./venv/bin/pip install -r requirements.txt -q

# ── Preserve or fetch spoke secret ───────────────────────────────────────────
if [[ -f "$ENV_FILE" ]] && grep -q "^SPOKE_SECRET=.\+" "$ENV_FILE"; then
    SPOKE_SECRET=$(grep "^SPOKE_SECRET=" "$ENV_FILE" | cut -d= -f2-)
    echo "Preserving existing SPOKE_SECRET."
else
    SPOKE_SECRET=""
    echo "ℹ️  No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
fi

SPOKE_ID="${SERVICE_NAME}-$(hostname -s)"

cat > "$ENV_FILE" <<EOF
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_URL=$HUB_URL
EOF
chmod 600 "$ENV_FILE"

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
WorkingDirectory=$INSTALL_DIR/src
ExecStart=$INSTALL_DIR/venv/bin/python3 control_plane.py --id \$SPOKE_ID --secret \$SPOKE_SECRET --hub \$HUB_URL
Restart=always
RestartSec=10
# The app now owns the canonical /var/log/lm/kvm.log via a Python FileHandler
# (control_plane.py _resolve_log_file — the location the hub scans); stderr goes
# to journald. No redundant append-redirect to /var/log/lm-kvm.log.

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "$(date) — KVM spoke installed and started." | tee -a "$LOG_FILE"
echo "Spoke ID: $SPOKE_ID → Hub: $HUB_URL"
