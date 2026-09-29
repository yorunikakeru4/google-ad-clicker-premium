//! Контракт read-only читалки боевой БД (TDD: красный → зелёный).
//!
//! Движок (`engine/db`) пишет в WAL, UI читает тот же файл, но только
//! соединением `SQLITE_OPEN_READ_ONLY`: без создания файла, без записи и
//! с `busy_timeout`, зеркальным `BUSY_TIMEOUT_MS` движка.

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU32, Ordering};

    use rusqlite::Connection;

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
        conn.execute_batch(SCHEMA_SQL).expect("схема движка применяется");
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

        assert!(matches!(err, DbError::OpenFailed { .. }), "получено: {err:?}");
        assert!(
            err.to_string().contains("каталог"),
            "текст должен объяснять причину: {err}"
        );
    }

    #[test]
    fn open_rejects_foreign_file_without_modifying_it() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let garbage = "это не sqlite, а обычный текстовый файл".as_bytes().to_vec();
        fs::write(&path, &garbage).expect("файл пишется тестом");

        let err = DbReader::open(&path).expect_err("не-SQLite файл должен быть отвергнут");

        assert!(matches!(err, DbError::OpenFailed { .. }), "получено: {err:?}");
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

        assert!(matches!(err, DbError::OpenFailed { .. }), "получено: {err:?}");
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
            vec![entry(&data[2]), entry(&data[1]), entry(&data[0])],
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
            "при равном ts свежее вставленная строка идёт первой"
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
        assert!(debug.is_empty(), "неизвестный уровень не должен ничего возвращать");
    }

    #[test]
    fn list_logs_filters_by_category() {
        let tmp = TempDb::new();
        let path = tmp.path();
        let data = [
            Row::new(1.0, "INFO", "клик")
                .category("click")
                .browser("b1"),
            Row::new(2.0, "INFO", "прокси").category("proxy").browser("b1"),
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
        assert_eq!(rows[0].browser_id, None, "NULL browser_id должен стать None");
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
            timeout_ms,
            BUSY_TIMEOUT_MS as i64,
            "читатель ждёт ровно столько, сколько ждёт движок"
        );
    }
}
