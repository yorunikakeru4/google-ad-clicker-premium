export type SettingType = "bool" | "int" | "float" | "string" | "enum" | "path";

export interface SettingOption {
  title: string;
  value: number;
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
}

export interface SettingSection {
  key: "paths" | "webdriver" | "behavior";
  title: string;
  fields: SettingFieldDef[];
}

export const settingsSections: SettingSection[] = [
  {
    key: "paths",
    title: "Файлы и пути",
    fields: [
      {
        key: "query_file",
        type: "path",
        hint: "Файл с поисковыми запросами, по одному в строке. Пусто — запрос берётся из behavior.query",
        default: "queries.txt",
      },
      {
        key: "proxy_file",
        type: "path",
        hint: "Файл со списком прокси, по одному в строке. Пусто — используется behavior.proxy",
        default: "proxies.txt",
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
        hint: "Нижняя граница случайной паузы на странице с рекламой",
        default: 10,
      },
      {
        key: "ad_page_max_wait",
        type: "int",
        unit: "сек",
        min: 0,
        hint: "Верхняя граница случайной паузы на странице с рекламой",
        default: 15,
      },
      {
        key: "nonad_page_min_wait",
        type: "int",
        unit: "сек",
        min: 0,
        hint: "Нижняя граница случайной паузы на странице без рекламы",
        default: 15,
      },
      {
        key: "nonad_page_max_wait",
        type: "int",
        unit: "сек",
        min: 0,
        hint: "Верхняя граница случайной паузы на странице без рекламы",
        default: 20,
      },
      {
        key: "max_scroll_limit",
        type: "int",
        unit: "стр.",
        min: 0,
        hint: "Максимум прокруток страницы, 0 — без ограничения",
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
        min: 1,
        max: 20,
        hint: "Порядковый номер рекламного результата, по которому выполняется клик",
        default: 5,
      },
      {
        key: "browser_count",
        type: "int",
        min: 0,
        max: 32,
        hint: "Число параллельных браузеров, 0 — по числу ядер процессора",
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
        hint: "Пауза между проходами по списку запросов",
        default: 60,
      },
      {
        key: "wait_factor",
        type: "float",
        step: 0.1,
        min: 0,
        hint: "Множитель всех случайных пауз: 1 — как задано, 0.5 — вдвое быстрее",
        default: 1,
      },
      {
        key: "running_interval_start",
        type: "string",
        hint: "Начало окна работы по расписанию в формате HH:MM",
        default: "00:00",
      },
      {
        key: "running_interval_end",
        type: "string",
        hint: "Конец окна работы по расписанию в формате HH:MM",
        default: "00:00",
      },
      {
        key: "2captcha_apikey",
        type: "string",
        hint: "Ключ 2Captcha для автоматического решения CAPTCHA",
        default: "",
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
    ],
  },
];
