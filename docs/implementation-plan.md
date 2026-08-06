# Telegram AI Presentation Bot — Implementation Plan

## 1. Maqsad va delivery chegarasi

Ushbu repository o‘zbek, rus va ingliz tillarida ishlaydigan Telegram bot, FastAPI backend, PostgreSQL/Redis asosidagi fon navbati va PptxGenJS render dvigatelidan iborat production-ready modular monolith bo‘ladi. Credential talab qiluvchi qismlar adapter orqali ajratiladi; lokal development va CI mock AI/payment hamda local storage bilan haqiqiy PPTX/PDF artefakt yaratadi.

## 2. Arxitektura qarorlari

- **Python 3.12** — API, bot, domen, DB va worker image’lari.
- **FastAPI + aiogram 3** — webhook/API va Telegram UX alohida processlarda, bir xil service qatlamidan foydalanadi.
- **SQLAlchemy 2 async + PostgreSQL + Alembic** — transaction va row locking talab qiladigan biznes operatsiyalari.
- **Dramatiq + Redis** — durable background generation, retry va priority queue.
- **PptxGenJS Node worker** — JSON contract asosida editable PPTX yaratadi.
- **LibreOffice headless** — PPTX → PDF eksport; konverter alohida adapter.
- **Storage port** — development’da local filesystem, production’da S3/MinIO.
- **AI port** — deterministic MockAI va OpenAI-compatible HTTP adapter; biznes qatlam providerga bog‘lanmaydi.
- **Payment port** — Mock, Click va Payme adapterlari; summa serverdagi orderdan olinadi, webhook idempotency DB orqali ta’minlanadi.
- **FSM state persistence** — aiogram Redis storage; draft order sozlamalari DBda saqlanib, qayta davom ettiriladi.
- **Localization** — handlerlarda hardcoded user-facing matn yo‘q; `locales/{uz,ru,en}.json`.
- **Observability** — JSON log, correlation/request/order/presentation/task ID, health/readiness endpointlar.

## 3. Modul chegaralari

- `core/`: config, database, logging, security, exceptions.
- `modules/users`: registration, language, profile, deletion.
- `modules/plans`: DB-driven tariffs and slide limits.
- `modules/orders`: order aggregate and state machine.
- `modules/payments`: provider interface, adapters, webhook application service.
- `modules/presentations`: outline approval, generation lifecycle and artifact metadata.
- `modules/ai`: request analysis, outline/content structured contracts and adapters.
- `modules/templates`: direction/style rules and template selection.
- `modules/files`: storage ports, filename sanitization and lifecycle cleanup.
- `modules/promo_codes`, `referrals`, `notifications`, `admin`: bounded support modules.
- `apps/bot`: thin handlers/FSM only.
- `apps/api`: HTTP routes and dependency wiring.
- `apps/worker`: Dramatiq actors and orchestration.
- `apps/presentation_engine`: Node JSON→PPTX renderer plus Python process adapter.

## 4. Muhim invariantlar

1. `telegram_user_id` unique va Free Demo uchun yagona asosiy identifikator.
2. Money faqat integer UZS; float ishlatilmaydi.
3. Order holatlari faqat explicit state machine orqali o‘tadi.
4. Pullik order `PAID` bo‘lmasdan generatsiya navbatiga qo‘yilmaydi.
5. Bir payment event/idempotency key faqat bir marta qo‘llanadi.
6. Bir presentation uchun bir vaqtning o‘zida faqat bitta generation job ishlaydi.
7. User topic/prompt hech qachon system instruction sifatida interpolatsiya qilinmaydi; structured user data sifatida yuboriladi.
8. Free Demo reservation transaction va row lock bilan olinadi; tizim xatosida qayta tiklash audit bilan bajariladi.
9. QA’dan o‘tmagan artefakt foydalanuvchiga yuborilmaydi.
10. Storage key serverda hosil qilinadi; user-supplied path ishlatilmaydi.

## 5. TDD vertikal tracer-bullet ketma-ketligi

1. Plan slide limit validation: failing unit test → implementation.
2. Order state transition: failing unit test → transition engine.
3. Free Demo race-safe claim: failing service/integration test → atomic repository method.
4. Mock payment webhook idempotency: failing integration test → payment service.
5. AI JSON parsing and prompt separation: failing tests → mock/openai adapters.
6. Presentation spec → real PPTX: failing contract test → Node renderer.
7. Generation orchestration → PPTX/PDF/storage: failing E2E test → worker service.
8. Telegram `/start` registration and menu localization: failing handler/service tests → handlers.

## 6. Bosqichlar

### Phase A — Foundation
- Repository, package metadata, config, exceptions, logging, dependency injection.
- Dockerfiles, Compose base services, Makefile, `.env.example`.

### Phase B — Domain + persistence
- Enums, state machine, money/plan validation.
- SQLAlchemy models, indexes, FKs, Alembic initial migration and plan seed.
- Repositories and transactional services.

### Phase C — Interfaces
- FastAPI health/readiness, orders, plans, payment webhooks, files/admin endpoints.
- aiogram onboarding, menu, creation FSM, outline approval, profile/admin views.

### Phase D — Generation
- AI request/outline/content contracts and adapters.
- Design/template selector.
- PptxGenJS renderer, PDF converter, QA, local/S3 storage.
- Dramatiq actors with retry, priority and progress tracking.

### Phase E — Validation and deployment
- Unit, integration and mock E2E suites.
- Ruff, mypy, security/secret scan.
- Docker Compose config validation when Docker is available.
- Full documentation and sample generated artifacts.

## 7. Xavflar va mitigatsiya

- **Real Click/Payme credential yo‘q:** adapterlar fail-closed bo‘ladi; mock provider bilan barcha biznes oqimi testlanadi.
- **Telegram token yo‘q/yoki boshqa bot tomonidan ishlatilmoqda:** token `.env` orqali; import/startup dry-run testlanadi, real polling credential bilan operator tomonidan yoqiladi.
- **Docker lokal hostda yo‘q:** Compose va image build fayllari yaratiladi; Python/Node testlari lokal venv’da bajariladi; Docker tekshiruvi cheklov sifatida qayd etiladi.
- **LibreOffice lokal hostda PATH’da yo‘q:** adapter aniq xato beradi; testda fake converter, qo‘shimcha real smoke test mavjud bo‘lsa DMG yoki container binary ishlatiladi.
- **AI halusinatsiyasi:** structured schemas, source field, deterministic validation va `verification_recommended` flag.
- **PPTX layout overflow:** renderer bounds contract, min-font limit, content splitting va PDF/image QA.

## 8. Definition of Done

- Mock rejimda user/order/payment/outline/generation oqimi real PPTX va PDF artefakt bilan yakunlanadi.
- Free Demo second-use bloklanadi va parallel claim testlanadi.
- Tariff/price/slide limit DBdan olinadi.
- API/bot/worker import va health tekshiruvlari o‘tadi.
- Unit + integration + E2E, Ruff va mypy muvaffaqiyatli.
- Docker/Compose, Alembic, Nginx, docs va secrets-safe `.env.example` mavjud.
- Click/Payme real credential bo‘lmasa ham mock oqim to‘liq ishlaydi; real adapterlar fail-closed va hujjatlashtirilgan.
