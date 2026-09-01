# pravburo-reff-bounty

Сервис начислений агентской программы Правбюро.

Только этот сервис создаёт `Reward` и выполняет переходы
`pending → approved | rejected`. Идемпотентность закреплена уникальным `deal_id`
из общего модуля схемы.

## Common submodule

```bash
git clone --recurse-submodules https://github.com/zadkie1ll/pravburo-reff-bounty.git
git submodule update --init --recursive
```

Собственных миграций у bounty нет.

## Маршруты

- `POST /internal/rewards` — требует `X-Internal-Token`;
- `GET /admin/rewards`;
- `POST /admin/rewards/{reward_id}/decide`;
- `GET /health/live`, `GET /health/ready`.

`SESSION_SECRET` должен совпадать с `pravburo-reff-site`, чтобы административный
маршрут принимал ту же cookie-сессию.

На production сервис работает в host network и слушает только
`127.0.0.1:8041`.

```bash
cp .env.example .env
uv sync
uv run pytest
docker compose up --build -d
```
