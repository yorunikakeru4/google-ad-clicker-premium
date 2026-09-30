import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from engine.log_rotation import LEVEL_ORDER


LOG_FILENAME = Path.cwd() / "logs" / "adclicker.log"
LOG_FILENAME.parent.mkdir(exist_ok=True)

# Имя уровня -> число logging. Валидация и установка идут по одному и тому же
# словарю: допустимые значения не проверяются отдельным списком, а имена
# приходят из engine.log_rotation — там же живут enum behavior.log_file_level
# и фильтр дневного экспорта, поэтому словари не могут разойтись.
FILE_LEVELS: dict[str, int] = {
    name: logging.getLevelNamesMapping()[name] for name in LEVEL_ORDER
}

# Create a custom logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Create handlers
console_handler = logging.StreamHandler()
file_handler = RotatingFileHandler(
    LOG_FILENAME, maxBytes=20971520, encoding="utf-8", backupCount=50
)
console_handler.setLevel(logging.INFO)
file_handler.setLevel(logging.DEBUG)

# Create formatters and add it to handlers
console_log_format = "%(asctime)s [%(levelname)5s] %(lineno)3d: %(message)s"
file_log_format = "%(asctime)s [%(levelname)5s] %(filename)s:%(lineno)3d: %(message)s"
console_formatter = logging.Formatter(console_log_format, datefmt="%d-%m-%Y %H:%M:%S")
console_handler.setFormatter(console_formatter)
file_formatter = logging.Formatter(file_log_format, datefmt="%d-%m-%Y %H:%M:%S")
file_handler.setFormatter(file_formatter)

# Add handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)


def apply_file_level(name: str) -> int:
    """Ставит уровень файлового обработчика ``adclicker.log``.

    Валидация — по тому же словарю ``FILE_LEVELS``, что и установка:
    неизвестное имя не доходит до ``setLevel`` и поднимает ``ValueError``
    со списком допустимых значений. Консольный обработчик не трогается —
    INFO в терминале остаётся прежним независимо от настройки.

    Вызывается там, где читается ``config.json``: в ``config_reader`` при
    каждом чтении (старт воркера и перечитывание настроек) и в
    ``build_daemon`` при старте демона — плюс после каждого успешного
    ``POST /control/config``, чтобы демон применил новый уровень сразу, без
    рестарта. Воркеры live-уровень не получают: они перечитывают конфиг в
    своём процессе и подхватывают значение при следующем старте/перечитке.
    Уровень — поле конфига (``behavior.log_file_level``), поэтому
    legacy-зеркало двойной записи подхватывает его без правки кода.

    Возвращает установленный уровень (число logging).
    """
    try:
        level = FILE_LEVELS[name]
    except (KeyError, TypeError):
        raise ValueError(
            f"неизвестный уровень файлового лога: {name!r}; "
            f"допустимы {list(FILE_LEVELS)}"
        ) from None
    file_handler.setLevel(level)
    return level


class MultiprocessLogFilter(logging.Filter):
    """Custom log filter for multiple browser runs

    :type browser_id: str
    :param browser_id: Id for browser instance
    """

    def __init__(self, browser_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.browser_id = browser_id

    def filter(self, record: logging.LogRecord) -> bool:
        """Register custom keyword for log formatters

        :type record: logging.LogRecord
        :param record: LogRecord instance
        :rtype: bool
        :returns: True
        """

        record.browser_id = self.browser_id

        return True


def update_log_formats(browser_id: str) -> None:
    """Update log formats for multiprocess runs

    :type browser_id: str
    :param browser_id: Id for browser instance
    """

    logger.removeHandler(console_handler)
    logger.removeHandler(file_handler)

    console_log_format = "%(asctime)s <<%(browser_id)s>> [%(levelname)5s] %(lineno)3d: %(message)s"
    file_log_format = (
        "%(asctime)s <<%(browser_id)s>> [%(levelname)5s] %(filename)s:%(lineno)3d: %(message)s"
    )
    console_formatter = logging.Formatter(console_log_format, datefmt="%d-%m-%Y %H:%M:%S")
    console_handler.setFormatter(console_formatter)
    file_formatter = logging.Formatter(file_log_format, datefmt="%d-%m-%Y %H:%M:%S")
    file_handler.setFormatter(file_formatter)

    # Фильтры сбрасываются перед добавлением: update_log_formats зовётся на
    # каждый раунд воркера (раньше — один раз на процесс), и без сброса
    # цепочка фильтров росла бы неограниченно, заставляя каждую строку
    # проходить через все накопленные MultiprocessLogFilter.
    console_handler.filters.clear()
    file_handler.filters.clear()
    console_handler.addFilter(MultiprocessLogFilter(browser_id))
    file_handler.addFilter(MultiprocessLogFilter(browser_id))

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
