pub mod commands;
pub mod db;
pub mod metrics;

use commands::{
    active_workers, captcha_share, clicks_per_hour, count_logs, db_open, list_logs, list_logs_page,
    requests_last_hour, runs_summary, DbState,
};

// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(DbState::default())
        .invoke_handler(tauri::generate_handler![
            greet,
            db_open,
            list_logs,
            list_logs_page,
            count_logs,
            runs_summary,
            clicks_per_hour,
            requests_last_hour,
            captcha_share,
            active_workers,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod test_support;
