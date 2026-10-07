// Управление демоном: построение запросов и правила блокировки кнопок.
//
// Семантика команд — план §2 «Модель управления», ошибки демона —
// engine/control_plane/supervisor.py: Start падает при непустом пуле
// (AlreadyRunningError), Pause — при пустом (NotRunningError), Resume — без
// флага паузы (NotPausedError), Restart — при пустом пуле. Disabled-правила
// ниже повторяют эти проверки, чтобы не слать заведомо проигрышный запрос.

import type { Health, StateSnapshot } from "./types";

/** Команды UI. Kill в API демона — POST /control/stop. */
export type ControlAction = "start" | "pause" | "resume" | "kill" | "restart";

export interface ControlRequest {
  method: "POST";
  path: string;
}

const CONTROL_PATHS: Record<ControlAction, string> = {
  start: "/control/start",
  pause: "/control/pause",
  resume: "/control/resume",
  kill: "/control/stop",
  restart: "/control/restart",
};

export function controlRequest(action: ControlAction): ControlRequest {
  return { method: "POST", path: CONTROL_PATHS[action] };
}

/** Чтение конфига: GET /control/config. Тела нет вовсе. */
export interface ConfigRequest {
  method: "GET";
  path: string;
  body?: undefined;
}

export function configRequest(): ConfigRequest {
  return { method: "GET", path: "/control/config" };
}

/** Действия API прокси (контракт /control/proxies* параллельной ветки). */
export type ProxiesAction =
  | { kind: "list" }
  | { kind: "add"; lines: string[] }
  | { kind: "import" }
  | { kind: "delete"; id: number }
  | { kind: "deleteMany"; ids: number[] }
  | { kind: "deleteAll" }
  | { kind: "check" }
  | { kind: "file" };

export interface ProxiesRequest {
  method: "GET" | "POST";
  path: string;
  /** JSON-тело POST; у GET его нет вовсе. */
  body?: string;
}

/**
 * Билдер запросов к API прокси.
 *
 * Тела собираются здесь, а не в вызывающем коде: контракт демона
 * (`{"lines": [...]}`, `{"id"}`, пустой объект) живёт в одном месте, и
 * тесты видят ровно то, что уйдёт в control_request.
 */
export function proxiesRequest(action: ProxiesAction): ProxiesRequest {
  switch (action.kind) {
    case "list":
      return { method: "GET", path: "/control/proxies" };
    case "add":
      return {
        method: "POST",
        path: "/control/proxies",
        body: JSON.stringify({ lines: action.lines }),
      };
    case "import":
      return { method: "POST", path: "/control/proxies/import", body: "{}" };
    case "delete":
      return {
        method: "POST",
        path: "/control/proxies/delete",
        body: JSON.stringify({ id: action.id }),
      };
    case "deleteMany":
      // Батч: демон отвечает best-effort {deleted, skipped, problems}, а не
      // одним кодом — занятый воркером прокси не должен ронять весь запрос.
      return {
        method: "POST",
        path: "/control/proxies/delete",
        body: JSON.stringify({ ids: action.ids }),
      };
    case "deleteAll":
      // Список id собирает демон, а не UI: тело запроса не растёт с
      // размером пула (у демона лимит 64 КБ на запрос).
      return { method: "POST", path: "/control/proxies/delete", body: '{"all":true}' };
    case "check":
      return { method: "POST", path: "/control/proxies/check", body: "{}" };
    case "file":
      // Путь к файлу демон резолвит сам: он относительный в конфиге, а
      // каталог данных UI неизвестен. Тела нет — брать нечего.
      return { method: "GET", path: "/control/proxies/file" };
  }
}

/** Действия API списков (контракт /control/queries*, /control/domains*). */
export type WordlistAction =
  | { kind: "list" }
  | { kind: "add"; lines: string[] }
  | { kind: "delete"; values: string[] }
  | { kind: "deleteAll" }
  | { kind: "file" };

export interface WordlistRequest {
  method: "GET" | "POST";
  path: string;
  /** JSON-тело POST; у GET его нет вовсе. */
  body?: string;
}

/**
 * Билдер запросов к списку.
 *
 * Оба списка устроены одинаково (план §5, фаза 13), поэтому у них один
 * билдер: различаются только базовый путь и ключ тела удаления — «queries»
 * у запросов, «domains» у доменов. Тела собираются здесь, как у прокси:
 * контракт демона живёт в одном месте и виден в тестах.
 */
