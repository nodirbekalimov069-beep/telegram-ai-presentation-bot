# Telegram AI Presentation Bot

Telegram orqali mavzu qabul qilib, AI-generated structured outline, tahrirlanadigan PPTX va mos PDF yaratadigan bot. Generation transactional outbox, Redis/Dramatiq worker, Node.js/PptxGenJS renderer va LibreOffice orqali bajariladi.

## Imkoniyatlar

- `uz`, `ru`, `en` lokalizatsiyasi va aiogram FSM;
- `telegram_user_id` bo‘yicha bir martalik Free Demo;
- OpenAI-compatible structured JSON outline, timeout va bounded retry;
- outline preview, regenerate va approve;
- PostgreSQL transactional outbox va Redis/Dramatiq worker;
- editable 16:9 PPTX, LibreOffice PDF va structural QA;
- PPTX/PDF’ni Telegramga idempotent yuborish;
- polling yoki authenticated webhook rejimi;
- PostgreSQL, Redis va barcha app processlari uchun Docker Compose.

## Tez ishga tushirish

Talab: Docker Desktop/Engine va Compose plugin.

```bash
cp .env.example .env
```

`.env` ichida kamida quyidagini kiriting:

```dotenv
TELEGRAM_BOT_TOKEN=<BotFather tokeni>
```

Real AI uchun:

```dotenv
AI_PROVIDER=openai_compatible
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=<API kalit>
AI_MODEL=gpt-4.1-mini
```

So‘ng:

```bash
docker compose up -d --build
docker compose ps
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
```

Migration va tarif seed’i `migrate` one-shot service tomonidan API/bot/worker’dan oldin bajariladi. Loglar:

```bash
docker compose logs -f --tail=200 api bot worker outbox
docker compose down
```

> `.env` Git’ga kiritilmaydi. Token va API kalitlarni chat, log yoki image ichiga yozmang.

## Telegram rejimlari

### Polling — lokal/default

```dotenv
TELEGRAM_MODE=polling
```

Standart `docker compose up -d` bot service’ni ishga tushiradi.

### Webhook — production

```dotenv
TELEGRAM_MODE=webhook
PUBLIC_BASE_URL=https://bot.example.com
TELEGRAM_WEBHOOK_SECRET=<kuchli-tasodifiy-qiymat>
```

API webhook’ni startup’da Telegram’ga ro‘yxatdan o‘tkazadi. Alohida polling process kerak emas:

```bash
docker compose up -d --build --scale bot=0
```

`PUBLIC_BASE_URL` tashqi HTTPS reverse proxy/load balancer orqali `api:8000`ga yo‘naltirilishi kerak.

## Asosiy servislar

| Service | Vazifa |
|---|---|
| `postgres` | authoritative relational state |
| `redis` | Dramatiq broker |
| `migrate` | Alembic upgrade va idempotent plan seed |
| `api` | FastAPI health, plans, webhook va provider callbacks |
| `bot` | aiogram long polling |
| `outbox` | PostgreSQL outbox → Dramatiq publish |
| `worker` | outline/payment jobs, PPTX/PDF generation va Telegram delivery |

Persistent volume’lar: `postgres_data`, `redis_data`, `presentation_storage`.

## Development va quality gate

```bash
uv sync --frozen
PYTHONPATH='' uv run pytest -q
uv run ruff check .
uv run ruff format --check .
PYTHONPATH='' uv run mypy apps core modules
npm test --prefix apps/presentation_engine
```

Migration tekshiruvi:

```bash
docker compose run --rm migrate alembic check
docker compose run --rm migrate alembic current
```

## Hujjatlar

- [Production deployment](docs/deployment.md)
- [Backup va restore](docs/backup-restore.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Arxitektura va implementation rejasi](docs/implementation-plan.md)
