//! Перенос ресурсов бандла в каталог, из которого их читает демон.
//!
//! Legacy открывает `config.json`, `queries.txt` и остальное из cwd, а у
//! запущенного из Finder приложения cwd — `/`: рабочий каталог уходит в
//! каталог данных приложения (см. `daemon::seed_target`), но самим файлам
//! откуда-то взяться. Они пакуются в .app через `bundle.resources`
//! (tauri.conf.json) и при старте переносятся туда — иначе первый же запуск
//! упирается в «не найден config.json».
//!
//! Правило — только недостающие: правки пользователя в каталоге данных
//! сильнее того, что лежит в бандле. Файл, которого в бандле нет (dev-запуск
//! из исходников, где паковать нечего), пропускается молча.
//!
//! Путь в [`BUNDLED_FILES`] — ровно строка из `tauri.conf.json > bundle >
//! resources`: Tauri превращает `../../` в `_up_/_up_` внутри каталога
//! ресурсов, и [`seed`] проходит тем же синтаксисом через
//! `PathResolver::resolve`. Тест `bundle_resources_match_the_declared_list`
//! не даёт двум спискам разойтись.

use std::fs;
use std::path::{Path, PathBuf};

use tauri::path::{BaseDirectory, PathResolver};
use tauri::Runtime;

/// Файлы, которые пакуются в бандл и переносятся в рабочий каталог демона.
///
/// `queries.txt` и `proxies.txt` — локальные файлы пользователя (в git не
/// входят): на машине, где их нет, сборка бандла должна упасть явно, а не
/// выдать приложение, у которого не соберётся пул прокси.
pub const BUNDLED_FILES: &[&str] = &[
    "../../config.json",
    "../../queries.txt",
    "../../proxies.txt",
    "../../user_agents.txt",
    "../../domains.txt",
    "../../domain_mapping.json",
    "../../cookies.txt",
];

/// Пары «источник в бандле → файл в каталоге данных» для [`copy_missing`].
///
/// Пути резолвятся, но не проверяются на существование: отсутствие — это
/// dev-запуск, где бандла нет, и это не ошибка.
pub fn bundle_items<R: Runtime>(
    resolver: &PathResolver<R>,
    target: &Path,
) -> Vec<(PathBuf, PathBuf)> {
    BUNDLED_FILES
        .iter()
        .filter_map(|resource| {
            let source = resolver.resolve(*resource, BaseDirectory::Resource).ok()?;
            let name = Path::new(resource).file_name()?;
            Some((source, target.join(name)))
        })
        .collect()
}

/// Копирует недостающие файлы. Возвращает имена скопированных.
///
/// Существующее назначение не перезаписывается — правки пользователя
/// сохраняются между обновлениями приложения. Источник, которого нет,
/// пропускается: так выглядит запуск без бандла ресурсов.
pub fn copy_missing(items: &[(PathBuf, PathBuf)]) -> Result<Vec<String>, String> {
    let mut copied = Vec::new();
    for (source, target) in items {
        if target.exists() || !source.is_file() {
            continue;
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("не удалось создать {parent:?}: {error}"))?;
        }
        fs::copy(source, target)
            .map_err(|error| format!("не удалось скопировать {source:?} → {target:?}: {error}"))?;
        if let Some(name) = target.file_name() {
            copied.push(name.to_string_lossy().into_owned());
        }
    }
    Ok(copied)
}

/// Перенос ресурсов бандла в `target` (рабочий каталог демона).
pub fn seed<R: Runtime>(resolver: &PathResolver<R>, target: &Path) -> Result<Vec<String>, String> {
    copy_missing(&bundle_items(resolver, target))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn temp_dir(prefix: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("{prefix}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("каталог создаётся");
        dir
    }

    #[test]
    fn copy_missing_copies_only_absent_files_and_keeps_edits() {
        let root = temp_dir("adclicker-seed");
        let source = root.join("bundle");
        let target = root.join("data");
        fs::create_dir_all(&source).expect("bundle создаётся");
        fs::write(source.join("config.json"), "{\"from\":\"bundle\"}").expect("файл пишется");
        fs::write(source.join("queries.txt"), "buy laptop").expect("файл пишется");

        let items = |target: &Path| {
            vec![
                (source.join("config.json"), target.join("config.json")),
                (source.join("queries.txt"), target.join("queries.txt")),
            ]
        };

        let copied = copy_missing(&items(&target)).expect("перенос проходит");
        assert_eq!(
            copied,
            vec!["config.json".to_string(), "queries.txt".to_string()]
        );
        assert_eq!(
            fs::read_to_string(target.join("queries.txt")).unwrap(),
            "buy laptop"
        );

        // Правки пользователя сильнее бандла: повторный перенос не трогает файл.
        fs::write(target.join("config.json"), "{\"from\":\"user\"}").expect("правка пишется");
        let copied = copy_missing(&items(&target)).expect("повторный перенос проходит");
        assert!(copied.is_empty(), "не копируется ничего: {copied:?}");
        assert_eq!(
            fs::read_to_string(target.join("config.json")).unwrap(),
            "{\"from\":\"user\"}",
            "содержимое пользователя сохранено"
        );

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn copy_missing_skips_absent_sources_and_creates_parents() {
        let root = temp_dir("adclicker-seed-partial");
        let source = root.join("bundle");
        let target = root.join("nested").join("data");
        fs::create_dir_all(&source).expect("bundle создаётся");
        fs::write(source.join("config.json"), "{}").expect("файл пишется");

        let copied = copy_missing(&[
            (source.join("config.json"), target.join("config.json")),
            (source.join("proxies.txt"), target.join("proxies.txt")),
        ])
        .expect("перенос проходит");

        assert_eq!(copied, vec!["config.json".to_string()]);
        assert!(
            !target.join("proxies.txt").exists(),
            "отсутствующий источник не создаёт пустышку"
        );
        assert!(
            target.join("config.json").is_file(),
            "каталоги создаются под копируемый файл"
        );

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn bundle_resources_match_the_declared_list() {
        // Список в коде и список в tauri.conf.json — один контракт в двух
        // местах: разойдутся — часть файлов перестанет попадать в .app, и
        // первая же ошибка всплывёт только на машине пользователя.
        let raw = fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tauri.conf.json"))
            .expect("tauri.conf.json читается");
        let config: serde_json::Value = serde_json::from_str(&raw).expect("конфиг валиден");

        let declared = config["bundle"]["resources"]
            .as_array()
            .unwrap_or_else(|| panic!("bundle.resources должен быть списком"))
            .iter()
            .map(|item| {
                item.as_str()
                    .unwrap_or_else(|| panic!("ресурс должен быть строкой: {item}"))
                    .to_string()
            })
            .collect::<Vec<_>>();

        assert_eq!(
            declared, BUNDLED_FILES,
            "tauri.conf.json > bundle.resources и resources::BUNDLED_FILES должны совпадать"
        );
    }
}
