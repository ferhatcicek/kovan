# Değişiklik günlüğü

Biçim [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/), sürümleme [SemVer](https://semver.org/lang/tr/) esaslıdır.

## [Yayınlanmadı]
### Değişti
- Proje adı **PyDeck → Kovan** (PyPI'daki başka bir paketle ad çakışması nedeniyle). Yeni logo ve marka.
- Komut `pydeck` → `kovan`, ortam değişkenleri `PYDECK_*` → `KOVAN_*`.

### Uyumluluk
- Önceki sürümün verisi kaybolmaz: `%LOCALAPPDATA%\PyDeck` (Linux: `~/.local/share/pydeck`) klasörü ilk açılışta
  yeni konuma kopyalanır; eski kopya yerinde bırakılır. `pydeck.db`, `pydeck.portable`, `PYDECK_DATA_DIR` ve
  `pydeck-config` biçimli dışa aktarma dosyaları tanınmaya devam eder.

## [0.2.0]
### Eklendi
- **Süreç gözetimi:** çıkış nedeni sınıflandırması (kullanıcı durdurdu / dışarıdan kapatıldı / öldürüldü / çöktü /
  tamamlandı), süreç ağacı takibi, işçi kaybı ve yetim alt süreç tespiti, HTTP/TCP sağlık kontrolü.
- Otomatik yeniden başlatma (üstel bekleme, çökme döngüsü koruması); proje, kategori ve genel politika.
- Panel yeniden açıldığında çalışan süreçleri devralma; panel dışında başlatılmış süreçlerin tespiti.
- Bildirim merkezi, masaüstü bildirimleri, olay zaman çizelgesi.
- Şema sürümleme ve atomik migrasyonlar (öncesinde otomatik yedek), sürüm düşürme koruması, karantina.
- Günlük ve işlem öncesi yedekler, geri yükleme, okunabilir JSON aynası, taşınabilir mod.
- Kök eşlemeli dışa/içe aktarma, şifreli gizli değerler (scrypt + AES-256-GCM).
- Komutlar: `doctor`, `backup`, `restore`, `export`, `import`.

### Güvenlik
- İçe aktarmada tam komut önizlemesi, UNC/kök dışı yolların reddi, çalışan projelerin güncellenmemesi,
  geri yükleme ve içe aktarmada parola ile yeniden doğrulama.

## [0.1.0]
### Eklendi
- İlk sürüm: script / module / uvicorn / flask / django / streamlit / özel komut ile başlatma, canlı loglar,
  traceback gruplama, port tespiti ve çakışma önleme, isteğe bağlı CPU/RAM izleme, kategoriler ve etiketler,
  güvenli doküman görüntüleyici, klasör keşfi.
