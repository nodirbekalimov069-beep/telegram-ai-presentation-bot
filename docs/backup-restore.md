# Backup va restore

Backup’lar shifrlangan, access-controlled storage’da va Docker hostdan tashqarida saqlanishi kerak. Quyidagi buyruqlar loyiha root’idan bajariladi.

## Backup

```bash
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T postgres \
  pg_dump -U presentation -d presentation --format=custom \
  > "backups/postgres-${stamp}.dump"
docker compose run --rm --no-deps -T api \
  tar -C /app/storage -czf - . \
  > "backups/artifacts-${stamp}.tar.gz"
shasum -a 256 "backups/postgres-${stamp}.dump" \
  "backups/artifacts-${stamp}.tar.gz" \
  > "backups/${stamp}.sha256"
```

DB dump va artifact archive bir xil timestamp bilan juft saqlanadi. Redis authoritative ma’lumot emas; uning navbati DB outbox va idempotent job state’dan tiklanadi.

## Backup verification

```bash
shasum -a 256 -c "backups/<timestamp>.sha256"
```

Dump katalogini tekshirish:

```bash
docker compose exec -T postgres pg_restore --list \
  < "backups/postgres-<timestamp>.dump" >/dev/null
```

Faqat checksum emas, staging muhitida davriy to‘liq restore test ham o‘tkazing.

## Restore

Yozuvchi service’larni to‘xtating:

```bash
docker compose stop api bot worker outbox
docker compose up -d postgres redis
```

DB’ni restore qiling:

```bash
docker compose exec -T postgres \
  pg_restore -U presentation -d presentation \
  --clean --if-exists --no-owner --no-privileges \
  < "backups/postgres-<timestamp>.dump"
```

Artifact volume’ni restore qiling:

```bash
docker compose run --rm --no-deps -T api \
  sh -ec 'rm -rf /app/storage/* && tar -C /app/storage -xzf -' \
  < "backups/artifacts-<timestamp>.tar.gz"
```

Schema va servislarni tekshirib qayta ishga tushiring:

```bash
docker compose run --rm migrate alembic upgrade head
docker compose up -d
docker compose ps
curl --fail http://localhost:8000/health/ready
```

Webhook production’da bo‘lsa oxirgi buyruqda `--scale bot=0` ishlating.

## Retention tavsiyasi

- daily: 7 nusxa;
- weekly: 4 nusxa;
- monthly: 12 nusxa;
- har backup uchun SHA-256 va off-host nusxa;
- credentiallar va `.env` DB/artifact backup’iga qo‘shilmaydi, secret manager’da alohida saqlanadi.
