//! Рестарт Python-демона из-под Tauri-хоста.
//!
//! Демон — отдельный процесс, который держит пул браузеров. Если он упадёт
//! сам (исключение в HTTP-обработчике, OOM, `kill` от соседнего скрипта),
//! воркеры продолжат работать без надзора: их никто не перезапустит и не
//! погасит. Поэтому жизненным циклом демона владеет хост приложения, а не
//! он сам: хост его порождает, следит и поднимает заново.
//!
//! Четыре решения, которые стоит знать:
//!
//! 1. **SIGTERM, а не `Child::kill()`.** На unix `kill()` — это SIGKILL.
//!    Демон, получивший его, не успевает погасить воркеров, а воркеры живут
//!    в собственных сессиях (`start_new_session`) и остаются сиротами с
//!    открытыми Chrome. Сначала SIGTERM, выдержка, и только потом SIGKILL.
//! 2. **Откат счётчика после здорового прогона.** Демон, проработавший
//!    полминуты и упавший один раз, — не то же самое, что демон, падающий
//!    каждые полсекунды. Без отката потолок съедался бы за сутки работы,
//!    и «поднять вручную» превращалось бы в вечную блокировку.
//! 3. **`try_wait()` под блокировкой, а не `wait()`.** `wait()` блокирует
//!    вызывающего, и `stop()` не смог бы забрать процесс, пока монитор сидит
//!    в `wait()`. Короткая блокировка на опросе и захват процесса снаружи
//!    не мешают друг другу: кто первый забрал `Child`, тот и владеет.
//! 4. **Процесс владеется через `Option<Child>` в общей структуре.** Монитор
//!    и `stop()` конкурируют за один объект, и мьютекс разрешает это
//!    детерминированно: либо `stop()` забрал и гасит сам, либо монитор увидел
//!    флаг остановки ещё до регистрации и убил ребёнка сам.
//!
//! Сборка демона как Tauri sidecar (`bundle.externalBin` + PyInstaller)
//! отложена в фазу 11; здесь работает dev-путь — команда из окружения.

use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// Python, которым стартует демон. Переопределяется `ADCLICKER_DAEMON_PYTHON`.
pub const DEFAULT_PYTHON: &str = "python3";

/// Аргументы по умолчанию — модуль control plane из корня репозитория.
pub const DEFAULT_ARGS: &[&str] = &["-m", "engine.control_plane.daemon"];

/// Файл, по которому ищется корень проекта: демону нужен каталог, в котором
/// лежат `config.json` и `queries.txt`.
const PROJECT_MARKER: &str = "config.json";

/// Сколько уровней вверх разрешается подниматься при поиске корня проекта.
const MAX_ROOT_SEARCH_UP: usize = 8;

/// Первый шаг экспоненты перед перезапуском.
pub const RESTART_BACKOFF_BASE: Duration = Duration::from_secs(1);
/// Потолок шага: ждать дольше бессмысленно, чем поднять сразу.
pub const RESTART_BACKOFF_MAX: Duration = Duration::from_secs(30);
/// Потолок падений подряд, после которого перезапуски прекращаются.
pub const MAX_CONSECUTIVE_FAILURES: u32 = 5;
/// Прогон дольше этого считается здоровым и обнуляет счётчик падений.
pub const HEALTHY_RUN_AFTER: Duration = Duration::from_secs(30);
/// Выдержка после SIGTERM перед SIGKILL.
///
/// Больше grace самого демона (10 с) и на то есть счёт: при остановке демон
/// сначала ждёт поток супервизора (до 10 с), потом рассылает SIGTERM пулу и
/// держит общий deadline (ещё 10 с), потом гасит HTTP. Дать ровно 10 с
/// значило бы SIGKILL'ить демон посреди этой последовательности, а воркеры
/// живут в собственных сессиях и пережили бы его как сироты с открытыми
/// Chrome. 45 с — это 10 + 10 с запасом на SIGKILL упёршихся воркеров.
pub const STOP_GRACE: Duration = Duration::from_secs(45);
/// Как часто монитор спрашивает у процесса, жив ли он.
pub const POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Токен control API. Одна константа на весь крейт: control.rs читает ровно
/// её, а два независимых объявления разошлись бы при первом же переименовании.
pub use crate::control::TOKEN_ENV;

