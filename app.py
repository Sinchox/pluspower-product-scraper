import streamlit as st
import pandas as pd
from scraper.core import (
    init_db, list_products, count_products, import_sitemap,
    scrape_urls, export_csv, get_product, delete_all_products, parse_pasted_product, save_product
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
    st.caption("Varsayılan bekleme 5 saniyedir. 403 durumunda yapılandırılmış Tavily Extract yedeği denenir; erişim kısıtlaması aşılmaz.")

products_count = count_products()
c1,c2,c3 = st.columns(3)
c1.metric("Kayıtlı ürün", products_count)
c2.metric("Pilot hedefi", int(limit))
c3.metric("Veritabanı", "SQLite")

tab1, tab2, tab3, tab4 = st.tabs(["URL ile ürün ekle", "Metin aktarımı", "Ürünleri görüntüle", "Dışa aktar"])

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
    st.caption("Tavily anahtarı bağlıysa doğrudan erişim 403 verdiğinde Extract API yedek kaynak olarak denenir.")
    manual = st.text_area("Ürün URL'lerini gir (her satıra bir URL)", placeholder="https://www.pluspowersupplements.com/100-whey-isolate-1816gr")
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
    st.subheader("İsteğe bağlı metin aktarımı")
    st.write("Bu bölüm yalnızca URL ile erişim mümkün olmadığında yedek yöntemdir. Ana yöntem ürün URL’sini otomatik analiz etmektir.")
    pasted_url = st.text_input("Ürün sayfasının URL'si", placeholder="https://www.pluspowersupplements.com/100-whey-isolate-1816gr")
    pasted_text = st.text_area("Kopyalanan ürün metni", height=320, placeholder="Ürün sayfasındaki metni buraya yapıştır...")
    if st.button("Metni analiz et", type="primary"):
        if not pasted_text.strip():
            st.warning("Önce ürün sayfasından kopyaladığın metni yapıştır.")
        else:
            parsed = parse_pasted_product(pasted_text, pasted_url)
            st.session_state["pasted_product"] = parsed
            st.warning("Metin analiz edildi; otomatik doğrulanmış sayılmaz. Kaydetmeden önce alanları kontrol et.")
    parsed = st.session_state.get("pasted_product")
    if parsed:
        st.markdown("#### Bulunan ürün bilgileri")
        col1, col2 = st.columns(2)
        with col1:
            edit_title = st.text_input("Ürün adı", value=parsed.get("title", ""))
            edit_brand = st.text_input("Marka", value=parsed.get("brand", ""))
            edit_price = st.text_input("Fiyat (€)", value=parsed.get("price", ""))
            edit_ean = st.text_input("EAN", value=parsed.get("ean", ""))
            edit_sku = st.text_input("Ürün kodu / SKU", value=parsed.get("sku", ""))
        with col2:
            edit_url = st.text_input("Kaynak URL", value=parsed.get("source_url", ""))
            st.caption("Stok, görseller ve varyantlar kopyalanan metinde yoksa boş kalır.")
        edit_description = st.text_area("Açıklama", value=parsed.get("description", ""), height=130)
        edit_ingredients = st.text_area("İçindekiler", value=parsed.get("ingredients", ""), height=130)
        edit_nutrition = st.text_area("Besin değerleri", value=parsed.get("nutrition", ""), height=180)
        if st.button("Ürünü veritabanına kaydet"):
            parsed.update({"title": edit_title, "brand": edit_brand, "price": edit_price,
                           "ean": edit_ean, "sku": edit_sku, "source_url": edit_url,
                           "description": edit_description, "ingredients": edit_ingredients,
                           "nutrition": edit_nutrition, "status": "needs_review"})
            if not edit_url.strip():
                st.error("Kaydetmek için ürün URL'si gerekli.")
            else:
                save_product(parsed)
                st.success("Ürün kaydedildi. Ürünleri görüntüle bölümünde bulabilirsin.")
                st.session_state.pop("pasted_product", None)

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
