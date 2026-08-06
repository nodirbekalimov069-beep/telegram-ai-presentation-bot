# Production deployment

## 1. Host talablari

- Linux host yoki Docker Desktop;
- Docker Engine va Compose plugin;
- kamida 4 CPU, 8 GB RAM va 20 GB bo‘sh disk;
- Telegram webhook ishlatilsa public HTTPS domain;
- muntazam PostgreSQL va artifact volume backup’i.

## 2. Production konfiguratsiyasi

`.env.example`dan `.env` yarating. Quyidagi qiymatlar production’da majburiy:

```dotenv
APP_ENV=production
DEBUG=false
SECRET_KEY=<kamida-32-byte-tasodifiy-qiymat>
POSTGRES_PASSWORD=<kuchli-parol>
PUBLIC_BASE_URL=https://bot.example.com

TELEGRAM_BOT_TOKEN=<BotFather-tokeni>
TELEGRAM_MODE=webhook
TELEGRAM_WEBHOOK_SECRET=<tasodifiy-secret>
TELEGRAM_ADMIN_IDS=<vergul-bilan-numeric-idlar>
ADMIN_API_TOKEN=<tasodifiy-admin-token>

AI_PROVIDER=openai_compatible
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=<api-key>
AI_MODEL=<providerdagi-real-model>

PAYMENT_PROVIDER=click
CLICK_MERCHANT_ID=<merchant-id>
CLICK_SERVICE_ID=<service-id>
CLICK_SECRET_KEY=<secret>
```

Payme tanlansa Click qiymatlari o‘rniga `PAYME_MERCHANT_ID`, `PAYME_LOGIN` va `PAYME_SECRET_KEY` ishlatiladi. Production validator mock payment va default secret bilan startup’ni rad etadi.

Secret yaratish misoli:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`.env` ruxsatlari:

```bash
chmod 600 .env
```

## 3. TLS va reverse proxy

Telegram webhook faqat public HTTPS endpoint bilan ishlaydi. TLS’ni cloud load balancer, Caddy, Traefik yoki Nginx’da terminate qiling va quyidagi path’larni API portiga uzating:

- `/api/v1/telegram/webhook`;
- `/api/v1/payments/*`;
- `/health/live` va `/health/ready`.

Proxy original request body’ni o‘zgartirmasligi va `X-Telegram-Bot-Api-Secret-Token` hamda payment provider headerlarini saqlashi shart. API portini public internetga to‘g‘ridan-to‘g‘ri ochmang.

## 4. Deploy

```bash
docker compose pull postgres redis
docker compose build --pull
docker compose up -d --scale bot=0
docker compose ps
curl --fail https://bot.example.com/health/live
curl --fail https://bot.example.com/health/ready
```

One-shot `migrate` service muvaffaqiyatli tugamaguncha app service’lari start bo‘lmaydi.

## 5. Tekshirish

```bash
docker compose ps
docker compose logs --tail=200 migrate api worker outbox
docker compose exec -T postgres pg_isready -U presentation -d presentation
docker compose exec -T redis redis-cli ping
docker compose run --rm --no-deps api node --version
docker compose run --rm --no-deps api soffice --version
docker compose run --rm --no-deps api alembic current
```

FastAPI docs production’da o‘chiriladi. `/health/ready` PostgreSQL va Redis holatini tekshiradi.

## 6. Yangilash va rollback

Deploy oldidan backup oling. So‘ng:

```bash
docker compose build --pull
docker compose run --rm migrate alembic upgrade head
docker compose up -d --remove-orphans --scale bot=0
```

Rollback faqat migration downgrade xavfsizligi oldindan staging’da tekshirilgan bo‘lsa qilinadi. Aks holda oldingi image’ni yangi schema bilan ishga tushiring yoki DB backup’dan restore qiling.

## 7. Security checklist

- `.env` va backup’lar Git’dan tashqarida;
- Telegram webhook secret va admin token alohida qiymatlar;
- Click/Payme callback URL’lari faqat HTTPS;
- Docker socket app containerlariga mount qilinmagan;
- container non-root `app` user bilan ishlaydi;
- DB/Redis portlari hostga publish qilinmagan;
- image va base image’lar muntazam qayta build qilinadi;
- Telegram va AI credentiallari davriy rotate qilinadi.
