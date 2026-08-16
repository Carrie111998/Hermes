# Yerel Kişisel Finans Analiz Sistemi — İlk Sürüm

Bu klasör, üç bankadan gelen kredi kartı/hesap özeti belgelerini **yerel cihazda** işlemek için temel altyapıyı içerir. Gmail OAuth, yalnızca PDF attachment indiren bir Document Source olarak ingestion katmanına bağlanır; parser ve DuckDB mantığı ortak pipeline'da kalır.

## Mevcut kapsam

- Python paket yapısı ve ortak `Statement` / `Transaction` modelleri
- Tam kart numarası saklamayı reddeden veri modeli; yalnızca maskeli son dört hane
- DuckDB şeması: bankalar, ekstreler, işlemler, merchant kuralları, raporlar ve işleme logu
- Ekstre hash'i ve işlem fingerprint'i ile duplicate koruması
- Üç ayrı banka için YAML tabanlı yapılandırma
- Merchant normalizasyonu ve deterministik kategori kuralları
- Banka ücretleri, faiz ve vergi ifadeleri için ayrı tespit katmanı
- Bankaya özel parser'lar için açık interface ve registry
- İlk gerçek parser: İş Bankası Maximum PDF (metin tabanlı, OCR gerektirmez)
- İkinci gerçek parser: Akbank Axess Platinum PDF (iki sayfalı tablo, yerel OCR fallback)
- Üçüncü gerçek parser: Enpara.com kredi kartı ekstresi (tek sayfalı, text layer)
- Kaynak bağımsız ingestion servisi, batch inbox akışı ve güvenli `.gitignore`

## Kurulum (Ubuntu)

```bash
cd finance-assistant
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
python -m app.main init
```

`uv` yoksa Ubuntu'da Python virtual environment oluşturup `pip install -r requirements.txt` kullanılabilir. Gerçek banka adresleri `config/banks.yaml` içine kullanıcı tarafından yazılmalıdır; kod içinde hardcode edilmez.

## Komutlar

```bash
python -m app.main init
python -m app.main gmail-auth
python -m app.main gmail-sync --dry-run
python -m app.main sync       # sonraki faz: rapor/senkronizasyon işlemleri
python -m app.main parse /path/to/statement.pdf
python -m app.main ingest                 # data/inbox altındaki PDF'ler
python -m app.main ingest /custom/folder  # özel klasör
python -m app.main inspect-pdf /path/to/statement.pdf
python -m app.main report --month 2026-08 --output-dir data/reports
python -m app.main dashboard  # Streamlit dashboard; read-only
```

İlk `init` çalıştırması `data/` altında yerel klasörleri ve DuckDB dosyasını oluşturur. Bu veriler Git'e alınmaz.

## Yerel rapor ve dashboard

`report`, yalnızca `AnalysisService` toplulaştırmalarından deterministik bir CSV
ve HTML dosyası üretir. Raporlarda merchant açıklaması, kart bilgisi, sır/secret
ve işlem kimliği bulunmaz. Çıktı klasörü yoksa oluşturulur; dosya yolu olarak
verilen `--output-dir` reddedilir:

```bash
uv run python -m app.main report --month 2026-08 --output-dir data/reports
# data/reports/2026-08.csv
# data/reports/2026-08.html
```

Dashboard `streamlit run app/dashboard.py` üzerinden açılır ve analiz servisini
salt-okunur kullanır. Ay seçici, KPI'lar, banka/kategori grafikleri, altı aylık
trend, ekstre bütünlük uyarıları (LEGACY_UNVERIFIED dahil) ve kategorisiz işlem
sayısını gösterir; tekil işlem detayları gizlenir. Dashboard merchant kuralı
veya başka bir veritabanı yazımı yapmaz.

## Kaynak bağımsız ingestion workflow'u

PDF'yi `data/inbox/` klasörüne bırakıp tek pipeline ile işleyin:

```text
PDF source → SHA-256 → parser registry → metadata/transactions → validation → DuckDB
```

```bash
uv run python -m app.main ingest
```

`parse /path/to/file.pdf` de aynı `IngestionService`'i kullanır. Başarılı belgeler
`data/archive/<yıl>/<banka>/` altında SHA prefix'i içeren güvenli adla saklanır.
Tanınmayan PDF'ler `data/failed/unsupported/`, tanınan bankanın format hataları
`data/failed/format_error/`, diğer işlem hataları ise `data/failed/processing_error/`
altına taşınır. Exact hash duplicate'leri yeniden parse edilmeden
`data/archive/duplicates/` altına taşınır.