#[derive(Clone, Debug)]
pub struct SupervisorOptions {
    pub poll_interval: Duration,
    pub restart_backoff_base: Duration,
    pub restart_backoff_max: Duration,
    pub max_consecutive_failures: u32,
    pub healthy_run_after: Duration,
    pub stop_grace: Duration,
}

impl Default for SupervisorOptions {
    fn default() -> Self {
        Self {
            poll_interval: POLL_INTERVAL,
            restart_backoff_base: RESTART_BACKOFF_BASE,
            restart_backoff_max: RESTART_BACKOFF_MAX,
            max_consecutive_failures: MAX_CONSECUTIVE_FAILURES,
            healthy_run_after: HEALTHY_RUN_AFTER,
            stop_grace: STOP_GRACE,
        }
    }
}

/// Что делать после неожиданного выхода демона.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NextStep {
    /// Поднять заново через указанную выдержку.
    Restart { delay: Duration },
    /// Потолок падений исчерпан — перезапуски прекращаются.
    GiveUp,
}

/// Решение после выхода процесса и обновлённый счётчик падений.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExitDecision {
    pub consecutive_failures: u32,
    pub next: NextStep,
}

/// Экспоненциальный шаг перед перезапуском.
///
/// Умножение, а не степень: `2u32.pow(n)` переполняется, а потолок обязан
/// давать выдержку, а не панику в рантайме.
pub fn backoff_delay(attempt: u32, base: Duration, max: Duration) -> Duration {
    if attempt <= 1 {
        return base.min(max);
    }
    let mut delay = base;
    for _ in 1..attempt {
        if delay >= max {
            return max;
        }
        delay = delay.saturating_mul(2);
    }
    delay.min(max)
}

/// Решение после выхода демона: перезапуск с выдержкой или отказ.
///
/// Семантика совпадает с python-супервизором: ``max_consecutive_failures``
/// падений подряд означают «экземпляр неисправен», а здоровый прогон дольше
/// ``healthy_run_after`` обнуляет счётчик.
pub fn decide_after_exit(
    uptime: Duration,
    consecutive_failures: u32,
    options: &SupervisorOptions,
) -> ExitDecision {
    let mut failures = if uptime >= options.healthy_run_after {
        0
    } else {
        consecutive_failures
    };
    failures = failures.saturating_add(1);

    if failures > options.max_consecutive_failures {
        ExitDecision {
            consecutive_failures: failures,
            next: NextStep::GiveUp,
        }
    } else {
        ExitDecision {
            consecutive_failures: failures,
            next: NextStep::Restart {
                delay: backoff_delay(
                    failures,
                    options.restart_backoff_base,
                    options.restart_backoff_max,
                ),
            },
        }
    }
}

/// Ищет каталог с `config.json`, поднимаясь вверх от `start`.
///
/// Рабочий каталог приложения — `ui/src-tauri` при dev-запуске, а демону
/// нужен корень репозитория. Отсутствие маркера — не ошибка: возвращается
/// `None`, и вызывающий получает понятное сообщение вместо запуска в
/// случайной точке файловой системы.
pub fn find_project_root_from(start: &Path) -> Option<PathBuf> {
    find_project_root_within(start, MAX_ROOT_SEARCH_UP)
}

/// То же, но с явным лимитом подъёма. Лимит отделён от основной функции,
/// чтобы тест мог гарантированно проверить ветку «маркера нет»: без него
/// поиск всё равно добрался бы до настоящего корня репозитория и увидел бы
/// чужой ``config.json``.
pub fn find_project_root_within(start: &Path, max_up: usize) -> Option<PathBuf> {
    let mut current = start.to_path_buf();
    for _ in 0..=max_up {
        if current.join(PROJECT_MARKER).is_file() {
            return Some(current);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// Токен control API из окружения.
///
/// Обязателен, а не генерируется: control.rs трактует отсутствие переменной
/// как явную ошибку и сознательно держит токен вне frontend-кода, а
/// параллельный источник значений дал бы UI токен, с которым оно не умеет
/// авторизоваться. Перевод строки отклоняется — он сломал бы HTTP-заголовок.
fn token_from_vars(vars: &HashMap<String, String>) -> Result<String, String> {
    let token = vars
        .get(TOKEN_ENV)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            format!(
                "{TOKEN_ENV} не задана: демон без токена не стартует, \
                 а UI не сможет авторизоваться. Задайте её при запуске."
            )
        })?
        .to_string();
    if token.contains('\r') || token.contains('\n') {
        return Err(format!("{TOKEN_ENV} содержит перевод строки"));
    }
    Ok(token)
}

/// Как запускать демон: программа, аргументы, каталог, переменные.
#[derive(Clone, Debug)]
pub struct DaemonSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: Vec<(String, String)>,
}

