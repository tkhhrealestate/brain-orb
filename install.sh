#!/bin/bash
# Installs the BRAIN orb page on this OpenClaw server. Run as root from the repo dir.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)

# 1) systemd service, runs as the openclaw user (reads its ~/.openclaw config at runtime)
cat > /etc/systemd/system/brain-orb.service <<EOF
[Unit]
Description=BRAIN orb (sphere page + ElevenLabs TTS proxy)
After=network-online.target
[Service]
User=openclaw
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/orb.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

# 2) Caddy route: /orb -> the service (inserted before the catch-all handle)
if ! grep -q "handle_path /orb" /etc/caddy/Caddyfile; then
  python3 - <<'PY'
p = "/etc/caddy/Caddyfile"
s = open(p).read()
block = "  handle_path /orb* {\n    reverse_proxy 127.0.0.1:8090\n  }\n"
i = s.index("  handle {")
open(p, "w").write(s[:i] + block + s[i:])
PY
fi

systemctl daemon-reload
systemctl enable --now brain-orb
systemctl reload caddy || systemctl restart caddy
sleep 2
systemctl is-active brain-orb caddy
echo "ORB READY: https://$(cat /root/public-ip | tr . -).sslip.io/orb"
