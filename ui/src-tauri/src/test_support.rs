//! Общие тестовые фикстуры: временная БД с боевой схемой движка.
//!
//! Схема берётся из того же файла, который применяет движок, — тесты читают
//! реальный контракт, а не копию.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

use rusqlite::Connection;

/// Схема движка — источник истины для тестов читалки.
pub const SCHEMA_SQL: &str = include_str!("../../../engine/db/schema.sql");

static NEXT_TEMP_ID: AtomicU32 = AtomicU32::new(0);

/// Временный каталог: падение теста не оставляет мусор в системном /tmp.
pub struct TempDb {
    dir: PathBuf,
}

impl TempDb {
    pub fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "ui-metrics-reader-{}-{}",
            std::process::id(),
            NEXT_TEMP_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&dir).expect("временный каталог должен создаться");
        Self { dir }
    }

    pub fn path(&self) -> PathBuf {
        self.dir.join("adclicker.db")
    }
}

impl Drop for TempDb {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

/// Создаёт БД так же, как движок: WAL-журнал первым, затем схема. Возвращает
/// соединение писателя — данные остаются в `-wal`, и читалка видит их до
/// закрытия.
pub fn seed(path: &Path) -> Connection {
    let conn = Connection::open(path).expect("писатель должен создать файл БД");
    conn.execute_batch("PRAGMA journal_mode = WAL;")
        .expect("WAL включается писателем");
    conn.execute_batch(SCHEMA_SQL)
        .expect("схема движка применяется");
    conn
}

/// Строка `runs` для тестов агрегатов: время старта/финиша, статус, ошибка
/// и явное `created_at` (иначе срабатывает DEFAULT «сейчас»).
pub struct RunRow<'a> {
    pub started_at: Option<f64>,
    pub ended_at: Option<f64>,
    pub status: &'a str,
    pub error: Option<&'a str>,
    pub created_at: Option<f64>,
}

impl<'a> RunRow<'a> {
    pub fn new(status: &'a str) -> Self {
        Self {
            started_at: None,
            ended_at: None,
            status,
            error: None,
            created_at: None,
        }
    }

    pub fn started(mut self, ts: f64) -> Self {
        self.started_at = Some(ts);
        self
    }

    pub fn ended(mut self, ts: f64) -> Self {
        self.ended_at = Some(ts);
        self
    }

    pub fn error(mut self, text: &'a str) -> Self {
        self.error = Some(text);
        self
    }

    pub fn created(mut self, ts: f64) -> Self {
        self.created_at = Some(ts);
        self
    }
}

pub fn insert_run(conn: &Connection, row: &RunRow<'_>) {
    match row.created_at {
        Some(created) => conn
            .execute(
                "INSERT INTO runs (started_at, ended_at, status, error, created_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![row.started_at, row.ended_at, row.status, row.error, created],
            )
            .expect("строка runs вставляется"),
        None => conn
            .execute(
                "INSERT INTO runs (started_at, ended_at, status, error) VALUES (?1, ?2, ?3, ?4)",
                rusqlite::params![row.started_at, row.ended_at, row.status, row.error],
            )
            .expect("строка runs вставляется"),
    };
}

pub fn insert_click(conn: &Connection, ts: f64) {
    conn.execute(
        "INSERT INTO clicks (ts, url) VALUES (?1, 'https://example.test/')",
        rusqlite::params![ts],
    )
    .expect("строка clicks вставляется");
}

pub fn insert_network_request(conn: &Connection, ts: f64, browser_id: Option<&str>) {
    conn.execute(
        "INSERT INTO network_requests (ts, browser_id) VALUES (?1, ?2)",
        rusqlite::params![ts, browser_id],
    )
    .expect("строка network_requests вставляется");
}

pub fn insert_captcha_event(conn: &Connection, ts: f64) {
    conn.execute(
        "INSERT INTO captcha_events (ts) VALUES (?1)",
        rusqlite::params![ts],
    )
    .expect("строка captcha_events вставляется");
}

/// Строка `workers` для тестов живости: `status` по умолчанию как в схеме.
pub struct WorkerRow<'a> {
    pub browser_id: &'a str,
    pub pid: Option<i64>,
    pub status: &'a str,
    pub started_at: Option<f64>,
    pub heartbeat_at: Option<f64>,
    pub last_error: Option<&'a str>,
}

impl<'a> WorkerRow<'a> {
    pub fn new(browser_id: &'a str) -> Self {
        Self {
            browser_id,
            pid: None,
            status: "starting",
            started_at: None,
            heartbeat_at: None,
            last_error: None,
        }
    }

    pub fn pid(mut self, pid: i64) -> Self {
        self.pid = Some(pid);
        self
    }

    pub fn status(mut self, status: &'a str) -> Self {
        self.status = status;
        self
    }

    pub fn started(mut self, ts: f64) -> Self {
        self.started_at = Some(ts);
        self
    }

    pub fn heartbeat(mut self, ts: f64) -> Self {
        self.heartbeat_at = Some(ts);
        self
    }

    pub fn last_error(mut self, text: &'a str) -> Self {
        self.last_error = Some(text);
        self
    }

    pub fn insert(self, conn: &Connection) {
        conn.execute(
            "INSERT INTO workers (browser_id, pid, status, started_at, heartbeat_at, last_error) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                self.browser_id,
                self.pid,
                self.status,
                self.started_at,
                self.heartbeat_at,
                self.last_error
            ],
        )
        .expect("строка workers вставляется");
    }
}