impl DaemonSpec {
    /// Спецификация из окружения процесса, с дефолтами для dev-запуска.
    pub fn from_env() -> Result<Self, String> {
        let vars: HashMap<String, String> = env::vars().collect();
        let cwd = env::current_dir().map_err(|error| format!("не удалось определить cwd: {error}"))?;
        Self::resolve(&vars, cwd)
    }

    /// Сборка спецификации из произвольного набора переменных.
    ///
    /// Вынесена отдельно от ``from_env`` не ради красоты: чтение глобального
    /// окружения в тестах гонялось бы параллельно с чужими тестами и делало
    /// бы результат зависимым от порядка выполнения.
    pub fn resolve(vars: &HashMap<String, String>, start_dir: PathBuf) -> Result<Self, String> {
        let program = vars
            .get("ADCLICKER_DAEMON_PYTHON")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty())
            .unwrap_or(DEFAULT_PYTHON)
            .to_string();

        let args = match vars.get("ADCLICKER_DAEMON_ARGS") {
            Some(raw) if !raw.trim().is_empty() => raw
                .split_whitespace()
                .map(str::to_string)
                .collect::<Vec<_>>(),
            _ => DEFAULT_ARGS.iter().map(|item| (*item).to_string()).collect(),
        };

        let cwd = match vars.get("ADCLICKER_DAEMON_CWD") {
            Some(raw) if !raw.trim().is_empty() => PathBuf::from(raw),
            _ => find_project_root_from(&start_dir)
                .ok_or_else(|| format!("не найден {PROJECT_MARKER} выше {start_dir:?}"))?,
        };

        // Ребёнку нужен полный набор переменных родителя (PYTHONPATH, PATH
        // из nix-окружения), поэтому копируется всё, что есть, и поверх
        // ложится только токен.
        let mut env_pairs: Vec<(String, String)> = vars
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        let token = token_from_vars(vars)?;
        match env_pairs.iter_mut().find(|(key, _)| key == TOKEN_ENV) {
            Some((_, value)) => *value = token,
            None => env_pairs.push((TOKEN_ENV.to_string(), token)),
        }

        Ok(Self {
            program,
            args,
            cwd,
            env: env_pairs,
        })
    }
}

/// Сводка состояния для UI и для тестов.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct DaemonStatus {
    pub running: bool,
    pub pid: Option<u32>,
    pub restarts: u32,
    pub consecutive_failures: u32,
    pub gave_up: bool,
    pub last_error: Option<String>,
}

#[derive(Debug, Default)]
struct Inner {
    child: Option<Child>,
    running: bool,
    /// Есть ли живой монитор. Нужен, чтобы ``start()`` во время backoff-сна
    /// старого монитора не породил второй процесс и второй цикл надзора.
    monitor_alive: bool,
    pid: Option<u32>,
    restarts: u32,
    consecutive_failures: u32,
    gave_up: bool,
    last_error: Option<String>,
}

/// Общее состояние монитора и управляющих вызовов.
///
/// Выделено из ``DaemonSupervisor``: монитору нужен ``Arc`` с состоянием, а
/// не ``Arc`` всего объекта, иначе ``start`` пришлось бы принимать
/// ``self: &Arc<Self>``, чего stable Rust не позволяет.
#[derive(Debug)]
struct Shared {
    inner: Mutex<Inner>,
    options: SupervisorOptions,
    stop: AtomicBool,
}

/// Супервизор демона: порождает, следит, перезапускает, гасит.
pub struct DaemonSupervisor {
    shared: Arc<Shared>,
}

impl DaemonSupervisor {
    pub fn new(options: SupervisorOptions) -> Self {
        Self {
            shared: Arc::new(Shared {
                inner: Mutex::new(Inner::default()),
                options,
                stop: AtomicBool::new(false),
            }),
        }
    }

    pub fn status(&self) -> DaemonStatus {
        let inner = self.shared.inner.lock().expect("mutex poisoned");
        DaemonStatus {
            running: inner.running,
            pid: inner.pid,
            restarts: inner.restarts,
            consecutive_failures: inner.consecutive_failures,
            gave_up: inner.gave_up,
            last_error: inner.last_error.clone(),
        }
    }

