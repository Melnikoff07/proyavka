# Changelog

English first, [по-русски — ниже](#журнал-изменений).

## Unreleased (1.4)

### Your own films and the community
- **Film editor.** A film is a set of numbers (contrast, fade, shadow and highlight tints, red halation, bloom, softness, grain, vignette…). Build one with sliders and a live preview on your own frame, start from any built-in film, edit it later — frames using it are redrawn. On phones the settings are tabs under a big preview (Colour, Shadows, Highlights, Glow, Grain, Bands, Channels); on a computer the frame is on the left and all sliders on the right, previewed at 1000 px.
- **Film codes.** A whole film fits in one line (`proyavka-look:1:…`): send it to a friend, they paste it under Community → My films → Add by code.
- **Community catalog.** Community is a main tab next to Gallery. The server downloads the catalog (`community/looks.json`, by default from GitHub, hourly, with a copy in the repo as a fallback) together with ready-made preview images, so browsing renders nothing on your server. Open a film to see it on the shared sample or on your own frame with a before/after slider; add it in one tap. The catalog also opens over the frame viewer to try films on that frame.
- **Send a film to the author without GitHub.** Any Proyavka server can receive films (`COMMUNITY_HUB=1`): an admin-only Review tab to rename, describe, approve or reject; approved films get a preview and appear in the catalog that the server also serves to others. Other servers point at it with `COMMUNITY_SUBMIT_URL`; without it, "Author" sends the code via Telegram or opens a GitHub issue. Submissions are limited (8 KB, 5 an hour and 20 a day per address, queue of 300, duplicates ignored, unreviewed ones leave after 30 days).
- **Before/after slider** in the frame viewer, the editor and the film view.

### Film engine
- New parameters, off by default (old films look exactly the same):
  - colour bands: hue shift (±40°) and saturation for reds, yellows, greens, cyans, blues and magentas, smooth between bands, greys untouched;
  - channel mixing: six cross-channel amounts, greys stay grey;
  - grain in the shadows: coarser, stronger grain where the frame is dark;
  - glow in linear light: halation and bloom computed by light energy (softer, wider around bright lights), processed in 256-row strips to keep memory low on 24 MP frames.
- **The 12 built-in films were retuned** with bands and shadow grain (Night 800T uses linear glow, Expired uses channel mixing). The old values are in `bot/films_v1.json` and the git tag `films-v1`; the engine still reproduces them exactly. After the update, frames that use built-in films are redrawn once in the background, so the feed matches downloads and albums.

### App
- **Header:** a labelled "+ Add" button and a ☰ menu: add photos, select frames, create a film, community films, trash, settings.
- **Trash:** Restore or Delete for good on each frame, and "Empty the trash for good".
- **Frame viewer:** Film / Light leak / Crop as a large switch; "Another leak variant" sits under the leak strip; **zoom** with two fingers, the mouse wheel or a double tap, drag to move, a reset badge; on a computer — frame on the left, controls on the right, arrows between frames.
- **Instant switching back:** the server keeps the last 8 rendered states of each frame (film, strength, leak, crop, date, border). Going back to one you have seen takes milliseconds instead of a new render, and the browser shows it from its cache right away.
- Author's Telegram (`CONTACT_TG`) in the album footer and in Settings → About.

### Security
- **Image links no longer carry the session.** `<img>` and download links use a separate signed view-only token (`?m=`, 1–2 days). It opens only your images and downloads, nothing else. The old `?s=` is still accepted for now and goes away in the next version.
- Unlinking a device revokes the image links it had.
- The camera's `config.txt` (it holds the upload key) is downloaded through its own 5-minute link, not the image token.
- Hardening: huge numbers in film codes or submissions are a normal error, not a crash; garbage tokens give 401, not 500; the image key file is created with mode 600; the receiving server's catalog can't be overwritten by two approvals at once.

### Under the hood
- `bot/filmbot.py` (≈5300 lines) became an entry point; the code lives in the `bot/proyavka/` package, 25 modules in layers — see [docs/architecture.md](docs/architecture.md). The service and the setup wizard still run `bot/filmbot.py`, so updating with `git pull` works as before.
- **Public tests** (`tests/`, 19 suites, synthetic frames, Telegram mocked): `python tests/run_all.py`; GitHub Actions runs pyflakes and the tests. Reference images pin the look of all 12 films (`tests/make_film_ref.py`).
- Fixed: `/lang` in the bot could not switch back; a race that could hide an approved film; the user list could look empty for a moment during a reload.

### Updating
`python3 setup.py` → Update. No new dependencies. After the restart the bot redraws frames with built-in films in the background (a few minutes per few hundred frames on one core). New optional settings: `COMMUNITY_URL`, `COMMUNITY_REPO`, `COMMUNITY_HUB`, `COMMUNITY_SUBMIT_URL`, `CONTACT_TG` — see [docs/configuration.md](docs/configuration.md).

## 1.3 — 2026-10-06
- **Albums by link:** pick frames → Album → a link that opens without sign-in, in the app's style, with full-size downloads and "Download all" (ZIP). Link previews in messengers, no search indexing; manage, rename, change frames or delete in settings.
- **New-frame notifications** (Web Push) in the installed app; **theme switch** (light / dark / system).
- Fixed: the upload progress bar overlapped the selection bar.

## 1.2 — 2026-10-05
- **Telegram is optional.** Proyavka works as an app in any browser and installs on phones and computers; setup ends with a QR code.
- Sign in with one-time codes (`K7QM-2XPF`), QR or link; device list and removal.
- Settings in the app: name, language, default film, storage and trash, devices, camera files, Telegram, users and invites.
- Invites work in the app and in the bot. Downloads: full size on a computer, ZIP for several frames, Share on iPhone.
- Short FTP passwords; RAW on by default (`rawpy`), JPEG preferred for RAW+JPEG.
- Guards for hosting others: pixel limit, daily upload limit, CSP, rate limits.
- Fixed: setup on Ubuntu 26.04, RAW without `rawpy`, a deleted data folder.

## 1.1 — 2026-10-05
- **Several people in one bot:** invites, users with their own feed, camera, language and storage limit.
- **Your own LUTs** (`.cube`), private to each user; phone photos with "+"; selection mode with batch film/leak/strength, files and delete; **crop**; **trash**; per-user language.
- Faster and lighter rendering (one render per edit, smaller film tables, paced chat updates, fair queue between users).
- Camera app 1.3: Wi-Fi list without glyphs missing from the camera font.

## 1.0 — 2026-09-30
- First release: frames from the camera go through a receiving server to the bot, get a film look (LUT, grain, halation) and arrive in Telegram and the Mini App. 12 films, light leaks, date stamp and border, strength, setup wizard, Sony camera app.

---

# Журнал изменений

## Не выпущено (1.4)

### Свои плёнки и сообщество
- **Редактор плёнок.** Плёнка — набор чисел (контраст, выцветание, оттенки теней и светов, красное свечение, дымка, мягкость, зерно, виньетка…). Собирается ползунками с живым просмотром на своём кадре, можно начать с любой встроенной и потом поправить — кадры с ней перерисуются. На телефоне настройки — вкладки под крупным превью (Цвет, Тени, Света, Свечение, Зерно, Полосы, Каналы), на компьютере — кадр слева, все ползунки справа, просмотр в 1000 px.
- **Код плёнки.** Вся плёнка в одной строке (`proyavka-look:1:…`): отправь другу, он вставит её в «Сообщество» → «Мои плёнки» → «Добавить по коду».
- **Каталог сообщества.** «Сообщество» — вкладка рядом с «Галереей». Сервер сам скачивает каталог (`community/looks.json`, по умолчанию с GitHub, раз в час, запасная копия — в репозитории) вместе с готовыми превью, поэтому просмотр каталога ничего не рисует на сервере. Плёнку можно открыть на общем образце или на своём кадре с ползунком «до/после» и добавить одним нажатием. Каталог открывается и поверх просмотра кадра — примерять плёнки на этот кадр.
- **«Автору» без GitHub.** Любой сервер Проявки может принимать плёнки (`COMMUNITY_HUB=1`): вкладка «На проверке» у администратора — переименовать, описать, одобрить или отклонить; одобренная получает превью и попадает в каталог, который сервер раздаёт другим. Остальные серверы шлют туда по `COMMUNITY_SUBMIT_URL`; без него «Автору» отправляет код через Telegram или открывает заявку на GitHub. Приём ограничен (8 КБ, 5 в час и 20 в сутки с адреса, очередь 300, дубли не копятся, непросмотренные уходят через 30 дней).
- **Ползунок «до/после»** в просмотре кадра, в редакторе и на экране плёнки.

### Движок плёнок
- Новые параметры, по умолчанию выключены (старые плёнки выглядят точно так же):
  - полосы цвета: сдвиг оттенка (±40°) и насыщенность для красных, жёлтых, зелёных, голубых, синих и пурпурных, плавно между полосами, серое не трогается;
  - смешивание каналов: шесть перетеканий, серое остаётся серым;
  - зерно в тенях: крупнее и заметнее там, где кадр тёмный;
  - свечение в линейном свете: халяция и дымка по энергии света (мягче и шире вокруг огней), кадр обрабатывается полосами по 256 строк — на 24 Мп памяти нужно мало.
- **12 встроенных плёнок перенастроены** с полосами и зерном в тенях (Night 800T — линейное свечение, Expired — смешивание каналов). Прежние значения — `bot/films_v1.json` и git-тег `films-v1`; движок по-прежнему воспроизводит их точь-в-точь. После обновления кадры со встроенными плёнками один раз перерисовываются в фоне, чтобы лента совпадала со скачанными файлами и альбомами.

### Приложение
- **Шапка:** подписанная кнопка «+ Добавить» и меню ☰: добавить фото, выбрать кадры, создать плёнку, плёнки сообщества, корзина, настройки.
- **Корзина:** у каждого кадра «Вернуть» и «Удалить» навсегда, плюс «Очистить корзину навсегда».
- **Просмотр кадра:** «Плёнка / Засвет / Кадрировать» — крупный переключатель; «Другой вариант засвета» — под лентой засветов; **зум** двумя пальцами, колесом мыши или двойным нажатием, кадр двигается, есть кнопка сброса; на компьютере — кадр слева, управление справа, стрелки между кадрами.
- **Мгновенный возврат к плёнке:** сервер хранит последние 8 нарисованных состояний кадра (плёнка, сила, засвет, кроп, дата, рамка). Вернуться к уже виденному — миллисекунды вместо новой отрисовки, а браузер сразу показывает картинку из своего кэша.
- Telegram автора (`CONTACT_TG`) в подвале альбома и в «Настройки» → «О проекте».

### Безопасность
- **В ссылках на картинки больше нет сессии.** `<img>` и ссылки на скачивание несут отдельный подписанный токен «только смотреть» (`?m=`, 1–2 суток): он открывает только свои картинки и скачивание. Прежний `?s=` пока принимается и уйдёт в следующей версии.
- Отвязка устройства гасит выданные ему ссылки на картинки.
- `config.txt` камеры (в нём ключ для загрузки кадров) скачивается по своей ссылке на 5 минут, а не по токену картинок.
- Укрепления: огромные числа в кодах плёнок и заявках — обычная ошибка, а не падение; мусорный токен — 401, а не 500; файл ключа картинок создаётся с правами 600; два одновременных одобрения больше не затирают каталог приёмника.

### Внутри
- `bot/filmbot.py` (≈5300 строк) стал точкой входа; код — в пакете `bot/proyavka/`, 25 модулей по слоям, см. [docs/architecture.ru.md](docs/architecture.ru.md). Служба и мастер по-прежнему запускают `bot/filmbot.py`, обновление через `git pull` работает как раньше.
- **Публичные тесты** (`tests/`, 19 наборов, синтетические кадры, Telegram подменён): `python tests/run_all.py`; GitHub Actions гоняет pyflakes и тесты. Эталонные картинки закрепляют вид всех 12 плёнок (`tests/make_film_ref.py`).
- Исправлено: `/lang` в боте не переключал язык обратно; гонка, из-за которой одобренная плёнка могла пропасть из каталога; список пользователей на миг казался пустым при перечитывании.

### Обновление
`python3 setup.py` → «Обновить». Новых зависимостей нет. После перезапуска бот фоном перерисует кадры со встроенными плёнками (несколько минут на несколько сотен кадров на одном ядре). Новые необязательные настройки: `COMMUNITY_URL`, `COMMUNITY_REPO`, `COMMUNITY_HUB`, `COMMUNITY_SUBMIT_URL`, `CONTACT_TG` — см. [docs/configuration.ru.md](docs/configuration.ru.md).

## 1.3 — 2026-10-06
- **Альбомы по ссылке:** выбрал кадры → «Альбом» → ссылка, которая открывается без входа, в стиле приложения, с полным размером и «Скачать всё» (ZIP). Превью ссылки в мессенджерах, без индексации; управление, переименование, правка состава и удаление — в настройках.
- **Уведомления о новых кадрах** (Web Push) в установленном приложении; **переключатель темы** (светлая / тёмная / как в системе).
- Исправлено: полоса загрузки перекрывала панель выбора.

## 1.2 — 2026-10-05
- **Telegram необязателен.** Проявка работает как приложение в любом браузере и ставится на телефон и компьютер; установка заканчивается QR-кодом.
- Вход одноразовыми кодами (`K7QM-2XPF`), QR или ссылкой; список устройств и отвязка.
- Настройки в приложении: имя, язык, плёнка по умолчанию, место и корзина, устройства, файлы камеры, Telegram, пользователи и приглашения.
- Приглашения работают и в приложении, и в боте. Скачивание: полный размер на компьютере, ZIP для нескольких кадров, «Поделиться» на iPhone.
- Короткие FTP-пароли; RAW включён по умолчанию (`rawpy`), при RAW+JPEG берётся JPEG.
- Защита при размещении чужих: предел пикселей, дневной лимит загрузок, CSP, ограничение попыток.
- Исправлено: установка на Ubuntu 26.04, RAW без `rawpy`, удалённая папка данных.

## 1.1 — 2026-10-05
- **Несколько человек в одном боте:** приглашения, у каждого своя лента, камера, язык и лимит места.
- **Свои LUT** (`.cube`), видны только владельцу; фото с телефона через «+»; режим выбора с пакетной сменой плёнки/засвета/силы, файлами и удалением; **кадрирование**; **корзина**; язык у каждого свой.
- Отрисовка быстрее и легче (один рендер на правку, плёнки меньше в памяти, правки в чате по лимитам Telegram, очередь по кругу между пользователями).
- Приложение камеры 1.3: список Wi-Fi без символов, которых нет в шрифте камеры.

## 1.0 — 2026-09-30
- Первый выпуск: кадры с камеры через сервер-приёмник попадают к боту, получают плёночный вид (LUT, зерно, халяция) и приходят в Telegram и Mini App. 12 плёнок, засветы, дата и рамка, сила эффекта, мастер установки, приложение для камер Sony.
