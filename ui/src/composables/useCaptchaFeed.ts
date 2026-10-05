// Лента CAPTCHA на Dashboard (план §5, фаза 8): опрос событий за сутки,
// всплывающее уведомление о новом событии и производная подсветка воркеров.
//
// Опрос — паттерн useProxies/useDiagnostics: тик по интервалу, дедупликация
// параллельных тиков, тики без открытой БД — no-op. Лента читает
// `list_captcha_events` одной читалки, подсветка воркеров — производная от
// уже загруженных событий, отдельного запроса по воркерам нет.
//
// Уведомления: первый успешный тик — базовая линия (история не сыплет
// всплывашками при открытии экрана), дальше каждое событие с ещё не
// показанным id попадает в очередь ровно один раз — повторные тики тех же
// строк ничего не добавляют.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import {
  CAPTCHA_FEED_LIMIT,
  inFeedWindow,
  openScreenshot,
  toNotice,
  unsolvedBrowserIds,
  type CaptchaEvent,
  type CaptchaNotice,
  type ScreenshotOpener,
} from "../lib/captcha";
import { dbApi, dbErrorMessage, type DbApi } from "../lib/dbApi";
import { useDb } from "./useDb";

/** Интервал опроса ленты: 4 с — свежесть уведомлений без ДДОСа БД. */
export const CAPTCHA_FEED_POLL_MS = 4000;

export interface CreateCaptchaFeedOptions {
  /** БД открыта: вне этого состояния тики — no-op, а не ошибка чтения. */
  isReady?: () => boolean;
  /** Интервал опроса; по умолчанию [`CAPTCHA_FEED_POLL_MS`]. */
  pollMs?: number;
  /** Часы для окна подсветки; по умолчанию `Date.now`. */
  now?: () => number;
  /** Открыватель скриншота; по умолчанию opener-плагин Tauri. */
  opener?: ScreenshotOpener;
}

export interface CaptchaFeedState {
  /** Последние события из читалки — строки карточки «CAPTCHA». */
  events: Ref<CaptchaEvent[]>;
  /** Очередь непросмотренных уведомлений, старые — впереди. */
  notices: Ref<CaptchaNotice[]>;
  /** Текущее уведомление для всплывашки; null — показывать нечего. */
  notice: ComputedRef<CaptchaNotice | null>;
  /** Воркеры с нерешённой капчей в окне 30 минут — подсветка таблицы. */
  alertIds: ComputedRef<string[]>;
  /** Первая загрузка: скелетон, а не мигание карточки на каждом тике. */
  loading: Ref<boolean>;
  /** Ошибка чтения; текст чистится первым успешным тиком. */
  error: Ref<string | null>;
  /** Ошибка открытия скриншота (нет пути, нет программы по умолчанию). */
  screenshotError: Ref<string | null>;
  /** Первый тик + интервал; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик ленты; параллельный тик возвращает тот же промис. */
  tick(): Promise<void>;
  /** Снять текущее уведомление, показать следующее из очереди. */
  dismiss(): void;
  /** Открыть скриншот события по его пути. */
  openShot(path: string | null): Promise<void>;
}

export function createCaptchaFeed(
  api: Pick<DbApi, "listCaptchaEvents"> = dbApi,
  options: CreateCaptchaFeedOptions = {},
): CaptchaFeedState {
  const isReady = options.isReady ?? (() => true);
  const pollMs = options.pollMs ?? CAPTCHA_FEED_POLL_MS;
  const now = options.now ?? (() => Date.now());

  const events = ref<CaptchaEvent[]>([]);
  const notices = ref<CaptchaNotice[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const screenshotError = ref<string | null>(null);

  /** id уже показанных (или отмеченных базовой линией) событий. */
  const seen = new Set<number>();
  /** Первый успешный тик прошёл: история больше не считается «новой». */
  let baselined = false;

  let timer: ReturnType<typeof setInterval> | null = null;
  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;
  /** Экран хочет опрос: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Лента уже хотя бы раз читалась: скелетон больше не нужен. */
  let loaded = false;

  /** Разбор выборки: окно суток, базовая линия либо очередь новых уведомлений. */
  function accept(rows: CaptchaEvent[]): void {
    // Окно режется здесь, а не в БД: читалка отдаёт просто последние N
    // строк, и без этой проверки лента показывала бы события прошлых дней
    // как «последние» — Dashboard живёт в окне суток.
    const visible = rows.filter((row) => inFeedWindow(row.ts, now() / 1000));
    events.value = visible;

    if (!baselined) {
      for (const row of visible) seen.add(row.id);
      baselined = true;
      return;
    }

    const fresh = visible.filter((row) => !seen.has(row.id));
    for (const row of visible) seen.add(row.id);
    if (fresh.length > 0) {
      notices.value = [...notices.value, ...fresh.map(toNotice)];
    }
  }

  async function runLoad(): Promise<void> {
    if (!isReady()) return;
    if (!loaded) loading.value = true;
    try {
      const rows = await api.listCaptchaEvents(CAPTCHA_FEED_LIMIT);
      accept(rows);
      error.value = null;
    } catch (caught) {
      error.value = dbErrorMessage(caught);
    } finally {
      loaded = true;
      loading.value = false;
    }
  }

  function tick(): Promise<void> {
    if (inFlight !== null) return inFlight;
    // Отвязка guard'а — здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя, и guard
    // навсегда остался бы «в полёте», зажав опрос.
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  async function start(): Promise<void> {
    desired = true;
    if (timer !== null) return;
    await tick();
    if (!desired || timer !== null) return; // stop/повторный start за время тика
    timer = setInterval(() => void tick(), pollMs);
  }

  function stop(): void {
    desired = false;
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  function dismiss(): void {
    if (notices.value.length === 0) return;
    notices.value = notices.value.slice(1);
  }

  async function openShot(path: string | null): Promise<void> {
    screenshotError.value = await openScreenshot(path, options.opener);
  }

  const notice = computed<CaptchaNotice | null>(
    () => notices.value[0] ?? null,
  );

  const alertIds = computed<string[]>(() =>
    unsolvedBrowserIds(events.value, now() / 1000),
  );

  return {
    events,
    notices,
    notice,
    alertIds,
    loading,
    error,
    screenshotError,
    start,
    stop,
    tick,
    dismiss,
    openShot,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: CaptchaFeedState | null = null;

export function useCaptchaFeed(): CaptchaFeedState {
  shared ??= createCaptchaFeed(dbApi, {
    isReady: () => useDb().phase.value === "open",
    pollMs: CAPTCHA_FEED_POLL_MS,
  });
  return shared;
}
