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

FOOTWEAR_WORDS = (
    "туфл", "човник", "лодоч", "pump", "підбор", "подбор", "heel",
    "лофер", "loafer", "балет", "ballerina", "ballet", "slingback",
    "слінгбек", "mary jane", "мері джейн", "дербі", "derby",
    "оксфорд", "oxford", "брог", "brogue", "мокасин", "moccasin",
    "мюлі", "mule", "сабо", "clog", "босоніж", "босонож", "sandal",
    "чобіт", "чобот", "сапог", "boot", "ботфорт", "напівчоб",
    "полусап", "ботильйон", "ботильон", "ботільйон", "черевик",
    "ботин", "ботін", "челсі", "chelsea",
)

SPORT_WORDS = (
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
        return self.data["meta"].get(key)

    def set_meta(self, key: str, value: str):
        self.data["meta"][key] = str(value)

    def save(self):
        bucket = self.data["seen"]["shafa"]
        if len(bucket) > 20000:
            self.data["seen"]["shafa"] = dict(
                sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:20000]
            )
        STATE_PATH.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def brand_list(cfg: dict) -> list[str]:
    result = []
    for item in cfg["filters"].get("brands", []):
        if isinstance(item, str):
            name = item.strip()
        else:
            name = str(item.get("name", "")).strip()
        if name and name not in result:
            result.append(name)
    return result


def slugify(name: str) -> str:
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " ").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def parse_price(text: str) -> int | None:
    m = re.search(r"(\d[\d\s\xa0.,]{0,12})\s*грн", text, re.I)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_sizes(text: str) -> list[float]:
    result = set()
    # Prefer explicitly labelled EU/UA sizes.
    for m in re.finditer(r"\b(?:EU|UA)\s*(\d{2}(?:[.,]5)?)", text, re.I):
        try:
            x = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 30 <= x <= 46:
            result.add(x)
    # Shafa sometimes omits EU/UA before the first size.
    if not result:
        for m in re.finditer(r"(?<!\d)(3[4-9]|4[0-6])(?:[.,]5)?(?!\d)", text):
            token = m.group(0).replace(",", ".")
            try:
                x = float(token)
            except ValueError:
                continue
            if 30 <= x <= 46:
                result.add(x)
                if len(result) >= 4:
                    break
    return sorted(result)


def card_text(anchor) -> str:
    node = anchor
    best = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
    for _ in range(7):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if len(text) > 1400:
            break
        if "грн" in text.lower():
            best = text
    return best


def card_image(anchor) -> str | None:
    node = anchor
    for _ in range(6):
        if node is None:
            break
        img = node.find("img") if hasattr(node, "find") else None
        if img:
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if not src and img.get("srcset"):
                src = str(img.get("srcset")).split(",")[0].strip().split(" ")[0]
            if src and not str(src).startswith("data:"):
                return urljoin("https://shafa.ua", str(src))
        node = getattr(node, "parent", None)
    return None


def parse_brand_page(html: str, brand: str, limit: int = 60) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = PRODUCT_RE.search(href)
        if not m:
            continue

        url = urljoin("https://shafa.ua", href.split("?")[0])
        if url in seen:
            continue

        seen.add(url)
        text = card_text(a)
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()

        if not title:
            # Sometimes title is in an image alt.
            img = a.find("img")
            if img:
                title = str(img.get("alt") or "").strip()

        if not title:
            continue

        items.append(
            Item(
                listing_id=m.group(1),
                url=url,
                title=title,
                brand=brand,
                price=parse_price(text),
                sizes=parse_sizes(text),
                card_text=text,
                image=card_image(a),
            )
        )
        if len(items) >= limit:
            break

    return items


def matches_card(item: Item, filters: dict) -> bool:
    text = f"{item.title} {item.card_text}".lower()

    if any(word in text for word in SPORT_WORDS):
        return False

    if not any(word in text for word in FOOTWEAR_WORDS):
        return False

    if item.price is None:
        return False

    if not int(filters.get("min_price_uah", 300)) <= item.price <= int(filters.get("max_price_uah", 4000)):
        return False

    lo = float(filters.get("min_size", 36))
    hi = float(filters.get("max_size", 41))

    if not item.sizes or not any(lo <= x <= hi for x in item.sizes):
        return False

    return True


