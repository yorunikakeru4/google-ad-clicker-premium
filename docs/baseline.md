# Baseline-замер: процедура для стенда

Числа «кликов в час» и частоты капчи на текущем коде — точка отсчёта для
приёмки фазы 12. Снимаются один раз на стенде с реальным Chrome, прокси и
запросами. Харнес подсчёта — `engine/baseline.py`, его арифметика покрыта
тестами (`tests/engine/test_baseline.py`).

## Почему не здесь

На dev-машине замера нет и быть не может:

- нет `queries.txt` / `proxies.txt` / ключа 2captcha — гнать клики не на чем;
- системный Chromium 153 есть, но автокачанный chromedriver падает (status
  127, нет системных библиотек), а `nixpkgs#chromedriver` — версии 148
  против браузера 153 и тоже падает;
- даже если браузер поднять, прямой IP без прокси — нерепрезентативно и
  жжёт адрес.

## Подготовка стенда

1. macOS или Linux с реальным Chrome 153, Python 3.12, `nix develop`.
2. `queries.txt` (10+ запросов), `proxies.txt` (авторизованные, проверенные
   через экран Proxies или вручную), ключ 2captcha в `config.json`.
3. `config.json`: относительные пути (`queries.txt`, `proxies.txt`),
   `browser_count` как в проде (2–3).

## Прогон

1. Зафиксировать время старта (секунды): `date +%s` → `START`.
2. Запустить демон и поднять пул:

   ```bash
   export ADCLICKER_CONTROL_TOKEN=$(openssl rand -hex 16)
   python -m engine.control_plane.daemon &
   curl -X POST -H "X-Auth-Token: $ADCLICKER_CONTROL_TOKEN" \
        -H 'Content-Type: application/json' -d '{"workers": 3}' \
        http://127.0.0.1:8787/control/start
   ```

   Дать отработать фиксированное окно — **не меньше часа**, иначе шум.
3. Остановить штатно (SIGTERM демону, не kill -9, чтобы `clicklogs.db`
   сбросился на диск).
4. Зафиксировать время конца: `date +%s` → `END`.
5. Снять финальную статистику. stdout воркеров супервизор гасит
   (`DEVNULL`), поэтому смотреть нужно в legacy-лог `logs/*.log`: строки
   `Captcha Seen` / `Captcha Solved` у каждого контроллера, просуммировать.
   Если файла нет — капча проходит как «не измерялась», а не как ноль.

## Подсчёт

```bash
nix develop --command python - <<'EOF'
import json
from engine import baseline
from engine.db import migrations

report = baseline.build_report(
    clicklogs_db_path="clicklogs.db",
    started_at=START,   # подставить
    ended_at=END,       # подставить
    captcha_seen=...,   # подставить или опустить
    captcha_solved=..., # подставить или опустить
    notes="stand, N workers, <режим прокси>",
)
Path("baseline.json").write_text(json.dumps(report, indent=2))

migrations.migrate("adclicker.db")
baseline.record_baseline_run("adclicker.db", report)
print(json.dumps(report, indent=2))
EOF
```

## Куда ложатся цифры

- `baseline.json` — рядом с процедурой, коммитится как артефакт замера;
- строка `status='baseline'` в таблице `runs` — прямое сравнение с
  пост-рефакторинговыми прогонами тем же SQL;
- итог вносится в `план.md`, фаза 12: «не хуже baseline по кликам/час,
  капча не чаще».

## Что считается успехом замера

- Окно ≥ 1 часа непрерывной работы без ручных перезапусков.
- `clicks_per_hour` посчитан, `captcha_seen/solved` сняты или явно
  помечены как неизмеренные.
- `baseline.json` и строка в `runs` на месте.
