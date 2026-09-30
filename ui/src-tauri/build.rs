//! Сборка Tauri-хоста: штатный `tauri_build::build()` плюс забота о sidecar'е.
//!
//! `bundle.externalBin` обрабатывается уже в build.rs: tauri-build ищет файл
//! `binaries/engine-<triple>` и валит ВСЮ cargo-сборку (check/test включая),
//! пока его нет. Настоящий бинарник появляется только после
//! `scripts/build-sidecar.sh`, поэтому для свежего клона и для прогона тестов
//! здесь создаётся пустая заглушка с `cargo:warning`.
//!
//! Заглушка ничего не подменяет: dev-путь демона идёт через
//! `ADCLICKER_DAEMON_PYTHON`/`python3` и от sidecar не зависит, а `tauri
//! build` всё равно обязан идти после scripts/build-sidecar.sh — иначе в
//! пакете окажется пустышка вместо движка (об этом и кричит warning).

use std::{env, fs, path::PathBuf};

fn main() {
    ensure_external_binaries();
    tauri_build::build()
}

/// Создаёт заглушки для `bundle.externalBin`, которых ещё нет в `binaries/`.
///
/// Имя файла повторяет формулу tauri-utils `external_binaries()`:
/// `{path}-{target_triple}{.exe на windows}`, triple берётся из env `TARGET`,
/// который cargo выставляет для build.rs.
fn ensure_external_binaries() {
    let Ok(target) = env::var("TARGET") else {
        return;
    };
    let extension = if target.contains("windows") {
        ".exe"
    } else {
        ""
    };

    // Дальше без конфига упадёт сам tauri_build со своей внятной ошибкой —
    // здесь нечего чинить и нечего скрывать.
    let Ok(raw) = fs::read_to_string("tauri.conf.json") else {
        return;
    };
    let Ok(config) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    let Some(externals) = config["bundle"]["externalBin"].as_array() else {
        return;
    };

    for external in externals {
        let Some(path) = external.as_str() else {
            continue;
        };
        let stub = PathBuf::from(format!("{path}-{target}{extension}"));
        if stub.exists() {
            continue;
        }
        if let Some(parent) = stub.parent() {
            let _ = fs::create_dir_all(parent);
        }
        match fs::write(&stub, b"") {
            Ok(()) => println!(
                "cargo:warning=sidecar {} не собран — положена пустая заглушка; \
                 запусти scripts/build-sidecar.sh перед tauri build",
                stub.display()
            ),
            Err(error) => println!(
                "cargo:warning=не удалось создать заглушку {}: {error}",
                stub.display()
            ),
        }
    }
}
