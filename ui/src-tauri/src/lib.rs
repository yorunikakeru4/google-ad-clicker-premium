pub mod commands;
pub mod control;
pub mod daemon;
pub mod db;
pub mod metrics;

use tauri::Manager;

use commands::{
    active_workers, captcha_share, clicks_per_hour, count_logs, db_open, list_diagnostics,
    list_logs, list_logs_page, list_profiles, list_proxies, requests_last_hour, runs_summary,
    DbState,
};
use daemon::{DaemonSpec, DaemonStatus, DaemonSupervisor, SupervisorOptions};

/// Состояние демона для UI: жив ли, PID, число перезапусков, последняя ошибка.
///
/// Токен сюда не попадает намеренно: control.rs держит его вне frontend-кода,
/// и публиковать его второй командой значило бы отменять это решение.
#[tauri::command]
fn daemon_status(supervisor: tauri::State<'_, DaemonSupervisor>) -> DaemonStatus {
    supervisor.status()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let supervisor = DaemonSupervisor::new(SupervisorOptions::default());

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(DbState::default())
        .manage(supervisor)
        .setup(|app| {
            // Ошибка подъёма демона не роняет приложение: UI обязан открыться,
            // чтобы показать причину, а не исчезнуть без объяснений.
            let supervisor = app.state::<DaemonSupervisor>();
            match DaemonSpec::from_env() {
                Ok(spec) => {
                    if let Err(error) = supervisor.start(spec) {
                        eprintln!("демон не поднялся: {error}");
                    }
                }
                Err(error) => eprintln!("демон не поднялся: {error}"),
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            control::control_request,
            db_open,
            list_logs,
            list_logs_page,
            count_logs,
            runs_summary,
            clicks_per_hour,
            requests_last_hour,
            captcha_share,
            active_workers,
            list_proxies,
            list_profiles,
            list_diagnostics,
            daemon_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while running tauri application");

    app.run(|app_handle, event| {
        // ExitRequested, а не Exit: окна уже закрыты, но процесс ещё жив, и
        // здесь можно дождаться SIGTERM → grace → SIGKILL. На Exit монитор
        // демона убил бы вместе с процессом, оставив браузеров сиротами.
        if let tauri::RunEvent::ExitRequested { .. } = event {
            let supervisor = app_handle.state::<DaemonSupervisor>();
            supervisor.stop();
        }
    });
}

#[cfg(test)]
mod test_support;
