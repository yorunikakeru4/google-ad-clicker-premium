import { describe, expect, it } from "vitest";
import {
  MAX_HOURLY_BUCKETS,
  alignCounts,
  buildHourlyBuckets,
  countByHour,
  hourBucket,
  hourLabel,
} from "./series";

describe("hourBucket", () => {
  it("округляет вниз до начала часа", () => {
    expect(hourBucket(0)).toBe(0);
    expect(hourBucket(3599.9)).toBe(0);
    expect(hourBucket(3600)).toBe(3600);
    expect(hourBucket(7201)).toBe(7200);
  });
});

describe("buildHourlyBuckets", () => {
  it("строит часы по возрастанию, включая неполный текущий", () => {
    expect(buildHourlyBuckets(0, 7201)).toEqual([0, 3600, 7200]);
    expect(buildHourlyBuckets(3600, 3600)).toEqual([3600]);
  });

  it("обратный диапазон — пусто, а не ошибка", () => {
    expect(buildHourlyBuckets(7200, 0)).toEqual([]);
    expect(buildHourlyBuckets(3600, 3599)).toEqual([]);
  });

  it("гигантское окно усекается до свежих часов, а не подвешивает цикл", () => {
    const buckets = buildHourlyBuckets(0, 3600 * 5000);

    expect(buckets).toHaveLength(MAX_HOURLY_BUCKETS);
    expect(buckets.at(-1)).toBe(3600 * 5000, "самый свежий час сохраняется");
  });
});

describe("countByHour", () => {
  it("раскладывает строки по часам с нулевыми пропусками", () => {
    const buckets = [0, 3600, 7200];
    const rows = [
      { ts: 0 }, // граница часа — в первый бакет
      { ts: 3599.999 }, // последняя секунда часа — тоже в первый
      { ts: 3600 }, // начало второго часа
      { ts: 5000 },
      { ts: 5000 },
    ];

    expect(countByHour(rows, buckets)).toEqual([2, 3, 0]);
  });

  it("строки вне окна не попадают в счётчики", () => {
    const buckets = [3600];

    expect(countByHour([{ ts: 1 }, { ts: 7200 }], buckets)).toEqual([0]);
  });

  it("пустой список строк — нули по всем часам", () => {
    expect(countByHour([], [0, 3600])).toEqual([0, 0]);
  });
});

describe("alignCounts", () => {
  it("выравнивает разреженные бакеты (как clicks_per_hour) по окну", () => {
    const buckets = [0, 3600, 7200, 10800];
    const points = [
      { bucket: 3600, count: 5 },
      { bucket: 10800, count: 2 },
      { bucket: 999_999, count: 7 }, // чужой час — игнорируется
    ];

    expect(alignCounts(points, buckets)).toEqual([0, 5, 0, 2]);
  });

  it("дубли бакетов суммируются", () => {
    expect(
      alignCounts(
        [
          { bucket: 0, count: 1 },
          { bucket: 0, count: 2 },
        ],
        [0],
      ),
    ).toEqual([3]);
  });
});

describe("hourLabel", () => {
  it("метка бакета — локальные HH:MM", () => {
    expect(hourLabel(new Date(2026, 0, 1, 5).getTime() / 1000)).toBe("05:00");
    expect(hourLabel(new Date(2026, 11, 31, 23).getTime() / 1000)).toBe("23:00");
    expect(hourLabel(new Date(2026, 0, 1, 0).getTime() / 1000)).toBe("00:00");
  });

  it("минуты не отбрасываются: в поясе с получасовым сдвигом метка честная", () => {
    const at = new Date(2026, 8, 29, 7, 5, 3).getTime() / 1000;
    expect(hourLabel(at)).toBe("07:05");
  });
});
