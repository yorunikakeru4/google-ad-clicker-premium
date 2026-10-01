//! HTTP-прокси control plane: единственная точка, где UI ходит в демон.
//!
//! Фронтенд не умеет сам по хорошей причине: CSP и скоупы Tauri не дают
//! лезть в сеть напрямую, а токен демона не должен жить во frontend-коде.
//! Поэтому все запросы идут через эту команду.
//!
//! Контракт с демоном (`engine/control_plane/api.py`, `daemon.py`):
//!
//! - токен — та же переменная, что у демона: `ADCLICKER_CONTROL_TOKEN`;
//!   на старте приложение заполняет её само ([`ensure_control_token`]), если
//!   переменной нет снаружи, а её отсутствие в рантайме остаётся явной
//!   ошибкой, а не молчаливым запросом без заголовка;
//! - базовый URL — `ADCLICKER_API_URL`, дефолт `http://127.0.0.1:8787`
//!   (порт `--port` демона, хост — только loopback);
//! - транспорт — голый HTTP без TLS: демон слушает только loopback и только
//!   HTTP, так что «маленький ureq» с rustls'ом здесь лишняя зависимость.
//!
//! Ошибки делятся на два вида, и это важно для UI:
//!
//! - `Err(String)` — сети не было: соединение, таймаут, разбор ответа,
//!   отсутствие токена. UI показывает строку как есть и помечает демон
//!   недоступным;
//! - `Ok(ControlReply)` — демон ответил, даже 4xx/5xx. Тело несут сообщение
//!   демона (например «воркеры уже запущены»), его и читает пользователь.
//!
//! Токен не попадает ни в один `Err` и ни в один лог: он нужен только
//! в заголовке запроса.

use serde::Serialize;
use std::io::{ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

/// Имя переменной окружения с токеном. Не менять: это же имя читает демон
/// (`TOKEN_ENV_VAR` в `engine/control_plane/api.py`).
pub const TOKEN_ENV: &str = "ADCLICKER_CONTROL_TOKEN";

/// Базовый URL демона. URL-переменной у демона нет (порт задаётся флагом
/// `--port`, дефолт 8787), поэтому имя берётся для UI.
pub const BASE_URL_ENV: &str = "ADCLICKER_API_URL";

/// Дефолт совпадает с дефолтом `--port` в `daemon.py`.
pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8787";

/// Сколько байт энтропии в сгенерированном токене: 16 байт дают 32
/// hex-символа, как `openssl rand -hex 16` из README.
const TOKEN_BYTES: usize = 16;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const READ_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_RESPONSE_BYTES: usize = 10 * 1024 * 1024;

/// Ответ демона: любой HTTP-статус, включая 4xx/5xx — тело читает UI.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ControlReply {
    pub status: u16,
    pub body: String,
}

/// Разобранный базовый URL: только хост и порт, без схемы и путей.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub host: String,
    pub port: u16,
}

/// Токен control API на этот запуск: берёт заданную извне переменную,
/// иначе генерирует свою и записывает её в окружение процесса.
///
/// Зачем: запущенное из Finder (или из launchd без `EnvironmentVariables`)
/// приложение не получает окружения, и раньше это давало две ошибки разом —
/// `DaemonSpec::from_env` отказывался спавнить демон, а `control_request`
/// не мог авторизоваться у него. Генерация убирает обе: спавнимый демон и UI
/// видят одно и то же значение, потому что оно прокидывается через
/// окружение процесса — тот же канал, что и раньше.
///
/// Правила:
///
/// - заданная извне переменная всегда сильнее: в ней может быть токен уже
///   работающего демона (dev-shell, launchd plist);
/// - генерируется один раз на процесс, на диск не пишется, в логи и ответы
///   не попадает — токен живёт в памяти процесса и уходит ребёнку при спавне
///   демона;
/// - перевод строки в заданном значении — ошибка, а не тихая подмена:
///   иначе сломанный env выглядел бы как рабочий.
///
/// Вызывается в `run()` до старта потоков Tauri: `std::env::set_var`
/// небезопасен при параллельном чтении окружения, а [`control_request`]
/// читает переменную на каждый вызов — после вызова здесь она уже готова.
pub fn ensure_control_token() -> Result<String, String> {
    let provided = std::env::var(TOKEN_ENV).ok();
    let token = token_or_generate(provided.as_deref())?;
    std::env::set_var(TOKEN_ENV, &token);
    Ok(token)
}

