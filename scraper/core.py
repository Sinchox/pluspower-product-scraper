import json, re, sqlite3, time, csv, io, os
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

DB_PATH = "data/products.sqlite3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PlusPowerProductImporter/2.0)"}

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
    con.commit(); con.close()

def _local(tag):
    return tag.split("}")[-1].lower()

def _flatten_sitemap(root, base_url, session, depth=0):
    if depth > 4: return []
    urls = []
    for child in root:
        if _local(child.tag) == "loc" and child.text:
            urls.append(child.text.strip())
    if urls: return urls
    for child in root:
        if _local(child.tag) == "sitemap":
            loc = next((x.text.strip() for x in child if _local(x.tag) == "loc" and x.text), None)
            if loc:
                r = session.get(loc, timeout=25, headers=HEADERS)
                r.raise_for_status()
                urls.extend(_flatten_sitemap(ET.fromstring(r.content), loc, session, depth+1))
    return urls

def import_sitemap(sitemap_url):
    p = urlparse(sitemap_url)
    if p.scheme not in ("http","https") or p.netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"):
        raise ValueError("Yalnızca PlusPower sitemap adresi kabul edilir.")
    session = requests.Session()
    r = session.get(sitemap_url, headers=HEADERS, timeout=30)
    if r.status_code in (401,403,429):
        raise RuntimeError(f"Site sitemap isteğini reddetti (HTTP {r.status_code}). Engeli aşmaya çalışmadan durduruldu.")
    r.raise_for_status()
    urls = _flatten_sitemap(ET.fromstring(r.content), sitemap_url, session)
    excluded = ("/blog", "/nutrizione-sportiva", "/salute-e-benessere", "/controllo-del-peso",
                "/alimenti-fit", "/abbigliamento", "/marche", "/promo", "/last-minute",
                "/chi-siamo", "/contatti", "/privacy", "/cookie", "/registrati",
                "/condizioni-di-vendita", "/resi-e-rimborsi", "/rivenditore")
    out=[]
    for u in urls:
        p=urlparse(u); path=p.path.strip("/")
        if p.netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"): continue
        if path and "/" not in path and not any(path.startswith(x.strip("/")) for x in excluded):
            out.append(u)
    return list(dict.fromkeys(out))

def _walk_jsonld(obj, out):
    if isinstance(obj, dict):
        typ=obj.get("@type", [])
        if isinstance(typ,str): typ=[typ]
        if any(str(t).lower()=="product" for t in typ): out.append(obj)
        for v in obj.values(): _walk_jsonld(v,out)
    elif isinstance(obj,list):
        for v in obj: _walk_jsonld(v,out)

def _meta(soup, *names):
    for name in names:
        tag=soup.find("meta", attrs={"property":name}) or soup.find("meta", attrs={"name":name})
        if tag and tag.get("content"): return tag["content"].strip()
    return ""

def _clean(value):
    if isinstance(value, list): value = " ".join(str(x) for x in value)
    return re.sub(r"\s+", " ", str(value or "")).strip()

def _brand_value(value):
    if isinstance(value, dict): return _clean(value.get("name"))
    return _clean(value)

def _first_text(soup, selectors):
    for sel in selectors:
        node=soup.select_one(sel)
        if node:
            val=_clean(node.get_text(" ",strip=True))
            if val: return val
    return ""

def _jsonld_product(soup):
    found=[]
    for script in soup.find_all("script",type=re.compile("ld\\+json",re.I)):
        try: _walk_jsonld(json.loads(script.string or script.get_text()),found)
        except Exception: continue
    # Prefer a Product node with a name and offers, rather than the first Product fragment.
    found.sort(key=lambda x: (bool(x.get("name")), bool(x.get("offers")), bool(x.get("brand"))), reverse=True)
    return found[0] if found else {}

def _extract_labeled_section(soup, labels):
    wanted={x.lower() for x in labels}
    for node in soup.find_all(["section","div","article","table"]):
        heading=node.find(["h2","h3","h4","strong","b"])
        if heading and _clean(heading.get_text()).lower().rstrip(":") in wanted:
            txt=_clean(node.get_text(" ",strip=True))
            if len(txt)>len(_clean(heading.get_text()))+5:
                return txt
    # Common structured selectors
    for sel in [".ingredients", "#ingredients", "[class*='ingredient']", ".nutrition", "#nutrition", "[class*='nutri']"]:
        node=soup.select_one(sel)
        if node:
            txt=_clean(node.get_text(" ",strip=True))
            if txt: return txt
    return ""