def explicitly_new(detail_text: str, title: str) -> bool:
    compact = re.sub(r"\s+", " ", detail_text)
    m = re.search(
        r"(?:стан|состояние)\s*:?\s*(.{0,70}?)"
        r"(?=(?:колір|цвет|розмір|размер|категор|матеріал|материал|$))",
        compact,
        re.I,
    )
    if m:
        state = m.group(1).lower()
        if re.search(r"\bнов", state):
            return True
        if any(x in state for x in ("ідеаль", "идеаль", "дуже хорош", "очень хорош", "хорош", "задов", "удов")):
            return False
    return bool(re.search(r"\b(нові|новий|нова|новые|новая|новое)\b", title.lower()))


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.client = httpx.Client(timeout=30)

    def send_text(self, text: str):
        r = self.client.post(
            f"{self.base}/sendMessage",
            data={"chat_id": self.chat_id, "text": text},
        )
        r.raise_for_status()

    def send_item(self, item: Item, prefix: str = "🆕"):
        price = f"{item.price:,}".replace(",", " ") + " грн" if item.price else "—"
        sizes = ", ".join(
            str(int(x)) if x.is_integer() else str(x).replace(".", ",")
            for x in item.sizes
        )
        text = (
            f"{prefix} SHAFA\n"
            f"{item.title}\n\n"
            f"Бренд: {item.brand}\n"
            f"Ціна: {price}\n"
            f"Розмір: {sizes or '—'}\n"
            f"{item.url}"
        )
        if item.image:
            r = self.client.post(
                f"{self.base}/sendPhoto",
                data={
                    "chat_id": self.chat_id,
                    "photo": item.image,
                    "caption": text[:1024],
                },
            )
            if r.is_success:
                return
        r = self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        r.raise_for_status()

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
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(18)
    return driver


def load_rendered(driver, url: str, wait: float = 1.4) -> str:
    driver.get(url)
    time.sleep(wait)
    driver.execute_script("window.scrollTo(0, 1200);")
    time.sleep(0.55)
    return driver.page_source


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    cfg = load_config()
    filters = cfg["filters"]
    brands = brand_list(cfg)

    state = State()
    tg = Telegram(token, chat_id)
    driver = make_driver()

    # First successful browser run sends a few real current matches as a proof,
    # then normal monitoring switches to new-only.
    proof_mode = state.get_meta("browser_monitor_proof_done") != "1"
    proof_left = 5

    total_products = 0
    qualifying = 0

    try:
        for index, brand in enumerate(brands, start=1):
            slug = slugify(brand)
            urls = [
                f"https://shafa.ua/uk/brands/{slug}/women?sort=4",
                f"https://shafa.ua/uk/tufli-{slug}.xhtml?sort=4",
                f"https://shafa.ua/uk/zhenskie-tufli-{slug}.xhtml?sort=4",
            ]

            items = []
            for url in urls:
                try:
                    html = load_rendered(driver, url)
                    items = parse_brand_page(html, brand)
                    logging.info("%s/%s %s -> %s products", index, len(brands), brand, len(items))
                    if items:
                        break
                except Exception:
                    logging.exception("Render failed: %s", url)

            if not items:
                continue

            total_products += len(items)

            for item in items:
                if state.has(item.listing_id):
                    continue

                # Mark every encountered item once so irrelevant items aren't rechecked.
                state.mark(item.listing_id)

                if not matches_card(item, filters):
                    continue

                # Check condition on the rendered listing page.
                try:
                    detail_html = load_rendered(driver, item.url, wait=0.8)
                    detail_text = " ".join(
                        BeautifulSoup(detail_html, "html.parser").stripped_strings
                    )
                    if explicitly_new(detail_text, item.title):
                        continue
                except Exception:
                    logging.exception("Condition check failed for %s", item.url)

                qualifying += 1

                if proof_mode and proof_left > 0:
                    tg.send_item(item, prefix="🧪 TEST")
                    proof_left -= 1
                elif not proof_mode:
                    tg.send_item(item, prefix="🆕")

        if proof_mode:
            state.set_meta("browser_monitor_proof_done", "1")
            tg.send_text(
                f"✅ Перевірка Shafa завершена. "
                f"Бот побачив {total_products} оголошень у сторінках брендів; "
                f"під фільтри пройшло {qualifying}. "
                f"Далі надсилатиму тільки нові."
            )

        state.save()

    finally:
        driver.quit()
        tg.close()


if __name__ == "__main__":
    main()
