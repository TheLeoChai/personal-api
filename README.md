# personal-api

Source of truth for the **Personal API** — the FastAPI backend live at
**https://api.leochai.com**. Serves content (posts, photo albums, script
runner) for [leochai.com](https://github.com/TheLeoChai/leochai-website).

## Status

- ✅ **Source recovered 2026-09-10** from the live container into [`src/`](src/)
  (see [`RECOVERY.md`](RECOVERY.md) for the history).
- ✅ Contract: [`openapi.json`](openapi.json) matches the live service.

## Architecture

```
                 Cloudflare DNS (DDNS-managed A record)
                          │
                 https://api.leochai.com :443
                          │
                    ┌─────▼─────┐
                    │   Caddy   │ TLS via Cloudflare DNS-01
                    │  /media/* → file_server (uploads)
                    └─────┬─────┘
                          │ http://app:8000
   ┌──────────┬───────────▼───────┬─────────────┐
   │          │                   │             │
┌──▼───┐  ┌───▼────┐   ┌──────────┐  │         ┌───▼────┐
│ app  │  │ postgres│  │  redis   │  │         │ worker │
│:8000 │  │    :5432│  │   :6379  │  │         │ (RQ)   │
└──────┘  └────────┘   └──────────┘  │         └────────┘
  FastAPI     db: personal   RQ broker            runs /app/scripts/*.py
```

Production runs on the NAS as compose project `server` (root-owned
`/home/mihu/Server`) — full reference: [`docs/prod-stack.md`](docs/prod-stack.md).
NAS-wide infra (DNS, DDNS, Caddy, VPN): [leochai-website `docs/infra.md`](https://github.com/TheLeoChai/leochai-website/blob/main/docs/infra.md).

## Code layout

```
src/            FastAPI app (main.py) + SQLAlchemy models + RQ worker entry
src/scripts/    Scripts executable via POST /api/run (bind-mounted in prod)
src/uploads/    Photo storage — bind-mounted in prod, gitignored
openapi.json    API contract snapshot
docs/           prod-stack.md, schema.sql, caddy/Caddyfile reference
```

## API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/posts` | — | list posts |
| GET | `/api/posts/{slug}` | — | read post (`body_md`) |
| POST | `/api/posts` | bearer | create post (`title`, `body_md` form fields) |
| PUT/DELETE | `/api/posts/{slug}` | bearer | update / delete |
| GET/POST | `/api/albums` | — / bearer | list / create album |
| GET/POST | `/api/albums/{id}/photos` | — / bearer | photos (multipart `file` + `caption`); URLs point to `/media/*` |
| POST | `/api/run` | bearer | run `src/scripts/<script>.py` sync or queued (`mode: "queue"`) |

Bearer = `Authorization: Bearer $ADMIN_TOKEN`. Interactive docs:
https://api.leochai.com/docs

## Develop

```bash
cp .env.example .env             # set ADMIN_TOKEN etc.
docker compose up -d --build     # app on :8000, db :5432, worker
open http://localhost:8000/docs
```

- Tables auto-create on startup; a schema snapshot is in
  [`docs/schema.sql`](docs/schema.sql) for reference.
- Frontend (leochai-website) can point at this local stack or the live URL.
- Contract changes: update `openapi.json`
  (`curl -s localhost:8000/openapi.json > openapi.json`) and commit together
  with the code change.

## Deploy (push updates to the live NAS)

The prod compose project is root-owned at `/home/mihu/Server` — it stays the
deployment home; **this repo is the source of truth for the code**.

```bash
# on the NAS (root, or as kimaki with docker group for the build part)
cd /volume1/projects/personal-api && git pull
docker build -t server-app /volume1/projects/personal-api/src    # tag must match prod image name

# recreate the two app containers from the repo image (root:
# the prod compose file at /home/mihu/Server is the cleanest way:
#   cd /home/mihu/Server && docker compose up -d --build app worker
# )

# apply pending migrations to the live DB (see migrations/):
#   migrations/0001_fix_posts_updated_at.sql — required, POST /api/posts
#   is broken on the live DB without it (NotNullViolation on updated_at)
```

- Caddy routing and uploads/scripts bind mounts need **no changes** — source
  and scripts flow through the existing mounts.
- Uploads live in `/home/mihu/Server/app/uploads` on the host — never wipe it.
- After deploying:
  `curl -s https://api.leochai.com/openapi.json | diff - openapi.json`
  should show only intended changes.
- Rollback: `docker tag` previous image before rebuilding; recreate from it.
