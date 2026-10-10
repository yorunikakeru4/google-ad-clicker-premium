# plan.md — рабочий план (обновлять инкрементально, не переписывать)

> Правило: этот файл — единственный постоянный контекст между сессиями.
> Новые детали — добавлять/уточнять пункты и секцию §8 (журнал), не выбрасывать решённое.

---

## 0. Статус

- [x] Фикс гонки `thread.start()` в `engine/control_plane/daemon.py::_on_signal`
      (порядок «ссылка → start → флаг»); закоммичено в `609856f`; тесты
      `TestOwnerWatch` + `TestDaemonLifecycle` прошли 15×15 повторов.
- [x] **Задача экспорта в PostgreSQL ВЫПОЛНЕНА (2026-10-07), изменения НЕ закоммичены** —
      §9–§16: постоянный экспорт логов/метрик/событий/справочников из локальной
      SQLite в внешнюю PostgreSQL; docker-compose.yml + init.sql в корне; 3 агента.
- [x] **Задача профилей ВЫПОЛНЕНА (2026-10-06), изменения НЕ закоммичены** (16 файлов,
      см. §0.1): 3 агента отработали в worktrees, патчи применены к develop,
      интеграция + e2e зелёные. Worktrees/ветки удалены.
  - [x] UA-импорт (формат §1, имена UA-N)
  - [x] «Удалить все» (backend + API-режимы + UI)
  - [x] «Импорт из user_agents.txt» (`{"file": true}`)
  - [x] FIX A: `formatProblems` в `ui/src/lib/format.ts`, применён в
        `profiles.ts` + `proxies.ts` (pickChangeResult/pickDeleteResult)
  - [x] FIX B: `{"deleted": 1}` вместо `{"deleted": true}`
  - [x] Интеграция: ruff OK, pytest 241 passed, vitest 177 passed,
        `vue-tsc --noEmit` exit 0
  - [x] E2E `/tmp/opencode/e2e_profiles.py` — 23/23 проверки, код демона 0

### 0.1 Изменённые файлы (ждут коммита)

```
engine/control_plane/api.py              engine/profile_pool.py
tests/engine/control_plane/test_api_profiles.py
tests/engine/test_profile_pool.py
ui/src/lib/{control,profiles,proxies,format}.ts (+ их .test.ts)
ui/src/composables/useProfiles.ts (+ .test.ts)
ui/src/views/ProfilesView.vue
ui/src/views/screens.test.ts
+ plan.md (новый, untracked)
```

### 0.2 Решения e2e-скрипта (приняты при отладке)

- config в tempdir: `behavior.query` НЕ задавать вместе с `paths.query_file`
  (взаимоисключающие поля, ConfigError), пути в конфиге — абсолютные,
  `PYTHONPATH=<repo>` обязателен (cwd демона = tempdir).
- Проверка таба в e2e: строка `UA\tколонка2` (первая колонка непустая) —
  срез по табу идёт до strip, строка вида `\tUA` даёт «строка пустая».
- Порт 8791.


### Хронология / что выяснено ранее (не переспрашивать)

- Легаси xfail-тесты (5 шт.) — известные баги legacy, НЕ трогать в этой задаче.
- `create_webdriver` работает локально и с прокси (проверено: Chrome 154 поднялся
  и напрямую, и через `pr.oxylabs.io:7777`).
- **Диагноз «браузер не запускает» (заказчик)**: это НЕ баг запуска Chrome.
  `ad_clicker.py:206-230` — гейт `probe_proxy_captcha()` ДО `create_webdriver`:
  прокси молчит/5xx → `None` → раунд пропускается, Chrome не поднимается (by design).
  В логах заказчика: `proxy captcha probe did not complete {ProxyError}` +
  `proxy rotation postponed ... no free alive proxy`. Прокси мёртвые:
  `p.webshare.io:80` → `402 Payment Required / X-Webshare-Reason: bandwidthlimit`
  (баквенд исчерпан). Оксибакс (`proxies.txt`, 2 строки) — рабочие (probe → False).
  В БД заказчика `proxies` пуста (0 строк) — пул не импортирован.
  Следствие: починка = рабочие прокси + импорт в пул, код гейта править не надо.
- `logs/adclicker.log` локальный (до 06-10 12:01) к диagnostике заказчика не относится.

---

## 1. Объём текущей задачи (решения приняты)

