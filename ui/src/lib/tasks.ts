// Логика экрана Tasks: расписание запуска, источник запросов и диапазоны пауз.
//
// Трактовка окна — не выдумка UI: она повторяет engine/scheduler.py
// (inside_running_interval), иначе экран врал бы о поведении движка:
//
// - пустое окно и 00:00–00:00 — «всегда включено»;
// - ночное окно (23:00–06:00) легально: сравнение идёт по длине окна
//   по модулю суток, а не «start < end»;
// - окно короче 10 минут и окно, заданное частично, — IntervalError,
//   из-за которого воркеры не стартуют, а ждут и ругаются в лог;
// - вне окна воркеры спят и перепроверяют время раз в минуту
//   (OUTSIDE_INTERVAL_WAIT_SECONDS = 60).
//
// Экран только читает конфиг: правится он в Settings, здесь — показ и ссылка.

import { apiErrorMessage, configRequest } from "./control";
import { tauriTransport, type Transport } from "./daemonApi";

/** Поля конфига, которые экран Tasks читает из GET /control/config. */
export interface TasksConfig {
  /** paths.query_file — файл с запросами (пусто = не задан). */
  queryFile: string;
  /** behavior.query — одиночный запрос (пусто = не задан). */
  query: string;
  /** behavior.running_interval_start, ЧЧ:ММ или пусто. */
  intervalStart: string;
  /** behavior.running_interval_end, ЧЧ:ММ или пусто. */
  intervalEnd: string;
  adPageMinWait: number;
  adPageMaxWait: number;
  nonadPageMinWait: number;
  nonadPageMaxWait: number;
  loopWaitTime: number;
}

/**
 * Подсказка про взаимную exclusivity источников запросов.
 *
 * Одна строка на все случаи (file/single/conflict): правило одно и то же,
 * его дублирование в трёх местах гарантировано разошлось бы.
 */
export const QUERY_EXCLUSIVITY_HINT =
  "Поля paths.query_file и behavior.query взаимно исключающие: демон принимает только одно из них — оставьте что-то одно.";

function expectObject(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`ответ демона без ${what} на /control/config`);
  }
  return value as Record<string, unknown>;
}

function sectionOf(
  config: Record<string, unknown>,
  name: string,
): Record<string, unknown> {
  return expectObject(config[name], `секции ${name}`);
}

function stringField(section: Record<string, unknown>, name: string): string {
  if (!(name in section)) {
    throw new Error(`ответ демона без behavior.${name} на /control/config`);
  }
  const value = section[name];
  if (typeof value !== "string") {
    throw new Error(`поле behavior.${name} должно быть текстом, получено ${typeof value}`);
  }
  return value;
}

function pathField(section: Record<string, unknown>, name: string): string {
  if (!(name in section)) {
    throw new Error(`ответ демона без paths.${name} на /control/config`);
  }
  const value = section[name];
  if (typeof value !== "string") {
    throw new Error(`поле paths.${name} должно быть текстом, получено ${typeof value}`);
  }
  return value;
}

function numberField(section: Record<string, unknown>, name: string): number {
  if (!(name in section)) {
    throw new Error(`ответ демона без behavior.${name} на /control/config`);
  }
  const value = section[name];
  if (typeof value !== "number") {
    throw new Error(`поле behavior.${name} должно быть числом, получено ${typeof value}`);
  }
  return value;
}

/**
 * Разбор ответа GET /control/config.
 *
 * Отсутствующее поле — ошибка с его именем, а не молчаливый undefined:
 * экран обязан либо показать значения, либо сказать, что демон прислал
 * неполный ответ, — но не рисовать прочерки на месте чисел.
 */
export function pickTasksConfig(payload: unknown): TasksConfig {
  const root = expectObject(payload, "объекта {config: ...}");
  const config = expectObject(root.config, "поля config");
  const paths = sectionOf(config, "paths");
  const behavior = sectionOf(config, "behavior");

  return {
    queryFile: pathField(paths, "query_file"),
    query: stringField(behavior, "query"),
    intervalStart: stringField(behavior, "running_interval_start"),
    intervalEnd: stringField(behavior, "running_interval_end"),
    adPageMinWait: numberField(behavior, "ad_page_min_wait"),
    adPageMaxWait: numberField(behavior, "ad_page_max_wait"),
    nonadPageMinWait: numberField(behavior, "nonad_page_min_wait"),
    nonadPageMaxWait: numberField(behavior, "nonad_page_max_wait"),
    loopWaitTime: numberField(behavior, "loop_wait_time"),
  };
}

