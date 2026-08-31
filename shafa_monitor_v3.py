from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"

PRODUCT_RE = re.compile(
    r"/(?:uk/)?women/zhenskaya-obuv/[^/]+/(\d{7,})-[^/?#]+",
    re.I,
)

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
}

DEFAULT_INCLUDE = (
    "туфл", "човник", "лодоч", "pump", "підбор", "подбор", "heel",
    "лофер", "loafer", "балет", "ballerina", "ballet", "slingback",
    "слінгбек", "mary jane", "мері джейн", "дербі", "derby",
    "оксфорд", "oxford", "брог", "brogue", "мокасин", "moccasin",
    "мюлі", "mule", "сабо", "clog", "босоніж", "босонож", "sandal",
    "чобіт", "чобот", "сапог", "boot", "ботфорт", "напівчоб",
    "полусап", "ботильйон", "ботильон", "ботільйон", "черевик",
    "ботин", "ботін", "челсі", "chelsea",
)

DEFAULT_EXCLUDE = (
    "кросів", "кроссов", "sneaker", "кеди", "кеды", "trainer",
    "уггі", "угги", "ugg", "crocs", "шльоп", "шлеп", "flip flop",
    "flip-flop", "в'єтнам", "вьетнам",
)


@dataclass
class Item:
    listing_id: str
    url: str
    title: str
    brand: str
    priority: bool
    price: int | None
    sizes: list[float]
    card_text: str
    image: str | None = None


class State:
    def __init__(self):
        self.data = {"seen": {"shafa": {}}, "meta": {}}

        if STATE_PATH.exists():
            try:
                loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
            except Exception:
                logging.exception("Cannot read state.json")

        self.data.setdefault("seen", {})
        self.data["seen"].setdefault("shafa", {})
        self.data.setdefault("meta", {})

    def has(self, listing_id: str) -> bool:
        return listing_id in self.data["seen"]["shafa"]

    def mark(self, listing_id: str):
        self.data["seen"]["shafa"][listing_id] = int(time.time())

    def get_meta(self, key: str) -> str | None:
        value = self.data["meta"].get(key)
        return str(value) if value is not None else None

    def set_meta(self, key: str, value):
        self.data["meta"][key] = str(value)

    def save(self):
        bucket = self.data["seen"]["shafa"]

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
    return yaml.safe_load(
        CONFIG_PATH.read_text(encoding="utf-8")
    )


def normalize_brands(cfg: dict) -> list[dict]:
    result = []

    for raw in cfg.get("filters", {}).get("brands", []):
        if isinstance(raw, str):
            name = raw.strip()
            priority = False
        else:
            name = str(raw.get("name", "")).strip()
            priority = bool(raw.get("priority", False))

        if name and not any(x["name"] == name for x in result):
            result.append(
                {
                    "name": name,
                    "priority": priority,
                }
            )

    return result


def slugify(name: str) -> str:
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]

    text = unicodedata.normalize("NFKD", name)
    text = "".join(
        ch for ch in text
        if not unicodedata.combining(ch)
    )
    text = (
        text.lower()
        .replace("&", " ")
        .replace("'", "")
        .replace("’", "")
    )

    return re.sub(
        r"[^a-z0-9]+",
        "-",
        text,
    ).strip("-")


