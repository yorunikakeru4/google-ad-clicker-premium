import { describe, expect, it } from "vitest";
import { CSV_COLUMNS, csvCell, logsToCsv, type CsvLogRow } from "./csv";

function row(partial: Partial<CsvLogRow> = {}): CsvLogRow {
  return {
    ts: 1_760_000_000,
    level: "INFO",
    browser_id: null,
    category: null,
    message: "строка",
    fields: null,
    ...partial,
  };
}

describe("csvCell", () => {
  it("простые значения идут без кавычек", () => {
    expect(csvCell("INFO")).toBe("INFO");
    expect(csvCell("клик по ссылке")).toBe("клик по ссылке");
    expect(csvCell(42)).toBe("42");
  });

  it("null и undefined — пустая ячейка, а не «null»", () => {
    expect(csvCell(null)).toBe("");
    expect(csvCell(undefined)).toBe("");
  });

  it("запятая требует кавычек", () => {
    expect(csvCell("a,b")).toBe('"a,b"');
  });

  it("кавычка внутри удваивается и берётся в кавычки", () => {
    expect(csvCell('сказал "привет"')).toBe('"сказал ""привет"""');
  });

  it("перенос строки и возврат каретки требуют кавычек", () => {
    expect(csvCell("первая\nвторая")).toBe('"первая\nвторая"');
    expect(csvCell("первая\rвторая")).toBe('"первая\rвторая"');
  });

  it("смесь опасных символов экранируется один раз", () => {
    expect(csvCell('итог: 1, "да"\nконец')).toBe('"итог: 1, ""да""\nконец"');
  });
});

describe("logsToCsv", () => {
  it("первая строка — заголовок в порядке колонок", () => {
    const csv = logsToCsv([]);
    expect(csv).toBe(CSV_COLUMNS.join(","));
  });

  it("каждая запись — строка, колонки в порядке CSV_COLUMNS", () => {
    const csv = logsToCsv([
      row({ ts: 1.5, level: "ERROR", browser_id: "b1", category: "click" }),
      row({ ts: 2.5, level: "INFO", message: "вторая" }),
    ]);
    const lines = csv.split("\n");

    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe("ts,level,browser_id,category,message,fields");
    expect(lines[1]).toBe('1.5,ERROR,b1,click,строка,');
    expect(lines[2]).toBe("2.5,INFO,,,вторая,");
  });

  it("перенос в сообщении остаётся внутри кавычек и не создаёт новую запись", () => {
    const csv = logsToCsv([row({ message: "часть 1\nчасть 2" })]);

    // Весь CSV — заголовок и ровно одна запись: перенос экранирован кавычками.
    expect(csv).toBe(
      'ts,level,browser_id,category,message,fields\n' +
        '1760000000,INFO,,,"часть 1\nчасть 2",',
    );
  });

  it("JSON в fields проходит как есть, экранируясь по правилам CSV", () => {
    const csv = logsToCsv([
      row({ message: "", fields: '{"query": "купить, кроссовки"}' }),
    ]);

    expect(csv.split("\n")[1]).toBe(
      '1760000000,INFO,,,,"{""query"": ""купить, кроссовки""}"',
    );
  });

  it("пустой список даёт только заголовок", () => {
    expect(logsToCsv([]).split("\n")).toHaveLength(1);
  });
});