1. **Импорт строк вида `Mozilla/5.0 ...` → колонка `user_agent` профиля.**
   Формат-источник — `user_agents.txt` (53 строки, все начинаются с `Mozilla/`,
   без табов). Имя профиля — **порядковый номер** `UA-1`, `UA-2`, … (выбрано).
   - Строка режется по первому `\t` (первая колонка) — терпит вставку из таблиц.
   - `|` как раньше: `UA | явное имя` → явное имя имеет приоритет.
   - `key_ref` у UA-строк остаётся NULL; колонка «Ключ» покажет «—».
   - Дедуп UA-строк — по `user_agent` (БД + пачка), не по key_ref.
2. **Кнопка «Удалить все» для профилей** (аналог `proxies-delete-all`):
   бэкенд `ProfilePool.delete_many/all_ids/delete_all`, API-режимы
   `{"id"}` | `{"ids":[...]}` | `{"all":true}`, UI-кнопка + confirm + алерт отчёта.
   Занятые живым воркером → `skipped` + `problems` (best-effort, как у прокси).
3. **Кнопка «Импорт из user_agents.txt»** (аналог «Импорт из proxies.txt»):
   `POST /control/profiles/import` с телом `{"file": true}` → читает
   `paths.user_agents` из конфига. Новый путь URL не нужен (Rust allowlist
   принимает только сегменты из a-z, а существующий `/control/profiles/import` уже разрешён).
4. **Багфикс A (UI теряет причины пропуска)**: демон шлёт
   `problems: [{line_index, message}]` (import) / `[{index, message}]` (add),
   а `pickChangeResult` в `profiles.ts` и `proxies.ts` фильтрует только строки →
   причины молча выбрасываются («пропущено 3» без объяснения). Фикс — нормализация
   в UI (контракт демона не менять, иначе поедут python-тесты).
   Wordlist и delete-problems уже строки — не трогать.
5. **Багфикс B (одиночное удаление профиля)**: демон отдаёт `{"deleted": true}`
   (`api.py:670`), а UI (`pickNumberField`) ждёт число → ошибка в интерфейсе после
   успешного удаления. Фикс на стороне демона: `{"deleted": 1}` (счётчик, как у
   прокси, `api.py:457`). Обновить 2 assert'а в `test_api_profiles.py` (строки ~405, ~430).

**Не входит**: legacy xfail-баги; миграция уже испорченных строк (name=key_ref=UA) —
их вычистит сам оператор новой кнопкой «Удалить все» и завезёт заново; tab-колонки
типа `name\tkey_ref` (только первая колонка); правка гейта проб; Rust/tauri;
`GET /control/profiles/file` (открывалка файла) — не просили.

---

## 2. Разбивка на 3 независимых агента (слойная, файлы не пересекаются)

Каждый агент работает в своём git worktree, **не коммитит**, правит ТОЛЬКО свои файлы,
запускает ТОЛЬКО затронутые им тест-файлы (не весь suite, не e2e, не cargo, не vue-tsc).

| | Агент A — Backend (Python) | Агент B — TS API-слой | Агент C — UI-экран |
|---|---|---|---|
| worktree | `.worktrees/agent-backend` | `.worktrees/agent-tsapi` | `.worktrees/agent-ui` |
| ветка | `agent/backend` | `agent/tsapi` | `agent/ui` |
| файлы | `engine/profile_pool.py`, `engine/control_plane/api.py`, `tests/engine/test_profile_pool.py`, `tests/engine/control_plane/test_api_profiles.py` | `ui/src/lib/control.ts`, `ui/src/lib/profiles.ts`, `ui/src/lib/proxies.ts`, `ui/src/lib/format.ts` + их `.test.ts` | `ui/src/composables/useProfiles.ts` (+`.test.ts`), `ui/src/views/ProfilesView.vue`, `ui/src/views/screens.test.ts` |
| тесты (запуск) | `nix develop -c python -m pytest tests/engine/test_profile_pool.py tests/engine/control_plane/test_api_profiles.py -q` + `nix develop -c ruff check engine tests` | `cd ui && npx vitest run src/lib/control.test.ts src/lib/profiles.test.ts src/lib/proxies.test.ts src/lib/format.test.ts` | `cd ui && npx vitest run src/composables/useProfiles.test.ts src/views/screens.test.ts` |

Зависимость C→B **контрактная** (сигнатуры заданы в §3): C импортирует типы через
`import type`, реальные методы API в тестах подменяет фейком — поэтому C зелёный и
в своём worktree. vue-tsc/typecheck гоняется ТОЛЬКО на интеграции (§7).

---

## 3. Контракты (единый источник правды для всех трёх агентов)

### 3.1 HTTP (реализует A, кодирует B, зовёт C)