    /// Порождает демона и запускает фоновый монитор. Повторный вызов — no-op.
    ///
    /// Первый процесс создаётся синхронно, а не внутри монитора. Иначе
    /// между вызовом и регистрацией в ``inner`` был бы зазор, в который
    /// второй ``start()`` или ``stop()`` увидели бы пустое состояние и
    /// породили бы двух демонов либо оставили бы сироту. Ошибка запуска
    /// возвращается вызывающему: повторять «нет python» пять раз с бэкоффом
    /// — не восстановление, а маскировка проблемы.
    pub fn start(&self, spec: DaemonSpec) -> Result<(), String> {
        let mut inner = self.shared.inner.lock().expect("mutex poisoned");
        // Уже работает — повторный start это no-op, а не ошибка: двойной
        // клик не должен ронять UI.
        if inner.running || inner.child.is_some() {
            return Ok(());
        }
        // monitor_alive обязателен: между падением демона и концом backoff-сна
        // монитор держит ``running=false, child=None``, и без этой проверки
        // второй start() спавнил бы второго демона, а проснувшийся первый
        // перезаписал бы inner.child, бросив первый Child без остановки.
        // Молчаливый no-op здесь недопустим: вызывающий поверил бы, что
        // демон поднят.
        if inner.monitor_alive {
            return Err(
                "монитор демона ещё не завершился — повторите запрос позже".to_string(),
            );
        }
        self.shared.stop.store(false, Ordering::SeqCst);
        inner.gave_up = false;
        inner.restarts = 0;
        inner.consecutive_failures = 0;
        inner.last_error = None;

        let child = match spawn_daemon(&spec) {
            Ok(child) => child,
            Err(error) => {
                // Причина обязана остаться в статусе: stderr сюда не попадает,
                // а UI показывает ровно last_error.
                inner.last_error = Some(error.clone());
                return Err(error);
            }
        };
        inner.pid = Some(child.id());
        inner.running = true;
        inner.child = Some(child);
        inner.monitor_alive = true;
        drop(inner);

        let shared = Arc::clone(&self.shared);
        let spawned = thread::Builder::new()
            .name("daemon-monitor".into())
            .spawn(move || monitor(shared, spec, Instant::now()));

        if let Err(error) = spawned {
            // Монитор не родился — снимаем флаг и гасим процесс сами, иначе
            // start() навсегда вернётся в «монитор жив», а демон повиснет.
            let orphan = {
                let mut inner = self.shared.inner.lock().expect("mutex poisoned");
                inner.monitor_alive = false;
                inner.running = false;
                inner.pid = None;
                inner.last_error = Some(format!("монитор демона не запустился: {error}"));
                inner.child.take()
            };
            if let Some(mut child) = orphan {
                terminate(&mut child, self.shared.options.stop_grace);
            }
            return Err(format!("монитор демона не запустился: {error}"));
        }
        Ok(())
    }

    /// Гасит демон: SIGTERM, выдержка, SIGKILL. Повторный вызов безопасен.
    pub fn stop(&self) {
        self.shared.stop.store(true, Ordering::SeqCst);

        let child = {
            let mut inner = self.shared.inner.lock().expect("mutex poisoned");
            inner.running = false;
            inner.pid = None;
            inner.child.take()
        };

        if let Some(mut child) = child {
            terminate(&mut child, self.shared.options.stop_grace);
        }

        // Ждём монитора: он мог забрать ребёнка первым и терминировать его
        // сам. Без этого stop() вернулся бы, пока демон ещё жив, а выход
        // приложения убил бы монитора вместе с процессом.
        let deadline = Instant::now() + self.shared.options.stop_grace;
        while Instant::now() < deadline {
            let monitor_alive = {
                let inner = self.shared.inner.lock().expect("mutex poisoned");
                inner.monitor_alive
            };
            if !monitor_alive {
                return;
            }
            thread::sleep(self.shared.options.poll_interval);
        }
    }
}

fn spawn_daemon(spec: &DaemonSpec) -> Result<Child, String> {
    Command::new(&spec.program)
        .args(&spec.args)
        .current_dir(&spec.cwd)
        .envs(spec.env.iter().cloned())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        // stderr наследуется намеренно: демон печатает туда только ошибки
        // конфигурации, и в dev-запуске их видно сразу.
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|error| format!("не удалось запустить {}: {error}", spec.program))
}

