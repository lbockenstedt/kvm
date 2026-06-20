#!/usr/bin/env bash
# Lab Manager — KVM Spoke Installer v1.0.0
# Single-line install:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/kvm/main/install_kvm.sh \
#     | sudo bash -s -- --hub ws://LM_HUB_IP:8765 --admin-token LM_ADMIN_TOKEN
set -euo pipefail

REPO_URL="https://github.com/lbockenstedt/kvm.git"
INSTALL_DIR="/opt/lm-kvm"
SERVICE_NAME="lm-kvm"
ENV_FILE="$INSTALL_DIR/.env"
LOG_FILE="/var/log/lm-kvm-install.log"

HUB_URL=""
ADMIN_TOKEN=""

usage() { echo "Usage: $0 --hub <ws://HUB_IP:8765> --admin-token <TOKEN>"; exit 1; }

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --hub)          HUB_URL="$2";      shift ;;
        --admin-token)  ADMIN_TOKEN="$2";  shift ;;
        *)              usage ;;
    esac
    shift
done

[[ -z "$HUB_URL" || -z "$ADMIN_TOKEN" ]] && usage

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
if [[ -f "$ENV_FILE" ]] && grep -q "^SPOKE_SECRET=" "$ENV_FILE"; then
    SPOKE_SECRET=$(grep "^SPOKE_SECRET=" "$ENV_FILE" | cut -d= -f2-)
    echo "Preserving existing SPOKE_SECRET."
else
    SPOKE_ID="${SERVICE_NAME}-$(hostname -s)"
    HUB_HTTP_URL="${HUB_URL/ws:\/\//http://}"
    HUB_HTTP_URL="${HUB_HTTP_URL/wss:\/\//https://}"
    SPOKE_SECRET=$(curl -sf -X POST "$HUB_HTTP_URL/setup/generate-secret" \
        -H "Content-Type: application/json" \
        -H "X-Admin-Token: $ADMIN_TOKEN" \
        -d "{\"spoke_id\":\"$SPOKE_ID\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")
    echo "Auto-registered spoke. Secret stored."
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
StandardOutput=append:/var/log/lm-kvm.log
StandardError=append:/var/log/lm-kvm.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "$(date) — KVM spoke installed and started." | tee -a "$LOG_FILE"
echo "Spoke ID: $SPOKE_ID → Hub: $HUB_URL"
