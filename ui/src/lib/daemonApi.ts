// API-слой над HTTP-прокси демона.
//
// Транспорт подменяется в тестах: боевой ходит через Rust-команду
// control_request (см. src-tauri/src/control.rs), тестовый возвращает
// готовые ответы. Здесь только контракт ответов демона: 200 — JSON-тело,
// любая ошибка — читаемый текст из `{"error": {"message"}}`.

import { invoke } from "@tauri-apps/api/core";
import { apiErrorMessage, controlRequest, type ControlAction } from "./control";
import type { Health, StateSnapshot } from "./types";

export interface TransportRequest {
  path: string;
  method: "GET" | "POST";
  body?: string;
}

export interface TransportReply {
  status: number;
  body: string;
}

export type Transport = (request: TransportRequest) => Promise<TransportReply>;

/** Боевой транспорт: HTTP к демону идёт через Rust, мимо CSP Tauri. */
export const tauriTransport: Transport = (request) =>
  invoke<TransportReply>("control_request", {
    path: request.path,
    method: request.method,
    body: request.body ?? null,
  });

export interface DaemonApi {
  health(): Promise<Health>;
  state(): Promise<StateSnapshot>;
  /** Возвращает тело успешного ответа. */
  control(action: ControlAction): Promise<string>;
}

function parseJson<T>(body: string, path: string): T {
  try {
    return JSON.parse(body) as T;
  } catch {
    throw new Error(`не-JSON ответ демона на ${path}: ${body.slice(0, 200)}`);
  }
}

async function getJson<T>(
  transport: Transport,
  path: string,
): Promise<T> {
  const reply = await transport({ path, method: "GET" });
  if (reply.status !== 200) {
    throw new Error(apiErrorMessage(reply.status, reply.body));
  }
  return parseJson<T>(reply.body, path);
}

export function createDaemonApi(transport: Transport): DaemonApi {
  return {
    health: () => getJson<Health>(transport, "/health"),
    state: () => getJson<StateSnapshot>(transport, "/state"),
    async control(action: ControlAction): Promise<string> {
      const request = controlRequest(action);
      const reply = await transport({
        path: request.path,
        method: request.method,
      });
      if (reply.status < 200 || reply.status >= 300) {
        throw new Error(apiErrorMessage(reply.status, reply.body));
      }
      return reply.body;
    },
  };
}
