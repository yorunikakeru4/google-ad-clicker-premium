// Контракт API профилей: пути, тела запросов, разбор ответов демона и
// клиентские помощники экрана (view-model строки, фильтр, разбор импорта и
// валидация диапазона).
//
// Транспорт подменяется так же, как в proxies.test.ts: тесты проверяют
// именно то, что уходит в control_request, и то, что остаётся в UI. Кредов
// в данных профилей нет по построению — объект строки собирается поле в поле.

import { describe, expect, it } from "vitest";
import {
  checkAssignRange,
  createProfilesApi,
  filterProfiles,
  parseProfileFields,
  parseProfileImportLines,
  parseProfileId,
  profileApiError,
  toProfileTableRow,
  type ProfileRow,
} from "./profiles";
import type { WritableProfileStatus } from "./control";
import type { ProxyRow } from "./proxies";
import type { Transport } from "./daemonApi";

const PROFILE_JSON = {
  id: 3,
  name: "аккаунт-3",
  key_ref: "hooks.KEY_3",
  proxy_id: 7,
  user_agent: "Mozilla/5.0",
  locale: "de-DE",
  timezone: "Europe/Berlin",
  status: "assigned",
  last_used_at: 1_760_000_000.0,
  fields: '{"note":"осторожно"}',
  assigned_browser_id: "br-1",
};

function transportOf(
  replies: Record<string, { status: number; body: string }>,
): {
  transport: Transport;
  calls: { path: string; method: string; body?: string }[];
} {
  const calls: { path: string; method: string; body?: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({
      path: request.path,
      method: request.method,
      body: request.body,
    });
    const reply = replies[request.path];
    if (!reply) throw new Error(`нет заглушки для ${request.path}`);
    return reply;
  };
  return { transport, calls };
}

function errorBody(code: string, message?: string): string {
  return JSON.stringify({ error: { code, message } });
}

/** Убирает ключ fields: тест проверяет ответ демона, где поля вообще нет. */
function withoutFields(
  source: typeof PROFILE_JSON,
): Omit<typeof PROFILE_JSON, "fields"> {
  const copy: Partial<typeof PROFILE_JSON> = { ...source };
  delete copy.fields;
  return copy as Omit<typeof PROFILE_JSON, "fields">;
}

/** Строка списка: контрактные поля по умолчанию, сверху — только нужные тесту. */
function profileStub(overrides: Partial<ProfileRow> & { id: number }): ProfileRow {
  return {
    name: `profile-${overrides.id}`,
    key_ref: null,
    proxy_id: null,
    user_agent: null,
    locale: null,
    timezone: null,
    status: "free",
    last_used_at: null,
    fields: null,
    assigned_browser_id: null,
    ...overrides,
  };
}

