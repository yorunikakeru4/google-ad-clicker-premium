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

/** Действия API прокси (контракт /control/proxies* параллельной ветки). */
export type ProxiesAction =
  | { kind: "list" }
  | { kind: "add"; lines: string[] }
  | { kind: "import" }
  | { kind: "delete"; id: number }
  | { kind: "check" };

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
    case "check":
      return { method: "POST", path: "/control/proxies/check", body: "{}" };
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
