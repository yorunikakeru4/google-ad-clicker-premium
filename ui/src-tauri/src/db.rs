//! Read-only читалка боевой БД для экрана Logs.
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

/// Читатель боевой БД: одно соединение, строго на чтение.
///
/// Соединение не разделяется между потоками (`SQLITE_OPEN_NO_MUTEX`):
/// каждый поток держит своего читателя.
#[derive(Debug)]
pub struct DbReader {
    conn: Connection,
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
        if limit == 0 {
            return Ok(Vec::new());
        }
        let limit = limit.min(MAX_LOGS_LIMIT);

        // Фильтры собираются динамически: равенство столбца индексируется
        // (`idx_logs_browser_id` и др.), а вариант «? IS NULL OR col = ?»
        // заставлял бы SQLite сканировать индекс по ts целиком.
        let mut sql =
            String::from("SELECT ts, level, browser_id, category, message, fields FROM logs");
        let mut conditions: Vec<&str> = Vec::new();
        let mut binds: Vec<&dyn ToSql> = Vec::new();

        if level.is_some() {
            conditions.push("level = ?");
            binds.push(&level);
        }
        if category.is_some() {
            conditions.push("category = ?");
            binds.push(&category);
        }
        if browser_id.is_some() {
            conditions.push("browser_id = ?");
            binds.push(&browser_id);
        }
        if !conditions.is_empty() {
            sql.push_str(" WHERE ");
            sql.push_str(&conditions.join(" AND "));
        }
        // id — детерминированный разрыв равных ts: свежевставленная строка
        // идёт первой, и пагинация фазы 4 не будет терять и дублировать строки.
        sql.push_str(" ORDER BY ts DESC, id DESC LIMIT ?");
        binds.push(&limit);

        let mut stmt = self.conn.prepare(&sql).map_err(read_failed)?;
        let rows = stmt
            .query_map(binds.as_slice(), |row| {
                Ok(LogEntry {
                    ts: row.get(0)?,
                    level: row.get(1)?,
                    browser_id: row.get(2)?,
                    category: row.get(3)?,
                    message: row.get(4)?,
                    fields: row.get(5)?,
                })
            })
            .map_err(read_failed)?;

        rows.map(|row| row.map_err(read_failed)).collect()
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

fn read_failed(err: rusqlite::Error) -> DbError {
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
}
