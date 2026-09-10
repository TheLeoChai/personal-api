# Agent instructions

Personal API — FastAPI backend live at https://api.leochai.com, hosted on the
NAS (kawaiinas) as Docker Compose project `server`.

## Structure

- `src/` — the FastAPI app + worker (recovered from the live container; this repo is the source of truth)
- `src/scripts/` — scripts runnable via `POST /api/run`; bind-mounted into prod
- `src/uploads/` — photo storage, bind-mounted into prod. **Never delete or rewrite this folder** — prod photos live there (host path `/home/mihu/Server/app/uploads`)
- `migrations/` — numbered SQL migrations for the live DB; apply in order, note applied date in the file header
- `docs/prod-stack.md` — the authoritative map of the live containers, mounts, env names, deploy state
- `openapi.json` — API contract; update alongside any endpoint change

## Deploy

- Live compose project is root-owned at `/home/mihu/Server` (not readable by
  the `kimaki` user). The `kimaki` user **is** in the docker group, so
  container build/recreate/inspect work without root.
- Deploy flow: edit code here → commit → sync changed files into
  `/home/mihu/Server/app` (via a mount helper container) → `docker build -t server-app src`
  → recreate `server-app-1` (and `server-worker-1` if worker changed),
  preserving env/mounts/`--network-alias app|worker` exactly — see
  `docs/prod-stack.md` for the captured spec and rollback tags.
- Caddy routing never changes for code deploys.

## Rules

- Never commit secrets — env values live in the root `.env` on the NAS; keep `.env.example` updated instead
- Never force-push `main`
- Verify contract parity after any deploy: `curl -s https://api.leochai.com/openapi.json | diff - openapi.json`
- NAS-wide infra (DNS, DDNS, Caddy, VPN) is documented in
  `../leochai-website/docs/infra.md` — keep both sides current