/** Как движок трактует окно: ok — работает, error — воркеры не стартуют. */
export type ScheduleKind =
  | "always"
  | "day"
  | "overnight"
  | "partial"
  | "too_short"
  | "bad_format";

export type ScheduleTone = "ok" | "error";

export interface ScheduleInfo {
  kind: ScheduleKind;
  tone: ScheduleTone;
  /** Короткая строка для карточки: «23:00–06:00 (через полночь)». */
  label: string;
  /** Как именно движок трактует окно — честно, словами движка. */
  detail: string;
}

// Тот же формат, что _INTERVAL_RE в engine/control_plane/config.py:
// две цифры часа, две цифры минуты, 00:00–23:59.
const CLOCK_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

// MIN_INTERVAL_SECONDS из engine/scheduler.py.
const MIN_INTERVAL_SECONDS = 10 * 60;
const DAY_SECONDS = 24 * 3600;

function secondsOfDay(clock: string): number {
  const [hours, minutes] = clock.split(":").map(Number);
  return hours * 3600 + minutes * 60;
}

/**
 * Окно расписания глазами движка.
 *
 * Порядок проверок повторяет inside_running_interval: пустое окно, частичное
 * окно, формат, 00:00–00:00, минимальная длина, положение относительно
 * полуночи. Каждая ветка отдаёт и label для карточки, и detail с тем, что
 * сделает демон, — экран не может «не показать» ошибочное окно.
 */
export function describeSchedule(start: string, end: string): ScheduleInfo {
  if (start === "" && end === "") {
    return {
      kind: "always",
      tone: "ok",
      label: "Круглосуточно",
      detail:
        "Окно не задано — ограничения по времени нет: воркеры работают все сутки.",
    };
  }

  if (start === "" || end === "") {
    const missing = end === "" ? "running_interval_end" : "running_interval_start";
    return {
      kind: "partial",
      tone: "error",
      label: "Окно задано частично",
      detail:
        `${missing} не заполнено — движок отклоняет такое окно (IntervalError) ` +
        `и воркеры не стартуют, а ждут и ругаются в лог. Заполните обе границы ` +
        `либо очистите обе.`,
    };
  }

  if (!CLOCK_RE.test(start) || !CLOCK_RE.test(end)) {
    return {
      kind: "bad_format",
      tone: "error",
      label: `${start}–${end}`,
      detail:
        "Формат не ЧЧ:ММ в пределах 00:00–23:59 — движок не разберёт окно " +
        "(IntervalError), воркеры не стартуют.",
    };
  }

  // 00:00–00:00 — «всегда включено», а не нулевое окно: это значение стоит
  // в config.json по умолчанию (см. _ALWAYS в engine/scheduler.py).
  if (start === "00:00" && end === "00:00") {
    return {
      kind: "always",
      tone: "ok",
      label: "Круглосуточно",
      detail: "Окно 00:00–00:00 = всегда включено: ограничения по времени нет.",
    };
  }

  // Длина окна по модулю суток: именно так ночное окно оказывается легальным,
  // а 12:25–12:30 (5 минут) — нет.
  const begin = secondsOfDay(start);
  const finish = secondsOfDay(end);
  const duration = (finish - begin + DAY_SECONDS) % DAY_SECONDS;

  if (duration < MIN_INTERVAL_SECONDS) {
    const minutes = Math.floor(duration / 60);
    return {
      kind: "too_short",
      tone: "error",
      label: `${start}–${end}`,
      detail:
        `Окно ${start}–${end} длится ${minutes} мин — движок отклоняет окно ` +
        `короче 10 минут (IntervalError), воркеры не стартуют.`,
    };
  }

  if (begin < finish) {
    return {
      kind: "day",
      tone: "ok",
      label: `${start}–${end}`,
      detail:
        `Окно внутри одних суток: воркеры берут сценарии с ${start} до ${end}; ` +
        `вне окна спят и перепроверяют время раз в минуту.`,
    };
  }

  return {
    kind: "overnight",
    tone: "ok",
    label: `${start}–${end} (через полночь)`,
    detail:
      `Ночное окно через полночь: воркеры берут сценарии с ${start} до 00:00 ` +
      `и с 00:00 до ${end} — окно идёт через полуночь и трактуется как ` +
      `непрерывное; вне окна спят и перепроверяют время раз в минуту.`,
  };
}

