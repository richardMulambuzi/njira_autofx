# Podman migration plan: server.indemw.tech

Status: plan only. Nothing in this document has been deployed.

## Purpose

Move application workloads on server.indemw.tech from cPanel-managed hosting into Podman containers, without breaking cPanel, WHM, mail, DNS, or the certificates that already run on the host.

## Current state (measured)

| Item | Value |
|---|---|
| Host | server.indemw.tech, 129.121.74.200 |
| OS | AlmaLinux 9.8 |
| CPU | 2 vCPU (AMD EPYC 9J45) |
| RAM | 3.6 GiB total, 2.3 GiB available, swap 2.0 of 4.0 GiB used |
| Disk | 99 GB, 68 GB used, 27 GB free (72%) |
| cPanel/WHM | 11.138.0.15 |
| Web stack | cPanel EA-Nginx 1.31.6 on 80/443 in front of Apache 2.4.68 on 81/444 |
| MySQL | Community 8.0.46 on 127.0.0.1:3306 |
| Podman | 5.8.2 (ea-podman) |
| Accounts | 29 accounts, 60 domains, 1 suspended (justice / mejn.mw) |
| Containers running | redis (127.0.0.1:6379), memcached (0.0.0.0:11211) |

The RAM figure sets the ceiling for this plan. Swap is already half used, so the host is under memory pressure before any new workload is added.

## Target architecture

```
Internet
  -> EA-Nginx (ports 80/443, owned by cPanel, AutoSSL certificates)
       -> gateway + load balancer (nginx container, 127.0.0.1:8080)
            -> app_pool (least_conn, keepalive)
                 -> app1:PORT, app2:PORT, ... (Podman containers, private network)
       -> cPanel PHP sites (unchanged, served by Apache as today)
  Data: MySQL 8 on 127.0.0.1:3306 (reused) + external S3-compatible object storage
```

cPanel stays the front door. It keeps ports 80/443, terminates TLS, renews certificates, and handles mail and DNS. The container layer sits behind it and never competes for those ports.

## Hard rules

1. Never run a second web server that binds 80 or 443. cPanel EA-Nginx owns both. The gateway binds 127.0.0.1:8080 only.
2. Each application container joins the shared `apps` Podman network and is reached by network alias, not by a published host port.
3. Reuse the existing MySQL instance before adding a new database engine.
4. Do not store object storage keys in frontend code. Use server-side environment variables with least-privilege credentials.

## Network and naming

Create one bridge network for application traffic.

```
podman network create apps
```

Give each app container a stable alias on that network, for example `--network apps --network-alias app1`. Do not publish app ports to the host. Only the gateway publishes a port, and only on loopback.

## Gateway and load balancer

The gateway is a single nginx container. It does API routing, request-size limits, rate limiting, timeouts, access logging, and load balancing. There is no separate load balancer process.

```nginx
# /etc/nginx/conf.d/gateway.conf (inside the gateway container)
client_max_body_size 20m;
proxy_read_timeout 60s;

limit_req_zone $binary_remote_addr zone=api:10m rate=20r/s;
limit_conn_zone $binary_remote_addr zone=perip:10m;

upstream app_pool {
    least_conn;
    server app1:3000 max_fails=3 fail_timeout=15s;
    # server app2:3000 max_fails=3 fail_timeout=15s;
    keepalive 32;
}

server {
    listen 8080;

    location /api/ {
        limit_req zone=api burst=40 nodelay;
        limit_conn perip 50;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_next_upstream error timeout http_502 http_503 http_504;
        proxy_pass http://app_pool;
    }

    location /lb-health {
        return 200 "ok\n";
    }
}
```

Because cPanel's nginx passes the real client address in `X-Forwarded-For`, set `set_real_ip_from 127.0.0.1` and `real_ip_header X-Forwarded-For` in the gateway so the rate limits key on the true client IP rather than the proxy.