export function wordlistRequest(
  base: string,
  key: string,
  action: WordlistAction,
): WordlistRequest {
  switch (action.kind) {
    case "list":
      return { method: "GET", path: base };
    case "add":
      return {
        method: "POST",
        path: base,
        body: JSON.stringify({ lines: action.lines }),
      };
    case "delete":
      return {
        method: "POST",
        path: `${base}/delete`,
        body: JSON.stringify({ [key]: action.values }),
      };
    case "deleteAll":
      // Список значений для «удалить всё» собирает демон, не UI.
      return { method: "POST", path: `${base}/delete`, body: '{"all":true}' };
    case "file":
      return { method: "GET", path: `${base}/file` };
  }
}

/** Запросы (Key Words): /control/queries*. */
export function queriesRequest(action: WordlistAction): WordlistRequest {
  return wordlistRequest("/control/queries", "queries", action);
}

/** Домены: /control/domains*. */
export function domainsRequest(action: WordlistAction): WordlistRequest {
  return wordlistRequest("/control/domains", "domains", action);
}

/** Действия API диагностики (контракт /control/diagnostics/collect). */export type DiagnosticsAction =
  | { kind: "collect"; browserId: string }
  | { kind: "collectAll" };

export interface DiagnosticsRequest {
  method: "POST";
  path: string;
  body: string;
}

/**
 * Билдер запросов сбора диагностики.
 *
 * Тот же принцип, что у [`proxiesRequest`]: контракт (`{"browser_id": ...}`
 * либо `{"all": true}`) живёт здесь, а не в вызывающем коде, — тесты видят
 * ровно то, что уйдёт в control_request.
 */
export function diagnosticsRequest(action: DiagnosticsAction): DiagnosticsRequest {
  switch (action.kind) {
    case "collect":
      return {
        method: "POST",
        path: "/control/diagnostics/collect",
        body: JSON.stringify({ browser_id: action.browserId }),
      };
    case "collectAll":
      return {
        method: "POST",
        path: "/control/diagnostics/collect",
        body: JSON.stringify({ all: true }),
      };
  }
}

/** Действия API очистки профилей (контракт /control/cleanup/*). */
export type CleanupAction = { kind: "run"; dryRun: boolean } | { kind: "status" };

export interface CleanupRequest {
  method: "GET" | "POST";
  path: string;
  /** JSON-тело POST; у GET его нет вовсе. */
  body?: string;
}

/**
 * Билдер запросов к API очистки.
 *
 * Тот же принцип, что у [`proxiesRequest`]: контракт тел (`{}` против
 * `{"dry_run": true}`) живёт здесь, а не в вызывающем коде, — тесты видят
 * ровно то, что уйдёт в control_request. Пути — из строчных сегментов,
 * поэтому проходят allowlist `control.rs::allowed_path`.
 */
export function cleanupRequest(action: CleanupAction): CleanupRequest {
  switch (action.kind) {
    case "run":
      return {
        method: "POST",
        path: "/control/cleanup/run",
        body: action.dryRun ? JSON.stringify({ dry_run: true }) : "{}",
      };
    case "status":
      return { method: "GET", path: "/control/cleanup/status" };
  }
}

/**
 * Разбор ответа демона вида `{"error": {"code", "message"}}`.
 *
 * Стороны ошибки независимы: `code` может прийти без `message` (тогда вызывающий
 * код подставляет свой текст по коду), `message` — без `code`. Не-JSON тело и
 * любой иной формат — `null`: вызывающий падает на [`apiErrorMessage`].
 */
export function errorPayload(
  body: string,
): { code: string | null; message: string | null } | null {
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed !== "object" || parsed === null) return null;
    const error = (parsed as { error?: unknown }).error;
    if (typeof error !== "object" || error === null) return null;
    const code = (error as { code?: unknown }).code;
    const message = (error as { message?: unknown }).message;
    return {
      code: typeof code === "string" ? code : null,
      message:
        typeof message === "string" && message.trim() !== "" ? message : null,
    };
  } catch {
    return null;
  }
}

