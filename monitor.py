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
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
    "Mobile/15E148 Safari/604.1"
)

# Shafa SEO pages use transliterated brand names.
SLUG_OVERRIDES = {
    "Dolce & Gabbana": "dolce-gabbana",
    "Louis Vuitton": "louis-vuitton",
    "Golden Goose": "golden-goose",
    "Brunello Cucinelli": "brunello-cucinelli",
    "Bottega Veneta": "bottega-veneta",
    "Jil Sander": "jil-sander",
    "Acne Studios": "acne-studios",
    "Alexander McQueen": "alexander-mcqueen",
    "Cesare Paciotti": "cesare-paciotti",
    "Giuseppe Zanotti": "giuseppe-zanotti",
    "Gianmarco Lorenzi": "gianmarco-lorenzi",
    "Gianvito Rossi": "gianvito-rossi",
    "Sergio Rossi": "sergio-rossi",
    "John Fluevog": "john-fluevog",
    "Saint Laurent": "saint-laurent",
    "Vivienne Westwood": "vivienne-westwood",
    "Miu Miu": "miu-miu",
    "Christian Louboutin": "christian-louboutin",
    "The Row": "the-row",
    "Maison Margiela": "maison-margiela",
    "Roger Vivier": "roger-vivier",
    "Loro Piana": "loro-piana",
    "Rene Caovilla": "rene-caovilla",
    "Tod's": "tods",
    "Ferragamo": "salvatore-ferragamo",
    "Hermès": "hermes",
    "Alaïa": "alaia",
    "Chloé": "chloe",
    "Celine": "celine",
}

PRODUCT_RE = re.compile(
    r"/(?:uk/)?women/zhenskaya-obuv/[^/]+/(\d{7,})-[^/?#]+",
    re.I,
)

# Extra footwear words so useful listings aren't missed even if config changes.
EXTRA_INCLUDE = (
    "туфл", "човник", "лодоч", "pump", "підбор", "подбор", "heel",
    "лофер", "loafer", "балет", "ballerina", "ballet",
    "slingback", "слінгбек", "mary jane", "мері джейн",
    "дербі", "derby", "оксфорд", "oxford", "брог", "brogue",
    "мокасин", "moccasin", "мюлі", "mule", "сабо", "clog",
    "босоніж", "босонож", "sandal",
    "чобіт", "чобот", "сапог", "boot", "ботфорт",
    "напівчоб", "полусап", "ботильйон", "ботильон", "ботільйон",
    "черевик", "ботин", "ботін", "челсі", "chelsea",
)

EXTRA_EXCLUDE = (
    "кросів", "кроссов", "sneaker", "кеди", "кеды",
    "trainer", "уггі", "угги", "ugg", "crocs",
    "шльоп", "шлеп", "flip flop", "flip-flop", "в'єтнам", "вьетнам",
)

NEW_MARKERS = (
    "новий", "нова ", "нові ", "новая", "новые", "новое",
    "brand new", "new with tags", "нові з бірк", "новые с бирк",
)


@dataclass
class Candidate:
    listing_id: str
    url: str
    brand: str
    title: str
    price: int | None
    sizes: list[float]
    image: str | None
    card_text: str
    priority: bool = False


