# Kovan v3 — Python Projeleri için Yerel Süreç Yönetim Paneli
### (3. tur son kullanıcı geri bildirimiyle yenilenmiş tasarım — onaya sunulan sürüm)

## Context
`D:\code` altında farklı klasörlerde, farklı biçimlerde çalışan (`python x.py`, `uvicorn main:app`, `flask run`, `manage.py runserver`, `streamlit run` …) Python projeleri var. Her birini ayrı CLI pencerelerinde başlatıp izlemek zahmetli. Hedef: tüm projeleri **tek bir görsel panelden** yönetmek; CLI'da görülen **log ve hataları** canlı incelemek; **çalışma durumunu ve portları** görmek; port çakışmasını **çalıştırma parametresiyle** önlemek; istenirse **hafif CPU/RAM izleme**; ayrıca **paket bağımlılıklarını izlemek**, **README vb. dokümanları panelde okumak** ve projeleri **kategorilere ayırıp düzenlemek**. Dil: Python. Çıktı: yerel web arayüzü + tek tıkla çalışan Windows `.exe` (tepsi uygulaması).

Çalışma dizini boş; sıfırdan proje. Kod çapraz platform (Windows birincil, Linux'ta test), OS'e özgü kısımlar adaptör katmanında.

### Sürüm geçmişi
| Sürüm | Değişiklik |
|---|---|
| v1 | İlk analiz; son kullanıcı 14 eksik buldu (E1–E14). |
| v2 | E1–E14 tasarıma işlendi; 2. turda R1–R4 eklendi. |
| **v3** | Kullanıcı istekleri: **U1 bağımlılık izleme**, **U2 README/doküman inceleme**, **U3 kategori seçimi ve düzenleme**. Ayrıca "olmazsa olmaz" eksikler analiz edilip **M1–M8** eklendi. Tüm bölümler yenilendi. |

---

## 1. Yazılım Mühendisliği — Gereksinimler

### 1.1 Fonksiyonel gereksinimler
**A. Çekirdek (talep)**
| # | Gereksinim |
|---|---|
| FR1 | Kök klasörler (varsayılan `D:\code`) taranır, projeler **kod çalıştırmadan** tespit edilir, profil önerilir; kullanıcı onaylar. Manuel ekleme. |
| FR2 | Tüm çalıştırma biçimleri (§1.3) + serbest "özel komut". |
| FR3 | Yorumlayıcı: `.venv`/`venv`/`env`, uv, Poetry, Pipenv, Conda, sistem, elle yol. |
| FR4 | Başlat / Durdur (nazik → zorla) / Yeniden başlat; tüm süreç ağacı. |
| FR5 | Canlı stdout/stderr, ANSI renk, arama/filtre, zaman damgası, traceback gruplama, indirme, geçmiş çalıştırmalarda tarih aralığıyla arama. |
| FR6 | Durumlar: Çalışıyor / Başlatılıyor / Sağlıksız / Durdu / Çöktü (exit code) / Bekliyor / Çökme döngüsü / **Harici** (M1). |
| FR7 | Gerçek dinlenen portlar + yapılandırılan port, tıklanabilir link, port haritası. |
| FR8 | `{port}` yer tutucu + launcher'a özel enjeksiyon, çakışma kontrolü ve sihirbazı. |
| FR9 | CPU/RAM izleme varsayılan kapalı; global/proje bazında; 2–10 sn; yalnız panel açıkken. |
| FR10 | Env değişkenleri, `.env`, çalışma dizini, argümanlar; gizliler şifreli/maskeli. |

**B. v1–v2 değerlendirmelerinden**
| # | Gereksinim | Kaynak |
|---|---|---|
| FR11 | Proje başına çoklu profil (dev/prod…). | E5 |
| FR12 | Yığınlar + çalışma zamanı bağımlılık sırası (DAG). | E3 |
| FR13 | Sağlık kontrolü (HTTP / TCP / log metni). | E4 |
| FR14 | Yeniden başlatma politikası + çökme döngüsü koruması. | E6 |
| FR15 | Toast + tepsi rozeti + bildirim merkezi + sessize alma / rahatsız etme saatleri. | E1, R2 |
| FR16 | Tepsi uygulaması; sekme kapanınca süreçler etkilenmez. | E2 |
| FR17 | Traceback → VS Code'da aç; klasör/terminal aç. | E7 |
| FR18 | Ortam doğrulama (yorumlayıcı, launcher modülü). | E8 |
| FR19 | YAML içe/dışa aktarma + `.kovan.yaml` (onaylı). | E10 |
| FR20 | Git rozeti. | E11 |
| FR21 | Windows ile başlat + panel açılınca otomatik başlat. | E12 |
| FR22 | Zamanlayıcı (cron). | E14 |
| FR23 | Birleşik log görünümü. | R1 |
| FR24 | İlk kullanım rehberi. | R3 |
| FR25 | Otomatik yedek + geri yükleme. | R4 |

**C. v3 — kullanıcı istekleri**
| # | Gereksinim | Kaynak |
|---|---|---|
| FR26 | **Paket bağımlılık izleme:** `requirements*.txt`, `pyproject.toml` (PEP 621 / Poetry / uv), `Pipfile`, `setup.cfg`, `environment.yml`, kilit dosyaları (`uv.lock`, `poetry.lock`, `Pipfile.lock`) okunur. Projenin venv'inde **kurulu sürümler** okunur. Her paket için: beyan edilen kısıt, kurulu sürüm, durum (✓ uyumlu / ✗ eksik / ⚠ kısıt dışı / ＋ beyan edilmemiş-fazla), doğrudan/dolaylı ayrımı, bağımlılık ağacı. | U1 |
| FR27 | **Güncel/eski ve güvenlik açığı kontrolü (isteğe bağlı, ağ gerektirir):** PyPI'dan en son sürüm, OSV.dev'den bilinen açıklar; varsayılan **kapalı**, elle "Kontrol et" veya günlük; sonuç önbelleklenir. Çevrimdışı modda yalnız yerel analiz. | U1 |
| FR28 | **Bağımlılık değişim takibi:** bağımlılık dosyası veya venv değiştiğinde (mtime/hash) "Bağımlılıklar değişti — venv senkron değil" uyarısı; anlık görüntüler arasında fark (eklendi/kaldırıldı/sürüm değişti). | U1 |
| FR29 | **Projeler arası bağımlılık görünümü:** bir proje diğerini yerel yol/editable (`-e ../shared-lib`, `path = "../x"`) olarak kullanıyorsa ilişki grafiği; "şu paketi kullanan projeler" araması (ör. hangi projeler `pydantic<2`). | U1 |
| FR30 | **Doküman inceleme:** proje kökü ve `docs/` altındaki `README*`, `CHANGELOG*`, `CONTRIBUTING*`, `LICENSE*`, `*.md`, `*.rst`, `*.txt`, `.env.example` panelde **güvenli şekilde** görüntülenir: Markdown render (başlık içindekiler, kod blokları renkli, göreli linkler panel içinde), RST/TXT düz metin; dosya listesi, arama; README'nin ilk paragrafı proje kartında **açıklama** olarak gösterilir. | U2 |
| FR31 | **README'den komut önerisi:** README'deki kod bloklarında `uvicorn …`, `python …`, `streamlit run …` gibi komutlar tespit edilip "Profil olarak ekle" önerilir (kullanıcı onayıyla, komut önizlemesiyle). | U2 |
| FR32 | **Kategoriler:** kullanıcı kategori oluşturur/düzenler/siler/sıralar (ad, renk, ikon, açıklama); hiyerarşik (bir seviye alt kategori, ör. `İş > API`); proje **birden çok kategoride değil, tek kategoride** + serbest **etiketler** (çoklu). Keşifte otomatik kategori önerisi (launcher tipine göre: Web API, Web UI, Bot/Script, Worker, Notebook, Zamanlanmış). | U3 |
| FR33 | **Kategori işlemleri:** kenar çubuğunda kategoriye göre filtre, sürükle-bırak ile proje taşıma, toplu kategori/etiket atama, **kategori bazında toplu başlat/durdur**, kategori bazında varsayılanlar (ör. "Bot" kategorisindeki projelere varsayılan yeniden başlatma politikası always, bildirim açık). | U3 |

**D. v3 — olmazsa olmaz eklemeler (M1–M8)**
| # | Gereksinim | Neden gerekli |
|---|---|---|
| FR34 | **M1 Harici süreç tespiti:** panel dışından (ör. eski bir CLI penceresinden) başlatılmış ve çalışma dizini/komut satırı izinli kök içindeki Python süreçleri "Harici" olarak gösterilir (PID, port, komut). Log alınamaz; "Durdur" yalnız açık onayla; "Paneli kullanarak yeniden başlat" seçeneği. | Geçiş döneminde kullanıcı hangi projenin zaten çalıştığını görmezse port çakışmalarının sebebi anlaşılmaz. |
| FR35 | **M2 Toplu işlemler ve seçim:** çoklu seçim (Shift/Ctrl), toplu başlat/durdur/yeniden başlat/kategori atama/metrik aç-kapa. | 10+ projede tek tek işlem kabul edilemez. |
| FR36 | **M3 Favoriler ve görünümler:** favori (sabitle) projeler en üstte; kaydedilmiş filtreler (ör. "Çöken + API"); sıralama (ad, durum, son çalışma, CPU). | Günlük akış hızı. |
| FR37 | **M4 Proje notları:** proje başına Markdown not (ör. "8001 portu frontend'e sabit, değiştirme"). | Yapılandırmanın "neden"i kaybolmasın. |
| FR38 | **M5 CLI eşlikçisi:** `kovan start order-api`, `kovan status`, `kovan logs -f scraper` — aynı yerel API'yi kullanır (kullanıcıya özel token dosyası). | Betiklerden/IDE görevlerinden otomasyon; CLI'ı tamamen yasaklamak değil, isteğe bağlı kılmak. |
| FR39 | **M6 Panel öz-tanılama:** Ayarlar > Tanılama: panel sürümü, kendi CPU/RAM'i, DB boyutu, log dizini boyutu, aktif WS sayısı, son hatalar, "tanılama paketini dışa aktar" (gizliler hariç). | Panelin kendisi sorun çıkarırsa inceleyebilmek. |
| FR40 | **M7 Disk ve log saklama kotası:** toplam log boyutu limiti (varsayılan 2 GB), en eski çalıştırma logları otomatik silinir; proje bazında "logları temizle". | Uzun süre çalışan botlar diski doldurmasın. |
| FR41 | **M8 Panel güncelleme bildirimi (isteğe bağlı):** GitHub Releases'tan yeni sürüm kontrolü (varsayılan kapalı), indirilen paket SHA-256/imza doğrulaması; otomatik kurulum yok. | Güvenlik yamalarının kullanıcıya ulaşması. |

### 1.2 Fonksiyonel olmayan gereksinimler
- **Performans:** Panel boşta <%1 CPU, <80 MB RAM (izleme açık <%2). Olay tabanlı asyncio; port/metrik/git/harici-süreç taraması yalnız istemci bağlıyken (tek `psutil.process_iter`/`net_connections` geçişi tüm işlere paylaştırılır). Bağımlılık analizi **talep üzerine** ve dosya değişince (mtime) önbellek geçersizleşir; kurulu paketler `site-packages/*.dist-info/METADATA` okunarak bulunur (yorumlayıcı çalıştırmadan, ms düzeyinde).
- **Gizlilik / Çevrimdışı:** Varsayılan **sıfır dış ağ trafiği**. Dış ağa çıkan tek özellikler FR27 ve FR41; ikisi de açıkça açılır, Ayarlar'da "Ağ erişimi" bölümünde listelenir.
- **Güvenilirlik:** Job Object `KILL_ON_JOB_CLOSE` ile sahipsiz süreç kalmaz; DB WAL + günlük yedek.
- **Güvenlik:** §3.
- **Kullanılabilirlik:** Kurulumsuz `.exe`, ilk kullanım rehberi, TR/EN, WCAG 2.1 AA.
- **Bakım:** Hexagonal mimari, tip ipuçları, domain/application %80+ test, ruff + mypy + bandit + pip-audit CI, ADR'ler.

### 1.3 Launcher tipleri (Strategy) × Yorumlayıcı çözümleyici
`LauncherStrategy`: `build_argv`, `inject_port`, `detect(project_dir) -> güven`, `required_modules()`, `default_category()` (FR32).

| Tip | Komut (argv, **shell yok**) | Port | Varsayılan kategori |
|---|---|---|---|
| `script` | `python x.py [args]` | `{port}` / `PORT` env | Bot/Script |
| `module` | `python -m pkg` | aynı | Bot/Script |
| `uvicorn` | `python -m uvicorn main:app --host 127.0.0.1` | `--port N` | Web API |
| `hypercorn` / `granian` | `python -m hypercorn main:app` | `--bind 127.0.0.1:N` | Web API |
| `waitress` | `python -m waitress app:app` | `--listen=127.0.0.1:N` | Web API |
| `gunicorn` (Linux) | `python -m gunicorn app:app` | `-b 127.0.0.1:N` | Web API |
| `flask` | `python -m flask --app app run` | `--port N` | Web API |
| `django` | `python manage.py runserver` | `127.0.0.1:N` | Web API |
| `streamlit` | `python -m streamlit run app.py --server.headless true` | `--server.port N` | Web UI |
| `gradio`/`dash`/`panel` | script | `GRADIO_SERVER_PORT`/`PORT` | Web UI |
| `celery`/`rq`/`dramatiq` | `python -m celery -A proj worker` | — | Worker |
| `jupyter` | `python -m jupyter lab --no-browser` | `--port N` | Notebook |
| `custom` | kullanıcı token listesi | `{port}` | — |

`InterpreterResolver`: venv / `uv run` / `poetry run` / `pipenv run` / `conda run -n` / sistem / elle yol. Zorunlu env: `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `FORCE_COLOR=1`.

### 1.4 Mimari — Hexagonal (Ports & Adapters)
```
┌──────────────────────────────── Presentation ─────────────────────────────────┐
│ FastAPI REST /api/v1 · WebSocket /ws · Jinja2+HTMX UI · i18n · CLI (kovan)  │
│ Auth · CSRF · Host/Origin guard · CSP · Rate limit                            │
└───────────────┬───────────────────────────────────────────────────────────────┘
┌───────────────▼────────────────────── Application ────────────────────────────┐
│ ProjectService  ProfileService  CategoryService  StackService  RunService     │
│ DiscoveryService  EnvValidator  DependencyService  AdvisoryService(opt-in)    │
│ DocsService  NotesService  PortService  ExternalProcessService  HealthService │
│ MetricsService  LogService  RetentionService  NotificationService             │
│ SchedulerService  BackupService  ImportExportService  AuditService            │
│ DiagnosticsService  UpdateService(opt-in)  EventBus                           │
└───────────────┬───────────────────────────────────────────────────────────────┘
┌───────────────▼───────────────────────── Domain ──────────────────────────────┐
│ Project 1─N RunProfile · Category(parent?) 1─N Project · Tag N─N Project      │
│ Interpreter · Stack─StackMember(depends_on) · RunInstance(state machine)      │
│ DependencySpec(name, constraint, source, direct) · InstalledPackage           │
│ DependencySnapshot · DependencyIssue · Advisory · ProjectLink(local path)     │
│ DocFile · Note · LogEntry · ErrorEvent · PortBinding · ExternalProcess        │
│ RestartPolicy · HealthCheck · Schedule · LauncherStrategy (ABC)               │
└───────────────┬───────────────────────────────────────────────────────────────┘
┌───────────────▼────────────────────── Infrastructure ─────────────────────────┐
│ ProcessSupervisor · os/windows (JobObject, CTRL_BREAK, DPAPI, Startup, Toast) │
│ os/posix · PsutilInspector · GitInspector · EditorLauncher                    │
│ ManifestParsers (requirements, pyproject, Pipfile, setup.cfg, env.yml, locks) │
│ SitePackagesReader (dist-info) · PyPIClient · OSVClient (httpx, timeout, TLS) │
│ MarkdownRenderer (markdown-it-py + nh3 sanitizer) · SqliteRepository(WAL)     │
│ RotatingLogStore · SecretVault · RedactionFilter · TracebackGrouper · Tray    │
└───────────────────────────────────────────────────────────────────────────────┘
```
Desenler: SOLID, Strategy (launcher, manifest parser), Repository, Observer/EventBus, DI, State Machine, 12-Factor, ADR, SemVer, Conventional Commits, OpenAPI.

**Durum makinesi:**
```
STOPPED → STARTING ──(sağlık OK)──► RUNNING ⇄ UNHEALTHY
   ▲          └──(exit≠0)──► CRASHED ──(policy)──► BACKOFF ──► STARTING
   │                            └──(N çökme / T sürede)──► CRASH_LOOP
   └── STOPPING ◄── kullanıcı / yığın / kategori / zamanlayıcı
EXTERNAL (panel dışı süreç) ──(kullanıcı "panelle yeniden başlat")──► STOPPING → STARTING
```
Durdurma: Windows `CTRL_BREAK_EVENT` → zaman aşımı → `TerminateJobObject`; POSIX `SIGINT → SIGTERM → SIGKILL`.

**Bağımlılık analizi hattı (FR26–FR29):**
1. `ManifestParsers` beyanları çıkarır (`packaging.requirements.Requirement` ile PEP 508 ayrıştırma; `tomllib` ile pyproject; `-r` include'ları izinli kök içinde izlenir, `-e`/`path=` yerel bağlantılar `ProjectLink` olur).
2. `SitePackagesReader` projenin venv'indeki `*.dist-info/METADATA`'dan ad/sürüm/`Requires-Dist` okur → kurulu ağaç (yorumlayıcı **çalıştırılmaz**; conda/sistem gibi yeri bilinmeyenlerde tek seferlik sabit betik `importlib.metadata` ile sys.path listesi alınır).
3. Karşılaştırma: `packaging.specifiers` ile kısıt kontrolü → `DependencyIssue` (eksik / kısıt dışı / beyansız / çakışan kısıt).
4. `DependencySnapshot` (hash) saklanır; dosya/venv mtime değişince yeniden analiz + fark.
5. İsteğe bağlı `AdvisoryService`: yalnız **paket adı + sürüm** OSV.dev `querybatch` ve PyPI JSON API'ye gönderilir (proje adı/yolu gönderilmez), 24 sa önbellek.

**Doküman hattı (FR30–FR31):** DocsService izinli dosya kalıplarını listeler (boyut sınırı 2 MB) → MarkdownRenderer: `markdown-it-py` (HTML girişi kapalı) → `nh3` ile beyaz liste temizleme → göreli linkler panel rotasına, dış linkler `rel="noopener noreferrer"` + "dış bağlantı" onayı; uzak görseller varsayılan **yüklenmez** (izleme piksellerini engeller), yerel görseller proje içinden servis edilir. Komut önerisi: kod bloklarında regex ile launcher eşleşmesi → profil taslağı (kaydetmeden önce onay).

**Kategori modeli (FR32–FR33):** `Category{id, name, color, icon, parent_id?, sort, defaults{restart_policy?, notify?, metrics?}}`; projenin kendi ayarı kategori varsayılanını ezer (açık > kategori > global). Silinen kategorinin projeleri "Kategorisiz"e taşınır (onaylı).

**Log hattı:** satır okuyucu (16 KB sınır) → zaman damgası → RedactionFilter → TracebackGrouper → bellek tamponu (5000 satır) + döner dosya + WS (50 ms toplu, geri basınç). RetentionService toplam kota ve 14 gün kuralını uygular.

### 1.5 Teknoloji yığını
Python 3.12+, FastAPI + uvicorn, SQLModel/SQLite (WAL), psutil, pydantic-settings, PyYAML (`safe_load`), tomllib, **packaging**, **httpx** (yalnız opt-in özellikler), **markdown-it-py + nh3**, croniter, pywin32, windows-toasts, pystray + Pillow, argon2-cffi, itsdangerous, Typer (CLI eşlikçisi).
Ön yüz: Jinja2 + HTMX + Alpine.js, xterm.js, uPlot, **Sortable.js** (sürükle-bırak), **Cytoscape.js** veya hafif SVG (bağımlılık/proje grafiği), highlight.js (doküman kod blokları) — hepsi vendored.
Paketleme: PyInstaller onedir → `Kovan.exe` + `kovan.exe` (CLI). Kalite: pytest, pytest-asyncio, Playwright, ruff, mypy, bandit, pip-audit, pre-commit, uv lock.

---

## 2. Ön Yüz — Arayüz Tasarımı (v3)

### İlkeler
Sade "developer tool" estetiği; açık/koyu tema; Inter + JetBrains Mono; `--accent #4F46E5`, `--ok #16A34A`, `--warn #D97706`, `--err #DC2626`, `--idle #6B7280`, `--ext #0EA5E9` (harici). Kategori renkleri 10'luk erişilebilir paletten. Durum her zaman ikon + metin + renk.

### A) Dashboard
```
┌──────────────┬───────────────────────────────────────────────────────────────────────┐
│ ◆ Kovan     │ 🔍 Ara / komut (Ctrl+K)                🔔3  [⟳ Tara] [+ Proje]        │
│              ├───────────────────────────────────────────────────────────────────────┤
│ ★ Favoriler 3│ 4 çalışıyor · 1 sağlıksız · 1 çöktü · 1 harici · 5 durdu  CPU/RAM [○─]│
│ ▤ Tümü     12│ Görünüm: [Tablo|Kart]  Sırala: Durum ▾  Filtre: "Çöken+API" ▾          │
│ KATEGORİLER ✎├───────────────────────────────────────────────────────────────────────┤
│ ■ Web API   5│ ☐ ★ ● order-api · dev ▾   ■Web API  uvicorn :8001↗ main*  ⚠2 dep 2s ■⟳⋯│
│   ↳ Shop    3│      "Sipariş servisi — FastAPI + Postgres"          #shop #backend   │
│ ■ Web UI    2│ ☐   ◐ auth-api · dev ▾    ■Web API  uvicorn :8002↗ feat/x       3dk ■⟳⋯│
│ ■ Bot/Script3│ ☐   ● scraper             ■Bot      script   —     main     1s 02dk ■⟳⋯│
│ ■ Worker    1│ ☐   ✖ report-svc          ■Web API  flask   :5000  main  exit 1 [Hata]▶│
│ ○ Kategorisiz│        └ KeyError: 'DB_URL' (3 kez) [VS Code]                         │
│ ETİKETLER    │ ☐   ⇄ legacy-bot  HARİCİ  ■Bot      python  :9000  pid 7720 [Devral]  │
│ #shop #ml    │ ☐   ○ nightly-etl ⏰02:00 ■Bot      script   —     main      —    ▶  ⋯ │
│ Yığınlar     ├───────────────────────────────────────────────────────────────────────┤
│ Port haritası│ 2 seçili: [▶ Başlat] [■ Durdur] [Kategori ▾] [Etiket ▾] [Metrik ▾]    │
│ Bağımlılıklar│                                                                       │
│ Birleşik log │                                                                       │
│ Zamanlayıcı  │                                                                       │
│ ⚙ Ayarlar    │                                                                       │
└──────────────┴───────────────────────────────────────────────────────────────────────┘
```
- Kenar çubuğunda kategori ağacı: sayaçlar, renk kareleri, kategoriye sağ tık → `Tümünü başlat / Tümünü durdur / Düzenle / Sil`; ✎ ile **Kategori yöneticisi**. Projeyi satırdan kategoriye **sürükle-bırak**.
- Satırda README'den alınan **açıklama** (FR30) ve etiketler; `⚠2 dep` bağımlılık sorun rozeti (FR26); `⇄ HARİCİ` satırı (FR34).
- Altta seçim çubuğu (FR35) yalnız seçim varken görünür.
- Kart görünümünde kategoriye göre gruplu bölümler (katlanabilir).

### B) Kategori yöneticisi (modal / Ayarlar sekmesi) — FR32/FR33
```
 Kategoriler                                            [+ Yeni kategori]
 ⠿ ■ Web API      ⚡ 5 proje   Varsayılan: restart=on-failure, bildirim=açık   ✎ 🗑
 ⠿   ↳ ■ Shop     ⚡ 3 proje                                                   ✎ 🗑
 ⠿ ■ Web UI       ⚡ 2 proje                                                   ✎ 🗑
 ⠿ ■ Bot/Script   ⚡ 3 proje   Varsayılan: restart=always                      ✎ 🗑
 ┌ Düzenle: Bot/Script ───────────────────────────────────────────┐
 │ Ad [Bot/Script]  Renk ●●●●●●●●●●  İkon [🤖 ▾]  Üst kategori [— ▾]│
 │ Açıklama [...........]                                          │
 │ Varsayılanlar: Yeniden başlatma [always ▾]  Bildirim [✓]  Metrik [ ]│
 │                                          [İptal] [Kaydet]       │
 └─────────────────────────────────────────────────────────────────┘
```
⠿ tutamacıyla sıralama; silmede "5 proje Kategorisiz'e taşınacak" onayı.

### C) Proje detayı — sekmeler
`Genel` · `Loglar` · `Hatalar` · `Bağımlılıklar` · `Dokümanlar` · `Yapılandırma` · `Sağlık` · `Metrikler` · `Geçmiş` · `Notlar`

- **Genel:** README özeti, kategori/etiket seçici (inline düzenleme), durum, portlar, yorumlayıcı, git, son çalışma, bağımlılık özeti, hızlı eylemler.
- **Loglar / Hatalar / Sağlık / Metrikler / Geçmiş:** v2 ile aynı (xterm.js, gruplanmış traceback + VS Code, sağlık zaman çizelgesi, uPlot, çalıştırma geçmişi + log arama).
- **Bağımlılıklar (FR26–FR29):**
```
 Kaynak: pyproject.toml + uv.lock   Yorumlayıcı: .venv (3.12.4)   Son analiz: 2 dk önce [⟳]
 Özet: 42 paket · 12 doğrudan · ✗1 eksik · ⚠1 kısıt dışı · ＋3 beyansız · 🛡 2 açık [Kontrol et]
 ┌ Filtre: [Tümü|Doğrudan|Sorunlu] 🔍 ─────────────────────────────────────────────────┐
 │ PAKET         BEYAN        KURULU    EN SON   DURUM            GÜVENLİK            │
 │ fastapi       >=0.110      0.115.2   0.118.0  ✓ (güncel değil)  —                  │
 │ pydantic      >=2,<3       2.9.1     2.11.0   ✓                 —                  │
 │ python-dotenv >=1.0        —         1.0.1    ✗ eksik  [pip komutunu kopyala]      │
 │ requests      ==2.31.0     2.28.1    2.32.3   ⚠ kısıt dışı      🛡 GHSA-9wx4… orta │
 │ ▸ urllib3 (dolaylı: requests)  1.26.5         ＋ beyansız        🛡 CVE-2024-… yüksek│
 └─────────────────────────────────────────────────────────────────────────────────────┘
 [Ağaç görünümü] [Değişiklik geçmişi: 3 anlık görüntü] [Bu paketi kullanan diğer projeler]
 ⚠ requirements.txt 10 dk önce değişti — venv senkron değil.
```
  Ağaç görünümü katlanabilir; "Projeler arası" sekmesinde yerel bağlantı grafiği (`order-api → shared-lib ← auth-api`).
- **Dokümanlar (FR30–FR31):**
```
 ┌ Dosyalar ─────────┬──────────────────────────────────────────────────┐
 │ 📄 README.md   ●  │ # Order API                       İçindekiler ▸  │
 │ 📄 CHANGELOG.md   │ Sipariş servisi...                               │
 │ 📄 .env.example   │ ## Çalıştırma                                     │
 │ 📁 docs/          │ ```bash                                           │
 │   📄 api.md       │ uvicorn app.main:app --reload --port 8001         │
 │   📄 deploy.rst   │ ```  💡 Bu komut profil olarak eklensin mi? [Ekle]│
 │ 🔍 Dokümanda ara  │ 🖼 Uzak görseller engellendi [Bir kez göster]     │
 └───────────────────┴──────────────────────────────────────────────────┘
```
  `.env.example` açıldığında "Eksik env değişkenleri: `DB_URL`, `SECRET_KEY` — Yapılandırmaya ekle" önerisi (değerler kopyalanmaz).
- **Yapılandırma:** profil sekmeleri, launcher, yorumlayıcı + Ortam doğrula, hedef, dizin, port + boş port bul, argümanlar, env tablosu (🔒), `.env`, yeniden başlatma (kategori varsayılanı gri ipucuyla gösterilir: "Kategoriden: always"), otomatik başlat, zamanlama, bildirim; altta **komut önizlemesi**.
- **Notlar (FR37):** Markdown editör + önizleme (aynı güvenli renderer).

### D) Diğer ekranlar
- **Bağımlılıklar (global):** tüm projelerde paket arama ("pydantic" → hangi projede hangi sürüm), sorunlu projeler listesi, güvenlik açığı özeti, projeler arası grafik.
- **Port haritası, Birleşik log, Zamanlayıcı, Bildirim merkezi, Yığınlar, Keşif sihirbazı** (keşifte artık önerilen **kategori** sütunu ve README açıklaması da var), **İlk kullanım rehberi** (5 adım: parola → kök klasör → keşif → kategorileri onayla → ilk projeyi başlat), **Tepsi menüsü** (kategori alt menüleri ile).
- **Ayarlar:** kök klasörler, port aralığı, metrik aralığı, log saklama + **disk kotası**, tema, dil, bildirimler, Windows ile başlat, editör yolu, **Ağ erişimi** (güvenlik açığı kontrolü, güncelleme kontrolü — ikisi de kapalı varsayılan), CLI token yenile, yedekler, içe/dışa aktar, parola, denetim kaydı, **Tanılama** (FR39).

### Etkileşim
Ctrl+K komut paleti ("kategori Bot başlat", "deps order-api", "readme scraper"), kısayollar (`S`, `R`, `L`, `D` bağımlılık, `M` doküman, `/`), toast'lar, yıkıcı işlemlerde onay, iskelet yükleyiciler, WS yeniden bağlanma şeridi, WCAG 2.1 AA, tam klavye gezinme (sürükle-bırak'ın klavye alternatifi: "Kategoriye taşı" menüsü).