Authentication is not delegated to nginx. nginx has no built-in JWT or session logic. Applications enforce auth, or the gateway calls an `auth_request` endpoint that an application exposes. Do not pretend the gateway provides auth on its own.

## Data and storage

Reuse the existing MySQL 8 instance at 127.0.0.1:3306. A container reaches it through the host loopback. Adding a second database engine costs RAM the host does not have.

For uploads, generated documents, and media, use an external S3-compatible service such as Cloudflare R2, Backblaze B2, Wasabi, or a MinIO instance off the box. Keep buckets private by default. Use dedicated least-privilege credentials. Test a restore before trusting the backup. Do not keep access keys in frontend code.

Persistent container data lives under `/opt/apps/<name>/` on the host, mounted into the container. Databases are backed up with `mysqldump` on a schedule.

## Container template

Run application containers as systemd quadlets so they start on boot and restart on failure.

```ini
# /etc/containers/systemd/<name>.container
[Unit]
Description=<name> app

[Container]
Image=your-image:tag
ContainerName=<name>
Network=apps
NetworkAlias=<name>
Volume=/opt/apps/<name>/data:/app/data:Z
EnvironmentFile=/opt/apps/<name>/app.env
PodmanArgs=--memory 384m --cpus 0.5

[Install]
WantedBy=multi-user.target
```

Pin the size cap per container. 384 MB memory and 0.5 CPU is a working starting point for a small app. Raise it only after you measure, because the host has 2 vCPUs and 2.3 GiB free.

## cPanel integration

This is the one unverified step. cPanel regenerates its nginx configuration, so a custom proxy block must live in a per-domain include that survives regeneration. Confirm the exact include path for this cPanel version before building anything.

The pattern is: create the domain or subdomain in cPanel, add a per-domain nginx include that proxies to the gateway, then reload nginx.

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

```
systemctl reload nginx
```

## Resource budget

| Component | Estimated RAM |
|---|---|
| Gateway nginx container | 15 to 25 MB |
| MySQL 8 (reused) | 0 new |
| Redis (existing) | 0 new |
| Each small app container | 150 to 250 MB |

Keep at least 512 MB free at all times. On this host that allows roughly 5 to 6 small applications. The first open decision is whether to upgrade RAM to 8 GB, which lifts that limit, or to cap the host at about 5 apps.

## Migration order

1. Foundation: bind memcached to 127.0.0.1 instead of 0.0.0.0, and verify the cPanel nginx include path.
2. Pilot: containerize one low-risk application and route it through the gateway.
3. Platform: create the `apps` network, deploy the gateway, and wire the cPanel include.
4. Batch: move one application per day, keeping the cPanel version available as a fallback.
5. Decommission: empty and remove cPanel accounts that no longer host anything.

## Verification

```
podman ps
curl -sf http://127.0.0.1:8080/lb-health
# spread check: send 20 requests and print which backend served each
for i in $(seq 20); do curl -s -o /dev/null -w "%{remote_ip}\n" http://127.0.0.1:8080/api/; done
```

## Rollback

Stop and remove the gateway container, then remove the cPanel proxy include and reload nginx. Traffic returns to the original cPanel route. Perform the rollback test before the first real migration so the path is proven.

## Backup

Back up container volumes and dump each database off the server on a schedule. Store object storage credentials separately from the repositories. Test a restore end to end.

## Open decisions

1. Upgrade RAM to 8 GB, or cap the host at about 5 apps.
2. Reuse shared MySQL 8, or add PostgreSQL for applications that need it.
3. Pick the pilot application.

## Not in scope

Kubernetes, Kong, Traefik, a full observability stack, and containerizing mail or DNS. Each adds memory or support surface the host cannot spare, and cPanel already covers mail and DNS.

## Pre-build checks

1. Confirm the cPanel per-domain nginx include path that survives EA regeneration.
2. Confirm the first app image, its port, and whether it needs MySQL or PostgreSQL.
3. Decide the RAM question.