describe("createProfilesApi: список", () => {
  it("GET /control/profiles без тела, строки разбираются по контракту", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles": { status: 200, body: JSON.stringify({ profiles: [PROFILE_JSON] }) },
    });
    const api = createProfilesApi(transport);

    const rows = await api.list();

    expect(calls).toEqual([
      { path: "/control/profiles", method: "GET", body: undefined },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe(3);
    expect(rows[0].name).toBe("аккаунт-3");
    expect(rows[0].key_ref).toBe("hooks.KEY_3");
    expect(rows[0].status).toBe("assigned");
    expect(rows[0].assigned_browser_id).toBe("br-1");
    expect(rows[0].last_used_at).toBe(1_760_000_000.0);
  });

  it("лишние ключи демона (креды прокси) не попадают в данные UI", async () => {
    const { transport } = transportOf({
      "/control/proxies": { status: 200, body: '{"proxies":[]}' },
      "/control/profiles": {
        status: 200,
        body: JSON.stringify({
          profiles: [
            {
              ...PROFILE_JSON,
              username: "user",
              password: "secret-password",
            },
          ],
        }),
      },
    });
    const api = createProfilesApi(transport);

    const [row] = await api.list();

    expect(Object.keys(row)).not.toContain("username");
    expect(Object.keys(row)).not.toContain("password");
    expect(JSON.stringify(row)).not.toContain("secret-password");
  });

  it("fields: строка проходит как есть, объект сериализуется, отсутствие → null", async () => {
    const { transport } = transportOf({
      "/control/profiles": {
        status: 200,
        body: JSON.stringify({
          profiles: [
            { ...PROFILE_JSON, id: 1, fields: '{"raw":true}' },
            { ...PROFILE_JSON, id: 2, fields: { parsed: "объектом" } },
            { ...PROFILE_JSON, id: 3, fields: null },
            { ...withoutFields(PROFILE_JSON), id: 4 },
          ],
        }),
      },
    });
    const api = createProfilesApi(transport);

    const rows = await api.list();

    expect(rows[0].fields).toBe('{"raw":true}');
    expect(rows[1].fields).toBe('{"parsed":"объектом"}');
    expect(rows[2].fields).toBeNull();
    expect(rows[3].fields).toBeNull();
  });

  it("ответ без поля profiles — читаемая ошибка, а не тихий пустой список", async () => {
    const { transport } = transportOf({
      "/control/profiles": { status: 200, body: '{"ok":true}' },
    });
    const api = createProfilesApi(transport);

    await expect(api.list()).rejects.toThrow(/без списка профилей/);
  });

  it("нестрока профиля — читаемая ошибка, а не строка с undefined", async () => {
    const { transport } = transportOf({
      "/control/profiles": {
        status: 200,
        body: JSON.stringify({ profiles: [{ key_ref: "без id и имени" }] }),
      },
    });
    const api = createProfilesApi(transport);

    await expect(api.list()).rejects.toThrow(/неполная строка профиля/);
  });

  it("обрыв транспорта доходит до вызывающего", async () => {
    const transport: Transport = async () => {
      throw new Error("нет соединения с демоном на 127.0.0.1:8787");
    };
    const api = createProfilesApi(transport);

    await expect(api.list()).rejects.toThrow("нет соединения");
  });

  it("listProxies бьёт в GET /control/proxies — join прокси идёт из того же демона", async () => {
    const { transport, calls } = transportOf({
      "/control/proxies": {
        status: 200,
        body: JSON.stringify({
          proxies: [
            {
              id: 7,
              label: "немецкий",
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
            },
          ],
        }),
      },
    });
    const api = createProfilesApi(transport);

    const proxies = await api.listProxies();

    expect(calls).toEqual([
      { path: "/control/proxies", method: "GET", body: undefined },
    ]);
    expect(proxies).toHaveLength(1);
    expect(proxies[0].host).toBe("a.example");
  });
});

describe("createProfilesApi: добавление и импорт", () => {
  it("add шлёт профили как есть в POST /control/profiles", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles": {
        status: 200,
        body: JSON.stringify({ added: 2, skipped: 1, problems: ["дубликат"] }),
      },
    });
    const api = createProfilesApi(transport);

    const result = await api.add([
      { name: "alpha", key_ref: "k-1" },
      { name: "beta", proxy_id: 7, locale: "de-DE" },
    ]);

    expect(calls).toEqual([
      {
        path: "/control/profiles",
        method: "POST",
        body: JSON.stringify({
          profiles: [
            { name: "alpha", key_ref: "k-1" },
            { name: "beta", proxy_id: 7, locale: "de-DE" },
          ],
        }),
      },
    ]);
    expect(result).toEqual({ added: 2, skipped: 1, problems: ["дубликат"] });
  });

  it("import шлёт lines в /control/profiles/import", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles/import": {
        status: 200,
        body: JSON.stringify({ added: 5, skipped: 2, problems: ["строка 4"] }),
      },
    });
    const api = createProfilesApi(transport);

    const result = await api.importLines(["k-1", "k-2"]);

    expect(calls).toEqual([
      {
        path: "/control/profiles/import",
        method: "POST",
        body: JSON.stringify({ lines: ["k-1", "k-2"] }),
      },
    ]);
    expect(result).toEqual({ added: 5, skipped: 2, problems: ["строка 4"] });
  });

  it("ответ без added/skipped/problems — ошибка, а не нули", async () => {
    const { transport } = transportOf({
      "/control/profiles": { status: 200, body: '{"ok":true}' },
    });
    const api = createProfilesApi(transport);

    await expect(api.add([{ name: "alpha" }])).rejects.toThrow(/added/);
  });
});

