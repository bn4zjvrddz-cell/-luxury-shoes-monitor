from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx
import yaml
from bs4 import BeautifulSoup


BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"
CONFIG_PATH = BASE_DIR / "config.yaml"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0 Safari/537.36"
)

UK_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

CONDITION_ALIASES = {
    "нове": "new",
    "новий": "new",
    "новая": "new",
    "новое": "new",
    "вживане": "used",
    "б/у": "used",
    "бу": "used",
    "идеальное": "ideal",
    "ідеальний": "ideal",
    "ідеальне": "ideal",
    "дуже хороший": "very_good",
    "очень хорошее": "very_good",
    "хороший": "good",
    "хорошее": "good",
    "задовільний": "satisfactory",
    "удовлетворительное": "satisfactory",
}


@dataclass
class SearchCandidate:
    source: str
    brand_query: str
    listing_id: str
    url: str
    priority: bool = False
    title: str = ""
    price: int | None = None
    image: str | None = None


@dataclass
class Listing:
    source: str
    listing_id: str
    url: str
    title: str
    brand: str
    price: int | None
    sizes: list[float] = field(default_factory=list)
    condition: str | None = None
    image: str | None = None
    raw_text: str = ""
    priority: bool = False


class SeenState:
    """
    JSON state for GitHub Actions.
    The runner is temporary, so state.json is committed back to the repository.
    No Telegram token/chat data is stored here.
    """
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, dict[str, int]] = {"olx": {}, "shafa": {}}
        self.meta: dict[str, str] = {}
        self.dirty = False

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw_seen = data.get("seen", {}) or {}
                for source in ("olx", "shafa"):
                    source_seen = raw_seen.get(source, {}) or {}
                    if isinstance(source_seen, list):
                        # Compatibility with an older simple format if needed.
                        self.seen[source] = {str(x): int(time.time()) for x in source_seen}
                    elif isinstance(source_seen, dict):
                        self.seen[source] = {
                            str(k): int(v) if str(v).isdigit() else int(time.time())
                            for k, v in source_seen.items()
                        }
                self.meta = {
                    str(k): str(v)
                    for k, v in (data.get("meta", {}) or {}).items()
                }
            except Exception:
                logging.exception("Could not read state.json; starting with empty state")

    def has(self, source: str, listing_id: str) -> bool:
        return listing_id in self.seen.setdefault(source, {})

    def mark(self, source: str, listing_id: str, url: str = ""):
        bucket = self.seen.setdefault(source, {})
        if listing_id not in bucket:
            bucket[listing_id] = int(time.time())
            self.dirty = True

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str):
        value = str(value)
        if self.meta.get(key) != value:
            self.meta[key] = value
            self.dirty = True

    def save(self):
        # Keep state bounded so the public repository does not grow forever.
        # 20,000 IDs/source is far above what this monitor normally needs.
        for source, bucket in self.seen.items():
            if len(bucket) > 20000:
                newest = sorted(
                    bucket.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:20000]
                self.seen[source] = dict(newest)
                self.dirty = True

        if not self.dirty and self.path.exists():
            return

        payload = {
            "seen": self.seen,
            "meta": self.meta,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self.dirty = False


class WebClient:
    def __init__(self, timeout: float, request_delay: float):
        self.delay = request_delay
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def get(self, url: str) -> str:
        await asyncio.sleep(self.delay + random.uniform(0.15, 0.55))
        r = await self.client.get(url)
        r.raise_for_status()
        if "text/html" not in r.headers.get("content-type", ""):
            raise RuntimeError(
                f"Unexpected content-type for {url}: "
                f"{r.headers.get('content-type')}"
            )
        return r.text


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self.client.aclose()

    async def send_text(self, text: str):
        r = await self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        r.raise_for_status()

    async def send(self, item: Listing):
        source = "OLX" if item.source == "olx" else "SHAFA"
        price = f"{item.price:,}".replace(",", " ") + " грн" if item.price is not None else "—"
        sizes = ", ".join(format_size(x) for x in item.sizes) if item.sizes else "—"
        condition = condition_label(item.condition)

        hot = "🔥 " if item.priority else ""
        caption = (
            f"🆕 {hot}<b>{source}</b>\n"
            f"<b>{html_lib.escape(item.title)}</b>\n\n"
            f"Бренд: {html_lib.escape(item.brand or '—')}\n"
            f"Ціна: <b>{price}</b>\n"
            f"Розмір: {html_lib.escape(sizes)}\n"
            f"Стан: {html_lib.escape(condition)}\n\n"
            f'<a href="{html_lib.escape(item.url, quote=True)}">Відкрити оголошення</a>'
        )

        if item.image:
            try:
                r = await self.client.post(
                    f"{self.base}/sendPhoto",
                    data={
                        "chat_id": self.chat_id,
                        "photo": item.image,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                )
                r.raise_for_status()
                return
            except Exception:
                logging.exception("sendPhoto failed; falling back to sendMessage")

        r = await self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        r.raise_for_status()


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def slugify_brand(brand: str) -> str:
    s = brand.strip().lower()
    s = re.sub(r"[’'`]+", "", s)
    s = re.sub(r"\s+", "-", s)
    return quote(s, safe="-")


def clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def extract_price(text: str) -> int | None:
    # Беремо першу суму перед "грн".
    m = re.search(r"(\d[\d\s\xa0.,]{0,15})\s*грн", text, re.I)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def parse_size_token(token: str) -> float | None:
    token = token.replace(",", ".").strip()
    try:
        x = float(token)
    except ValueError:
        return None
    if 30 <= x <= 50:
        return x
    return None


def extract_sizes(text: str) -> list[float]:
    """
    Спочатку читаємо поле "Розмір/Размер" біля характеристик товару.
    Це важливо для Shafa, де нижче на сторінці є "схожі товари"
    з чужими розмірами.
    """
    found: set[float] = set()

    field = re.search(
        r"(?:розмір|размер)\s*:?\s*(.{0,180}?)"
        r"(?=(?:стан|состояние|колір|цвет|категор|матеріал|материал|"
        r"купити|купить|додати|добавить|опис|описание|$))",
        text,
        re.I,
    )
    if field:
        for token in re.findall(r"(?<!\d)(\d{2}(?:[.,]5)?)(?!\d)", field.group(1)):
            x = parse_size_token(token)
            if x is not None:
                found.add(x)
        if found:
            return sorted(found)

    # Фолбек для OLX/нестандартної розмітки — лише рання частина сторінки.
    early = text[:2500]
    patterns = [
        r"(?:розмір|размер)\s*[:\-]?\s*((?:EU|UA|US)?\s*\d{2}(?:[.,]5)?)",
        r"\b(?:EU|UA)\s*(\d{2}(?:[.,]5)?)\b",
        r"\b(\d{2}(?:[.,]5)?)\s*(?:р\.?|розмір|размер)\b",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, early, re.I):
            token = m.group(1)
            token = re.sub(r"(?i)\b(?:EU|UA|US)\b", "", token).strip()
            x = parse_size_token(token)
            if x is not None:
                found.add(x)
    return sorted(found)


def extract_condition(text: str) -> str | None:
    # Пріоритет — саме поле "Стан/Состояние", а не текст схожих товарів.
    field = re.search(
        r"(?:стан|состояние)\s*:?\s*(.{0,80}?)"
        r"(?=(?:розмір|размер|колір|цвет|матеріал|материал|категор|$))",
        text,
        re.I,
    )
    scope = field.group(1).lower() if field else text[:1800].lower()

    # Спочатку точніші/довші варіанти.
    for phrase in sorted(CONDITION_ALIASES, key=len, reverse=True):
        if phrase in scope:
            return CONDITION_ALIASES[phrase]
    return None


def extract_meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return None


def nearest_card_text(anchor) -> str:
    node = anchor
    best = anchor.get_text(" ", strip=True)
    for _ in range(7):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if len(text) > 1500:
            break
        if "грн" in text.lower():
            best = text
    return re.sub(r"\s+", " ", best).strip()


def nearest_image(anchor, base_url: str) -> str | None:
    node = anchor
    for _ in range(6):
        if node is None:
            break
        img = node.find("img") if hasattr(node, "find") else None
        if img:
            src = img.get("src") or img.get("data-src") or img.get("srcset")
            if src:
                if "," in src and " " in src:
                    src = src.split(",")[0].split(" ")[0]
                return urljoin(base_url, src)
        node = getattr(node, "parent", None)
    return None


def olx_id_from_url(url: str) -> str:
    m = re.search(r"-ID([A-Za-z0-9]+)\.html", url)
    if m:
        return m.group(1)
    return hashlib.sha1(clean_url(url).encode()).hexdigest()[:20]


def shafa_id_from_url(url: str) -> str:
    m = re.search(r"/(\d{7,})-", url)
    if m:
        return m.group(1)
    return hashlib.sha1(clean_url(url).encode()).hexdigest()[:20]


def parse_olx_search(html: str, brand: str, limit: int) -> list[SearchCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchCandidate] = []
    seen_urls: set[str] = set()

    anchors = soup.select('a[href*="/d/"][href*="obyavlenie"]')
    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        url = clean_url(urljoin("https://www.olx.ua", href))
        if url in seen_urls:
            continue
        seen_urls.add(url)

        card_text = nearest_card_text(a)
        title = a.get_text(" ", strip=True) or card_text[:160]
        image = nearest_image(a, url)
        out.append(
            SearchCandidate(
                source="olx",
                brand_query=brand,
                listing_id=olx_id_from_url(url),
                url=url,
                title=title,
                price=extract_price(card_text),
                image=image,
            )
        )
        if len(out) >= limit:
            break
    return out


SHAFA_PRODUCT_RE = re.compile(
    r"/(?:uk/)?women/zhenskaya-obuv/[^/]+/\d{7,}-",
    re.I,
)


def parse_shafa_search(html: str, brand: str, limit: int) -> list[SearchCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchCandidate] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if not SHAFA_PRODUCT_RE.search(href):
            continue

        url = clean_url(urljoin("https://shafa.ua", href))
        if url in seen_urls:
            continue
        seen_urls.add(url)

        card_text = nearest_card_text(a)
        title = a.get_text(" ", strip=True) or card_text[:160]
        image = nearest_image(a, url)
        out.append(
            SearchCandidate(
                source="shafa",
                brand_query=brand,
                listing_id=shafa_id_from_url(url),
                url=url,
                title=title,
                price=extract_price(card_text),
                image=image,
            )
        )
        if len(out) >= limit:
            break

    return out


def parse_listing_detail(candidate: SearchCandidate, html: str) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    text = "\n".join(soup.stripped_strings)
    text_compact = re.sub(r"\s+", " ", text)

    h1 = soup.find("h1")
    title = (
        h1.get_text(" ", strip=True) if h1
        else extract_meta(soup, "og:title")
        or candidate.title
    )
    title = re.sub(r"\s+", " ", title).strip()

    image = extract_meta(soup, "og:image") or candidate.image
    price = candidate.price if candidate.price is not None else extract_price(text_compact)

    # Для розміру/стану детальна сторінка надійніша за картку каталогу.
    sizes = extract_sizes(text_compact)
    condition = extract_condition(text_compact)

    # Бренд у разі сумнівів беремо із заданого пошукового бренду:
    # задача — не "довіряти" полю бренду продавця, а знайти оголошення,
    # що вийшло у видачі конкретного бренду.
    brand = candidate.brand_query

    return Listing(
        source=candidate.source,
        listing_id=candidate.listing_id,
        url=candidate.url,
        title=title,
        brand=brand,
        price=price,
        sizes=sizes,
        condition=condition,
        image=image,
        raw_text=text_compact,
        priority=candidate.priority,
    )


def keyword_match(text: str, includes: Iterable[str], excludes: Iterable[str]) -> bool:
    lower = text.lower()
    if any(x.lower() in lower for x in excludes):
        return False
    includes = list(includes)
    if not includes:
        return True
    return any(x.lower() in lower for x in includes)


def size_match(
    found_sizes: list[float],
    wanted_sizes: list[float],
    min_size: float | None = None,
    max_size: float | None = None,
) -> bool:
    """
    Підтримує або точний список розмірів, або діапазон.
    Для діапазону 36–41 пройдуть також 36.5, 37.5, 40.5 тощо.
    """
    if not found_sizes:
        # Якщо розмір взагалі не вдалося зчитати — не надсилаємо,
        # бо користувачу потрібен чіткий фільтр 36–41.
        return False

    if min_size is not None or max_size is not None:
        lo = float(min_size if min_size is not None else -10**9)
        hi = float(max_size if max_size is not None else 10**9)
        return any(lo <= found <= hi for found in found_sizes)

    if not wanted_sizes:
        return True

    return any(
        abs(found - wanted) < 0.01
        for found in found_sizes
        for wanted in wanted_sizes
    )


def listing_matches(item: Listing, filters: dict) -> bool:
    combined = f"{item.title} {item.raw_text[:2500]}"

    if not keyword_match(
        combined,
        filters.get("include_keywords", []),
        filters.get("exclude_keywords", []),
    ):
        return False

    min_price = int(filters.get("min_price_uah", 0) or 0)
    max_price = int(filters.get("max_price_uah", 10**9) or 10**9)
    if item.price is None or not (min_price <= item.price <= max_price):
        return False

    wanted_sizes = [float(x) for x in filters.get("sizes", [])]
    min_size = filters.get("min_size")
    max_size = filters.get("max_size")
    if not size_match(item.sizes, wanted_sizes, min_size, max_size):
        return False

    # Якщо заданий allow-list станів — працюємо як раніше.
    wanted_conditions = set(filters.get("conditions", []) or [])
    if wanted_conditions:
        if item.condition is None or item.condition not in wanted_conditions:
            return False

    # Для цього resale-профілю важливіше інше:
    # відсікаємо ТІЛЬКИ те, що сайт/оголошення явно позначило як "new".
    # Якщо стан не вдалося розпізнати — оголошення не губимо.
    excluded_conditions = set(filters.get("exclude_conditions", []) or [])
    if item.condition is not None and item.condition in excluded_conditions:
        return False

    return True


def format_size(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x).replace(".", ",")


def condition_label(c: str | None) -> str:
    return {
        "new": "нове",
        "used": "вживане",
        "ideal": "ідеальний",
        "very_good": "дуже хороший",
        "good": "хороший",
        "satisfactory": "задовільний",
        None: "—",
    }.get(c, c or "—")


async def discover_shafa_brand_id(web: WebClient, brand: str) -> int | None:
    slug = slugify_brand(brand)
    urls = [
        f"https://shafa.ua/uk/brands/{slug}/women",
        f"https://shafa.ua/brands/{slug}/women",
    ]

    for url in urls:
        try:
            html = await web.get(url)
        except Exception:
            logging.exception("Shafa brand page failed: %s", url)
            continue

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "zhenskaya-obuv" not in href:
                continue
            decoded = href.replace("%3D", "=").replace("%3d", "=")
            m = re.search(r"brands=(\d+)", decoded)
            if m:
                return int(m.group(1))

        # Фолбек: шукаємо brand ID у всьому HTML.
        for pattern in [
            r"brands%3D(\d+)",
            r"brands=(\d+)",
        ]:
            m = re.search(pattern, html, re.I)
            if m:
                return int(m.group(1))

    return None



def normalize_brand_specs(cfg: dict) -> list[dict]:
    raw = cfg["filters"].get("brands", [])
    specs: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if name:
                specs.append({
                    "name": name,
                    "priority": False,
                    "olx_queries": [name],
                })
            continue

        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            queries = item.get("olx_queries") or [name]
            queries = [str(q).strip() for q in queries if str(q).strip()]
            if name not in queries:
                queries.insert(0, name)
            specs.append({
                "name": name,
                "priority": bool(item.get("priority", False)),
                "olx_queries": list(dict.fromkeys(queries)),
            })
    return specs


def current_cycle_index() -> int:
    # GitHub increments GITHUB_RUN_NUMBER automatically.
    raw = os.getenv("GITHUB_RUN_NUMBER", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    # Local/manual fallback: a new batch every five minutes.
    return int(time.time() // 300)


def choose_brand_batch(db: SeenState, cfg: dict, specs: list[dict]) -> list[dict]:
    if not specs:
        return []

    per_cycle = max(
        1,
        min(
            len(specs),
            int(cfg["monitor"].get("brands_per_cycle", len(specs)))
        )
    )
    cycle = current_cycle_index()
    cursor = ((cycle - 1) * per_cycle) % len(specs)
    return [specs[(cursor + i) % len(specs)] for i in range(per_cycle)]


async def cached_shafa_brand_id(
    web: WebClient,
    db: SeenState,
    cfg: dict,
    brand: str,
) -> int | None:
    overrides = cfg.get("shafa_brand_ids", {}) or {}
    if brand in overrides:
        return int(overrides[brand])

    key = f"shafa_brand_id:{brand.lower()}"
    cached = db.get_meta(key)
    if cached:
        try:
            return int(cached)
        except ValueError:
            pass

    # If discovery fails we do not permanently cache NONE:
    # a temporary Shafa error should not disable a brand forever.
    brand_id = await discover_shafa_brand_id(web, brand)
    if brand_id:
        db.set_meta(key, str(brand_id))
    return brand_id


async def collect_candidates(
    web: WebClient,
    db: SeenState,
    cfg: dict,
) -> list[SearchCandidate]:
    specs = normalize_brand_specs(cfg)
    specs = choose_brand_batch(db, cfg, specs)
    max_items = int(cfg["monitor"].get("max_items_per_brand", 20))
    candidates: list[SearchCandidate] = []
    completed_keys: set[tuple[str, str]] = set()

    if not specs:
        return [], completed_keys

    logging.info(
        "This cycle brands: %s",
        ", ".join(x["name"] for x in specs),
    )

    if cfg.get("sources", {}).get("olx", True):
        for spec in specs:
            brand = spec["name"]

            # Щоб не множити запити на кожному циклі, для брендів із кількома
            # варіантами написання OLX-аліаси ротуються між перевірками.
            queries = spec["olx_queries"]
            alias_cursor = current_cycle_index() % len(queries)
            query = queries[alias_cursor]

            slug = slugify_brand(query)
            url = (
                "https://www.olx.ua/uk/moda-i-stil/zhenskaya-obuv/"
                f"q-{slug}/?search%5Border%5D=created_at%3Adesc"
            )
            try:
                html = await web.get(url)
                found = parse_olx_search(html, brand, max_items)
                for c in found:
                    c.priority = spec["priority"]
                logging.info(
                    "OLX %s [%s]: %d candidates",
                    brand, query, len(found)
                )
                candidates.extend(found)
                completed_keys.add(("olx", brand))
            except Exception:
                logging.exception("OLX search failed for %s [%s]", brand, query)

    if cfg.get("sources", {}).get("shafa", True):
        for spec in specs:
            brand = spec["name"]
            brand_id = await cached_shafa_brand_id(web, db, cfg, brand)

            if not brand_id:
                logging.warning(
                    "Shafa: cannot determine brand ID for %s",
                    brand,
                )
                continue

            url = (
                "https://shafa.ua/uk/women/zhenskaya-obuv/if/"
                f"brands%3D{int(brand_id)}?sort=4"
            )
            try:
                html = await web.get(url)
                found = parse_shafa_search(html, brand, max_items)
                for c in found:
                    c.priority = spec["priority"]
                logging.info("Shafa %s: %d candidates", brand, len(found))
                candidates.extend(found)
                completed_keys.add(("shafa", brand))
            except Exception:
                logging.exception("Shafa search failed for %s", brand)

    deduped = {}
    for c in candidates:
        key = (c.source, c.listing_id)
        # Якщо оголошення знайшлося в кількох запитах, пріоритет не губимо.
        if key in deduped:
            deduped[key].priority = deduped[key].priority or c.priority
        else:
            deduped[key] = c
    return list(deduped.values()), completed_keys



async def one_check(
    web: WebClient,
    notifier: TelegramNotifier,
    db: SeenState,
    cfg: dict,
):
    candidates, completed_keys = await collect_candidates(web, db, cfg)
    send_existing = bool(cfg["monitor"].get("send_existing_on_first_run", False))

    # При великому списку брендів вони перевіряються пакетами.
    # Тому baseline ведемо ОКРЕМО для кожного source + brand.
    # Це гарантує, що другий/третій пакет не засипле Telegram старими товарами.
    baseline_candidates: set[tuple[str, str]] = set()

    if not send_existing:
        by_key: dict[tuple[str, str], list[SearchCandidate]] = {}
        for c in candidates:
            by_key.setdefault((c.source, c.brand_query), []).append(c)

        for source, brand in completed_keys:
            meta_key = f"baseline:{source}:{brand.lower()}"
            if db.get_meta(meta_key) == "1":
                continue

            for c in by_key.get((source, brand), []):
                db.mark(c.source, c.listing_id, c.url)

            db.set_meta(meta_key, "1")
            baseline_candidates.add((source, brand))
            logging.info(
                "Baseline created for %s / %s (%d listings)",
                source,
                brand,
                len(by_key.get((source, brand), [])),
            )

    new_candidates = [
        c for c in candidates
        if (c.source, c.brand_query) not in baseline_candidates
        and not db.has(c.source, c.listing_id)
    ]

    logging.info("New candidates after baseline: %d", len(new_candidates))

    for c in reversed(new_candidates):
        try:
            detail_html = await web.get(c.url)
            item = parse_listing_detail(c, detail_html)

            if listing_matches(item, cfg["filters"]):
                await notifier.send(item)
                logging.info("SENT: %s | %s", item.source, item.title)
            else:
                logging.info("Filtered out: %s | %s", item.source, item.title)

        except Exception:
            # При технічній помилці не позначаємо оголошення як seen:
            # наступний цикл спробує ще раз.
            logging.exception("Failed to process %s", c.url)
            continue

        db.mark(c.source, c.listing_id, c.url)


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable"
        )

    cfg = load_config()
    timeout = float(cfg["monitor"].get("timeout_seconds", 20))
    delay = max(0.7, float(cfg["monitor"].get("request_delay_seconds", 1.0)))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    db = SeenState(STATE_PATH)
    web = WebClient(timeout=timeout, request_delay=delay)
    notifier = TelegramNotifier(token, chat_id)

    brand_specs = normalize_brand_specs(cfg)
    logging.info("Luxury Shoes Monitor — GitHub one-shot run")
    logging.info("Total brands configured: %d", len(brand_specs))
    logging.info("GitHub run/cycle: %d", current_cycle_index())

    try:
        # One-time confirmation that GitHub + Telegram are connected.
        if db.get_meta("startup_notified") != "1":
            await notifier.send_text(
                "✅ Luxury Shoes Monitor підключено. "
                "Моніторинг OLX + Shafa запущений. "
                "Перші проходи створять базу поточних оголошень, "
                "після чого я надсилатиму тільки нові."
            )
            db.set_meta("startup_notified", "1")

        await one_check(web, notifier, db, cfg)

    finally:
        db.save()
        await web.close()
        await notifier.close()


if __name__ == "__main__":
    asyncio.run(main())