class State:
    def __init__(self):
        self.data = {"seen": {"olx": {}, "shafa": {}}, "meta": {}}

        if STATE_PATH.exists():
            try:
                loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception:
                logging.exception("Could not read state.json")

        self.data.setdefault("seen", {})
        self.data["seen"].setdefault("olx", {})
        self.data["seen"].setdefault("shafa", {})
        self.data.setdefault("meta", {})

    def has(self, listing_id: str) -> bool:
        return listing_id in self.data["seen"]["shafa"]

    def mark(self, listing_id: str):
        self.data["seen"]["shafa"][listing_id] = int(time.time())

    def get_meta(self, key: str) -> str | None:
        return self.data["meta"].get(key)

    def set_meta(self, key: str, value: str):
        self.data["meta"][key] = str(value)

    def save(self):
        bucket = self.data["seen"]["shafa"]

        # Prevent state.json growing forever.
        if len(bucket) > 20000:
            self.data["seen"]["shafa"] = dict(
                sorted(
                    bucket.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:20000]
            )

        STATE_PATH.write_text(
            json.dumps(
                self.data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def normalize_brands(cfg: dict) -> list[dict]:
    result = []

    for item in cfg["filters"].get("brands", []):
        if isinstance(item, str):
            name = item.strip()
            if name:
                result.append({"name": name, "priority": False})
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                result.append(
                    {
                        "name": name,
                        "priority": bool(item.get("priority", False)),
                    }
                )

    return result


def slugify_brand(name: str) -> str:
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]

    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = s.replace("&", " ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def current_cycle() -> int:
    # GitHub Actions automatically increments this for every run.
    value = os.getenv("GITHUB_RUN_NUMBER", "").strip()
    if value.isdigit() and int(value) > 0:
        return int(value)

    return int(time.time() // 300)


def extract_price(text: str) -> int | None:
    m = re.search(r"(\d[\d\s\xa0.,]{0,15})\s*грн", text, re.I)

    if not m:
        return None

    digits = re.sub(r"\D", "", m.group(1))

    return int(digits) if digits else None


def _size_number(token: str) -> float | None:
    try:
        value = float(token.replace(",", "."))
    except ValueError:
        return None

    return value if 30 <= value <= 46 else None


def extract_sizes(card_text: str, title: str) -> list[float]:
    """
    Try to read Shafa's size value without confusing review counts with sizes.
    """
    result: set[float] = set()
    compact = re.sub(r"\s+", " ", card_text)

    # Most reliable: EU / UA prefix.
    for m in re.finditer(
        r"\b(?:EU|UA)\s*(\d{2}(?:[.,]5)?)\b",
        compact,
        re.I,
    ):
        value = _size_number(m.group(1))
        if value is not None:
            result.add(value)

    # Shafa often renders: "38 і ще 1".
    for m in re.finditer(
        r"\b(\d{2}(?:[.,]5)?)\s*(?:і\s+ще|и\s+еще)\b",
        compact,
        re.I,
    ):
        value = _size_number(m.group(1))
        if value is not None:
            result.add(value)

    # Size explicitly mentioned in the listing title.
    for m in re.finditer(
        r"(?:розмір|размер|size|р\.?)\s*[:\-]?\s*(\d{2}(?:[.,]5)?)",
        title,
        re.I,
    ):
        value = _size_number(m.group(1))
        if value is not None:
            result.add(value)

    # If there is still no size, inspect only text immediately after the title.
    if not result:
        low_card = compact.lower()
        low_title = re.sub(r"\s+", " ", title).lower()
        pos = low_card.find(low_title)

        if pos >= 0:
            tail = compact[pos + len(title): pos + len(title) + 90]

            for m in re.finditer(
                r"(?<!\d)(\d{2}(?:[.,]5)?)(?!\d)",
                tail,
            ):
                value = _size_number(m.group(1))
                if value is not None:
                    result.add(value)
                    break

    return sorted(result)


def nearest_card_text(anchor) -> str:
    node = anchor
    best = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))

    for _ in range(6):
        node = getattr(node, "parent", None)

        if node is None:
            break

        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))

        # Product cards are compact. Don't climb into the whole product grid.
        if len(text) > 1000:
            break

        if "грн" in text.lower():
            best = text

    return best


def nearest_image(anchor) -> str | None:
    node = anchor

    for _ in range(6):
        if node is None:
            break

        img = node.find("img") if hasattr(node, "find") else None

        if img:
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
            )

            if not src and img.get("srcset"):
                src = str(img.get("srcset")).split(",")[0].strip().split(" ")[0]

            if src:
                return urljoin("https://shafa.ua", src)

        node = getattr(node, "parent", None)

    return None


