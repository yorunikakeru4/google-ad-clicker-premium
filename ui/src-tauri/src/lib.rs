// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod daemon;

use tauri::Manager;

use daemon::{DaemonSpec, DaemonStatus, DaemonSupervisor, SupervisorOptions};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// Состояние демона для UI: жив ли, PID, число перезапусков, токен.
///
/// Токен отдаётся наружу намеренно: это единственная защита loopback-порта,
/// и фронтенд — единственная сторона, которая должна подставлять его в
/// заголовок запроса. За пределы приложения он не уходит.
#[tauri::command]
fn daemon_status(supervisor: tauri::State<'_, DaemonSupervisor>) -> DaemonStatus {
    supervisor.status()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let supervisor = DaemonSupervisor::new(SupervisorOptions::default());

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
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
        .invoke_handler(tauri::generate_handler![greet, daemon_status])
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
