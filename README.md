# personal-api

Source of truth for the **Personal API** — the FastAPI backend live at
**https://api.leochai.com**. Serves content (posts, photo albums, run log)
for [leochai.com](https://github.com/TheLeoChai/leochai-website).

## Status

- 🔴 **Source code not recovered yet** — the running container predates this
  repo and its code lives inside the root-only Docker stack on the NAS.
  See [`RECOVERY.md`](RECOVERY.md) for the exact extraction steps.
- ✅ **API contract captured** — [`openapi.json`](openapi.json) (snapshot of
  the live service, 2026-09-10). Frontend work can proceed against the live
  URL meanwhile.

## Architecture

```
                    Cloudflare DNS (DDNS-managed A record)
                             │
                    https://api.leochai.com :443
                             │
                       ┌─────▼─────┐
                       │   Caddy   │  TLS (auto Let's Encrypt)
                       └─────┬─────┘
                             │
   ┌─────────────┬───────────▼──────────┬──────────────┐
   │             │                      │              │
┌──▼───┐   ┌────▼────┐   ┌─────────┐   ┌──▼───┐   ┌──────▼─────┐
│  api │   │ postgres│   │  redis  │   │worker│   │ openvpn-as │
│ :8081│   │ (personal)│  │  :6379  │   │      │   │ (separate) │
└──────┘   └─────────┘   └─────────┘   └──────┘   └────────────┘
  FastAPI      db: personal   cache/queue   background   vpn.leochai.com
```

API container runs `uvicorn app.main:app --host 0.0.0.0 --port 8081`
(python 3.11). Full NAS infra: [leochai-website `docs/infra.md`](https://github.com/TheLeoChai/leochai-website/blob/main/docs/infra.md).

## API surface (from live contract)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/posts` | — | list posts |
| POST | `/api/posts` | bearer | create post (`title`, `body_md`) |
| GET/PUT/DELETE | `/api/posts/{slug}` | — / bearer / bearer | read / update / delete post |
| GET/POST | `/api/albums` | — / bearer | list / create album (`title`, `description`) |
| POST | `/api/albums/{album_id}/photos` | bearer | add photo |
| POST | `/api/run` | — | run log entry |

Interactive docs: https://api.leochai.com/docs

## Develop

Local stack via Docker Compose (postgres + redis provided; `api` service
activates once `src/` exists):

```bash
cp .env.example .env          # fill in secrets (never commit .env)
docker compose up -d          # postgres + redis
# after recovery, with src/ present:
docker compose --profile app up --build
uvicorn app.main:app --reload --port 8081   # or run directly
```

- API contract is the source of truth for behavior: keep
  [`openapi.json`](openapi.json) updated when endpoints change
  (`curl -s localhost:8081/openapi.json > openapi.json`).
- Bearer token for write endpoints comes from env — see `.env.example`.

## Deploy (push updates to the NAS)

The live stack is root-owned Docker on `kawaiinas`. Once `src/` is
recovered and this repo is the build context:

```bash
# on the NAS, as root
cd /volume1/projects/personal-api
docker build -t personal-api:latest ./src
# recreate the api container with the same ports/env as today
# (capture current definition first: RECOVERY.md step 1)
```

- Caddy routing needs **no changes** — it proxies to the container port.
- After deploying: `curl -s https://api.leochai.com/openapi.json | diff - openapi.json`
  should show only intended changes.
- Rollback: retag previous image `personal-api:<hash>` and recreate.

## Repository layout (once recovered)

```
src/            FastAPI application (app.main:app)
openapi.json    API contract snapshot
compose files   dev (this repo) + prod definition captured from the NAS
docs/           runbooks, recovery, maintenance notes
```