/// Ядро [`ensure_control_token`]: заданный токен побеждает, пустой или
/// отсутствующий — генерация. Отделено от записи в окружение, чтобы тесты
/// не трогали глобальное состояние процесса (иначе они гонялись бы
/// параллельно с чужими чтениями env).
fn token_or_generate(provided: Option<&str>) -> Result<String, String> {
    let provided = provided.map(str::trim).unwrap_or_default();
    if provided.is_empty() {
        return generate_token();
    }
    if provided.contains('\r') || provided.contains('\n') {
        return Err(format!("{TOKEN_ENV} содержит перевод строки"));
    }
    Ok(provided.to_owned())
}

/// 128 бит из `/dev/urandom`, закодированные в hex.
///
/// Фолбэка на время/пида сознательно нет: он дал бы предсказуемый токен.
/// `/dev/urandom` есть на обеих целевых платформах проекта (macOS, Linux),
/// и его отсутствие — понятная ошибка, а не повод ставить слабое значение.
fn generate_token() -> Result<String, String> {
    let mut bytes = [0u8; TOKEN_BYTES];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut source| source.read_exact(&mut bytes))
        .map_err(|error| format!("не удалось прочитать /dev/urandom для токена: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Команда Tauri. Читает env на каждый вызов: dev-смена переменных
/// подхватывается без перезапуска приложения.
#[tauri::command]
pub fn control_request(
    path: String,
    method: String,
    body: Option<String>,
) -> Result<ControlReply, String> {
    request(
        std::env::var(TOKEN_ENV).ok().as_deref(),
        std::env::var(BASE_URL_ENV).ok().as_deref(),
        &method,
        &path,
        body.as_deref(),
    )
}

/// Ядро запроса с явными параметрами — тестируется без обращения к env.
pub fn request(
    token: Option<&str>,
    base_url: Option<&str>,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<ControlReply, String> {
    let token = token
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            format!(
                "{TOKEN_ENV} не задана: UI не может авторизоваться у демона. \
             Задайте её тем же значением, что и при запуске демона."
            )
        })?;
    // Перевод строки в токене сломал бы заголовок и позволил бы подсунуть
    // в запрос свои заголовки — отклоняем до построения запроса.
    if token.contains('\r') || token.contains('\n') {
        return Err(format!(
            "{TOKEN_ENV} содержит перевод строки — такой токен нельзя положить в HTTP-заголовок"
        ));
    }

    let endpoint = parse_endpoint(base_url.unwrap_or(DEFAULT_BASE_URL))?;

    let method = method.to_ascii_uppercase();
    if method != "GET" && method != "POST" {
        return Err(format!(
            "метод {method} не поддерживается: демон принимает только GET и POST"
        ));
    }
    if !allowed_path(path) {
        return Err(format!(
            "путь {path} не входит в список разрешённых (/health, /state, /control/*)"
        ));
    }

    send(
        &endpoint,
        &method,
        path,
        body,
        token,
        CONNECT_TIMEOUT,
        READ_TIMEOUT,
    )
}

/// Разбор базового URL. Только `http://` — демон не умеет TLS, и молча
/// стучаться по https на порт демона бессмысленно.
pub fn parse_endpoint(base_url: &str) -> Result<Endpoint, String> {
    let rest = base_url.strip_prefix("http://").ok_or_else(|| {
        format!(
            "{BASE_URL_ENV} должен начинаться с http:// (получено «{base_url}»): \
                 демон слушает только HTTP на loopback"
        )
    })?;
    // Единственное, что разрешаем после хоста — завершающий слэш.
    let rest = rest.strip_suffix('/').unwrap_or(rest);
    if rest.is_empty() {
        return Err(format!("{BASE_URL_ENV} пуст: ожидался http://host[:port]"));
    }
    if rest.contains('/') {
        return Err(format!(
            "путь в {BASE_URL_ENV} не поддерживается: «{base_url}» — укажите только http://host[:port]"
        ));
    }

    let (host, port_str) = match rest.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (rest, None),
    };
    if host.is_empty() {
        return Err(format!("в {BASE_URL_ENV} не указан хост: «{base_url}»"));
    }
    let port = match port_str {
        Some(port) => port
            .parse::<u16>()
            .map_err(|_| format!("некорректный порт в {BASE_URL_ENV}: «{port}»"))?,
        None => 80,
    };
    Ok(Endpoint {
        host: host.to_owned(),
        port,
    })
}