---

## 3. Güvenlik (STRIDE) — v3

> Panel tanımı gereği komut çalıştırma aracıdır; ele geçirilmesi = makinenin ele geçirilmesi. v3'te eklenen her özellik (bağımlılık, doküman, kategori, harici süreç, CLI, ağ erişimi) bu gözle değerlendirildi.

| Risk | Önlem |
|---|---|
| Ağdan erişim | Yalnız `127.0.0.1`; 0.0.0.0 seçeneği yok (uzak erişim için SSH tüneli dokümante). |
| DNS rebinding | `TrustedHostMiddleware`, WS'de `Origin` doğrulaması. |
| CSRF | CSRF token + `SameSite=Strict` + `Origin/Referer` kontrolü. |
| Kimlik doğrulama | İlk kullanımda parola (argon2id); imzalı `HttpOnly` oturum (8 sa); giriş hız sınırı; tepsiden tek kullanımlık 30 sn token. **CLI eşlikçisi:** `%LOCALAPPDATA%\Kovan\cli.token` (yalnız kullanıcı ACL), `Authorization: Bearer`, Ayarlar'dan yenilenebilir/iptal edilebilir; CLI token'ı parola değiştirme/ayar dışa aktarma yapamaz (kapsam sınırlı). |
| Komut enjeksiyonu | `shell=False`, argv liste, yalnız `{port}` şablonu. README'den önerilen komutlar `shlex.split` ile token'lanır, operatör içerenler (`&&`, `|`, `>`, `;`) **reddedilir**, kaydetmeden önce önizleme + onay. |
| Path traversal | Tüm dosya erişimi (çalışma dizini, `.env`, editör, doküman, yerel görsel, `-r` include, `path=` bağlantıları) **izinli kök** içinde (`resolve()` + `is_relative_to`, symlink çözümü). Doküman uç noktası proje ID + göreli yol alır, mutlak yol almaz. |
| Kod çalıştırma (keşif/bağımlılık/doküman) | Keşif ve manifest okuma yalnız dosya ayrıştırır (`tomllib`, `packaging`, `ast.parse`, regex) — `setup.py` **çalıştırılmaz** (yalnız AST'ten statik `install_requires` okunur, okunamazsa "dinamik — analiz edilemedi"). Kurulu paketler `dist-info` dosyalarından okunur. `.kovan.yaml` ve içe aktarılan YAML güvenilmeyen girdi: `safe_load` + şema + onay + hash. |
| XSS (doküman/not/log) | Markdown: HTML girişi kapalı + `nh3` beyaz liste (script/iframe/style/on* yok, `javascript:` ve `data:` URL yok); katı CSP (`default-src 'self'; script-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'`) — uzak görseller CSP ile de engelli, "bir kez göster" panel tarafında proxy'lenmez, yalnız tarayıcıda yeni sekmede açılır. Log xterm.js'e düz metin. Kategori/etiket/not adları autoescape. |
| SSRF / veri sızıntısı (dış ağ) | Dış istekler yalnız sabit host'lara (`api.osv.dev`, `pypi.org`, `api.github.com`), TLS doğrulamalı, zaman aşımlı; gönderilen veri yalnız paket adı/sürümü; varsayılan kapalı, Ayarlar'da açıkça listelenir. Sağlık kontrolü hedefleri varsayılan yalnız `127.0.0.1`/`localhost` (başka host için ayrı onay). |
| Gizli bilgi sızıntısı | Gizli env DPAPI ile şifreli, UI'da maskeli; RedactionFilter; YAML export, yedek ve tanılama paketi gizlileri içermez; `.env.example` önerisi değer kopyalamaz; panel gerçek `.env` dosyalarını Dokümanlar'da **göstermez** (yalnız `.env.example`/`.env.sample`). Toast'ta yalnız proje adı + istisna tipi. |
| Yanlış süreç öldürme | PID + `create_time` doğrulaması; yalnız kendi Job Object'i. **Harici süreçler** yalnız izinli kök içindekiler listelenir; durdurma açık onay + süreç komut satırı gösterimi + denetim kaydıyla; yönetici/başka kullanıcı süreçleri asla. |
| Güncelleme tedarik zinciri | Güncelleme yalnız bildirim; indirme kullanıcıya bırakılır, yayın SHA-256 ve imza bilgisi gösterilir; otomatik kurulum yok. |
| Yetki yükseltme | Admin çalıştırılırsa uyarı; "Windows ile başlat" kullanıcı Startup kısayolu. |
| Veri bütünlüğü | `%LOCALAPPDATA%\Kovan` kullanıcı ACL; yedekler aynı ACL; geri yüklemede şema doğrulaması. |
| İnkâr | Append-only denetim kaydı: giriş, başlat/durdur (tetikleyen: kullanıcı/CLI/yığın/kategori/zamanlayıcı/otomatik), harici süreç durdurma, config/kategori değişikliği, README'den profil ekleme, ağ özelliği açma, içe/dışa aktarma, geri yükleme. |
| Tedarik zinciri (panel) | Hash pinli lock, pip-audit CI, minimal bağımlılık, vendored JS'lerin SHA-256'sı kayıtlı, `.exe` isteğe bağlı imzalama. |
| DoS | Halka tampon/satır sınırı, WS geri basınç, çökme döngüsü koruması, zamanlayıcıda üst üste binme yok, doküman 2 MB ve manifest include derinliği (5) sınırı, disk kotası, dış istek hız sınırı. |

---

## 4. Son Kullanıcı Değerlendirmesi — 3. Tur

### 4.1 Yeni istekler
| İstek | v3'te karşılanma | Sonuç |
|---|---|---|
| U1 Proje bağımlılıkları izlenebilsin | FR26 beyan vs kurulu karşılaştırma, ağaç; FR27 güncel/açık kontrolü (opt-in); FR28 değişim takibi + "venv senkron değil"; FR29 projeler arası grafik ve global paket arama; Bağımlılıklar sekmesi + global ekran; dashboard rozeti | ✅ |
| U2 README vb. dosyalar incelenebilsin | FR30 güvenli Markdown/RST/TXT görüntüleyici, içindekiler, arama, kartta açıklama; FR31 README'den profil önerisi; `.env.example` → eksik env önerisi | ✅ |
| U3 Kategori seçimi ve düzenleme | FR32 kategori CRUD (ad/renk/ikon/alt kategori/sıra) + etiketler + keşifte otomatik öneri; FR33 filtre, sürükle-bırak, toplu atama, kategori bazında toplu başlat/durdur ve varsayılanlar; Kategori yöneticisi | ✅ |

### 4.2 Olmazsa olmaz eklemeler (M1–M8)
Harici süreç tespiti (FR34), toplu işlemler (FR35), favoriler ve kaydedilmiş görünümler (FR36), proje notları (FR37), CLI eşlikçisi (FR38), panel öz-tanılama (FR39), disk/log kotası (FR40), güncelleme bildirimi (FR41) — hepsi gereksinim, mimari, arayüz ve güvenlik bölümlerine işlendi.

### 4.3 Önceki turlar
E1–E14 ve R1–R4'ün tamamı korunuyor (FR11–FR25); v3 değişiklikleri bunlarla çakışmıyor — kategori varsayılanları yeniden başlatma/bildirim/metrik ayarlarıyla "açık > kategori > global" önceliğiyle bütünleşti; tepsi menüsü ve komut paleti kategorileri destekliyor.

**Son kullanıcı görüşü:** İlk talepteki tüm temel ihtiyaçlar, üç değerlendirme turunda çıkan 25 ek istek (E1–E14, R1–R4, U1–U3, M1–M8 içinden kullanıcıya dönük olanlar) karşılanıyor. Varsayılan davranış güvenli ve çevrimdışı; ağ gerektiren özellikler açıkça açılıyor. Kapsam geniş, fazlara bölünerek teslim edilmeli (§6). **Onaya hazır.**

---

## 5. İzlenebilirlik (istek → tasarım)
| İstek | FR | Bileşen | Arayüz | Güvenlik notu |
|---|---|---|---|---|
| Temel talep | FR1–FR10 | Run/Log/Port/Metrics/Discovery | Dashboard, Loglar, Yapılandırma | 127.0.0.1, auth, shell=False |
| E1/R2 | FR15 | NotificationService | 🔔, tepsi rozeti | Mesaj içeriği yok |
| E2 | FR16 | Tray | Tepsi menüsü | Tek kullanımlık token |
| E3 | FR12 | StackService | Yığınlar | Aynı RunService yolu |
| E4 | FR13 | HealthService | Sağlık | Yalnız localhost varsayılan |
| E5 | FR11 | ProfileService | Profil seçici | — |
| E6 | FR14 | RunService | Bekliyor/Çökme döngüsü | DoS koruması |
| E7 | FR17 | EditorLauncher | VS Code | Yol allowlist |
| E8 | FR18 | EnvValidator | Ortam doğrula | find_spec, oto kurulum yok |
| E9 | FR7/8 | PortService | Port haritası | Yabancı süreç öldürülmez |
| E10 | FR19 | ImportExportService | Ayarlar, keşif | safe_load + onay + hash |
| E11 | FR20 | GitInspector | Git rozeti | Git çalıştırılmaz |
| E12 | FR21 | os/windows | Ayarlar | Admin gerekmez |
| E13 | FR5 | LogService | Geçmiş | Redaksiyon |
| E14 | FR22 | SchedulerService | Zamanlayıcı | Üst üste binme yok |
| R1 | FR23 | LogService | Birleşik log | — |
| R3 | FR24 | — | Rehber | Parola zorunlu |
| R4 | FR25 | BackupService | Yedekler | Gizliler şifreli |
| **U1** | FR26–29 | DependencyService, ManifestParsers, SitePackagesReader, AdvisoryService | Bağımlılıklar sekmesi + global, dep rozeti | setup.py çalıştırılmaz; dış ağ opt-in, yalnız ad/sürüm |
| **U2** | FR30–31 | DocsService, MarkdownRenderer | Dokümanlar sekmesi, kart açıklaması | nh3 + CSP, uzak görsel yok, `.env` gizli |
| **U3** | FR32–33 | CategoryService | Kenar çubuğu ağacı, Kategori yöneticisi, toplu çubuk | Autoescape, denetim kaydı |
| M1 | FR34 | ExternalProcessService | Harici satırı, Devral | Yalnız izinli kök, onaylı durdurma |
| M2 | FR35 | — | Seçim çubuğu | Yıkıcı toplu işlem onayı |
| M3 | FR36 | — | Favoriler, kayıtlı filtre | — |
| M4 | FR37 | NotesService | Notlar | Güvenli renderer |
| M5 | FR38 | CLI (Typer) | — | Kapsamlı token, ACL |
| M6 | FR39 | DiagnosticsService | Tanılama | Gizliler hariç paket |
| M7 | FR40 | RetentionService | Ayarlar | Disk DoS önlemi |
| M8 | FR41 | UpdateService | Ayarlar > Ağ | Oto kurulum yok, hash |

## 6. Kod yapısı ve fazlar
```
kovan/
├─ pyproject.toml / uv.lock
├─ src/kovan/
│  ├─ main.py  config.py  cli.py
│  ├─ domain/          models.py states.py events.py stack_graph.py categories.py dependencies.py
│  │                   launchers/{base,script,module,uvicorn,flask,django,streamlit,...}.py interpreters.py
│  ├─ application/     project/profile/category/stack/run services, discovery.py env_validator.py
│  │                   dependency_service.py advisory_service.py docs_service.py notes_service.py
│  │                   ports.py external_processes.py health.py metrics.py logs.py retention.py
│  │                   notifications.py scheduler.py backup.py import_export.py audit.py diagnostics.py update.py event_bus.py
│  ├─ infrastructure/  supervisor.py os/{windows,posix}.py psutil_inspector.py git_inspector.py editor.py
│  │                   manifests/{requirements,pyproject,pipfile,setup_cfg,setup_py_ast,conda,locks}.py
│  │                   site_packages.py pypi_client.py osv_client.py markdown.py db.py log_store.py
│  │                   secrets.py redaction.py traceback_grouper.py tray.py toast.py
│  └─ web/             app.py security.py api/*.py ws.py i18n/{tr,en}.json templates/ static/(vendored)
├─ tests/ unit/ integration/ e2e/ fixtures/sample_projects/{script,fastapi,flask,django_min,streamlit,
│         crasher,slow_start,deps_broken,shared_lib,readme_rich,malicious_readme}
├─ docs/adr/ docs/security.md docs/user-guide.md
└─ packaging/kovan.spec
```
1. **Faz 1 – Çekirdek (MVP):** launcher'lar (script, module, uvicorn, flask, django, streamlit, custom), InterpreterResolver (venv/sistem/uv), ProcessSupervisor, canlı log, dashboard, proje/profil editörü + komut önizlemesi, port tespiti + çakışma kontrolü, **kategoriler + etiketler + filtre** (U3 temel), **README görüntüleyici** (U2 temel), auth/CSRF/Host/CSP, SQLite, ilk kullanım rehberi.
2. **Faz 2 – İzleme & dayanıklılık:** keşif sihirbazı (kategori önerisiyle), hata gruplama + VS Code, CPU/RAM, sağlık, yeniden başlatma + çökme döngüsü, tepsi + toast + bildirim merkezi, gizli env + redaksiyon, denetim kaydı, port haritası, **harici süreç tespiti**, **toplu işlemler + kategori toplu başlat/durdur + kategori varsayılanları**, **bağımlılık analizi (yerel: FR26, FR28)**, disk kotası, PyInstaller `.exe`.
3. **Faz 3 – Verimlilik:** yığınlar, çoklu profil, birleşik log, geçmiş araması, ortam doğrulama, YAML/`.kovan.yaml`, git rozeti, Windows ile başlat, zamanlayıcı, yedekleme, **güvenlik açığı/güncel kontrol (FR27)**, **projeler arası grafik + global paket arama (FR29)**, **README'den profil önerisi (FR31)**, favoriler/kayıtlı görünümler, notlar, CLI eşlikçisi, tanılama, güncelleme bildirimi, kalan launcher/yorumlayıcılar, i18n.

## 7. Doğrulama
- **Birim:** launcher × yorumlayıcı argv/port testleri; state machine; yığın DAG; kategori ağacı (döngü, silmede taşıma, varsayılan öncelik "açık > kategori > global"); manifest ayrıştırıcıları (PEP 508 kenar durumları, `-r` include, `-e ../lib`, Poetry/uv/Pipfile/conda, AST'ten setup.py); kısıt karşılaştırma (eksik/kısıt dışı/beyansız); snapshot farkı; README komut çıkarma (operatörlü komut reddi); TracebackGrouper, RedactionFilter; path allowlist; PID+create_time.
- **Entegrasyon:** fixture projelerini çalıştır → log WS, port tespiti, çakışma engeli, `--reload` ağacı tam durma, `crasher` → backoff + çökme döngüsü, `slow_start` → sağlık + yığın sırası; `deps_broken` → doğru sorun listesi ve venv değişince "senkron değil" uyarısı; `shared_lib` → projeler arası bağlantı; `readme_rich` → TOC, göreli link, kod bloğu önerisi; panel dışından başlatılan süreç "Harici" görünür; kategori toplu başlat doğru projeleri başlatır; CLI `kovan status` token ile çalışır, tokensız reddedilir.
- **Güvenlik:** yanlış `Host` → 400; CSRF'siz POST → 403; yabancı `Origin` WS → red; oturumsuz → 401; kök dışı yol (dizin, editör, doküman `../`, include) → red; `malicious_readme` (script, `javascript:` link, onerror, uzak görsel, iframe) → hiçbiri çalışmaz/yüklenmez; `.env` doküman listesinde yok; `setup.py` içindeki yan etkili kod çalışmaz (dosya oluşturmaz); ağ özellikleri kapalıyken sıfır dış bağlantı (proxy loguyla doğrulanır); export/yedek/tanılama paketinde gizli yok; bandit + pip-audit temiz.
- **E2E (Playwright, önyüklü Chromium):** rehber → keşif (kategori onayı) → kategori düzenle + sürükle-bırak → başlat → log → port linki → README oku → bağımlılık sekmesi → metrik aç/kapa → kategori toplu durdur.
- **Performans:** 10 proje çalışırken panel <%1 CPU / <80 MB; 50 projelik bağımlılık analizi <3 sn (önbellekli <200 ms).
- **Paket:** Windows'ta `Kovan.exe` → tepsi, otomatik giriş, toast, çıkışta çocuk süreç kalmıyor; `kovan.exe status` çalışıyor.


---

# v4 — Süreç Gözetimi ve Kalıcı / Taşınabilir Yapılandırma (Kovan 0.2)

> Kullanıcı geri bildirimine göre eklendi. Aşağıdaki metin onaylanan plandır; uygulama notları en sondadır.


## Context
Kullanıcı geri bildirimi:
1. **"Uygulama kırılıp sonlanırsa veya üçüncü parti bir uygulama tarafından kapatılırsa panel bunu algılayamıyor."**
2. **"Yazılım güncellemelerinde yapılandırma/etiket vb. kaybolmamalı, yapılandırma taşınabilmeli."**

Mevcut kodda (`src/kovan/application/run_service.py`, `infrastructure/supervisor.py`) tespit yalnızca **kök
sürecin** `Popen.wait()` dönüşüne bağlı. Kök neden analizi (ekip — backend):

| # | Senaryo | Bugünkü davranış | Neden |
|---|---|---|---|
| A | `uvicorn --reload`, `flask --debug`, Django autoreload, `uv run`, Windows venv `python.exe` başlatıcısı: **asıl işçi** (alt süreç) çöker/öldürülür, ebeveyn yaşar | "Çalışıyor" görünür, port kapalı | Sadece kök izleniyor |
| B | Kök süreç öldürülür, alt süreç yetim kalır ve portu tutmaya devam eder | "Çöktü"/"Durdu" görünür ama uygulama aslında çalışıyor; yeniden başlatmada port çakışması | Yetimler izlenmiyor |
| C | Üçüncü parti süreci **nazikçe** kapatır (SIGTERM/WM_CLOSE → çıkış kodu 0) | Sessizce "Durdu", bildirim yok | `rc==0` kullanıcı durdurması ile ayırt edilmiyor |
| D | Süreç donar (canlı ama yanıt vermiyor) | "Çalışıyor" | Sağlık kontrolü yok (plan Faz 2'deydi) |
| E | Panel yeniden başlar/güncellenir; Linux'ta çocuklar yaşar, Windows'ta Job Object ile ölür | Panel eski süreçleri tanımaz; `runs` kayıtları açık kalır | Çalışan süreç bilgisi kalıcı değil |
| F | Panel dışından başlatılmış süreç (eski CLI penceresi) | Görünmez, sadece port çakışması | Harici süreç tespiti yok (plan M1) |
| G | Tarayıcı sekmesi kapalıyken olan çökme | Kullanıcı fark etmez | Bildirim yalnızca panel içi toast |

Yapılandırma tarafında (ekip — backend/güvenlik): veri `%LOCALAPPDATA%\Kovan\kovan.db`'de, ancak **şema sürümü,
migrasyon, yedek, dışa/içe aktarma yok**; yeni sürümde model alanı değişirse pydantic doğrulaması kayıtları
okuyamayabilir; başka PC'ye taşımanın yolu yok; proje/kategori için makineden bağımsız kimlik yok.

Hedef: Her sonlanma biçimi **saniyeler içinde, nedeniyle birlikte** algılanır ve bildirilir; istenirse otomatik
yeniden başlatılır. Yapılandırma güncellemelerde **asla kaybolmaz**, tek dosyayla başka makineye taşınır.

---

## WP1 — Süreç Gözetimi (Watchdog)

### Tasarım
**1. Süreç ağacı izleme (`infrastructure/supervisor.py` + yeni `application/watchdog.py`)**
- `ManagedProcess` kök PID'e ek olarak **izlenen ağaç**ı tutar: `{pid: create_time}` (kök + tüm torunlar),
  her turda `psutil.Process(root).children(recursive=True)` ile yenilenir. PID yeniden kullanımına karşı her
  kontrol `create_time` ile doğrulanır (mevcut `os_adapters.terminate_tree` mantığı yeniden kullanılır).
- `Watchdog` asyncio görevi: **her 2 sn**, UI bağlı olmasa da çalışır (yalnız yönetilen PID'ler kontrol edilir →
  maliyet ihmal edilebilir). Kontroller:
  - Kök öldü ama bekleyici thread henüz bildirmedi → çıkışı işle (yedek yol).
  - Kök öldü, torunlar yaşıyor → durum **`ORPHANED` ("Yetim süreç")**, yetim PID'ler + tuttukları portlar
    gösterilir; "Yetimleri sonlandır" eylemi (yalnız create_time doğrulanmış, bizim başlattığımız ağaç).
  - Kök yaşıyor, profilde port var ve port **daha önce dinleniyordu** ama `grace` (varsayılan 10 sn) boyunca
    dinlenmiyor → **`UNHEALTHY` ("Yanıt vermiyor")**; işçi sayısı 0'a düştüyse neden "İşçi süreç sonlandı".
- Port taraması (`inspector.listening_map`) watchdog içinde yalnızca portlu çalışan projeler için ve 5 sn'de bir.

**2. Sağlık kontrolü (FR13'ün öne çekilmesi)** — profil alanı `health: {type: none|tcp|http|log, target, interval,
timeout, start_grace, failures}`; varsayılan: portlu profillerde `tcp` (127.0.0.1:port), diğerlerinde `none`.
HTTP yalnız `127.0.0.1/localhost` hedeflerine izinli (SSRF koruması). Ardışık `failures` (3) başarısızlıkta
`UNHEALTHY`, düzelince `RUNNING`.

**3. Çıkış nedeni sınıflandırması** — `runs` tablosuna `end_reason`, `signal` sütunları; `Runtime.end_reason`:

| end_reason | Koşul | Durum | Bildirim |
|---|---|---|---|
| `user_stop` | `stopping=True` (UI/toplu/CLI) | STOPPED | yok |
| `exited_ok` | rc=0, kullanıcı durdurmadı, kısa ömürlü betik profili (`expect_exit=True`) | STOPPED ("Tamamlandı") | bilgi |
| `external_stop` | rc=0 veya SIGTERM/SIGINT/CTRL_BREAK, kullanıcı durdurmadı | **STOPPED_EXTERNALLY** ("Dışarıdan kapatıldı") | uyarı |
| `killed` | POSIX negatif rc (SIGKILL vb.) / Windows rc=1 + traceback yok + `TerminateProcess` imzası | **CRASHED** ("Öldürüldü: SIGKILL") | hata |
| `crashed` | rc≠0 ve traceback/hata çıktısı var | CRASHED | hata (son istisna ile) |
| `spawn_failed` | başlatılamadı | CRASHED | hata |
| `panel_restart` | panel kapanırken/yeniden açılırken | STOPPED | — |

Windows notu: `TerminateProcess` (Görev Yöneticisi) varsayılan rc=1 bıraktığı için "killed" ile "crashed"
ayrımı stderr'de traceback olup olmamasına göre yapılır; kesin değilse "Beklenmedik sonlanma (kod 1)".

**4. Yeniden başlatma politikası (FR14'ün öne çekilmesi)** — profil alanı `restart: {policy: never|on-failure|always,
max_retries: 5, window_sec: 300, backoff: 1→2→4…≤60 sn}`. `on-failure` = crashed/killed/unhealthy(ops.)
durumlarında. Pencere içinde `max_retries` aşılırsa **`CRASH_LOOP`** ("Çökme döngüsü — otomatik başlatma
durduruldu"). Kategori varsayılanı desteklenir (açık > kategori > global). Yeni durum `BACKOFF` ("Yeniden
başlatılıyor… 4 sn"). Varsayılan politika `never` (beklenmedik davranış olmasın).

**5. Kalıcı canlı süreç kaydı (Senaryo E)** — yeni tablo `live_processes(project_id, run_id, pid, create_time,
argv, started)`. Başlatmada yazılır, çıkışta silinir. Panel açılışında:
- Kayıtlı PID hâlâ canlı ve create_time eşleşiyor → **"Sahiplenildi"** (`ADOPTED`): log akışı yok (bilgi bandı),
  durum/port/CPU izlenir, durdurulabilir (create_time doğrulamalı). Windows'ta Job Object nedeniyle normalde
  süreç kalmaz; kalırsa aynı yol.
- Canlı değil → ilgili `runs` kaydı `end_reason=panel_restart` ile kapatılır.

**6. Harici süreç tespiti (Senaryo F, plan M1)** — UI bağlıyken 5 sn'de bir `psutil.process_iter(['pid','name',
'cmdline','cwd','create_time'])`: adı python/uv/uvicorn/streamlit olan, `cwd` veya cmdline yolu bir proje
klasörünün içinde olan ve yönetilen ağaçta olmayan süreçler → projenin satırında **"⇄ Harici çalışıyor"**
(PID, port, komut). Eylemler: "Durdur" (açık onay + komut satırı gösterimi + denetim kaydı; başka kullanıcının/
yönetici sürecine asla), "Durdur ve panelden başlat". Erişim reddedilen süreçler sessizce atlanır.

**7. Bildirimler (Senaryo G)** — `notifications` tablosu + üst çubukta 🔔 bildirim merkezi (okunmamış sayısı).
Tarayıcı **Notification API** (kullanıcı izniyle; sekme arka plandayken de çıkar). Proje bazında sessize alma.
İçerik: proje adı + neden + istisna tipi (log metni yok — sızıntı önlemi). Windows toast/tepsi Faz 2'de kalır.

**8. Olay zaman çizelgesi** — `events(project_id, ts, kind, detail)`; Geçmiş sekmesinde çalıştırmalar
"neden" sütunu (Kullanıcı durdurdu / Dışarıdan kapatıldı / Öldürüldü / Çöktü / Yetim / Otomatik yeniden başlatıldı).

### Arayüz değişiklikleri (ekip — ön yüz)
- Yeni durum rozetleri: `Yanıt vermiyor` (sarı), `Dışarıdan kapatıldı` (gri-mavi), `Öldürüldü` (kırmızı),
  `Yetim süreç` (turuncu, uyarı ikonlu), `Yeniden başlatılıyor… Ns` (sarı, geri sayım), `Çökme döngüsü` (kırmızı),
  `Sahiplenildi` (mavi), `Harici` (mavi). Rozet tooltip'i: neden + zaman.
- Satır altında tek satır neden: "14:02'de dışarıdan kapatıldı (SIGTERM)" / "İşçi süreç sonlandı, :8000 kapalı".
- Yetim/harici durumda satırda eylem düğmeleri: "Yetimleri sonlandır", "Devral".
- Yapılandırma sekmesine **"Gözetim"** bölümü: sağlık kontrolü tipi/hedef, yeniden başlatma politikası, deneme
  sınırı, sessize al. Kategori yöneticisinde kategori varsayılanları (restart policy, bildirim).
- Genel sekmesinde sağlık zaman çizelgesi (son 20 kontrol noktası), son çıkış nedeni.
- 🔔 bildirim merkezi açılır paneli + ilk girişte "Masaüstü bildirimlerine izin ver" önerisi.

### Güvenlik (ekip — güvenlik)
- Sonlandırma yalnızca PID+create_time eşleşen, **bizim başlattığımız ağaca** ait süreçlere; harici süreç
  durdurma ayrı uç nokta, açık onay token'ı (`confirm: pid:create_time`) ve denetim kaydı ister; süreç sahibi
  panel kullanıcısı değilse reddedilir.
- Sağlık HTTP hedefi yalnız loopback; yönlendirme takip edilmez; zaman aşımı ≤5 sn.
- Otomatik yeniden başlatma DoS'a karşı backoff + crash-loop sınırı.
- Bildirim gövdesinde log içeriği yok.

---

## WP2 — Kalıcı ve Taşınabilir Yapılandırma

### Tasarım
**1. Veri konumu güncellemeden bağımsız** — veri zaten uygulama klasörünün dışında (`%LOCALAPPDATA%\Kovan`);
`.exe`/paket değişse de dokunulmaz. Ek: **taşınabilir mod** — `.exe` yanında `kovan.portable` dosyası veya
`data/` klasörü varsa veri orada tutulur (USB/klasör kopyalama ile taşınır). `config.py: default_data_dir()`
bu kuralı uygular; Ayarlar > Tanılama'da aktif veri yolu gösterilir.

**2. Şema sürümleme + migrasyon (`infrastructure/migrations.py`)**
- `settings.schema_version` (int). `MIGRATIONS: list[(version, fn(conn))]` sıralı uygulanır, her biri tek
  transaction. Mevcut kurulumlar (sürüm yok) v1 kabul edilir → v2: `uid` alanları, `runs.end_reason/signal`,
  `live_processes`, `events`, `notifications` tabloları.
- **Migrasyondan hemen önce otomatik yedek** (`sqlite3.Connection.backup`) → `backups/pre-migrate-v1-to-v2-<ts>.db`.
  Hata olursa transaction geri alınır, yedek korunur, panel açık hata sayfası gösterir (veri değişmez).
- **Sürüm düşürme koruması**: DB şeması uygulamadan yeniyse panel salt-okunur "Bu veri daha yeni bir Kovan
  sürümüyle oluşturulmuş" ekranı açar; veriye yazmaz.
- Kayıt düzeyinde dayanıklılık: domain modellerine `model_config = ConfigDict(extra="ignore")`; JSON kayıtlarına
  `"_v"` alanı; okunamayan kayıt **silinmez**, `quarantine` tablosuna ham JSON ile taşınır ve UI'da uyarı
  gösterilir ("1 proje kaydı okunamadı — yedekten geri yükleyin veya dışa aktarın").

**3. Kararlı kimlikler** — `Project.uid`, `Category.uid` (uuid4); migrasyonda mevcut kayıtlara atanır. İçe
aktarmada eşleştirme önce `uid`, sonra normalleştirilmiş yol ile yapılır.

**4. Otomatik yedekleme (`application/backup_service.py`)**
- Günlük (açılışta, son yedek >24 sa ise), migrasyon öncesi, içe aktarma/geri yükleme öncesi.
- Saklama: son 10 otomatik + elle alınanlar (silinmez). `backups/` klasörü, kullanıcı ACL'i.
- Her değişiklikte (2 sn debounce) **insan-okunur ayna**: `config-latest.kovan.json` (dışa aktarma formatında,
  gizliler hariç). DB bozulsa bile yapılandırma bu dosyadan geri yüklenebilir.
- Açılışta `PRAGMA integrity_check`; bozuksa son sağlam yedekten otomatik geri yükleme önerisi.

**5. Taşınabilir dışa/içe aktarma formatı (`application/portability.py`)** — JSON (yeni bağımlılık yok, YAML yerine
güvenli ve deterministik):
```json
{ "format": "kovan-config", "format_version": 1, "app_version": "0.2.0", "exported_at": "...",
  "source": {"host": "<makine adı>", "os": "nt"},
  "roots": [{"id": "r1", "path": "D:\\code"}],
  "settings": {"port_range": [8000, 8999], "metrics_enabled": false},
  "categories": [{"uid": "...", "name": "Web API", "color": "#4f46e5", "parent_uid": null, "sort": 0, "defaults": {}}],
  "projects": [{"uid": "...", "name": "order-api", "root": "r1", "rel_path": "order-api",
                "category_uid": "...", "tags": ["shop"], "favorite": true, "notes": "...",
                "profiles": [ ... env değerleri: gizliler "secret_omitted": true ... ]}] }
```
- **Yol taşınabilirliği**: proje yolu kök klasöre göre göreli saklanır (`root` + `rel_path`); içe aktarmada
  **kök eşleme** adımı: `D:\code → C:\work\code`. Eşlenen yolda klasör yoksa proje "Klasör bulunamadı" uyarısıyla
  yine de içe aktarılabilir (yapılandırma kaybolmasın) veya atlanır.
- **Gizli değerler**: varsayılan hariç. İsteğe bağlı "Gizlileri dahil et": kullanıcı parolasıyla
  **scrypt + AES-256-GCM** şifreli `secrets` bloğu (`cryptography` paketi). İçe aktarmada parola sorulur.
- **İçe aktarma akışı** (önizleme zorunlu): dosya → şema doğrulama (pydantic, boyut ≤5 MB, alan sınırları) →
  kök eşleme → **fark önizlemesi** (eklenecek / güncellenecek / değişmeyecek / çakışan; her profil için komut
  önizlemesi, çünkü dosya komut içerir = güvenilmeyen girdi) → mod seçimi **Birleştir** (uid/yol eşleşeni
  güncelle, yenileri ekle) veya **Değiştir** (önce yedek, sonra tümünü değiştir) → uygula + denetim kaydı.
- Eski format sürümleri için `format_version` dönüştürücüleri (ileri uyumluluk).

**6. CLI eşlikçisi (kısmi FR38)** — `kovan export -o dosya.json [--include-secrets]`,
`kovan import dosya.json --map "D:\code=C:\work" [--mode merge|replace] [--dry-run]`,
`kovan backup`, `kovan restore <yedek>`, `kovan doctor` (veri yolu, şema, bütünlük, yedekler).
Panel kapalıyken doğrudan DB üzerinde çalışır; açıkken kilit dosyası ile reddeder ("Önce paneli kapatın").

### Arayüz (ekip — ön yüz)
Ayarlar penceresi sekmelere bölünür: **Genel** (kökler, port aralığı) · **Yedekler** (liste: tarih, tür,
boyut, sürüm; "Şimdi yedekle", "Geri yükle" (önce mevcut durumun yedeği alınır), "Klasörü göster") ·
**Taşı / Aktar** (Dışa aktar: gizli dahil et seçeneği + parola; İçe aktar: dosya seç → kök eşleme tablosu →
fark önizlemesi → mod → uygula) · **Tanılama** (sürüm, veri yolu, taşınabilir mod, şema sürümü, DB boyutu,
bütünlük, karantinadaki kayıtlar) · **Denetim kaydı**.

### Güvenlik (ekip — güvenlik)
İçe aktarılan dosya güvenilmeyen girdi: yalnız `json.loads` + pydantic şema; komutlar önizlemede gösterilir,
uygulama sonrası hiçbir proje otomatik başlatılmaz; kök eşleme sonrası tüm yollar izinli kök denetiminden geçer
(`domain/paths.resolve_within`). Gizli şifreleme: scrypt (n=2^15) + AES-GCM, rastgele salt/nonce; yanlış parola =
açık hata, kısmi içe aktarma yok. Yedek ve ayna dosyalarında gizli değer düz metin bulunmaz. Geri yükleme/içe
aktarma CSRF + yeniden parola doğrulaması (re-auth) ister.

---

## Ekip iş bölümü ve sıra
1. **Backend — veri katmanı (WP2 temel, önce çünkü WP1 yeni tablolar gerektiriyor):** `migrations.py`,
   `db.py` (schema_version, quarantine, uid), modellerde `extra="ignore"`, `backup_service.py`, taşınabilir mod.
2. **Backend — süreç gözetimi (WP1):** `supervisor.py` ağaç takibi, `watchdog.py`, çıkış sınıflandırması,
   sağlık kontrolü, restart politikası, `live_processes` sahiplenme, harici süreç tespiti, bildirim/olay tabloları.
3. **Backend — taşınabilirlik (WP2):** `portability.py` (export/import/diff/kök eşleme/şifreleme), API uçları,
   `cli.py` alt komutları.
4. **Ön yüz:** yeni durum rozetleri + neden satırı, Gözetim bölümü, bildirim merkezi + Notification API,
   Ayarlar sekmeleri (Yedekler, Taşı/Aktar, Tanılama).
5. **Güvenlik incelemesi:** harici süreç durdurma, içe aktarma, şifreleme, re-auth; bağımsız kod denetimi.
6. **QA:** aşağıdaki test matrisi + Playwright senaryosu; `docs/TASARIM.md` v4 bölümü, README güncellemesi.

## Kritik dosyalar
- Değişecek: `src/kovan/infrastructure/supervisor.py`, `infrastructure/db.py`, `infrastructure/os_adapters.py`,
  `infrastructure/inspector.py`, `application/run_service.py`, `domain/models.py`, `config.py`, `main.py`,
  `web/app.py`, `web/security.py` (re-auth), `web/static/app.js`, `web/static/app.css`.
- Yeni: `application/watchdog.py`, `application/health.py`, `application/external_processes.py`,
  `application/backup_service.py`, `application/portability.py`, `infrastructure/migrations.py`,
  `infrastructure/crypto.py`, `src/kovan/cli.py`.
- Yeniden kullanılacaklar: `os_adapters.terminate_tree` (create_time doğrulaması), `inspector.listening_map/
  ports_for/tree_pids`, `domain/paths.resolve_within/resolve_child`, `RunService.prepare` (içe aktarma önizlemesinde
  komut önizlemesi), `logstore.ProjectLog.errors` (crashed/killed ayrımı), `EventBus.publish`.
- Yeni bağımlılık: `cryptography` (yalnız gizli şifreleme için).

## Doğrulama
**Birim:** çıkış sınıflandırma tablosu (rc × stopping × traceback × platform); restart backoff ve crash-loop
penceresi; migrasyon v1→v2 (gerçek v0.1 DB fixture'ı ile — mevcut commit'ten üretilen DB), sürüm düşürme
koruması, karantina; export→import gidiş-dönüş eşitliği; kök eşleme; gizli şifreleme (yanlış parola reddi);
bozuk/aşırı büyük/şema dışı import reddi.

**Entegrasyon (gerçek süreçlerle, fixture projeler):**
- `uvicorn --reload` işçisini dışarıdan `kill -9` → ≤5 sn içinde `UNHEALTHY`/"İşçi süreç sonlandı".
- Kök süreci öldür, işçi yaşasın → `ORPHANED`, yetim PID listesi, "Yetimleri sonlandır" sonrası port boşalır.
- Dışarıdan SIGTERM (rc 0) → `STOPPED_EXTERNALLY` + bildirim kaydı; SIGKILL → `CRASHED/killed`.
- `crasher` + `on-failure` → backoff ile yeniden başlatma, 5 denemede `CRASH_LOOP`.
- Donan süreç fixture'ı (`time.sleep` ile portu açık ama yanıt vermeyen HTTP) → HTTP sağlık kontrolüyle `UNHEALTHY`.
- Panel süreç çalışırken yeniden başlatılır (Linux) → süreç `ADOPTED`, durdurulabilir; ölü kayıt `panel_restart`.
- Panel dışından başlatılan `python bot.py` → "Harici çalışıyor"; durdurma onaysız reddedilir.
- v0.1 veritabanıyla v0.2 açılır → kategoriler/etiketler/notlar/profiller korunur, `pre-migrate` yedeği oluşur.
- Export → farklı kök ile yeni veri dizinine import → tüm projeler eşlenmiş yollarla gelir.

**Güvenlik:** harici süreç durdurmada CSRF/onay token'ı/re-auth eksikse 403; import dosyasındaki `cwd: ../..`,
kök dışı yol, `\x00` içeren argüman reddedilir; yedek/ayna dosyalarında gizli değer aranır ve bulunmaz.

**E2E (Playwright):** proje başlat → terminalden `kill` → rozet ve bildirim merkezi güncellenir → otomatik
yeniden başlatma görülür; Ayarlar > Taşı/Aktar ile dışa aktar → içe aktar önizlemesi → uygula. Ekran görüntüleri
`docs/img/` altına; tüm sürecin sonunda güncel zip kullanıcıya gönderilir.

**Platform notu:** Windows'a özgü yollar (CTRL_BREAK, TerminateProcess rc=1 sınıflandırması, venv başlatıcı ağacı,
Job Object) bu Linux ortamında birim testlerle (mock) doğrulanır; gerçek Windows testi kullanıcı tarafında
`pytest` ile yapılmalıdır — README'ye Windows test adımları eklenir.

## Uygulama sırasında öğrenilenler (v4)
- **uvicorn `--reload` dinleme soketini ebeveyn süreçte tutar.** İşçi ölse bile TCP bağlantısı kabul edilir; bu
  yüzden web launcher'larında (uvicorn/flask/django/streamlit) otomatik sağlık kontrolü **HTTP** olarak yapılır.
- **Öldürülen işçiler zombi olarak kalabilir.** `psutil.children()` zombileri de döndürdüğü için ağaç takibinde
  zombi süreçler ölü sayılır.
- **Kayıp işçi bilgisi biriktirilir.** Sağlık kontrolü zamanı gelene kadar kaybolmaması için "kayıp işçi" bilgisi
  raporlanana kadar saklanır; "işçi öldü + yanıt yok" durumu ilk başarısız kontrolde kesin kabul edilir (< 10 sn).
- **uvicorn bazı durumlarda işçiyi kendisi yeniden başlatır.** Uygulama toparlanırsa durum "Çalışıyor" kalır ve
  "İşçi süreç yeniden başladı" olayı kaydedilir.
- **SQLite `executescript()` açık transaction'ı commit eder.** Migrasyonlar atomik kalsın diye ifadeler tek tek çalıştırılır.

## Güvenlik incelemesi (v4) ve giderilen bulgular
Bağımsız güvenlik denetimi sonrasında düzeltilenler (her biri için regresyon testi vardır):

| Önem | Bulgu | Düzeltme |
|---|---|---|
| Yüksek | Çalışan projeye "birleştir" ile içe aktarılan dosya, otomatik yeniden başlatma yoluyla yeni komutu onaysız çalıştırabiliyordu | Çalışan / yeniden başlatma bekleyen projeler güncellenmez (409); dosyadaki yeniden başlatma ayarları varsayılan olarak yok sayılır (`keep_watch` ile açık onay); otomatik yeniden başlatma, kullanıcının başlattığı andaki profili kullanır |
| Orta | Windows'ta içe aktarma önizlemesi UNC yoluna dokunarak NTLM özeti sızdırabilirdi | `rel_path` hem Windows hem POSIX kurallarıyla doğrulanır; UNC yollar reddedilir; dosya sistemine dokunmadan önce metinsel kök denetimi |
| Orta | Önizleme yorumlayıcı, dizin ve ortam değişkenlerini göstermiyordu | Önizlemede yorumlayıcı, dizin, env (gizliler maskeli), yeniden başlatma politikası ve tehlikeli değişken uyarısı (`LD_*`, `PYTHON*`, `PATH`, özel yorumlayıcı) |
| Düşük | Geri yükleme / "Değiştir" yetim süreçleri takipten düşürebiliyordu | Tüm projeler gerçekten boşta olmalı; `live_processes` temizlenir |
| Düşük | Doğrulanamayan PID'e süreç grubu sinyali gidebiliyordu | `psutil` doğrulaması başarısızsa hiçbir sinyal gönderilmez |
| Düşük | Yedek dosyası parola/tetikleyici taşıyabilirdi | Geri yüklemede mevcut parola korunur, tetikleyici ve görünümler kaldırılır |
| Düşük | Bozuk/derin/yinelenen içerik 500 hatası veya yarım içe aktarma | Şema düzeyinde tekillik, port aralığı, kategori derinliği; `RecursionError` yakalanır; birleştirme tek transaction |
| Düşük | CLI dışa aktarma dosya izinleri, kilit yarışı | Dosya 0600; kilit `O_EXCL` ile atomik |
