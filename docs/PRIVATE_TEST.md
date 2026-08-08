# Private Telegram test

Bu rejimda botdan faqat bitta Telegram akkaunt — `OWNER_TELEGRAM_ID` egasi foydalana oladi. Boshqa foydalanuvchilarning xabari database, Free Demo, AI yoki generation job oqimiga kirmaydi.

## Kerak bo‘ladigan qiymatlar

1. **Telegram bot tokeni** — BotFather bergan token.
2. **Telegram owner ID** — Telegram akkauntingizning raqamli IDsi.
3. **OpenAI API key** — OpenAI platformasidagi API kalit.

> Kalit va tokenni chatga, Git’ga yoki screenshotga joylamang.

## 1. `.env` faylini tayyorlang

Terminalda loyiha papkasida:

```bash
test -f .env || cp .env.example .env
```

Bu command mavjud `.env` faylini overwrite qilmaydi.

`.env` faylini ochib faqat quyidagi uch qatorni to‘ldiring:

```dotenv
TELEGRAM_BOT_TOKEN=BotFather_tokeningiz
OWNER_TELEGRAM_ID=raqamli_telegram_idingiz
AI_API_KEY=OpenAI_api_keyingiz
```

Quyidagi qiymatlar aynan shunday ekanini tekshiring:

```dotenv
BOT_ENABLED=true
BOT_PRIVATE_MODE=true
AI_PROVIDER=openai
AI_MODEL=gpt-4.1-mini
TELEGRAM_MODE=polling
```

`OWNER_TELEGRAM_ID` faqat raqamlardan iborat bo‘ladi. Uni Telegram’dagi ID ko‘rsatuvchi bot orqali aniqlashingiz mumkin.

## 2. Botni ishga tushiring

Docker Desktop ishlayotgan bo‘lsin. So‘ng loyiha papkasida bitta command bajaring:

```bash
docker compose up --build
```

Terminalda `bot`, `worker`, `outbox`, `api`, `postgres` va `redis` ishlayotganini ko‘ring. Birinchi build bir necha daqiqa davom etishi mumkin.

Test tugagach `BOT_ENABLED=false`ga qaytaring; bu consumer’ni default holatda yopiq saqlaydi.

## 3. Telegram orqali sinang

1. O‘z botingizni Telegram’da oching.
2. `/start` yuboring.
3. **🚀 Yangi prezentatsiya**ni bosing.
4. O‘zbek tilini va kerakli parametrlarni tanlang.
5. Mavzu sifatida quyidagini yuboring:

   **Sun’iy intellektning ta’limdagi imkoniyatlari**

6. `8` slaydni tanlang.
7. **Free Demo**ni tanlang.
8. AI outline’ni ko‘ring.
9. Zarur bo‘lsa **Qayta yaratish** yoki **Tahrirlash**dan foydalaning.
10. **Tasdiqlash**ni bosing.
11. Bitta status xabari `10% → 30% → 60% → 90% → 100%` bo‘lib yangilanadi.
12. Telegram’da `.pptx` va `.pdf` fayllari kelishini kuting.

Private owner test rejimida bu oqim oddiy foydalanuvchining bir martalik Free Demo
entitlementini sarflamaydi; testni qayta bajarish mumkin.

### Telegram delivery semantikasi

Oddiy retry saqlangan Telegram message ID’laridan foydalanib yuborilgan faylni qayta
yubormaydi. Biroq Telegram Bot API idempotency key bermaydi: Telegram faylni qabul qilgan,
lekin process message ID’ni database’ga yozishdan oldin hard-crash bo‘lgan juda tor oynada
bitta artefakt takroran kelishi mumkin. Delivery shu chekka holatda **at-least-once**;
operator duplicate xabarni qo‘lda tekshiradi.

## Xavfsizlik tekshiruvi

Private rejimni tekshirish uchun boshqa Telegram akkauntdan botga `/start` yuboring. Bot private-mode xabarini qaytarishi kerak; AI chaqiruvi, Free Demo claim yoki generation job yaratilmasligi kerak.

## To‘xtatish

Terminalda `Ctrl+C` bosing. Keyin:

```bash
docker compose down
```

Database va yaratilgan fayllar Docker volume’larda saqlanadi.

## Ko‘p uchraydigan muammolar

### `AI_API_KEY is required`

`.env` ichidagi `AI_API_KEY=` qatoriga haqiqiy OpenAI API key yozilmagan.

### `OWNER_TELEGRAM_ID is required`

`.env` ichidagi `OWNER_TELEGRAM_ID=` qatoriga raqamli Telegram ID yozilmagan.

### `Conflict: terminated by other getUpdates request`

Shu bot tokeni bilan boshqa polling process ishlayapti. Eski bot processini to‘xtating va `docker compose up --build`ni qayta bajaring. Bitta token uchun faqat bitta poller ishlashi kerak.

### AI timeout yoki vaqtinchalik xato

Bot friendly xabar ko‘rsatadi. Bir necha soniyadan keyin qayta urinib ko‘ring; texnik traceback faqat server logida qoladi.

Loglarni ko‘rish:

```bash
docker compose logs -f --tail=200 bot worker outbox
```