def extract_product(url, html):
    soup=BeautifulSoup(html,"html.parser")
    product=_jsonld_product(soup)
    offers=product.get("offers",{}) if isinstance(product,dict) else {}
    if isinstance(offers,list): offers=offers[0] if offers else {}
    if not isinstance(offers,dict): offers={}
    # Title priority: Product JSON-LD name, actual H1, product-specific title metadata.
    title=_clean(product.get("name")) or _first_text(soup,["main h1","h1.product-title","h1"]) or _meta(soup,"og:title")
    # Never use long SEO description as product title.
    if len(title)>180: title=""
    brand=_brand_value(product.get("brand"))
    if not brand:
        brand=_meta(soup,"product:brand","brand","og:brand")
    if not brand:
        brand=_first_text(soup,[".product-brand","[itemprop='brand']","a.brand","[class*='brand-name']"])
    price=_clean(offers.get("price") or _meta(soup,"product:price:amount"))
    currency=_clean(offers.get("priceCurrency") or _meta(soup,"product:price:currency"))
    availability=_clean(offers.get("availability"))
    sku=_clean(product.get("sku") or _meta(soup,"product:retailer_item_id"))
    ean=_clean(product.get("gtin13") or product.get("gtin14") or product.get("gtin12") or product.get("gtin8") or product.get("gtin"))
    description=_clean(product.get("description")) or _meta(soup,"og:description","description") or _first_text(soup,[".product-description",".description"])
    # Keep a long description out of title and avoid promotional snippets being treated as structured facts.
    images=product.get("image",[]) if isinstance(product,dict) else []
    if isinstance(images,str): images=[images]
    if not images:
        images=[]
        for sel in ["main img","img.product-image","img[itemprop='image']"]:
            for img in soup.select(sel):
                src=img.get("src") or img.get("data-src") or img.get("data-large-image")
                if src and src.startswith("http") and src not in images: images.append(src)
    nutrition=_extract_labeled_section(soup,["valori nutrizionali","informazioni nutrizionali","nutrition facts","nutritional information"])
    ingredients=_extract_labeled_section(soup,["ingredienti","ingredients","composizione"])
    # Variant names are collected only from explicit option controls.
    variants=[]
    for node in soup.select("select option, [data-attribute], .swatch, .variant-option"):
        val=_clean(node.get("value") or node.get("data-attribute") or node.get_text(" ",strip=True))
        if val and val.lower() not in ("select","seleziona","choose","scegli"): variants.append(val)
    variants=list(dict.fromkeys(variants))
    missing=[]
    for key,val in [("title",title),("brand",brand),("price",price),("images",images),("description",description)]:
        if not val: missing.append(key)
    # A technically fetched page is not automatically a valid product record.
    status="needs_review" if missing else "ready_for_review"
    return {
        "source_url":url,"title":title,"brand":brand,"price":price,
        "currency":currency,"ean":ean,"sku":sku,"availability":availability,
        "description":description,"image_urls":json.dumps(images,ensure_ascii=False),
        "ingredients":ingredients,"nutrition":nutrition,
        "variants":json.dumps(variants,ensure_ascii=False),
        "raw_jsonld":json.dumps(product,ensure_ascii=False),
        "last_checked":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status":status,"error":("Eksik alanlar: "+", ".join(missing)) if missing else ""
    }

