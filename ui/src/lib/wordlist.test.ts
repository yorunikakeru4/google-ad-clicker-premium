// API-слой списков (queries/domains): контракт ответов и разбор ошибок.
//
// Транспорт подменяется (боевой — Rust control_request, см. daemonApi):
// здесь проверяется ровно то, на чём стоит UI — снапшоты, счётчики
// добавления/удаления и читаемость ошибок демона.

import { describe, expect, it } from "vitest";
import {
  createWordlistApi,
  pickDomains,
  pickQueries,
} from "./wordlist";
import { domainsRequest, queriesRequest } from "./control";
import type { Transport } from "./daemonApi";

const QUERIES_SNAPSHOT = {
  queries: ["usb hub"],
  source: "file",
  query_file: "queries.txt",
  query: "",
};

const QUERIES_BASE = "/control/queries";
const DOMAINS_BASE = "/control/domains";

/** Транспорт-фейк: фиксирует отправленные запросы и отдаёт заготовленные ответы. */
function requestsTransport(replies: Record<string, { status: number; body: string }>) {
  const sent: Array<{ path: string; method: string; body?: string }> = [];
  const transport: Transport = async (request) => {
    sent.push({ path: request.path, method: request.method, body: request.body });
    const reply = replies[request.path];
    if (!reply) throw new Error(`неожиданный запрос ${request.method} ${request.path}`);
    return reply;
  };
  return { transport, sent };
}

const DOMAINS_SNAPSHOT = {
  domains: ["edelind.de"],
  filtered_domains: "domains.txt",
  own_domain: "edelind.de",
  excludes: "goldkette",
};

describe("pickQueries", () => {
  it("разбирает полный снапшот", () => {
    expect(pickQueries(QUERIES_SNAPSHOT, "/control/queries")).toEqual(QUERIES_SNAPSHOT);
  });

  it("принимает source single — одиночный запрос как источник", () => {
    const snapshot = pickQueries(
      { ...QUERIES_SNAPSHOT, source: "single", queries: [], query: "wireless" },
      "/control/queries",
    );

    expect(snapshot.source).toBe("single");
    expect(snapshot.query).toBe("wireless");
  });

  it("отклоняет снапшот без массива строк", () => {
    expect(() => pickQueries({ ...QUERIES_SNAPSHOT, queries: "usb" }, "/control/queries"))
      .toThrowError(/без массива строк queries/);
  });

  it("отклоняет неизвестный источник", () => {
    expect(
      () => pickQueries({ ...QUERIES_SNAPSHOT, source: "db" }, "/control/queries"),
    ).toThrowError(/неизвестный источник/);
  });
});

describe("pickDomains", () => {
  it("разбирает список вместе с настройками чёрного списка", () => {
    expect(pickDomains(DOMAINS_SNAPSHOT, "/control/domains")).toEqual(DOMAINS_SNAPSHOT);
  });

  it("отклоняет снапшот без строки own_domain", () => {
    const broken = { ...DOMAINS_SNAPSHOT } as Record<string, unknown>;
    delete broken.own_domain;

    expect(() => pickDomains(broken, "/control/domains")).toThrowError(
      /без строки own_domain/,
    );
  });
});

describe("createWordlistApi", () => {
  it("list шлёт GET на базовый путь и разбирает снапшот", async () => {
    const { transport, sent } = requestsTransport({
      [QUERIES_BASE]: { status: 200, body: JSON.stringify(QUERIES_SNAPSHOT) },
    });
    const api = createWordlistApi(queriesRequest, transport, pickQueries);

    const snapshot = await api.list();

    expect(snapshot.queries).toEqual(["usb hub"]);
    expect(sent).toEqual([{ path: QUERIES_BASE, method: "GET", body: undefined }]);
  });

  it("add кладёт строки в тело и отдаёт счётчики", async () => {
    const { transport, sent } = requestsTransport({
      [QUERIES_BASE]: {
        status: 200,
        body: JSON.stringify({ added: 1, skipped: 1, problems: ["уже есть: q"] }),
      },
    });
    const api = createWordlistApi(queriesRequest, transport, pickQueries);

    const result = await api.add(["q", "Q"]);

    expect(result).toEqual({ added: 1, skipped: 1, problems: ["уже есть: q"] });
    expect(JSON.parse(sent[0].body ?? "{}")).toEqual({ lines: ["q", "Q"] });
  });

  it("remove шлёт значения под ключом списка, removeAll — {all:true}", async () => {
    const { transport, sent } = requestsTransport({
      [`${QUERIES_BASE}/delete`]: {
        status: 200,
        body: JSON.stringify({ deleted: 1, skipped: 0, problems: [] }),
      },
    });
    const api = createWordlistApi(queriesRequest, transport, pickQueries);

    await api.remove(["usb hub"]);
    await api.removeAll();

    expect(JSON.parse(sent[0].body ?? "{}")).toEqual({ queries: ["usb hub"] });
    expect(JSON.parse(sent[1].body ?? "{}")).toEqual({ all: true });
  });

  it("filePath отдаёт путь и флаг наличия файла", async () => {
    const { transport } = requestsTransport({
      [`${DOMAINS_BASE}/file`]: {
        status: 200,
        body: JSON.stringify({ path: "/data/domains.txt", exists: false }),
      },
    });
    const api = createWordlistApi(domainsRequest, transport, pickDomains);

    expect(await api.filePath()).toEqual({ path: "/data/domains.txt", exists: false });
  });

  it("ошибка демона доходит текстом из {error:{message}}", async () => {
    const { transport } = requestsTransport({
      [QUERIES_BASE]: {
        status: 400,
        body: JSON.stringify({
          error: { code: "invalid_request", message: "paths.query_file не задан" },
        }),
      },
    });
    const api = createWordlistApi(queriesRequest, transport, pickQueries);

    await expect(api.list()).rejects.toThrowError("paths.query_file не задан");
  });

  it("не-JSON ответ падает с указанием пути", async () => {
    const { transport } = requestsTransport({
      [QUERIES_BASE]: { status: 200, body: "<html>прокси" },
    });
    const api = createWordlistApi(queriesRequest, transport, pickQueries);

    await expect(api.list()).rejects.toThrowError(/не-JSON ответ демона/);
  });
});