/// Снятие флага ``monitor_alive`` при выходе монитора (в любом исходе).
struct MonitorGuard(Arc<Shared>);

impl Drop for MonitorGuard {
    fn drop(&mut self) {
        let mut inner = self
            .0
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        inner.monitor_alive = false;
    }
}

/// Цикл монитора: дождаться выхода, решить «поднять или сдаться», поднять.
///
/// Процесс на входе уже зарегистрирован (это делает ``start`` или прошлая
/// итерация), поэтому здесь нет гонки между спавном и чтением ``inner``.
fn monitor(shared: Arc<Shared>, spec: DaemonSpec, mut started: Instant) {
    // RAII: флаг monitor_alive снимается на любом выходе, включая panic в
    // этом потоке, иначе start() навсегда сошёлся бы на «монитор жив».
    let _guard = MonitorGuard(Arc::clone(&shared));
    loop {
        let stopped = shared.stop.load(Ordering::SeqCst);
        let code = wait_for_exit(&shared);
        let uptime = started.elapsed();

        // Ребёнок берётся, а не выбрасывается: Drop у Child процесс не
        // убивает. Если остановка пришла одновременно с выходом, монитор
        // успевает забирать лок первым, stop() получает None — и тогда
        // терминировать обязан именно здесь, иначе демон остаётся сиротой.
        let orphan = {
            let mut inner = shared.inner.lock().expect("mutex poisoned");
            inner.running = false;
            inner.pid = None;
            inner.child.take()
        };
        if let Some(mut orphan) = orphan {
            terminate(&mut orphan, shared.options.stop_grace);
        }
        if stopped || shared.stop.load(Ordering::SeqCst) {
            return;
        }

        let detail = match code {
            Some(code) => format!("демон завершился с кодом {code}"),
            None => "демон завершён сигналом".to_string(),
        };
        let decision = note_exit(&shared, uptime, &detail);
        if !apply(&shared, decision) {
            return;
        }

        // Потребовался новый процесс. Здесь ошибки спавна — это и есть
        // падение, поэтому они проходят через тот же счётчик и тот же бэкофф,
        // что и ненулевой код выхода.
        loop {
            match spawn_daemon(&spec) {
                Ok(mut child) => {
                    let mut inner = shared.inner.lock().expect("mutex poisoned");
                    if shared.stop.load(Ordering::SeqCst) {
                        drop(inner);
                        terminate(&mut child, shared.options.stop_grace);
                        return;
                    }
                    inner.pid = Some(child.id());
                    inner.running = true;
                    inner.last_error = None;
                    inner.child = Some(child);
                    started = Instant::now();
                    break;
                }
                Err(error) => {
                    let decision = note_exit(&shared, Duration::ZERO, &error);
                    if !apply(&shared, decision) {
                        return;
                    }
                }
            }
        }
    }
}

/// Ждёт выхода процесса короткими шагами, чтобы оставаться управляемым.
///
/// ``wait()`` здесь недопустим: он заблокировал бы монитор, а ``stop()``
/// обязан забрать ``Child`` в любой момент.
fn wait_for_exit(shared: &Shared) -> Option<i32> {
    loop {
        if shared.stop.load(Ordering::SeqCst) {
            return None;
        }
        let polled = {
            let mut inner = shared.inner.lock().expect("mutex poisoned");
            match inner.child.as_mut() {
                Some(child) => match child.try_wait() {
                    Ok(Some(status)) => {
                        inner.child = None;
                        Some(status.code())
                    }
                    Ok(None) => None,
                    Err(_) => {
                        inner.child = None;
                        Some(None)
                    }
                },
                // stop() забрал процесс — владение перешло к нему.
                None => None,
            }
        };
        if let Some(code) = polled {
            return code;
        }
        thread::sleep(shared.options.poll_interval);
    }
}

/// Фиксирует падение и возвращает обновлённое решение.
fn note_exit(shared: &Shared, uptime: Duration, detail: &str) -> ExitDecision {
    let mut inner = shared.inner.lock().expect("mutex poisoned");
    inner.restarts = inner.restarts.saturating_add(1);
    inner.last_error = Some(detail.to_string());
    let decision = decide_after_exit(uptime, inner.consecutive_failures, &shared.options);
    inner.consecutive_failures = decision.consecutive_failures;
    decision
}

