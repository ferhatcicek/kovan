# Katkı rehberi

Katkılarınız memnuniyetle karşılanır! Lütfen büyük değişikliklerden önce bir issue açarak konuyu tartışın.

## Geliştirme ortamı

```bash
git clone https://github.com/ferhatcicek/kovan.git
cd kovan
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
kovan --root /yol/kod --data-dir ./data   # yerel deneme (data/ git'e eklenmez)
```

## Kontroller

```bash
ruff check src tests
pytest
```

Entegrasyon testleri gerçek süreçler başlatır (`tests/fixtures/code` altındaki örnek projeler). POSIX sinyali
kullanan gözetim testleri Windows'ta otomatik atlanır.

## Mimari

| Katman | Klasör | Kural |
|---|---|---|
| Domain | `src/kovan/domain/` | Saf Python; I/O yok, kolay test edilir |
| Application | `src/kovan/application/` | İş kuralları ve servisler |
| Infrastructure | `src/kovan/infrastructure/` | Süreç, psutil, SQLite, dosya |
| Web | `src/kovan/web/` | FastAPI, güvenlik ara katmanı, arayüz |

Ayrıntılı tasarım: [docs/TASARIM.md](docs/TASARIM.md).

## Kurallar

- Komutlar **her zaman** argv listesiyle çalıştırılır; `shell=True` kullanılmaz.
- Kullanıcıdan veya dosyadan gelen her yol `domain/paths.py` ile izinli köklere karşı doğrulanır.
- Arayüzde HTML üretirken yalnızca `h()` yardımcısı (textContent) kullanılır; `innerHTML` yalnızca sunucuda
  temizlenmiş doküman içeriği içindir. Satır içi script/stil yazılmaz (CSP).
- Veritabanı şeması değişirse `infrastructure/migrations.py`'ye yeni bir migrasyon ve test ekleyin.
- Her davranış değişikliği için test ekleyin; `CHANGELOG.md`'yi güncelleyin.

## Commit mesajları

[Conventional Commits](https://www.conventionalcommits.org/tr/) tercih edilir: `feat: …`, `fix: …`, `docs: …`.