1. `POST /control/profiles/import` — тело:
   - `{"lines": [...]}` — как сейчас (строки формата §1);
   - `{"file": true}` — **новое**: читает `config.paths.user_agents`
     (дефолт `user_agents.txt`), пути резолвятся от cwd демона (как у прокси);
     путь пуст/файл не найден/не читается → **400 `profile_import_failed`**
     (новый `ProfileImportError(ProfileError)` + запись в `_ERROR_STATUS`).
   Ответ в обоих случаях: `{added:int, skipped:int, problems:[{line_index:int, message:str}]}`.
2. `POST /control/profiles/delete` — тело (парсер общий с прокси; сейчас
   `_requested_proxy_ids` в `api.py:847`, переименовать в `_requested_delete_ids`
   и использовать обоими хендлерами):
   - `{"id": n}` → 200 `{"deleted": 1}` (**FIX B: было `true`**);
     404 `profile_not_found`, 409 `profile_in_use` — без изменений;
   - `{"ids": [..]}` → 200 `{deleted:int, skipped:int, problems:[str]}`;
   - `{"all": true}` → 200 `{deleted:int, skipped:int, problems:[str]}`;
   - иное тело → 400 `invalid_request`; `all` проверяется первым.
   `problems` — **строки**, формат как у прокси: `id=5: назначен воркеру br-1`,
   `id=5: профиль не найден`. Пустой пул/пустой `all` → no-op с нулями.
3. Формат строки импорта (§1): `strip → split("\t")[0] → partition("|")`;
   левая часть начинается с `Mozilla/` → UA-строка (`user_agent=левая`,
   `key_ref=NULL`, имя = явное либо следующий свободный `UA-N`:
   `n = 1 + max(суффикс существующих имён UA-\d+)`, `while name_exists(UA-n): n+=1`,
   инкремент на каждую вставленную строку); иначе — как сейчас key_ref.
   Дедуп UA — `_user_agent_exists` + seen-множество пачки, проблема
   `дубликат: user_agent уже есть` (без полного UA в тексте).
4. Успешный одиночный delete: `{"deleted": 1}` (FIX B).

### 3.2 TS API-слой (реализует B, использует C)

```ts
// control.ts
export type ProfilesAction =
  | ...существующие
  | { kind: "deleteMany"; ids: number[] }   // POST /control/profiles/delete {"ids":[..]}
  | { kind: "deleteAll" }                   // POST /control/profiles/delete {"all":true}
  | { kind: "importFile" };                 // POST /control/profiles/import {"file":true}

// profiles.ts
export interface ProfileDeleteResult { deleted: number; skipped: number; problems: string[] }
export interface ProfilesApi {
  ...существующие,
  removeMany(ids: number[]): Promise<ProfileDeleteResult>;
  removeAll(): Promise<ProfileDeleteResult>;
  importFile(): Promise<ProfileChangeResult>;
}

// format.ts — FIX A (общий для profiles.ts и proxies.ts)
export function formatProblems(raw: unknown[]): string[];
// строка → как есть; {line_index, message} → `строка ${line_index+1}: ${message}`;
// {index, message} → `запись ${index+1}: ${message}`; прочее → отбросить.
// Применить внутри pickChangeResult в profiles.ts и proxies.ts
// (тип problems остаётся string[]).
```

### 3.3 Composable + view (реализует C)

- `useProfiles`: новые поля `deleteResult: Ref<ProfileDeleteResult | null>`,
  методы `removeMany(ids)`, `removeAll()` (pending `"delete"`),
  `importFile()` (pending `"import"`, кладёт результат в `importResult`).
  Типы импортировать только через `import type`.
- `ProfilesView.vue`:
  - кнопка `profiles-delete-all` «Удалить все (N)» (disabled при 0 строк),
    `ConfirmDialog` `profiles-delete-all-dialog`, текст:
    «Из списка уйдут N профилей. Назначенные живому воркеру останутся — они
    попадут в отчёт под таблицей; чтобы удалить и их, сначала нажмите
    „Сбросить назначения".»;
  - алерт `profiles-delete-result` над таблицей: «Удалено X, пропущено Y» + список problems;
  - кнопка `profiles-import-file` «Импорт из user_agents.txt» (pending «import»);
    результат — верхний алерт `profiles-import-result` на `importResult`
    (диалоговый алерт остаётся; `openImport` по-прежнему чистит `importResult`);
  - диалог импорта: label «User-Agent или key_ref, по одному в строке»,
    hint «Одна строка = один User-Agent (Mozilla/…) или key_ref; "значение | имя"
    задаёт имя; пустые строки и # — комментарии»;
  - empty-state (2 места): «Создайте профиль вручную или импортируйте список
    User-Agent/key_ref».
