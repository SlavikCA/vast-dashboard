# vast.ai Node Status Page

Minimal Python web server that displays host hardware status (via vast.ai API) and
Docker container inventory. Zero dependencies — stdlib only.

## Prerequisites

- Python 3.9+
- Docker (CLI + daemon) if you want the containers section

## Configuration

Set your vast.ai credentials in `server.py` (lines 12–13)

## Docker access

The script runs `docker ps -a`. You do **not** need root — membership in the
`docker` group is sufficient:

```bash
sudo usermod -aG docker $USER
newgrp docker          # apply without logout
```

Verify:

```bash
docker ps
```

## Quick start

```bash
python3 server.py
# → listening on :8080
```

Visit `http://<node-ip>:8080/`.

## Install as a systemd service (auto-start on boot)

```bash
sudo tee /etc/systemd/system/vast-status.service <<'EOF'
[Unit]
Description=vast.ai node dashboard page
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/vast-status/server.py
Restart=always
RestartSec=5
Environment=PORT=8080

[Install]
WantedBy=multi-user.target
EOF
```

Copy the script into place and enable:

```bash
sudo mkdir -p /opt/vast-status
sudo cp server.py /opt/vast-status/
sudo systemctl daemon-reload
sudo systemctl enable --now vast-status
sudo systemctl status vast-status
```

- **User:** set to the user that can talk to Docker (often `root` on vast.ai
  instances, or your own user if it's in the `docker` group).
- **Port:** Vast.ai maps a range of ports to the instance's public IP. Check
  your rental's port mappings and set `PORT` accordingly.

## Endpoints

| Path      | Description                          |
|-----------|--------------------------------------|
| `/`       | HTML status page                     |
| `/health` | Plain-text `ok` for health checks    |
