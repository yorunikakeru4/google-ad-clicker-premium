// Контракт API прокси: пути, тела запросов, разбор ответов демона и
// клиентские помощники экрана (разбор строк, view-model строки таблицы).
//
// Транспорт подменяется так же, как в daemonApi.test.ts: тесты проверяют
// именно то, что уходит в control_request, и то, что остаётся в UI.
// Креды (username/password) не должны пережить даже ответ демона с ними.

import { describe, expect, it } from "vitest";
import {
  createProxiesApi,
  parseProxyLines,
  proxyApiError,
  toProxyTableRow,
  type ProxyRow,
} from "./proxies";
import type { Transport } from "./daemonApi";

const PROXY_JSON = {
  id: 1,
  label: "немецкий",
  scheme: "http",
  host: "a.example",
  port: 8080,
  latency_ms: 120,
  is_alive: true,
  fail_count: 0,
  last_checked_at: 1_000.0,
  last_error: null,
  assigned_browser_id: "br-1",
  usage_count: 3,
};

function transportOf(
  replies: Record<string, { status: number; body: string }>,
): { transport: Transport; calls: { path: string; method: string; body?: string }[] } {
  const calls: { path: string; method: string; body?: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({ path: request.path, method: request.method, body: request.body });
    const reply = replies[request.path];
    if (!reply) throw new Error(`нет заглушки для ${request.path}`);
    return reply;
  };
  return { transport, calls };
}

function errorBody(code: string, message?: string): string {
  return JSON.stringify({ error: { code, message } });
}

describe("createProxiesApi: список", () => {
  it("GET /control/proxies без тела, строки разбираются по контракту", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies": { status: 200, body: JSON.stringify({ proxies: [PROXY_JSON] }) },
    });
    const api = createProxiesApi(transport);

    const rows = await api.list();

    expect(calls).toEqual([
      { path: "/control/proxies", method: "GET", body: undefined },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].host).toBe("a.example");
    expect(rows[0].port).toBe(8080);
    expect(rows[0].assigned_browser_id).toBe("br-1");
    expect(rows[0].usage_count).toBe(3);
    expect(rows[0].is_alive).toBe(true);
  });

  it("креды из ответа демона не попадают в данные UI", async () => {
    const { transport } = transportOf({
      "/control/proxies": {
        status: 200,
        body: JSON.stringify({
          proxies: [
            {
              ...PROXY_JSON,
              username: "user",
              password: "secret-password",
            },
          ],
        }),
      },
    });
    const api = createProxiesApi(transport);

    const [row] = await api.list();

    expect(Object.keys(row)).not.toContain("username");
    expect(Object.keys(row)).not.toContain("password");
    expect("password" in row).toBe(false);
    expect(JSON.stringify(row)).not.toContain("secret-password");
  });

  it("не-200 — ошибка с сообщением демона", async () => {
    const { transport } = transportOf({
      "/control/proxies": {
        status: 500,
        body: errorBody("internal_error", "внутренняя ошибка демона"),
      },
    });
    const api = createProxiesApi(transport);

    await expect(api.list()).rejects.toThrow("внутренняя ошибка демона");
  });

  it("ответ без поля proxies — читаемая ошибка, а не тихий пустой список", async () => {
    const { transport } = transportOf({
      "/control/proxies": { status: 200, body: '{"ok":true}' },
    });
    const api = createProxiesApi(transport);

    await expect(api.list()).rejects.toThrow(/без списка прокси/);
  });

  it("обрыв транспорта доходит до вызывающего", async () => {
    const transport: Transport = async () => {
      throw new Error("нет соединения с демоном на 127.0.0.1:8787");
    };
    const api = createProxiesApi(transport);

    await expect(api.list()).rejects.toThrow("нет соединения");
  });
});