describe("createProfilesApi: удаление, назначение и статус", () => {
  it("delete шлёт id и возвращает deleted", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles/delete": { status: 200, body: '{"deleted":1}' },
    });
    const api = createProfilesApi(transport);

    const deleted = await api.remove(7);

    expect(calls).toEqual([
      { path: "/control/profiles/delete", method: "POST", body: '{"id":7}' },
    ]);
    expect(deleted).toBe(1);
  });

  it("409 profile_in_use с сообщением демона → его текст", async () => {
    const { transport } = transportOf({
      "/control/profiles/delete": {
        status: 409,
        body: errorBody("profile_in_use", "профиль закреплён за br-1"),
      },
    });
    const api = createProfilesApi(transport);

    await expect(api.remove(7)).rejects.toThrow("профиль закреплён за br-1");
  });

  it("409 profile_in_use без сообщения → читаемый текст про поток", async () => {
    const { transport } = transportOf({
      "/control/profiles/delete": {
        status: 409,
        body: errorBody("profile_in_use"),
      },
    });
    const api = createProfilesApi(transport);

    await expect(api.remove(7)).rejects.toThrow(/назначен потоку/);
  });

  it("assign шлёт start_id/end_id и возвращает assigned/available", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles/assign": {
        status: 200,
        body: JSON.stringify({ assigned: 3, available: 7 }),
      },
    });
    const api = createProfilesApi(transport);

    const result = await api.assign(1, 10);

    expect(calls).toEqual([
      {
        path: "/control/profiles/assign",
        method: "POST",
        body: '{"start_id":1,"end_id":10}',
      },
    ]);
    expect(result).toEqual({ assigned: 3, available: 7 });
  });

  it("assign: 400 без сообщения → локальный текст, с сообщением → текст демона", async () => {
    const { transport } = transportOf({
      "/control/profiles/assign": {
        status: 400,
        body: errorBody("invalid_request"),
      },
    });
    const api = createProfilesApi(transport);

    await expect(api.assign(10, 1)).rejects.toThrow(/start_id и end_id/);

    const chatty = createProfilesApi(
      async () => ({
        status: 400,
        body: errorBody("invalid_request", "start_id больше end_id"),
      }),
    );
    await expect(chatty.assign(10, 1)).rejects.toThrow("start_id больше end_id");
  });

  it("unassign шлёт {} и возвращает released", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles/unassign": { status: 200, body: '{"released":5}' },
    });
    const api = createProfilesApi(transport);

    const result = await api.unassign();

    expect(calls).toEqual([
      { path: "/control/profiles/unassign", method: "POST", body: "{}" },
    ]);
    expect(result).toEqual({ released: 5 });
  });

  it("status шлёт id/status и не требует тела ответа", async () => {
    const { transport, calls } = transportOf({
      "/control/profiles/status": { status: 200, body: "{}" },
    });
    const api = createProfilesApi(transport);

    await expect(api.setStatus(3, "blocked")).resolves.toBeUndefined();

    expect(calls).toEqual([
      {
        path: "/control/profiles/status",
        method: "POST",
        body: '{"id":3,"status":"blocked"}',
      },
    ]);
  });

  it("status: 400 без сообщения → локальный текст про допустимые статусы", async () => {
    const { transport } = transportOf({
      "/control/profiles/status": {
        status: 400,
        body: errorBody("invalid_request"),
      },
    });
    const api = createProfilesApi(transport);

    // "active" недопустим контрактом: отправляем осознанно, чтобы поймать 400.
    await expect(
      api.setStatus(3, "active" as WritableProfileStatus),
    ).rejects.toThrow(/free, blocked, error/);
  });

  it("не-JSON тело ошибки — код и начало тела, а не [object Object]", async () => {
    const { transport } = transportOf({
      "/control/profiles/status": { status: 502, body: "<html>bad gateway</html>" },
    });
    const api = createProfilesApi(transport);

    await expect(api.setStatus(3, "free")).rejects.toThrow(/HTTP 502/);
  });
});

