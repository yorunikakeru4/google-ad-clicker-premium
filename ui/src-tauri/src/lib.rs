pub mod commands;
pub mod control;
pub mod daemon;
pub mod db;
pub mod metrics;
pub mod resources;

use std::collections::HashMap;

use tauri::Manager;

use commands::{
    active_workers, captcha_share, captchas_per_hour, clicks_per_hour, count_logs, db_open,
    db_size, list_captcha_events, list_diagnostics, list_logs, list_logs_page, list_profiles,
    list_proxies, requests_last_hour, runs_summary, uptime_summary, DbState,
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
    // Токен control API нужен и UI, и спавнимому демону. Из Finder/launchd
    // окружения может не быть — тогда приложение генерирует его само, до
    // первого чтения в control.rs и до DaemonSpec::from_env. Ошибка здесь
    // не роняет запуск: UI обязан открыться и показать причину, а без
    // токена и спавн, и запросы упадут с теми же читаемыми сообщениями.
    if let Err(error) = control::ensure_control_token() {
        eprintln!("токен control API не подготовлен: {error}");
    }

    let supervisor = DaemonSupervisor::new(SupervisorOptions::default());

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(DbState::default())
        .manage(supervisor)
        .setup(|app| {
            // Порядок важен и ошибки обязаны дойти до UI (команда
            // daemon_status читает last_error, баннер показывает его в
            // тексте «нет связи»): stderr у приложения из Finder никто не
            // читает, и без этого «демон не виден» оставалось без причины.
            let vars: HashMap<String, String> = std::env::vars().collect();
            let start_dir =
                std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));

            // 1. Файлы бандла в каталог демона — без config.json рабочий
            //    каталог не соберётся вообще.
            if let Some(target) = daemon::seed_target(&vars, &start_dir) {
                match resources::seed(app.path(), &target) {
                    Ok(copied) if !copied.is_empty() => eprintln!(
                        "ресурсы бандла перенесены в {target:?}: {}",
                        copied.join(", ")
                    ),
                    Ok(_) => {}
                    Err(error) => eprintln!("ресурсы бандла не перенеслись: {error}"),
                }
            }

            // 2. Порт: чужой демон (401) или уже запущенный с нашим токеном —
            //    второй не спавним, иначе получим цикл падений на занятом
            //    порту плюс 401 на каждый запрос UI. Но и без демона не
            //    остаёмся: если тот экземпляр уйдёт (догасает после прошлого
            //    запуска, упал в терминале), recover_when_free поднимет наш,
            //    как только порт освободится (план, фаза 13, проблема 4).
            let supervisor = app.state::<DaemonSupervisor>();
            match control::probe_daemon() {
                control::DaemonProbe::AlreadyRunning => {
                    eprintln!(
                        "на порту уже работает демон с этим же токеном — второй не спавним, \
                         но следим за портом"
                    );
                    match DaemonSpec::from_env() {
                        Ok(spec) => supervisor.recover_when_free(spec),
                        Err(error) => eprintln!("восстановление демона не настроено: {error}"),
                    }
                }
                control::DaemonProbe::ForeignDaemon { detail } => {
                    eprintln!("{detail}");
                    supervisor.note_launch_error(&detail);
                    match DaemonSpec::from_env() {
                        Ok(spec) => supervisor.recover_when_free(spec),
                        Err(error) => eprintln!("восстановление демона не настроено: {error}"),
                    }
                }
                control::DaemonProbe::Free => match DaemonSpec::from_env() {
                    Ok(spec) => {
                        if let Err(error) = supervisor.start(spec) {
                            eprintln!("демон не поднялся: {error}");
                        }
                    }
                    Err(error) => {
                        eprintln!("демон не поднялся: {error}");
                        supervisor.note_launch_error(&error);
                    }
                },
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
            captchas_per_hour,
            requests_last_hour,
            captcha_share,
            active_workers,
            uptime_summary,
            list_proxies,
            list_profiles,
            list_diagnostics,
            list_captcha_events,
            db_size,
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