/** Новый профиль для POST /control/profiles: только name обязателен. */
export interface NewProfile {
  name: string;
  key_ref?: string;
  proxy_id?: number;
  user_agent?: string;
  locale?: string;
  timezone?: string;
  fields?: string;
}

/** Контрактный статус профиля для POST /control/profiles/status. */
export type WritableProfileStatus = "free" | "blocked" | "error";

/** Действия API профилей (контракт /control/profiles* параллельной ветки). */
export type ProfilesAction =
  | { kind: "list" }
  | { kind: "add"; profiles: NewProfile[] }
  | { kind: "import"; lines: string[] }
  | { kind: "delete"; id: number }
  | { kind: "deleteMany"; ids: number[] }
  | { kind: "deleteAll" }
  | { kind: "importFile" }
  | { kind: "assign"; start_id: number; end_id: number }
  | { kind: "unassign" }
  | { kind: "status"; id: number; status: WritableProfileStatus };

export interface ProfilesRequest {
  method: "GET" | "POST";
  path: string;
  /** JSON-тело POST; у GET его нет вовсе. */
  body?: string;
}

/**
 * Билдер запросов к API профилей.
 *
 * Тот же принцип, что у [`proxiesRequest`]: контракт (`{"profiles": [...]}`,
 * `{"lines": [...]}`, `{"id"}` / `{"ids"}` / `{"all"}`, `{"file": true}`,
 * `{"start_id", "end_id"}`, `{"id", "status"}`) живёт здесь, а не в
 * вызывающем коде, — тесты видят ровно то, что уйдёт в control_request.
 */
export function profilesRequest(action: ProfilesAction): ProfilesRequest {
  switch (action.kind) {
    case "list":
      return { method: "GET", path: "/control/profiles" };
    case "add":
      return {
        method: "POST",
        path: "/control/profiles",
        body: JSON.stringify({ profiles: action.profiles }),
      };
    case "import":
      return {
        method: "POST",
        path: "/control/profiles/import",
        body: JSON.stringify({ lines: action.lines }),
      };
    case "delete":
      return {
        method: "POST",
        path: "/control/profiles/delete",
        body: JSON.stringify({ id: action.id }),
      };
    case "deleteMany":
      // Батч: демон отвечает best-effort {deleted, skipped, problems}, а не
      // одним кодом — занятый живым воркером профиль не должен ронять весь
      // запрос (парсер тела общий с прокси).
      return {
        method: "POST",
        path: "/control/profiles/delete",
        body: JSON.stringify({ ids: action.ids }),
      };
    case "deleteAll":
      // Список id собирает демон, а не UI: тело запроса не растёт с
      // размером пула (у демона лимит 64 КБ на запрос).
      return { method: "POST", path: "/control/profiles/delete", body: '{"all":true}' };
    case "importFile":
      // Путь к user_agents.txt демон резолвит сам из конфига (paths.user_agents):
      // у UI каталога данных демона нет. Флаг file отличает режим от импорта
      // строк — путь тот же, тело разное.
      return { method: "POST", path: "/control/profiles/import", body: '{"file":true}' };
    case "assign":
      return {
        method: "POST",
        path: "/control/profiles/assign",
        body: JSON.stringify({ start_id: action.start_id, end_id: action.end_id }),
      };
    case "unassign":
      return { method: "POST", path: "/control/profiles/unassign", body: "{}" };
    case "status":
      return {
        method: "POST",
        path: "/control/profiles/status",
        body: JSON.stringify({ id: action.id, status: action.status }),
      };
  }
}

/** Сводное состояние, из которого считаются disabled-правила. */
export interface StatusView {
  online: boolean;
  busy: boolean;
  /** RUN_STATE из БД демона: stopped | running | paused | stopping. */
  state: string | null;
  paused: boolean;
  /** Размер пула супервизора (/health.workers_total), а не строк в БД. */
  workersTotal: number;
  workersAlive: number;
}

export function toStatusView(input: {
  online: boolean;
  busy: boolean;
  health: Health | null;
  state: StateSnapshot | null;
}): StatusView {
  return {
    online: input.online,
    busy: input.busy,
    state: input.health?.state ?? input.state?.state ?? null,
    paused: input.health?.paused ?? input.state?.paused ?? false,
    workersTotal: input.health?.workers_total ?? 0,
    workersAlive: input.health?.workers_alive ?? 0,
  };
}