describe("createProxiesApi: добавление и импорт", () => {
  it("add шлёт строки как есть в POST /control/proxies", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies": {
        status: 200,
        body: JSON.stringify({ added: 2, skipped: 1, problems: ["дубликат"] }),
      },
    });
    const api = createProxiesApi(transport);

    const result = await api.add(["http://a:1", "b:2"]);

    expect(calls).toEqual([
      {
        path: "/control/proxies",
        method: "POST",
        body: JSON.stringify({ lines: ["http://a:1", "b:2"] }),
      },
    ]);
    expect(result).toEqual({ added: 2, skipped: 1, problems: ["дубликат"] });
  });

  it("import бьёт в /control/proxies/import пустым телом", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/import": {
        status: 200,
        body: JSON.stringify({ added: 5, skipped: 0, problems: [] }),
      },
    });
    const api = createProxiesApi(transport);

    const result = await api.importFile();

    expect(calls).toEqual([
      { path: "/control/proxies/import", method: "POST", body: "{}" },
    ]);
    expect(result.added).toBe(5);
  });

  it("ответ без added/skipped/problems — ошибка, а не нули", async () => {
    const { transport } = transportOf({
      "/control/proxies": { status: 200, body: '{"ok":true}' },
    });
    const api = createProxiesApi(transport);

    await expect(api.add(["a:1"])).rejects.toThrow(/added/);
  });

  it("problems-объекты демона ({line_index, message}, {index, message}) → строки", async () => {
    const { transport } = transportOf({
      "/control/proxies": {
        status: 200,
        body: JSON.stringify({
          added: 1,
          skipped: 2,
          problems: [
            { line_index: 1, message: "нет порта" },
            { index: 0, message: "дубликат" },
            "уже готовая строка",
          ],
        }),
      },
      "/control/proxies/import": {
        status: 200,
        body: JSON.stringify({
          added: 0,
          skipped: 1,
          problems: [{ line_index: 3, message: "некорректный адрес" }],
        }),
      },
    });
    const api = createProxiesApi(transport);

    const added = await api.add(["host-only"]);
    const imported = await api.importFile();

    // Раньше filter(typeof string) выбрасывал объекты — «пропущено 2» без причин.
    expect(added.problems).toEqual([
      "строка 2: нет порта",
      "запись 1: дубликат",
      "уже готовая строка",
    ]);
    expect(imported.problems).toEqual(["строка 4: некорректный адрес"]);
  });
});

describe("createProxiesApi: удаление и проверка", () => {
  it("delete шлёт id и возвращает deleted", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/delete": { status: 200, body: '{"deleted":1}' },
    });
    const api = createProxiesApi(transport);

    const deleted = await api.remove(7);

    expect(calls).toEqual([
      { path: "/control/proxies/delete", method: "POST", body: '{"id":7}' },
    ]);
    expect(deleted).toBe(1);
  });

  it("409 proxy_in_use с сообщением демона → его текст", async () => {
    const { transport } = transportOf({
      "/control/proxies/delete": {
        status: 409,
        body: errorBody("proxy_in_use", "прокси закреплён за br-1"),
      },
    });
    const api = createProxiesApi(transport);

    await expect(api.remove(7)).rejects.toThrow("прокси закреплён за br-1");  });

  it("409 proxy_in_use без сообщения → читаемый текст про поток", async () => {
    const { transport } = transportOf({
      "/control/proxies/delete": { status: 409, body: errorBody("proxy_in_use") },
    });
    const api = createProxiesApi(transport);

    await expect(api.remove(7)).rejects.toThrow(/назначен потоку/);
  });

  it("check ставит POST /control/proxies/check", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/check": { status: 200, body: '{"started":true}' },
    });
    const api = createProxiesApi(transport);

    await expect(api.check()).resolves.toBeUndefined();

    expect(calls).toEqual([
      { path: "/control/proxies/check", method: "POST", body: "{}" },
    ]);
  });

  it("409 check_in_progress без сообщения → читаемый текст про проверку", async () => {
    const { transport } = transportOf({
      "/control/proxies/check": {
        status: 409,
        body: errorBody("check_in_progress"),
      },
    });
    const api = createProxiesApi(transport);

    await expect(api.check()).rejects.toThrow(/Проверка уже идёт/);
  });

  it("не-JSON тело ошибки — код и начало тела, а не [object Object]", async () => {
    const { transport } = transportOf({
      "/control/proxies/check": { status: 502, body: "<html>bad gateway</html>" },
    });
    const api = createProxiesApi(transport);

    await expect(api.check()).rejects.toThrow(/HTTP 502/);
  });
});