/// Allowlist путей: даже с чужим телом прокси не уедет на произвольный URL.
///
/// Хвост `/control/` — сегменты из строчных латинских букв, разделённых
/// одинарным `/`: это открывает подпути вида `/control/proxies/import`
/// (контракт API прокси), но не открывает обход (`..`), верхний регистр,
/// точку, пробел, перевод строки и произвольные URL.
pub fn allowed_path(path: &str) -> bool {
    if !path.starts_with('/') || path.chars().any(|c| c.is_whitespace()) {
        return false;
    }
    if matches!(path, "/health" | "/state") {
        return true;
    }
    match path.strip_prefix("/control/") {
        Some(tail) => {
            !tail.is_empty()
                && tail.split('/').all(|segment| {
                    !segment.is_empty() && segment.chars().all(|c| c.is_ascii_lowercase())
                })
        }
        None => false,
    }
}

/// Сборка HTTP/1.1-запроса. `Connection: close` — читаем ответ до EOF и не
/// парсим keep-alive.
pub fn build_request(
    method: &str,
    endpoint: &Endpoint,
    path: &str,
    body: Option<&str>,
    token: &str,
) -> String {
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\n\
         Host: {}:{}\r\n\
         Connection: close\r\n\
         Accept: application/json\r\n\
         X-Auth-Token: {token}\r\n",
        endpoint.host, endpoint.port,
    );
    match body {
        Some(body) => {
            request.push_str("Content-Type: application/json\r\n");
            request.push_str(&format!("Content-Length: {}\r\n\r\n", body.len()));
            request.push_str(body);
        }
        None => request.push_str("\r\n"),
    }
    request
}

/// Разбор ответа: статус-код + тело по Content-Length (или до конца).
pub fn parse_response(raw: &[u8]) -> Result<(u16, String), String> {
    let header_end = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "нечитаемый ответ демона: нет конца заголовков".to_owned())?;

    let head = std::str::from_utf8(&raw[..header_end])
        .map_err(|_| "нечитаемый ответ демона: заголовки не в UTF-8".to_owned())?;

    let mut lines = head.split("\r\n");
    let status_line = lines
        .next()
        .ok_or_else(|| "нечитаемый ответ демона: пустой статус".to_owned())?;
    let mut parts = status_line.split_whitespace();
    let protocol = parts
        .next()
        .ok_or_else(|| format!("нечитаемый ответ демона: «{status_line}»"))?;
    if !protocol.starts_with("HTTP/") {
        return Err(format!(
            "нечитаемый ответ демона: ожидался {protocol}, а это не HTTP"
        ));
    }
    let code = parts
        .next()
        .ok_or_else(|| format!("нечитаемый ответ демона: нет кода в «{status_line}»"))?;
    let status: u16 = code
        .parse()
        .map_err(|_| format!("нечитаемый ответ демона: код «{code}» не число"))?;

    let mut content_length: Option<usize> = None;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = Some(value.trim().parse().map_err(|_| {
                    format!("нечитаемый ответ демона: Content-Length «{value}» не число")
                })?);
            }
        }
    }

    let body_raw = &raw[header_end + 4..];
    let body = match content_length {
        Some(length) => {
            if body_raw.len() < length {
                return Err(format!(
                    "неполный ответ демона: ждали {length} байт тела, получили {}",
                    body_raw.len()
                ));
            }
            &body_raw[..length]
        }
        None => body_raw,
    };
    Ok((status, String::from_utf8_lossy(body).into_owned()))
}

