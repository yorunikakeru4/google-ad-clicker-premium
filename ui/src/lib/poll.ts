// Reducer опроса демона: /health + /state раз в секунду (план §5, фаза 4).
//
// Тик нумеруется (seq) и может быть в полёте только один: параллельный begin
// отклоняется, а ответ с чужим seq (опоздавший после смены тика) игнорируется.
// Без этого два тика в полёте перетирали бы состояние в произвольном порядке
// и индикатор heartbeat мерцал бы между разными срезами.
//
// `inflight` и `phase` разделены намеренно. Раньше «тик в полёте» сам по себе
// был фазой, из-за чего каждый успешный опрос на время сетевого запроса
// выглядел как «нет связи»: баннер мигал ~раз в секунду, чип сбрасывался в
// «нет данных», кнопки гасли с «демон недоступен». Теперь фаза — это последнее
// **известное** состояние (idle | online | offline), а inflight — просто флаг
// «ждём ответ», который ничего не говорит о доступности.

import type { Health, StateSnapshot } from "./types";

export type PollPhase = "idle" | "online" | "offline";

/**
 * Сколько неудачных тиков подряд, прежде чем считать демон офлайным.
 *
 * Один сбой (рестарт демона, опоздавший ответ, заминка на старте) раньше
 * рисовал баннер «нет связи» почти сразу и тут же гасил его при
 * восстановлении — секундное моргание, на которое не посмотреть. Три тика
 * при интервале в секунду — это уже три секунды недоступности.
 */
export const OFFLINE_AFTER_FAILURES = 3;

export interface PollState {
  /** Последнее известное состояние: idle — ответа ещё не было. */
  phase: PollPhase;
  /** Ждём ответ текущего тика. Не влияет на phase. */
  inflight: boolean;
  /** Номер последнего начатого тика. */
  seq: number;
  /** Эпоха в мс последнего успешного ответа; хранится и после офлайна. */
  lastOkAt: number | null;
  lastError: string | null;
  /** Неудачных тиков подряд; сбрасывается успешным ответом. */
  failures: number;
  health: Health | null;
  snapshot: StateSnapshot | null;
}

export type PollEvent =
  | { kind: "begin" }
  | {
      kind: "success";
      seq: number;
      health: Health;
      snapshot: StateSnapshot;
      at: number;
    }
  | { kind: "failure"; seq: number; error: string; at: number };

export function createPollState(): PollState {
  return {
    phase: "idle",
    inflight: false,
    seq: 0,
    lastOkAt: null,
    lastError: null,
    failures: 0,
    health: null,
    snapshot: null,
  };
}

export function reducePoll(state: PollState, event: PollEvent): PollState {
  switch (event.kind) {
    case "begin":
      // Тик уже в полёте — второй не запускаем, состояние не трогаем.
      if (state.inflight) return state;
      return { ...state, inflight: true, seq: state.seq + 1 };

    case "success": {
      // Ответ старого тика: не перетираем актуальное состояние.
      if (event.seq !== state.seq || !state.inflight) return state;
      return {
        ...state,
        phase: "online",
        inflight: false,
        lastOkAt: event.at,
        lastError: null,
        failures: 0,
        health: event.health,
        snapshot: event.snapshot,
      };
    }

    case "failure": {
      if (event.seq !== state.seq || !state.inflight) return state;
      const failures = state.failures + 1;
      const failed = { ...state, inflight: false, failures, lastError: event.error };

      // Ниже порога фазу не меняем: «online» значит «был на связи секунду
      // назад», и баннер/чип/кнопки не должны моргать из-за одного сбоя.
      if (failures < OFFLINE_AFTER_FAILURES) return failed;

      // Явный off-стан: данные чистим, чтобы не показывать устаревшее как
      // живое. lastOkAt сохраняется — по нему живёт «последний ответ».
      return {
        ...failed,
        phase: "offline",
        health: null,
        snapshot: null,
      };
    }
  }
}