/// Применяет решение. ``false`` — монитор должен завершиться.
fn apply(shared: &Shared, decision: ExitDecision) -> bool {
    match decision.next {
        NextStep::GiveUp => {
            let mut inner = shared.inner.lock().expect("mutex poisoned");
            inner.gave_up = true;
            inner.running = false;
            inner.pid = None;
            false
        }
        NextStep::Restart { delay } => {
            // Сон кусками, а не одним sleep: остановка обязана прервать и
            // backoff, иначе stop() ждал бы до 30 секунд, а monitor_alive
            // блокировал бы следующий start().
            let deadline = Instant::now() + delay;
            loop {
                if shared.stop.load(Ordering::SeqCst) {
                    return false;
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                thread::sleep(remaining.min(shared.options.poll_interval));
            }
            !shared.stop.load(Ordering::SeqCst)
        }
    }
}

/// SIGTERM -> выдержка -> SIGKILL. Вызывается владельцем ``Child``.
///
/// На не-unix SIGTERM не существует, поэтому там путь сразу переходит к
/// выдержке и ``kill()``: целевая платформа проекта — macOS, где ветка
/// ``cfg(unix)`` работает.
fn terminate(child: &mut Child, grace: Duration) {
    #[cfg(unix)]
    {
        // SAFETY: корректный pid у живого процесса и сигнал SIGTERM.
        // Невалидный pid даёт -1 и errno, а не неопределённое поведение.
        unsafe {
            libc::kill(child.id() as i32, libc::SIGTERM);
        }
    }

    let deadline = Instant::now() + grace;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {}
            Err(_) => return,
        }
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }

    let _ = child.kill();
    let _ = child.wait();
}

