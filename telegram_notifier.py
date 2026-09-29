import asyncio
import os
from html import escape
from pathlib import Path
from typing import Optional

import telegram
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from telegram.constants import ParseMode

from engine.log import get_logger
from stats import SearchStats


log = get_logger()


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

bot = telegram.Bot(token=TELEGRAM_TOKEN)

telegram_chat_id_file = Path(".TELEGRAM_CHAT_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command

    :type update: Update
    :param update: Incoming update object
    :type context: ContextTypes
    :param context: Callback context
    """

    with open(telegram_chat_id_file, mode="w", encoding="utf-8") as chat_id_file:
        log.info("scheduler", "Chat ID", fields={"chat_id": update.effective_chat.id})
        chat_id_file.write(str(update.effective_chat.id))

    response = "Started Ad Clicker Premium Notifier! Please end the script with CTRL+C"

    await context.bot.send_message(chat_id=update.effective_chat.id, text=response)

    log.info("scheduler", "Please end the script with CTRL+C")


async def send_message(chat_id: str, message: str) -> None:
    """Send message to user with the given chat id

    Limit the message length with maximum of 2048 characters.

    :type chat_id: str
    :param chat_id: Chat ID of the user
    :type message: str
    :param message: Message to send
    """

    async with bot:
        if len(message) > 2048:
            message = message[:2048]

            if "</pre>" not in message:
                message += "</pre>\n"

        await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)


def notify_matching_ads(query: str, links: list, stats: Optional[SearchStats] = None) -> None:
    """Notify matching ads via Telegram

    :type query: str
    :param query: Query used for search
    :type links: AllLinks
    :param links: List of (ad, ad_link, ad_title) tuples
    :type stats: SearchStats
    :param stats: Search statistics data
    """

    if not telegram_chat_id_file.exists():
        log.info("scheduler", "Please start the messaging with bot to get a chat ID!")
        raise SystemExit()

    with open(telegram_chat_id_file, encoding="utf-8") as chat_id_file:
        chat_id = chat_id_file.read().strip()

    if not links:
        message = f"{stats.to_pre_text()}\n" if stats else ""
        message += f"<b>No matching ads found in the search results for query:</b> {query}\n"

        asyncio.run(send_message(chat_id=chat_id, message=message))
        return

    if stats:
        message = f"{stats.to_pre_text()}\n<b>Query:</b> {query}\n"
    else:
        message = f"<b>Query:</b> {query}\n"

    for link in links:
        link_url = link[1]
        original_ad_title = link[2].replace("\n", " ")

        ad_title = original_ad_title.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")

        message += f"<b>Ad Title:</b> {ad_title}\n"

        log.debug(
            "click",
            "Notification was added",
            fields={"ad_title": original_ad_title, "url": link_url},
        )

    try:
        log.info("click", "Sending Telegram notification...")
        asyncio.run(send_message(chat_id=chat_id, message=message))

    except Exception as exp:
        log.debug("click", "Telegram send error", fields={"error": str(exp)})
        log.error("click", "Failed to send notification!")
        log.debug("click", "Message", fields={"message": message})


def notify_captcha_event(
    *,
    browser_id: Optional[str],
    page_url: Optional[str],
    screenshot_path: Optional[str],
    solved: bool,
    policy: str,
) -> None:
    """Notify about a CAPTCHA event the moment it happens (план §5, фаза 8).

    ``notify_matching_ads`` срабатывает по результатам прогона, а CAPTCHA —
    событие посреди сценария: оператор должен узнать о нём до того, как
    воркер уйдёт в паузу. Отличия от result-уведомлений:

    * нет chat id — INFO и выход, без ``SystemExit``: сценарий уже принял
      решение, ломать его из-за ненастроенного бота нельзя;
    * ошибка отправки гасится здесь же (WARNING/ERROR в логе), наружу не
      выходит — вызывающая сторона уже записала событие в БД;
    * URL и путь к скриншоту экранируются: сообщение уходит с
      ``parse_mode=HTML``, а ``&`` в URL ломает разбор.
    """

    if not telegram_chat_id_file.exists():
        log.info("captcha", "Please start the messaging with bot to get a chat ID!")
        return

    with open(telegram_chat_id_file, encoding="utf-8") as chat_id_file:
        chat_id = chat_id_file.read().strip()

    message = f"<b>CAPTCHA detected</b> (policy: {escape(policy)})\n"
    message += f"<b>Browser:</b> {escape(browser_id or 'unknown')}\n"
    message += f"<b>URL:</b> {escape(page_url or 'unknown')}\n"
    message += f"<b>Solved:</b> {'yes' if solved else 'no'}\n"
    if screenshot_path:
        message += f"<b>Screenshot:</b> {escape(str(screenshot_path))}\n"

    try:
        log.info("captcha", "Sending Telegram CAPTCHA notification...")
        asyncio.run(send_message(chat_id=chat_id, message=message))
    except Exception as exp:
        log.debug("captcha", "Telegram send error", fields={"error": str(exp)})
        log.error("captcha", "Failed to send CAPTCHA notification!")


def start_bot() -> None:
    """Start polling updates to get /start command"""

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    start_handler = CommandHandler("start", start)
    application.add_handler(start_handler)

    log.info("scheduler", "Waiting for /start command...")
    application.run_polling()