Üç duplicate katmanı korunur: exact PDF SHA-256, banka/dönem/kart metadata'sına
dayalı statement duplicate ve transaction fingerprint duplicate. Statement ile
transaction insert'leri ve başarılı processing log kaydı tek DuckDB transaction'ı
içindedir; hata halinde rollback yapılır. İşlem logu hassas dosya adını değil,
`document-<sha-prefix>.pdf` güvenli adını tutar.

Gmail, yalnızca aynı servise `GMAIL` source değeriyle bağlanan bir Document
Source implementasyonudur. OAuth, mailbox araması, PDF attachment indirme ve
temporary dosya yönetimi Gmail adapter'da; parser, duplicate ve DuckDB işlemleri
`IngestionService` içindedir.

## İş Bankası Maximum parserı

Parser, banka adı veya dosya adı yerine PDF içindeki `isbank.com.tr`, `maximum` ve
`Hesap Özetiniz` anchor'larını kullanır. İşlem tablosu, İş Bankası'nın
`İŞLEM TARİHİ / AÇIKLAMA / TUTAR` başlığından başlayarak okunur; açıklama satır
sonuna taşarsa continuation satırları birleştirilir. Türkçe tutarlar `1.234,56`
biçiminde `Decimal` olarak normalize edilir.

Gerçek dosyayı repository'ye kopyalamadan lokal çalıştırma:

```bash
uv run python -m app.main inspect-pdf /path/to/statement.pdf
uv run python -m app.main parse /path/to/statement.pdf
```

İkinci çalıştırmada ekstre SHA-256 ve işlem fingerprint'leri üzerinden duplicate
koruması uygulanır. Parser biçim anchor'larını bulamazsa sessizce boş sonuç
üretmek yerine `ParserFormatError` verir. Ekstre toplamı ile satır toplamı,
önceki bakiye ve ödeme gibi kalemler nedeniyle doğrudan uyuşmayabileceğinden
CLI farkı ve uyarıyı ayrıca gösterir.

## Akbank Axess parserı

Axess parserı dosya adına güvenmez; PDF içindeki Axess/Akbank, hesap özeti ve
işlem tablosu anchor'larını kullanır. Gerçek Axess PDF'si standart olmayan Type3
fontlar kullandığı için okunabilir metin katmanı yoksa PDF sayfaları yerel
`pypdfium2` + `rapidocr-onnxruntime` ile işlenir. Belge üçüncü taraf bir servise
gönderilmez.

Parser iki sayfadaki tekrarlanan işlem başlıklarını ve ilk sayfa ara toplamını
ayırır; yalnızca `Genel Toplam` satırında durur. Axess'in `toplam/mevcut taksit`
gösterimini ortak modele `installment_current` / `installment_total` olarak
çevirir. Bu belgede ödeme, otomatik fatura ödeme faizi ve üç taksitli işlem
örneği bulunur; parser bunları sırasıyla `PAYMENT`, `INTEREST` ve `INSTALLMENT`
olarak normalize eder.

## Test

```bash
uv run --with pytest --with duckdb --with pyyaml pytest tests -q
```

## Enpara.com parserı

Enpara parserı dosya adına güvenmez; `Enpara`, kredi kartı ekstresi, ekstre
borcu ve işlem tablosu başlıklarını birlikte arar. Gerçek örnekte PDF tek
sayfalı, şifresiz ve kullanılabilir text layer'a sahipti; OCR gerekmedi.
Ödeme satırları negatif tutar ve `PAYMENT`, alışverişler pozitif tutar ve
`PURCHASE` olarak normalize edilir. Fiziksel kartın yanı sıra sanal kart
başlığı görülürse işlem bazında yalnızca maskeli son dört hane tutulur.
Ekstre dönemi, Enpara belgesindeki kesim tarihine göre bir önceki aylık
dönemin ertesi gününden kesim gününe türetilir. Önceki ekstre bakiyesi işlem
satırı değildir; bu nedenle CLI doğrudan satır toplamı ile ekstre borcu
arasında fark varsa uyarı verir, toplamı zorla eşitlemez.

## Gizlilik sınırları

- PDF, OAuth token, müşteri bilgisi ve üretilen finansal veri repository'ye eklenmez.
- İlk sürümde herhangi bir üçüncü parti AI/OCR servisine belge gönderilmez.
- LLM entegrasyonu eklenecekse yalnızca kural ile sınıflandırılamayan merchant'ın minimize edilmiş alanları gönderilecek; tam PDF, kart/hesap numarası ve kimlik bilgileri gönderilmeyecektir.
- Gmail entegrasyonu salt-okunur OAuth scope ile tasarlanacaktır.

