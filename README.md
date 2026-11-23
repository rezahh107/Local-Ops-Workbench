# Local Ops Workbench — Foundation خصوصی (CANDIDATE)

این مخزن، **کد جداشده از داده‌های عملیاتی** داشبورد محلی را نگه می‌دارد. نسخه مبنا ZIP خصوصی v1.6.0 است (SHA-256: `3f092528ee612827eb83734e32964f2a025cf99b0179e6db08b0df27b6fc8e51`). این Foundation هنوز نسخه Production، Merge‌شده یا تأییدشده روی Windows نیست.

## معماری واقعی

- `server.py`: وب‌سرور محلی مبتنی بر Python standard library؛ خواندن مستقیم XLSX، API محلی و نگهداری تنظیمات/Revision/Freeze در SQLite. به Excel چیزی نمی‌نویسد.
- `index.template.html`: رابط HTML/JS فارسی RTL؛ داده اولیه از `/` با رندر امن سرور تزریق می‌شود، سپس وضعیت معتبر از `/api/state` دریافت می‌شود. کد تاریخی و استثناهای فردی در مخزن نگهداری نمی‌شوند.
- `start_with_uv.bat` و `start_uv_offline.bat`: اجرای Windows از طریق uv و Python 3.12، بدون EXE. برای اجرای عادی وابستگی Python ثالث وجود ندارد.
- `tools/` و `tests/`: تست‌های Python/SQLite/API، داده XLSX مصنوعی، Browser Lab، Privacy Gate و بسته‌بندی با manifest.

**تغییر فنی ضروری نسبت به ZIP تاریخی:** `dashboard.sqlite3`، Excelهای واقعی و `config.private.json` اکنون خارج از پوشه کد هستند. این جداسازی برای جلوگیری از جایگزینی ناخواسته داده‌های Owner هنگام Upgrade و جلوگیری از انتشار اطلاعات شخصی لازم است؛ قواعد امتیازدهی، `Freeze` و `weightsOnly` نباید عوض شوند.

## راه‌اندازی Windows با داده مالک

۱. نسخه جاری و فایل SQLite واقعی را **ببندید و جداگانه پشتیبان بگیرید**. از ZIP تاریخی، SQLite قدیمی را روی SQLite جدیدتر ننویسید.

۲. پوشه داده خارجی به صورت پیش‌فرض `%LOCALAPPDATA%\LocalOpsWorkbench` است؛ در صورت نیاز `LOCAL_OPS_DATA_DIR` را به یک مسیر خصوصی دیگر تنظیم کنید. پوشه برنامه و پوشه داده باید جدا باشند.

۳. در همان کامپیوتر خصوصی، با اصل ZIP مطابقت‌داده‌شده، پیکربندی خصوصی را **بدون جایگزینی فایل موجود** بسازید:

```powershell
uv run --no-project --python 3.12 tools/private_config_from_baseline.py "C:\private\baseline.zip" --data-dir "$env:LOCALAPPDATA\LocalOpsWorkbench"
# پس از بازبینی خروجی Dry-run:
uv run --no-project --python 3.12 tools/private_config_from_baseline.py "C:\private\baseline.zip" --data-dir "$env:LOCALAPPDATA\LocalOpsWorkbench" --write
```

سازوکار امنیتی: فایل موقت از لحظهٔ ایجاد با محدودیت دسترسی ساخته می‌شود (POSIX: `0600`؛ Windows: DACL محافظت‌شده برای کاربر اجراکننده و SYSTEM). تنها پس از نوشتن کامل، اعتبارسنجی و بستن فایل، با Hard Link بدون قابلیت جایگزینی به مسیر نهایی منتقل می‌شود. خطا، قطع نوشتن و رقابت دو فرایند باعث بازنویسی فایل موجود نمی‌شوند؛ خطاها بدون چاپ داده خصوصی گزارش می‌شوند. آزمون واقعی ACL روی Windows/NTFS برای تأیید استفاده عملی ضروری است.

این دستور فقط `config.private.json` را می‌سازد؛ Excel و SQLite را کپی یا تغییر نمی‌دهد. **این فایل را به GitHub، CI یا گزارش عمومی ندهید.** اگر استثنای خصوصی کتاب در پیکربندی موجود نباشد، UI منتخب کتاب را اعلام نمی‌کند و پیغام محدودیت نمایش می‌دهد؛ هیچ نام واقعی به‌عنوان مقدار پیش‌فرض در سورس وجود ندارد.

۴. Excel اصلی و Excel پشتیبانان را از ZIP تاریخی در پوشه داده خصوصی قرار دهید. فایل SQLite معتبر جاری را فقط پس از تشخیص نسخه/پشتیبان‌گیری به محل خارجی منتقل کنید. برای کاربر جدید، درصورت نبود SQLite، برنامه دیتابیس خالی برای همین دوره می‌سازد؛ آن را با وضعیت سابق اشتباه نگیرید.

۵. از پوشه کد، `start_with_uv.bat` را اجرا کنید و `http://127.0.0.1:8765/` را باز کنید. `index.template.html` نباید مستقیم با `file://` باز شود. برای تغییر workbook/sheet، ورودی خط فرمان سرور موجود است.

**محدودیت:** این جداسازی با داده مصنوعی آزموده شده؛ انتقال پرونده واقعی Owner یا پذیرش اجرای Windows هنوز انجام نشده است.

## راستی‌آزمایی محلی و CI

```bash
python tools/verify.py            # تست واقعی Python/SQLite/API، Privacy و ZIP smoke
python tools/verify.py --browser  # علاوه بر موارد بالا Chromium واقعی؛ روی محدودیت Browser خطا می‌دهد
```

