from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import asdict

from bs4 import BeautifulSoup

from shafa_monitor_v3 import (
    Item,
    PRODUCT_RE,
    State,
    Telegram,
    catalog_urls,
    explicitly_new,
    load_config,
    make_driver,
    matches_card,
    normalize_brands,
    parse_brand_page,
    slugify,
)

TOP_RESALE = {
    "Chanel", "Dior", "Hermès", "Celine", "Loewe", "The Row",
    "Loro Piana", "Bottega Veneta", "Prada", "Miu Miu", "Gucci",
    "Saint Laurent", "Christian Louboutin", "Roger Vivier",
}

STRONG_RESALE = {
    "Fendi", "Valentino", "Jimmy Choo", "Gianvito Rossi", "Sergio Rossi",
    "Manolo Blahnik", "Ferragamo", "Maison Margiela", "Alaïa",
    "Brunello Cucinelli", "Khaite", "Tom Ford", "Balenciaga",
    "Rene Caovilla", "Aquazzura", "Chloé", "Versace",
}


def item_to_dict(item: Item, **extra) -> dict:
    raw = asdict(item)
    raw.update(extra)
    return raw


def item_from_dict(raw: dict) -> Item:
    return Item(
        listing_id=str(raw.get("listing_id", "")),
        url=str(raw.get("url", "")),
        title=str(raw.get("title", "")),
        brand=str(raw.get("brand", "")),
        priority=bool(raw.get("priority", False)),
        price=raw.get("price"),
        sizes=[float(x) for x in raw.get("sizes", [])],
        card_text=str(raw.get("card_text", "")),
        image=raw.get("image"),
    )


class StateV4(State):
    def __init__(self):
        super().__init__()
        self.data.setdefault("pending_new", {})
        self.data.setdefault("best_candidates", {})
        self.data.setdefault("best_history", {})

    def queue_new(self, item: Item):
        self.data["pending_new"][item.listing_id] = item_to_dict(
            item,
            queued_at=int(time.time()),
        )

    def pending_items(self) -> list[tuple[Item, int]]:
        result = []
        for raw in self.data["pending_new"].values():
            try:
                result.append(
                    (
                        item_from_dict(raw),
                        int(raw.get("queued_at", 0)),
                    )
                )
            except Exception:
                logging.exception("Bad pending item in state")
        return result

    def remove_pending(self, listing_id: str):
        self.data["pending_new"].pop(listing_id, None)

    def best_history_time(self, listing_id: str) -> int:
        try:
            return int(
                self.data["best_history"].get(listing_id, 0)
            )
        except (TypeError, ValueError):
            return 0

    def mark_best_sent(self, listing_id: str):
        self.data["best_history"][listing_id] = int(time.time())

    def cache_best(
        self,
        brand: str,
        items: list[Item],
        filters: dict,
        keep: int,
        ttl_seconds: int,
    ):
        now = int(time.time())
        merged: dict[str, tuple[Item, int]] = {}

        for raw in self.data["best_candidates"].get(brand, []):
            try:
                cached_at = int(raw.get("cached_at", 0))
                if now - cached_at <= ttl_seconds:
                    item = item_from_dict(raw)
                    merged[item.listing_id] = (item, cached_at)
            except Exception:
                continue

        for item in items:
            if not matches_card(item, filters):
                continue

            # A title/card that explicitly says "new" is never a best pick.
            if explicitly_new(item.card_text, item.title):
                continue

            merged[item.listing_id] = (item, now)

        ranked = sorted(
            merged.values(),
            key=lambda pair: resale_score(pair[0]),
            reverse=True,
        )[:keep]

        self.data["best_candidates"][brand] = [
            item_to_dict(item, cached_at=cached_at)
            for item, cached_at in ranked
        ]

    def best_for_brand(
        self,
        brand: str,
        ttl_seconds: int,
    ) -> list[Item]:
        now = int(time.time())
        result = []

        for raw in self.data["best_candidates"].get(brand, []):
            try:
                if now - int(raw.get("cached_at", 0)) <= ttl_seconds:
                    result.append(item_from_dict(raw))
            except Exception:
                continue

        return sorted(
            result,
            key=resale_score,
            reverse=True,
        )

    def save(self):
        cutoff = int(time.time()) - 7 * 24 * 3600
        self.data["best_history"] = {
            listing_id: ts
            for listing_id, ts in self.data["best_history"].items()
            if int(ts or 0) >= cutoff
        }
        super().save()


