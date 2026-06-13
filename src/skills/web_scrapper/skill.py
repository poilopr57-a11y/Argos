import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

# CSS-классы сниппетов DuckDuckGo (разные версии HTML-выдачи)
_DDG_SNIPPET_SELECTORS = [
    ("a", "result__snippet"),  # старая выдача
    ("span", "result__snippet"),  # промежуточная выдача
    ("div", "result__body"),  # альтернативный контейнер
    ("td", "result-snippet"),  # табличный макет
]


class ArgosScrapper:
    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }

    def quick_search(self, query):
        """Парсинг поисковой выдачи для получения оперативной информации."""
        try:
            # Явное URL-кодирование запроса
            encoded_query = quote_plus(query)
            url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
            response = requests.get(url, headers=self.headers, timeout=10)

            if not (200 <= response.status_code < 300):
                return "Ошибка доступа к сетевым узлам поиска."

            soup = BeautifulSoup(response.text, "html.parser")

            # Перебираем известные CSS-селекторы сниппетов
            snippets = []
            for tag, css_class in _DDG_SNIPPET_SELECTORS:
                snippets = soup.find_all(tag, class_=css_class)
                if snippets:
                    break

            # Резервный вариант: ищем любой элемент с «result» в классе
            if not snippets:
                snippets = [
                    el
                    for el in soup.find_all(class_=True)
                    if any("result" in c for c in el.get("class", [])) and el.get_text(strip=True)
                ]

            if not snippets:
                return "Поиск не дал результатов. Информация в зашифрованных слоях не найдена."

            # Берем первые 3 результата для лаконичности
            results = [s.get_text(separator=" ", strip=True) for s in snippets[:3]]
            formatted_data = " | ".join(r for r in results if r)

            return f"Данные из глобальной сети: {formatted_data}"

        except Exception as e:
            return f"Сбой сетевого сканирования: {str(e)}"

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        if not t or t.lower() in ("статус", "status", "помощь"):
            return "🔍 WebScrapper готов. Укажи поисковый запрос."
        return self.quick_search(t)


def execute(text: str = "") -> str:
    """Модульная точка входа SkillLoader."""
    s = ArgosScrapper()
    t = (text or "").strip()
    if not t:
        return "🔍 WebScrapper готов. Укажи поисковый запрос."
    return s.quick_search(t)
