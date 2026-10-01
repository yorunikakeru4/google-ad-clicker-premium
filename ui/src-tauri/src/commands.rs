//! Tauri-команды для фронта: открытие боевой БД и все read-only чтения.
//!
//! Путь к базе резолвится так же, как `engine/log.py::resolve_db_path`:
//! явный аргумент → env `ADCLICKER_DB` → `adclicker.db` в рабочем каталоге
//! демона (`daemon::working_dir`). Ни один путь не захардкожен — его всегда
//! передаёт вызывающий.

use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use tauri::State;

use crate::db::{
    CaptchaEventRow, DbError, DbReader, DbSize, DiagnosticRow, LogEntry, LogFilters, LogPageEntry,
    ProfileRow, ProxyRow,
};
use crate::metrics::{ActiveWorker, HourlyClicks, RequestsLastHour, RunsSummary, UptimeSummary};

/// Имя переменной окружения с путём к БД — зеркало `DB_ENV_VAR` из
/// `engine/log.py`.
pub const DB_ENV_VAR: &str = "ADCLICKER_DB";

/// Дефолтное имя файла базы — зеркало `DEFAULT_DB_NAME` из `engine/log.py`.
pub const DEFAULT_DB_NAME: &str = "adclicker.db";

/// Состояние читателя для команд: открытое соединение или ничего.
///
/// Соединение не `Sync`, поэтому доступ только через замок; каждый вызов
/// команды держит его ровно на время одного чтения.
#[derive(Debug, Default)]
pub struct DbState(pub Mutex<Option<DbReader>>);

/// Резолвит путь к БД: явный аргумент → env `ADCLICKER_DB` → `adclicker.db`
/// в рабочем каталоге демона.
///
/// Дефолт ищется не в cwd процесса, а там, где файл создаёт демон
/// (`daemon::working_dir`): из Finder у приложения cwd — `/`, а демон пишет
/// базу в свой каталог (корень репозитория в dev, каталог данных в
/// упакованном запуске). Без этого UI смотрел бы не на тот файл и честно
/// отчитывался «база не найдена».
///
/// `~` разворачивается в HOME, относительный путь — в абсолютный от cwd,
/// как `Path(...).expanduser().resolve()` в Python.
pub fn resolve_db_path(explicit: Option<&str>) -> PathBuf {
    let vars: HashMap<String, String> = std::env::vars().collect();
    let home = std::env::var("HOME").ok();
    let cwd = std::env::current_dir().ok();
    let workdir = cwd
        .as_deref()
        .and_then(|dir| crate::daemon::working_dir(&vars, dir).ok());

    resolve_db_path_in(
        explicit,
        std::env::var(DB_ENV_VAR).ok().as_deref(),
        home.as_deref(),
        cwd.as_deref(),
        workdir.as_deref(),
    )
}

/// Чистое ядро резолва: параметры окружения приходят аргументами, чтобы
/// тесты не трогали состояние процесса. `workdir` — каталог демона, куда
/// база и пишется; `None` — fallback на каталог запуска.
fn resolve_db_path_in(
    explicit: Option<&str>,
    env: Option<&str>,
    home: Option<&str>,
    cwd: Option<&Path>,
    workdir: Option<&Path>,
) -> PathBuf {
    let raw = explicit
        .filter(|value| !value.is_empty())
        .or_else(|| env.filter(|value| !value.is_empty()));
    if let Some(raw) = raw {
        return absolutize(&expand_home(raw, home), cwd);
    }

    let base = workdir
        .map(Path::to_path_buf)
        .or_else(|| cwd.map(Path::to_path_buf))
        .unwrap_or_default();
    absolutize(&base.join(DEFAULT_DB_NAME), cwd)
}

/// `~` и `~/...` — в HOME. Без HOME путь остаётся как есть, как и в Python.
fn expand_home(raw: &str, home: Option<&str>) -> PathBuf {
    if raw == "~" {
        return home.map_or_else(|| PathBuf::from(raw), PathBuf::from);
    }
    match (raw.strip_prefix("~/"), home) {
        (Some(rest), Some(home)) => Path::new(home).join(rest),
        _ => PathBuf::from(raw),
    }
}