## Gerçek belge gizliliği

İlk banka parser'ını geliştirmek için gerçek belgeyi repository'ye koymayın. Anonimleştirilmiş örnek PDF'yi sohbet/çalışma alanı üzerinden sağlayın veya hassas alanları maskeleyin. Gerekli bilgiler:

- Ekstre dönemi ve tarih alanlarının PDF'deki görünümü
- İşlem satırlarının kolon düzeni
- Tutar/işaret biçimi, iadeler ve taksit gösterimi
- Kart bilgisinin nasıl maskelendiği
- Ekstre toplamının nerede bulunduğu

Gerçek banka PDF'leri yalnızca lokal smoke testlerinde kullanılmalı; repository,
test fixture'ı veya Git geçmişine alınmamalıdır.

## Gmail entegrasyonu (salt-okunur)

Gmail adapter yalnızca e-posta arama, PDF attachment indirme ve mevcut
`IngestionService.process_file()` çağrısından sorumludur. Parser, transaction
duplicate, DuckDB ve archive mantığı Gmail kodunda tekrarlanmaz.

### Google Cloud kurulumu

1. Google Cloud Console'da bir proje oluşturun veya mevcut projeyi seçin.
2. Gmail API'yi etkinleştirin.
3. OAuth consent screen'i yapılandırın. Kişisel kullanım için test user olarak
   Gmail hesabınızı ekleyin.
4. Desktop OAuth client oluşturun ve indirilen dosyayı şu konuma, gerçek içeriği
   sohbete veya Git'e koymadan kaydedin:

```text
secrets/gmail_credentials.json
```

OAuth yalnızca şu read-only scope'u kullanır:

```text
https://www.googleapis.com/auth/gmail.readonly
```

Token otomatik olarak şu konuma ve dosya modu `0600` ile yazılır:

```text
secrets/gmail_token.json
```

İlk yetkilendirme:

```bash
uv run python -m app.main gmail-auth
```

Headless Ubuntu sunucuda komut `open_browser=False` ile lokal callback
sunucusunu başlatır. Gösterilen authorization URL'sini SSH port forwarding
ile sunucuya erişebilen bir tarayıcıda açın; örneğin lokal portu SSH ile
sunucunun callback portuna yönlendirin. OAuth client secret veya token'ı
paylaşmayın. OOB veya güvensiz plaintext OAuth akışı kullanılmaz.

### Banka Gmail kuralları

`config/banks.yaml` içindeki `gmail` bölümü sender, subject keyword ve
attachment extension kurallarını taşır. Sender değeri boş veya `[REDACTED]`
ise sistem fail-closed olur; bütün mailbox'ı tarayan fallback query yoktur.
Gerçek sender adreslerini yalnızca lokal config'e kendiniz ekleyin.

### Gmail sync

Varsayılan lookback 90 gündür. Tarih veya banka filtresi kullanılabilir:

```bash
uv run python -m app.main gmail-sync --dry-run
uv run python -m app.main gmail-sync --since 2026-07-01 --dry-run
uv run python -m app.main gmail-sync --month 2026-08 --bank enpara
uv run python -m app.main gmail-sync --since 2026-08-01
```

Gmail query server-side olarak `from`, `has:attachment`, `filename:pdf`,
subject ve tarih filtrelerini kullanır. Query banka adayı bulur; gerçek banka
kararını yine parser registry verir. Yalnızca PDF attachment'ları indirilir.
Body, inline image, CSV ve XLSX işlenmez.

Her attachment için `gmail:<message-id>:<attachment-id>` external ID'si
processing log'a kaydedilir. Bu kontrol SHA256, semantic statement duplicate
ve transaction duplicate kontrollerinin yerine geçmez; onların önünde ek bir
katmandır. Gmail mesajlarına label eklenmez, read/unread durumu değiştirilmez.

### Gmail güvenlik notları

- `secrets/`, Gmail token/credential dosyaları, lokal temporary PDF'ler ve
  finansal PDF'ler `.gitignore` kapsamındadır.
- Attachment temporary dosyası ingestion tamamlanınca `archive` veya `failed`
  workflow'una devredilir ve geçici klasörden silinir.
- Varsayılan çıktıda sender, tam subject, attachment adı ve e-posta body'si
  yazdırılmaz.
- Gmail body database'e kaydedilmez ve PDF üçüncü parti AI servisine gönderilmez.
- OAuth token yenilenemezse, credentials eksikse veya Gmail API 429/5xx sonrası
  sınırlı retry başarısız olursa komut güvenli hata ile sonlanır.