/// Отправка запроса и чтение ответа с таймаутами (таймауты — параметр,
/// чтобы тест проверял таймаут за миллисекунды, а не за секунды).
pub fn send(
    endpoint: &Endpoint,
    method: &str,
    path: &str,
    body: Option<&str>,
    token: &str,
    connect_timeout: Duration,
    read_timeout: Duration,
) -> Result<ControlReply, String> {
    let request = build_request(method, endpoint, path, body, token);

    let addresses = (endpoint.host.as_str(), endpoint.port)
        .to_socket_addrs()
        .map_err(|error| {
            format!(
                "не удалось разрешить адрес {}:{} — {error}",
                endpoint.host, endpoint.port
            )
        })?;

    let mut stream: Option<TcpStream> = None;
    let mut last_error = String::new();
    for address in addresses {
        match TcpStream::connect_timeout(&address, connect_timeout) {
            Ok(connected) => {
                stream = Some(connected);
                break;
            }
            Err(error) => last_error = error.to_string(),
        }
    }
    let mut stream = stream.ok_or_else(|| {
        format!(
            "нет соединения с демоном на {}:{} — {last_error}",
            endpoint.host, endpoint.port
        )
    })?;

    let io_error = |stage: &str, error: std::io::Error| match error.kind() {
        ErrorKind::TimedOut | ErrorKind::WouldBlock => format!(
            "демон не ответил за {} мс ({stage})",
            read_timeout.as_millis()
        ),
        _ => format!("сбой соединения с демоном ({stage}): {error}"),
    };

    stream
        .set_read_timeout(Some(read_timeout))
        .and_then(|()| stream.set_write_timeout(Some(read_timeout)))
        .map_err(|error| io_error("настройка таймаута", error))?;

    stream
        .write_all(request.as_bytes())
        .and_then(|()| stream.flush())
        .map_err(|error| io_error("отправка запроса", error))?;

    let raw = read_until_complete(&mut stream, read_timeout)?;
    let (status, body) = parse_response(&raw)?;
    Ok(ControlReply { status, body })
}

/// Читает тело до Content-Length, иначе — до EOF (`Connection: close`).
fn read_until_complete(stream: &mut TcpStream, read_timeout: Duration) -> Result<Vec<u8>, String> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 8192];

    loop {
        if let Some(expected) = declared_length(&buffer) {
            if body_start(&buffer).is_some_and(|start| buffer.len() >= start + expected) {
                return Ok(buffer);
            }
        }
        if buffer.len() > MAX_RESPONSE_BYTES {
            return Err(format!(
                "слишком большой ответ демона: больше {MAX_RESPONSE_BYTES} байт"
            ));
        }

        match stream.read(&mut chunk) {
            Ok(0) => {
                if buffer.is_empty() {
                    return Err("демон закрыл соединение, не ответив".to_owned());
                }
                return Ok(buffer);
            }
            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
            Err(error) if matches!(error.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock) => {
                // Часть ответа уже есть: отдаём как есть, parse_response
                // скажет, хватило ли тела по Content-Length.
                if !buffer.is_empty() {
                    return Ok(buffer);
                }
                return Err(format!(
                    "демон не ответил за {} мс",
                    read_timeout.as_millis()
                ));
            }
            Err(error) => return Err(format!("сбой чтения ответа демона: {error}")),
        }
    }
}

/// Content-Length из накопленных байтов, если заголовки уже прочитаны.
fn declared_length(buffer: &[u8]) -> Option<usize> {
    let header_end = buffer.windows(4).position(|window| window == b"\r\n\r\n")?;
    let head = std::str::from_utf8(&buffer[..header_end]).ok()?;
    head.split("\r\n").find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse::<usize>().ok())
            .flatten()
    })
}

