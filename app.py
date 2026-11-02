import streamlit as st
import pandas as pd
import json
from scraper.core import (
    init_db, list_products, count_products, import_sitemap,
    scrape_urls, export_csv, get_product, delete_all_products,
    parse_pasted_product, extract_product, save_product
)

st.set_page_config(page_title="PlusPower Product Scraper", page_icon="🛒", layout="wide")
init_db()
try:
    tavily_api_key = st.secrets.get("TAVILY_API_KEY", "")
except Exception:
    tavily_api_key = ""

st.markdown("""
<style>
.block-container {padding-top: 1.5rem; max-width: 1450px;}
.hero {padding: 1.4rem 1.6rem; border-radius: 16px; background: linear-gradient(120deg,#152238,#27466a); color:white; margin-bottom:1rem;}
.hero h1 {margin:0; color:white; font-size:2rem;}
.hero p {margin:.35rem 0 0 0; color:#dce8f5;}
div[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.25); padding:12px; border-radius:12px;}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="hero"><h1>PlusPower Product Scraper</h1><p>Product discovery · extraction · local database · CSV export</p></div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("Tarama ayarları")
    sitemap_url = st.text_input("Sitemap URL", "https://www.pluspowersupplements.com/sitemap.xml")
    delay = st.number_input("İstekler arası bekleme (saniye)", min_value=5, max_value=60, value=5, step=1)
    limit = st.number_input("Pilot ürün sayısı", min_value=1, max_value=100, value=10, step=1)
    st.caption("Cloudflare erişimi engellerse sayfanın View Source HTML kodunu HTML aktarım bölümünden yapıştırabilirsin.")

products_count = count_products()
c1,c2,c3 = st.columns(3)
c1.metric("Kayıtlı ürün", products_count)
c2.metric("Pilot hedefi", int(limit))
c3.metric("Veritabanı", "SQLite")

tab1, tab2, tab3, tab4 = st.tabs(["URL ile ürün ekle", "HTML / Metin aktarımı", "Ürünleri görüntüle", "Dışa aktar"])

with tab1:
    st.subheader("1. Sitemap'ten ürün URL'lerini keşfet")
    st.write("Sitemap'teki URL'ler alınır; ürün sayfası gibi görünen bağlantılar tarama kuyruğuna eklenir.")
    if st.button("Sitemap'i oku", type="primary"):
        with st.spinner("Sitemap okunuyor..."):
            try:
                urls = import_sitemap(sitemap_url)
                st.session_state["discovered_urls"] = urls
                st.success(f"{len(urls)} olası ürün URL'si bulundu.")
            except Exception as e:
                st.error(f"Sitemap okunamadı: {e}")

    urls = st.session_state.get("discovered_urls", [])
    if urls:
        st.caption(f"Keşfedilen URL: {len(urls)}")
        st.dataframe(pd.DataFrame({"Ürün URL'si": urls[:100]}), use_container_width=True, hide_index=True)
        if st.button(f"İlk {int(limit)} ürünü tara", type="primary"):
            progress = st.progress(0)
            status = st.empty()
            def update(i, total, url, result):
                progress.progress(min(i/total,1.0))
                status.write(f"{i}/{total} — {url} — {result}")
            try:
                results = scrape_urls(urls[:int(limit)], delay_seconds=int(delay), callback=update, tavily_api_key=tavily_api_key)
                ready = sum(1 for x in results if x.get("status") == "ready_for_review")
                review = sum(1 for x in results if x.get("status") == "needs_review")
                st.info(f"İşlem tamamlandı: {ready} incelenmeye hazır, {review} eksik bilgi içeriyor. Engellenen kayıtlar kaydedilmedi.")
                st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
            except Exception as e:
                st.error(f"Tarama sırasında hata: {e}")

    st.divider()
    st.subheader("2. Ürün URL'si ile otomatik aktarım")
    st.caption("Uygulama doğrudan HTML'yi okur; erişim engellenirse Tavily yedeğini deneyebilir.")
    manual = st.text_area("Ürün URL'lerini gir (her satıra bir URL)", placeholder="https://www.pluspowersupplements.com/100-hydro-isolate-2kg")
    if st.button("Manuel linkleri tara"):
        manual_urls = [x.strip() for x in manual.splitlines() if x.strip()]
        if not manual_urls:
            st.warning("En az bir ürün URL'si gir.")
        else:
            with st.spinner("Ürünler taranıyor..."):
                results = scrape_urls(manual_urls, delay_seconds=int(delay), tavily_api_key=tavily_api_key)
            st.info(f"İşlem tamamlandı: {sum(x.get('status')=='ready_for_review' for x in results)} incelenmeye hazır; {sum(x.get('status')=='blocked_or_failed' for x in results)} başarısız/engelli.")
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("View Source HTML ile ürün aktarımı")
    st.write("PlusPower ürün sayfasını Safari'de aç, View Source içeriğinin tamamını kopyala ve aşağıya yapıştır. HTML içindeki ürün şeması, varyantlar, EAN, stok ve görseller ayrıştırılır.")
    html_url = st.text_input("Ürün sayfasının URL'si", placeholder="https://www.pluspowersupplements.com/100-hydro-isolate-2kg", key="html_product_url")
    html_source = st.text_area("View Source HTML kodu", height=360, placeholder="<!doctype html> ...", key="html_source_input")
    if st.button("HTML kodunu analiz et", type="primary"):
        if not html_url.strip():
            st.warning("Ürün URL'sini gir.")
        elif not html_source.strip():
            st.warning("View Source HTML kodunu yapıştır.")
        else:
            try:
                parsed = extract_product(html_url.strip(), html_source)
                parsed["error"] = "Kaynak: Kullanıcının yapıştırdığı View Source HTML. Kaydetmeden önce alanları doğrula."
                parsed["status"] = "needs_review"
                st.session_state["html_product"] = parsed
                st.success("HTML analiz edildi. Bulunan alanları kontrol et.")
            except Exception as e:
                st.error(f"HTML analiz edilemedi: {e}")

    parsed = st.session_state.get("html_product")
    if parsed:
        st.markdown("#### Bulunan ürün bilgileri")
        col1, col2 = st.columns(2)
        with col1:
            edit_title = st.text_input("Ürün adı", value=parsed.get("title", ""), key="html_edit_title")
            edit_brand = st.text_input("Marka", value=parsed.get("brand", ""), key="html_edit_brand")
            edit_price = st.text_input("Fiyat (€)", value=parsed.get("price", ""), key="html_edit_price")
            edit_currency = st.text_input("Para birimi", value=parsed.get("currency", "EUR"), key="html_edit_currency")
            edit_ean = st.text_input("Genel EAN (varyant EAN'leri aşağıda)", value=parsed.get("ean", ""), key="html_edit_ean")
            edit_sku = st.text_input("Genel SKU", value=parsed.get("sku", ""), key="html_edit_sku")
        with col2:
            edit_url = st.text_input("Kaynak URL", value=parsed.get("source_url", ""), key="html_edit_url")
            edit_availability = st.text_input("Genel stok durumu", value=parsed.get("availability", ""), key="html_edit_availability")
            st.caption("Varyant EAN ve stokları ayrı JSON alanında saklanır.")
        edit_description = st.text_area("Açıklama", value=parsed.get("description", ""), height=130, key="html_edit_description")
        edit_ingredients = st.text_area("İçindekiler", value=parsed.get("ingredients", ""), height=130, key="html_edit_ingredients")
        edit_nutrition = st.text_area("Besin değerleri", value=parsed.get("nutrition", ""), height=180, key="html_edit_nutrition")
        edit_images = st.text_area("Görsel URL'leri (JSON)", value=parsed.get("image_urls", "[]"), height=100, key="html_edit_images")
        edit_variants = st.text_area("Aroma / varyant bilgileri (JSON)", value=parsed.get("variants", "[]"), height=180, key="html_edit_variants")
        if st.button("HTML ürününü veritabanına kaydet", type="primary"):
            try:
                json.loads(edit_images or "[]")
                json.loads(edit_variants or "[]")
                parsed.update({
                    "title": edit_title, "brand": edit_brand, "price": edit_price,
                    "currency": edit_currency, "ean": edit_ean, "sku": edit_sku,
                    "source_url": edit_url, "availability": edit_availability,
                    "description": edit_description, "ingredients": edit_ingredients,
                    "nutrition": edit_nutrition, "image_urls": edit_images,
                    "variants": edit_variants, "status": "needs_review",
                    "error": "Kaynak: Kullanıcının yapıştırdığı View Source HTML. Manuel doğrulama gerekli."
                })
                if not edit_url.strip():
                    st.error("Kaydetmek için ürün URL'si gerekli.")
                else:
                    save_product(parsed)
                    st.success("Ürün HTML verileriyle kaydedildi. Ürünleri görüntüle bölümünde bulabilirsin.")
                    st.session_state.pop("html_product", None)
            except json.JSONDecodeError:
                st.error("Görsel veya varyant JSON alanlarından biri geçersiz. Köşeli parantezleri ve virgülleri kontrol et.")

    st.divider()
    st.subheader("Düz metin aktarımı (eski yöntem)")
    pasted_url = st.text_input("Ürün URL'si", placeholder="https://www.pluspowersupplements.com/product-url", key="plain_product_url")
    pasted_text = st.text_area("Kopyalanan ürün metni", height=150, placeholder="Ürün sayfasındaki metni buraya yapıştır...", key="plain_product_text")
    if st.button("Düz metni analiz et"):
        if not pasted_text.strip():
            st.warning("Önce ürün metnini yapıştır.")
        else:
            st.session_state["pasted_product"] = parse_pasted_product(pasted_text, pasted_url)
            st.warning("Metin analiz edildi; alanları kaydetmeden önce kontrol et.")
    plain = st.session_state.get("pasted_product")
    if plain:
        st.json(plain)
        if st.button("Düz metin ürününü kaydet"):
            if plain.get("source_url"):
                save_product(plain)
                st.success("Ürün kaydedildi.")
                st.session_state.pop("pasted_product", None)
            else:
                st.error("Kaydetmek için ürün URL'si gerekli.")

with tab3:
    st.subheader("Kayıtlı ürünler")
    q = st.text_input("Ürün adı veya marka ara")
    rows = list_products(search=q)
    if rows:
        df = pd.DataFrame(rows)
        display_cols = [c for c in ["title","brand","price","currency","ean","availability","source_url","last_checked"] if c in df.columns]
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)
        selected = st.selectbox("Detayını görüntüle", options=df["source_url"].tolist(), format_func=lambda x: df.loc[df.source_url==x, "title"].iloc[0] or x)
        detail = get_product(selected)
        if detail:
            st.markdown("#### Ürün detayı")
            st.json(detail)
    else:
        st.info("Henüz ürün kaydı yok. Önce sitemap'i okuyup pilot tarama yap.")

with tab4:
    st.subheader("CSV dışa aktarımı")
    st.write("Excel ile açılabilen UTF-8 CSV dosyası oluşturur.")
    csv_bytes = export_csv()
    st.download_button("CSV indir", data=csv_bytes, file_name="pluspower_products.csv", mime="text/csv", disabled=(products_count == 0))
    st.caption("İlk sürüm CSV dışa aktarır. XLSX ve eBay ilan üretimi sonraki geliştirme aşamasında.")
