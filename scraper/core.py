import json
import re
import sqlite3
import time
import csv
import io
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET


DB_PATH = "data/products.sqlite3"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PlusPowerProductImporter/2.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
ALLOWED_HOSTS = {"www.pluspowersupplements.com", "pluspowersupplements.com"}
PRODUCT_FIELDS = (
    "title", "brand", "price", "currency", "ean", "sku", "availability",
    "description", "image_urls", "ingredients", "nutrition", "variants",
    "raw_jsonld",
)


def db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS products (
      source_url TEXT PRIMARY KEY, title TEXT, brand TEXT, price TEXT, currency TEXT,
      ean TEXT, sku TEXT, availability TEXT, description TEXT, image_urls TEXT,
      ingredients TEXT, nutrition TEXT, variants TEXT, raw_jsonld TEXT,
      last_checked TEXT, status TEXT, error TEXT
    )""")
    con.commit()
    con.close()


def _local(tag):
    return tag.split("}")[-1].lower()


def _is_allowed_url(url):
    try:
        parsed = urlparse(str(url))
        return parsed.scheme in ("http", "https") and parsed.hostname in ALLOWED_HOSTS
    except Exception:
        return False


def _flatten_sitemap(root, base_url, session, depth=0):
    if depth > 4:
        return []
    urls = []
    for child in root:
        if _local(child.tag) == "loc" and child.text:
            urls.append(child.text.strip())
    if urls:
        return urls
    for child in root:
        if _local(child.tag) == "sitemap":
            loc = next(
                (x.text.strip() for x in child if _local(x.tag) == "loc" and x.text),
                None,
            )
            if loc:
                response = session.get(loc, timeout=25, headers=HEADERS)
                if _is_cloudflare_challenge(response):
                    raise RuntimeError("Sitemap isteğinde Cloudflare doğrulama sayfası döndü.")
                response.raise_for_status()
                urls.extend(
                    _flatten_sitemap(ET.fromstring(response.content), loc, session, depth + 1)
                )
    return urls


def import_sitemap(sitemap_url):
    if not _is_allowed_url(sitemap_url):
        raise ValueError("Yalnızca PlusPower sitemap adresi kabul edilir.")
    session = requests.Session()
    response = session.get(sitemap_url, headers=HEADERS, timeout=30)
    if response.status_code in (401, 403, 429):
        raise RuntimeError(
            f"Site sitemap isteğini reddetti (HTTP {response.status_code}). "
            "Engeli aşmaya çalışmadan durduruldu."
        )
    if _is_cloudflare_challenge(response):
        raise RuntimeError("Sitemap isteği Cloudflare doğrulama sayfasına yönlendirildi.")
    response.raise_for_status()
    urls = _flatten_sitemap(ET.fromstring(response.content), sitemap_url, session)
    excluded = (
        "/blog", "/nutrizione-sportiva", "/salute-e-benessere", "/controllo-del-peso",
        "/alimenti-fit", "/abbigliamento", "/marche", "/promo", "/last-minute",
        "/chi-siamo", "/contatti", "/privacy", "/cookie", "/registrati",
        "/condizioni-di-vendita", "/resi-e-rimborsi", "/rivenditore",
    )
    output = []
    for url in urls:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if parsed.hostname not in ALLOWED_HOSTS:
            continue
        if path and "/" not in path and not any(
            path.startswith(item.strip("/")) for item in excluded
        ):
            output.append(url)
    return list(dict.fromkeys(output))


def _walk_jsonld(obj, out):
    if isinstance(obj, dict):
        typ = obj.get("@type", [])
        if isinstance(typ, str):
            typ = [typ]
        if any(str(item).lower() == "product" for item in typ):
            out.append(obj)
        for value in obj.values():
            _walk_jsonld(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _walk_jsonld(value, out)


def _meta(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _clean(value):
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _brand_value(value):
    if isinstance(value, dict):
        return _clean(value.get("name"))
    return _clean(value)


def _first_text(soup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = _clean(node.get_text(" ", strip=True))
            if value:
                return value
    return ""


def _jsonld_product(soup):
    found = []
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            _walk_jsonld(json.loads(script.string or script.get_text()), found)
        except Exception:
            continue
    found.sort(
        key=lambda item: (
            bool(item.get("name")),
            bool(item.get("offers")),
            bool(item.get("brand")),
        ),
        reverse=True,
    )
    return found[0] if found else {}


def _extract_labeled_section(soup, labels):
    wanted = {item.lower() for item in labels}
    for node in soup.find_all(["section", "div", "article", "table"]):
        heading = node.find(["h2", "h3", "h4", "strong", "b"])
        if heading and _clean(heading.get_text()).lower().rstrip(":") in wanted:
            text = _clean(node.get_text(" ", strip=True))
            if len(text) > len(_clean(heading.get_text())) + 5:
                return text
    for selector in (
        ".ingredients", "#ingredients", "[class*='ingredient']",
        ".nutrition", "#nutrition", "[class*='nutri']",
    ):
        node = soup.select_one(selector)
        if node:
            text = _clean(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _is_cloudflare_challenge(response):
    """Detect common Cloudflare challenge pages, including HTTP 200 responses."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    text = str(getattr(response, "text", "") or "")
    sample = text[:60000].lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", sample, re.I | re.S)
    page_title = _clean(title_match.group(1)).lower() if title_match else ""

    markers = (
        "just a moment",
        "challenge-platform",
        "challenges.cloudflare.com",
        "cf-chl-",
        "__cf_chl_tk",
        "cloudflare ray id",
        "enable javascript and cookies to continue",
        "checking your browser",
        "attention required! | cloudflare",
    )
    server = str(headers.get("Server", headers.get("server", ""))).lower()
    cf_mitigated = str(headers.get("cf-mitigated", headers.get("CF-Mitigated", ""))).lower()

    if cf_mitigated == "challenge":
        return True
    if any(marker in sample for marker in markers):
        return True
    if "cloudflare" in page_title and status in (403, 503):
        return True
    if "cloudflare" in server and status in (403, 503):
        return True
    return False




