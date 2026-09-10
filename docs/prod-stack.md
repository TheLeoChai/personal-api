# Production stack reference (NAS, kawaiinas)

The live stack is a Docker Compose project rooted at **`/home/mihu/Server`**
on the NAS (root-owned directory; `kimaki` has docker-group access to the
containers but not to that folder). Captured 2026-09-10.

## Containers (compose project `server`)

| Container | Image | Command / port | Role |
|---|---|---|---|
| `server-app-1` | `server-app` (built) | `uvicorn main:app :8000` | **Personal API** (FastAPI, python 3.12) |
| `server-worker-1` | `server-worker` (built, same context) | `python worker.py` | RQ worker — runs `/app/scripts/*.py` |
| `server-db-1` | `postgres:16` | internal 5432 | DB (user/db `personal`) |
| `server-redis-1` | `redis:7` | internal 6379 | RQ broker |
| `server-caddy-1` | `server-caddy` (built) | `:80`, `:443` (tcp+udp), `:8443` | Reverse proxy / TLS |
| `server-nas-ingest-1` | `server-nas-ingest` (built) | internal `8081` | Separate service — only reachable at `api.leochai.com:8443/xiaoesp/*` |
| `openvpn-as` (standalone) | `openvpn/openvpn-as` | `:943`, `:1194/udp`, `:9444→443` | VPN — see leochai-website `docs/infra.md` |
| `jellyfin-app-1` (standalone) | ugreen jellyfin | `:8899` | Media server |

## Host bind mounts

```
/home/mihu/Server/caddy/Caddyfile → caddy /etc/caddy/Caddyfile
/home/mihu/Server/app/uploads     → caddy /srv/media   (serves https://api.leochai.com/media/*)
/home/mihu/Server/app/uploads     → app   /app/uploads (photo storage)
/home/mihu/Server/app/scripts     → app   /app/scripts (script runner)
```

Docker named volumes: `server_caddy_config`, `server_caddy_data` (TLS certs).

## Caddy

- TLS via **Cloudflare DNS-01** challenge (`dns cloudflare {$CF_API_TOKEN}`),
  so certs work regardless of port reachability.
- Site `{$CADDY_API_DOMAIN}` (443): `/media/*` → file_server from
  `{$CADDY_MEDIA_ROOT}`; everything else → `{$CADDY_API_UPSTREAM}` = `http://app:8000`.
- Site `api.leochai.com:8443`: `/xiaoesp/*` → `nas-ingest:8081`; else 404.
- Reference copy of the Caddyfile: [`caddy/Caddyfile`](caddy/Caddyfile).

## Environment (names only — values live in the root compose .env)

`DOMAIN_BASE`, `DOMAIN_HEAD`, `PUBLIC_BASE`, `CORS_ORIGINS`, `ADMIN_TOKEN`,
`DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`REDIS_URL`, `CADDY_API_DOMAIN`, `CADDY_API_UPSTREAM`, `CADDY_MEDIA_ROOT`,
`CADDY_NAS_DOMAIN`, `CADDY_NAS_UPSTREAM`, `CADDY_ACME_EMAIL`, `CF_API_TOKEN`

## DB schema

Snapshot for local dev: [`schema.sql`](schema.sql) (pg_dump --schema-only,
2026-09-10). Tables: `users`, `posts`, `albums`, `photos`. The app also
auto-creates tables on startup (`Base.metadata.create_all`).
