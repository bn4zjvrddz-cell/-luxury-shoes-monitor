from __future__ import annotations

import unicodedata
import re

import shafa_monitor_v4 as v4


def _slugify_query(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
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


def _title_aliases() -> dict[str, list[str]]:
    cfg = v4.load_config()
    result: dict[str, list[str]] = {}

    for raw in cfg.get("filters", {}).get("brands", []):
        if isinstance(raw, str):
            name = raw.strip()
            queries = []
        else:
            name = str(raw.get("name", "")).strip()
            queries = raw.get("olx_queries", []) or []

        if not name:
            continue

        aliases = [name, *queries]

        # Extra useful marketplace spellings.
        if name == "Saint Laurent":
            aliases += ["YSL", "Yves Saint Laurent"]
        elif name == "Ferragamo":
            aliases += ["Salvatore Ferragamo"]
        elif name == "Christian Louboutin":
            aliases += ["Louboutin"]
        elif name == "Maison Margiela":
            aliases += ["Margiela", "Maison Martin Margiela"]
        elif name == "Alexander McQueen":
            aliases += ["McQueen"]
        elif name == "Chloé":
            aliases += ["Chloe"]
        elif name == "Celine":
            aliases += ["Céline"]
        elif name == "Hermès":
            aliases += ["Hermes"]
        elif name == "Tod's":
            aliases += ["Tods"]
        elif name == "Rene Caovilla":
            aliases += ["René Caovilla"]

        slugs = []
        for alias in aliases:
            slug = _slugify_query(str(alias))
            if slug and slug not in slugs:
                slugs.append(slug)

        result[name] = slugs

    return result


TITLE_ALIASES = _title_aliases()


def catalog_urls_with_title_search(brand: str) -> list[str]:
    """
    Source 0 = Shafa brand field page.
    Sources 1+ = SEO/search pages generated from the brand name and aliases.

    This deliberately combines:
    1) sellers who selected the brand field;
    2) sellers who only typed the brand in the listing title/keywords.
    """
    canonical = v4.slugify(brand)
    aliases = TITLE_ALIASES.get(brand, [canonical])

    urls = [
        f"https://shafa.ua/uk/brands/{canonical}/women?sort=4",
    ]

    # Broad footwear keyword pages first — most useful for title-based search.
    for slug in aliases:
        urls.append(
            f"https://shafa.ua/uk/obuv-{slug}.xhtml?sort=4"
        )

    # Then two shoe-title SEO variants as additional rotating sources.
    for slug in aliases:
        urls.append(
            f"https://shafa.ua/uk/tufli-{slug}.xhtml?sort=4"
        )

    for slug in aliases:
        urls.append(
            f"https://shafa.ua/uk/zhenskie-tufli-{slug}.xhtml?sort=4"
        )

    # Keep order stable and remove duplicates.
    unique = []
    for url in urls:
        if url not in unique:
            unique.append(url)

    return unique


def source_indices_v5(
    state: v4.StateV4,
    brand: str,
    rotation: int,
) -> list[int]:
    """
    Every cycle checks:
    - the actual Shafa brand page;
    - one rotating title/SEO search source.

    This keeps runtime reasonable while progressively covering aliases
    without sacrificing the 15-minute scan cadence.
    """
    urls = catalog_urls_with_title_search(brand)

    if len(urls) <= 1:
        return [0]

    key = f"v5_title_cursor:{v4.slugify(brand)}"

    try:
        cursor = int(state.get_meta(key) or 0)
    except ValueError:
        cursor = 0

    title_index = 1 + (cursor % (len(urls) - 1))
    state.set_meta(key, cursor + 1)

    return [0, title_index]


def no_preferred_source(
    state: v4.StateV4,
    brand: str,
    index: int,
    count: int,
):
    # v5 uses a deterministic rotating title-source cursor instead.
    return


# Patch v4's globals before running its tested main loop.
v4.catalog_urls = catalog_urls_with_title_search
v4.source_indices = source_indices_v5
v4.update_preferred_source = no_preferred_source


if __name__ == "__main__":
    v4.main()