describe("createProxiesApi: путь к файлу", () => {
  it("filePath бьёт в GET /control/proxies/file и отдаёт путь с exists", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/file": {
        status: 200,
        body: JSON.stringify({ path: "/data/proxies.txt", exists: true }),
      },
    });
    const api = createProxiesApi(transport);

    const file = await api.filePath();

    expect(file).toEqual({ path: "/data/proxies.txt", exists: true });
    expect(calls).toEqual([
      { path: "/control/proxies/file", method: "GET", body: undefined },
    ]);
  });

  it("отсутствующий файл — exists false, а не ошибка запроса", async () => {
    const { transport } = transportOf({
      "/control/proxies/file": {
        status: 200,
        body: JSON.stringify({ path: "/data/proxies.txt", exists: false }),
      },
    });
    const api = createProxiesApi(transport);

    await expect(api.filePath()).resolves.toEqual({
      path: "/data/proxies.txt",
      exists: false,
    });
  });

  it("ответ без path — читаемая ошибка, а не пустая строка в opener", async () => {
    const { transport } = transportOf({
      "/control/proxies/file": { status: 200, body: '{"exists":true}' },
    });
    const api = createProxiesApi(transport);

    await expect(api.filePath()).rejects.toThrow(/без path/);
  });

  it("путь в теле запроса не учитывается — его не существует вовсе", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/file": {
        status: 200,
        body: JSON.stringify({ path: "/data/proxies.txt", exists: true }),
      },
    });
    const api = createProxiesApi(transport);

    await api.filePath();

    expect(calls[0].body).toBeUndefined();
  });
});

describe("proxyApiError", () => {
  it("известный 409 без message берёт локальный текст", () => {
    expect(proxyApiError(409, errorBody("proxy_in_use"))).toMatch(
      /назначен потоку/,
    );
    expect(proxyApiError(409, errorBody("check_in_progress"))).toMatch(
      /Проверка уже идёт/,
    );
  });

  it("message демона сильнее локального текста", () => {
    expect(proxyApiError(409, errorBody("proxy_in_use", "br-3 держит прокси"))).toBe(
      "br-3 держит прокси",
    );
  });

  it("неизвестный код 409 и прочие статусы идут через apiErrorMessage", () => {
    expect(proxyApiError(409, errorBody("other_conflict", "другой конфликт"))).toBe(
      "другой конфликт",
    );
    expect(proxyApiError(500, "")).toBe("HTTP 500");
  });
});

describe("parseProxyLines", () => {
  it("пустые строки и комментарии отбрасываются, валидные проходят", () => {
    const parsed = parseProxyLines(
      "  http://a.example:8080 \n\n# комментарий\nuser:pass@b.example:3128\r\n",
    );

    expect(parsed.lines).toEqual([
      "http://a.example:8080",
      "user:pass@b.example:3128",
    ]);
    expect(parsed.problems).toEqual([]);
  });

  it("битые строки остаются в списке (сервер решает), но помечаются подсказкой", () => {
    const parsed = parseProxyLines(
      ["host-only", "a.example:0", "a.example:70000", "a.example:abc", "ok.example:80"].join(
        "\n",
      ),
    );

    expect(parsed.lines).toHaveLength(5);
    expect(parsed.problems).toHaveLength(4);
    expect(parsed.problems[0]).toMatch(/строка 1:.*порт/);
    expect(parsed.problems[1]).toMatch(/вне диапазона/);
    expect(parsed.problems[2]).toMatch(/вне диапазона/);
    expect(parsed.problems[3]).toMatch(/не число/);
    expect(parsed.problems.join("\n")).not.toMatch(/строка 5/);
  });

  it("строка с кредами валидируется по host:port после схемы и @", () => {
    const parsed = parseProxyLines("https://user:pass@a.example:8443");

    expect(parsed.lines).toEqual(["https://user:pass@a.example:8443"]);
    expect(parsed.problems).toEqual([]);
  });
});