def parse_pasted_product(text, source_url=""):
    raw=text.strip(); lines=[x.strip() for x in raw.splitlines() if x.strip()]
    title=""
    for line in lines[:20]:
        if len(line)<140 and re.search(r"\b\d+(?:[.,]\d+)?\s*(?:g|gr|kg|ml)\b",line,re.I):
            title=line; break
    price=""
    for line in lines[:50]:
        m=re.search(r"€\s*([0-9][0-9.,]*)",line)
        if m: price=m.group(1).replace(".","").replace(",","."); break
    ean=""
    m=re.search(r"\b(?:EAN|GTIN)\s*[:：]?\s*(\d{8,14})",raw,re.I)
    if m: ean=m.group(1)
    sku=""
    m=re.search(r"\b(?:Codice|SKU|Cod\.)\s*[:：]?\s*([A-Za-z0-9_-]+)",raw,re.I)
    if m: sku=m.group(1)
    # Do not guess brand from an unrelated fixed brand list.
    brand=""
    m=re.search(r"(?:Marca|Brand|Produttore)\s*[:：]\s*([^\n]+)",raw,re.I)
    if m: brand=m.group(1).strip()
    return {
      "source_url":source_url.strip(),"title":title,"brand":brand,"price":price,
      "currency":"EUR" if price else "","ean":ean,"sku":sku,"availability":"",
      "description":"","image_urls":"[]","ingredients":"","nutrition":"",
      "variants":"[]","raw_jsonld":"{}",
      "last_checked":datetime.now(timezone.utc).isoformat(timespec="seconds"),
      "status":"needs_review","error":"Metin aktarımı alanları doğrulanmalı."
    }

def save_product(item):
    con=db(); cols=list(item.keys()); placeholders=",".join(["?"]*len(cols))
    updates=",".join([f"{c}=excluded.{c}" for c in cols if c!="source_url"])
    con.execute(f"INSERT INTO products ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(source_url) DO UPDATE SET {updates}",[item[c] for c in cols])
    con.commit(); con.close()



def _parse_tavily_result(url, result):
    content = str(result.get("raw_content") or "")
    lines = [re.sub(r"\s+", " ", re.sub(r"^#{1,6}\s*", "", x)).strip() for x in content.splitlines() if x.strip()]
    # Prefer a concise top-level heading; never accept nutrition table headings as product names.
    title = ""
    for line in lines[:35]:
        candidate = re.sub(r"^#{1,6}\s*", "", line).strip(" -*")
        if (len(candidate) <= 120 and len(candidate) >= 5
            and not re.search(r"valori nutrizionali|nutrition facts|ingredienti|ingredients|per dose|per 100 g", candidate, re.I)
            and re.search(r"[A-Za-z]", candidate)):
            title = candidate
            break
    # URL slug is only a fallback for a readable title, not a brand assertion.
    if not title:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        title = re.sub(r"[-_]+", " ", slug).strip().title()

    def labeled(patterns):
        for line in lines:
            for pattern in patterns:
                m = re.search(pattern + r"\s*[:：]\s*(.+)", line, re.I)
                if m:
                    return m.group(1).strip()
        return ""

    brand = labeled([r"brand", r"marca", r"marchio", r"produttore"])
    # Product pages often show brand as a standalone heading/link. Accept only explicit brand context.
    price = ""
    currency = ""
    for line in lines[:100]:
        m = re.search(r"(?:€\s*([0-9][0-9.,]*)|([0-9][0-9.,]*)\s*€)", line)
        if m:
            price = (m.group(1) or m.group(2)).replace(".", "").replace(",", ".")
            currency = "EUR"
            break
    ean = labeled([r"ean", r"gtin", r"barcode", r"codice a barre"])
    sku = labeled([r"sku", r"codice prodotto", r"product code"])
    availability = labeled([r"disponibilità", r"availability", r"stock"])
    description = ""
    for line in lines:
        if len(line) > 160 and not re.search(r"valori nutrizionali|per dose|per 100 g", line, re.I):
            description = line
            break

    def section(start_patterns, stop_patterns):
        start = None
        for idx, line in enumerate(lines):
            if any(re.search(p, line, re.I) for p in start_patterns):
                start = idx
                break
        if start is None: return ""
        collected = []
        for line in lines[start:start+35]:
            if collected and any(re.search(p, line, re.I) for p in stop_patterns):
                break
            collected.append(line)
        return "\n".join(collected)

    ingredients = section([r"^ingredienti\b", r"^ingredients\b", r"^composizione\b"],
                          [r"^valori nutrizionali\b", r"^nutrition", r"^modo d'uso", r"^allergeni"])
    nutrition = section([r"^valori nutrizionali\b", r"^nutrition facts", r"^informazioni nutrizionali"],
                        [r"^ingredienti\b", r"^ingredients\b", r"^modo d'uso", r"^allergeni"])
    images = result.get("images") or []
    if isinstance(images, str): images = [images]
    images = [x for x in images if isinstance(x, str) and x.startswith("http")]
    missing = [name for name, val in [("title", title), ("brand", brand), ("price", price)] if not val]
    return {
        "source_url": url, "title": title, "brand": brand, "price": price,
        "currency": currency, "ean": ean, "sku": sku, "availability": availability,
        "description": description, "image_urls": json.dumps(images, ensure_ascii=False),
        "ingredients": ingredients, "nutrition": nutrition, "variants": "[]",
        "raw_jsonld": json.dumps({"provider": "Tavily Extract", "raw_content": content}, ensure_ascii=False),
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "needs_review", "error": "Tavily ile çıkarıldı; doğrulanması gereken alanlar: " + (", ".join(missing) if missing else "tüm temel alanlar")
    }


