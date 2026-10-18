# Podman Deployment for Njira FX API

This directory contains the files needed to deploy the Njira FX API on `server.indemw.tech` using Podman quadlets and the existing nginx gateway.

## Files

| File | Target Path on Host | Purpose |
|------|---------------------|---------|
| `njira-api.container` | `/etc/containers/systemd/njira-api.container` | Systemd quadlet for the API container |
| `gateway-8004.conf.snippet` | Append inside `http { }` of `/opt/apps/gateway/nginx.conf` | Nginx server block for port 8004 |
| `app.env.example` | `/opt/apps/njira-api/app.env` (user creates) | Environment variable template |

---

## Prerequisites (verified by CEO)

- Host: `server.indemw.tech` (129.121.74.200), AlmaLinux 9.8
- Podman 5.8.2 (rootful), 2 vCPU, ~2.4 GiB usable RAM
- Podman network `apps` already exists
- Gateway container `lb` runs `nginx:alpine` on host network, config at `/opt/apps/gateway/nginx.conf`
- Existing `upstream app_pool` already points at `127.0.0.1:9001` and `127.0.0.1:9002`
- Ports 8004, 9001, 9002 confirmed free
- API image `njira-api:local` built and available locally
- Container listens on port 9090 internally, WSGI app `forex.wsgi:application`

---

## Apply Sequence (copy-paste on host)

```bash
# 1. Place the quadlet
sudo mkdir -p /etc/containers/systemd
sudo cp deploy/podman/njira-api.container /etc/containers/systemd/njira-api.container

# 2. Create the app.env from the example (fill in real secrets, mode 600)
sudo mkdir -p /opt/apps/njira-api
sudo cp deploy/podman/app.env.example /opt/apps/njira-api/app.env
sudo chmod 600 /opt/apps/njira-api/app.env
# EDIT /opt/apps/njira-api/app.env with real values

# 3. Append the gateway snippet inside the http { } block
#    (verify the file first, then append)
sudo cp /opt/apps/gateway/nginx.conf /opt/apps/gateway/nginx.conf.bak
cat deploy/podman/gateway-8004.conf.snippet | sudo tee -a /opt/apps/gateway/nginx.conf > /dev/null

# 4. Reload nginx in the gateway container (runs on host network)
sudo podman exec lb nginx -t && sudo podman exec lb nginx -s reload

# 5. Reload systemd and start the API quadlet
sudo systemctl daemon-reload
sudo systemctl enable --now njira-api

# 6. Verify
sudo systemctl status njira-api
journalctl -u njira-api -f
curl -sf http://127.0.0.1:9001/
curl -sf http://indemw.tech:8004/
```

---

## Logs

```bash
# Follow the service journal
journalctl -u njira-api -f

# Last 100 lines
journalctl -u njira-api -n 100 --no-pager
```

---

## Rollback

```bash
# Stop and disable the API quadlet
sudo systemctl disable --now njira-api

# Remove the quadlet
sudo rm /etc/containers/systemd/njira-api.container
sudo systemctl daemon-reload

# Restore gateway nginx config
sudo mv /opt/apps/gateway/nginx.conf.bak /opt/apps/gateway/nginx.conf
sudo podman exec lb nginx -t && sudo podman exec lb nginx -s reload

# Remove app.env (optional, contains secrets)
# sudo rm -rf /opt/apps/njira-api
```

---

## Security Note

**Port 8004 is plain HTTP.** Tokens and credentials travel in clear text between the client and `indemw.tech:8004`. This is acceptable for the initial deployment because:

- The gateway container `lb` is on the host network and only reachable via loopback from the API container
- External access to 8004 goes through the public IP, but TLS is not yet terminated there

**TLS should later terminate on the existing cPanel EA-Nginx 443 listener via AutoSSL.** The pattern is:

1. Create the domain/subdomain in cPanel
2. Add a per-domain nginx include that proxies `https://indemw.tech` → `http://127.0.0.1:8004`
3. cPanel AutoSSL provisions the certificate
4. Reload cPanel nginx

Until that is in place, treat port 8004 as an internal/test endpoint only.