def parse_price(text: str) -> int | None:
    match = re.search(
        r"(\d[\d\s\xa0.,]{0,12})\s*грн",
        text,
        re.I,
    )

    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def parse_sizes(text: str, title: str) -> list[float]:
    result = set()

    for match in re.finditer(
        r"\b(?:EU|UA)\s*(\d{2}(?:[.,]5)?)",
        text,
        re.I,
    ):
        try:
            value = float(
                match.group(1).replace(",", ".")
            )
        except ValueError:
            continue

        if 30 <= value <= 46:
            result.add(value)

    for match in re.finditer(
        r"(?:розмір|размер|size|р\.?)\s*[:\-]?\s*(\d{2}(?:[.,]5)?)",
        title,
        re.I,
    ):
        try:
            value = float(
                match.group(1).replace(",", ".")
            )
        except ValueError:
            continue

        if 30 <= value <= 46:
            result.add(value)

    if not result:
        compact = re.sub(r"\s+", " ", text)
        low_card = compact.lower()
        low_title = re.sub(
            r"\s+",
            " ",
            title,
        ).lower()
        pos = low_card.find(low_title)

        if pos >= 0:
            tail = compact[
                pos + len(title):
                pos + len(title) + 100
            ]

            for match in re.finditer(
                r"(?<!\d)(3[4-9]|4[0-6])(?:[.,]5)?(?!\d)",
                tail,
            ):
                token = match.group(0).replace(",", ".")

                try:
                    value = float(token)
                except ValueError:
                    continue

                if 30 <= value <= 46:
                    result.add(value)
                    break

    return sorted(result)


def nearest_card_text(anchor) -> str:
    node = anchor
    best = re.sub(
        r"\s+",
        " ",
        anchor.get_text(" ", strip=True),
    )

    for _ in range(7):
        node = getattr(node, "parent", None)

        if node is None:
            break

        text = re.sub(
            r"\s+",
            " ",
            node.get_text(" ", strip=True),
        )

        if len(text) > 1400:
            break

        if "грн" in text.lower():
            best = text

    return best


def nearest_image(anchor) -> str | None:
    node = anchor

    for _ in range(6):
        if node is None:
            break

        img = (
            node.find("img")
            if hasattr(node, "find")
            else None
        )

        if img:
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
            )

            if not src and img.get("srcset"):
                src = (
                    str(img.get("srcset"))
                    .split(",")[0]
                    .strip()
                    .split(" ")[0]
                )

            if src and not str(src).startswith("data:"):
                return urljoin(
                    "https://shafa.ua",
                    str(src),
                )

        node = getattr(node, "parent", None)

    return None


def parse_brand_page(
    html: str,
    brand: str,
    priority: bool,
    limit: int,
) -> list[Item]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    items = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = PRODUCT_RE.search(href)

        if not match:
            continue

        url = urljoin(
            "https://shafa.ua",
            href.split("?")[0],
        )

        if url in seen_urls:
            continue

        seen_urls.add(url)

        title = re.sub(
            r"\s+",
            " ",
            anchor.get_text(" ", strip=True),
        ).strip()

        if not title:
            img = anchor.find("img")
            if img:
                title = str(
                    img.get("alt") or ""
                ).strip()

        if not title:
            continue

        card = nearest_card_text(anchor)

        items.append(
            Item(
                listing_id=match.group(1),
                url=url,
                title=title,
                brand=brand,
                priority=priority,
                price=parse_price(card),
                sizes=parse_sizes(card, title),
                card_text=card,
                image=nearest_image(anchor),
            )
        )

        if len(items) >= limit:
            break

    return items


def keywords(filters: dict, key: str, default) -> tuple[str, ...]:
    raw = filters.get(key, [])

    if not raw:
        return tuple(x.lower() for x in default)

    return tuple(
        str(x).strip().lower()
        for x in raw
        if str(x).strip()
    )


def matches_card(
    item: Item,
    filters: dict,
) -> bool:
    text = (
        f"{item.title} {item.card_text}"
        .lower()
    )

    include = keywords(
        filters,
        "include_keywords",
        DEFAULT_INCLUDE,
    )
    exclude = keywords(
        filters,
        "exclude_keywords",
        DEFAULT_EXCLUDE,
    )

    if any(word in text for word in exclude):
        return False

    if not any(word in text for word in include):
        return False

    if item.price is None:
        return False

    min_price = int(
        filters.get(
            "min_price_uah",
            300,
        )
    )
    max_price = int(
        filters.get(
            "max_price_uah",
            4000,
        )
    )

    if not min_price <= item.price <= max_price:
        return False

    min_size = float(
        filters.get(
            "min_size",
            36,
        )
    )
    max_size = float(
        filters.get(
            "max_size",
            41,
        )
    )

    if not item.sizes:
        return False

    return any(
        min_size <= size <= max_size
        for size in item.sizes
    )


