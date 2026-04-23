# SQLite to PostgreSQL migration

This branch includes:

- `docker-compose.prod.postgres.yaml`: production override for the existing
  `openwebui` service, adding PostgreSQL 17.9 Bookworm.
- `scripts/migrate-sqlite-to-postgres.sh`: an automated wrapper around
  `open-webui-sqlite-migration`.

The script stops Open WebUI, backs up SQLite, starts PostgreSQL, lets Open WebUI
create the PostgreSQL schema, runs a dry-run, imports the data, validates row
counts, and restarts Open WebUI.

## Production command

Run this from the repository checkout on the production host. Adjust
`/data/container/docker-compose.yml` if your production compose file lives
elsewhere.

```sh
OPEN_WEBUI_POSTGRES_PASSWORD='change-me' \
./scripts/migrate-sqlite-to-postgres.sh \
  -f /data/container/docker-compose.yml \
  -f ./docker-compose.prod.postgres.yaml \
  --sqlite-db /data/container/openwebui_data/webui.db \
  --openwebui-service openwebui \
  --build \
  --yes
```

The PostgreSQL data directory defaults to:

```text
the sibling directory next to openwebui_data, for example
/data/container/openwebui_postgres
```

You can override it with:

```sh
OPEN_WEBUI_POSTGRES_DATA_DIR=/another/path/openwebui_postgres
```

## Dry run

To prepare PostgreSQL and preview the migration without importing rows:

```sh
OPEN_WEBUI_POSTGRES_PASSWORD='change-me' \
./scripts/migrate-sqlite-to-postgres.sh \
  -f /data/container/docker-compose.yml \
  -f ./docker-compose.prod.postgres.yaml \
  --sqlite-db /data/container/openwebui_data/webui.db \
  --openwebui-service openwebui \
  --build \
  --dry-run-only
```

## Local path example

If you test with the downloaded copy on macOS, pass the downloaded compose and
SQLite paths explicitly:

```sh
OPEN_WEBUI_POSTGRES_PASSWORD='change-me' \
./scripts/migrate-sqlite-to-postgres.sh \
  -f /Users/romualdbuisson/Downloads/docker-compose.yml \
  -f ./docker-compose.prod.postgres.yaml \
  --sqlite-db /Users/romualdbuisson/Downloads/data/container/openwebui_data/webui.db \
  --openwebui-service openwebui \
  --build \
  --dry-run-only
```

## Notes

- Keep the generated backup directory until the UI has been checked manually.
- The migration tool truncates PostgreSQL Open WebUI tables before importing.
- The production compose you supplied references `.env`; that file must exist
  next to the base compose file.
- If the PostgreSQL password contains URL-special characters, set
  `MIGRATE_DATABASE_URL` explicitly.
- The `dockernet` network must already exist for the production compose.