def parse_catalog(
    html: str,
    brand: str,
    priority: bool,
    limit: int,
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    used_urls = set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = PRODUCT_RE.search(href)

        if not m:
            continue

        url = urljoin("https://shafa.ua", href.split("?")[0])

        if url in used_urls:
            continue

        title = re.sub(
            r"\s+",
            " ",
            a.get_text(" ", strip=True),
        ).strip()

        if not title:
            continue

        used_urls.add(url)
        card_text = nearest_card_text(a)

        items.append(
            Candidate(
                listing_id=m.group(1),
                url=url,
                brand=brand,
                title=title,
                price=extract_price(card_text),
                sizes=extract_sizes(card_text, title),
                image=nearest_image(a),
                card_text=card_text,
                priority=priority,
            )
        )

        if len(items) >= limit:
            break

    return items


def desired_footwear(candidate: Candidate, filters: dict) -> bool:
    text = f"{candidate.title} {candidate.card_text}".lower()

    excludes = {
        str(x).lower()
        for x in filters.get("exclude_keywords", [])
    }
    excludes.update(EXTRA_EXCLUDE)

    if any(word in text for word in excludes):
        return False

    includes = {
        str(x).lower()
        for x in filters.get("include_keywords", [])
    }
    includes.update(EXTRA_INCLUDE)

    return any(word in text for word in includes)


def price_and_size_match(
    candidate: Candidate,
    filters: dict,
) -> bool:
    if candidate.price is None:
        return False

    min_price = int(filters.get("min_price_uah", 300))
    max_price = int(filters.get("max_price_uah", 4000))

    if not min_price <= candidate.price <= max_price:
        return False

    if not candidate.sizes:
        return False

    min_size = float(filters.get("min_size", 36))
    max_size = float(filters.get("max_size", 41))

    return any(min_size <= size <= max_size for size in candidate.sizes)


def explicitly_new(detail_text: str, title: str) -> bool:
    """
    Exclude only listings that clearly say the condition is new.
    """
    compact = re.sub(r"\s+", " ", detail_text)

    # Shafa's explicit state field.
    m = re.search(
        r"(?:стан|состояние)\s*:?\s*(.{0,80}?)"
        r"(?=(?:розмір|размер|колір|цвет|матеріал|материал|"
        r"категор|опис|описание|$))",
        compact,
        re.I,
    )

    if m:
        state = m.group(1).lower()

        if re.search(r"\bнов", state):
            return True

        # Recognised used conditions = not new.
        if any(
            marker in state
            for marker in (
                "ідеаль",
                "идеаль",
                "дуже хорош",
                "очень хорош",
                "хорош",
                "задов",
                "удов",
            )
        ):
            return False

    # Also catch obvious wording in title.
    low_title = title.lower()

    return any(marker in low_title for marker in NEW_MARKERS)


class WebClient:
    def __init__(self, timeout: float, delay: float):
        self.delay = max(0.8, delay)

        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    async def get(self, url: str) -> str:
        await asyncio.sleep(
            self.delay + random.uniform(0.15, 0.45)
        )

        r = await self.client.get(url)
        r.raise_for_status()

        return r.text

    async def close(self):
        await self.client.aclose()


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=30)

    async def send_listing(self, item: Candidate):
        price = (
            f"{item.price:,}".replace(",", " ") + " грн"
            if item.price is not None
            else "—"
        )

        sizes = ", ".join(
            str(int(x)) if x.is_integer()
            else str(x).replace(".", ",")
            for x in item.sizes
        )

        hot = "🔥 " if item.priority else ""

        caption = (
            f"🆕 {hot}<b>SHAFA</b>\n"
            f"<b>{html_lib.escape(item.title)}</b>\n\n"
            f"Бренд: {html_lib.escape(item.brand)}\n"
            f"Ціна: <b>{price}</b>\n"
            f"Розмір: {html_lib.escape(sizes or '—')}\n\n"
            f'<a href="{html_lib.escape(item.url, quote=True)}">'
            f"Відкрити оголошення</a>"
        )

        if item.image:
            photo = await self.client.post(
                f"{self.base}/sendPhoto",
                data={
                    "chat_id": self.chat_id,
                    "photo": item.image,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                },
            )

            if photo.is_success:
                return

        message = await self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        message.raise_for_status()

    async def send_text(self, text: str):
        r = await self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
            },
        )
        r.raise_for_status()

    async def close(self):
        await self.client.aclose()