def explicitly_new(
    detail_text: str,
    title: str,
) -> bool:
    compact = re.sub(
        r"\s+",
        " ",
        detail_text,
    )

    match = re.search(
        r"(?:стан|состояние)\s*:?\s*(.{0,80}?)"
        r"(?=(?:розмір|размер|колір|цвет|матеріал|материал|"
        r"категор|опис|описание|$))",
        compact,
        re.I,
    )

    if match:
        state = match.group(1).lower()

        if re.search(r"\bнов", state):
            return True

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

    return bool(
        re.search(
            r"\b(нові|новий|нова|нове|новые|новая|новое)\b",
            title.lower(),
        )
    )


class Telegram:
    def __init__(
        self,
        token: str,
        chat_id: str,
    ):
        self.base = (
            f"https://api.telegram.org/bot{token}"
        )
        self.chat_id = chat_id
        self.client = httpx.Client(timeout=30)

    def send_item(self, item: Item):
        price = (
            f"{item.price:,}"
            .replace(",", " ")
            + " грн"
            if item.price is not None
            else "—"
        )

        sizes = ", ".join(
            str(int(x))
            if x.is_integer()
            else str(x).replace(".", ",")
            for x in item.sizes
        )

        hot = "🔥 " if item.priority else ""

        text = (
            f"🆕 {hot}SHAFA\n"
            f"{item.title}\n\n"
            f"Бренд: {item.brand}\n"
            f"Ціна: {price}\n"
            f"Розмір: {sizes or '—'}\n"
            f"{item.url}"
        )

        if item.image:
            response = self.client.post(
                f"{self.base}/sendPhoto",
                data={
                    "chat_id": self.chat_id,
                    "photo": item.image,
                    "caption": text[:1024],
                },
            )

            if response.is_success:
                return

        response = self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()

    def close(self):
        self.client.close()


def make_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1800")
    options.add_argument("--lang=uk-UA")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    )
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(
        options=options,
    )
    driver.set_page_load_timeout(16)

    return driver


def load_rendered(
    driver,
    url: str,
    wait: float = 1.0,
) -> str:
    driver.get(url)
    time.sleep(wait)

    try:
        driver.execute_script(
            "window.scrollTo(0, 1200);"
        )
        time.sleep(0.35)
    except Exception:
        pass

    return driver.page_source


def catalog_urls(brand: str) -> list[str]:
    slug = slugify(brand)

    # The dedicated all-footwear SEO page is the best source because it
    # includes pumps, loafers, sandals, boots, ankle boots, etc.  The mixed
    # women brand page is only a fallback.
    return [
        f"https://shafa.ua/uk/obuv-{slug}.xhtml?sort=4",
        f"https://shafa.ua/uk/brands/{slug}/women?sort=4",
        f"https://shafa.ua/uk/tufli-{slug}.xhtml?sort=4",
        f"https://shafa.ua/uk/zhenskie-tufli-{slug}.xhtml?sort=4",
    ]


