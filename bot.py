"""
Telegram-бот: собирает финансовые новости из русских и английских RSS-лент,
переводит английские на русский и постит в канал.

Два режима:
  python bot.py          - работает постоянно (ПК, VPS), проверяет ленты каждые N минут
  python bot.py --once   - один проход и выход (для GitHub Actions по расписанию)
"""
import html
import json
import logging
import os
import re
import sys
import time
from calendar import timegm

import feedparser
import requests
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.environ["9120563197:AAHu2zFG_AdqblSi60x9ymSLM-zuorNT-SU"]
CHANNEL_ID = os.environ["@Business_NEWs20"]  # @my_news_channel или -100123456789
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MIN", "10")) * 60
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "6"))
DELAY_BETWEEN_POSTS = 5  # секунд между постами
STATE_PATH = os.getenv("STATE_PATH", "seen.json")

# (название, ссылка на RSS, язык "ru"/"en", фильтровать ли по ключевым словам)
# Если лента перестала работать - замени ссылку или удали строку.
FEEDS = [
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", "ru", True),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml", "ru", True),
    ("Ведомости", "https://www.vedomosti.ru/rss/news", "ru", True),
    ("Investing.com", "https://www.investing.com/rss/news.rss", "en", False),
    ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html", "en", False),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "en", False),
]

# Фильтр для общих русских лент (где последний флаг True).
KEYWORDS = [
    "акци", "облигац", "инвест", "бирж", "ключев", "ставк", "центробанк", "цб рф",
    "инфляц", "курс доллар", "курс евро", "курс юан", "рубл", "нефт", "газпром",
    "сбер", "дивиденд", "ipo", "крипт", "биткоин", "bitcoin", "фонд", "индекс мосбирж",
    "ввп", "санкц", "банк", "налог", "бюджет",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("news_bot")

_translator = GoogleTranslator(source="auto", target="ru")


# ---------------------------------------------------------------- состояние (что уже постили)
def load_state() -> tuple[dict, bool]:
    """Возвращает (словарь ключ->время, существовал ли файл раньше)."""
    if not os.path.exists(STATE_PATH):
        return {}, False
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f), True
    except Exception:
        log.warning("Не удалось прочитать %s, начинаю с чистого состояния", STATE_PATH)
        return {}, False


def save_state(state: dict) -> None:
    cutoff = time.time() - 30 * 86400  # храним 30 дней
    state = {k: v for k, v in state.items() if v >= cutoff}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def entry_keys(entry) -> list[str]:
    keys = []
    link = entry.get("link") or entry.get("id")
    if link:
        keys.append("url:" + link.split("?")[0])
    title = re.sub(r"\W+", "", (entry.get("title") or "").lower())
    if title:
        keys.append("title:" + title)
    return keys


def is_seen(state: dict, entry) -> bool:
    keys = entry_keys(entry)
    return (not keys) or any(k in state for k in keys)


def mark_seen(state: dict, entry) -> None:
    now = time.time()
    for k in entry_keys(entry):
        state[k] = now


# ---------------------------------------------------------------- ленты
def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def matches_keywords(entry) -> bool:
    haystack = (entry.get("title", "") + " " + clean_text(entry.get("summary", ""))).lower()
    return any(k in haystack for k in KEYWORDS)


def entry_ts(entry) -> int:
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(ts) if ts else 0


def is_fresh(entry) -> bool:
    ts = entry_ts(entry)
    return True if not ts else time.time() - ts <= MAX_AGE_HOURS * 3600


def fetch_entries():
    """Список (источник, язык, entry) от старых к новым."""
    result = []
    for name, url, lang, use_filter in FEEDS:
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 NewsBot"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            log.warning("Не удалось загрузить %s: %s", name, e)
            continue
        for entry in feed.entries:
            if use_filter and not matches_keywords(entry):
                continue
            result.append((name, lang, entry))
    result.sort(key=lambda item: entry_ts(item[2]))
    return result


# ---------------------------------------------------------------- перевод и отправка
def translate(text: str) -> str:
    if not text:
        return text
    for _ in range(2):
        try:
            return _translator.translate(text) or text
        except Exception as e:
            log.warning("Ошибка перевода: %s", e)
            time.sleep(2)
    return text  # если перевод не удался, постим оригинал


def format_message(source: str, lang: str, entry) -> str:
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:300].rsplit(" ", 1)[0] + "…"

    if lang != "ru":
        title = translate(title)
        summary = translate(summary)

    title = html.escape(title)
    summary = html.escape(summary)
    link = html.escape(entry.get("link", ""), quote=True)
    label = f"Источник: {html.escape(source)}" + (" (перевод)" if lang != "ru" else "")

    parts = [f"<b>{title}</b>"]
    if summary and summary.lower() != title.lower():
        parts.append(summary)
    parts.append(f'<a href="{link}">{label}</a>')
    return "\n\n".join(parts)


def send_to_channel(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            data = r.json()
        except Exception as e:
            log.warning("Ошибка сети при отправке: %s", e)
            time.sleep(5)
            continue
        if data.get("ok"):
            return True
        if r.status_code == 429:
            wait = data.get("parameters", {}).get("retry_after", 10)
            log.warning("Лимит Telegram, ждём %s сек", wait)
            time.sleep(wait + 1)
            continue
        log.error("Telegram вернул ошибку: %s", data)
        return False
    return False


# ---------------------------------------------------------------- логика
def run_cycle(state: dict) -> None:
    posted = 0
    for source, lang, entry in fetch_entries():
        if posted >= MAX_POSTS_PER_CYCLE:
            break
        if is_seen(state, entry):
            continue
        if not is_fresh(entry):
            mark_seen(state, entry)
            continue
        if send_to_channel(format_message(source, lang, entry)):
            mark_seen(state, entry)
            posted += 1
            log.info("Опубликовано: %s", entry.get("title", "")[:80])
            save_state(state)
            time.sleep(DELAY_BETWEEN_POSTS)
    log.info("Проход завершён, опубликовано новостей: %d", posted)


def bootstrap(state: dict) -> None:
    """Первый запуск: помечаем всё текущее как прочитанное, чтобы не завалить канал."""
    count = 0
    for _, _, entry in fetch_entries():
        mark_seen(state, entry)
        count += 1
    save_state(state)
    log.info("Первый запуск: пропущено %d существующих новостей", count)


def main() -> None:
    once = "--once" in sys.argv
    state, existed = load_state()
    if not existed:
        bootstrap(state)
        if once:
            return
    while True:
        try:
            run_cycle(state)
            save_state(state)
        except Exception:
            log.exception("Ошибка в основном цикле")
        if once:
            return
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