/// Индекс начала тела, если заголовки уже прочитаны.
fn body_start(buffer: &[u8]) -> Option<usize> {
    buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|position| position + 4)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    const TOKEN: &str = "test-token-value";

    fn endpoint_of(listener: &TcpListener) -> Endpoint {
        let address = listener.local_addr().unwrap();
        Endpoint {
            host: address.ip().to_string(),
            port: address.port(),
        }
    }

    /// Разовый сервер: принимает один запрос, возвращает готовый ответ,
    /// отдаёт прочитанное запрос через канал.
    fn serve_once(response: String) -> (Endpoint, mpsc::Receiver<String>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = endpoint_of(&listener);
        let (sender, receiver) = mpsc::channel();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut chunk = [0u8; 1024];
            loop {
                let read = stream.read(&mut chunk).unwrap();
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..read]);
                if let (Some(header_end), Some(length)) =
                    (body_start(&request), declared_length(&request))
                {
                    if request.len() >= header_end + length {
                        break;
                    }
                }
                if body_start(&request).is_some() && declared_length(&request).is_none() {
                    // GET без Content-Length: заголовков достаточно
                    break;
                }
            }
            let _ = sender.send(String::from_utf8_lossy(&request).into_owned());
            stream.write_all(response.as_bytes()).unwrap();
            let _ = stream.flush();
        });
        (endpoint, receiver, handle)
    }

    fn response(status_line: &str, body: &str) -> String {
        format!(
            "{status_line}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
    }

    fn closed_endpoint() -> Endpoint {
        // Резервируем порт и сразу отпускаем: соединение будет отклонено.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = endpoint_of(&listener);
        drop(listener);
        endpoint
    }

    #[test]
    fn parse_endpoint_takes_host_and_port() {
        assert_eq!(
            parse_endpoint("http://127.0.0.1:8787").unwrap(),
            Endpoint {
                host: "127.0.0.1".into(),
                port: 8787
            }
        );
        // завершающий слэш допустим
        assert_eq!(parse_endpoint("http://127.0.0.1:8787/").unwrap().port, 8787);
        // без порта — 80
        assert_eq!(parse_endpoint("http://localhost").unwrap().port, 80);
    }

    #[test]
    fn parse_endpoint_rejects_non_http_and_paths() {
        let https = parse_endpoint("https://127.0.0.1:8787").unwrap_err();
        assert!(https.contains("http://"), "ошибка объясняет схему: {https}");

        let path = parse_endpoint("http://127.0.0.1:8787/api").unwrap_err();
        assert!(path.contains("путь"), "ошибка объясняет путь: {path}");

        assert!(parse_endpoint("http://:8787").is_err());
        assert!(parse_endpoint("http://127.0.0.1:99999").is_err());
        assert!(parse_endpoint("http://").is_err());
    }

    #[test]
    fn allowed_path_is_an_allowlist() {
        assert!(allowed_path("/health"));
        assert!(allowed_path("/state"));
        assert!(allowed_path("/control/start"));
        assert!(allowed_path("/control/stop"));
        assert!(allowed_path("/control/config"));

        assert!(!allowed_path("/"));
        assert!(!allowed_path("/health/extra"));
        assert!(!allowed_path("/control/../state"));
        assert!(!allowed_path("/control/Start"));
        assert!(!allowed_path("/control/"));
        assert!(!allowed_path("/evil"));
        assert!(!allowed_path("/health q"));
        assert!(!allowed_path("health"));
        assert!(!allowed_path("/health\r\nX-Injected: 1"));
    }

    #[test]
    fn allowlist_passes_proxies_endpoints_but_not_lookalikes() {
        // Контракт API прокси: /control/proxies и его подпути.
        assert!(allowed_path("/control/proxies"));
        assert!(allowed_path("/control/proxies/import"));
        assert!(allowed_path("/control/proxies/delete"));
        assert!(allowed_path("/control/proxies/check"));

        // Строки с обходом, регистром и запросом не проходят.
        assert!(!allowed_path("/control/proxies/"));
        assert!(!allowed_path("/control/proxies/Import"));
        assert!(!allowed_path("/control/proxies/../state"));
        assert!(!allowed_path("/control/proxies/import/../../state"));
        assert!(!allowed_path("/control/proxies?all=1"));
        assert!(!allowed_path("/control/proxies/delete\r\nX-Injected: 1"));
    }

    #[test]
    fn request_carries_proxies_post_with_body_through_allowlist() {
        let (endpoint, received, handle) = serve_once(response(
            "HTTP/1.1 200 OK",
            r#"{"added":1,"skipped":0,"problems":[]}"#,
        ));
        let base_url = format!("http://{}:{}", endpoint.host, endpoint.port);

        let reply = request(
            Some(TOKEN),
            Some(&base_url),
            "POST",
            "/control/proxies",
            Some(r#"{"lines":["http://a:8080"]}"#),
        )
        .unwrap();
        handle.join().unwrap();

        assert_eq!(reply.status, 200);
        let request = received.recv().unwrap();
        assert!(request.starts_with("POST /control/proxies HTTP/1.1"));
        assert!(request.ends_with(r#"{"lines":["http://a:8080"]}"#));
    }

    #[test]
    fn provided_token_wins_over_generation() {
        // Заданная извне переменная (dev-shell, launchd) всегда сильнее:
        // в ней может быть токен уже работающего демона.
        assert_eq!(
            token_or_generate(Some("  known-token  ")).expect("заданный токен принимается"),
            "known-token",
            "значение обрезается, но не подменяется"
        );
    }

    #[test]
    fn missing_or_blank_token_is_generated() {
        for provided in [None, Some(""), Some("   ")] {
            let token = token_or_generate(provided).expect("генерация не падает");

            assert_eq!(token.len(), TOKEN_BYTES * 2, "32 hex-символа: {token}");
            assert!(
                token
                    .chars()
                    .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)),
                "только строчные hex: {token}"
            );
        }

        assert_ne!(
            token_or_generate(None).expect("первый токен"),
            token_or_generate(None).expect("второй токен"),
            "два запуска не должны получить одно и то же значение"
        );
    }

    #[test]
    fn provided_token_with_newline_is_refused() {
        // Тихая генерация вместо ошибки скрыла бы сломанный env: приложение
        // сгенерировало бы свой токен, а демон продолжал бы жить со старым.
        let error =
            token_or_generate(Some("bad\ntoken")).expect_err("перевод строки должен быть отклонён");
        assert!(error.contains("перевод строки"), "{error}");
    }

    #[test]
    fn request_rejects_missing_or_blank_token() {
        let error = request(None, Some("http://127.0.0.1:1"), "GET", "/health", None).unwrap_err();
        assert!(
            error.contains(TOKEN_ENV),
            "ошибка называет переменную: {error}"
        );

        let error = request(
            Some("   "),
            Some("http://127.0.0.1:1"),
            "GET",
            "/health",
            None,
        )
        .unwrap_err();
        assert!(
            error.contains(TOKEN_ENV),
            "пустой токен тоже ошибка: {error}"
        );
    }

    #[test]
    fn request_rejects_token_with_newline() {
        let error = request(
            Some("abc\r\nX-Injected: 1"),
            Some("http://127.0.0.1:1"),
            "GET",
            "/health",
            None,
        )
        .unwrap_err();
        assert!(error.contains("перевод строки"), "{error}");
    }

    #[test]
    fn request_rejects_unknown_method_and_path() {
        let error = request(
            Some(TOKEN),
            Some("http://127.0.0.1:1"),
            "DELETE",
            "/health",
            None,
        )
        .unwrap_err();
        assert!(error.contains("GET"), "{error}");

        let error = request(
            Some(TOKEN),
            Some("http://127.0.0.1:1"),
            "GET",
            "/secrets",
            None,
        )
        .unwrap_err();
        assert!(error.contains("разрешённых"), "{error}");
    }

    #[test]
    fn build_request_carries_method_path_and_token() {
        let endpoint = Endpoint {
            host: "127.0.0.1".into(),
            port: 8787,
        };
        let request = build_request(
            "POST",
            &endpoint,
            "/control/start",
            Some(r#"{"workers":3}"#),
            TOKEN,
        );

        assert!(request.starts_with("POST /control/start HTTP/1.1\r\n"));
        assert!(request.contains("Host: 127.0.0.1:8787\r\n"));
        assert!(request.contains("Connection: close\r\n"));
        assert!(request.contains(&format!("X-Auth-Token: {TOKEN}\r\n")));
        assert!(request.contains("Content-Length: 13\r\n"));
        assert!(request.ends_with("\r\n\r\n{\"workers\":3}"));

        let get = build_request("GET", &endpoint, "/health", None, TOKEN);
        assert!(get.ends_with("X-Auth-Token: test-token-value\r\n\r\n"));
        assert!(!get.contains("Content-Length"));
    }

    #[test]
    fn parse_response_reads_status_and_body() {
        let raw = response("HTTP/1.1 200 OK", r#"{"state":"running"}"#).into_bytes();
        let (status, body) = parse_response(&raw).unwrap();
        assert_eq!(status, 200);
        assert_eq!(body, r#"{"state":"running"}"#);

        let raw = response("HTTP/1.1 409 Conflict", r#"{"error":{}}"#).into_bytes();
        let (status, _) = parse_response(&raw).unwrap();
        assert_eq!(status, 409);
    }

    #[test]
    fn parse_response_tolerates_body_without_content_length() {
        let raw = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"ok\":true}";
        let (status, body) = parse_response(raw).unwrap();
        assert_eq!(status, 200);
        assert_eq!(body, r#"{"ok":true}"#);
    }

    #[test]
    fn parse_response_rejects_garbage() {
        assert!(parse_response(b"not http at all").is_err());
        assert!(parse_response(b"SPDY/3 200 OK\r\n\r\nbody").is_err());
        assert!(parse_response(b"HTTP/1.1 abc OK\r\n\r\nbody").is_err());
        let truncated = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort";
        assert!(parse_response(truncated).is_err());
    }

    #[test]
    fn send_returns_reply_for_any_status() {
        let (endpoint, received, handle) = serve_once(response(
            "HTTP/1.1 409 Conflict",
            r#"{"error":{"code":"already_running","message":"воркеры уже запущены"}}"#,
        ));

        let reply = send(
            &endpoint,
            "POST",
            "/control/start",
            None,
            TOKEN,
            Duration::from_secs(2),
            Duration::from_secs(2),
        )
        .unwrap();
        handle.join().unwrap();

        // 4xx — не исключение: тело с сообщением демона уходит в UI
        assert_eq!(reply.status, 409);
        assert!(reply.body.contains("воркеры уже запущены"));

        let request = received.recv().unwrap();
        assert!(request.starts_with("POST /control/start HTTP/1.1"));
        assert!(request.contains(&format!("X-Auth-Token: {TOKEN}")));
    }

    #[test]
    fn send_gets_health_json() {
        let (endpoint, received, handle) = serve_once(response(
            "HTTP/1.1 200 OK",
            r#"{"status":"ok","version":1,"workers_total":2}"#,
        ));

        let reply = send(
            &endpoint,
            "GET",
            "/health",
            None,
            TOKEN,
            Duration::from_secs(2),
            Duration::from_secs(2),
        )
        .unwrap();
        handle.join().unwrap();

        assert_eq!(reply.status, 200);
        assert!(reply.body.contains("\"version\":1"));
        let request = received.recv().unwrap();
        assert!(request.starts_with("GET /health HTTP/1.1"));
        assert!(request.contains("Connection: close"));
    }

    #[test]
    fn connection_refused_is_readable_and_hides_token() {
        let endpoint = closed_endpoint();
        let error = send(
            &endpoint,
            "GET",
            "/health",
            None,
            TOKEN,
            Duration::from_millis(500),
            Duration::from_millis(500),
        )
        .unwrap_err();

        assert!(
            error.contains("нет соединения"),
            "ошибка объясняет проблему: {error}"
        );
        assert!(
            error.contains(&format!("{}:{}", endpoint.host, endpoint.port)),
            "ошибка называет адрес: {error}"
        );
        assert!(
            !error.contains(TOKEN),
            "токен не должен попадать в ошибки: {error}"
        );
    }

    #[test]
    fn silent_server_times_out_with_readable_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = endpoint_of(&listener);
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            // прочитать запрос и промолчать дольше таймаута клиента
            let mut sink = [0u8; 1024];
            let _ = stream.read(&mut sink);
            thread::sleep(Duration::from_millis(400));
        });

        let error = send(
            &endpoint,
            "GET",
            "/health",
            None,
            TOKEN,
            Duration::from_secs(2),
            Duration::from_millis(100),
        )
        .unwrap_err();
        handle.join().unwrap();

        assert!(error.contains("не ответил"), "{error}");
        assert!(!error.contains(TOKEN), "{error}");
    }

    #[test]
    fn default_base_url_matches_daemon_default_port() {
        let endpoint = parse_endpoint(DEFAULT_BASE_URL).unwrap();
        assert_eq!(endpoint.host, "127.0.0.1");
        assert_eq!(endpoint.port, 8787);
    }
}