- `screens.test.ts`: добавить в список присутствующих `profiles-delete-all`,
  `profiles-import-file`; в отсутствующие (на старте):
  `profiles-delete-all-dialog`, `profiles-delete-result`, `profiles-import-result`.

---

## 4. Подготовка workspaces (один раз перед запуском агентов)

```bash
git worktree prune
git worktree add .worktrees/agent-backend -b agent/backend
git worktree add .worktrees/agent-tsapi   -b agent/tsapi
git worktree add .worktrees/agent-ui       -b agent/ui
# node_modules для UI-worktrees (не копировать 183M):
cp -al ui/node_modules .worktrees/agent-tsapi/ui/node_modules   # hardlink; fallback: ln -s
cp -al ui/node_modules .worktrees/agent-ui/ui/node_modules
```

- Python-worktree: зависимостей не надо, `nix develop` берётся из store.
- Запуск трёх агентов — параллельно (Task tool, `subagent_type: general`).
  Каждому в промпт: путь worktree, его файлы (§2), контракты (§3), его команды
  тестов (§2), запреты: не коммитить, не создавать новые файлы вне списка,
  не запускать полный suite/e2e/cargo/vue-tsc, стиль — русские докстринги как
  в соседнем коде.

### Сбор изменений (агенты не коммитят)

```bash
git -C .worktrees/agent-backend diff > /tmp/opencode/pA.patch   # A
git -C .worktrees/agent-tsapi   diff > /tmp/opencode/pB.patch   # B
git -C .worktrees/agent-ui       diff > /tmp/opencode/pC.patch   # C
git apply /tmp/opencode/pA.patch && git apply /tmp/opencode/pB.patch && git apply /tmp/opencode/pC.patch
git worktree remove --force .worktrees/agent-backend   # и аналогично двум
git branch -D agent/backend agent/tsapi agent/ui
```

Файлы у агентов непересекающиеся → патчи применяются чисто. Если агент создал
новый файл — `git diff` его не покажет: после каждого агента проверять
`git -C <wt> status --porcelain` и брать незtracked через `git -C <wt> add -N <file>`.

---

## 5. Интеграция ВНЕ агентов (после сбора патчей)

1. `nix develop -c ruff check engine tests`
2. `nix develop -c python -m pytest tests/engine/test_profile_pool.py tests/engine/control_plane/test_api_profiles.py -q`
3. `cd ui && npx vitest run src/lib/control.test.ts src/lib/profiles.test.ts src/lib/proxies.test.ts src/lib/format.test.ts src/composables/useProfiles.test.ts src/views/screens.test.ts`
4. `cd ui && npx vue-tsc --noEmit` — ловит рассинхрон контрактов A↔B↔C
5. **Один e2e (HTTP против демона)** — §6, скрипт вне агентов, один процесс демона.
6. Полный suite — НЕ гонять (по требованию: только добавленные/затронутые).

---

## 6. E2E-скрипт (один, вне агентов) — `/tmp/opencode/e2e_profiles.py`

Шаги (всё во временном каталоге, cwd = tempdir, `ADCLICKER_CONTROL_TOKEN=<random>`):

1. tempdir: `config.json` с **абсолютными** путями `paths.user_agents`
   (→ копия `user_agents.txt`, 53 строки), свежий `adclicker.db`;
   запуск `nix develop -c python -m engine.control_plane.daemon --db <tmp>/adclicker.db
   --config <tmp>/config.json --port 8791`, ожидание `/health`.
2. `POST /control/profiles/import {"file": true}` → `added == 53`, `skipped == 0`.
3. `GET /control/profiles` → 53 строки: `name` = `UA-1..UA-53` (уникальные),
   `user_agent` заполнен, `key_ref is None`, `status == "free"`.
4. Повтор `{"file": true}` → `added == 0`, `skipped == 53`, каждый problem —
   объект `{line_index, message}` (контракт демона; UI-нормализация проверяется в vitest).
5. `POST import {"lines": ["Mozilla/5.0 (тест) Chrome/99", "key-1 | Имя", "\tMozilla/5.0 (тест) Chrome/99"]}`
   → 2-я строка добавлена как key_ref с именем «Имя»; 1-я добавлена (`UA-54`);
   3-я (дубликат UA после tab-нарезки) → skipped с problem по `user_agent`.
6. `POST delete {"id": N}` → `{"deleted": 1}` **и JSON-тип number** (FIX B);
   `{"id": 999999}` → 404; `{"id": true}` → 400.
