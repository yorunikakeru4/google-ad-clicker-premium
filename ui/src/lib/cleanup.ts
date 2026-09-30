// API-слой очистки профилей поверх control API демона.
//
// Контракт (engine/control_plane/api.py, engine/cleanup.py):
//   POST /control/cleanup/run     {} | {"dry_run": true} → {"report": {...}};
//   GET  /control/cleanup/status  → {"last": {"ts", "report"} | null,
//                                    "next_run": epoch | null}.
//
// Разбор строгий: счётчики обязаны быть конечными числами, иначе UI показал
// бы «удалено NaN». Пути и тела собирает lib/control::cleanupRequest — здесь
// остаётся разбор ответов и читаемость ошибок.

import { apiErrorMessage, cleanupRequest, type CleanupAction } from "./control";
import { tauriTransport, type Transport } from "./daemonApi";

/** Отчёт одного прогона — поля контракта POST /control/cleanup/run. */
export interface CleanupReport {
  /** Удалено каталогов; при dry_run — найдено кандидатов. */
  removed: number;
  /** Освобождённые байты; при dry_run — размер найденных кандидатов. */
  removed_bytes: number;
  /** Пропущено как активные (живой PID или heartbeat). */
  skipped_active: number;
  errors: number;
  duration_ms: number;
  /** true — перечисление кандидатов, ничего не удалено. */
  dry_run: boolean;
}

/** Последний реальный прогон: метка времени плюс его отчёт. */
export interface CleanupLastRun {
  ts: number;
  report: CleanupReport;
}

export interface CleanupStatus {
  last: CleanupLastRun | null;
  /** Ближайший запуск по расписанию; null — job выключен. */
  next_run: number | null;
}

/** Цвет алерта с отчётом: ошибки — предупреждение, остальное — успех. */
export type CleanupTone = "success" | "warning";

const REPORT_PATH = "/control/cleanup/run";
const STATUS_PATH = "/control/cleanup/status";

function asFiniteNumber(value: unknown, field: string, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`ответ демона без числа ${field} на ${path}`);
  }
  return value;
}

/**
 * Отчёт из ответа демона; кривое поле — ошибка с путём, а не NaN в UI.
 *
 * `dry_run` читается как «только явно true»: движок пишет поле всегда, но
 * превью помечать безопаснее лишь когда сервер его и подтвердил — иначе
 * потерянный флаг выдал бы реальный прогон за «ничего не удалено».
 */
export function parseCleanupReport(raw: unknown, path: string): CleanupReport {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error(`не-объект отчёта на ${path}`);
  }
  const source = raw as Record<string, unknown>;
  return {
    removed: asFiniteNumber(source.removed, "removed", path),
    removed_bytes: asFiniteNumber(source.removed_bytes, "removed_bytes", path),
    skipped_active: asFiniteNumber(source.skipped_active, "skipped_active", path),
    errors: asFiniteNumber(source.errors, "errors", path),
    duration_ms: asFiniteNumber(source.duration_ms, "duration_ms", path),
    dry_run: source.dry_run === true,
  };
}

/**
 * Статус из ответа демона.
 *
 * Оба ключа обязательны: `last: null` — прогона не было, `next_run: null` —
 * расписание выключено; отсутствие ключа отличается от null и означает
 * изменившийся контракт — это ошибка, а не «данных нет».
 */
export function parseCleanupStatus(raw: unknown, path: string): CleanupStatus {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error(`не-объект статуса на ${path}`);
  }
  const source = raw as Record<string, unknown>;
  if (!("last" in source)) throw new Error(`ответ демона без last на ${path}`);
  if (!("next_run" in source)) throw new Error(`ответ демона без next_run на ${path}`);

  let last: CleanupLastRun | null = null;
  if (source.last !== null) {
    const lastRaw = source.last;
    if (typeof lastRaw !== "object" || Array.isArray(lastRaw)) {
      throw new Error(`ответ демона без объекта last на ${path}`);
    }
    const record = lastRaw as Record<string, unknown>;
    if (!("ts" in record)) throw new Error(`ответ демона без last.ts на ${path}`);
    last = {
      ts: asFiniteNumber(record.ts, "ts", path),
      report: parseCleanupReport(record.report, path),
    };
  }

  const nextRun =
    source.next_run === null
      ? null
      : asFiniteNumber(source.next_run, "next_run", path);

  return { last, next_run: nextRun };
}

/** Ошибок больше нуля — предупреждение (жёлтый), иначе — успех. */
export function cleanupReportTone(report: CleanupReport | null): CleanupTone {
  return report !== null && report.errors > 0 ? "warning" : "success";
}

export interface CleanupApi {
  /** Один прогон; dryRun=true — перечисление кандидатов без удаления. */
  run(dryRun: boolean): Promise<CleanupReport>;
  status(): Promise<CleanupStatus>;
}

export function createCleanupApi(transport: Transport): CleanupApi {
  async function send(action: CleanupAction): Promise<unknown> {
    const request = cleanupRequest(action);
    const reply = await transport({
      path: request.path,
      method: request.method,
      body: request.body,
    });
    if (reply.status < 200 || reply.status >= 300) {
      throw new Error(apiErrorMessage(reply.status, reply.body));
    }
    try {
      return JSON.parse(reply.body) as unknown;
    } catch {
      throw new Error(
        `не-JSON ответ демона на ${request.path}: ${reply.body.slice(0, 200)}`,
      );
    }
  }

  return {
    async run(dryRun) {
      const payload = await send({ kind: "run", dryRun });
      const report =
        typeof payload === "object" && payload !== null
          ? (payload as { report?: unknown }).report
          : undefined;
      if (report === undefined) {
        throw new Error(`ответ демона без report на ${REPORT_PATH}`);
      }
      return parseCleanupReport(report, REPORT_PATH);
    },

    async status() {
      return parseCleanupStatus(await send({ kind: "status" }), STATUS_PATH);
    },
  };
}

/** Боевой экземпляр: транспорт — Rust-команда control_request. */
export const cleanupApi: CleanupApi = createCleanupApi(tauriTransport);
