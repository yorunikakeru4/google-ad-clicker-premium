//! Tauri-команды для фронта: открытие боевой БД и все read-only чтения.
//!
//! Путь к базе резолвится так же, как `engine/log.py::resolve_db_path`:
//! явный аргумент → env `ADCLICKER_DB` → `adclicker.db` в каталоге запуска.
//! Ни один путь не захардкожен — его всегда передаёт вызывающий.

#[cfg(test)]
mod tests {
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
        );
        assert_eq!(from_env, PathBuf::from("/env/adclicker.db"));

        let empty_arg = resolve_db_path_in(
            Some(""),
            Some("/env/adclicker.db"),
            None,
            Some(Path::new("/cwd")),
        );
        assert_eq!(
            empty_arg,
            PathBuf::from("/env/adclicker.db"),
            "пустой аргумент эквивалентен отсутствующему, как `or` в Python"
        );

        let empty_env = resolve_db_path_in(None, Some(""), None, Some(Path::new("/cwd")));
        assert_eq!(
            empty_env,
            PathBuf::from("/cwd/adclicker.db"),
            "пустой env — дефолтное имя adclicker.db"
        );

        let default = resolve_db_path_in(None, None, None, Some(Path::new("/cwd")));
        assert_eq!(
            default,
            PathBuf::from("/cwd/adclicker.db"),
            "дефолт — adclicker.db в каталоге запуска"
        );
    }

    #[test]
    fn resolve_db_path_expands_home_and_normalizes_relative_path() {
        let home = resolve_db_path_in(
            Some("~/data/adclicker.db"),
            None,
            Some("/home/runner"),
            Some(Path::new("/cwd")),
        );
        assert_eq!(
            home,
            PathBuf::from("/home/runner/data/adclicker.db"),
            "тильда разворачивается в HOME"
        );

        let tilde_only = resolve_db_path_in(Some("~"), None, Some("/home/runner"), None);
        assert_eq!(tilde_only, PathBuf::from("/home/runner"));

        let dotted = resolve_db_path_in(
            Some("./sub/../adclicker.db"),
            None,
            None,
            Some(Path::new("/cwd")),
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
