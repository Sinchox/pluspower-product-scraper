import json, re, sqlite3, time, csv, io, os
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

DB_PATH = "data/products.sqlite3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ProductResearchBot/1.0; +local-user-tool)"}

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS products (
      source_url TEXT PRIMARY KEY, title TEXT, brand TEXT, price TEXT, currency TEXT,
      ean TEXT, sku TEXT, availability TEXT, description TEXT, image_urls TEXT,
      ingredients TEXT, nutrition TEXT, variants TEXT, raw_jsonld TEXT,
      last_checked TEXT, status TEXT, error TEXT
    )""")
    con.commit(); con.close()

def _local(tag):
    return tag.split("}")[-1].lower()

def _flatten_sitemap(root, base_url, session, depth=0):
    if depth > 4:
        return []
    urls=[]
    for child in root:
        tag=_local(child.tag)
        if tag == "loc" and child.text:
            urls.append(child.text.strip())
    if urls:
        return urls
    # sitemap index: recursively visit child sitemap files
    for child in root:
        if _local(child.tag) == "sitemap":
            loc = next((x.text.strip() for x in child if _local(x.tag)=="loc" and x.text), None)
            if loc:
                r=session.get(loc, timeout=25, headers=HEADERS)
                r.raise_for_status()
                nested=ET.fromstring(r.content)
                urls.extend(_flatten_sitemap(nested, loc, session, depth+1))
    return urls

def import_sitemap(sitemap_url):
    p=urlparse(sitemap_url)
    if p.scheme not in ("http","https") or p.netloc != "www.pluspowersupplements.com":
        raise ValueError("Bu MVP sürümünde yalnızca www.pluspowersupplements.com sitemap adresi destekleniyor.")
    session=requests.Session()
    r=session.get(sitemap_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root=ET.fromstring(r.content)
    all_urls=_flatten_sitemap(root, sitemap_url, session)
    # Exclude obvious non-product sections. Product pages in this site's sitemap are root-level slugs;
    # category and brand URLs are handled by this conservative heuristic.
    excluded = ("/blog", "/nutrizione-sportiva", "/salute-e-benessere", "/controllo-del-peso",
                "/alimenti-fit", "/abbigliamento", "/marche", "/promo", "/last-minute",
                "/chi-siamo", "/contatti", "/privacy", "/cookie", "/registrati",
                "/condizioni-di-vendita", "/resi-e-rimborsi", "/rivenditore")
    product_urls=[]
    for u in all_urls:
        if urlparse(u).netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"):
            continue
        path=urlparse(u).path.strip("/")
        if not path or any(path.startswith(x.strip("/")) for x in excluded):
            continue
        # Root-level product slugs are the product candidates in the supplied sitemap.
        if "/" not in path:
            product_urls.append(u)
    return list(dict.fromkeys(product_urls))

def _walk_jsonld(obj, out):
    if isinstance(obj, dict):
        typ=obj.get("@type", [])
        if isinstance(typ, str): typ=[typ]
        if any(t.lower()=="product" for t in typ):
            out.append(obj)
        for v in obj.values(): _walk_jsonld(v,out)
    elif isinstance(obj,list):
        for v in obj: _walk_jsonld(v,out)

def _meta(soup, *names):
    for name in names:
        tag=soup.find("meta", attrs={"property":name}) or soup.find("meta", attrs={"name":name})
        if tag and tag.get("content"): return tag["content"].strip()
    return ""

def _text(soup, selectors):
    for sel in selectors:
        node=soup.select_one(sel)
        if node:
            value=" ".join(node.get_text(" ",strip=True).split())
            if value: return value
    return ""

def extract_product(url, html):
    soup=BeautifulSoup(html,"html.parser")
    json_products=[]
    for script in soup.find_all("script",type="application/ld+json"):
        try: _walk_jsonld(json.loads(script.string or script.get_text()),json_products)
        except Exception: pass
    product=json_products[0] if json_products else {}
    offers=product.get("offers",{}) if isinstance(product,dict) else {}
    if isinstance(offers,list): offers=offers[0] if offers else {}
    brand=product.get("brand","") if isinstance(product,dict) else ""
    if isinstance(brand,dict): brand=brand.get("name","")
    image=product.get("image",[]) if isinstance(product,dict) else []
    if isinstance(image,str): image=[image]
    if not image:
        ogimg=_meta(soup,"og:image","twitter:image")
        image=[ogimg] if ogimg else []
    title=(product.get("name") if isinstance(product,dict) else "") or _meta(soup,"og:title","twitter:title") or _text(soup,["h1","h1.product-title"])
    description=(product.get("description") if isinstance(product,dict) else "") or _meta(soup,"og:description","description") or _text(soup,[".product-description",".description"])
    price=offers.get("price","") if isinstance(offers,dict) else ""
    currency=offers.get("priceCurrency","") if isinstance(offers,dict) else ""
    availability=offers.get("availability","") if isinstance(offers,dict) else ""
    sku=(product.get("sku","") if isinstance(product,dict) else "") or _meta(soup,"product:retailer_item_id")
    ean=""
    if isinstance(product,dict):
        ean=product.get("gtin13") or product.get("gtin") or product.get("gtin12") or product.get("gtin8") or ""
    if not price:
        price=_meta(soup,"product:price:amount")
    if not currency:
        currency=_meta(soup,"product:price:currency") or "EUR"
    # Capture visible product details conservatively; never invent nutrition or ingredients.
    body=soup.select_one(".product-description, .product-detail, .tab-content, main")
    details=" ".join(body.get_text(" ",strip=True).split())[:12000] if body else ""
    return {
        "source_url":url, "title":title, "brand":str(brand), "price":str(price),
        "currency":str(currency), "ean":str(ean), "sku":str(sku),
        "availability":str(availability), "description":description,
        "image_urls":json.dumps(image,ensure_ascii=False),
        "ingredients":"", "nutrition":"", "variants":"", "raw_jsonld":json.dumps(product,ensure_ascii=False),
        "last_checked":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status":"ok" if title else "partial", "error":""
    }


def _section(text, start_patterns, end_patterns):
    """Extract a labelled section from pasted page text, conservatively."""
    lines = [x.strip() for x in text.replace("\r", "").splitlines()]
    start = None
    for i, line in enumerate(lines):
        if any(re.search(p, line, re.I) for p in start_patterns):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start, len(lines)):
        if any(re.search(p, lines[i], re.I) for p in end_patterns):
            end = i
            break
    return "\n".join(x for x in lines[start:end] if x).strip()


def parse_pasted_product(text, source_url=""):
    """Parse text copied by the user from a supplier product page."""
    raw = text.strip()
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    title = ""
    for line in lines[:12]:
        if re.search(r'\b\d{2,4}\s*(?:g|gr|kg|ml)\b', line, re.I) and len(line) < 150:
            title = line.lstrip("#•*- ")
            break
    if not title and lines:
        title = lines[0]

    price = ""
    # First Euro amount in the copied product heading/price block.
    for line in lines[:35]:
        m = re.search(r'€\s*([0-9][0-9.,]*)', line)
        if m:
            price = m.group(1).replace(".", "").replace(",", ".")
            break

    ean = ""
    m = re.search(r'\bEAN\s*[:：]?\s*(\d{8,14})', raw, re.I)
    if m: ean = m.group(1)
    sku = ""
    m = re.search(r'\b(?:Codice|SKU|Cod\.)\s*[:：]?\s*([A-Za-z0-9_-]+)', raw, re.I)
    if m: sku = m.group(1)

    brand = ""
    brand_candidates = ["Scitec Nutrition", "OstroVit", "BioTechUSA", "Biotech USA", "Optimum Nutrition", "4+ Nutrition", "Nutrend", "Enervit", "Why Sport", "Yamamoto Nutrition"]
    for candidate in brand_candidates:
        if re.search(re.escape(candidate), raw, re.I):
            brand = candidate
            break

    nutrition = _section(raw, [r'^VALORI NUTRIZIONALI$'], [r'^INGREDIENTI(?:\s|$)', r'^MODALIT[ÀA] D[’\']USO', r'^AVVERTENZE$'])
    ingredients = _section(raw, [r'^INGREDIENTI$'], [r'^MODALIT[ÀA] D[’\']USO', r'^AVVERTENZE$', r'^CARATTERISTICHE$'])
    description = _section(raw, [r'^Descrizione$'], [r'^Scheda Tecnica$', r'^Caratteristiche$'])
    if not description:
        # Use the first descriptive paragraph after the product highlights, excluding controls/prices.
        candidates=[]
        for line in lines:
            if len(line) > 100 and not re.search(r'VALORI NUTRIZIONALI|INGREDIENTI|MODALIT', line, re.I):
                candidates.append(line)
            if len(" ".join(candidates)) > 1800: break
        description = "\n".join(candidates[:8])[:4000]

    variants = []
    for label in ["Banana", "Cioccolato e Nocciola", "Cioccolato", "Cookies & Cream", "Pistacchio", "Fragola e Cioccolato Bianco", "Caramello Salato", "Fragola", "Vaniglia", "Vaniglia e Frutti di Bosco"]:
        if re.search(r'(?m)^' + re.escape(label) + r'\s*:', raw, re.I):
            variants.append(label)

    return {
        "source_url": source_url.strip(), "title": title, "brand": brand,
        "price": price, "currency": "EUR" if price else "", "ean": ean,
        "sku": sku, "availability": "", "description": description,
        "image_urls": "[]", "ingredients": ingredients, "nutrition": nutrition,
        "variants": json.dumps(variants, ensure_ascii=False), "raw_jsonld": "{}",
        "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok" if title else "partial", "error": ""
    }


def save_product(item):
    con=db()
    cols=list(item.keys())
    placeholders=",".join(["?"]*len(cols))
    updates=",".join([f"{c}=excluded.{c}" for c in cols if c!="source_url"])
    con.execute(f"INSERT INTO products ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(source_url) DO UPDATE SET {updates}",
                [item[c] for c in cols])
    con.commit(); con.close()

def scrape_urls(urls, delay_seconds=5, callback=None):
    results=[]
    session=requests.Session()
    for i,url in enumerate(urls,1):
        try:
            p=urlparse(url)
            if p.scheme not in ("http","https") or p.netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"):
                raise ValueError("Yalnızca PlusPower ürün URL'leri kabul edilir.")
            resp=session.get(url,headers=HEADERS,timeout=30)
            if resp.status_code in (401,403,429):
                raise RuntimeError(f"Sunucu taramayı reddetti (HTTP {resp.status_code}); durduruldu.")
            resp.raise_for_status()
            item=extract_product(url,resp.text)
            save_product(item)
            result={"url":url,"title":item["title"],"status":item["status"],"price":item["price"],"error":""}
        except Exception as e:
            result={"url":url,"title":"","status":"error","price":"","error":str(e)}
        results.append(result)
        if callback: callback(i,len(urls),url,result["status"])
        if i < len(urls): time.sleep(max(5,int(delay_seconds)))
        if "HTTP 429" in result.get("error","") or "HTTP 403" in result.get("error",""):
            break
    return results

def list_products(search=""):
    con=db()
    if search:
        rows=con.execute("SELECT * FROM products WHERE title LIKE ? OR brand LIKE ? ORDER BY last_checked DESC",
                         (f"%{search}%",f"%{search}%")).fetchall()
    else:
        rows=con.execute("SELECT * FROM products ORDER BY last_checked DESC").fetchall()
    con.close()
    return [dict(x) for x in rows]

def count_products():
    con=db(); n=con.execute("SELECT COUNT(*) FROM products").fetchone()[0]; con.close(); return n

def get_product(url):
    con=db(); row=con.execute("SELECT * FROM products WHERE source_url=?",(url,)).fetchone(); con.close()
    return dict(row) if row else None

def export_csv():
    rows=list_products()
    output=io.StringIO()
    if rows:
        writer=csv.DictWriter(output,fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")

def delete_all_products():
    con=db(); con.execute("DELETE FROM products"); con.commit(); con.close()