7. `POST delete {"all": true}` → `deleted == остаток`, `skipped == 0`, `problems == []`.
8. `GET /control/profiles` → пусто. `POST import {}` (ни lines, ни file) → 400.
9. SIGTERM демона, код возврата 0.

---

## 7. Риски / заметки

- C без B: тесты C зелёные (фейк api), но vue-tsc упадёт ДО интеграции — это
  ожидаемо, typecheck только в §5.4.
- `cp -al node_modules` — hardlink: vitest пишет только в свой кэш вне
  node_modules (fsModuleCache выключен), риск минимален; при проблемах — симлинк.
- FIX B меняет контракт демона: единственные потребители `{"deleted": true}` —
  два python-теста (обновляет A) и UI, который и так ждёт число.
- FIX A не трогает wordlist.ts (там уже строки) — не раздувать.
- Параллельные vitest двух worktrees на общем hardlink-node_modules: кэш-гонки
  не должны возникать (кэш выключен); если возникнут — запускать B и C последовательно.
- Порт e2e: 8791 (8787 может быть занят).

## 8. Журнал изменений плана

- `2026-10-06` — создан: фикс daemon-гонки закоммичен; задача профилей разбита на
  3 агентов (слойная нарезка, непересекающиеся файлы), контракты HTTP/TS зафиксированы,
  e2e = HTTP против демона (выбрано пользователем), имена UA-N (выбрано),
  оба багфикса в объёме (выбрано).
- `2026-10-06` (вечер) — **всё выполнено**:
  - worktrees созданы, 3 агента отработали параллельно, свои тесты каждого зелёные;
  - патчи (pA/pB/pC) применены к develop без конфликтов, worktrees удалены;
  - интеграция: `ruff check engine tests` OK; `pytest tests/engine/test_profile_pool.py
    tests/engine/control_plane/test_api_profiles.py` → **241 passed**; vitest по 6
    затронутым файлам → **177 passed**; `vue-tsc --noEmit` → exit 0;
  - доп. регресс-прогон соседних модулей: `pytest tests/engine/control_plane/
    tests/engine/test_proxy_pool.py` → **1005 passed** (переименование
    `_requested_proxy_ids` → `_requested_delete_ids` ничего не сломало);
    `vitest src/lib src/composables src/views` → **611 passed**;
  - e2e `/tmp/opencode/e2e_profiles.py` → **23/23 OK**, код выхода демона 0.
  - Замечание агента A учтено: в e2e строка-дубль UA — `UA\tколонка2`
    (срез по табу до strip; `\tUA` дал бы «строка пустая») — §0.2.
  - Изменения НЕ закоммичены (коммит — по явной команде пользователя).
- `2026-10-07` — добавлена задача экспорта в PostgreSQL (§9–§16).
- `2026-10-07` (вечер) — **задача экспорта ВЫПОЛНЕНА**:
  - 3 агента в worktrees отработали параллельно, свои тесты каждого зелёные;
    патчи (pA/pB/pC) применены к develop без конфликтов, worktrees/ветки удалены;
  - состав: `engine/exporter.py` (курсорный SQLite→PG, 11 таблиц, psycopg v3
    лениво), export-job в `daemon.py` (env `ADCLICKER_EXPORT_INTERVAL`,
    дефолт 60с), секция `export` в `_SCHEMA`/`config.json` (пароль — секрет),
    категория `export` в `engine/log.py`, `docker-compose.yml` + `init.sql`
    в корне (postgres:16-alpine, 12 таблиц без FK), psycopg во
    `flake.nix`/`pyproject.toml`/`requirements.txt`, секция README,
    `settingsSchema.ts` (8 полей, секрет export.password);
  - интеграция: `ruff check engine tests` OK; `pytest test_exporter.py +
    test_export_job.py + test_config.py` → **256 passed**; vitest
    `settingsSchema.test.ts` → **10 passed**; `vue-tsc --noEmit` → exit 0;
    регресс `pytest tests/engine/control_plane + test_log + test_log_access +
    legacy/test_config_reader` → **1090 passed**; vitest
    `src/constants + lib/settings + composables/useSettings + views/screens`
    → **68 passed**;
  - e2e `/tmp/opencode/e2e_export.py` → **19/19 OK**: compose healthy, все 11
    таблиц наполнились, `proxies.username/password` в PG = NULL,
    `export_state` заполнен (11), повторные тики идемпотентны, SIGTERM → exit 0.
  - Замечания агентов учтены: CATEGORIES в `engine/log.py` — `export` (в
    `metrics` не было); в `test_log.py`/`test_api.py` поправлены по строке
    (новая категория/секция ломали точные ассерты); UI-фильтр логов
    (`logFilters.ts`) категорию `export` не предлагает — как и `metrics`,
    вне объёма.
  - Изменения НЕ закоммичены (коммит — по явной команде пользователя).

