"""Тесты logger.py: фильтр browser_id для мультипроцессного режима.

update_log_formats() меняет глобальное состояние логгера, поэтому каждый тест
работает с фикстурой restore_logging, которая возвращает исходные обработчики,
форматтеры и фильтры.
"""

import logging

from logger import MultiprocessLogFilter, logger, update_log_formats


def make_record(message="Starting search"):
    return logging.LogRecord("adclicker", logging.INFO, "search_controller.py", 131, message, None, None)


def render(handler, record):
    """Прогнать запись через фильтры обработчика и отформатировать её."""

    for log_filter in handler.filters:
        log_filter.filter(record)
    return handler.formatter.format(record)


def test_filter_adds_browser_id_to_record():
    record = make_record()

    result = MultiprocessLogFilter("7").filter(record)

    assert result is True
    assert record.browser_id == "7"


def test_filter_keeps_numeric_browser_id_as_given():
    record = make_record()

    MultiprocessLogFilter(4).filter(record)

    assert record.browser_id == 4


def test_filter_accepts_every_record():
    log_filter = MultiprocessLogFilter("7")

    assert all(log_filter.filter(make_record()) for _ in range(5))


def test_update_log_formats_adds_browser_id_to_output(restore_logging):
    update_log_formats("7")
    console = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, "baseFilename"))

    rendered = render(console, make_record())

    assert "<<7>>" in rendered
    assert "Starting search" in rendered


def test_update_log_formats_puts_browser_id_before_level(restore_logging):
    update_log_formats("7")
    console = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, "baseFilename"))

    rendered = render(console, make_record())

    assert rendered.index("<<7>>") < rendered.index("INFO")


def test_update_log_formats_keeps_level_and_line_number(restore_logging):
    update_log_formats("7")
    console = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, "baseFilename"))

    rendered = render(console, make_record())

    assert " 131:" in rendered
    # levelname выровнен по правому краю до 5 символов, lineno - по левому до 3.
    assert "[ INFO] 131:" in rendered


def test_update_log_formats_applies_to_both_handlers(restore_logging):
    update_log_formats("7")

    formatted = [render(handler, make_record()) for handler in logger.handlers]

    assert formatted
    assert all("<<7>>" in text for text in formatted)


def test_update_log_formats_does_not_duplicate_handlers(restore_logging):
    before = len(logger.handlers)

    update_log_formats("7")

    assert len(logger.handlers) == before


def test_update_log_formats_is_reversible(restore_logging):
    update_log_formats("7")
    update_log_formats("9")
    console = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, "baseFilename"))

    rendered = render(console, make_record())

    assert "<<9>>" in rendered
    assert "<<7>>" not in rendered


def test_default_format_has_no_browser_id(restore_logging):
    console = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, "baseFilename"))

    assert "browser_id" not in console.formatter._fmt


def test_update_log_formats_does_not_accumulate_filters(restore_logging):
    """Фильтры обязаны заменяться, а не копиться.

    update_log_formats теперь зовётся на каждый раунд воркера, а не один раз
    на процесс: без сброса каждый обработчик накапливал бы по фильтру на
    прогон, и каждая строка лога прошла бы через всю цепочку.
    """
    for browser_id in ("1", "2", "3", "4", "5"):
        update_log_formats(browser_id)

    handlers = list(logger.handlers)
    assert handlers, "логгер должен иметь обработчики"
    for handler in handlers:
        assert len(handler.filters) == 1, (
            f"у {handler} накопилось {len(handler.filters)} фильтров, ожидался ровно 1"
        )

    for handler in handlers:
        assert "<<5>>" in render(handler, make_record()), (
            "остался фильтр не того воркера: последний вызов обязан перекрыть прежние"
        )
