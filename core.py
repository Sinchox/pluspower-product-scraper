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

def scrape_urls(urls, delay_seconds=5, callback=None):
    results=[]; session=requests.Session()
    for i,url in enumerate(urls,1):
        try:
            p=urlparse(url)
            if p.scheme not in ("http","https") or p.netloc not in ("www.pluspowersupplements.com","pluspowersupplements.com"):
                raise ValueError("Yalnızca PlusPower ürün URL'leri kabul edilir.")
            resp=session.get(url,headers=HEADERS,timeout=30)
            if resp.status_code in (401,403,429):
                raise RuntimeError(f"Sunucu erişimi reddetti (HTTP {resp.status_code}). Bu kısıtlama aşılmadı; ürün kaydedilmedi.")
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