def _meta_itemprop(soup, *names):
    for name in names:
        node = soup.find(attrs={"itemprop": name})
        if node:
            value = node.get("content") or node.get("value") or node.get_text(" ", strip=True)
            if value:
                return _clean(value)
    return ""


def _all_product_images(soup, product=None):
    images = []
    product = product or {}
    raw = product.get("image", []) if isinstance(product, dict) else []
    if isinstance(raw, str):
        raw = [raw]
    for value in raw:
        if isinstance(value, dict):
            value = value.get("url") or value.get("contentUrl") or ""
        value = _clean(value)
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("https://") and value not in images:
            images.append(value)

    for node in soup.select("picture source[srcset], img[srcset], img[src], img[data-src], img[data-large-image], img[data-original]"):
        for attr in ("srcset", "data-srcset"):
            srcset = node.get(attr, "")
            for candidate in srcset.split(","):
                url = candidate.strip().split(" ")[0]
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("https://") and url not in images:
                    images.append(url)
        for attr in ("data-large-image", "data-original", "data-src", "src"):
            url = _clean(node.get(attr))
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("https://") and url not in images:
                images.append(url)
    return images


def _find_js_object(text, variable_names):
    """Extract a balanced JavaScript object assigned to one of the named variables."""
    for name in variable_names:
        match = re.search(r"(?:var|let|const)?\s*" + re.escape(name) + r"\s*[:=]\s*\{", text, re.I)
        if not match:
            continue
        start = text.find("{", match.start())
        depth = 0
        quote = None
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ("'", '"', "`"):
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
    return ""


def _parse_js_object(raw):
    if not raw:
        return None
    # First try strict JSON, then a conservative conversion of common JS literals.
    try:
        return json.loads(raw)
    except Exception:
        pass
    converted = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', raw)
    converted = re.sub(r"\btrue\b", "true", converted, flags=re.I)
    converted = re.sub(r"\bfalse\b", "false", converted, flags=re.I)
    converted = re.sub(r"\bundefined\b", "null", converted)
    try:
        return json.loads(converted)
    except Exception:
        pass
    try:
        import ast
        py = converted.replace("null", "None").replace("true", "True").replace("false", "False")
        py = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", lambda m: json.dumps(m.group(1)), py)
        return ast.literal_eval(py)
    except Exception:
        return None


