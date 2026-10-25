# Güvenlik politikası

Kovan bilgisayarınızda komut çalıştırabilen bir araçtır; bu yüzden güvenlik bildirimlerini öncelikli ele alırız.

## Açık bildirme

Lütfen güvenlik açıklarını **herkese açık issue olarak açmayın**. GitHub üzerinden
[özel güvenlik bildirimi](https://github.com/ferhatcicek/kovan/security/advisories/new) gönderin.
Bildiriminizde etkilenen sürümü, tekrarlama adımlarını ve olası etkiyi belirtin.

## Desteklenen sürümler

| Sürüm | Destek |
|---|---|
| En son sürüm | ✅ |
| Daha eski sürümler | ❌ |

## Tasarım gereği güvenlik önlemleri

- Panel yalnızca `127.0.0.1` üzerinde dinler; Host başlığı doğrulanır (DNS rebinding koruması).
- Parola zorunludur (argon2id); oturum çerezi `HttpOnly` + `SameSite=Strict`; giriş hız sınırı.
- Değiştirici isteklerde CSRF belirteci ve Origin denetimi; katı Content-Security-Policy.
- Komutlar argv listesiyle, kabuk olmadan çalıştırılır; dosya erişimi izinli kök klasörlerle sınırlıdır.
- İçe aktarılan yapılandırma dosyaları güvenilmeyen girdi sayılır: tam komut önizlemesi, otomatik başlatma yok,
  çalışan projeler güncellenmez, parola ile yeniden doğrulama gerekir.
- Gizli ortam değişkenleri arayüzde maskelenir, loglarda `***` ile değiştirilir; dışa aktarımda yalnızca parola
  ile şifrelenmiş olarak yer alabilir.