---

## 9. Задача: постоянный экспорт данных во внешнюю PostgreSQL (решения приняты)

Заказчик: «постоянный экспорт логов, метрик и прочего — максимально возможной
информации — на внешнюю БД по хосту/паролю и т.д.; в корне docker-compose.yml
с init.sql для этой БД».

### 9.1 Что экспортируем (всё из adclicker.db, кроме kv-мусора)

Инкрементально-append таблицы (курсор по `id`):

| таблица | содержимое |
|---|---|
| `logs` | структурированные логи (ts, day, level, browser_id, category, message, fields) |
| `clicks` | клики (url, query, category, browser_id, proxy_id, http_status) |
| `network_requests` | CDP-запросы |
| `captcha_events` | события капчи |
| `diagnostics` | снимки отпечатков сессий |
| `proxy_usage` | выдачи прокси |
| `runs` | запуски (append + догоняющее обновление изменённых строк) |

Полные снимки (upsert целиком каждый тик — таблицы маленькие):

| таблица | содержимое |
|---|---|
| `workers` | воркеры (по browser_id) |
| `proxies` | пул прокси (**без** `username`/`password` — секреты не покидают машину, как в schema.sql:37) |
| `profiles` | профили (без секретов; `key_ref` — только имя ссылки) |
| `metrics_hourly` | часовые агрегаты (по bucket) |

`kv` — служебное, НЕ экспортируем. Legacy `clicklogs.db`/`geolocation.db` —
НЕ экспортируем (источник истины уже в `adclicker.db`).

### 9.2 Куда и как

- Целевая БД: **PostgreSQL 16**, поднимается `docker-compose.yml` в корне репо
  (сервис `export-db`), схема — корневой `init.sql` (зеркало колонок SQLite с
  типами PG: `REAL→double precision`, `INTEGER→bigint`, `TEXT→text`), плюс
  таблица курсоров `export_state(table_name text PK, last_id bigint, updated_at
  double precision)`. Все таблицы целевой БД — `INSERT ... ON CONFLICT ... DO
  UPDATE` (идемпотентно, повторный тик не плодит дубли).
- Драйвер: **psycopg (v3)**, lazy-импорт внутри `engine/exporter.py`
  (проект без export-секции или без psycopg не должен падать на импорте).
- Куда подключаться/что экспортировать — **секция `export` в config.json**
  (см. §13.1), пароль в `_SECRET_FIELDS`.
- Интервал — **env `ADCLICKER_EXPORT_INTERVAL`** (сек, дефолт 60, 0 = job
  выключен), как у остальных job'ов демона (конвенция daemon.py:99-103:
  частота — env, настройки — config.json, читаются на каждом тике).
- Пишет только демон (один процесс, control plane); воркеры экспорт не делают.
- Сбой сети/БД не роняет тик: `ExportError` логируется через
  `store.log(..., category="export")`, курсор не двигается, следующий тик
  повторяет. Курсор двигается ТОЛЬКО после успешного upsert батча.

### 9.3 Батчинг и порядок

- Батч: `LIMIT batch_size` (конфиг `export.batch_size`, дефолт 500, 50..5000)
  по таблице за проход, таблиц — за тик, пока не отработают все или не
  выйдет бюджет итераций (≤ 20 батчей на таблицу за тик, остальное — со следующим).
- Порядок таблиц за тик: снимки справочников первыми (`proxies`, `profiles`,
  `workers`, `metrics_hourly`), потом append-таблицы (`runs`, `logs`, `clicks`,
  `network_requests`, `captcha_events`, `diagnostics`, `proxy_usage`) — чтобы
  FK-ссылки (proxy_id, profile_id, worker_id) уже существовали в PG.
- `runs`: инкремент по курсору `id`, плюс повторный upsert «живых» строк
  (`status='running'`) — их `ended_at` дописывается позже.

---

## 10. Разбивка на 3 агента (файлы не пересекаются)