def _variant_records(soup):
    scripts = "\n".join(script.get_text(" ", strip=False) for script in soup.find_all("script"))
    raw = _find_js_object(scripts, ("varianti", "variants", "productVariants"))
    data = _parse_js_object(raw)
    records = []

    def walk(value, inherited=""):
        if isinstance(value, dict):
            # Treat objects with recognizable variant fields as variant records.
            lowered = {str(k).lower(): v for k, v in value.items()}
            keys = set(lowered)
            recognizable = any(
                token in key
                for key in keys
                for token in ("ean", "barcode", "codice", "sku", "variante", "gusto", "aroma", "disponibile", "quantita", "stock")
            )
            if recognizable:
                def pick(*names):
                    for key, val in lowered.items():
                        if any(name in key for name in names) and val not in (None, ""):
                            return _clean(val)
                    return ""
                name = pick("descrizione", "nome", "variante", "gusto", "aroma", "label", "valore") or inherited
                record = {
                    "name": name,
                    "variant_id": pick("idvariante", "id_variante", "variantid", "id"),
                    "product_code": pick("codice", "codprod", "cod_prodotto", "productcode", "sku"),
                    "ean": pick("ean", "gtin", "barcode", "codiceabarre"),
                    "stock_quantity": pick("quantita", "quantity", "giacenza", "stock", "disponibilita"),
                    "available_online": pick("disponibileweb", "disponibile_web", "availableonline", "available"),
                    "price": pick("prezzo", "price", "prezzoscontato", "prezzo_scontato"),
                }
                if any(record.values()):
                    records.append(record)
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, _clean(key))
        elif isinstance(value, list):
            for child in value:
                walk(child, inherited)

    if data is not None:
        walk(data)

    # Fallback: parse individual JS object blocks if the whole object is not valid JSON.
    if not records and raw:
        blocks = re.findall(r"\{[^{}]{20,2000}\}", raw, re.S)
        for block in blocks:
            fields = {}
            aliases = {
                "ean": r"(?:ean|gtin|barcode)\s*[:=]\s*['\"]?(\d{8,14})",
                "variant_id": r"(?:id_variante|idvariante|variant_id)\s*[:=]\s*['\"]?(\d+)",
                "product_code": r"(?:codice|codprod|cod_prodotto|sku)\s*[:=]\s*['\"]?([A-Za-z0-9_-]+)",
                "stock_quantity": r"(?:quantita|quantity|giacenza|stock)\s*[:=]\s*['\"]?(\d+)",
            }
            for field, pattern in aliases.items():
                found = re.search(pattern, block, re.I)
                fields[field] = found.group(1) if found else ""
            if fields.get("ean"):
                name = re.search(r"(?:nome|descrizione|gusto|variante)\s*[:=]\s*['\"]([^'\"]+)", block, re.I)
                fields["name"] = name.group(1) if name else ""
                fields["available_online"] = ""
                fields["price"] = ""
                records.append(fields)
    # De-duplicate without collapsing distinct flavors that share other attributes.
    unique = []
    seen = set()
    for item in records:
        key = (item.get("variant_id"), item.get("ean"), item.get("product_code"), item.get("name"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _extract_nutrition_table(soup):
    rows = []
    for table in soup.find_all("table"):
        text = _clean(table.get_text(" ", strip=True))
        if not re.search(r"energia|energy|proteine|protein|grassi|fat|carboidrati", text, re.I):
            continue
        for tr in table.find_all("tr"):
            cells = [_clean(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(" | ".join(cells))
    return "\n".join(rows)


def _extract_ingredients_text(soup):
    for node in soup.select("[class*='ingredient'], [id*='ingredient'], [class*='composizione']"):
        value = _clean(node.get_text(" ", strip=True))
        if len(value) > 20:
            return value
    for node in soup.find_all(["div", "section", "p", "td"]):
        value = _clean(node.get_text(" ", strip=True))
        if re.match(r"^(ingredienti|ingredients|composizione)\s*[:\-]", value, re.I) and len(value) > 25:
            return value
    return ""


def extract_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    product = _jsonld_product(soup)
    offers = product.get("offers", {}) if isinstance(product, dict) else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}

    title = (
        _clean(product.get("name"))
        or _meta_itemprop(soup, "name")
        or _first_text(soup, ["main h1", "h1.product-title", "h1"])
        or _meta(soup, "og:title")
    )
    if len(title) > 180:
        title = ""

    brand = _brand_value(product.get("brand"))
    if not brand:
        brand = _meta_itemprop(soup, "brand")
    if not brand:
        brand = _meta(soup, "product:brand", "brand", "og:brand")
    if not brand:
        brand = _first_text(soup, [".product-brand", "[itemprop='brand']", "a.brand", "[class*='brand-name']"])

    price = (
        _clean(offers.get("price"))
        or _meta_itemprop(soup, "price")
        or _meta(soup, "product:price:amount")
    )
    currency = (
        _clean(offers.get("priceCurrency"))
        or _meta_itemprop(soup, "priceCurrency")
        or _meta(soup, "product:price:currency")
        or ("EUR" if price else "")
    )
    availability = _clean(offers.get("availability") or _meta_itemprop(soup, "availability"))
    sku = _clean(product.get("sku") or _meta_itemprop(soup, "sku") or _meta(soup, "product:retailer_item_id"))
    ean = _clean(
        product.get("gtin13") or product.get("gtin14") or product.get("gtin12")
        or product.get("gtin8") or product.get("gtin") or _meta_itemprop(soup, "gtin13", "gtin")
    )
    description = (
        _clean(product.get("description"))
        or _meta(soup, "og:description", "description")
        or _first_text(soup, [".product-description", ".description"])
    )

    images = _all_product_images(soup, product)
    ingredients = _extract_ingredients_text(soup) or _extract_labeled_section(
        soup, ["ingredienti", "ingredients", "composizione"]
    )
    nutrition = _extract_nutrition_table(soup) or _extract_labeled_section(
        soup, ["valori nutrizionali", "informazioni nutrizionali", "nutrition facts", "nutritional information"]
    )
    variants = _variant_records(soup)

    # If the page-level SKU/EAN duplicates one variant's code, keep it in the
    # variant record and avoid presenting it as a universal product identifier.
    variant_eans = {v.get("ean") for v in variants if v.get("ean")}
    if ean in variant_eans and len(variant_eans) > 1:
        ean = ""
    variant_codes = {v.get("product_code") for v in variants if v.get("product_code")}
    if sku in variant_codes and len(variant_codes) > 1:
        sku = ""

    source_title = title
    source_description = description
    ebay_title = _ebay_title(source_title, brand, source_description)
    ebay_description = _ebay_description(
        source_title, brand, source_description, ingredients, nutrition, ean, sku
    )

    missing = [
        key for key, value in [
            ("title", source_title), ("brand", brand), ("price", price), ("images", images),
            ("description", source_description),
        ] if not value
    ]
    status = "needs_review" if missing else "ready_for_review"

    return {
        "source_url": url,
        "title": ebay_title,
        "brand": brand,
        "price": price,
        "currency": currency,
        "ean": ean,
        "sku": sku,
        "availability": availability,
        "description": ebay_description,
        "image_urls": json.dumps(images, ensure_ascii=False),
        "ingredients": ingredients,
        "nutrition": nutrition,
        "variants": json.dumps(variants, ensure_ascii=False),
        "raw_jsonld": json.dumps(
            {"jsonld_product": product, "parser": "pluspower_html_schema_variants_v2"},
            ensure_ascii=False,
        ),
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "error": ("Kaynak: doğrudan HTML. " + ("Eksik alanlar: " + ", ".join(missing) if missing else "Temel alanlar bulundu; varyant ve stok bilgilerini doğrula.")),
    }


def parse_pasted_product(text, source_url=""):
    raw = text.strip()
    lines = [item.strip() for item in raw.splitlines() if item.strip()]
    title = ""
    for line in lines[:20]:
        if len(line) < 140 and re.search(
            r"\b\d+(?:[.,]\d+)?\s*(?:g|gr|kg|ml)\b", line, re.I
        ):
            title = line
            break

    price = ""
    for line in lines[:50]:
        match = re.search(r"€\s*([0-9][0-9.,]*)", line)
        if match:
            price = match.group(1).replace(".", "").replace(",", ".")
            break

    ean = ""
    match = re.search(r"\b(?:EAN|GTIN)\s*[:：]?\s*(\d{8,14})", raw, re.I)
    if match:
        ean = match.group(1)

    sku = ""
    match = re.search(r"\b(?:Codice|SKU|Cod\.)\s*[:：]?\s*([A-Za-z0-9_-]+)", raw, re.I)
    if match:
        sku = match.group(1)

    brand = ""
    match = re.search(r"(?:Marca|Brand|Produttore)\s*[:：]\s*([^\n]+)", raw, re.I)
    if match:
        brand = match.group(1).strip()

    return {
        "source_url": source_url.strip(),
        "title": title,
        "brand": brand,
        "price": price,
        "currency": "EUR" if price else "",
        "ean": ean,
        "sku": sku,
        "availability": "",
        "description": "",
        "image_urls": "[]",
        "ingredients": "",
        "nutrition": "",
        "variants": "[]",
        "raw_jsonld": "{}",
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "needs_review",
        "error": "Metin aktarımı alanları doğrulanmalı.",
    }


def _merge_preserving_existing(existing, incoming):
    """
    Never replace a previously populated field with an empty value.
    Incoming non-empty values may refresh existing values.
    """
    merged = dict(existing or {})
    for key, value in incoming.items():
        if key == "source_url":
            merged[key] = value
        elif key in ("last_checked", "status", "error"):
            merged[key] = value
        elif value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def save_product(item):
    con = db()
    existing_row = con.execute(
        "SELECT * FROM products WHERE source_url = ?", (item.get("source_url", ""),)
    ).fetchone()
    existing = dict(existing_row) if existing_row else None
    safe_item = _merge_preserving_existing(existing, item)

    cols = list(safe_item.keys())
    placeholders = ",".join(["?"] * len(cols))
    updates = ",".join(
        f"{column}=excluded.{column}" for column in cols if column != "source_url"
    )
    con.execute(
        f"INSERT INTO products ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source_url) DO UPDATE SET {updates}",
        [safe_item[column] for column in cols],
    )
    con.commit()
    con.close()


def _parse_tavily_result(url, result):
    content = str(result.get("raw_content") or result.get("content") or "")
    lines = [
        re.sub(r"\s+", " ", re.sub(r"^#{1,6}\s*", "", item)).strip()
        for item in content.splitlines() if item.strip()
    ]

    # Search snippets are not sufficient evidence for a reliable product title.
    title = ""
    for line in lines[:35]:
        candidate = re.sub(r"^#{1,6}\s*", "", line).strip(" -*")
        if (
            5 <= len(candidate) <= 120
            and not re.search(
                r"valori nutrizionali|nutrition facts|ingredienti|ingredients|"
                r"per dose|per 100 g",
                candidate,
                re.I,
            )
            and re.search(r"[A-Za-z]", candidate)
        ):
            title = candidate
            break

    def labeled(patterns):
        for line in lines:
            for pattern in patterns:
                match = re.search(pattern + r"\s*[:：]\s*(.+)", line, re.I)
                if match:
                    return match.group(1).strip()
        return ""

    brand = labeled([r"brand", r"marca", r"marchio", r"produttore"])
    price = ""
    currency = ""
    for line in lines[:100]:
        match = re.search(
            r"(?:€\s*([0-9][0-9.,]*)|([0-9][0-9.,]*)\s*€)", line
        )
        if match:
            price = (match.group(1) or match.group(2)).replace(".", "").replace(",", ".")
            currency = "EUR"
            break

    ean = labeled([r"ean", r"gtin", r"barcode", r"codice a barre"])
    sku = labeled([r"sku", r"codice prodotto", r"product code"])
    availability = labeled([r"disponibilità", r"availability", r"stock"])

    description = ""
    for line in lines:
        if len(line) > 160 and not re.search(
            r"valori nutrizionali|per dose|per 100 g", line, re.I
        ):
            description = line
            break

    def section(start_patterns, stop_patterns):
        start = None
        for index, line in enumerate(lines):
            if any(re.search(pattern, line, re.I) for pattern in start_patterns):
                start = index
                break
        if start is None:
            return ""
        collected = []
        for line in lines[start:start + 35]:
            if collected and any(
                re.search(pattern, line, re.I) for pattern in stop_patterns
            ):
                break
            collected.append(line)
        return "\n".join(collected)

    ingredients = section(
        [r"^ingredienti\b", r"^ingredients\b", r"^composizione\b"],
        [r"^valori nutrizionali\b", r"^nutrition", r"^modo d'uso", r"^allergeni"],
    )
    nutrition = section(
        [r"^valori nutrizionali\b", r"^nutrition facts", r"^informazioni nutrizionali"],
        [r"^ingredienti\b", r"^ingredients\b", r"^modo d'uso", r"^allergeni"],
    )

    images = result.get("images") or []
    if isinstance(images, str):
        images = [images]
    images = [
        image for image in images
        if isinstance(image, str) and image.startswith("https://")
    ]

    missing = [
        name for name, value in [
            ("title", title), ("brand", brand), ("price", price)
        ] if not value
    ]
    return {
        "source_url": url,
        "title": title,
        "brand": brand,
        "price": price,
        "currency": currency,
        "ean": ean,
        "sku": sku,
        "availability": availability,
        "description": description,
        "image_urls": json.dumps(images, ensure_ascii=False),
        "ingredients": ingredients,
        "nutrition": nutrition,
        "variants": "[]",
        "raw_jsonld": json.dumps(
            {"provider": "Tavily Search", "raw_content": content},
            ensure_ascii=False,
        ),
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "needs_review",
        "error": (
            "Kaynak: Tavily Search. Manuel doğrulama gerekli. Eksik alanlar: "
            + (", ".join(missing) if missing else "temel alanlar mevcut")
        ),
    }


def _tavily_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _tavily_error(response, payload=None):
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("code")
            if message:
                return str(message)[:500]
        elif error:
            return str(error)[:500]
        failed = payload.get("failed_results") or []
        if failed:
            parts = []
            for item in failed[:3]:
                if isinstance(item, dict):
                    parts.append(
                        str(item.get("error") or item.get("message") or "URL çıkarılamadı")
                    )
            if parts:
                return "; ".join(parts)[:500]
    try:
        body = response.text[:350].replace("\n", " ")
    except Exception:
        body = ""
    return body or f"Tavily HTTP {getattr(response, 'status_code', 'bilinmiyor')}"


def _tavily_post(endpoint, payload, api_key, timeout=60):
    """Single Tavily request implementation with consistent error handling."""
    response = requests.post(
        f"https://api.tavily.com/{endpoint}",
        json=payload,
        headers=_tavily_headers(api_key),
        timeout=timeout,
    )
    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Tavily API kimlik doğrulama/izin hatası (HTTP {response.status_code}). "
            "Streamlit Secrets içindeki TAVILY_API_KEY değerini kontrol et."
        )
    if response.status_code == 429:
        raise RuntimeError("Tavily API kullanım limiti aşıldı (HTTP 429). Daha sonra tekrar dene.")
    if not response.ok:
        raise RuntimeError(
            f"Tavily {endpoint} hatası (HTTP {response.status_code}): "
            f"{_tavily_error(response, data)}"
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"Tavily {endpoint} geçerli JSON döndürmedi.")
    return data


def _tavily_extract(url, api_key):
    payload = _tavily_post(
        "extract",
        {
            "urls": [url],
            "extract_depth": "advanced",
            "format": "markdown",
            "include_images": True,
        },
        api_key,
        timeout=65,
    )
    results = payload.get("results") or []
    if results:
        return results[0], payload
    reason = _tavily_error(
        type("Response", (), {"status_code": 200, "text": ""})(), payload
    )
    raise RuntimeError(f"Extract başarılı içerik döndürmedi: {reason}")


def _tavily_search_candidates(url, api_key):
    parsed = urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    readable_slug = re.sub(r"[-_]+", " ", slug).strip()
    query = f'site:{parsed.hostname} "{readable_slug}"'
    payload = _tavily_post(
        "search",
        {
            "query": query,
            "topic": "general",
            "search_depth": "advanced",
            "max_results": 5,
            "include_domains": [parsed.hostname],
            "include_raw_content": "markdown",
            "include_answer": False,
        },
        api_key,
        timeout=45,
    )

    candidates = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        candidate_url = result.get("url", "")
        candidate_parsed = urlparse(candidate_url)
        if (
            candidate_parsed.scheme in ("http", "https")
            and candidate_parsed.hostname in ALLOWED_HOSTS
            and candidate_parsed.path.rstrip("/").lower()
                == parsed.path.rstrip("/").lower()
        ):
            candidates.append(result)
    return candidates, payload


def tavily_extract_product(url, api_key):
    if not api_key:
        raise RuntimeError(
            "Tavily API anahtarı tanımlı değil. Streamlit Secrets içindeki "
            "TAVILY_API_KEY ayarını kontrol et."
        )
    if not _is_allowed_url(url):
        raise ValueError("Tavily için yalnızca PlusPower ürün URL'leri kabul edilir.")

    extract_error = ""
    try:
        result, _payload = _tavily_extract(url, api_key)
        raw = result.get("raw_content") or result.get("content") or ""
        if raw.strip():
            item = _parse_tavily_result(url, result)
            item["error"] = "Kaynak: Tavily Extract. Alanlar manuel doğrulanmalı."
            return item
        extract_error = "Extract sonuç döndürdü ancak kullanılabilir içerik yok."
    except Exception as exc:
        extract_error = str(exc)[:400]

    # Search is used as a separate public-search fallback. It does not bypass
    # Cloudflare, solve challenges, or retry the blocked page through another route.
    try:
        candidates, _search_payload = _tavily_search_candidates(url, api_key)
        for candidate in candidates:
            raw = candidate.get("raw_content") or candidate.get("content") or ""
            if raw.strip():
                item = _parse_tavily_result(url, candidate)
                item["error"] = (
                    "Kaynak: Tavily Search. Ürün URL'si eşleşti; "
                    "alanları manuel doğrula."
                )
                return item
        search_error = "Aynı ürün URL'si için kullanılabilir arama içeriği bulunamadı."
    except Exception as exc:
        search_error = str(exc)[:400]

    raise RuntimeError(
        "Tavily ürün bilgisi sağlayamadı. "
        f"Extract: {extract_error} | Search: {search_error}"
    )


def _is_complete_cached_product(item):
    if not item:
        return False
    return bool(
        _clean(item.get("title"))
        and _clean(item.get("brand"))
        and _clean(item.get("price"))
    )


def scrape_urls(urls, delay_seconds=5, callback=None, tavily_api_key=None):
    results = []
    session = requests.Session()

    for index, url in enumerate(urls, 1):
        try:
            if not _is_allowed_url(url):
                raise ValueError("Yalnızca PlusPower ürün URL'leri kabul edilir.")

            # Avoid repeated network/API work for products already successfully
            # processed. Incomplete records may be retried to fill missing fields.
            cached = get_product(url)
            if _is_complete_cached_product(cached):
                result = {
                    "url": url,
                    "title": cached.get("title", ""),
                    "brand": cached.get("brand", ""),
                    "status": cached.get("status", "ready_for_review"),
                    "price": cached.get("price", ""),
                    "error": "Mevcut kayıt kullanıldı; yeniden istek gönderilmedi.",
                }
                results.append(result)
                if callback:
                    callback(index, len(urls), url, result["status"])
                if index < len(urls):
                    time.sleep(max(5, int(delay_seconds)))
                continue

            response = session.get(url, headers=HEADERS, timeout=30)

            if response.status_code == 429:
                raise RuntimeError(
                    "Site HTTP 429 döndürdü. İstekler durduruldu; daha sonra tekrar dene."
                )

            blocked = response.status_code in (401, 403) or _is_cloudflare_challenge(response)
            if blocked:
                if tavily_api_key:
                    item = tavily_extract_product(url, tavily_api_key)
                else:
                    raise RuntimeError(
                        f"Site erişimi engellendi (HTTP {response.status_code}). "
                        "Cloudflare doğrulaması algılandı veya erişim reddedildi. "
                        "Tavily anahtarı yok; ürün kaydedilmedi."
                    )
            else:
                response.raise_for_status()
                item = extract_product(url, response.text)
                item["error"] = (
                    ("Kaynak: doğrudan ürün sayfası. " + item["error"]).strip()
                )

            if not item.get("title") and not item.get("brand") and not item.get("price"):
                raise RuntimeError(
                    "İçerik alındı ancak ürün alanları doğrulanamadı; kayıt oluşturulmadı."
                )

            # Existing populated fields are retained by save_product().
            save_product(item)
            result = {
                "url": url,
                "title": item.get("title", ""),
                "brand": item.get("brand", ""),
                "status": item.get("status", "needs_review"),
                "price": item.get("price", ""),
                "error": item.get("error", ""),
            }

        except Exception as exc:
            result = {
                "url": url,
                "title": "",
                "brand": "",
                "status": "blocked_or_failed",
                "price": "",
                "error": str(exc),
            }

        results.append(result)
        if callback:
            callback(index, len(urls), url, result["status"])

        # Do not keep sending requests after an explicit rate limit or block.
        error_text = result.get("error", "").lower()
        if "http 429" in error_text or "http 403" in error_text:
            break

        if index < len(urls):
            time.sleep(max(5, int(delay_seconds)))

    return results


def list_products(search=""):
    con = db()
    if search:
        rows = con.execute(
            "SELECT * FROM products WHERE title LIKE ? OR brand LIKE ? "
            "ORDER BY last_checked DESC",
            (f"%{search}%", f"%{search}%"),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM products ORDER BY last_checked DESC"
        ).fetchall()
    con.close()
    return [dict(row) for row in rows]


def count_products():
    con = db()
    count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    con.close()
    return count


def get_product(url):
    con = db()
    row = con.execute(
        "SELECT * FROM products WHERE source_url = ?", (url,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def export_csv():
    rows = list_products()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def delete_all_products():
    con = db()
    con.execute("DELETE FROM products")
    con.commit()
    con.close()


# ---------------- Configurable HTML CSS selectors ----------------
SELECTOR_FIELDS = (
    "title", "brand", "price", "ean", "sku", "availability",
    "description", "ingredients", "nutrition", "images", "variants",
)


def init_selector_settings():
    """Create persistent selector settings without touching product records."""
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL DEFAULT ''
    )""")
    con.commit()
    con.close()


def get_selector_settings():
    init_selector_settings()
    con = db()
    rows = con.execute(
        "SELECT setting_key, setting_value FROM app_settings WHERE setting_key LIKE 'selector:%'"
    ).fetchall()
    con.close()
    saved = {row["setting_key"].split(":", 1)[1]: row["setting_value"] for row in rows}
    return {field: saved.get(field, "") for field in SELECTOR_FIELDS}


def save_selector_settings(settings):
    init_selector_settings()
    con = db()
    for field in SELECTOR_FIELDS:
        value = str(settings.get(field, "") or "").strip()
        con.execute(
            "INSERT INTO app_settings(setting_key, setting_value) VALUES(?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
            ("selector:" + field, value),
        )
    con.commit()
    con.close()


def _selector_node_value(node, field):
    if field == "images":
        return node.get("data-large-image") or node.get("data-src") or node.get("data-original") or node.get("src") or node.get("srcset") or node.get("content") or ""
    if node.name == "meta":
        return node.get("content", "")
    if node.name in ("img", "source"):
        return node.get("data-large-image") or node.get("data-src") or node.get("src") or node.get("srcset") or ""
    return node.get("content") or node.get("value") or node.get_text(" ", strip=True)


def test_css_selectors(html, selectors):
    """Return match counts and samples for each configured CSS selector."""
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    for field in SELECTOR_FIELDS:
        selector = str(selectors.get(field, "") or "").strip()
        if not selector:
            results[field] = {"selector": "", "count": 0, "sample": "", "error": ""}
            continue
        try:
            nodes = soup.select(selector)
            samples = [_clean(_selector_node_value(node, field))[:220] for node in nodes[:3]]
            results[field] = {
                "selector": selector, "count": len(nodes),
                "sample": " | ".join(value for value in samples if value), "error": "",
            }
        except Exception as exc:
            results[field] = {"selector": selector, "count": -1, "sample": "", "error": str(exc)}
    return results


def extract_product_with_selectors(url, html, selectors=None):
    """Use the existing parser first, then configured CSS selectors as explicit overrides."""
    selectors = selectors if selectors is not None else get_selector_settings()
    item = extract_product(url, html)
    soup = BeautifulSoup(html, "html.parser")

    for field in ("title", "brand", "price", "ean", "sku", "availability", "description", "ingredients", "nutrition"):
        selector = str(selectors.get(field, "") or "").strip()
        if not selector:
            continue
        try:
            nodes = soup.select(selector)
        except Exception as exc:
            raise ValueError(f"CSS seçicisi hatalı ({field}): {exc}") from exc
        values = [_clean(_selector_node_value(node, field)) for node in nodes]
        values = [value for value in values if value]
        if values:
            item[field] = values[0]

    for field, output_key in (("images", "image_urls"), ("variants", "variants")):
        selector = str(selectors.get(field, "") or "").strip()
        if not selector:
            continue
        try:
            nodes = soup.select(selector)
        except Exception as exc:
            raise ValueError(f"CSS seçicisi hatalı ({field}): {exc}") from exc
        values = []
        for node in nodes:
            value = _clean(_selector_node_value(node, field))
            if value and value not in values:
                values.append(value)
        if values:
            if field == "images":
                normalized = []
                for value in values:
                    candidate = value.split(",")[0].strip().split(" ")[0]
                    if candidate.startswith("//"):
                        candidate = "https:" + candidate
                    elif candidate.startswith("/"):
                        candidate = "https://www.pluspowersupplements.com" + candidate
                    if candidate.startswith("https://") and candidate not in normalized:
                        normalized.append(candidate)
                if normalized:
                    item[output_key] = json.dumps(normalized, ensure_ascii=False)
            else:
                item[output_key] = json.dumps([{"name": value} for value in values], ensure_ascii=False)

    item["selector_fields_used"] = [field for field in SELECTOR_FIELDS if str(selectors.get(field, "") or "").strip()]
    return item