// Нужен только тестам, которые проверяют, что stop() действительно убил
// процесс, а не просто забыл его в статусе.
#[cfg(all(unix, test))]
fn pid_alive(pid: u32) -> bool {
    // kill(pid, 0) не шлёт сигнала, только спрашивает, существует ли процесс.
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(all(not(unix), test))]
fn pid_alive(_pid: u32) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fast_options() -> SupervisorOptions {
        SupervisorOptions {
            poll_interval: Duration::from_millis(5),
            restart_backoff_base: Duration::from_millis(5),
            restart_backoff_max: Duration::from_millis(20),
            max_consecutive_failures: 3,
            healthy_run_after: Duration::from_secs(3600),
            stop_grace: Duration::from_millis(700),
        }
    }

    fn spec(command: &str) -> DaemonSpec {
        DaemonSpec {
            program: "sh".to_string(),
            args: vec!["-c".to_string(), command.to_string()],
            cwd: env::temp_dir(),
            env: vec![],
        }
    }

    fn wait_until(deadline: Duration, mut condition: impl FnMut() -> bool) -> bool {
        let started = Instant::now();
        while started.elapsed() < deadline {
            if condition() {
                return true;
            }
            thread::sleep(Duration::from_millis(5));
        }
        condition()
    }

    // --- чистая логика ----------------------------------------------------

    #[test]
    fn backoff_grows_and_caps() {
        let base = Duration::from_secs(1);
        let max = Duration::from_secs(30);

        assert_eq!(backoff_delay(1, base, max), Duration::from_secs(1));
        assert_eq!(backoff_delay(2, base, max), Duration::from_secs(2));
        assert_eq!(backoff_delay(3, base, max), Duration::from_secs(4));
        assert_eq!(backoff_delay(10_000, base, max), max);
    }

    #[test]
    fn backoff_does_not_overflow_on_huge_attempt() {
        assert_eq!(
            backoff_delay(u32::MAX, Duration::from_secs(2), Duration::from_secs(60)),
            Duration::from_secs(60)
        );
    }

    #[test]
    fn failure_limit_is_reached_only_after_the_allowed_number_of_crashes() {
        let options = fast_options();
        let mut consecutive = 0;
        let mut steps = Vec::new();
        for _ in 0..5 {
            let decision = decide_after_exit(Duration::from_millis(1), consecutive, &options);
            consecutive = decision.consecutive_failures;
            steps.push(decision.next);
        }

        assert!(matches!(steps[0], NextStep::Restart { .. }));
        assert!(matches!(steps[1], NextStep::Restart { .. }));
        assert!(matches!(steps[2], NextStep::Restart { .. }));
        assert_eq!(steps[3], NextStep::GiveUp);
        assert_eq!(steps[4], NextStep::GiveUp);
    }

    #[test]
    fn healthy_run_resets_the_failure_counter() {
        let options = SupervisorOptions {
            healthy_run_after: Duration::from_secs(10),
            ..fast_options()
        };

        let decision = decide_after_exit(Duration::from_secs(60), 99, &options);

        assert_eq!(decision.consecutive_failures, 1);
        assert!(matches!(decision.next, NextStep::Restart { .. }));
    }

    // --- спецификация запуска ---------------------------------------------

    #[test]
    fn spec_defaults_to_the_python_control_module() {
        let root = env::temp_dir().join(format!("adclicker-defaults-{}", std::process::id()));
        fs::create_dir_all(&root).expect("каталог создаётся");
        fs::write(root.join(PROJECT_MARKER), "{}").expect("маркер пишется");

        let mut vars = HashMap::new();
        vars.insert("PATH".to_string(), "/usr/bin".to_string());
        vars.insert(TOKEN_ENV.to_string(), "provided-token".to_string());
        let spec = DaemonSpec::resolve(&vars, root.clone()).expect("спецификация собирается");

        assert_eq!(spec.program, DEFAULT_PYTHON);
        assert_eq!(spec.args, vec!["-m", "engine.control_plane.daemon"]);
        assert_eq!(spec.cwd, root);
        assert!(
            spec.env
                .iter()
                .any(|(key, value)| key == TOKEN_ENV && value == "provided-token"),
            "токен обязан дойти до демона как есть"
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn spec_honours_overrides_and_existing_token() {
        let mut vars = HashMap::new();
        vars.insert("ADCLICKER_DAEMON_PYTHON".to_string(), "/nix/bin/python".to_string());
        vars.insert("ADCLICKER_DAEMON_ARGS".to_string(), "-m my.daemon --verbose".to_string());
        vars.insert("ADCLICKER_DAEMON_CWD".to_string(), "/srv/app".to_string());
        vars.insert(TOKEN_ENV.to_string(), "known-token".to_string());

        let spec = DaemonSpec::resolve(&vars, PathBuf::from("/irrelevant")).expect("собирается");

        assert_eq!(spec.program, "/nix/bin/python");
        assert_eq!(spec.args, vec!["-m", "my.daemon", "--verbose"]);
        assert_eq!(spec.cwd, PathBuf::from("/srv/app"));
        assert!(spec
            .env
            .iter()
            .any(|(key, value)| key == TOKEN_ENV && value == "known-token"));
    }

    #[test]
    fn project_search_stops_at_the_configured_limit() {
        let root = env::temp_dir().join(format!("adclicker-limit-{}", std::process::id()));
        let nested = root.join("ui");
        fs::create_dir_all(&nested).expect("каталоги создаются");
        fs::write(root.join(PROJECT_MARKER), "{}").expect("маркер пишется");

        assert_eq!(
            find_project_root_within(&nested, 0),
            None,
            "лимит 0 — подниматься некуда, маркера в nested нет"
        );
        assert_eq!(
            find_project_root_within(&nested, 1).as_deref(),
            Some(root.as_path())
        );

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn spec_without_project_marker_reports_a_readable_error() {
        let start = env::temp_dir().join(format!("adclicker-orphan-{}", std::process::id()));
        fs::create_dir_all(&start).expect("каталог создаётся");

        let result = find_project_root_within(&start, 0)
            .map(|_| ())
            .ok_or_else(|| format!("не найден {PROJECT_MARKER}"));
        assert!(result.unwrap_err().contains(PROJECT_MARKER));

        let _ = fs::remove_dir_all(&start);
    }

    #[test]
    fn project_root_is_found_by_config_marker() {
        let root = env::temp_dir().join(format!("adclicker-root-{}", std::process::id()));
        let nested = root.join("ui").join("src-tauri");
        fs::create_dir_all(&nested).expect("каталоги создаются");
        fs::write(root.join(PROJECT_MARKER), "{}").expect("маркер пишется");

        assert_eq!(
            find_project_root_from(&nested).as_deref(),
            Some(root.as_path())
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn token_is_required_and_not_invented() {
        // Никакой генерации: control.rs считает отсутствие переменной явной
        // ошибкой, и параллельный источник дал бы UI токен, которым оно не
        // умеет авторизовываться.
        let error =
            token_from_vars(&HashMap::new()).expect_err("без токена старт запрещён");
        assert!(error.contains(TOKEN_ENV), "причина должна назвать переменную: {error}");

        let blank =
            token_from_vars(&HashMap::from([(TOKEN_ENV.to_string(), "   ".to_string())]))
                .expect_err("пробелы — не токен");
        assert!(blank.contains(TOKEN_ENV), "причина должна назвать переменную: {blank}");
    }

    #[test]
    fn token_is_taken_verbatim_from_the_environment() {
        let vars = HashMap::from([(TOKEN_ENV.to_string(), "  known-token  ".to_string())]);

        assert_eq!(
            token_from_vars(&vars).expect("заданный токен принимается"),
            "known-token"
        );
    }

    #[test]
    fn token_with_a_newline_is_refused() {
        // Перевод строки в значении сломал бы HTTP-заголовок запроса.
        let vars = HashMap::from([(TOKEN_ENV.to_string(), "bad\ntoken".to_string())]);

        let error = token_from_vars(&vars).expect_err("токен с переводом строки должен быть отвергнут");
        assert!(error.contains("перевод строки"), "{error}");
    }

    // --- жизненный цикл ---------------------------------------------------

    #[test]
    fn supervisor_starts_and_stops_a_real_child() {
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.start(spec("sleep 60")).expect("старт");

        let pid = supervisor.status().pid;
        assert!(pid.is_some(), "пока процесс жив, статус обязан показывать pid");

        supervisor.stop();

        let status = supervisor.status();
        assert!(!status.running);
        assert!(status.pid.is_none());
        if let Some(pid) = pid {
            assert!(wait_until(Duration::from_secs(3), || !pid_alive(pid)));
        }
    }

    #[test]
    fn crash_automatically_restarts_the_daemon() {
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.start(spec("exit 1")).expect("старт");

        let restarted =
            wait_until(Duration::from_secs(10), || supervisor.status().restarts >= 2);
        assert!(restarted, "упавший демон обязан быть перезапущен");

        supervisor.stop();
    }

    #[test]
    fn persistent_failure_opens_the_circuit() {
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.start(spec("exit 1")).expect("старт");

        let gave_up = wait_until(Duration::from_secs(10), || supervisor.status().gave_up);
        assert!(gave_up, "потолок падений обязан остановить перезапуски");

        let status = supervisor.status();
        assert!(!status.running);
        assert!(
            status
                .last_error
                .as_deref()
                .unwrap_or_default()
                .contains("код"),
            "причина отказа должна быть видна в last_error: {:?}",
            status.last_error
        );
        assert!(status.consecutive_failures > fast_options().max_consecutive_failures);

        supervisor.stop();
    }

    #[test]
    fn stop_is_a_noop_when_nothing_is_running() {
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.stop();
        assert!(!supervisor.status().running);
    }

    #[test]
    fn second_start_is_refused_while_a_monitor_is_alive() {
        // Монитор жив, но процесс уже погиб и ждёт backoff: без проверки
        // monitor_alive второй start() спавнил бы второго демона.
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.start(spec("exit 1")).expect("старт");

        let refused_while_alive = wait_until(Duration::from_secs(5), || {
            // ловим момент, когда процесс уже мёртв, а монитор ещё жив
            let status = supervisor.status();
            status.restarts >= 1 && !status.running
        });
        assert!(refused_while_alive, "демон должен упасть и уйти в backoff");

        let before = supervisor.status().restarts;
        let second = supervisor.start(spec("sleep 60"));
        assert!(
            second.is_err(),
            "start во время backoff-сна старого монитора обязан отказаться явно, \
             а не вернуть Ok и не спавнить второго демона"
        );
        assert!(
            supervisor.status().restarts >= before,
            "повторный start не должен сбрасывать состояние живого монитора"
        );

        supervisor.stop();
        // После stop() монитор гасится, и только тогда start() снова доступен.
        assert!(
            wait_until(Duration::from_secs(5), || {
                supervisor.start(spec("sleep 60")).is_ok()
            }),
            "после завершения монитора повторный start должен пройти"
        );
        supervisor.stop();
    }

    #[test]
    fn start_twice_does_not_spawn_a_second_process() {
        let supervisor = DaemonSupervisor::new(fast_options());
        supervisor.start(spec("sleep 60")).expect("старт");

        let first = supervisor.status().pid;
        supervisor.start(spec("sleep 60")).expect("повторный старт");
        thread::sleep(Duration::from_millis(100));

        assert_eq!(supervisor.status().pid, first, "второй процесс не должен появиться");

        supervisor.stop();
    }
}