برای محیط‌های Linux که Chromium دارند اما سیاست میزبان ارتباط مستقیم browser→localhost را مسدود می‌کند، یک آزمون محدود **رندر و تعامل واقعی مرورگر با واسط HTTP مصنوعی** نیز فراهم است:

```bash
LOCAL_OPS_BROWSER_EXECUTABLE=/usr/bin/chromium python tests/browser_contract.py --loopback-proxy
```

این حالت با درخواست واقعی Python به سرور مصنوعی متصل می‌شود، اما **معادل آزمون شبکه native مرورگر نیست**؛ CI باید همان مسیر اصلی `--browser` را اجرا کند. در این مسیر، تعارض مصنوعی `409` نیز باید خطا و عدم ثبت را در UI نشان دهد.

`tools/verify.py` قرارداد مشترک توسعه‌دهنده، AI agent و GitHub Actions است. CI خصوصی فقط پس از تأیید بودجه و حقوق اجرای Actions فعال شود؛ PR CI به خودی خود معنی تأیید Windows/Edge/پروژکتور نیست. `TEST_NOT_RUN` و `NOT_PROVEN` هیچ‌وقت PASS محسوب نمی‌شوند.

## حریم خصوصی و تاریخچه Git

فهرست محدود مسیرهای قابل‌ورود داخل `tools/privacy_gate.py` است. فایل‌های `*.xlsx`، `*.sqlite3`، `*.zip`، اسکرین‌شات‌ها، snapshotها، فایل‌های پشتیبان، لاگ و `config.private.json` نباید وارد تاریخچه شوند. `.gitignore` فقط یک لایه است و جای اجرای Privacy Gate را نمی‌گیرد.

برای بررسی یک شاخه قبل از Push پس از checkout واقعی مخزن:

```bash
python tools/privacy_gate.py --base <verified-main-sha> --private-baseline /private/original-baseline.zip
```

این بررسی **قبل از Push** نیازمند ZIP اصیل محلی با SHA معتبر است تا نام‌های خصوصی تأییدشده در حافظه بارگذاری و روی سورس، Staging و تمام Commitهای جدید بررسی شوند؛ هیچ نامی در گزارش نمایش داده نمی‌شود. اگر ZIP/احراز اصالت در دسترس نباشد، دستور `--base` fail-closed می‌شود. فایل ممنوع یا نام خصوصی که در یک Commit میانی اضافه و بعد حذف شده باشد نیز رد می‌شود. برای Source ZIP نیز گزینهٔ اختیاری `--private-baseline` در `--output` و `--validate` همان بررسی نام‌ها را انجام می‌دهد. CI بدون دسترسی به ZIP خصوصی صرفاً قرارداد ساختار/allowlist را احراز می‌کند و جای Gate محلیِ قبل از Push را نمی‌گیرد. این کنترل، اسکن تضمینی برای تمام داده‌های شخصی ناشناخته نیست؛ Code Review و بررسی محتوای واقعی همچنان لازم‌اند.

## بسته کاندید، انتشار و بازیابی

```bash
python tools/package_candidate.py --output ../Local-Ops-Workbench-CANDIDATE.zip
python tools/package_candidate.py --validate ../Local-Ops-Workbench-CANDIDATE.zip
# برای احراز هویت‌های خصوصی شناخته‌شده پیش از تحویل واقعی، ZIP مرجع را فقط روی دستگاه خصوصی بدهید:
python tools/package_candidate.py --validate ../Local-Ops-Workbench-CANDIDATE.zip --private-baseline /private/original-baseline.zip
python tools/package_candidate.py --validate ../Local-Ops-Workbench-CANDIDATE.zip --extract-source ../fresh-source
python tools/privacy_gate.py --source ../fresh-source
python tools/smoke_candidate.py ../Local-Ops-Workbench-CANDIDATE.zip
```

**قرارداد دقیق Manifest/Extraction:** فقط بستهٔ ZIP شامل `META/manifest.json` است؛ این مسیر جزو سورس یا Git نیست و هرگز نباید به درخت سورس انتقال داده شود. علاوه بر تطبیق `source[]`، هر **۱۹ مسیر الزامی** تعریف‌شده در `ALLOWED` فایل `tools/privacy_gate.py` باید مستقل از Manifest در سورس، ZIP و استخراج موجود باشند؛ `tools/package_candidate.py` در Build و Qualification این شرط را کنترل می‌کند. Manifest بازتولیدشده برای آرشیو ناقص، آن را معتبر نمی‌کند. فهرست `source[]` باید با محتویات ZIP منهای `META/manifest.json` دقیقاً برابر باشد و SHA-256 و اندازهٔ تک‌تک فایل‌ها کنترل می‌شوند. دستور `--extract-source` نخست آرشیو را اعتبارسنجی می‌کند، فقط فایل‌های مجاز سورس را در **دایرکتوری جدید و ناموجود** استخراج می‌کند و در صورت وجود مقصد، به‌جای جایگزینی محتوا خطا می‌دهد. استخراج دستی کل ZIP با `unzip`/`extractall` برای آماده‌سازی Git مطابق این قرارداد **نیست**. بسته‌بندی خروجی جدید را بدون بازنویسی آرشیو قدیمی می‌سازد و از داده عملیاتی بیرونی استفاده می‌کند. Smoketest واقعی باید از استخراج تمیز با Excel/SQLite مصنوعی و Upgrade بدون جایگزینی داده اجرا شود.

**نشر Production، Tag و GitHub Release در این کاندید فعال نشده‌اند.** Merge مجوز انتشار نیست. قبل از انتشار باید سازوکار احراز اجازه Owner، هویت Commit ادغام‌شده، نسخه Release، محدودیت Actions و Recovery روی GitHub واقعی مستقل احراز شوند.
