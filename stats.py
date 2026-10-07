from dataclasses import dataclass


@dataclass
class SearchStats:

    browser_id: int = 0
    captcha_seen: bool = False
    captcha_solved: bool = False
    ads_found: int = 0
    num_filtered_ads: int = 0
    num_excluded_ads: int = 0
    ads_clicked: int = 0
    non_ads_clicked: int = 0
    shopping_ads_found: int = 0
    num_filtered_shopping_ads: int = 0
    num_excluded_shopping_ads: int = 0
    shopping_ads_clicked: int = 0

    def _summary_parts(self) -> list[str]:
        """Сегменты сводки — единственный источник для обеих форм вывода.

        ASCII-таблица заменена на компактные сегменты вида
        ``ads: 3 found, 0 clicked``: набор данных тот же, читается в логе
        и на экране телефона без рамок и выравнивания по ширине.
        """

        parts: list[str] = []
        if self.browser_id:
            parts.append(f"browser: {self.browser_id}")
        parts.append(
            f"ads: {self.ads_found} found, {self.num_filtered_ads} filtered, "
            f"{self.num_excluded_ads} excluded, {self.ads_clicked} clicked"
        )
        parts.append(
            f"shopping: {self.shopping_ads_found} found, "
            f"{self.num_filtered_shopping_ads} filtered, "
            f"{self.num_excluded_shopping_ads} excluded, "
            f"{self.shopping_ads_clicked} clicked"
        )
        seen = "seen" if self.captcha_seen else "not seen"
        solved = "solved" if self.captcha_solved else "not solved"
        parts.append(f"captcha: {seen}, {solved}")
        parts.append(f"non-ads: {self.non_ads_clicked} clicked")
        return parts

    def to_pre_text(self) -> str:
        """Сводка как html ``<pre>``-блок для Telegram.

        Сегменты те же, что в :meth:`__str__`, но каждый на своей строке:
        на узком экране однострочная форма развернулась бы в сплошную
        кашу, а построчная раскладка группу видно сразу.
        """

        text = "<pre>Summary of Statistics"
        for part in self._summary_parts():
            text += f"\n{part}"
        text += "</pre>\n"

        return text

    def __str__(self):
        """Сводка одной строкой — вид записи в лог вместо ASCII-таблицы."""

        return " | ".join(self._summary_parts())
