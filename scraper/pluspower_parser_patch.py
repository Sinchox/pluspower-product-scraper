"""PlusPower JavaScript variant parser patch.

Copy these helpers into scraper/core.py (or import this module there).
Requires the existing `json`, `re`, and `_clean` helpers.
"""
import json
import re


def _extract_js_assignment(html, variable_name):
    """Extract a JSON-compatible JavaScript object/array assignment without eval."""
    match = re.search(r"\bvar\s+" + re.escape(variable_name) + r"\s*=\s*", html)
    if not match:
        return None
    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] not in "[{":
        return None
    opening = html[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(html)):
        char = html[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:pos + 1])
                except (TypeError, ValueError):
                    return None
    return None


def _extract_pluspower_variants(html, clean):
    """Join PlusPower's `varianti` records to `descvar1` flavour labels."""
    raw_variants = _extract_js_assignment(html, "varianti")
    labels = _extract_js_assignment(html, "descvar1")
    if not isinstance(raw_variants, dict):
        return []
    label_map = {}
    if isinstance(labels, list):
        for entry in labels:
            if isinstance(entry, dict):
                label_map[str(entry.get("id", ""))] = clean(entry.get("descrizione", ""))
    output = []
    for option_id, records in raw_variants.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            lots = record.get("lotti") or []
            quantity = record.get("quant", 0)
            output.append({
                "name": label_map.get(str(option_id), ""),
                "option_id": str(option_id),
                "variant_id": str(record.get("id", "")),
                "sku": clean(record.get("codice", "")),
                "ean": clean(record.get("ean", "")),
                "price": record.get("prezzo_sconto_ivato") or record.get("prezzo_ivato") or "",
                "original_price": record.get("prezzo_ivato", ""),
                "quantity": quantity,
                "available": (
                    any(str(lot.get("disponibile_web", "")) == "1"
                        for lot in lots if isinstance(lot, dict))
                    if lots else quantity > 0
                ),
                "lots": [
                    {"expiry": lot.get("scadenza", ""), "quantity": lot.get("quant", 0)}
                    for lot in lots if isinstance(lot, dict)
                ],
            })
    return output


def pluspower_brand_from_html(soup, clean):
    """Read nested schema.org Brand > name meta content."""
    node = soup.select_one('[itemprop="brand"] meta[itemprop="name"][content]')
    return clean(node.get("content", "")) if node else ""


def pluspower_image_urls_from_html(soup):
    """Collect canonical image and gallery images, preserving source order."""
    found = []
    selectors = [
        'link[itemprop="image"][href]',
        '#gallery a[href]',
        '#gallery img[src]',
        '#gallery source[srcset]',
    ]
    for selector in selectors:
        for node in soup.select(selector):
            value = node.get("href") or node.get("src") or node.get("srcset") or ""
            value = value.split(",")[0].strip().split(" ")[0]
            if value.startswith("//"):
                value = "https:" + value
            elif value.startswith("/"):
                value = "https://www.pluspowersupplements.com" + value
            if value.startswith("https://") and value not in found:
                found.append(value)
    return found
