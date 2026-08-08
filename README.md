# Telegram AI Presentation Bot

Telegram orqali mavzu qabul qilib, AI-generated structured outline, tahrirlanadigan PPTX va mos PDF yaratadigan bot. Generation transactional outbox, Redis/Dramatiq worker, Node.js/PptxGenJS renderer va LibreOffice orqali bajariladi.

## Imkoniyatlar

- `uz`, `ru`, `en` lokalizatsiyasi va aiogram FSM;
- `telegram_user_id` bo‘yicha bir martalik Free Demo;
- official async OpenAI SDK, strict structured outline, timeout va bounded retry;
- default owner-only private test gate;
- outline preview, regenerate va approve;
- PostgreSQL transactional outbox va Redis/Dramatiq worker;
- editable 16:9 PPTX, LibreOffice PDF va structural QA;
- PPTX/PDF’ni Telegramga idempotent yuborish;
- polling yoki authenticated webhook rejimi;
- PostgreSQL, Redis va barcha app processlari uchun Docker Compose.

## Tez ishga tushirish

Talab: Docker Desktop/Engine va Compose plugin.

```bash
test -f .env || cp .env.example .env
```

Command mavjud `.env` faylini overwrite qilmaydi.

`.env` ichida quyidagi credentiallarni kiriting va botni faqat kontrolli test uchun yoqing:

```dotenv
TELEGRAM_BOT_TOKEN=<BotFather tokeni>
OWNER_TELEGRAM_ID=<raqamli Telegram ID>
AI_API_KEY=<API kalit>
BOT_ENABLED=true
```

`.env.example`da safe default sifatida `BOT_ENABLED=false`, `BOT_PRIVATE_MODE=true`,
`AI_PROVIDER=openai` va `AI_MODEL=gpt-4.1-mini` sozlangan. Batafsil, sodda yo‘riqnoma:
[Private Telegram test](docs/PRIVATE_TEST.md).

So‘ng:

```bash
docker compose up --build
```

Boshqa terminalda holatni tekshirish:

```bash
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
BOT_ENABLED=true
TELEGRAM_MODE=polling
```

`BOT_ENABLED=true` bo‘lsa `docker compose up -d` bitta polling consumer’ni ishga
tushiradi. Default `false` holatda bot container healthy-idle turadi va `getUpdates`
chaqirmaydi.

### Webhook — production

```dotenv
BOT_ENABLED=true
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
- [Owner-only private Telegram test](docs/PRIVATE_TEST.md)
- [Backup va restore](docs/backup-restore.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Arxitektura va implementation rejasi](docs/implementation-plan.md)