/** Откуда берутся запросы: файл, одиночный запрос, ни того ни другого, конфликт. */
export type QuerySourceKind = "file" | "single" | "conflict" | "none";

export interface QuerySourceInfo {
  kind: QuerySourceKind;
  /** Строка для карточки: «Файл: queries.txt». */
  label: string;
  /** Подсказка о взаимной exclusivity полей. */
  hint: string;
}

/**
 * Какой источник запросов активен.
 *
 * Оба заполнены — это не «выберем любой», а конфликт: валидация конфига
 * (engine/control_plane/config.py, _validate_cross_field) отклоняет такую
 * комбинацию, и экран обязан показать её как ошибку, а не прятать.
 */
export function querySource(config: TasksConfig): QuerySourceInfo {
  const file = config.queryFile.trim();
  const query = config.query.trim();

  if (file !== "" && query !== "") {
    return {
      kind: "conflict",
      label: `Конфликт: файл (${file}) и одиночный запрос (${query})`,
      hint: QUERY_EXCLUSIVITY_HINT,
    };
  }
  if (file !== "") {
    return {
      kind: "file",
      label: `Файл: ${file}`,
      hint: QUERY_EXCLUSIVITY_HINT,
    };
  }
  if (query !== "") {
    return {
      kind: "single",
      label: `Одиночный запрос: ${query}`,
      hint: QUERY_EXCLUSIVITY_HINT,
    };
  }
  return {
    kind: "none",
    label: "Не задан",
    hint: "Заполните paths.query_file или behavior.query: без источника запросов сценарий не собрать.",
  };
}

export interface WaitRangeRow {
  key: "ad" | "nonad" | "loop";
  /** Подпись с именем поля конфига — чтобы искать в Settings было сразу. */
  label: string;
  /** Готовое значение: «10–15 с», «60 с». */
  value: string;
}

function formatSeconds(min: number, max: number): string {
  return min === max ? `${min} с` : `${min}–${max} с`;
}

/**
 * Справка по диапазонам пауз — read-only.
 *
 * Экран редактором не является (правится в Settings), поэтому здесь только
 * значения и подписи с точными именами полей.
 */
export function waitRanges(config: TasksConfig): WaitRangeRow[] {
  return [
    {
      key: "ad",
      label: "Рекламная страница (behavior.ad_page_min_wait … ad_page_max_wait)",
      value: formatSeconds(config.adPageMinWait, config.adPageMaxWait),
    },
    {
      key: "nonad",
      label: "Обычная страница (behavior.nonad_page_min_wait … nonad_page_max_wait)",
      value: formatSeconds(config.nonadPageMinWait, config.nonadPageMaxWait),
    },
    {
      key: "loop",
      label: "Пауза между итерациями (behavior.loop_wait_time)",
      value: `${config.loopWaitTime} с`,
    },
  ];
}

export interface TasksApi {
  loadConfig(): Promise<TasksConfig>;
}

/**
 * Чтение конфига экрана Tasks.
 *
 * Контракт ответа ({"config": {...}} и читаемость ошибок) живёт здесь, пути
 * собирает lib/control.configRequest — как у прокси и профилей.
 */
export function createTasksApi(transport: Transport): TasksApi {
  return {
    async loadConfig(): Promise<TasksConfig> {
      const request = configRequest();
      const reply = await transport({
        path: request.path,
        method: request.method,
        body: request.body,
      });
      if (reply.status !== 200) {
        throw new Error(apiErrorMessage(reply.status, reply.body));
      }
      let payload: unknown;
      try {
        payload = JSON.parse(reply.body);
      } catch {
        throw new Error(
          `не-JSON ответ демона на ${request.path}: ${reply.body.slice(0, 200)}`,
        );
      }
      return pickTasksConfig(payload);
    },
  };
}

/** Боевой экземпляр: транспорт — Rust-команда control_request. */
export const tasksApi: TasksApi = createTasksApi(tauriTransport);