/**
 * Причина блокировки кнопки или null, если команда доступна.
 *
 * Порядок проверок значим: недоступный демон и команда в полёте важнее
 * состояния пула — иначе пользователь увидел бы «нет воркеров», хотя демон
 * просто не отвечает.
 */
export function disabledReason(
  action: ControlAction,
  view: StatusView,
): string | null {
  if (!view.online) return "демон недоступен";
  if (view.busy) return "идёт выполнение команды";
  if (view.state === "stopping") return "идёт остановка воркеров";

  const poolAlive = view.workersTotal > 0;

  switch (action) {
    case "start":
      // AlreadyRunningError: старт при непустом пуле — ошибка, а не no-op.
      return poolAlive ? "воркеры уже запущены" : null;
    case "pause":
      if (view.paused) return "уже на паузе";
      return poolAlive ? null : "нет запущенных воркеров";
    case "resume":
      return view.paused ? null : "пауза не установлена";
    case "kill":
      // Stop безопасен и при живом пуле, и при залипшем флаге running:
      // он снимает флаг и закрывает записи. Мёртв всё — только когда и
      // пул пуст, и состояние не говорит «работаем».
      if (poolAlive || view.state === "running" || view.state === "paused") {
        return null;
      }
      return "все воркеры уже остановлены";
    case "restart":
      // NotRunningError: перезапуск при пустом пуле бессмыслен.
      return poolAlive ? null : "нечего перезапускать: воркеры не запущены";
  }
}

/**
 * Читаемый текст ошибки из ответа демона.
 *
 * Демон всегда отдаёт JSON `{"error": {"code", "message"}}` — его message и
 * показываем. Не-JSON тело (прокси, брошенный соединение) не роняем: даём
 * код и начало тела для диагностики.
 */
export function apiErrorMessage(status: number, body: string): string {
  const base = describeApiError(status, body);
  // 401 на первом запуске — самая частая и самая непонятная ситуация: токен
  // у приложения есть, но на порту стоит демон с другим (остаток прошлого
  // запуска, терминальный). Без подсказки остаётся только «неверный токен»,
  // а при этом приложение вовсе не показывает демон. Поля ввода токена у
  // приложения нет, поэтому дальше — только про то, что будет автоматически.
  return status === 401
    ? `${base} — на порту демон с другим ADCLICKER_CONTROL_TOKEN: найдите его` +
        " (lsof -nP -iTCP:8787 -sTCP:LISTEN) и остановите — приложение поднимёт" +
        " свой демон, как только порт освободится"
    : base;
}

/** Текст ошибки без пояснения для 401 — тело демона или HTTP-код. */
function describeApiError(status: number, body: string): string {
  if (body) {
    try {
      const parsed: unknown = JSON.parse(body);
      if (
        typeof parsed === "object" &&
        parsed !== null &&
        "error" in parsed &&
        typeof parsed.error === "object" &&
        parsed.error !== null &&
        "message" in parsed.error &&
        typeof parsed.error.message === "string"
      ) {
        return parsed.error.message;
      }
    } catch {
      // не JSON — падаем в общий вид ниже
    }
    return `HTTP ${status}: ${body}`;
  }
  return `HTTP ${status}`;
}

/**
 * Текст ошибки из исключения транспорта.
 *
 * Tauri-команда отдаёт Err как строку, сюда же попадают Error из API-слоя.
 * Любое значение приводится к строке, чтобы показать пользователю хоть что-то.
 */
export function errorMessage(error: unknown): string {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * Текст баннера «нет связи с демоном».
 *
 * Причина из супервизора (`daemon_status.last_error`) сильнее симптома из
 * опроса: «порт занят чужим демоном» / «не найден config.json» объясняют
 * ситуацию, а «неверный токен» и «нет соединения» — лишь следствие. Без
 * причины баннер говорил одно и то же и не вёл к первопричине.
 */
export function offlineBannerText(
  supervisorError: string | null,
  pollError: string | null,
): string {
  const reason = (supervisorError ?? pollError)?.trim().replace(/\.$/, "");
  const head = reason
    ? `Нет связи с демоном: ${reason}`
    : "Нет связи с демоном";
  return `${head}. Данные на экранах могут быть устаревшими.`;
}
