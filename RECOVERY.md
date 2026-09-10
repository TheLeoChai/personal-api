# Recovering the live source into this repo

> ✅ **DONE 2026-09-10.** kimaki was added to the docker group, source copied
> from `server-app-1:/app` into `src/`, prod stack captured in
> `docs/prod-stack.md`, schema in `docs/schema.sql`. Kept as the runbook for
> future container-hosted services.

The Personal API container was built outside any git repo and lives in the
root-only Docker stack on the NAS (`kawaiinas`). The `kimaki` user cannot
access the Docker socket, so these steps need **root** (or add `kimaki` to
the `docker` group once — then future maintenance needs no root).

## 1. Capture the current container definition (do this first!)

```bash
CID=$(docker ps --format '{{.Names}} {{.ID}}' | awk '/uvicorn|personal/ {print $2; exit}')
docker inspect "$CID" > /tmp/personal-api-inspect.json
```

Save the relevant bits into `docs/prod-container.json` in this repo: image,
ports (`8081`), env (names only — never values), mounts, restart policy.
This is the deploy reference for "push updates" later.

## 2. Copy the source out

```bash
# find where the code lives inside the container
docker exec "$CID" ls /app
docker cp "$CID":/app ./src
```

## 3. Land it in this repo

```bash
cd /volume1/projects/personal-api
# strip caches/venvs before committing
find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
git add src && git commit -m "Recover live Personal API source from container"
git push
```

## 4. Verify parity

```bash
pip install -r src/requirements.txt   # or equivalent
uvicorn app.main:app --port 8081 &
curl -s localhost:8081/openapi.json | python3 -m json.tool > /tmp/local.json
diff /tmp/local.json <(python3 -m json.tool openapi.json) && echo "contract matches"
```

If the contract differs, the live container is newer than the snapshot —
update `openapi.json` from live and note it.

## 5. Ownership handoff (optional but recommended)

```bash
usermod -aG docker kimaki   # lets the kimaki agent maintain the stack
```

Then rebuild/deploys run from this repo without root (see README → Deploy).
