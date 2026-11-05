"""
Telegram-бот: собирает финансовые новости из русских RSS-лент и постит их в канал.
Важные новости (по ключевым словам в заголовке) публикуются с картинкой, если она есть.

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
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]  # @my_news_channel или -100123456789
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MIN", "10")) * 60
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "6"))
DELAY_BETWEEN_POSTS = 5  # секунд между постами
STATE_PATH = os.getenv("STATE_PATH", "seen.json")

# (название, ссылка на RSS, фильтровать ли по ключевым словам)
# Если лента перестала работать - замени ссылку или удали строку: бот просто пропустит её.
FEEDS = [
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss", True),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml", True),
    ("Ведомости", "https://www.vedomosti.ru/rss/news", True),
    ("Интерфакс", "https://www.interfax.ru/rss.asp", True),
    ("ТАСС", "https://tass.ru/rss/v2.xml", True),
    ("РИА Новости", "https://ria.ru/export/rss2/economy/index.xml", True),
    ("Investing.com", "https://ru.investing.com/rss/news.rss", False),
]

# Общий фильтр для лент, где флаг True.
KEYWORDS = [
    "акци", "облигац", "инвест", "бирж", "ключев", "ставк", "центробанк", "цб рф",
    "инфляц", "курс доллар", "курс евро", "курс юан", "рубл", "нефт", "газпром",
    "сбер", "дивиденд", "ipo", "крипт", "биткоин", "bitcoin", "фонд", "индекс мосбирж",
    "ввп", "санкц", "банк", "налог", "бюджет",
]

# Новость с таким словом в заголовке считается важной и публикуется с картинкой.
IMPORTANT_KEYWORDS = [
    "срочно", "молния", "ключевая ставка", "ключевую ставку", "ключевой ставки",
    "ставку цб", "ставки цб", "рекорд", "обвал", "дефолт", "санкци", "девальвац",
    "банкрот", "ipo", "дивиденд", "резко", "паден", "обвалил", "взлет", "взлёт",
    "курс доллара", "курс евро", "курс юаня", "инфляци", "мосбирж", "нефть", "нефти",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("news_bot")


# ---------------------------------------------------------------- состояние (что уже постили)
def load_state() -> tuple[dict, bool]:
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


# ---------------------------------------------------------------- разбор лент
def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def matches_keywords(entry) -> bool:
    haystack = (entry.get("title", "") + " " + clean_text(entry.get("summary", ""))).lower()
    return any(k in haystack for k in KEYWORDS)


def is_important(entry) -> bool:
    title = (entry.get("title") or "").lower()
    return any(k in title for k in IMPORTANT_KEYWORDS)


def get_image(entry) -> str | None:
    """Ищем картинку в enclosure, media-тегах или в HTML описания."""
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        if href and (enc.get("type", "").startswith("image") or re.search(r"\.(jpe?g|png|webp)(\?|$)", href, re.I)):
            return href
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    blobs = [entry.get("summary", "")]
    blobs += [c.get("value", "") for c in entry.get("content", []) or []]
    for blob in blobs:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blob or "", re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def entry_ts(entry) -> int:
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(ts) if ts else 0


def is_fresh(entry) -> bool:
    ts = entry_ts(entry)
    return True if not ts else time.time() - ts <= MAX_AGE_HOURS * 3600


def fetch_entries():
    """Список (источник, entry) от старых к новым."""
    result = []
    for name, url, use_filter in FEEDS:
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
            result.append((name, entry))
    result.sort(key=lambda item: entry_ts(item[1]))
    return result


# ---------------------------------------------------------------- отправка
def format_message(source: str, entry) -> str:
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:300].rsplit(" ", 1)[0] + "…"

    title = html.escape(title)
    summary = html.escape(summary)
    link = html.escape(entry.get("link", ""), quote=True)

    parts = [f"<b>{title}</b>"]
    if summary and summary.lower() != title.lower():
        parts.append(summary)
    parts.append(f'<a href="{link}">Источник: {html.escape(source)}</a>')
    return "\n\n".join(parts)


def tg_request(method: str, payload: dict) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
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
        log.error("Telegram вернул ошибку (%s): %s", method, data)
        return False
    return False


def send_post(text: str, image: str | None) -> bool:
    """Если есть картинка - шлём фото с подписью; при неудаче шлём просто текст."""
    if image and len(text) <= 1000:  # лимит подписи к фото - 1024 символа
        ok = tg_request("sendPhoto", {
            "chat_id": CHANNEL_ID, "photo": image, "caption": text, "parse_mode": "HTML",
        })
        if ok:
            return True
        log.info("Фото не отправилось, публикую без картинки")
    return tg_request("sendMessage", {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"})


# ---------------------------------------------------------------- логика
def run_cycle(state: dict) -> None:
    posted = 0
    for source, entry in fetch_entries():
        if posted >= MAX_POSTS_PER_CYCLE:
            break
        if is_seen(state, entry):
            continue
        if not is_fresh(entry):
            mark_seen(state, entry)
            continue
        image = get_image(entry) if is_important(entry) else None
        if send_post(format_message(source, entry), image):
            mark_seen(state, entry)
            posted += 1
            log.info("Опубликовано%s: %s", " (с фото)" if image else "", entry.get("title", "")[:80])
            save_state(state)
            time.sleep(DELAY_BETWEEN_POSTS)
    log.info("Проход завершён, опубликовано новостей: %d", posted)


def bootstrap(state: dict) -> None:
    """Первый запуск: помечаем всё текущее как прочитанное, чтобы не завалить канал."""
    count = 0
    for _, entry in fetch_entries():
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
