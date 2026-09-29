-- 003: дневное разделение логов (план.md, §5 «Фаза 9»).
--
-- Контракт: у каждой строки logs есть day — локальная дата её ts в формате
-- YYYY-MM-DD. По этой колонке живут три вещи: экспорт дня в
-- logs/YYYY-MM-DD.log в 23:59, retention (DELETE ... WHERE day < отсечки) и
-- защита от роста БД (удаление самых старых дней). Индекс
-- (day, level, browser_id) обслуживает выборки «день + уровень + воркер»,
-- которые делают экспорт и UI.
--
-- day TEXT без DEFAULT: у SQLite нет выражения DEFAULT от другой колонки той
-- же таблицы (а date('now') дал бы дату записи, а не дату ts — разные вещи
-- для бэкалфа и для строк, записанных с задержкой). Новое значение ставит
-- писатель (engine/store.py, engine/control_plane/state.py) от самого ts;
-- здесь заполняется только история.
--
-- Порядок утверждений продикован неатомарностью executescript (см. докстринг
-- engine/db/migrations.py): UPDATE и CREATE INDEX не выполнимы без колонки,
-- поэтому неидемпотентный ALTER идёт первым — зеркально к 002, где он последний
-- и потому безопасен целиком. Окно риска здесь — от ALTER до bump'а версии:
-- обрыв внутри него на повторе даёт детерминированный "duplicate column
-- name" и не теряет данные (строки не тронуты). Практически обрыв возможен
-- только при отказе диска: UPDATE защищён WHERE day IS NULL, а ts — NOT NULL
-- REAL, падать ему не на чем, CREATE INDEX — IF NOT EXISTS.
--
-- Бэкалф считает дату той же libc, что и time.localtime в Python:
-- strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')
--     == time.strftime('%Y-%m-%d', time.localtime(ts)).
-- unixepoch обязателен: без него SQLite трактует число как Julian day, а не
-- как unix-секунды (см. sqlite.org/lang_datefunc).

ALTER TABLE logs ADD COLUMN day TEXT;

UPDATE logs SET day = strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') WHERE day IS NULL;

CREATE INDEX IF NOT EXISTS idx_logs_day_level_browser ON logs (day, level, browser_id);