def catalog_urls(brand: str) -> list[str]:
    slug = slugify_brand(brand)

    # Generic footwear page first. Category fallbacks cover brands for which
    # Shafa has not generated a generic SEO page.
    patterns = (
        f"obuv-{slug}.xhtml",
        f"tufli-{slug}.xhtml",
        f"zhenskie-tufli-{slug}.xhtml",
        f"botinki-{slug}.xhtml",
        f"sapozhki-{slug}.xhtml",
        f"bosonozhki-{slug}.xhtml",
        f"botilony-{slug}.xhtml",
        f"lofery-{slug}.xhtml",
    )

    return [
        f"https://shafa.ua/uk/{path}?sort=4"
        for path in patterns
    ]


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    cfg = load_config()
    filters = cfg["filters"]
    brands = normalize_brands(cfg)

    per_cycle = max(
        1,
        min(
            len(brands),
            int(
                cfg.get("monitor", {})
                .get("brands_per_cycle", 20)
            ),
        ),
    )

    cycle = current_cycle()
    start = ((cycle - 1) * per_cycle) % len(brands)
    batch = [
        brands[(start + i) % len(brands)]
        for i in range(per_cycle)
    ]

    state = State()

    web = WebClient(
        timeout=float(
            cfg.get("monitor", {})
            .get("timeout_seconds", 20)
        ),
        delay=float(
            cfg.get("monitor", {})
            .get("request_delay_seconds", 1.0)
        ),
    )

    telegram = Telegram(token, chat_id)

    max_items = max(
        40,
        int(
            cfg.get("monitor", {})
            .get("max_items_per_brand", 20)
        ),
    )

    try:
        logging.info(
            "SHAFA-only cycle %s | brands: %s",
            cycle,
            ", ".join(x["name"] for x in batch),
        )

        for spec in batch:
            brand = spec["name"]
            items: list[Candidate] = []

            for url in catalog_urls(brand):
                try:
                    page = await web.get(url)
                    found = parse_catalog(
                        page,
                        brand,
                        spec["priority"],
                        max_items,
                    )

                    logging.info(
                        "Shafa %s | %s | %d products",
                        brand,
                        url,
                        len(found),
                    )

                    if found:
                        items = found
                        break

                except httpx.HTTPStatusError as e:
                    # Many SEO fallback pages legitimately do not exist.
                    logging.info(
                        "Shafa fallback unavailable for %s: %s",
                        brand,
                        e.response.status_code,
                    )
                except Exception:
                    logging.exception(
                        "Shafa catalog failed for %s",
                        brand,
                    )

            if not items:
                logging.warning(
                    "No Shafa shoe page found for %s",
                    brand,
                )
                continue

            # Fresh baseline namespace for this rewritten Shafa monitor.
            baseline_key = (
                f"shafa_v4_baseline:{brand.lower()}"
            )

            if state.get_meta(baseline_key) != "1":
                for item in items:
                    state.mark(item.listing_id)

                state.set_meta(baseline_key, "1")

                logging.info(
                    "Baseline for %s: %d listings",
                    brand,
                    len(items),
                )
                continue

            for item in reversed(items):
                if state.has(item.listing_id):
                    continue

                # Mark immediately so an irrelevant new listing
                # isn't reprocessed forever.
                state.mark(item.listing_id)

                if not desired_footwear(item, filters):
                    continue

                if not price_and_size_match(item, filters):
                    continue

                try:
                    detail_html = await web.get(item.url)
                    detail_text = "\n".join(
                        BeautifulSoup(
                            detail_html,
                            "html.parser",
                        ).stripped_strings
                    )

                    if explicitly_new(
                        detail_text,
                        item.title,
                    ):
                        logging.info(
                            "Skipped NEW listing: %s",
                            item.title,
                        )
                        continue

                except Exception:
                    # Don't lose a potentially valuable listing just because
                    # one detail request temporarily failed.
                    logging.exception(
                        "Could not check condition: %s",
                        item.url,
                    )

                await telegram.send_listing(item)

                logging.info(
                    "SENT: %s | %s | %s грн",
                    brand,
                    item.title,
                    item.price,
                )

        # One-time confirmation specifically for the new Shafa version.
        if state.get_meta("shafa_v4_ready_notified") != "1":
            await telegram.send_text(
                "✅ Shafa Monitor активовано.\n"
                "Фільтри: luxury-бренди, 36–41, 300–4000 грн, "
                "усе крім явно нового стану.\n"
                "Перші проходи запам’ятовують поточні оголошення; "
                "далі надходитимуть нові."
            )
            state.set_meta("shafa_v4_ready_notified", "1")

        state.save()

    finally:
        await web.close()
        await telegram.close()


if __name__ == "__main__":
    asyncio.run(main())