| | Агент A — backend (Python) | Агент B — infra | Агент C — UI |
|---|---|---|---|
| worktree | `.worktrees/agent-export-backend` | `.worktrees/agent-export-infra` | `.worktrees/agent-export-ui` |
| ветка | `agent/export-backend` | `agent/export-infra` | `agent/export-ui` |
| файлы | `engine/exporter.py` (новый), `engine/control_plane/daemon.py`, `engine/control_plane/config.py`, `engine/log.py`, `config.json`, `tests/engine/test_exporter.py` (новый), `tests/engine/control_plane/test_export_job.py` (новый), правки `tests/engine/control_plane/test_config.py` | `docker-compose.yml` (новый, корень), `init.sql` (новый, корень), `README.md` (секция «Экспорт в PostgreSQL»), `flake.nix` (+psycopg), `pyproject.toml` (+psycopg), `requirements.txt` (+psycopg) | `ui/src/constants/settingsSchema.ts`, `ui/src/constants/settingsSchema.test.ts` |
| тесты | `nix develop -c python -m pytest tests/engine/test_exporter.py tests/engine/control_plane/test_export_job.py tests/engine/control_plane/test_config.py -q` + `nix develop -c ruff check engine tests` | валидация `docker compose config` (без поднятия БД) | `cd ui && npx vitest run src/constants/settingsSchema.test.ts` |

A владеет `config.json` и `_SCHEMA` — C повторяет их в settingsSchema.ts
(тест полноты требует синхронности, контракт §13.1). B владеет зависимостями,
чтобы A не трогал flake/pyproject; psycopg в nixpkgs называется `psycopg`
(python312, v3).

## 11. Подготовка workspaces

```bash
git worktree prune
git worktree add .worktrees/agent-export-backend -b agent/export-backend
git worktree add .worktrees/agent-export-infra   -b agent/export-infra
git worktree add .worktrees/agent-export-ui      -b agent/export-ui
cp -al ui/node_modules .worktrees/agent-export-ui/ui/node_modules
```

Сбор патчей — по схеме §4 (агенты не коммитят; untracked-файлы через
`git add -N`).

---

## 12. Интеграция (после сбора патчей, вне агентов)

1. `nix develop -c ruff check engine tests`
2. `nix develop -c python -m pytest tests/engine/test_exporter.py tests/engine/control_plane/test_export_job.py tests/engine/control_plane/test_config.py -q`
3. `cd ui && npx vitest run src/constants/settingsSchema.test.ts`
4. `cd ui && npx vue-tsc --noEmit`
5. E2E с реальной БД (§14).

---

## 13. Контракты

### 13.1 Секция конфига `export`

`_SCHEMA` (engine/control_plane/config.py), config.json, settingsSchema.ts —
в трёх местах одинаково:

```python
"export": {
    "enabled":     (bool,   False),     # False — тик no-op, соединение не открывается
    "host":        (str,    "127.0.0.1"),
    "port":        (int,    5432),
    "dbname":      (str,    "adclicker_export"),
    "user":        (str,    "adclicker"),
    "password":    (str,    ""),        # _SECRET_FIELDS: "export.password"
    "sslmode":     (str,    "prefer"),  # _ENUM_FIELDS: disable|allow|prefer|require|verify-ca|verify-full
    "batch_size":  (int,    500),       # _numeric_limits: 50..5000
}
```

В `config.json` корня — секция с дефолтами (enabled=false). В
`config_reader.py` НЕ добавляется (воркерам экспорт не нужен; legacy-читатель
секции не видит — `for section in ("paths", "webdriver", "behavior")` не
трогаем).

### 13.2 API модуля engine/exporter.py

```python
DEFAULT_BATCH_SIZE = 500
EXPORT_TABLES: tuple[str, ...]  # порядок экспорта, §9.3

@dataclass(frozen=True)
class ExportSettings:
    enabled: bool; host: str; port: int; dbname: str
    user: str; password: str; sslmode: str; batch_size: int

class Exporter:
    def __init__(self, db_path: str | Path, settings: ExportSettings): ...
    def export_pass(self, *, now: float | None = None) -> dict[str, int]:
        """Один проход: возвращает {таблица: сколько строк записано}.

        enabled=False → {}. Сетевые/SQL-ошибки → ExportError (не глотать:
        тик демона логирует и продолжает). Курсор в export_state двигается
        после успешного батча каждой таблицы.
        """
    def close(self) -> None: ...

class ExportError(Exception): ...
```

psycopg импортируется лениво внутри `Exporter.__init__` (при enabled=False
импорт не нужен); DSN собирается из settings, `connect_timeout=5`.

### 13.3 Job в daemon.py

По образцу metrics-job (daemon.py:1278-1332):

