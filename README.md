# PlusPower Product Scraper — MVP

Yerel bilgisayarda çalışan ilk prototip. Sitemap'ten ürün URL'lerini keşfeder, ürün sayfalarındaki yapılandırılmış verileri (JSON-LD), Open Graph/meta bilgilerini ve temel sayfa metnini çıkarır; SQLite'a kaydeder ve CSV dışa aktarır.

## Gereksinimler
- Python 3.10+
- İnternet bağlantısı
- Siteye erişim ve robots.txt kurallarına uygun kullanım

## Kurulum
### Windows
1. ZIP dosyasını çıkar.
2. Klasörü aç.
3. `run_windows.bat` dosyasına çift tıkla.
4. Tarayıcıda açılan Streamlit adresini kullan (genellikle http://localhost:8501).

### Linux / macOS
```bash
chmod +x run_linux_mac.sh
./run_linux_mac.sh
```

## Pilot
1. Sol panelde sitemap URL'sini kontrol et.
2. `Sitemap'i oku` düğmesine bas.
3. Önce 10 ürünlük pilotu çalıştır.
4. Sonuçlarda `ok`, `partial` ve `error` durumlarını kontrol et.
5. CSV sekmesinden verileri indir.

## Önemli sınırlar
- Bu MVP önce JSON-LD ve meta etiketlerini okur. Ürüne özgü besin tablosu, içerik listesi, varyant ve stok alanları her sayfada standart biçimde sunulmayabilir; henüz garanti edilmez.
- Bazı sayfalar JavaScript ile yükleniyorsa sonraki sürümde Playwright tabanlı tarayıcı eklenecek.
- Sitemap'teki kategori/marka/blog sayfalarını ürün URL'lerinden ayırmak için muhafazakâr URL filtresi kullanılır. İlk testte URL örneklemi kontrol edilmelidir.
- Robots.txt ve sitenin kullanım koşulları kontrol edilmeli; varsayılan istek aralığı 5 saniyedir. 403/429 yanıtlarında tarama durur.
- Ürün fotoğraflarını veya açıklamalarını başka pazaryerlerinde yeniden kullanmadan önce kullanım izni alınmalıdır.
- eBay ilanlarını otomatik yayımlamaz; ilk sürüm yalnızca veri toplar ve CSV üretir.