/// Абсолютный путь с схлопнутыми `.`/`..`. Лексически, без ФС: файла ещё
/// может не быть, а текст ошибки читалки должен называть каноничный путь.
fn absolutize(path: &Path, cwd: Option<&Path>) -> PathBuf {
    let joined = match cwd {
        Some(cwd) if !path.is_absolute() => cwd.join(path),
        _ => path.to_path_buf(),
    };

    let mut normalized = PathBuf::new();
    for component in joined.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => match normalized.components().next_back() {
                Some(Component::Normal(_)) => {
                    normalized.pop();
                }
                // Корень — сам себе родитель: `/..` остаётся `/`.
                Some(Component::RootDir) | Some(Component::Prefix(_)) => {}
                _ => normalized.push(".."),
            },
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

/// Открывает (или переоткрывает) читателя и подставляет его в состояние.
/// Возвращает резолвленный путь, которым база реально открылась.
///
/// Ошибка открытия не трогает уже открытого читателя: фронт получает текст
/// ошибки, а состояние остаётся рабочим.
pub fn open_db(state: &Mutex<Option<DbReader>>, path: Option<&str>) -> Result<String, DbError> {
    let resolved = resolve_db_path(path);
    let reader = DbReader::open(&resolved)?;
    *lock(state) = Some(reader);
    Ok(resolved.display().to_string())
}

/// Выполняет чтение через открытого читателя. `db_open` не вызван — внятная
/// ошибка для UI, а не паника.
pub fn with_reader<R>(
    state: &Mutex<Option<DbReader>>,
    read: impl FnOnce(&DbReader) -> Result<R, DbError>,
) -> Result<R, DbError> {
    match &*lock(state) {
        Some(reader) => read(reader),
        None => Err(DbError::NotOpen),
    }
}

/// Замок состояния: отравленный поток не должен делать читателя
/// недоступным — мы только читаем, откатывать нечего.
fn lock(state: &Mutex<Option<DbReader>>) -> MutexGuard<'_, Option<DbReader>> {
    state
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Открывает боевую БД для всех последующих чтений. `path` опционален:
/// без него действует [`resolve_db_path`]. Возвращает резолвленный путь.
#[tauri::command]
pub fn db_open(state: State<'_, DbState>, path: Option<String>) -> Result<String, DbError> {
    open_db(&state.0, path.as_deref())
}

/// Строки логов по убыванию `ts` — экран Logs, первая страница.
#[tauri::command]
pub fn list_logs(
    state: State<'_, DbState>,
    limit: u32,
    level: Option<String>,
    category: Option<String>,
    browser_id: Option<String>,
) -> Result<Vec<LogEntry>, DbError> {
    with_reader(&state.0, |reader| {
        reader.list_logs(
            limit,
            level.as_deref(),
            category.as_deref(),
            browser_id.as_deref(),
        )
    })
}

/// Курсорная страница логов. Курсор — `(before_ts, before_id)` последней
/// строки предыдущей страницы; `before_id` опционален, но без него равные
/// `ts` на границе страниц теряются (см. [`DbReader::list_logs_page`]).
/// `since`/`until` — включительное окно времени по `ts`, каждая граница
/// опциональна; `until < since` — пустая страница, а не ошибка.
// Восемь аргументов — потолок читаемости, но это плоский контракт с
// фронтом (camelCase в invoke): отдельная структура параметров была бы
// лишним слоем без выигрыша.
#[allow(clippy::too_many_arguments)]
#[tauri::command]
pub fn list_logs_page(
    state: State<'_, DbState>,
    limit: u32,
    level: Option<String>,
    category: Option<String>,
    browser_id: Option<String>,
    since: Option<f64>,
    until: Option<f64>,
    before_ts: Option<f64>,
    before_id: Option<i64>,
) -> Result<Vec<LogPageEntry>, DbError> {
    with_reader(&state.0, |reader| {
        let filters = LogFilters {
            level,
            category,
            browser_id,
            since,
            until,
        };
        reader.list_logs_page(&filters, before_ts, before_id, limit)
    })
}

/// Общее число строк логов под фильтрами — для пагинации экрана Logs.
/// Окно времени `since`/`until` то же, что у [`list_logs_page`].
#[tauri::command]
pub fn count_logs(
    state: State<'_, DbState>,
    level: Option<String>,
    category: Option<String>,
    browser_id: Option<String>,
    since: Option<f64>,
    until: Option<f64>,
) -> Result<i64, DbError> {
    with_reader(&state.0, |reader| {
        let filters = LogFilters {
            level,
            category,
            browser_id,
            since,
            until,
        };
        reader.count_logs(&filters)
    })
}

/// Сводка по запускам сценария с момента `since` — блок сценариев дашборда.
#[tauri::command]
pub fn runs_summary(state: State<'_, DbState>, since: f64) -> Result<RunsSummary, DbError> {
    with_reader(&state.0, |reader| reader.runs_summary(since))
}

/// Клики по часовым бакетам: `buckets` — потолок самых свежих часов.
#[tauri::command]
pub fn clicks_per_hour(
    state: State<'_, DbState>,
    since: f64,
    buckets: u32,
) -> Result<Vec<HourlyClicks>, DbError> {
    with_reader(&state.0, |reader| reader.clicks_per_hour(since, buckets))
}

/// Запросы в скользящем окне последнего часа плюс нагрузка по воркерам.
/// Порог «≥50/час» проверяет UI на возвращённом `total`.
#[tauri::command]
pub fn requests_last_hour(
    state: State<'_, DbState>,
    now: f64,
) -> Result<RequestsLastHour, DbError> {
    with_reader(&state.0, |reader| reader.requests_last_hour(now))
}

/// Доля капчи с момента `since`: `None` — «н/д» (запросов в окне нет).
/// Порог «<5%» проверяет UI.
#[tauri::command]
pub fn captcha_share(state: State<'_, DbState>, since: f64) -> Result<Option<f64>, DbError> {
    with_reader(&state.0, |reader| reader.captcha_share(since))
}

/// Воркеры со свежим heartbeat: `threshold_secs` приходит из UI.
#[tauri::command]
pub fn active_workers(
    state: State<'_, DbState>,
    now: f64,
    threshold_secs: u32,
) -> Result<Vec<ActiveWorker>, DbError> {
    with_reader(&state.0, |reader| {
        reader.active_workers(now, threshold_secs)
    })
}

/// Доля времени с живым воркером за окно в `since_hours` часов — карточка
/// Uptime. `ratio: None` — в окне нет данных (замер идёт с первого запуска
/// демона); порог «≥99%» проверяет UI.
#[tauri::command]
pub fn uptime_summary(
    state: State<'_, DbState>,
    since_hours: u32,
) -> Result<UptimeSummary, DbError> {
    with_reader(&state.0, |reader| reader.uptime_summary(since_hours))
}

/// Список прокси экрана Proxies: строки таблицы + назначенный воркер и
/// счётчик использований. Креды в ответ не входят (см. [`ProxyRow`]),
/// мутации списка идут только через control API демона, не через эту команду.
#[tauri::command]
pub fn list_proxies(state: State<'_, DbState>) -> Result<Vec<ProxyRow>, DbError> {
    with_reader(&state.0, |reader| reader.list_proxies())
}

/// Список профилей экрана Profiles: строки таблицы + назначенный воркер, прокси
/// и сырые `fields`. Креды прокси в ответ не входят (см. [`ProfileRow`]),
/// мутации списка идут только через control API демона, не через эту команду.
#[tauri::command]
pub fn list_profiles(state: State<'_, DbState>) -> Result<Vec<ProfileRow>, DbError> {
    with_reader(&state.0, |reader| reader.list_profiles())
}

/// Последний снимок диагностики на каждый воркер — экран Diagnostics.
///
/// Команда только читает: сбор снимка идёт через control API демона
/// (`POST /control/diagnostics/collect`), а в БД строка появляется
/// асинхронно — экран поллит этот список и показывает свежее.
#[tauri::command]
pub fn list_diagnostics(state: State<'_, DbState>) -> Result<Vec<DiagnosticRow>, DbError> {
    with_reader(&state.0, |reader| reader.list_diagnostics())
}

/// Последние события CAPTCHA — лента и подсветка воркеров на Dashboard.
/// `limit` усекается тем же потолком, что и логи; порядок `ts DESC, id DESC`.
#[tauri::command]
pub fn list_captcha_events(
    state: State<'_, DbState>,
    limit: u32,
) -> Result<Vec<CaptchaEventRow>, DbError> {
    with_reader(&state.0, |reader| reader.list_captcha_events(limit))
}

/// Размер файлов открытой БД: основной файл плюс `-wal`, байтами.
/// Дешёвая команда — два `stat` без чтения страниц базы, поэтому тулбар
/// Logs может опрашивать её по таймеру.
pub fn read_db_size(state: &Mutex<Option<DbReader>>) -> Result<DbSize, DbError> {
    with_reader(state, |reader| Ok(reader.size()))
}

/// Индикатор размера БД для UI. Без открытого читателя — `NotOpen`:
/// индикатор честно показывает «размер неизвестен», а не нули.
#[tauri::command]
pub fn db_size(state: State<'_, DbState>) -> Result<DbSize, DbError> {
    read_db_size(&state.0)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::Mutex;

    use super::*;
    use crate::db::{DbError, LogFilters};
    use crate::test_support::{seed, TempDb};

    #[test]
    fn resolve_db_path_prefers_explicit_over_env() {
        let path = resolve_db_path_in(
            Some("/explicit/adclicker.db"),
            Some("/env/adclicker.db"),
            Some("/home/runner"),
            Some(Path::new("/cwd")),
            None,
        );

        assert_eq!(
            path,
            PathBuf::from("/explicit/adclicker.db"),
            "явный аргумент сильнее env"
        );
    }

    #[test]
    fn resolve_db_path_falls_back_to_env_then_to_default_name() {
        let from_env = resolve_db_path_in(
            None,
            Some("/env/adclicker.db"),
            None,
            Some(Path::new("/cwd")),
            None,
        );
        assert_eq!(from_env, PathBuf::from("/env/adclicker.db"));

        let empty_arg = resolve_db_path_in(
            Some(""),
            Some("/env/adclicker.db"),
            None,
            Some(Path::new("/cwd")),
            None,
        );
        assert_eq!(
            empty_arg,
            PathBuf::from("/env/adclicker.db"),
            "пустой аргумент эквивалентен отсутствующему, как `or` в Python"
        );

        let empty_env = resolve_db_path_in(None, Some(""), None, Some(Path::new("/cwd")), None);
        assert_eq!(
            empty_env,
            PathBuf::from("/cwd/adclicker.db"),
            "пустой env — дефолтное имя adclicker.db"
        );

        let default = resolve_db_path_in(None, None, None, Some(Path::new("/cwd")), None);
        assert_eq!(
            default,
            PathBuf::from("/cwd/adclicker.db"),
            "без каталога демона — adclicker.db в каталоге запуска"
        );
    }

    #[test]
    fn resolve_db_path_defaults_to_the_daemon_working_dir() {
        // Боевой путь: демон пишет базу в свой каталог, UI обязан читать
        // оттуда же, а не из cwd графического приложения.
        let default = resolve_db_path_in(
            None,
            None,
            Some("/home/runner"),
            Some(Path::new("/")),
            Some(Path::new("/data/adclicker")),
        );
        assert_eq!(default, PathBuf::from("/data/adclicker/adclicker.db"));

        // Явный путь и env сильнее каталога демона.
        let explicit = resolve_db_path_in(
            Some("/explicit/adclicker.db"),
            None,
            None,
            None,
            Some(Path::new("/data/adclicker")),
        );
        assert_eq!(explicit, PathBuf::from("/explicit/adclicker.db"));
    }

    #[test]
    fn resolve_db_path_expands_home_and_normalizes_relative_path() {
        let home = resolve_db_path_in(
            Some("~/data/adclicker.db"),
            None,
            Some("/home/runner"),
            Some(Path::new("/cwd")),
            None,
        );
        assert_eq!(
            home,
            PathBuf::from("/home/runner/data/adclicker.db"),
            "тильда разворачивается в HOME"
        );

        let tilde_only = resolve_db_path_in(Some("~"), None, Some("/home/runner"), None, None);
        assert_eq!(tilde_only, PathBuf::from("/home/runner"));

        let dotted = resolve_db_path_in(
            Some("./sub/../adclicker.db"),
            None,
            None,
            Some(Path::new("/cwd")),
            None,
        );
        assert_eq!(
            dotted,
            PathBuf::from("/cwd/adclicker.db"),
            "относительный путь делается абсолютным, . и .. схлопываются"
        );
    }

    #[test]
    fn with_reader_reports_not_open_before_db_open() {
        let state: Mutex<Option<DbReader>> = Mutex::new(None);

        let err = with_reader(&state, |_reader| Ok(7)).expect_err("без db_open чтение невозможно");

        assert!(
            matches!(err, DbError::NotOpen),
            "ожидалась ошибка NotOpen, получено: {err:?}"
        );
        assert!(
            err.to_string().contains("db_open"),
            "текст должен подсказывать вызов db_open: {err}"
        );
    }

    #[test]
    fn db_size_requires_open_reader_and_then_reports_file_bytes() {
        let state: Mutex<Option<DbReader>> = Mutex::new(None);

        let err = read_db_size(&state).expect_err("без db_open размер неизвестен");
        assert!(
            matches!(err, DbError::NotOpen),
            "ожидалась NotOpen, получено: {err:?}"
        );

        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);
        let explicit = path.to_str().expect("путь валиден для OsStr");
        open_db(&state, Some(explicit)).expect("существующая БД открывается");

        let size = read_db_size(&state).expect("открытая БД отдаёт размер");
        assert_eq!(
            size.bytes,
            fs::metadata(&path).expect("файл БД существует").len()
        );
        assert_eq!(size.path, path.display().to_string());
    }

    #[test]
    fn open_db_stores_reader_and_keeps_it_when_next_open_fails() {
        let state: Mutex<Option<DbReader>> = Mutex::new(None);
        let tmp = TempDb::new();
        let path = tmp.path();
        let _writer = seed(&path);
        let explicit = path.to_str().expect("путь валиден для OsStr");

        let resolved = open_db(&state, Some(explicit)).expect("существующая БД открывается");
        assert_eq!(
            PathBuf::from(&resolved),
            path,
            "команда возвращает резолвленный путь, которым открылась база"
        );

        let rows = with_reader(&state, |reader| reader.count_logs(&LogFilters::default()))
            .expect("читатель подставлен в состояние");
        assert_eq!(rows, 0, "открытая база отдаёт чтение");

        let missing = path
            .parent()
            .expect("у временной БД есть каталог")
            .join("missing.db");
        let err = open_db(&state, Some(missing.to_str().expect("путь валиден")))
            .expect_err("несуществующий файл — ошибка");
        assert!(
            matches!(err, DbError::DatabaseNotFound { .. }),
            "получено: {err:?}"
        );

        let rows = with_reader(&state, |reader| reader.count_logs(&LogFilters::default()))
            .expect("неудачное открытие не сбрасывает уже открытого читателя");
        assert_eq!(rows, 0, "старый читатель остался в состоянии");
    }
}