```python
EXPORT_THREAD_NAME = "export"
DEFAULT_EXPORT_INTERVAL_SECONDS = 60.0
# env ADCLICKER_EXPORT_INTERVAL, парсер export_interval_from_environ()
# (общий _seconds_from_environ: 0/минус = выкл, мусор = ValueError)

def _export_tick(self, now: float | None = None) -> None:
    # config = self._current_config(); settings = ExportSettings(**config.export)
    # if not settings.enabled: return
    # self._exporter.export_pass(now=now); лог INFO category="export" со сводкой
# + _start_export_loop / _run_export_loop; в build_daemon — интервал из env;
# в Daemon.__init__ — ленивый Exporter; в shutdown() — close()
```

Категория `"export"` добавляется в `_CATEGORIES` engine/log.py (рядом с
`metrics`). Сводка тика — `store.log(INFO, "export", "export tick", fields={"rows": {...}})`.

### 13.4 init.sql (корень, монтируется в /docker-entrypoint-initdb.d/)

- `CREATE TABLE IF NOT EXISTS` для 11 таблиц-зеркал (§9.1) + `export_state`;
- типы: `double precision` (REAL), `bigint` (INTEGER id/счётчики), `text`;
- PK `id bigint PRIMARY KEY` (id-таблицы), `bucket bigint PRIMARY KEY`
  (metrics_hourly), `browser_id text PRIMARY KEY` (workers), `key text PRIMARY KEY` НЕТ — kv не экспортируем;
- без FK (упорядоченная вставка делает их избыточными, а PG-зеркало —
  не операционная БД); индексы по `ts` и `browser_id` как в schema.sql;
- колонки `username`/`password` в таблице `proxies` — ЕСТЬ (совместимость
  схемы), но экспортер их всегда пишет NULL (секреты не покидают машину).

### 13.5 docker-compose.yml (корень)

```yaml
services:
  export-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: adclicker_export
      POSTGRES_USER: adclicker
      POSTGRES_PASSWORD: ${EXPORT_DB_PASSWORD:-adclicker}
    ports: ["5432:5432"]
    volumes:
      - export-pgdata:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U adclicker -d adclicker_export"]
      interval: 5s
      timeout: 3s
      retries: 10
volumes:
  export-pgdata:
```

### 13.6 settingsSchema.ts (агент C)

Секция `export` с 8 полями по образцу существующих; секрет `export.password`
в `ENGINE_SECRETS` теста; лимиты `export.port` 1..65535,
`export.batch_size` 50..5000; enum sslmode. Тест полноты против своей копии
`ENGINE_SCHEMA` обновляется синхронно.

---

## 14. E2E (вне агентов) — `/tmp/opencode/e2e_export.py`

1. `docker compose up -d --wait` (healthcheck `pg_isready`).
2. tempdir: config.json с `export.enabled=true`, host=127.0.0.1, dbname/user/password
   как в compose; свежий adclicker.db; PYTHONPATH=<repo>.
3. Старт демона (`nix develop -c python -m engine.control_plane.daemon ... --port 8792`),
   env `ADCLICKER_EXPORT_INTERVAL=2`.
4. Накрутить данные: пару `store.log`, `record_click`, `record_captcha_event`,
   `record_network_request`, `start_run`/`finish_run`.
5. Ждать ≤ 15 с, затем `psql` (через `docker compose exec export-db psql -U adclicker -d adclicker_export -tAc`):
   counts по logs/clicks/captcha_events/network_requests/runs ≥ вставленного;
   `export_state` заполнен; повторный тик не раздувает counts (идемпотентность).
6. SIGTERM, код 0; `docker compose down` (volume сохранить можно, неважно).

---

## 15. Риски / заметки

- psycopg в flake: nixpkgs `psycopg` (v3) для python312 — если в закреплённом
  flake.lock нет, B ставит `psycopg` через uv-слой или фиксит lock (обсудить).
- export-секция ломает `TestDefaults` (сравнение default_config с config.json) —
  A правит оба, C — копию в тесте UI.
- Миграции целевой БД: init.sql выполняется только при первом создании volume —
  изменения схемы PG = `docker compose down -v` или ручные ALTER (в README).
- Параллельные демоны на одной PG: upsert идемпотентен, `export_state` может
  гоняться — приемлемо (одна БД — один демон, документируем).
- Курсор по `id` append-таблиц корректен только при монотонных id (SQLite
  AUTOINCREMENT-подобный rowid — растёт всегда; DELETE не делаем).

## 16. E2E-чек-лист (для §14)

- [ ] compose поднялся, healthcheck healthy
- [ ] демон пишет export-сводки в store.log
- [ ] все 11 таблиц наполнились
- [ ] `proxies.username/password` в PG — NULL
- [ ] повторный тик — counts не растут
- [ ] enabled=false — соединение не открывается (psql log пуст)
- [ ] SIGTERM — чистая остановка, exporter.close() без ошибок

