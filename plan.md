# plan.md — рабочий план (обновлять инкрементально, не переписывать)

> Правило: этот файл — единственный постоянный контекст между сессиями.
> Новые детали — добавлять/уточнять пункты и секцию §8 (журнал), не выбрасывать решённое.

---

## 0. Статус

- [x] Фикс гонки `thread.start()` в `engine/control_plane/daemon.py::_on_signal`
      (порядок «ссылка → start → флаг»); закоммичено в `609856f`; тесты
      `TestOwnerWatch` + `TestDaemonLifecycle` прошли 15×15 повторов.
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