describe("profileApiError", () => {
  it("известный 409 profile_in_use без message берёт локальный текст", () => {
    expect(profileApiError(409, errorBody("profile_in_use"))).toMatch(
      /назначен потоку/,
    );
  });

  it("message демона сильнее локального текста", () => {
    expect(
      profileApiError(409, errorBody("profile_in_use", "br-3 держит профиль")),
    ).toBe("br-3 держит профиль");
  });

  it("400 без message берёт переданный fallback, иначе apiErrorMessage", () => {
    expect(profileApiError(400, errorBody("invalid_request"), "свой текст")).toBe(
      "свой текст",
    );
    expect(profileApiError(400, errorBody("invalid_request"))).toContain("HTTP 400");
    expect(profileApiError(500, "")).toBe("HTTP 500");
  });

  it("неизвестный код 409 идёт через apiErrorMessage, а не молчит", () => {
    expect(profileApiError(409, errorBody("other_conflict", "другой конфликт"))).toBe(
      "другой конфликт",
    );
    expect(profileApiError(409, errorBody("other_conflict"))).toContain(
      "other_conflict",
    );
  });
});

describe("parseProfileImportLines", () => {
  it("пустые строки, комментарии и пробелы отбрасываются, key_ref проходят", () => {
    const lines = parseProfileImportLines(
      "  hooks.KEY_1 \r\n\n# комментарий\nhooks.KEY_2\t\n",
    );

    expect(lines).toEqual(["hooks.KEY_1", "hooks.KEY_2"]);
  });

  it("пустой текст — пустой список, а не ошибка", () => {
    expect(parseProfileImportLines("")).toEqual([]);
    expect(parseProfileImportLines("   \n  ")).toEqual([]);
  });
});

describe("parseProfileId и checkAssignRange", () => {
  it("parseProfileId принимает целые от 1 и отвергает остальное", () => {
    expect(parseProfileId("7")).toBe(7);
    expect(parseProfileId(" 12 ")).toBe(12);
    expect(parseProfileId("")).toBeNull();
    expect(parseProfileId("0")).toBeNull();
    expect(parseProfileId("-3")).toBeNull();
    expect(parseProfileId("3.5")).toBeNull();
    expect(parseProfileId("abc")).toBeNull();
    expect(parseProfileId("999999999999999999999")).toBeNull();
  });

  it("checkAssignRange: валидный диапазон возвращает числа", () => {
    expect(checkAssignRange("5", "9")).toEqual({ ok: true, start: 5, end: 9 });
    expect(checkAssignRange("4", "4")).toEqual({ ok: true, start: 4, end: 4 });
  });

  it("checkAssignRange: нечисла и пустое — одна читаемая причина", () => {
    const notNumbers = checkAssignRange("abc", "9");
    expect(notNumbers.ok).toBe(false);
    if (!notNumbers.ok) expect(notNumbers.error).toMatch(/целые числа/);

    const empty = checkAssignRange("", "");
    expect(empty.ok).toBe(false);
    if (!empty.ok) expect(empty.error).toMatch(/целые числа/);
  });

  it("checkAssignRange: start больше end — отдельная причина", () => {
    const reversed = checkAssignRange("10", "1");
    expect(reversed.ok).toBe(false);
    if (!reversed.ok) expect(reversed.error).toMatch(/start_id/);
  });
});

describe("parseProfileFields", () => {
  it("валидный JSON-объект разбирается, битый — null", () => {
    expect(parseProfileFields('{"note":"x","n":2}')).toEqual({ note: "x", n: 2 });
    expect(parseProfileFields("не json")).toBeNull();
    expect(parseProfileFields("[1,2]")).toBeNull();
    expect(parseProfileFields(null)).toBeNull();
    expect(parseProfileFields("")).toBeNull();
  });
});

