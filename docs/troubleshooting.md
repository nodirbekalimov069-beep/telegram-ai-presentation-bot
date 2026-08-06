# Troubleshooting

## Umumiy diagnostika

```bash
docker compose ps
docker compose logs --tail=200 migrate api bot worker outbox postgres redis
docker compose config --quiet
curl -v http://localhost:8000/health/live
curl -v http://localhost:8000/health/ready
```

## Docker Desktop “Rosetta installation failed”

Apple Silicon’da ARM64 image’lar uchun Rosetta shart emas. Docker error oynasida **Disable Rosetta** ni tanlang. So‘ng Docker Desktop’ni restart qiling va tekshiring:

```bash
docker info
docker compose version
```

Intel/amd64-only image kerak bo‘lsa Rosetta’ni macOS System Settings orqali keyinroq qayta yoqish mumkin.

## `migrate` service xato bilan tugadi

```bash
docker compose logs migrate
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic check
```

Migration xatosini chetlab API’ni majburan ishga tushirmang. DB backup oling, migration’ni staging’da tuzating va qayta deploy qiling.

## API ready emas

`/health/live` 200, `/health/ready` 503 bo‘lsa dependency muammosi mavjud:

```bash
docker compose exec -T postgres pg_isready -U presentation -d presentation
docker compose exec -T redis redis-cli ping
docker compose logs api postgres redis
```

`POSTGRES_PASSWORD` o‘zgartirilgan bo‘lsa mavjud volume eski parol bilan qolishi mumkin. Data kerak bo‘lsa parolni eski qiymatga qaytaring yoki backup/restore orqali yangi volume yarating; volume’ni ko‘r-ko‘rona o‘chirmang.

## Bot start bo‘lmaydi

```bash
docker compose logs bot
```

Tekshiring:

- `TELEGRAM_BOT_TOKEN` haqiqiy BotFather tokenimi;
- polling rejimida shu token bilan boshqa process ishlamayaptimi;
- webhook rejimida `bot` scale `0` qilinganmi;
- Telegram API hostga outbound HTTPS ruxsat berilganmi.

Tokenni log yoki command line’ga chiqarmang.

## Webhook update kelmaydi

- `PUBLIC_BASE_URL` public HTTPS bo‘lishi kerak;
- proxy secret-token headerini saqlashi kerak;
- `TELEGRAM_WEBHOOK_SECRET` API va Telegram registration’da bir xil;
- polling container to‘xtatilgan bo‘lishi kerak.

```bash
docker compose logs api
docker compose up -d --scale bot=0
```

## AI 401/404/429/5xx

- 401: API key yoki provider auth noto‘g‘ri;
- 404: `AI_BASE_URL` yoki `AI_MODEL` provider katalogiga mos emas;
- 429/5xx: adapter bounded exponential retry qiladi; davomiy bo‘lsa quota/status page’ni tekshiring;
- JSON contract error: model structured JSON talabiga mos javob bermadi.

Real model nomini placeholder bilan qoldirmang.

## Worker task olmayapti

```bash
docker compose logs worker outbox redis
docker compose exec -T redis redis-cli ping
```

Outbox eventlar PostgreSQL’da authoritative. Dispatcher qayta ishga tushirilganda pending eventlar qayta publish qilinadi; duplicate publish worker idempotency bilan yutiladi.

## PPTX yaraldi, PDF yaralmadi

```bash
docker compose run --rm --no-deps api soffice --version
docker compose logs worker
```

Ko‘p uchraydigan sabablar: LibreOffice timeout, corrupt PPTX yoki storage permission. Container ichida `/app/tmp/generation` va `/app/storage` `app` user uchun writable bo‘lishi kerak.

## Telegram faylni qabul qilmayapti

- artifact `MAX_UPLOAD_BYTES` limitidan kichikmi;
- bot chatga document yuborish huquqiga egami;
- user botni bloklamaganmi;
- `presentation.delivery_status` va message ID’lar DB’da saqlanganmi.

Delivery lease/retry sabab worker restart’dan keyin xavfsiz qayta urinadi.

## Toza lokal reset

Bu buyruq PostgreSQL, Redis va barcha artifactlarni **butunlay o‘chiradi**. Faqat backupdan keyin va data kerak emasligi aniq bo‘lsa:

```bash
docker compose down -v
```