def _tavily_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

def _tavily_error(response, payload=None):
    """Return a safe, useful provider error without exposing credentials."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or err.get("code")
            if msg:
                return str(msg)[:500]
        elif err:
            return str(err)[:500]
        failed = payload.get("failed_results") or []
        if failed:
            bits=[]
            for item in failed[:3]:
                if isinstance(item, dict):
                    bits.append(str(item.get("error") or item.get("message") or "URL çıkarılamadı"))
            if bits: return "; ".join(bits)[:500]
    try:
        body = response.text[:350].replace("\\n", " ")
    except Exception:
        body = ""
    return body or f"Tavily HTTP {response.status_code}"

def _tavily_post(endpoint, payload, api_key, timeout=60):
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
    if not response.ok:
        raise RuntimeError(f"Tavily {endpoint} hatası (HTTP {response.status_code}): {_tavily_error(response, data)}")
    return data

def _tavily_extract(url, api_key):
    payload = _tavily_post("extract", {
        "urls": [url],
        "extract_depth": "advanced",
        "format": "markdown",
        "include_images": True,
    }, api_key, timeout=65)
    results = payload.get("results") or []
    if results:
        return results[0], payload
    failed = payload.get("failed_results") or []
    reason = _tavily_error(type("Response", (), {"status_code": 200, "text": ""})(), payload)
    raise RuntimeError(f"Extract başarısız: {reason}")

def _tavily_search_candidates(url, api_key):
    parsed = urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ")
    query = f'site:{parsed.netloc} "{slug}"'
    payload = _tavily_post("search", {
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_raw_content": "markdown",
        "include_domains": [parsed.netloc],
    }, api_key, timeout=45)
    candidates=[]
    for result in payload.get("results") or []:
        candidate = result.get("url", "") if isinstance(result, dict) else ""
        cp = urlparse(candidate)
        if cp.scheme in ("http", "https") and cp.netloc.lower() == parsed.netloc.lower():
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates, payload

def _tavily_post(endpoint, payload, api_key):
    """Call Tavily using Bearer authentication."""
    response = requests.post(
        "https://api.tavily.com/" + endpoint,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=60,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Tavily API kimlik doğrulama/izin hatası (HTTP {response.status_code}). "
            "Streamlit Secrets içindeki TAVILY_API_KEY değerini kontrol et."
        )
    if response.status_code == 429:
        raise RuntimeError("Tavily API kullanım limiti aşıldı (HTTP 429). Bir süre sonra tekrar dene.")
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"Tavily {endpoint} HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(f"Tavily {endpoint} geçerli JSON döndürmedi.")


def tavily_extract_product(url, api_key):
    if not api_key:
        raise RuntimeError("Tavily API anahtarı tanımlı değil. Streamlit Secrets içinde TAVILY_API_KEY ayarını kontrol et.")

    extract_error = ""
    try:
        payload = _tavily_post(
            "extract",
            {"urls": [url], "extract_depth": "advanced", "format": "markdown", "include_images": True},
            api_key,
        )
        results = payload.get("results") or []
        if results and results[0].get("raw_content"):
            item = _parse_tavily_result(url, results[0])
            item["error"] = "Tavily Extract kaynağı; alanlar doğrulanmalı."
            return item
        failed = payload.get("failed_results") or []
        extract_error = str(failed[0].get("error") or failed[0])[:400] if failed else "Extract başarılı içerik döndürmedi."
    except Exception as exc:
        extract_error = str(exc)[:400]

    parsed_url = urlparse(url)
    slug = parsed_url.path.rstrip("/").split("/")[-1]
    query = f'site:{parsed_url.netloc} "{slug}"'
    try:
        search_payload = _tavily_post(
            "search",
            {
                "query": query,
                "topic": "general",
                "search_depth": "advanced",
                "max_results": 5,
                "include_domains": ["pluspowersupplements.com"],
                "include_raw_content": "advanced",
                "include_answer": False,
            },
            api_key,
        )
        candidates = search_payload.get("results") or []
        for candidate in candidates:
            candidate_url = candidate.get("url", "")
            cp = urlparse(candidate_url)
            candidate_slug = cp.path.rstrip("/").split("/")[-1]
            if cp.netloc not in ("www.pluspowersupplements.com", "pluspowersupplements.com"):
                continue
            if candidate_slug.lower() != slug.lower():
                continue
            raw = candidate.get("raw_content") or candidate.get("content") or ""
            if raw.strip():
                item = _parse_tavily_result(url, {"raw_content": raw, "images": []})
                item["error"] = "Tavily Search yedeği kullanıldı; kaynak sayfa ile karşılaştırıp doğrula."
                return item
            retry = _tavily_post(
                "extract",
                {"urls": [candidate_url], "extract_depth": "advanced", "format": "markdown", "include_images": True},
                api_key,
            )
            retry_results = retry.get("results") or []
            if retry_results and retry_results[0].get("raw_content"):
                item = _parse_tavily_result(url, retry_results[0])
                item["error"] = "Tavily Search sonrası Extract yedeği kullanıldı; alanlar doğrulanmalı."
                return item
        search_error = "Aynı ürünün PlusPower alan adındaki eşleşen arama sonucu bulunamadı."
    except Exception as exc:
        search_error = str(exc)[:400]

    raise RuntimeError(
        "Tavily ürün sayfasını alamadı. "
        f"Extract hatası: {extract_error} | Search yedeği: {search_error}"
    )

def scrape_urls(urls, delay_seconds=5, callback=None, tavily_api_key=None):
    results=[]; session=requests.Session()
    for i,url in enumerate(urls,1):
        try:
            p=urlparse(url)
            if p.scheme not in ("http","https") or p.netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"):
                raise ValueError("Yalnızca PlusPower ürün URL'leri kabul edilir.")
            resp=session.get(url,headers=HEADERS,timeout=30)
            if resp.status_code in (401,403,429):
                if resp.status_code == 403 and tavily_api_key:
                    item = tavily_extract_product(url, tavily_api_key)
                else:
                    raise RuntimeError(f"Sunucu erişimi reddetti (HTTP {resp.status_code}). Tavily yedeği yok veya kullanılamıyor; ürün kaydedilmedi.")
            else:
                resp.raise_for_status()
                item=extract_product(url,resp.text)
            # Do not save empty/error pages as products.
            if not item["title"] and not item["brand"] and not item["price"]:
                raise RuntimeError("Sayfa alındı ancak ürün alanları tanımlanamadı; kayıt oluşturulmadı.")
            save_product(item)
            result={"url":url,"title":item["title"],"brand":item["brand"],"status":item["status"],"price":item["price"],"error":item["error"]}
        except Exception as e:
            result={"url":url,"title":"","brand":"","status":"blocked_or_failed","price":"","error":str(e)}
        results.append(result)
        if callback: callback(i,len(urls),url,result["status"])
        if i<len(urls): time.sleep(max(5,int(delay_seconds)))
        if "HTTP 429" in result.get("error","") or "HTTP 403" in result.get("error",""): break
    return results

def list_products(search=""):
    con=db()
    rows=con.execute("SELECT * FROM products WHERE title LIKE ? OR brand LIKE ? ORDER BY last_checked DESC",(f"%{search}%",f"%{search}%")).fetchall() if search else con.execute("SELECT * FROM products ORDER BY last_checked DESC").fetchall()
    con.close(); return [dict(x) for x in rows]

def count_products():
    con=db(); n=con.execute("SELECT COUNT(*) FROM products").fetchone()[0]; con.close(); return n

def get_product(url):
    con=db(); row=con.execute("SELECT * FROM products WHERE source_url=?",(url,)).fetchone(); con.close(); return dict(row) if row else None

def export_csv():
    rows=list_products(); output=io.StringIO()
    if rows:
        writer=csv.DictWriter(output,fieldnames=list(rows[0].keys())); writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")

def delete_all_products():
    con=db(); con.execute("DELETE FROM products"); con.commit(); con.close()
