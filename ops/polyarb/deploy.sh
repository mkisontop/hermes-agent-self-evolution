#!/usr/bin/env bash
# One-shot provisioning of a fresh Debian/Ubuntu $5 VPS as a polyarb node.
# Run as root. Idempotent-ish. Read polyarb/AUTONOMY.md first — especially
# the jurisdiction and IP-reputation checks that must pass BEFORE deploying.
set -euo pipefail

REPO_URL="${1:?usage: deploy.sh <git-repo-url>}"

echo "== packages =="
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git chrony curl logrotate

echo "== swap (1G, protects against OOM mid-order) =="
if ! swapon --show | grep -q swapfile; then
    fallocate -l 1G /swapfile && chmod 600 /swapfile
    mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -w vm.swappiness=10
fi

echo "== user + dirs =="
id polyarb &>/dev/null || useradd -r -s /usr/sbin/nologin -d /opt/polyarb polyarb
mkdir -p /opt/polyarb /var/lib/polyarb/data /var/log/polyarb /etc/polyarb
chown -R polyarb:polyarb /opt/polyarb /var/lib/polyarb /var/log/polyarb

echo "== code + venv =="
sudo -u polyarb git -C /opt/polyarb clone "$REPO_URL" repo 2>/dev/null \
    || sudo -u polyarb git -C /opt/polyarb/repo pull
sudo -u polyarb python3 -m venv /opt/polyarb/venv
sudo -u polyarb /opt/polyarb/venv/bin/pip install -q --upgrade pip
sudo -u polyarb /opt/polyarb/venv/bin/pip install -q requests websockets
# install the repo itself so `python -m polyarb` resolves (pyproject
# packages polyarb* + evolution*); belt-and-braces with PYTHONPATH in the unit
sudo -u polyarb /opt/polyarb/venv/bin/pip install -q -e /opt/polyarb/repo || true
# live trading additionally needs: pip install py-clob-client-v2

echo "== initial config (human baseline; ceilings live here) =="
if [ ! -f /var/lib/polyarb/polyarb.json ]; then
    sudo -u polyarb env PYTHONPATH=/opt/polyarb/repo \
        /opt/polyarb/venv/bin/python -c "
from polyarb.tuning import TradingConfig, save_config
save_config(TradingConfig(note='human baseline — ceilings are hard'),
            '/var/lib/polyarb/polyarb.json')"
fi

echo "== env file (paper mode; edit to configure alerts / go live) =="
if [ ! -f /etc/polyarb/env ]; then
    cat > /etc/polyarb/env <<'EOF'
# NTFY_TOPIC=polyarb-<long-random-string>
# POLYARB_NIGHTLY_HC=https://hc-ping.com/<uuid>
# --- weekly Hermes reflection (optional, ~2-10 USD/wk) ---
# OPENAI_API_KEY=sk-...
# POLYARB_LLM_MODEL=gpt-5.4
# POLYARB_LLM_BASE_URL=
# --- live trading triple gate (leave commented for paper) ---
# POLYARB_MODE=live
# LIVE_TRADING_ENABLED=true
# DRY_RUN=false
# POLYMARKET_SIGNATURE_TYPE=1
# POLYMARKET_FUNDER=0x...
# NEVER put POLYMARKET_PRIVATE_KEY here — use systemd-creds (AUTONOMY.md)
EOF
    chmod 640 /etc/polyarb/env && chown root:polyarb /etc/polyarb/env
fi

echo "== journald caps =="
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/polyarb.conf <<'EOF'
[Journal]
SystemMaxUse=500M
SystemKeepFree=2G
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald

echo "== logrotate for journals =="
cat > /etc/logrotate.d/polyarb <<'EOF'
/var/lib/polyarb/data/*.jsonl {
    weekly
    compress
    delaycompress
    rotate 12
    missingok
    notifempty
    nocreate
    su polyarb polyarb
}
/var/log/polyarb/*.log {
    weekly
    compress
    rotate 8
    missingok
    notifempty
    su polyarb polyarb
}
EOF

echo "== systemd units =="
cp /opt/polyarb/repo/ops/polyarb/polyarb.service \
   /opt/polyarb/repo/ops/polyarb/polyarb-nightly.service \
   /opt/polyarb/repo/ops/polyarb/polyarb-nightly.timer \
   /opt/polyarb/repo/ops/polyarb/polyarb-reflect.service \
   /opt/polyarb/repo/ops/polyarb/polyarb-reflect.timer \
   /opt/polyarb/repo/ops/polyarb/polyarb-alert@.service \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polyarb.service polyarb-nightly.timer
# weekly LLM reflection: enable only after setting OPENAI_API_KEY in /etc/polyarb/env
# systemctl enable --now polyarb-reflect.timer

echo "== done =="
systemctl --no-pager status polyarb.service | head -8
echo
echo "next steps:"
echo "  1. verify feed: journalctl -u polyarb -f"
echo "  2. set NTFY_TOPIC + healthchecks in /etc/polyarb/env"
echo "  3. run paper for >=2 weeks; review proposals in /var/lib/polyarb/proposals"
echo "  4. read polyarb/AUTONOMY.md before EVER enabling live mode"