describe("toProxyTableRow", () => {
  it("строка таблицы готовит адрес, прочерки и статус из is_alive", () => {
    const alive = toProxyTableRow({
      id: 1,
      label: " дефолт ",
      scheme: "http",
      host: "a.example",
      port: 8080,
      latency_ms: 120,
      is_alive: true,
      fail_count: 2,
      last_checked_at: 10.0,
      last_error: "timeout",
      assigned_browser_id: "br-1",
      usage_count: 5,
    });

    expect(alive).toEqual({
      id: 1,
      address: "a.example:8080",
      label: "дефолт",
      latency_ms: 120,
      status: "ok",
      fail_count: 2,
      last_error: "timeout",
      assigned_browser_id: "br-1",
      usage_count: 5,
    });

    const dead = toProxyTableRow({
      id: 2,
      label: null,
      scheme: "http",
      host: "b.example",
      port: 3128,
      latency_ms: null,
      is_alive: 0,
      fail_count: 0,
      last_checked_at: null,
      last_error: null,
      assigned_browser_id: null,
      usage_count: 0,
    });

    expect(dead.address).toBe("b.example:3128");
    expect(dead.label).toBe("—");
    expect(dead.assigned_browser_id).toBe("—");
    expect(dead.status).toBe("error");
    expect(dead.latency_ms).toBeNull();
  });

  it("view-model не тащит кредов, даже если демон их прислал", () => {
    const source = {
      id: 1,
      label: null,
      scheme: "http",
      host: "a.example",
      port: 8080,
      latency_ms: null,
      is_alive: true,
      fail_count: 0,
      last_checked_at: null,
      last_error: null,
      assigned_browser_id: null,
      usage_count: 0,
      username: "user",
      password: "secret",
    };
    const row = toProxyTableRow(source as unknown as ProxyRow);

    expect("password" in row).toBe(false);
    expect("username" in row).toBe(false);
    expect(JSON.stringify(row)).not.toContain("secret");
  });
});

describe("createProxiesApi: батчевое удаление", () => {
  it("ids уходят одним запросом, итог разбирается по контракту", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/delete": {
        status: 200,
        body: JSON.stringify({
          deleted: 2,
          skipped: 1,
          problems: ["id=3: назначен воркеру br-1"],
        }),
      },
    });
    const api = createProxiesApi(transport);

    const result = await api.removeMany([1, 2, 3]);

    expect(calls).toEqual([
      { path: "/control/proxies/delete", method: "POST", body: '{"ids":[1,2,3]}' },
    ]);
    expect(result).toEqual({
      deleted: 2,
      skipped: 1,
      problems: ["id=3: назначен воркеру br-1"],
    });
  });

  it("ответ без deleted/skipped/problems — ошибка, а не нули", async () => {
    const { transport } = transportOf({
      "/control/proxies/delete": { status: 200, body: '{"deleted":true}' },
    });
    const api = createProxiesApi(transport);

    await expect(api.removeMany([1])).rejects.toThrow(/deleted\/skipped\/problems/);
  });

  it("удаление всего пула: all уходит одним запросом, отчёт разбирается", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies/delete": {
        status: 200,
        body: JSON.stringify({
          deleted: 1900,
          skipped: 33,
          problems: ["id=7: назначен воркеру br-1"],
        }),
      },
    });
    const api = createProxiesApi(transport);

    const result = await api.removeAll();

    expect(calls).toEqual([
      { path: "/control/proxies/delete", method: "POST", body: '{"all":true}' },
    ]);
    expect(result).toEqual({
      deleted: 1900,
      skipped: 33,
      problems: ["id=7: назначен воркеру br-1"],
    });
  });

  it("409 на батче доходит текстом демона", async () => {
    const { transport } = transportOf({
      "/control/proxies/delete": {
        status: 400,
        body: errorBody("invalid_request", "поле ids должно быть непустым списком"),
      },
    });
    const api = createProxiesApi(transport);

    await expect(api.removeMany([])).rejects.toThrow("непустым списком");
  });
});