def main():
    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
        )

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | %(levelname)s | "
            "%(message)s"
        ),
    )

    cfg = load_config()
    filters = cfg.get("filters", {})
    brands = normalize_brands(cfg)

    if not brands:
        raise SystemExit(
            "No brands configured"
        )

    monitor_cfg = cfg.get(
        "monitor",
        {},
    )

    max_items = max(
        20,
        int(
            monitor_cfg.get(
                "max_items_per_brand",
                20,
            )
        ),
    )

    # GitHub job timeout is 12 minutes. Stop the browser scan well before
    # that so the following workflow step always has time to commit state.json.
    run_budget_seconds = 510.0

    state = State()

    try:
        start_index = int(
            state.get_meta(
                "browser_next_brand_index"
            )
            or 0
        )
    except ValueError:
        start_index = 0

    start_index %= len(brands)

    logging.info(
        "Browser Shafa cycle | start=%s | max runtime=%ss",
        start_index,
        int(run_budget_seconds),
    )

    telegram = Telegram(
        token,
        chat_id,
    )
    driver = make_driver()
    started = time.monotonic()
    processed = 0

    try:
        # Try to cover the entire list each scheduled run. If the time budget
        # is reached, save the exact next brand and resume there next run.
        for offset in range(len(brands)):
            if (
                processed > 0
                and time.monotonic() - started
                >= run_budget_seconds
            ):
                logging.info(
                    "Time budget reached after %s brands; "
                    "will resume from index %s",
                    processed,
                    (
                        start_index + processed
                    ) % len(brands),
                )
                break

            brand_index = (
                start_index + offset
            ) % len(brands)

            spec = brands[brand_index]
            brand = spec["name"]
            items = []

            for url in catalog_urls(brand):
                try:
                    html = load_rendered(
                        driver,
                        url,
                    )

                    items = parse_brand_page(
                        html,
                        brand,
                        spec["priority"],
                        max_items,
                    )

                    logging.info(
                        "%s | %s products | %s",
                        brand,
                        len(items),
                        url,
                    )

                    if items:
                        break

                except Exception:
                    logging.exception(
                        "Render failed: %s",
                        url,
                    )

            next_index = (
                brand_index + 1
            ) % len(brands)

            if not items:
                logging.warning(
                    "No rendered Shafa listings for %s",
                    brand,
                )
                state.set_meta(
                    "browser_next_brand_index",
                    next_index,
                )
                state.save()
                processed += 1
                continue

            baseline_key = (
                "browser_baseline_v3:"
                + slugify(brand)
            )

            # First browser visit for this brand only memorises the current
            # listings. There are no TEST messages.
            if state.get_meta(
                baseline_key
            ) != "1":
                for item in items:
                    state.mark(
                        item.listing_id
                    )

                state.set_meta(
                    baseline_key,
                    "1",
                )
                state.set_meta(
                    "browser_next_brand_index",
                    next_index,
                )
                state.save()
                processed += 1

                logging.info(
                    "Baseline saved for %s: %s listings",
                    brand,
                    len(items),
                )
                continue

            for item in reversed(items):
                if state.has(
                    item.listing_id
                ):
                    continue

                # Mark in memory first. It is persisted only after the brand
                # finishes, so Telegram failures are retried on the next run.
                state.mark(
                    item.listing_id
                )

                if not matches_card(
                    item,
                    filters,
                ):
                    continue

                try:
                    detail_html = load_rendered(
                        driver,
                        item.url,
                        wait=0.65,
                    )

                    detail_text = " ".join(
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
                            "Skipped NEW: %s",
                            item.title,
                        )
                        continue

                except Exception:
                    # If Shafa's detail page is temporarily unavailable, do
                    # not lose a potentially valuable listing.
                    logging.exception(
                        "Condition check failed: %s",
                        item.url,
                    )

                telegram.send_item(
                    item
                )

                logging.info(
                    "SENT %s | %s | %s грн",
                    brand,
                    item.title,
                    item.price,
                )

            state.set_meta(
                "browser_next_brand_index",
                next_index,
            )
            state.save()
            processed += 1

        state.set_meta(
            "browser_monitor_v3_active",
            "1",
        )
        state.save()

        logging.info(
            "Cycle finished | processed=%s | next index=%s",
            processed,
            state.get_meta(
                "browser_next_brand_index"
            ),
        )

    finally:
        driver.quit()
        telegram.close()


if __name__ == "__main__":
    main()