describe("toProfileTableRow", () => {
  const proxiesById = (): Map<number, ProxyRow> =>
    new Map([
      [
        7,
        {
          id: 7,
          label: "немецкий",
          scheme: "http",
          host: "a.example",
          port: 8080,
          country: null,
          latency_ms: null,
          is_alive: true,
          fail_count: 0,
          last_checked_at: null,
          last_error: null,
          assigned_browser_id: null,
          usage_count: 0,
        },
      ],
      [
        8,
        {
          id: 8,
          label: null,
          scheme: "http",
          host: "b.example",
          port: 3128,
          country: null,
          latency_ms: null,
          is_alive: true,
          fail_count: 0,
          last_checked_at: null,
          last_error: null,
          assigned_browser_id: null,
          usage_count: 0,
        },
      ],
    ]);

  const base: ProfileRow = {
    id: 1,
    name: "alpha",
    key_ref: null,
    proxy_id: null,
    user_agent: null,
    locale: null,
    timezone: null,
    status: "free",
    last_used_at: null,
    fields: null,
    assigned_browser_id: null,
  };

  it("прокси: метка важнее адреса, без метки — host:port, без прокси — прочерк", () => {
    expect(toProfileTableRow({ ...base, proxy_id: 7 }, proxiesById()).proxy).toBe(
      "немецкий",
    );
    expect(toProfileTableRow({ ...base, proxy_id: 8 }, proxiesById()).proxy).toBe(
      "b.example:3128",
    );
    expect(toProfileTableRow(base, proxiesById()).proxy).toBe("—");
  });

  it("прокси есть в профиле, но пропал из списка — адрес остаётся честным", () => {
    expect(toProfileTableRow({ ...base, proxy_id: 99 }, proxiesById()).proxy).toBe(
      "#99",
    );
  });

  it("null'ы превращаются в прочерки, статус и last_used_at остаются как есть", () => {
    const row = toProfileTableRow(
      { ...base, status: "blocked", last_used_at: 1_760_000_000.0 },
      proxiesById(),
    );

    expect(row.key_ref).toBe("—");
    expect(row.user_agent).toBe("—");
    expect(row.assigned_browser_id).toBe("—");
    expect(row.status).toBe("blocked");
    expect(row.last_used_at).toBe(1_760_000_000.0);
    expect(row.name).toBe("alpha");
  });

  it("key_ref и name проходят текстом — это ссылка, а не пароль", () => {
    const row = toProfileTableRow(
      { ...base, key_ref: "hooks.KEY_9" },
      proxiesById(),
    );

    expect(row.key_ref).toBe("hooks.KEY_9");
    expect(row.name).toBe("alpha");
  });
});

describe("filterProfiles", () => {
  const rows: ProfileRow[] = [
    profileStub({ id: 1, name: "Alpha", status: "free" }),
    profileStub({ id: 2, name: "бета", status: "assigned" }),
    profileStub({ id: 3, name: "Гамма-2", status: "blocked" }),
  ];

  it("пустые фильтры возвращают весь список", () => {
    expect(filterProfiles(rows, { status: "", name: "" })).toHaveLength(3);
  });

  it("фильтр по статусу — точное совпадение", () => {
    const filtered = filterProfiles(rows, { status: "assigned", name: "" });

    expect(filtered.map((row) => row.id)).toEqual([2]);
  });

  it("поиск по name — подстрока без учёта регистра", () => {
    expect(
      filterProfiles(rows, { status: "", name: "ALP" }).map((row) => row.id),
    ).toEqual([1]);
    expect(
      filterProfiles(rows, { status: "", name: "гам" }).map((row) => row.id),
    ).toEqual([3]);
  });

  it("оба фильтра действуют вместе", () => {
    expect(filterProfiles(rows, { status: "free", name: "alpha" })).toHaveLength(1);
    expect(filterProfiles(rows, { status: "blocked", name: "alpha" })).toHaveLength(0);
  });
});
