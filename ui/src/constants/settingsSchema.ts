export type SettingType = "bool" | "int" | "float" | "string" | "enum" | "path";

export interface SettingOption {
  title: string;
  /** Число (multiprocess_style) или строка (proxy_transport) — как в движке. */
  value: number | string;
}

export interface SettingFieldDef {
  key: string;
  type: SettingType;
  hint: string;
  default: string | number | boolean;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  options?: SettingOption[];
  restart?: boolean;
  /** Секрет: движок отдаёт маску ********, в патч без правок не попадает. */
  secret?: boolean;
}

export interface SettingSection {
  key: "paths" | "webdriver" | "behavior";
  title: string;
  fields: SettingFieldDef[];
}

// Форма Settings (план §5, фаза 2). Состав секций, порядок ключей, типы и
// дефолты — точная копия engine/control_plane/config.py::_SCHEMA, границы —
// из _numeric_limits, секреты — из _SECRET_FIELDS. Держит их в согласии
// тест settingsSchema.test.ts: правка _SCHEMA требует правки этой схемы и
// фикстуры в тесте.
export const settingsSections: SettingSection[] = [
  {
    key: "paths",
    title: "Файлы и пути",
    fields: [
      {
        key: "query_file",
        type: "path",
        hint: "Файл с поисковыми запросами, по одному в строке. Пусто — запрос берётся из behavior.query",
        default: "",
      },
      {
        key: "proxy_file",
        type: "path",
        hint: "Файл со списком прокси, по одному в строке. Пусто — используется webdriver.proxy",
        default: "",
      },
      {
        key: "user_agents",
        type: "path",
        hint: "Файл со списком User-Agent, по одному в строке",
        default: "user_agents.txt",
      },
      {
        key: "filtered_domains",
        type: "path",
        hint: "Файл с доменами, результаты которых исключаются из выдачи",
        default: "domains.txt",
      },
    ],
  },
  {
    key: "webdriver",
    title: "Браузер и прокси",
    fields: [
      {
        key: "proxy",
        type: "string",
        hint: "Один прокси в формате scheme://host:port. Взаимоисключимо с paths.proxy_file",
        default: "",
        secret: true,
      },
      {
        key: "auth",
        type: "bool",
        hint: "Прокси требует авторизацию: учётные данные передаются через DevTools",
        default: true,
      },
      {
        key: "incognito",
        type: "bool",
        hint: "Запускать браузер в режиме инкогнито",
        default: false,
      },
      {
        key: "country_domain",
        type: "bool",
        hint: "Подбирать домен поиска по стране прокси",
        default: false,
      },
      {
        key: "language_from_proxy",
        type: "bool",
        hint: "Определять язык страницы по геолокации прокси",
        default: true,
      },
      {
        key: "ss_on_exception",
        type: "bool",
        hint: "Делать скриншот при исключении в сценарии",
        default: false,
      },
      {
        key: "window_size",
        type: "string",
        hint: "Размер окна браузера, например 1280x800. Пусто — размер по умолчанию",
        default: "",
      },
      {
        key: "shift_windows",
        type: "bool",
        hint: "Сдвигать окна при нескольких потоках, чтобы не перекрывались",
        default: false,
      },
      {
        key: "use_seleniumbase",
        type: "bool",
        hint: "Запуск через SeleniumBase вместо undetected-chromedriver",
        default: false,
      },
      {
        key: "proxy_transport",
        type: "enum",
        options: [
          { title: "CDP-авторизация (по умолчанию)", value: "cdp_auth" },
          { title: "Расширение браузера MV3", value: "extension" },
          { title: "Прямое подключение, whitelist IP", value: "direct" },
        ],
        hint: "Как креды прокси доходят до Chrome: через DevTools, через расширение или без кредов вовсе",
        default: "cdp_auth",
      },
    ],
  },
  {
    key: "behavior",
    title: "Поведение сценария",
    fields: [
      {
        key: "query",
        type: "string",
        hint: "Один поисковый запрос. Взаимоисключимо с paths.query_file",
        default: "",
      },
      {
        key: "ad_page_min_wait",
        type: "int",
        unit: "сек",
        min: 0,
        max: 3600,
        hint: "Нижняя граница случайной паузы на странице с рекламой, 0–3600 сек",
        default: 10,
      },
      {
        key: "ad_page_max_wait",
        type: "int",
        unit: "сек",
        min: 0,
        max: 3600,
        hint: "Верхняя граница случайной паузы на странице с рекламой, 0–3600 сек, не меньше нижней",
        default: 15,
      },
      {
        key: "nonad_page_min_wait",
        type: "int",
        unit: "сек",
        min: 0,
        max: 3600,
        hint: "Нижняя граница случайной паузы на странице без рекламы, 0–3600 сек",
        default: 15,
      },
      {
        key: "nonad_page_max_wait",
        type: "int",
        unit: "сек",
        min: 0,
        max: 3600,
        hint: "Верхняя граница случайной паузы на странице без рекламы, 0–3600 сек, не меньше нижней",
        default: 20,
      },
      {
        key: "max_scroll_limit",
        type: "int",
        unit: "стр.",
        min: 0,
        max: 3600,
        hint: "Максимум прокруток страницы, 0–3600, 0 — без ограничения",
        default: 0,
      },
      {
        key: "check_shopping_ads",
        type: "bool",
        hint: "Проверять товарные объявления в выдаче",
        default: true,
      },
      {
        key: "excludes",
        type: "string",
        hint: "Строки-исключения: запрос с таким вхождением пропускается",
        default: "",
      },
      {
        key: "random_mouse",
        type: "bool",
        hint: "Случайное движение мыши во время пауз",
        default: false,
      },
      {
        key: "custom_cookies",
        type: "bool",
        hint: "Использовать cookies.txt при открытии браузера",
        default: false,
      },
      {
        key: "click_order",
        type: "int",
        min: 0,
        max: 1000,
        hint: "Порядковый номер рекламного результата, по которому выполняется клик, 0–1000",
        default: 5,
      },
      {
        key: "browser_count",
        type: "int",
        min: 1,
        max: 8,
        hint: "Число параллельных браузеров, 1–8",
        default: 2,
      },
      {
        key: "multiprocess_style",
        type: "enum",
        options: [
          { title: "Разный запрос на каждый браузер", value: 1 },
          { title: "Одинаковый запрос на всех браузерах", value: 2 },
        ],
        hint: "Схема распределения запросов между потоками",
        default: 1,
      },
      {
        key: "loop_wait_time",
        type: "int",
        unit: "сек",
        min: 0,
        max: 86400,
        hint: "Пауза между проходами по списку запросов, 0–86400 сек",
        default: 60,
      },
      {
        key: "wait_factor",
        type: "float",
        step: 0.01,
        min: 0.01,
        max: 100,
        hint: "Множитель всех случайных пауз: 1 — как задано, 0.5 — вдвое быстрее; 0.01–100",
        default: 1,
      },
      {
        key: "running_interval_start",
        type: "string",
        hint: "Начало окна работы по расписанию, ЧЧ:ММ в пределах 00:00–23:59. Пусто — ограничения нет; заполняйте вместе с концом",
        default: "",
      },
      {
        key: "running_interval_end",
        type: "string",
        hint: "Конец окна работы по расписанию, ЧЧ:ММ в пределах 00:00–23:59. Пусто — ограничения нет; заполняйте вместе с началом",
        default: "",
      },
      {
        key: "2captcha_apikey",
        type: "string",
        hint: "Ключ 2Captcha для автоматического решения CAPTCHA",
        default: "",
        secret: true,
      },
      {
        key: "hooks_enabled",
        type: "bool",
        hint: "Включить пользовательские хуки сценария",
        default: false,
      },
      {
        key: "telegram_enabled",
        type: "bool",
        hint: "Отправлять уведомления о результатах в Telegram",
        default: false,
      },
      {
        key: "send_to_android",
        type: "bool",
        hint: "Отправлять отчёты на Android-устройства через ADB",
        default: false,
      },
      {
        key: "request_boost",
        type: "bool",
        hint: "Ускорять загрузку страницы отключением тяжёлых ресурсов",
        default: false,
      },
      // Пороговая политика CAPTCHA (план §5, фаза 8): что делать при самом
      // событии и что делать, когда доля CAPTCHA за час вышла за порог.
      {
        key: "captcha_policy",
        type: "enum",
        options: [
          { title: "Стоп: ждать оператора (по умолчанию)", value: "stop" },
          { title: "Авто-решение", value: "solve" },
          { title: "Решать, при неудаче — стоп", value: "both" },
        ],
        hint: "Что делать при обнаружении CAPTCHA: ждать оператора, решать через 2Captcha автоматически или решать, а при неудаче — остановиться",
        default: "stop",
      },
      {
        key: "captcha_threshold_percent",
        type: "float",
        unit: "%",
        step: 1,
        min: 0,
        max: 100,
        hint: "Порог доли CAPTCHA за скользящий час, в процентах, 0–100: 0 — срабатывать на любой доле; по умолчанию 5%",
        default: 5,
      },
      {
        key: "captcha_threshold_action",
        type: "enum",
        options: [
          { title: "Только предупреждение", value: "warn" },
          { title: "Автопауза", value: "pause" },
          { title: "Ротация прокси", value: "rotate" },
        ],
        hint: "Что делать при превышении порога: только предупредить, поставить сценарий на автопаузу или ротировать прокси",
        default: "warn",
      },
      // Хранение логов (план §5, фаза 9): срок жизни записей, уровень
      // дневного файла и потолок размера БД. Live-применение retention и
      // автозащита — на стороне демона; форма лишь сохраняет поля через
      // POST /control/config, как и любые другие.
      {
        key: "log_retention_days",
        type: "int",
        unit: "дн",
        min: 1,
        max: 3650,
        hint: "Хранение логов, дней: записи и файлы экспорта старше срока удаляются, 1–3650",
        default: 30,
      },
      {
        key: "log_file_level",
        type: "enum",
        options: [
          { title: "Все записи (DEBUG)", value: "DEBUG" },
          { title: "Информация и выше (INFO)", value: "INFO" },
          { title: "Предупреждения и выше (WARNING)", value: "WARNING" },
          { title: "Только ошибки (ERROR)", value: "ERROR" },
        ],
        hint: "Уровень в файл экспорта: какие записи попадают в дневной файл лога, DEBUG в БД остаётся всегда",
        default: "INFO",
      },
      {
        key: "db_size_limit_mb",
        type: "int",
        unit: "МБ",
        min: 0,
        max: 102400,
        hint: "Лимит размера БД, МБ, 0 = выкл: при превышении автозащита удаляет старые дни, 0–102400",
        default: 0,
      },
    ],
  },
];
