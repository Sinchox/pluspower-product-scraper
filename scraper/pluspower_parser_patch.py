"""
PlusPower product-page parser integration patch.

Upload this file as: scraper/pluspower_parser_patch.py

Then add these two lines at the very end of scraper/core.py:

    from .pluspower_parser_patch import install_pluspower_patch
    install_pluspower_patch(globals())

This patch extends the existing scraper; it does not replace scraper/core.py.
"""

import json
import re
from urllib.parse import urljoin


def _extract_js_assignment(html, variable_name):
    """Extract a JSON-compatible JavaScript object/array assignment safely."""
    pattern = re.compile(
        r"\b" + re.escape(variable_name) + r"\s*=\s*",
        re.MULTILINE,
    )
    match = pattern.search(html or "")
    if not match:
        return None

    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] not in "{[":
        return None

    opening = html[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    quote = None
    escaped = False

    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in ("'", '"', "`"):
            # JSON.parse-compatible values use double quotes. Single-quoted
            # JavaScript is intentionally not evaluated or executed.
            if char != '"':
                return None
            in_string = True
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                raw = html[start:index + 1]
                try:
                    return json.loads(raw)
                except (TypeError, ValueError):
                    return None
    return None


def _pluspower_variant_records(soup):
    """Join PlusPower's varianti object with descvar1 flavor descriptions."""
    html = str(soup)
    variants = _extract_js_assignment(html, "varianti")
    descriptions = _extract_js_assignment(html, "descvar1")
    if not isinstance(variants, dict):
        return []

    names = {}
    if isinstance(descriptions, list):
        for item in descriptions:
            if isinstance(item, dict) and item.get("id") is not None:
                names[str(item["id"])] = item.get("descrizione") or item.get("description") or ""

    records = []
    for option_id, entries in variants.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            lots = entry.get("lotti") or []
            lot_records = []
            if isinstance(lots, list):
                for lot in lots:
                    if isinstance(lot, dict):
                        lot_records.append({
                            "expiry": lot.get("scadenza"),
                            "quantity": lot.get("quant"),
                            "available_online": lot.get("disponibile_web"),
                        })

            records.append({
                "name": names.get(str(option_id), ""),
                "option_id": str(option_id),
                "variant_id": entry.get("id"),
                "sku": entry.get("codice"),
                "product_code": entry.get("codice"),
                "ean": entry.get("ean"),
                "stock_quantity": entry.get("quant"),
                "available_online": (
                    lots[0].get("disponibile_web")
                    if isinstance(lots, list) and lots and isinstance(lots[0], dict)
                    else None
                ),
                "price": entry.get("prezzo_sconto_ivato") or entry.get("prezzo_ivato"),
                "original_price": entry.get("prezzo_ivato"),
                "lots": lot_records,
            })
    return records


def _pluspower_brand(soup):
    node = soup.select_one('[itemprop="brand"] meta[itemprop="name"][content]')
    if node:
        return node.get("content", "").strip()
    node = soup.select_one('[itemtype*="schema.org/Brand"] [itemprop="name"]')
    if node:
        return (
            node.get("content") or node.get_text(" ", strip=True) or ""
        ).strip()
    return ""


def _pluspower_images(soup, existing=None):
    """Collect canonical and gallery image URLs, preserving order."""
    results = list(existing or [])
    selectors = [
        ('link[itemprop="image"][href]', "href"),
        ("#gallery a[href]", "href"),
        ("#gallery img[src]", "src"),
        ("#gallery source[srcset]", "srcset"),
    ]
    for selector, attribute in selectors:
        for node in soup.select(selector):
            value = node.get(attribute, "")
            if attribute == "srcset":
                value = value.split(",")[0].strip().split(" ")[0] if value else ""
            if value:
                results.append(value)

    normalized = []
    seen = set()
    for value in results:
        value = str(value).strip()
        if not value:
            continue
        value = urljoin("https://www.pluspowersupplements.com/", value)
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def install_pluspower_patch(namespace):
    """
    Monkey-patch selected functions in scraper.core.

    The patch is deliberately defensive: if a core function is absent, the
    rest of the application can still import and use its original parser.
    """
    original_meta = namespace.get("_meta_itemprop")
    original_images = namespace.get("_all_product_images")
    original_variants = namespace.get("_variant_records")
    original_extract = namespace.get("extract_product")

    def patched_meta_itemprop(soup, name, *args, **kwargs):
        if name == "brand":
            brand = _pluspower_brand(soup)
            if brand:
                return brand
        if callable(original_meta):
            return original_meta(soup, name, *args, **kwargs)
        return ""

    def patched_images(soup, *args, **kwargs):
        existing = []
        if callable(original_images):
            try:
                existing = original_images(soup, *args, **kwargs) or []
            except Exception:
                existing = []
        return _pluspower_images(soup, existing)

    def patched_variants(soup, *args, **kwargs):
        records = _pluspower_variant_records(soup)
        if records:
            return records
        if callable(original_variants):
            return original_variants(soup, *args, **kwargs)
        return []

    if callable(original_meta):
        namespace["_meta_itemprop"] = patched_meta_itemprop
    if callable(original_images):
        namespace["_all_product_images"] = patched_images
    if callable(original_variants):
        namespace["_variant_records"] = patched_variants

    if callable(original_extract):
        def patched_extract_product(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            if not isinstance(result, dict):
                return result

            # Recover the HTML/soup argument so PlusPower-specific fields can
            # be enriched without changing the existing extraction pipeline.
            soup = None
            for value in args:
                if hasattr(value, "select_one") and hasattr(value, "select"):
                    soup = value
                    break
            if soup is None:
                for value in kwargs.values():
                    if hasattr(value, "select_one") and hasattr(value, "select"):
                        soup = value
                        break

            if soup is not None:
                brand = _pluspower_brand(soup)
                if brand and not result.get("brand"):
                    result["brand"] = brand

                variants = _pluspower_variant_records(soup)
                if variants:
                    result["variants"] = variants

                images = _pluspower_images(soup, result.get("images") or [])
                if images:
                    result["images"] = images

                h1 = soup.select_one("h1")
                if h1:
                    title = h1.get_text(" ", strip=True)
                    if title:
                        result["title"] = title
            return result

        namespace["extract_product"] = patched_extract_product
