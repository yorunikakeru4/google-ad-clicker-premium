//! Read-only читалка боевой БД для экранов Logs и Proxies.
//!
//! Контракт: движок (`engine/db`) пишет в WAL, UI читает тот же файл, но
//! только соединением `SQLITE_OPEN_READ_ONLY` — без создания файла и без
//! какой-либо записи из фронтенда. `journal_mode` читалка не трогает:
//! журнал выставляет писатель (`engine/db/migrations.py`).

use std::fmt;
use std::fs;
use std::io;
use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, ToSql};
use serde::Serialize;

/// Зеркало `BUSY_TIMEOUT_MS` из `engine/db/migrations.py`: читатель ждёт
/// освобождения блокировки ровно столько же, сколько ждёт движок, вместо
/// ошибки SQLITE_BUSY, пока писатель дописывает батч под WAL.
pub const BUSY_TIMEOUT_MS: u64 = 5000;

/// Потолок строк на один запрос: экран логов не должен выгружать в память
/// UI миллионы строк. Запрос с большим `limit` усекается до этой величины.
pub const MAX_LOGS_LIMIT: u32 = 1000;

/// Ошибка читалки. `Display` отдаёт готовый текст для UI, `kind` в сериализованном
/// виде различает случаи для фронтенда.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", content = "message")]
pub enum DbError {
    /// Файла нет. Читалка не создаёт БД: это ошибка запуска, а не «пустая база».
    DatabaseNotFound { path: String },
    /// Файл есть, но это не база движка: каталог, не-SQLite файл или
    /// неприменённая схема (нет таблицы `logs`).
    OpenFailed { path: String, reason: String },
    /// Соединение открыто, но запрос к базе не удался.
    ReadFailed { reason: String },
    /// Читателя ещё нет: `db_open` не вызван или завершился ошибкой.
    /// Отдельный вариант, а не ReadFailed: UI различает «база закрыта» и
    /// «запрос сломался».
    NotOpen,
}

impl fmt::Display for DbError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DbError::DatabaseNotFound { path } => write!(
                f,
                "База данных не найдена: {path}. Запустите движок — он создаст \
                 файл при первом запуске."
            ),
            DbError::OpenFailed { path, reason } => {
                write!(f, "Не удалось открыть базу {path}: {reason}")
            }
            DbError::ReadFailed { reason } => write!(f, "Ошибка чтения из базы: {reason}"),
            DbError::NotOpen => write!(
                f,
                "База данных не открыта: сначала вызовите db_open с путём к файлу."
            ),
        }
    }
}

impl std::error::Error for DbError {}

/// Строка лога для экрана Logs. `fields` остаётся сырой строкой: JSON
/// разбирается на стороне фронтенда, Rust его не интерпретирует.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LogEntry {
    pub ts: f64,
    pub level: String,
    pub browser_id: Option<String>,
    pub category: Option<String>,
    pub message: String,
    pub fields: Option<String>,
}

/// Фильтры экрана логов: точное равенство по уровню, категории и `browser_id`
/// плюс окно времени `[since, until]` по `ts`. Пустое поле означает «фильтр
/// не применять» — ровно то, что делают `None` в [`DbReader::list_logs`].
/// `Eq` не выводится: окно времени задаётся `f64` — как и `ts` в самой таблице.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct LogFilters {
    pub level: Option<String>,
    pub category: Option<String>,
    pub browser_id: Option<String>,
    /// Нижняя граница окна включительно: `ts >= since`.
    pub since: Option<f64>,
    /// Верхняя граница окна включительно: `ts <= until`. `until < since` —
    /// пустое окно, а не ошибка: запрос корректен, данных в нём просто нет.
    pub until: Option<f64>,
}

/// Строка страницы логов: та же запись, что и в [`LogEntry`], плюс `id` —
/// вторая половина курсора. Без `id` группу строк с равным `ts` на границе
/// страниц нельзя ни продолжить, ни исключить без потерь и дублей.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LogPageEntry {
    pub id: i64,
    #[serde(flatten)]
    pub log: LogEntry,
}

/// Строка списка прокси — экран Proxies (контракт GET /control/proxies,
/// только без кредов: `username`/`password` сюда не входят вообще).
///
/// Маскирование (план §5, фаза 5): пароль не должен покидать БД даже через
/// read-only читалку — UI они не нужны, а риск утечки в лог или ответ нулевой
/// только пока поля нет в структуре. `assigned_browser_id` — воркер,
/// сидящий на прокси (`workers.proxy_id`), `usage_count` — строк в
/// `proxy_usage`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ProxyRow {
    pub id: i64,
    pub label: Option<String>,
    pub scheme: String,
    pub host: String,
    pub port: i64,
    pub country: Option<String>,
    pub latency_ms: Option<i64>,
    pub is_alive: bool,
    pub fail_count: i64,
    pub last_checked_at: Option<f64>,
    pub last_error: Option<String>,
    pub assigned_browser_id: Option<String>,
    pub usage_count: i64,
}

/// Строка списка профилей — экран Profiles (контракт GET /control/profiles).
///
/// Два поля выходят за колонки самой таблицы: `assigned_browser_id` — воркер,
/// сидящий на профиле (`workers.profile_id`), `proxy_label`/`proxy_address` —
/// прокси профиля через LEFT JOIN: у профиля может не быть прокси, и тогда все
/// три поля NULL, а не пустые строки.
///
/// `fields` остаётся сырой JSON-строкой: колонка появляется миграцией
/// `engine/db/migrations/002_profile_fields.sql`, поэтому читалка проверяет её
/// наличие и до миграции читает NULL. Разбор JSON — на стороне фронтенда.
///
/// Креды (`username`, `password`) прокси в выборку не входят: полей нет в
/// [`ProfileRow`], поэтому в JSON для UI их не может быть по построению.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ProfileRow {
    pub id: i64,
    pub name: String,
    pub key_ref: Option<String>,
    pub proxy_id: Option<i64>,
    pub user_agent: Option<String>,
    pub locale: Option<String>,
    pub timezone: Option<String>,
    pub status: String,
    pub last_used_at: Option<f64>,
    pub fields: Option<String>,
    pub assigned_browser_id: Option<String>,
    pub proxy_label: Option<String>,
    /// `host:port` прокси либо NULL, если у профиля прокси нет.
    pub proxy_address: Option<String>,
}

/// Строка диагностики: последний снимок сессии одного воркера — экран
/// Diagnostics. Колонки — контракт таблицы `diagnostics` из
/// `engine/db/schema.sql`, без изменений.
///
/// `headers` и `suspicion_flags` остаются сырыми строками JSON: читалка не
/// парсит их, поэтому битый JSON не превращается в ошибку чтения, а доходит
/// до фронта как есть — там и решается, что с ним делать.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DiagnosticRow {
    pub ts: f64,
    pub browser_id: Option<String>,
    pub proxy_id: Option<i64>,
    pub ip: Option<String>,
    pub country: Option<String>,
    pub user_agent: Option<String>,
    pub accept_language: Option<String>,
    pub timezone_id: Option<String>,
    pub screen_w: Option<i64>,
    pub screen_h: Option<i64>,
    pub platform: Option<String>,
    pub webgl_vendor: Option<String>,
    pub webgl_renderer: Option<String>,
    pub browser_version: Option<String>,
    pub headers: Option<String>,
    pub suspicion_flags: Option<String>,
}

/// Строка `captcha_events` для ленты CAPTCHA на Dashboard: когда случилось,
/// кто и на какой странице, решилась капча или нет и где лежит скриншот.
///
/// Колонки — контракт таблицы `captcha_events` из `engine/db/schema.sql`.
/// `solved` хранится в БД как INTEGER (0/1) и выходит в JSON булевым:
/// фронт читает его как правду об исходе решения, а не как число.
/// `sitekey` и `solver` приходят как есть — UI показывает их в строке
/// события, не интерпретируя.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CaptchaEventRow {
    pub id: i64,
    pub ts: f64,
    pub browser_id: Option<String>,
    pub proxy_id: Option<i64>,
    pub page_url: Option<String>,
    pub sitekey: Option<String>,
    pub screenshot_path: Option<String>,
    pub solved: bool,
    pub solver: Option<String>,
    pub elapsed_ms: Option<i64>,
}

/// Читатель боевой БД: одно соединение, строго на чтение.
///
/// Соединение не разделяется между потоками (`SQLITE_OPEN_NO_MUTEX`):
/// каждый поток держит своего читателя.
#[derive(Debug)]
pub struct DbReader {
    /// Соединение отдаётся модулю `metrics` для агрегатов дашборда: поле
    /// видно только внутри крейта, наружу читалка остаётся закрытой.
    pub(crate) conn: Connection,
}

impl DbReader {
    /// Открывает существующую базу строго на чтение.
    ///
    /// Несуществующий путь — ошибка `DatabaseNotFound`, а не созданная «с нуля»
    /// пустая БД: файл создаёт только движок.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, DbError> {
        let path = path.as_ref();
        ensure_openable(path)?;

        // Флаги осознанно без SQLITE_OPEN_CREATE: read-only соединение не имеет
        // права создавать ни файл, ни -wal/-shm — журнал ведёт писатель.
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let conn =
            Connection::open_with_flags(path, flags).map_err(|err| open_failed(path, err))?;
        conn.busy_timeout(Duration::from_millis(BUSY_TIMEOUT_MS))
            .map_err(|err| open_failed(path, err))?;

