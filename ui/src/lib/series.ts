// Часовые ряды дашборда: сетка часов ровно та же, что у `clicks_per_hour`
// в БД — `floor(ts / 3600) * 3600` (unix-час, при границах UTC выровненной
// сетки). Метки переводятся в локальный пояс при выводе.

const HOUR_SECONDS = 3600;

/**
 * Потолок окна: даже мусорный диапазон не должен крутить цикл дольше, чем
 * дождётся ответа UI. ~41 день часовых бакетов — любой реальный дашборд
 * укладывается.
 */
export const MAX_HOURLY_BUCKETS = 1000;

/** Начало часа, содержащего `ts`. */
export function hourBucket(ts: number): number {
  return Math.floor(ts / HOUR_SECONDS) * HOUR_SECONDS;
}

/**
 * Часовые бакеты от `from` до `to` включительно, по возрастанию.
 * Обратный диапазон — пусто. Окно длиннее [MAX_HOURLY_BUCKETS] усекается
 * до самых свежих часов.
 */
export function buildHourlyBuckets(from: number, to: number): number[] {
  let first = hourBucket(from);
  const last = hourBucket(to);
  if (last < first) return [];

  const needed = Math.floor((last - first) / HOUR_SECONDS) + 1;
  if (needed > MAX_HOURLY_BUCKETS) {
    first = last - (MAX_HOURLY_BUCKETS - 1) * HOUR_SECONDS;
  }

  const buckets: number[] = [];
  for (let bucket = first; bucket <= last; bucket += HOUR_SECONDS) {
    buckets.push(bucket);
  }
  return buckets;
}

function zerosAlignedWith(buckets: readonly number[]): {
  counts: number[];
  index: Map<number, number>;
} {
  const index = new Map<number, number>();
  buckets.forEach((bucket, at) => index.set(bucket, at));
  return { counts: new Array<number>(buckets.length).fill(0), index };
}

/** Счёт событий по часам: пропуски — нули, чужие часы игнорируются. */
export function countByHour(
  rows: readonly { ts: number }[],
  buckets: readonly number[],
): number[] {
  const { counts, index } = zerosAlignedWith(buckets);
  for (const row of rows) {
    const at = index.get(hourBucket(row.ts));
    if (at !== undefined) counts[at] += 1;
  }
  return counts;
}

/** Выравнивание разреженных бакетов (`clicks_per_hour`) по окну графика. */
export function alignCounts(
  points: readonly { bucket: number; count: number }[],
  buckets: readonly number[],
): number[] {
  const { counts, index } = zerosAlignedWith(buckets);
  for (const point of points) {
    const at = index.get(point.bucket);
    if (at !== undefined) counts[at] += point.count;
  }
  return counts;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Метка бакета: локальные `HH:MM` момента. Бакет приходит уже выровненный;
 * в поясе с получасовым сдвигом метка честно покажет и минуты.
 */
export function hourLabel(bucket: number): string {
  const date = new Date(bucket * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