class TelegramV4(Telegram):
    def send_header(self, text: str):
        response = self.client.post(
            f"{self.base}/sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()

    def send_labeled_item(
        self,
        item: Item,
        label: str,
    ):
        price = (
            f"{item.price:,}".replace(",", " ") + " грн"
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
            f"{label} {hot}SHAFA\n"
            f"{item.title}\n\n"
            f"Бренд: {item.brand}\n"
            f"Ціна: {price}\n"
            f"Розмір: {sizes or '—'}\n"
            f"Score: {resale_score(item)}/100\n"
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


def resale_score(item: Item) -> int:
    text = f"{item.title} {item.card_text}".lower()

    if item.brand in TOP_RESALE:
        score = 24
    elif item.brand in STRONG_RESALE:
        score = 16
    elif item.priority:
        score = 12
    else:
        score = 8

    if item.price is not None:
        if item.price <= 1000:
            score += 24
        elif item.price <= 1600:
            score += 20
        elif item.price <= 2400:
            score += 15
        elif item.price <= 3200:
            score += 10
        else:
            score += 6

    if any(37 <= size <= 39 for size in item.sizes):
        score += 15
    elif any(size in (36, 40) for size in item.sizes):
        score += 10
    elif 41 in item.sizes:
        score += 6

    if any(
        word in text
        for word in (
            "шкіра",
            "кожа",
            "leather",
            "замш",
            "suede",
        )
    ):
        score += 10

    if any(
        word in text
        for word in (
            "лофер",
            "loafer",
            "балет",
            "ballet",
            "ballerina",
            "slingback",
            "слінгбек",
            "човник",
            "лодоч",
            "pump",
            "mary jane",
            "мері джейн",
        )
    ):
        score += 12
    elif any(
        word in text
        for word in (
            "туфл",
            "heel",
            "підбор",
            "подбор",
        )
    ):
        score += 9
    elif any(
        word in text
        for word in (
            "чоб",
            "сапог",
            "boot",
            "ботиль",
        )
    ):
        score += 7
    elif any(
        word in text
        for word in (
            "босоніж",
            "босонож",
            "sandal",
        )
    ):
        score += 6

    if any(
        word in text
        for word in (
            "ідеаль",
            "идеаль",
            "дуже хорош",
            "очень хорош",
        )
    ):
        score += 6
    elif "хорош" in text:
        score += 3

    if item.image:
        score += 4

    if item.price is not None and item.sizes:
        score += 3

    return min(score, 100)


def product_count(html: str) -> int:
    return len(
        {
            match.group(1)
            for match in PRODUCT_RE.finditer(html)
        }
    )


def load_catalog_deep(
    driver,
    url: str,
    target_items: int,
) -> str:
    driver.get(url)
    time.sleep(0.75)

    html = driver.page_source
    previous = product_count(html)
    stable = 0

    for _ in range(7):
        if previous >= target_items:
            break

        try:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
        except Exception:
            break

        time.sleep(0.35)
        html = driver.page_source
        current = product_count(html)

        if current <= previous:
            stable += 1
        else:
            stable = 0

        previous = current

        if stable >= 2:
            break

    return html


def load_detail_text(
    driver,
    url: str,
) -> str:
    driver.get(url)
    time.sleep(0.55)

    return " ".join(
        BeautifulSoup(
            driver.page_source,
            "html.parser",
        ).stripped_strings
    )


def source_indices(
    state: StateV4,
    brand: str,
    rotation: int,
) -> list[int]:
    slug = slugify(brand)
    preferred = None

    try:
        raw = state.get_meta(
            f"preferred_source_v4:{slug}"
        )
        if (
            raw is not None
            and 0 <= int(raw) <= 3
        ):
            preferred = int(raw)
    except ValueError:
        preferred = None

    if preferred is None:
        return [
            rotation,
            (rotation + 1) % 4,
        ]

    if preferred == rotation:
        return [preferred]

    return [
        preferred,
        rotation,
    ]


def update_preferred_source(
    state: StateV4,
    brand: str,
    index: int,
    count: int,
):
    if count <= 0:
        return

    slug = slugify(brand)
    count_key = (
        f"preferred_source_count_v4:{slug}"
    )
    source_key = (
        f"preferred_source_v4:{slug}"
    )

    try:
        old_count = int(
            state.get_meta(count_key)
            or 0
        )
    except ValueError:
        old_count = 0

    if (
        state.get_meta(source_key) is None
        or count > old_count
    ):
        state.set_meta(
            source_key,
            index,
        )
        state.set_meta(
            count_key,
            count,
        )


def round_robin_new(
    pending: list[tuple[Item, int]],
    limit: int,
) -> list[Item]:
    buckets: dict[str, deque[Item]] = {}

    for item, queued_at in sorted(
        pending,
        key=lambda pair: pair[1],
    ):
        buckets.setdefault(
            item.brand,
            deque(),
        ).append(item)

    result = []
    brands = list(buckets)

    while brands and len(result) < limit:
        next_round = []

        for brand in brands:
            queue = buckets[brand]

            if (
                queue
                and len(result) < limit
            ):
                result.append(
                    queue.popleft()
                )

            if queue:
                next_round.append(brand)

        brands = next_round

    return result


def hourly_due(
    state: StateV4,
    interval_seconds: int,
) -> bool:
    now = int(time.time())
    raw = state.get_meta(
        "v4_last_digest_at"
    )

    if raw is None:
        state.set_meta(
            "v4_last_digest_at",
            now,
        )
        state.save()
        return False

    try:
        last = int(float(raw))
    except ValueError:
        state.set_meta(
            "v4_last_digest_at",
            now,
        )
        state.save()
        return False

    return (
        now - last
        >= interval_seconds
    )


def send_new_digest(
    state: StateV4,
    telegram: TelegramV4,
    max_items: int,
) -> set[str]:
    selected = round_robin_new(
        state.pending_items(),
        max_items,
    )

    if not selected:
        logging.info(
            "Hourly new digest: 0 items"
        )
        return set()

    telegram.send_header(
        "🆕 НОВІ ЗА ОСТАННЮ ГОДИНУ\n"
        f"{len(selected)} пар • "
        f"{len({item.brand for item in selected})} брендів"
    )

    sent = set()

    for item in selected:
        try:
            telegram.send_labeled_item(
                item,
                "🆕",
            )
            state.remove_pending(
                item.listing_id
            )
            state.save()
            sent.add(
                item.listing_id
            )

            logging.info(
                "DIGEST NEW %s | %s | %s грн",
                item.brand,
                item.title,
                item.price,
            )

            time.sleep(0.15)

        except Exception:
            logging.exception(
                "Failed new digest item: %s",
                item.url,
            )

    return sent


def send_best_digest(
    state: StateV4,
    telegram: TelegramV4,
    driver,
    brands: list[dict],
    max_items: int,
    repeat_seconds: int,
    ttl_seconds: int,
    excluded_ids: set[str],
    deadline: float,
):
    now = int(time.time())
    pending_ids = set(
        state.data["pending_new"]
    )

    brand_candidates = []

    for spec in brands:
        candidates = state.best_for_brand(
            spec["name"],
            ttl_seconds,
        )

        if candidates:
            brand_candidates.append(
                (
                    resale_score(candidates[0]),
                    spec["name"],
                    candidates,
                )
            )

    brand_candidates.sort(
        reverse=True
    )

    picks = []

    for _, brand, candidates in brand_candidates:
        if (
            len(picks) >= max_items
            or time.monotonic() >= deadline
        ):
            break

        for item in candidates:
            if (
                item.listing_id in excluded_ids
                or item.listing_id in pending_ids
            ):
                continue

            last_sent = state.best_history_time(
                item.listing_id
            )

            if (
                last_sent
                and now - last_sent
                < repeat_seconds
            ):
                continue

            try:
                detail = load_detail_text(
                    driver,
                    item.url,
                )

                if explicitly_new(
                    detail,
                    item.title,
                ):
                    logging.info(
                        "Best pick skipped NEW: %s",
                        item.title,
                    )
                    continue

            except Exception:
                logging.exception(
                    "Best pick validation failed: %s",
                    item.url,
                )
                continue

            picks.append(item)
            break

    if not picks:
        logging.info(
            "Hourly best digest: 0 items"
        )
        return

    telegram.send_header(
        "⭐ НАЙКРАЩІ АКТУАЛЬНІ ЗНАХІДКИ\n"
        f"{len(picks)} пар • "
        f"{len({item.brand for item in picks})} брендів"
    )

    for item in picks:
        if time.monotonic() >= deadline:
            break

        try:
            telegram.send_labeled_item(
                item,
                "⭐",
            )

            state.mark_best_sent(
                item.listing_id
            )
            state.save()

            logging.info(
                "DIGEST BEST %s | score=%s | %s | %s грн",
                item.brand,
                resale_score(item),
                item.title,
                item.price,
            )

            time.sleep(0.15)

        except Exception:
            logging.exception(
                "Failed best digest item: %s",
                item.url,
            )


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
    filters = cfg.get(
        "filters",
        {},
    )
    monitor = cfg.get(
        "monitor",
        {},
    )
    digest = cfg.get(
        "digest",
        {},
    )
    brands = normalize_brands(cfg)

    if not brands:
        raise SystemExit(
            "No brands configured"
        )

    max_items = max(
        50,
        int(
            monitor.get(
                "max_items_per_brand",
                50,
            )
        ),
    )

    scan_budget = float(
        monitor.get(
            "scan_budget_seconds",
            480,
        )
    )

    digest_interval = max(
        3600,
        int(
            digest.get(
                "interval_minutes",
                60,
            )
        ) * 60,
    )

    max_new = max(
        1,
        int(
            digest.get(
                "max_new_items",
                100,
            )
        ),
    )

    max_best = max(
        1,
        int(
            digest.get(
                "max_best_items",
                55,
            )
        ),
    )

    repeat_seconds = max(
        3600,
        int(
            digest.get(
                "best_repeat_hours",
                24,
            )
        ) * 3600,
    )

    ttl_seconds = max(
        3600,
        int(
            digest.get(
                "candidate_ttl_hours",
                6,
            )
        ) * 3600,
    )

    keep_best = max(
        5,
        int(
            digest.get(
                "candidates_per_brand",
                20,
            )
        ),
    )

    state = StateV4()

    try:
        start_index = int(
            state.get_meta(
                "browser_next_brand_index_v4"
            )
            or 0
        ) % len(brands)
    except ValueError:
        start_index = 0

    try:
        rotation = int(
            state.get_meta(
                "v4_source_rotation"
            )
            or 0
        ) % 4
    except ValueError:
        rotation = 0

    logging.info(
        "Shafa v4 cycle | start=%s | rotation=%s | depth=%s",
        start_index,
        rotation,
        max_items,
    )

    telegram = TelegramV4(
        token,
        chat_id,
    )
    driver = make_driver()
    started = time.monotonic()

    scan_deadline = (
        started
        + scan_budget
    )

    hard_deadline = (
        started
        + 760
    )

    processed = 0

    try:
        for offset in range(
            len(brands)
        ):
            if (
                processed > 0
                and time.monotonic()
                >= scan_deadline
            ):
                logging.info(
                    "Scan budget reached after %s brands",
                    processed,
                )
                break

            brand_index = (
                start_index
                + offset
            ) % len(brands)

            spec = brands[
                brand_index
            ]

            brand = spec["name"]
            urls = catalog_urls(
                brand
            )

            merged: dict[
                str,
                Item,
            ] = {}

            indices = source_indices(
                state,
                brand,
                rotation,
            )

            logging.info(
                "%s | sources=%s",
                brand,
                indices,
            )

            for index in indices:
                url = urls[index]

                try:
                    html = load_catalog_deep(
                        driver,
                        url,
                        max_items,
                    )

                    items = parse_brand_page(
                        html,
                        brand,
                        spec["priority"],
                        max_items,
                    )

                    logging.info(
                        "%s | source=%s | %s products | %s",
                        brand,
                        index,
                        len(items),
                        url,
                    )

                except Exception:
                    logging.exception(
                        "Render failed: %s",
                        url,
                    )
                    continue

                update_preferred_source(
                    state,
                    brand,
                    index,
                    len(items),
                )

                for item in items:
                    merged[
                        item.listing_id
                    ] = item

                if not items:
                    continue

                baseline_key = (
                    "source_baseline_v4:"
                    f"{slugify(brand)}:"
                    f"{index}"
                )

                # First visit of EACH source only memorises its current IDs.
                # This prevents old listings from another Shafa source from
                # suddenly being treated as new later.
                if (
                    state.get_meta(
                        baseline_key
                    )
                    != "1"
                ):
                    for item in items:
                        state.mark(
                            item.listing_id
                        )

                    state.set_meta(
                        baseline_key,
                        "1",
                    )

                    logging.info(
                        "Source baseline saved %s source=%s: %s listings",
                        brand,
                        index,
                        len(items),
                    )
                    continue

                for item in reversed(
                    items
                ):
                    if state.has(
                        item.listing_id
                    ):
                        continue

                    state.mark(
                        item.listing_id
                    )

                    if not matches_card(
                        item,
                        filters,
                    ):
                        continue

                    try:
                        detail = load_detail_text(
                            driver,
                            item.url,
                        )

                        if explicitly_new(
                            detail,
                            item.title,
                        ):
                            logging.info(
                                "Skipped NEW: %s",
                                item.title,
                            )
                            continue

                    except Exception:
                        # Safer behaviour: do not queue an item if its state
                        # could not be verified.
                        logging.exception(
                            "Condition check failed; item not queued: %s",
                            item.url,
                        )
                        continue

                    state.queue_new(
                        item
                    )

                    logging.info(
                        "QUEUED NEW %s | %s | %s грн",
                        brand,
                        item.title,
                        item.price,
                    )

            if merged:
                state.cache_best(
                    brand,
                    list(
                        merged.values()
                    ),
                    filters,
                    keep_best,
                    ttl_seconds,
                )
            else:
                logging.warning(
                    "No rendered Shafa listings for %s in this cycle",
                    brand,
                )

            next_index = (
                brand_index + 1
            ) % len(brands)

            state.set_meta(
                "browser_next_brand_index_v4",
                next_index,
            )

            state.save()
            processed += 1

        state.set_meta(
            "v4_source_rotation",
            (rotation + 1) % 4,
        )

        state.set_meta(
            "browser_monitor_v4_active",
            "1",
        )

        state.save()

        logging.info(
            "Scan finished | processed=%s | pending=%s",
            processed,
            len(
                state.data[
                    "pending_new"
                ]
            ),
        )

        if hourly_due(
            state,
            digest_interval,
        ):
            logging.info(
                "Hourly digest due"
            )

            sent_new = send_new_digest(
                state,
                telegram,
                max_new,
            )

            send_best_digest(
                state,
                telegram,
                driver,
                brands,
                max_best,
                repeat_seconds,
                ttl_seconds,
                sent_new,
                hard_deadline,
            )

            state.set_meta(
                "v4_last_digest_at",
                int(time.time()),
            )

            state.save()

        else:
            logging.info(
                "Hourly digest not due yet"
            )

    finally:
        driver.quit()
        telegram.close()


if __name__ == "__main__":
    main()