        // Файл должен быть базой движка. Чужой или неприменённый файл ловим
        // здесь, а не глубоко внутри запроса, чтобы UI получил внятный текст.
        let has_logs: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs')",
                [],
                |row| row.get(0),
            )
            .map_err(|err| open_failed(path, err))?;
        if !has_logs {
            return Err(DbError::OpenFailed {
                path: path.display().to_string(),
                reason: "в базе нет таблицы logs: схема движка не применена".to_string(),
            });
        }

        Ok(Self { conn })
    }

    /// Строки `logs` по убыванию `ts`, с точным фильтром по уровню,
    /// категории и `browser_id` (любой параметр можно опустить).
    ///
    /// `limit = 0` возвращает пустой список; значения выше
    /// [`MAX_LOGS_LIMIT`] усекаются до него. `browser_id IS NULL` не
    /// совпадает ни с каким значением фильтра.
    pub fn list_logs(
        &self,
        limit: u32,
        level: Option<&str>,
        category: Option<&str>,
        browser_id: Option<&str>,
    ) -> Result<Vec<LogEntry>, DbError> {
        let filters = LogFilters {
            level: level.map(str::to_string),
            category: category.map(str::to_string),
            browser_id: browser_id.map(str::to_string),
            // Окно времени у этого метода нет: экран идёт через
            // [`Self::list_logs_page`] и [`Self::count_logs`], где `since`/
            // `until` живут в самом фильтре.
            ..LogFilters::default()
        };
        let page = self.fetch_log_page(&filters, None, None, limit)?;
        Ok(page.into_iter().map(|row| row.log).collect())
    }

    /// Курсорная страница для экрана Logs: те же фильтры, что и у
    /// [`Self::list_logs`], порядок `ts DESC, id DESC`.
    ///
    /// Курсор «строже позиции» задаётся парой `before_ts`/`before_id` — так
    /// страницы не теряют и не дублируют строки с равным `ts`. Голый
    /// `before_ts` без `before_id` работает как строгое «старше `before_ts`»;
    /// `before_id` без `before_ts` игнорируется. `limit = 0` возвращает
    /// пустую страницу, значения выше [`MAX_LOGS_LIMIT`] усекаются до него.
    pub fn list_logs_page(
        &self,
        filters: &LogFilters,
        before_ts: Option<f64>,
        before_id: Option<i64>,
        limit: u32,
    ) -> Result<Vec<LogPageEntry>, DbError> {
        self.fetch_log_page(filters, before_ts, before_id, limit)
    }

    /// Число строк `logs` под теми же фильтрами, что и у
    /// [`Self::list_logs_page`]: общая величина для пагинации экрана Logs.
    pub fn count_logs(&self, filters: &LogFilters) -> Result<i64, DbError> {
        let (clause, binds) = log_where(filters, None, None);
        let mut sql = String::from("SELECT COUNT(*) FROM logs");
        sql.push_str(&clause);
        let mut stmt = self.conn.prepare(&sql).map_err(read_failed)?;
        let count: i64 = stmt
            .query_row(binds.as_slice(), |row| row.get(0))
            .map_err(read_failed)?;
        Ok(count)
    }

    /// Список прокси для экрана Proxies: строки `proxies` плюс назначенный
    /// воркер и счётчик использований. Порядок — по `id`: стабильный и
    /// совпадает с порядком вставки демона.
    ///
    /// Назначение и счётчик — подзапросами, а не JOIN: два воркера на одном
    /// прокси размножили бы строку, а экрану нужен ровно один адрес на строку.
    /// При нескольких воркерах отдаётся первый `browser_id` по алфавиту —
    /// выбор детерминирован, а не зависит от порядка в таблице.
    ///
    /// Креды (`username`, `password`) в выборку не входят: поля нет в
    /// [`ProxyRow`], поэтому в JSON для UI их не может быть по построению.
    pub fn list_proxies(&self) -> Result<Vec<ProxyRow>, DbError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT p.id, p.label, p.scheme, p.host, p.port, p.country, \
                        p.latency_ms, p.is_alive, p.fail_count, p.last_checked_at, \
                        p.last_error, \
                        (SELECT w.browser_id FROM workers w \
                          WHERE w.proxy_id = p.id \
                          ORDER BY w.browser_id LIMIT 1) AS assigned_browser_id, \
                        (SELECT COUNT(*) FROM proxy_usage u WHERE u.proxy_id = p.id) \
                          AS usage_count \
                   FROM proxies p \
                  ORDER BY p.id",
            )
            .map_err(read_failed)?;

        let rows = stmt
            .query_map([], |row| {
                let is_alive: i64 = row.get(7)?;
                Ok(ProxyRow {
                    id: row.get(0)?,
                    label: row.get(1)?,
                    scheme: row.get(2)?,
                    host: row.get(3)?,
                    port: row.get(4)?,
                    country: row.get(5)?,
                    latency_ms: row.get(6)?,
                    is_alive: is_alive != 0,
                    fail_count: row.get(8)?,
                    last_checked_at: row.get(9)?,
                    last_error: row.get(10)?,
                    assigned_browser_id: row.get(11)?,
                    usage_count: row.get(12)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }

    /// Список профилей для экрана Profiles: строки `profiles` плюс назначенный
    /// воркер, прокси и сырые `fields`. Порядок — по `id`, как у
    /// [`Self::list_proxies`]: стабильный и совпадает с порядком вставки.
    ///
    /// Назначение — подзапросом, а не JOIN: два воркера на одном профиле
    /// размножили бы строку, а экрану нужен ровно один профиль на строку. При
    /// нескольких воркерах отдаётся первый `browser_id` по алфавиту — выбор
    /// детерминирован, а не зависит от порядка в таблице.
    ///
    /// Прокси — LEFT JOIN: профиль может быть без прокси, и в этом случае
    /// `proxy_id`/`proxy_label`/`proxy_address` остаются NULL.
    ///
    /// Колонки `fields` в схеме ещё нет (миграция 002 параллельной ветки):
    /// читалка спрашивает `pragma_table_info` и до миграции отдаёт NULL, а не
    /// падает с «no such column». Это терпимо для read-only читалки, потому что
    /// сама БД остаётся старой — ошибка возникла бы на каждом вызове.
    pub fn list_profiles(&self) -> Result<Vec<ProfileRow>, DbError> {
        let has_fields = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('profiles') WHERE name = 'fields'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .map_err(read_failed)?
            > 0;
        let fields_column = if has_fields { "p.fields" } else { "NULL" };

        let sql = format!(
            "SELECT p.id, p.name, p.key_ref, p.proxy_id, p.user_agent, p.locale, \
                    p.timezone, p.status, p.last_used_at, {fields_column}, \
                    (SELECT w.browser_id FROM workers w \
                      WHERE w.profile_id = p.id \
                      ORDER BY w.browser_id LIMIT 1) AS assigned_browser_id, \
                    x.label AS proxy_label, \
                    CASE WHEN x.id IS NULL THEN NULL \
                         ELSE x.host || ':' || x.port END AS proxy_address \
               FROM profiles p \
               LEFT JOIN proxies x ON x.id = p.proxy_id \
              ORDER BY p.id"
        );

        let mut stmt = self.conn.prepare(&sql).map_err(read_failed)?;
        let rows = stmt
            .query_map([], |row| {
                Ok(ProfileRow {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    key_ref: row.get(2)?,
                    proxy_id: row.get(3)?,
                    user_agent: row.get(4)?,
                    locale: row.get(5)?,
                    timezone: row.get(6)?,
                    status: row.get(7)?,
                    last_used_at: row.get(8)?,
                    fields: row.get(9)?,
                    assigned_browser_id: row.get(10)?,
                    proxy_label: row.get(11)?,
                    proxy_address: row.get(12)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }

    /// Последний снимок диагностики на каждый `browser_id` — экран
    /// Diagnostics. Снимки пишет воркер асинхронно (`POST
    /// /control/diagnostics/collect`), поэтому экран только поллит читалку.
    ///
    /// Свежесть решает `ts`, равные `ts` — `id` (вставлено позже = новее):
    /// без второй части ключа выборка зависела бы от порядка строк в таблице.
    /// Строки без `browser_id` складываются в одну группу (так NULL трактует
    /// `PARTITION BY`) и идут последними: снимок без воркера не выбрасывается,
    /// но и не опережает карточки воркеров.
    ///
    /// `headers`/`suspicion_flags` возвращаются сырыми строками JSON — парсит
    /// фронтенд (см. [`DiagnosticRow`]).
    pub fn list_diagnostics(&self) -> Result<Vec<DiagnosticRow>, DbError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT ts, browser_id, proxy_id, ip, country, user_agent, \
                        accept_language, timezone_id, screen_w, screen_h, platform, \
                        webgl_vendor, webgl_renderer, browser_version, headers, \
                        suspicion_flags \
                   FROM (SELECT *, ROW_NUMBER() OVER ( \
                                  PARTITION BY browser_id \
                                  ORDER BY ts DESC, id DESC) AS rn \
                           FROM diagnostics) \
                  WHERE rn = 1 \
                  ORDER BY browser_id IS NULL, browser_id",
            )
            .map_err(read_failed)?;

        let rows = stmt
            .query_map([], |row| {
                Ok(DiagnosticRow {
                    ts: row.get(0)?,
                    browser_id: row.get(1)?,
                    proxy_id: row.get(2)?,
                    ip: row.get(3)?,
                    country: row.get(4)?,
                    user_agent: row.get(5)?,
                    accept_language: row.get(6)?,
                    timezone_id: row.get(7)?,
                    screen_w: row.get(8)?,
                    screen_h: row.get(9)?,
                    platform: row.get(10)?,
                    webgl_vendor: row.get(11)?,
                    webgl_renderer: row.get(12)?,
                    browser_version: row.get(13)?,
                    headers: row.get(14)?,
                    suspicion_flags: row.get(15)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }

    /// Последние события CAPTCHA — лента на Dashboard.
    ///
    /// Порядок `ts DESC, id DESC` — как у логов: свежее событие первым, а
    /// равный `ts` разрывается id (свежевставленная строка впереди).
    /// `limit = 0` возвращает пустой список; значения выше
    /// [`MAX_LOGS_LIMIT`] усекаются до того же потолка, что и у логов, —
    /// экран не должен выгружать в память всю историю событий.
    pub fn list_captcha_events(&self, limit: u32) -> Result<Vec<CaptchaEventRow>, DbError> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let limit = limit.min(MAX_LOGS_LIMIT);

        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, ts, browser_id, proxy_id, page_url, sitekey, \
                        screenshot_path, solved, solver, elapsed_ms \
                   FROM captcha_events \
                  ORDER BY ts DESC, id DESC \
                  LIMIT ?1",
            )
            .map_err(read_failed)?;

        let rows = stmt
            .query_map([limit], |row| {
                let solved: i64 = row.get(7)?;
                Ok(CaptchaEventRow {
                    id: row.get(0)?,
                    ts: row.get(1)?,
                    browser_id: row.get(2)?,
                    proxy_id: row.get(3)?,
                    page_url: row.get(4)?,
                    sitekey: row.get(5)?,
                    screenshot_path: row.get(6)?,
                    solved: solved != 0,
                    solver: row.get(8)?,
                    elapsed_ms: row.get(9)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }

    /// Общий путь запросов логов: одна страница в порядке `ts DESC, id DESC`.
    fn fetch_log_page(
        &self,
        filters: &LogFilters,
        before_ts: Option<f64>,
        before_id: Option<i64>,
        limit: u32,
    ) -> Result<Vec<LogPageEntry>, DbError> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let limit = limit.min(MAX_LOGS_LIMIT);

        let (clause, mut binds) = log_where(filters, before_ts.as_ref(), before_id.as_ref());
        let mut sql =
            String::from("SELECT id, ts, level, browser_id, category, message, fields FROM logs");
        sql.push_str(&clause);
        // id — детерминированный разрыв равных ts: свежевставленная строка
        // идёт первой, и курсор (ts, id) продолжает страницу с того места,
        // где остановилась предыдущая.
        sql.push_str(" ORDER BY ts DESC, id DESC LIMIT ?");
        binds.push(&limit);

        let mut stmt = self.conn.prepare(&sql).map_err(read_failed)?;
        let rows = stmt
            .query_map(binds.as_slice(), |row| {
                Ok(LogPageEntry {
                    id: row.get(0)?,
                    log: LogEntry {
                        ts: row.get(1)?,
                        level: row.get(2)?,
                        browser_id: row.get(3)?,
                        category: row.get(4)?,
                        message: row.get(5)?,
                        fields: row.get(6)?,
                    },
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
    }
}

/// WHERE и бинды для запросов логов: фильтры плюс, опционально, курсор.
///
/// * `since`/`until` — окно времени `ts >= since AND ts <= until`, обе границы
///   включаются; каждая сторона опциональна, `until < since` даёт пустую
///   выборку без ошибки;
/// * `before_ts` + `before_id` — «строже позиции курсора» в порядке
///   `ts DESC, id DESC`: `ts < ? OR (ts = ? AND id < ?)`. Равные `ts`
///   достаются странице целиком, поэтому потерь и дублей нет;
/// * только `before_ts` — строгое `ts < ?`: строка с `ts` на границе уже
///   отдана и повторно не приходит;
/// * `before_id` без `before_ts` игнорируется — это первая страница.
///
/// Фильтры собираются динамически: равенство столбца индексируется
/// (`idx_logs_browser_id` и др.), а вариант «? IS NULL OR col = ?»
/// заставлял бы SQLite сканировать индекс по ts целиком. Окно времени идёт
/// перед курсором: оба про по `ts`, и порядок биндов совпадает с порядком `?`.
fn log_where<'a>(
    filters: &'a LogFilters,
    before_ts: Option<&'a f64>,
    before_id: Option<&'a i64>,
) -> (String, Vec<&'a dyn ToSql>) {
    let mut conditions: Vec<&str> = Vec::new();
    let mut binds: Vec<&'a dyn ToSql> = Vec::new();

    if filters.level.is_some() {
        conditions.push("level = ?");
        binds.push(&filters.level);
    }
    if filters.category.is_some() {
        conditions.push("category = ?");
        binds.push(&filters.category);
    }
    if filters.browser_id.is_some() {
        conditions.push("browser_id = ?");
        binds.push(&filters.browser_id);
    }
    if let Some(ts) = &filters.since {
        conditions.push("ts >= ?");
        binds.push(ts);
    }
    if let Some(ts) = &filters.until {
        conditions.push("ts <= ?");
        binds.push(ts);
    }
    if let Some(ts) = before_ts {
        match before_id {
            Some(id) => {
                conditions.push("(ts < ? OR (ts = ? AND id < ?))");
                binds.push(ts);
                binds.push(ts);
                binds.push(id);
            }
            None => {
                conditions.push("ts < ?");
                binds.push(ts);
            }
        }
    }

    if conditions.is_empty() {
        (String::new(), binds)
    } else {
        (format!(" WHERE {}", conditions.join(" AND ")), binds)
    }
}

/// Путь существует и не является каталогом.
fn ensure_openable(path: &Path) -> Result<(), DbError> {
    match fs::metadata(path) {
        Ok(meta) if meta.is_dir() => Err(DbError::OpenFailed {
            path: path.display().to_string(),
            reason: "путь указывает на каталог, а не на файл базы".to_string(),
        }),
        Ok(_) => Ok(()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Err(DbError::DatabaseNotFound {
            path: path.display().to_string(),
        }),
        Err(err) => Err(DbError::OpenFailed {
            path: path.display().to_string(),
            reason: format!("нет доступа к файлу: {err}"),
        }),
    }
}

fn open_failed(path: &Path, err: impl fmt::Display) -> DbError {
    DbError::OpenFailed {
        path: path.display().to_string(),
        reason: err.to_string(),
    }
}

pub(crate) fn read_failed(err: rusqlite::Error) -> DbError {
    DbError::ReadFailed {
        reason: err.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU32, Ordering};

    use super::*;

    /// Схема берётся из того же файла, который применяет движок, — тесты
    /// читают реальный контракт, а не копию.
    const SCHEMA_SQL: &str = include_str!("../../../engine/db/schema.sql");

    static NEXT_TEMP_ID: AtomicU32 = AtomicU32::new(0);

    /// Временный каталог: падение теста не оставляет мусор в системном /tmp.
    struct TempDb {
        dir: PathBuf,
    }

    impl TempDb {
        fn new() -> Self {
            let dir = std::env::temp_dir().join(format!(
                "ui-db-reader-{}-{}",
                std::process::id(),
                NEXT_TEMP_ID.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&dir).expect("временный каталог должен создаться");
            Self { dir }
        }

        fn path(&self) -> PathBuf {
            self.dir.join("adclicker.db")
        }
    }

    impl Drop for TempDb {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.dir);
        }
    }

    struct Row {
        ts: f64,
        level: &'static str,
        browser_id: Option<&'static str>,
        category: Option<&'static str>,
        message: &'static str,
        fields: Option<&'static str>,
    }

    impl Row {
        fn new(ts: f64, level: &'static str, message: &'static str) -> Self {
            Self {
                ts,
                level,
                browser_id: None,
                category: None,
                message,
                fields: None,
            }
        }

        fn browser(mut self, browser_id: &'static str) -> Self {
            self.browser_id = Some(browser_id);
            self
        }

        fn category(mut self, category: &'static str) -> Self {
            self.category = Some(category);
            self
        }

        fn fields(mut self, fields: &'static str) -> Self {
            self.fields = Some(fields);
            self
        }
    }

    fn entry(row: &Row) -> LogEntry {
        LogEntry {
            ts: row.ts,
            level: row.level.to_string(),
            browser_id: row.browser_id.map(str::to_string),
            category: row.category.map(str::to_string),
            message: row.message.to_string(),
            fields: row.fields.map(str::to_string),
        }
    }

    /// Создаёт БД так же, как движок: WAL-журнал первым, затем схема, затем
    /// строки. Возвращает открытое соединение писателя — WAL-тесты читают
    /// при живом писателе.
    fn seed(path: &Path, rows: &[Row]) -> Connection {
        let conn = Connection::open(path).expect("писатель должен создать файл БД");
        conn.execute_batch("PRAGMA journal_mode = WAL;")
            .expect("WAL включается писателем");
        // Данные остаются в -wal до закрытия: читалке придётся читать журнал.
        conn.execute_batch("PRAGMA wal_autocheckpoint = 0;")
            .expect("авточекпоинт отключается");
        conn.execute_batch(SCHEMA_SQL)
            .expect("схема движка применяется");
        for row in rows {
            conn.execute(
                "INSERT INTO logs (ts, level, browser_id, category, message, fields) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![
                    row.ts,
                    row.level,
                    row.browser_id,
                    row.category,
                    row.message,
                    row.fields
                ],
            )
            .expect("строка лога вставляется");
        }
        conn
    }

    fn assert_readonly(result: &Result<usize, rusqlite::Error>) {
        match result {
            Err(rusqlite::Error::SqliteFailure(err, _)) => {
                assert_eq!(
                    err.code,
                    rusqlite::ffi::ErrorCode::ReadOnly,
                    "ожидалась ошибка SQLITE_READONLY"
                );
            }
            other => panic!("ожидалась ошибка SQLITE_READONLY, получено: {other:?}"),
        }
    }

    /// Строка `proxies` для тестов списка: креды подставляются теми же, что
    /// в реальных данных, — маскирование проверяется на живых значениях.
    fn insert_proxy(conn: &Connection, host: &str, port: i64) -> i64 {
        conn.execute(
            "INSERT INTO proxies (scheme, host, port) VALUES ('http', ?1, ?2)",
            rusqlite::params![host, port],
        )
        .expect("строка proxies вставляется");
        conn.last_insert_rowid()
    }

    fn insert_worker_on_proxy(conn: &Connection, browser_id: &str, proxy_id: i64) {
        conn.execute(
            "INSERT INTO workers (browser_id, proxy_id) VALUES (?1, ?2)",
            rusqlite::params![browser_id, proxy_id],
        )
        .expect("строка workers вставляется");
    }

    fn insert_proxy_usage(conn: &Connection, proxy_id: i64, ts: f64) {
        conn.execute(
            "INSERT INTO proxy_usage (ts, proxy_id) VALUES (?1, ?2)",
            rusqlite::params![ts, proxy_id],
        )
        .expect("строка proxy_usage вставляется");
    }

    #[test]
    fn open_reports_missing_file_and_creates_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();

        let err = DbReader::open(&path).expect_err("несуществующий файл — ошибка");

        assert!(
            matches!(err, DbError::DatabaseNotFound { .. }),
            "получено: {err:?}"
        );
        assert!(
            err.to_string().contains("adclicker.db"),
            "текст ошибки должен называть файл: {err}"
        );
        assert!(!path.exists(), "читатель не должен создавать файл БД");
        let leftovers = fs::read_dir(&tmp.dir).expect("read_dir").count();
        assert_eq!(leftovers, 0, "не должно появиться ни -wal, ни -shm");
    }

    #[test]
    fn open_rejects_directory_path() {
        let tmp = TempDb::new();

        let err = DbReader::open(&tmp.dir).expect_err("каталог — не БД");

        assert!(
            matches!(err, DbError::OpenFailed { .. }),
            "получено: {err:?}"
        );
        assert!(
            err.to_string().contains("каталог"),
            "текст должен объяснять причину: {err}"
        );
    }

    #[test]
    fn open_rejects_foreign_file_without_modifying_it() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let garbage = "это не sqlite, а обычный текстовый файл"
            .as_bytes()
            .to_vec();
        fs::write(&path, &garbage).expect("файл пишется тестом");

        let err = DbReader::open(&path).expect_err("не-SQLite файл должен быть отвергнут");

        assert!(
            matches!(err, DbError::OpenFailed { .. }),
            "получено: {err:?}"
        );
        assert_eq!(
            fs::read(&path).expect("файл читается"),
            garbage,
            "read-only открытие не должно перезаписывать файл"
        );
    }

    #[test]
    fn open_rejects_database_without_logs_table() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let conn = Connection::open(&path).expect("БД создаётся");
        conn.execute_batch("CREATE TABLE other (x INTEGER);")
            .expect("чужая схема");
        drop(conn);

        let err = DbReader::open(&path).expect_err("нет таблицы logs — ошибка");

        assert!(
            matches!(err, DbError::OpenFailed { .. }),
            "получено: {err:?}"
        );
        assert!(
            err.to_string().contains("logs"),
            "текст должен назвать отсутствующую таблицу: {err}"
        );
    }

    #[test]
    fn list_logs_returns_rows_newest_first() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(100.0, "INFO", "первая"),
            Row::new(300.0, "ERROR", "третья"),
            Row::new(200.0, "INFO", "вторая"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение без фильтров");

        assert_eq!(
            rows,
            vec![entry(&data[1]), entry(&data[2]), entry(&data[0])],
            "строки отдаются по убыванию ts, а не по порядку вставки"
        );
    }

    #[test]
    fn list_logs_breaks_ts_ties_by_insertion_order() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(500.0, "INFO", "старшая запись при равном ts"),
            Row::new(500.0, "INFO", "младшая запись при равном ts"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение без фильтров");

        assert_eq!(
            rows,
            vec![entry(&data[1]), entry(&data[0])],
            "при равном ts свежевставленная строка идёт первой"
        );
    }

    #[test]
    fn list_logs_filters_by_level() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "INFO", "инфо"),
            Row::new(2.0, "ERROR", "ошибка"),
            Row::new(3.0, "WARN", "предупреждение"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");

        let errors = reader
            .list_logs(10, Some("ERROR"), None, None)
            .expect("фильтр по уровню");
        assert_eq!(errors, vec![entry(&data[1])]);

        let debug = reader
            .list_logs(10, Some("DEBUG"), None, None)
            .expect("фильтр по несуществующему уровню");
        assert!(
            debug.is_empty(),
            "неизвестный уровень не должен ничего возвращать"
        );
    }

    #[test]
    fn list_logs_filters_by_category() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "INFO", "клик")
                .category("click")
                .browser("b1"),
            Row::new(2.0, "INFO", "прокси")
                .category("proxy")
                .browser("b1"),
            Row::new(3.0, "INFO", "клик без категории").browser("b1"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let clicks = reader
            .list_logs(10, None, Some("click"), None)
            .expect("фильтр по категории");

        assert_eq!(clicks, vec![entry(&data[0])]);
    }

    #[test]
    fn list_logs_filters_by_browser_id_and_skips_nulls() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "INFO", "от b1").browser("b1"),
            Row::new(2.0, "INFO", "от b2").browser("b2"),
            Row::new(3.0, "INFO", "от b1").browser("b1"),
            Row::new(4.0, "INFO", "без браузера"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");

        let from_b1 = reader
            .list_logs(10, None, None, Some("b1"))
            .expect("фильтр по browser_id");
        assert_eq!(from_b1, vec![entry(&data[2]), entry(&data[0])]);

        let unknown = reader
            .list_logs(10, None, None, Some("b404"))
            .expect("фильтр по неизвестному браузеру");
        assert!(unknown.is_empty());
    }

    #[test]
    fn list_logs_combines_all_filters() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "ERROR", "точное совпадение")
                .browser("b1")
                .category("click"),
            Row::new(2.0, "ERROR", "другая категория").browser("b1"),
            Row::new(3.0, "ERROR", "другой браузер")
                .browser("b2")
                .category("click"),
            Row::new(4.0, "INFO", "другой уровень")
                .browser("b1")
                .category("click"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(10, Some("ERROR"), Some("click"), Some("b1"))
            .expect("комбинация фильтров");

        assert_eq!(rows, vec![entry(&data[0])]);
    }

    #[test]
    fn list_logs_returns_raw_fields_and_null_columns() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [Row::new(1.0, "INFO", "строка с полем")
            .fields(r#"{"query":"купить кроссовки","http_status":200}"#)
            .category("click")];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение строки с полями");

        assert_eq!(rows.len(), 1);
        assert_eq!(
            rows[0].fields.as_deref(),
            Some(r#"{"query":"купить кроссовки","http_status":200}"#),
            "fields отдаётся сырой строкой, JSON разбирает фронтенд"
        );
        assert_eq!(
            rows[0].browser_id, None,
            "NULL browser_id должен стать None"
        );
        assert_eq!(rows[0].category.as_deref(), Some("click"));
    }

    #[test]
    fn list_logs_caps_limit_and_keeps_newest_rows() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data: Vec<Row> = (0..(MAX_LOGS_LIMIT + 25))
            .map(|i| Row::new(i as f64, "INFO", "строка"))
            .collect();
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(u32::MAX, None, None, None)
            .expect("бешеный limit не должен ронять чтение");

        assert_eq!(
            rows.len(),
            MAX_LOGS_LIMIT as usize,
            "limit должен упираться в потолок"
        );
        assert_eq!(
            rows.first().map(|row| row.ts),
            Some(MAX_LOGS_LIMIT as f64 + 24.0),
            "верхушка — самые новые строки"
        );
        assert_eq!(rows.last().map(|row| row.ts), Some(25.0));
    }

    #[test]
    fn list_logs_with_zero_limit_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[Row::new(1.0, "INFO", "строка")]);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_logs(0, None, None, None)
            .expect("нулевой limit — не ошибка");

        assert!(rows.is_empty());
    }

    #[test]
    fn reads_wal_files_while_writer_is_connected() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(
            &path,
            &[
                Row::new(1.0, "INFO", "в журнале 1"),
                Row::new(2.0, "INFO", "в журнале 2"),
                Row::new(3.0, "INFO", "в журнале 3"),
            ],
        );
        let wal = PathBuf::from(format!("{}-wal", path.display()));
        let shm = PathBuf::from(format!("{}-shm", path.display()));
        assert!(wal.exists() && shm.exists(), "режим должен быть WAL");
        assert!(
            fs::metadata(&wal).expect("wal читается").len() > 0,
            "строки должны лежать в журнале, а не в основном файле"
        );

        let reader = DbReader::open(&path).expect("read-only открытие WAL-базы");
        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение поверх активного журнала");
        assert_eq!(rows.len(), 3, "читатель видит недокоммиченные строки");

        drop(reader);
        drop(writer);
        assert!(
            !wal.exists() && !shm.exists(),
            "последний закрытый писатель убирает журнал"
        );

        let reader = DbReader::open(&path).expect("открытие после чекпоинта");
        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение после чекпоинта");
        assert_eq!(rows.len(), 3, "строки переживают чекпоинт в основной файл");
    }

    #[test]
    fn open_connection_rejects_writes() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[Row::new(1.0, "INFO", "исходная строка")]);

        let reader = DbReader::open(&path).expect("БД открывается");

        let insert = reader.conn.execute(
            "INSERT INTO logs (ts, message) VALUES (999, 'посторонняя запись')",
            [],
        );
        assert_readonly(&insert);
        let delete = reader.conn.execute("DELETE FROM logs", []);
        assert_readonly(&delete);
        let ddl = reader.conn.execute("CREATE TABLE evil (x INTEGER)", []);
        assert_readonly(&ddl);

        let rows = reader
            .list_logs(10, None, None, None)
            .expect("чтение после неудачных записей");
        assert_eq!(rows.len(), 1, "неудачные записи не должны менять данные");
    }

    #[test]
    fn reader_applies_engine_busy_timeout() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[Row::new(1.0, "INFO", "строка")]);

        let reader = DbReader::open(&path).expect("БД открывается");
        let timeout_ms: i64 = reader
            .conn
            .query_row("PRAGMA busy_timeout", [], |row| row.get(0))
            .expect("pragma busy_timeout читается");

        assert_eq!(
            timeout_ms, BUSY_TIMEOUT_MS as i64,
            "читатель ждёт ровно столько, сколько ждёт движок"
        );
    }

    /// Ожидаемая строка страницы: id из таблицы плюс сама запись лога.
    fn paged(id: i64, row: &Row) -> LogPageEntry {
        LogPageEntry {
            id,
            log: entry(row),
        }
    }

    #[test]
    fn list_logs_page_first_page_matches_list_logs() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(100.0, "INFO", "первая"),
            Row::new(300.0, "ERROR", "третья"),
            Row::new(200.0, "INFO", "вторая"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters::default();
        let page = reader
            .list_logs_page(&filters, None, None, 10)
            .expect("первая страница читается");

        assert_eq!(
            page,
            vec![paged(2, &data[1]), paged(3, &data[2]), paged(1, &data[0])],
            "первая страница — те же строки в том же порядке, что у list_logs"
        );
        let plain = reader
            .list_logs(10, None, None, None)
            .expect("list_logs читается");
        let flat: Vec<LogEntry> = page.iter().map(|row| row.log.clone()).collect();
        assert_eq!(flat, plain, "состав страницы неотличим от list_logs");
    }

    #[test]
    fn list_logs_page_walks_distinct_ts_without_loss_or_duplicates() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data: Vec<Row> = (0..7)
            .map(|i| Row::new(100.0 + i as f64, "INFO", "строка"))
            .collect();
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters::default();
        let mut pages: Vec<Vec<LogPageEntry>> = Vec::new();
        let mut before: Option<(f64, i64)> = None;
        loop {
            let (before_ts, before_id) = match before {
                Some((ts, id)) => (Some(ts), Some(id)),
                None => (None, None),
            };
            let page = reader
                .list_logs_page(&filters, before_ts, before_id, 3)
                .expect("страница читается");
            if page.is_empty() {
                break;
            }
            before = page.last().map(|row| (row.log.ts, row.id));
            pages.push(page);
            assert!(
                pages.len() <= 4,
                "пагинация должна завершиться пустой страницей"
            );
        }

        assert_eq!(
            pages.iter().map(Vec::len).collect::<Vec<_>>(),
            vec![3, 3, 1],
            "страницы режутся по limit, последняя забирает хвост"
        );
        let ids: Vec<i64> = pages.iter().flatten().map(|row| row.id).collect();
        assert_eq!(
            ids,
            vec![7, 6, 5, 4, 3, 2, 1],
            "проход по курсору не теряет и не дублирует строки"
        );
        assert_eq!(
            reader.count_logs(&filters).expect("count_logs"),
            7,
            "count_logs согласован с числом прочитанных строк"
        );
    }

    #[test]
    fn list_logs_page_splits_equal_ts_rows_with_id_cursor() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data: Vec<Row> = (0..6)
            .map(|_| Row::new(100.0, "INFO", "равные ts"))
            .chain((0..3).map(|_| Row::new(50.0, "INFO", "старые")))
            .collect();
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters::default();

        let first = reader
            .list_logs_page(&filters, None, None, 3)
            .expect("первая страница");
        assert_eq!(
            first.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![6, 5, 4],
            "при равном ts идёт порядок id DESC"
        );

        let second = reader
            .list_logs_page(&filters, Some(100.0), Some(first[2].id), 3)
            .expect("вторая страница");
        assert_eq!(
            second.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![3, 2, 1],
            "курсор (ts, id) продолжает группу равных ts, а не бросает её"
        );

        let third = reader
            .list_logs_page(&filters, Some(100.0), Some(second[2].id), 3)
            .expect("третья страница");
        assert_eq!(
            third.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![9, 8, 7],
            "после группы равных ts берутся более старые строки"
        );

        let fourth = reader
            .list_logs_page(&filters, Some(50.0), Some(third[2].id), 3)
            .expect("четвёртая страница");
        assert!(fourth.is_empty(), "за последней строкей — пустая страница");

        let mut read: Vec<i64> = [first, second, third]
            .concat()
            .iter()
            .map(|row| row.id)
            .collect();
        read.sort_unstable();
        assert_eq!(
            read,
            (1..=9).collect::<Vec<_>>(),
            "каждая строка прочитана ровно один раз"
        );
        assert_eq!(reader.count_logs(&filters).expect("count_logs"), 9);
    }

    #[test]
    fn list_logs_page_before_ts_without_id_is_exclusive() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(10.0, "INFO", "старая"),
            Row::new(20.0, "INFO", "граница"),
            Row::new(30.0, "INFO", "свежая"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters::default();
        let page = reader
            .list_logs_page(&filters, Some(20.0), None, 10)
            .expect("страница с ts-курсором");

        assert_eq!(
            page,
            vec![paged(1, &data[0])],
            "без id курсор строго старше before_ts: строка с ts = 20 не возвращается"
        );
    }

    #[test]
    fn list_logs_page_keeps_filters_on_every_page() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "ERROR", "ошибка 1"),
            Row::new(1.0, "INFO", "инфо 1"),
            Row::new(2.0, "ERROR", "ошибка 2"),
            Row::new(2.0, "INFO", "инфо 2"),
            Row::new(3.0, "ERROR", "ошибка 3"),
            Row::new(3.0, "INFO", "инфо 3"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters {
            level: Some("ERROR".to_string()),
            ..LogFilters::default()
        };

        let page1 = reader
            .list_logs_page(&filters, None, None, 2)
            .expect("первая страница");
        assert_eq!(
            page1.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![5, 3]
        );

        let page2 = reader
            .list_logs_page(&filters, Some(page1[1].log.ts), Some(page1[1].id), 2)
            .expect("вторая страница");
        assert_eq!(page2.iter().map(|row| row.id).collect::<Vec<_>>(), vec![1]);

        let page3 = reader
            .list_logs_page(&filters, Some(page2[0].log.ts), Some(page2[0].id), 2)
            .expect("третья страница");
        assert!(page3.is_empty(), "фильтр не пропускает строки на границе");
        assert_eq!(
            reader
                .count_logs(&filters)
                .expect("count_logs под фильтром"),
            3,
            "count_logs считает ровно то, что читает list_logs_page"
        );
    }

    #[test]
    fn list_logs_page_caps_limit_and_treats_zero_as_empty() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data: Vec<Row> = (0..(MAX_LOGS_LIMIT + 5))
            .map(|i| Row::new(i as f64, "INFO", "строка"))
            .collect();
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters::default();

        let page = reader
            .list_logs_page(&filters, None, None, u32::MAX)
            .expect("бешеный limit не должен ронять чтение");
        assert_eq!(
            page.len(),
            MAX_LOGS_LIMIT as usize,
            "limit упирается в потолок, как у list_logs"
        );
        assert_eq!(
            page.first().map(|row| row.log.ts),
            Some(MAX_LOGS_LIMIT as f64 + 4.0),
            "верхушка — самые новые строки"
        );

        let empty = reader
            .list_logs_page(&filters, None, None, 0)
            .expect("нулевой limit — не ошибка");
        assert!(empty.is_empty(), "limit = 0 — пустая страница");
    }

    #[test]
    fn count_logs_counts_rows_under_filters() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "ERROR", "ошибка").browser("b1"),
            Row::new(2.0, "INFO", "инфо").browser("b1"),
            Row::new(3.0, "ERROR", "ошибка").browser("b2"),
            Row::new(4.0, "INFO", "инфо"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader
                .count_logs(&LogFilters::default())
                .expect("count без фильтров"),
            4
        );
        assert_eq!(
            reader
                .count_logs(&LogFilters {
                    level: Some("ERROR".to_string()),
                    ..LogFilters::default()
                })
                .expect("count по уровню"),
            2
        );
        assert_eq!(
            reader
                .count_logs(&LogFilters {
                    level: Some("ERROR".to_string()),
                    browser_id: Some("b1".to_string()),
                    ..LogFilters::default()
                })
                .expect("count по уровню и браузеру"),
            1
        );
        assert_eq!(
            reader
                .count_logs(&LogFilters {
                    category: Some("нет_такой".to_string()),
                    ..LogFilters::default()
                })
                .expect("count по несуществующей категории"),
            0
        );
    }

    #[test]
    fn list_logs_page_filters_by_time_window_inclusive_on_both_bounds() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(10.0, "INFO", "до окна"),
            Row::new(20.0, "INFO", "левая граница"),
            Row::new(30.0, "INFO", "внутри"),
            Row::new(40.0, "INFO", "правая граница"),
            Row::new(50.0, "INFO", "после окна"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters {
            since: Some(20.0),
            until: Some(40.0),
            ..LogFilters::default()
        };
        let page = reader
            .list_logs_page(&filters, None, None, 10)
            .expect("окно времени читается");

        assert_eq!(
            page,
            vec![paged(4, &data[3]), paged(3, &data[2]), paged(2, &data[1]),],
            "обе границы окна включаются, строки вне окна не попадают"
        );
    }

    #[test]
    fn list_logs_page_time_window_works_with_only_one_bound() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(10.0, "INFO", "старая"),
            Row::new(20.0, "INFO", "свежая"),
            Row::new(30.0, "INFO", "самая свежая"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");

        let since_only = reader
            .list_logs_page(
                &LogFilters {
                    since: Some(20.0),
                    ..LogFilters::default()
                },
                None,
                None,
                10,
            )
            .expect("только since");
        assert_eq!(
            since_only,
            vec![paged(3, &data[2]), paged(2, &data[1])],
            "since без until — верхняя граница не ограничена"
        );

        let until_only = reader
            .list_logs_page(
                &LogFilters {
                    until: Some(20.0),
                    ..LogFilters::default()
                },
                None,
                None,
                10,
            )
            .expect("только until");
        assert_eq!(
            until_only,
            vec![paged(2, &data[1]), paged(1, &data[0])],
            "until без since — нижняя граница не ограничена"
        );
    }

    #[test]
    fn list_logs_page_walks_time_window_by_cursor_without_loss_or_duplicates() {
        let tmp = TempDb::new();
        let path = tmp.path();
        // 10..=50 шаг 5; окно [20, 45] берёт 45,40,35,30,25,20 — обе границы
        // включительно.
        let data: Vec<Row> = (10..=50)
            .step_by(5)
            .map(|ts| Row::new(f64::from(ts), "INFO", "строка"))
            .collect();
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters {
            since: Some(20.0),
            until: Some(45.0),
            ..LogFilters::default()
        };
        let mut read: Vec<f64> = Vec::new();
        let mut before: Option<(f64, i64)> = None;
        loop {
            let (before_ts, before_id) = match before {
                Some((ts, id)) => (Some(ts), Some(id)),
                None => (None, None),
            };
            let page = reader
                .list_logs_page(&filters, before_ts, before_id, 2)
                .expect("страница читается");
            if page.is_empty() {
                break;
            }
            read.extend(page.iter().map(|row| row.log.ts));
            before = page.last().map(|row| (row.log.ts, row.id));
            assert!(read.len() <= 6, "пагинация должна завершиться");
        }

        assert_eq!(
            read,
            vec![45.0, 40.0, 35.0, 30.0, 25.0, 20.0],
            "курсор ходит по окну без потерь и дублей, порядок ts DESC"
        );
        assert_eq!(
            reader.count_logs(&filters).expect("count под окном"),
            6,
            "count_logs считает те же строки, что отдаёт list_logs_page"
        );
    }

    #[test]
    fn time_window_combines_with_level_filter_and_inverted_window_is_empty() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(10.0, "ERROR", "ошибка до окна"),
            Row::new(20.0, "ERROR", "ошибка в окне"),
            Row::new(20.0, "INFO", "инфо в окне"),
            Row::new(30.0, "ERROR", "ошибка после окна"),
        ];
        let _writer = seed(&path, &data);

        let reader = DbReader::open(&path).expect("БД открывается");
        let filters = LogFilters {
            level: Some("ERROR".to_string()),
            since: Some(15.0),
            until: Some(25.0),
            ..LogFilters::default()
        };

        let page = reader
            .list_logs_page(&filters, None, None, 10)
            .expect("окно + уровень читаются");
        assert_eq!(
            page,
            vec![paged(2, &data[1])],
            "окно времени применяется вместе с фильтром уровня"
        );
        assert_eq!(
            reader.count_logs(&filters).expect("count"),
            1,
            "count_logs согласован с list_logs_page в окне"
        );

        let inverted = LogFilters {
            since: Some(25.0),
            until: Some(15.0),
            ..LogFilters::default()
        };
        assert!(
            reader
                .list_logs_page(&inverted, None, None, 10)
                .expect("инвертированное окно — не ошибка")
                .is_empty(),
            "until < since — пустое окно, а не ошибка"
        );
        assert_eq!(
            reader
                .count_logs(&inverted)
                .expect("count инвертированного окна"),
            0
        );
    }

    #[test]
    fn count_logs_on_empty_database_is_zero_not_an_error() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[]);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader
                .count_logs(&LogFilters::default())
                .expect("пустая БД — это ноль, а не ошибка"),
            0
        );
        assert!(reader
            .list_logs_page(&LogFilters::default(), None, None, 10)
            .expect("пустая БД отдаёт пустую страницу")
            .is_empty());
    }

    #[test]
    fn list_proxies_on_empty_db_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[]);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader.list_proxies().expect("пустая БД — пустой список"),
            vec![] as Vec<ProxyRow>,
            "отсутствие прокси — не ошибка чтения"
        );
    }

    #[test]
    fn list_proxies_joins_assignment_and_usage_without_row_multiplication() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        let assigned = insert_proxy(&writer, "a.example", 8080);
        let free = insert_proxy(&writer, "b.example", 3128);
        // Два воркера на одном прокси: строка не должна размножиться, а
        // назначение обязано быть детерминированным — берём первый browser_id.
        insert_worker_on_proxy(&writer, "br-2", assigned);
        insert_worker_on_proxy(&writer, "br-1", assigned);
        insert_proxy_usage(&writer, assigned, 10.0);
        insert_proxy_usage(&writer, assigned, 20.0);
        // Счётчик по proxy_id: строка usage достаётся своему прокси, а не всем.
        insert_proxy_usage(&writer, free, 30.0);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_proxies().expect("список читается");

        assert_eq!(rows.len(), 2, "join не размножает строки прокси");
        assert_eq!(rows[0].id, assigned, "порядок — по id");
        assert_eq!(
            rows[0].assigned_browser_id.as_deref(),
            Some("br-1"),
            "назначение — browser_id воркера на этом прокси"
        );
        assert_eq!(rows[0].usage_count, 2, "usage_count — строки proxy_usage");
        assert_eq!(rows[1].id, free);
        assert_eq!(
            rows[1].assigned_browser_id, None,
            "прокси без воркера — NULL, а не пустая строка"
        );
        assert_eq!(rows[1].usage_count, 1);
    }

    #[test]
    fn list_proxies_reports_liveness_nulls_and_hides_credentials() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        writer
            .execute(
                "INSERT INTO proxies (label, scheme, host, port, country, latency_ms, \
                  is_alive, fail_count, last_checked_at, last_error, username, password) \
                 VALUES ('проверенный', 'socks5', 'live.example', 1080, 'DE', 150, \
                  1, 0, 1000.0, NULL, 'user', 'secret-password')",
                [],
            )
            .expect("строка proxies с кредами вставляется");
        let live_id = writer.last_insert_rowid();

        writer
            .execute(
                "INSERT INTO proxies (scheme, host, port, is_alive, fail_count, last_error) \
                 VALUES ('http', 'dead.example', 8080, 0, 3, 'connection refused')",
                [],
            )
            .expect("мёртвая строка proxies вставляется");
        let dead_id = writer.last_insert_rowid();

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_proxies().expect("список читается");

        let live = rows
            .iter()
            .find(|row| row.id == live_id)
            .expect("живая строка в списке");
        assert!(live.is_alive);
        assert_eq!(live.label.as_deref(), Some("проверенный"));
        assert_eq!(live.scheme, "socks5");
        assert_eq!(live.host, "live.example");
        assert_eq!(live.port, 1080);
        assert_eq!(live.country.as_deref(), Some("DE"));
        assert_eq!(live.latency_ms, Some(150));
        assert_eq!(live.last_checked_at, Some(1000.0));
        assert_eq!(live.last_error, None);
        assert_eq!(live.fail_count, 0);

        let dead = rows
            .iter()
            .find(|row| row.id == dead_id)
            .expect("мёртвая строка в списке");
        assert!(!dead.is_alive, "is_alive = 0 читается как false");
        assert_eq!(dead.fail_count, 3);
        assert_eq!(dead.last_error.as_deref(), Some("connection refused"));
        assert_eq!(dead.label, None);
        assert_eq!(dead.country, None);
        assert_eq!(dead.latency_ms, None);
        assert_eq!(dead.last_checked_at, None);

        // Маскирование: в сериализованной строке нет кредов, но есть все
        // поля контракта GET /control/proxies.
        let json = serde_json::to_value(dead).expect("строка сериализуется");
        let object = json.as_object().expect("JSON — объект");
        assert!(
            !object.contains_key("username") && !object.contains_key("password"),
            "креды не должны покидать читалку: {object:?}"
        );
        assert!(
            !serde_json::to_string(dead)
                .expect("строка сериализуется")
                .contains("secret-password"),
            "пароль не должен попасть в выдачу"
        );
        for field in [
            "id",
            "label",
            "scheme",
            "host",
            "port",
            "country",
            "latency_ms",
            "is_alive",
            "fail_count",
            "last_checked_at",
            "last_error",
            "assigned_browser_id",
            "usage_count",
        ] {
            assert!(
                object.contains_key(field),
                "контрактная колонка {field} должна попасть в ответ"
            );
        }
    }

    /// Строка `profiles` для тестов списка: прокси и статус проставляются
    /// явно, остальные поля остаются NULL, как в реальных «сырых» профилях.
    fn insert_profile(conn: &Connection, name: &str, proxy_id: Option<i64>) -> i64 {
        conn.execute(
            "INSERT INTO profiles (name, proxy_id, status) VALUES (?1, ?2, 'free')",
            rusqlite::params![name, proxy_id],
        )
        .expect("строка profiles вставляется");
        conn.last_insert_rowid()
    }

    /// Прокси для тестов профилей: с меткой и с теми же кредами, что в
    /// реальных данных, — маскирование проверяется на живых значениях.
    fn insert_labelled_proxy(conn: &Connection, label: &str, host: &str, port: i64) -> i64 {
        conn.execute(
            "INSERT INTO proxies (label, scheme, host, port, username, password) \
             VALUES (?1, 'http', ?2, ?3, 'user', 'secret-password')",
            rusqlite::params![label, host, port],
        )
        .expect("строка proxies вставляется");
        conn.last_insert_rowid()
    }

    fn insert_worker_on_profile(conn: &Connection, browser_id: &str, profile_id: i64) {
        conn.execute(
            "INSERT INTO workers (browser_id, profile_id) VALUES (?1, ?2)",
            rusqlite::params![browser_id, profile_id],
        )
        .expect("строка workers вставляется");
    }

    #[test]
    fn list_profiles_on_empty_db_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[]);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader.list_profiles().expect("пустая БД — пустой список"),
            vec![] as Vec<ProfileRow>,
            "отсутствие профилей — не ошибка чтения"
        );
    }

    #[test]
    fn list_profiles_joins_proxy_and_worker_without_row_multiplication() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        let proxy = insert_labelled_proxy(&writer, "немецкий", "a.example", 8080);
        // Два профиля на одном прокси: join не размножает строки.
        let assigned = insert_profile(&writer, "alpha", Some(proxy));
        let idle = insert_profile(&writer, "beta", Some(proxy));
        // Два воркера на одном профиле: строка не размножается, а выбор
        // обязан быть детерминированным — берём первый browser_id.
        insert_worker_on_profile(&writer, "br-2", assigned);
        insert_worker_on_profile(&writer, "br-1", assigned);
        let bare = insert_profile(&writer, "gamma", None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_profiles().expect("список читается");

        assert_eq!(rows.len(), 3, "join не размножает строки профилей");
        assert_eq!(
            rows.iter().map(|row| row.id).collect::<Vec<_>>(),
            vec![assigned, idle, bare],
            "порядок — по id"
        );
        assert_eq!(
            rows[0].assigned_browser_id.as_deref(),
            Some("br-1"),
            "назначение — browser_id воркера на этом профиле"
        );
        assert_eq!(rows[0].proxy_id, Some(proxy));
        assert_eq!(rows[0].proxy_label.as_deref(), Some("немецкий"));
        assert_eq!(rows[0].proxy_address.as_deref(), Some("a.example:8080"));
        // Тот же прокси у второго профиля: строка профиля остаётся одна.
        assert_eq!(rows[1].assigned_browser_id, None);
        assert_eq!(rows[1].proxy_label.as_deref(), Some("немецкий"));
        assert_eq!(rows[1].proxy_address.as_deref(), Some("a.example:8080"));
    }

    #[test]
    fn list_profiles_reports_nulls_for_profile_without_proxy() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);
        let bare = insert_profile(&writer, "без всего", None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_profiles().expect("список читается");

        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.id, bare);
        assert_eq!(row.name, "без всего");
        assert_eq!(row.status, "free");
        // Профиль без прокси: и сам прокси, и его поля — NULL, а не пустые строки.
        assert_eq!(row.proxy_id, None);
        assert_eq!(row.proxy_label, None);
        assert_eq!(row.proxy_address, None);
        assert_eq!(row.assigned_browser_id, None);
        assert_eq!(row.key_ref, None);
        assert_eq!(row.user_agent, None);
        assert_eq!(row.locale, None);
        assert_eq!(row.timezone, None);
        assert_eq!(row.last_used_at, None);
    }

    #[test]
    fn list_profiles_without_fields_column_reports_null() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);
        // Схема движка содержит profiles.fields (миграция 002); старую схему
        // для теста моделируем удалением колонки: читалка не должна падать,
        // а поле на старой схеме отдаётся NULL.
        writer
            .execute("ALTER TABLE profiles DROP COLUMN fields", [])
            .expect("колонка fields убирается для моделирования старой схемы");
        insert_profile(&writer, "старая схема", None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_profiles().expect("список читается");

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].fields, None);
    }

    #[test]
    fn list_profiles_returns_raw_fields_json_when_column_exists() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);
        // Колонка уже есть в схеме (миграция 002) — вставляем строку напрямую.
        writer
            .execute(
                "INSERT INTO profiles (name, status, fields) \
                 VALUES ('с полями', 'blocked', '{\"note\":\"осторожно\",\"n\":2}')",
                [],
            )
            .expect("строка profiles с fields вставляется");
        let filled = writer.last_insert_rowid();
        insert_profile(&writer, "без полей", None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_profiles().expect("список читается");

        assert_eq!(rows.len(), 2);
        let with_fields = rows
            .iter()
            .find(|row| row.id == filled)
            .expect("строка с fields в списке");
        // Сырая строка, а не разобранный объект: JSON разбирает фронтенд.
        assert_eq!(
            with_fields.fields.as_deref(),
            Some(r#"{"note":"осторожно","n":2}"#),
            "fields должен отдаваться сырой JSON-строкой"
        );
        assert_eq!(with_fields.status, "blocked");
        // На свежей схеме колонка NOT NULL DEFAULT '{}': «без полей» — это
        // пустой JSON-объект, а не NULL (NULL остаётся для старых схем).
        assert_eq!(rows[1].fields.as_deref(), Some("{}"));
    }

    #[test]
    fn list_profiles_serializes_contract_fields_and_hides_proxy_credentials() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);
        let proxy = insert_labelled_proxy(&writer, "с кредами", "cred.example", 3128);
        insert_profile(&writer, "аккаунт", Some(proxy));

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_profiles().expect("список читается");

        let json = serde_json::to_value(&rows[0]).expect("строка сериализуется");
        let object = json.as_object().expect("JSON — объект");
        assert!(
            !object.contains_key("username") && !object.contains_key("password"),
            "креды прокси не должны покидать читалку: {object:?}"
        );
        assert!(
            !serde_json::to_string(&rows[0])
                .expect("строка сериализуется")
                .contains("secret-password"),
            "пароль прокси не должен попасть в выдачу"
        );
        for field in [
            "id",
            "name",
            "key_ref",
            "proxy_id",
            "user_agent",
            "locale",
            "timezone",
            "status",
            "last_used_at",
            "fields",
            "assigned_browser_id",
            "proxy_label",
            "proxy_address",
        ] {
            assert!(
                object.contains_key(field),
                "контрактная колонка {field} должна попасть в ответ"
            );
        }
    }

    // --- list_diagnostics -------------------------------------------------

    /// Строка `diagnostics` для тестов: заполняются только поля, важные
    /// конкретному тесту, остальные остаются NULL, как в реальном снимке.
    fn insert_diagnostic(
        conn: &Connection,
        ts: f64,
        browser_id: Option<&str>,
        ip: Option<&str>,
    ) -> i64 {
        conn.execute(
            "INSERT INTO diagnostics (ts, browser_id, ip) VALUES (?1, ?2, ?3)",
            rusqlite::params![ts, browser_id, ip],
        )
        .expect("строка diagnostics вставляется");
        conn.last_insert_rowid()
    }

    #[test]
    fn list_diagnostics_on_empty_db_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[]);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader
                .list_diagnostics()
                .expect("пустая БД — пустой список"),
            vec![] as Vec<DiagnosticRow>,
            "отсутствие снимков — не ошибка чтения"
        );
    }

    #[test]
    fn list_diagnostics_returns_freshest_snapshot_per_browser() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        // Снимки одного воркера вперемешку по времени: нужен самый свежий
        // по ts, а не последняя вставленная и не первая попавшаяся строка.
        insert_diagnostic(&writer, 100.0, Some("br-1"), Some("1.1.1.1"));
        insert_diagnostic(&writer, 300.0, Some("br-1"), Some("3.3.3.3"));
        insert_diagnostic(&writer, 200.0, Some("br-1"), Some("2.2.2.2"));
        insert_diagnostic(&writer, 250.0, Some("br-2"), Some("4.4.4.4"));

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_diagnostics().expect("список читается");

        assert_eq!(
            rows.len(),
            2,
            "на воркера — ровно один последний снимок, а не вся история"
        );
        assert_eq!(rows[0].browser_id.as_deref(), Some("br-1"));
        assert_eq!(rows[0].ts, 300.0, "строка br-1 — самый свежий снимок");
        assert_eq!(rows[0].ip.as_deref(), Some("3.3.3.3"));
        assert_eq!(rows[1].browser_id.as_deref(), Some("br-2"));
        assert_eq!(rows[1].ip.as_deref(), Some("4.4.4.4"));
    }

    #[test]
    fn list_diagnostics_breaks_equal_ts_ties_by_insertion_order() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        // Одинаковый ts бывает при пакетной записи: свежее считается то,
        // что вставлено позже (больший id), а не произвольная строка.
        insert_diagnostic(&writer, 500.0, Some("br-1"), Some("old.example"));
        insert_diagnostic(&writer, 500.0, Some("br-1"), Some("new.example"));

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_diagnostics().expect("список читается");

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].ip.as_deref(), Some("new.example"));
    }

    #[test]
    fn list_diagnostics_keeps_null_columns_and_raw_json_untouched() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        writer
            .execute(
                "INSERT INTO diagnostics (ts, browser_id, headers, suspicion_flags) \
                 VALUES (1000.0, 'br-1', \
                  '{\"User-Agent\":\"Mozilla/5.0\",\"Accept-Language\":\"de-DE\"}', \
                  '[\"language_mismatch\",\"ua_old\"]')",
                [],
            )
            .expect("снимок с JSON вставляется");
        writer
            .execute(
                "INSERT INTO diagnostics (ts, browser_id, headers, suspicion_flags) \
                 VALUES (900.0, 'br-2', '{broken', 'тоже не JSON')",
                [],
            )
            .expect("снимок с битым JSON вставляется");
        // Снимок без данных вообще: только время и воркер.
        insert_diagnostic(&writer, 800.0, Some("br-3"), None);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_diagnostics().expect("список читается");
        assert_eq!(rows.len(), 3);

        let filled = rows
            .iter()
            .find(|row| row.browser_id.as_deref() == Some("br-1"))
            .expect("заполненный снимок в списке");
        // Сырые строки, а не разобранный JSON: парсинг — работа фронтенда.
        assert_eq!(
            filled.headers.as_deref(),
            Some(r#"{"User-Agent":"Mozilla/5.0","Accept-Language":"de-DE"}"#)
        );
        assert_eq!(
            filled.suspicion_flags.as_deref(),
            Some(r#"["language_mismatch","ua_old"]"#)
        );
        assert_eq!(filled.ip, None, "не заполненные колонки — NULL");
        assert_eq!(filled.country, None);
        assert_eq!(filled.user_agent, None);
        assert_eq!(filled.accept_language, None);
        assert_eq!(filled.timezone_id, None);
        assert_eq!(filled.screen_w, None);
        assert_eq!(filled.screen_h, None);
        assert_eq!(filled.platform, None);
        assert_eq!(filled.webgl_vendor, None);
        assert_eq!(filled.webgl_renderer, None);
        assert_eq!(filled.browser_version, None);
        assert_eq!(filled.proxy_id, None);

        // Битый JSON не теряется и не «чинится»: читалка отдаёт строку как есть.
        let broken = rows
            .iter()
            .find(|row| row.browser_id.as_deref() == Some("br-2"))
            .expect("снимок с битым JSON в списке");
        assert_eq!(broken.headers.as_deref(), Some("{broken"));
        assert_eq!(broken.suspicion_flags.as_deref(), Some("тоже не JSON"));

        // Контракт колонок: фронт получает все 16 полей, включая NULL.
        let json = serde_json::to_value(filled).expect("строка сериализуется");
        let object = json.as_object().expect("JSON — объект");
        for field in [
            "ts",
            "browser_id",
            "proxy_id",
            "ip",
            "country",
            "user_agent",
            "accept_language",
            "timezone_id",
            "screen_w",
            "screen_h",
            "platform",
            "webgl_vendor",
            "webgl_renderer",
            "browser_version",
            "headers",
            "suspicion_flags",
        ] {
            assert!(
                object.contains_key(field),
                "контрактная колонка {field} должна попасть в ответ"
            );
        }
    }

    #[test]
    fn list_diagnostics_groups_rows_without_browser_id_and_puts_them_last() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        insert_diagnostic(&writer, 10.0, None, Some("anonymous-old"));
        insert_diagnostic(&writer, 20.0, None, Some("anonymous-new"));
        insert_diagnostic(&writer, 15.0, Some("br-1"), Some("1.1.1.1"));

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_diagnostics().expect("список читается");

        assert_eq!(
            rows.len(),
            2,
            "снимки без browser_id складываются в одну группу"
        );
        assert_eq!(rows[0].browser_id.as_deref(), Some("br-1"));
        assert_eq!(
            rows[1].browser_id, None,
            "анонимный снимок не выбрасывается, но идёт последним"
        );
        assert_eq!(rows[1].ip.as_deref(), Some("anonymous-new"));
        assert_eq!(rows[1].ts, 20.0);
    }

    /// Строка `captcha_events` для ленты CAPTCHA: время, воркер и исход
    /// решения; остальные колонки в тесте заполняются явно.
    fn insert_captcha(conn: &Connection, ts: f64, browser_id: Option<&str>, solved: bool) -> i64 {
        conn.execute(
            "INSERT INTO captcha_events (ts, browser_id, solved) VALUES (?1, ?2, ?3)",
            rusqlite::params![ts, browser_id, solved as i64],
        )
        .expect("строка captcha_events вставляется");
        conn.last_insert_rowid()
    }

    #[test]
    fn list_captcha_events_on_empty_db_returns_nothing() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path, &[]);

        let reader = DbReader::open(&path).expect("БД открывается");

        assert_eq!(
            reader
                .list_captcha_events(10)
                .expect("пустая БД — пустой список"),
            vec![] as Vec<CaptchaEventRow>,
            "отсутствие событий — не ошибка чтения"
        );
    }

    #[test]
    fn list_captcha_events_returns_newest_first_and_breaks_ts_ties_by_id() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        let first = insert_captcha(&writer, 100.0, Some("br-1"), true);
        let tied_old = insert_captcha(&writer, 300.0, Some("br-2"), false);
        let tied_new = insert_captcha(&writer, 300.0, Some("br-3"), false);
        insert_captcha(&writer, 200.0, Some("br-4"), true);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_captcha_events(10).expect("список читается");

        assert_eq!(
            rows.iter().map(|row| row.ts).collect::<Vec<_>>(),
            vec![300.0, 300.0, 200.0, 100.0],
            "порядок — по убыванию ts"
        );
        assert_eq!(
            rows[0].id, tied_new,
            "равный ts разрывается по id: свежевставленная строка первой"
        );
        assert_eq!(rows[1].id, tied_old);
        assert_eq!(rows[3].id, first, "самое старое событие — последним");
        assert_eq!(rows[0].browser_id.as_deref(), Some("br-3"));
    }

    #[test]
    fn list_captcha_events_caps_limit_and_treats_zero_as_empty() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        // Потолок строк — тот же, что у логов: запрос с большим limit
        // усекается, а не выгружает в UI всю историю событий.
        let total = MAX_LOGS_LIMIT + 25;
        for index in 0..total {
            insert_captcha(&writer, f64::from(index), Some("br-1"), false);
        }

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader
            .list_captcha_events(u32::MAX)
            .expect("лимит усекается без ошибки");

        assert_eq!(rows.len(), MAX_LOGS_LIMIT as usize);
        assert_eq!(
            rows[0].ts,
            f64::from(total - 1),
            "усечение оставляет самые свежие события"
        );
        assert!(
            reader
                .list_captcha_events(0)
                .expect("limit = 0 — пустой список")
                .is_empty(),
            "нулевой limit не должен читать таблицу"
        );
    }

    #[test]
    fn list_captcha_events_keeps_null_columns_and_converts_solved_flag() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let writer = seed(&path, &[]);

        writer
            .execute(
                "INSERT INTO captcha_events (ts, browser_id, page_url, sitekey, \
                  screenshot_path, solved, solver, elapsed_ms) \
                  VALUES (1000.0, 'br-1', 'https://www.google.com/search?q=ad', \
                  '6Le-waAUAAAAAPuu', '/tmp/engine/screenshots/br-1.png', 1, \
                  '2captcha', 4200)",
                [],
            )
            .expect("событие с полными полями вставляется");
        // Событие без воркера, страницы и решателя: NULL — норма, а не ошибка.
        insert_captcha(&writer, 900.0, None, false);

        let reader = DbReader::open(&path).expect("БД открывается");
        let rows = reader.list_captcha_events(10).expect("список читается");
        assert_eq!(rows.len(), 2);

        let solved = &rows[0];
        assert_eq!(solved.id, 1);
        assert_eq!(solved.ts, 1000.0);
        assert_eq!(solved.browser_id.as_deref(), Some("br-1"));
        assert_eq!(
            solved.page_url.as_deref(),
            Some("https://www.google.com/search?q=ad")
        );
        assert_eq!(solved.sitekey.as_deref(), Some("6Le-waAUAAAAAPuu"));
        assert_eq!(
            solved.screenshot_path.as_deref(),
            Some("/tmp/engine/screenshots/br-1.png")
        );
        assert!(solved.solved, "1 в колонке solved — это true");
        assert_eq!(solved.solver.as_deref(), Some("2captcha"));
        assert_eq!(solved.elapsed_ms, Some(4200));

        let unsolved = &rows[1];
        assert_eq!(unsolved.browser_id, None);
        assert_eq!(unsolved.page_url, None);
        assert_eq!(unsolved.sitekey, None);
        assert_eq!(unsolved.screenshot_path, None);
        assert!(!unsolved.solved, "0 в колонке solved — это false");
        assert_eq!(unsolved.solver, None);
        assert_eq!(unsolved.elapsed_ms, None);
        assert_eq!(unsolved.proxy_id, None);

        // Контракт для фронта: все поля колонок доходят в JSON.
        let json = serde_json::to_value(solved).expect("строка сериализуется");
        let object = json.as_object().expect("JSON — объект");
        for field in [
            "id",
            "ts",
            "browser_id",
            "proxy_id",
            "page_url",
            "sitekey",
            "screenshot_path",
            "solved",
            "solver",
            "elapsed_ms",
        ] {
            assert!(
                object.contains_key(field),
                "контрактное поле {field} должно попасть в ответ"
            );
        }
    }
}
