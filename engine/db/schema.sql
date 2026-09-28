-- Схема adclicker.db — источник истины.
--
-- Контракт: движок пишет, UI (rusqlite) читает. Поэтому изменения здесь
-- несовместимы с уже собранным UI и делаются только через migrations/NNN_*.sql
-- с повышением SCHEMA_VERSION в engine/db/migrations.py.
--
-- Время хранится как REAL (unix-секунды): быстрые диапазонные запросы и
-- агрегация для дашборда, форматирование делает UI.
--
-- created_at ставится в DEFAULT, чтобы никто не мог забыть его проставить.

PRAGMA foreign_keys = ON;


-- Аккаунты/ключи, с которыми работает кликер.
CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    -- Ссылка на ключ в hooks.py, сам ключ в БД не хранится
    key_ref      TEXT,
    proxy_id     INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    user_agent   TEXT,
    locale       TEXT,
    timezone     TEXT,
    status       TEXT    NOT NULL DEFAULT 'new',
    last_used_at REAL,
    created_at   REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Прокси. username/password хранятся, но никогда не попадают в логи и отчёты.
CREATE TABLE IF NOT EXISTS proxies (
    id             INTEGER PRIMARY KEY,
    label          TEXT,
    scheme         TEXT    NOT NULL DEFAULT 'http',
    host           TEXT    NOT NULL,
    port           INTEGER NOT NULL,
    username       TEXT,
    password       TEXT,
    country        TEXT,
    latency_ms     INTEGER,
    is_alive       INTEGER NOT NULL DEFAULT 1,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    last_checked_at REAL,
    last_error     TEXT,
    created_at     REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL)),
    UNIQUE (host, port, username)
);

-- Воркеры — по одному на браузер.
CREATE TABLE IF NOT EXISTS workers (
    id            INTEGER PRIMARY KEY,
    pid           INTEGER,
    browser_id    TEXT    NOT NULL UNIQUE,
    profile_id    INTEGER REFERENCES profiles (id) ON DELETE SET NULL,
    proxy_id      INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    status        TEXT    NOT NULL DEFAULT 'starting',
    started_at    REAL,
    heartbeat_at  REAL,
    restart_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Один запуск сценария воркером.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    worker_id      INTEGER REFERENCES workers (id) ON DELETE CASCADE,
    started_at     REAL,
    ended_at       REAL,
    status         TEXT    NOT NULL DEFAULT 'running',
    error          TEXT,
    total_clicks   INTEGER NOT NULL DEFAULT 0,
    captcha_seen   INTEGER NOT NULL DEFAULT 0,
    captcha_solved INTEGER NOT NULL DEFAULT 0,
    created_at     REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Структурированные логи: message читаем человеком, fields — всё машинное в JSON.
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    level      TEXT    NOT NULL DEFAULT 'INFO',
    browser_id TEXT,
    category   TEXT,
    message    TEXT    NOT NULL,
    fields     TEXT,
    created_at REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Заменяет clicklogs.db из legacy-кода.
CREATE TABLE IF NOT EXISTS clicks (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    url         TEXT    NOT NULL,
    query       TEXT,
    category    TEXT,
    browser_id  TEXT,
    proxy_id    INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    http_status INTEGER,
    created_at  REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

CREATE TABLE IF NOT EXISTS captcha_events (
    id             INTEGER PRIMARY KEY,
    ts             REAL    NOT NULL,
    browser_id     TEXT,
    proxy_id       INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    page_url       TEXT,
    sitekey        TEXT,
    screenshot_path TEXT,
    solved         INTEGER NOT NULL DEFAULT 0,
    solver         TEXT,
    elapsed_ms     INTEGER,
    created_at     REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

CREATE TABLE IF NOT EXISTS proxy_usage (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    proxy_id   INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    browser_id TEXT,
    result     TEXT,
    latency_ms INTEGER,
    created_at REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Снимок отпечатка и окружения сессии для разбора расхождений с антиботом.
CREATE TABLE IF NOT EXISTS diagnostics (
    id              INTEGER PRIMARY KEY,
    ts              REAL    NOT NULL,
    browser_id      TEXT,
    proxy_id        INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    ip              TEXT,
    country         TEXT,
    user_agent      TEXT,
    accept_language TEXT,
    timezone_id     TEXT,
    screen_w        INTEGER,
    screen_h        INTEGER,
    platform        TEXT,
    webgl_vendor    TEXT,
    webgl_renderer  TEXT,
    browser_version TEXT,
    headers         TEXT,
    suspicion_flags TEXT,
    created_at      REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Источник метрики "запросы/час" через CDP-события, а не по кликам.
CREATE TABLE IF NOT EXISTS network_requests (
    id            INTEGER PRIMARY KEY,
    ts            REAL    NOT NULL,
    browser_id    TEXT,
    method        TEXT,
    url           TEXT,
    resource_type TEXT,
    status        INTEGER,
    created_at    REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);

-- Агрегаты дашборда, наполняются по bucket.
CREATE TABLE IF NOT EXISTS metrics_hourly (
    bucket         INTEGER PRIMARY KEY,
    successes      INTEGER NOT NULL DEFAULT 0,
    failures       INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    captchas       INTEGER NOT NULL DEFAULT 0,
    uptime_seconds INTEGER NOT NULL DEFAULT 0
);

-- Служебные значения: retention-настройки, флаги паузы, версии.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);


-- Фильтры UI: по времени и по browser_id.
CREATE INDEX IF NOT EXISTS idx_logs_ts             ON logs (ts);
CREATE INDEX IF NOT EXISTS idx_logs_browser_id     ON logs (browser_id);
CREATE INDEX IF NOT EXISTS idx_clicks_ts           ON clicks (ts);
CREATE INDEX IF NOT EXISTS idx_clicks_browser_id   ON clicks (browser_id);
CREATE INDEX IF NOT EXISTS idx_captcha_events_ts   ON captcha_events (ts);
CREATE INDEX IF NOT EXISTS idx_proxy_usage_ts      ON proxy_usage (ts);
CREATE INDEX IF NOT EXISTS idx_diagnostics_ts      ON diagnostics (ts);

CREATE INDEX IF NOT EXISTS idx_network_requests_ts        ON network_requests (ts);
CREATE INDEX IF NOT EXISTS idx_network_requests_browser  ON network_requests (browser_id);

CREATE INDEX IF NOT EXISTS idx_runs_worker_id    ON runs (worker_id);
CREATE INDEX IF NOT EXISTS idx_workers_status    ON workers (status);
CREATE INDEX IF NOT EXISTS idx_workers_heartbeat ON workers (heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_proxies_alive     ON proxies (is_alive);
