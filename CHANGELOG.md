# Changelog

English first, [по-русски — ниже](#журнал-изменений).

## Unreleased

### Films
- The colour and tone of all 25 built-in films were re-fitted against reference renders (the same frames developed with real film emulations): curves, black lift, tints, saturation and per-colour bands. Grain, halation, glow and vignette are unchanged. Frames already rendered are redrawn in the background once after the update (films version 4); the previous values are kept in `bot/films_v3.json`.
- The film editor now starts from the film you have picked: the “Create” button in the film strip is now “Edit”, and the editor opens with that film's values (the one on the current frame, otherwise the last one you picked). The “Based on” list at the top switches to another film at any time; the name follows it until you type your own. Saving always makes a new film of your own, built-in films stay as they are.
- Grain in previews now matches the finished frame: the film strip, the editor, community previews and the app view (when it is drawn directly at 1600 px) used to show grain 1.3–4× stronger than the same frame at full size scaled to the screen, because small renders cannot make grain finer than a pixel. The grain strength of those small renders is now corrected from measurements (within ~3 % of the full-size result on flat grey). Finished and chat images are unchanged.

### Albums
- Editing by album link: anyone with the link can press “Edit” and change film, strength, light leak, date, frame and crop of the album's frames in the same screen as the app, and download them in full size. Changes are made to the owner's frames themselves, so the owner sees them at once; nothing is sent to the owner's chat or notifications. Guests cannot add, delete, send to chat, open settings or see other frames and custom films. On by default for new albums (a checkbox when creating), off for existing ones; the owner can switch it in the album sheet, and turning it off ends guest sessions at once.

### Speed
- Films with halation/glow render faster with the same picture: the sRGB ↔ linear conversion in the glow step uses lookup tables instead of per-pixel powers (about 25 % faster at 1600 px, about 2× at full size), and full-size frames are processed in strips on several threads. Result differs from the exact calculation by at most 1 level on a fraction of a percent of pixels (below JPEG noise).

### Fixes and hardening
- The Telegram bot token no longer ends up in error messages shown in the chat or written to the log (failed downloads and uploads used to include the full API address).
- After viewing a frame with the "Original" film on a server without Telegram, later renders of that frame came out at 500 px. Fixed.
- Sign-in through Telegram is refused when the server has no bot token (the signature could be forged with an empty key); a broken `auth_date` is a normal error.
- Notification subscriptions are accepted only from the browsers' push services (Google, Mozilla, Apple, Microsoft): the server no longer sends requests to arbitrary addresses.
- Numbers of removed users are not reused, so old image links can never open a new person's photos.
- The session in the address (`?s=`) is gone for good; image links use only `?m=`.
- Batch summaries no longer block the two Telegram upload threads (deleting, files, the "Compare" sheet could stall for up to 15 minutes after a big batch).
- Idle or stalled connections are closed after 2 minutes; negative sizes and `limit`/`offset` are rejected; broken `.cube` files give a clear error instead of a crash.
- Album titles with quotes can't break the link preview tags; the bot token file, the push key and the camera users' password hashes are created with private permissions from the start.
- A frame whose edit was recorded but not yet drawn when the server restarted is now queued again at start (before, it showed “developing” forever and the app polled every second).
- Intake survives a crash in the middle: a frame written to the database before its files were moved is completed or returned to the incoming folder at start (before, the leftover file was taken for a duplicate and deleted).
- Downloaded and exported files now carry the shooting date, camera and exposure data (no GPS) and an sRGB profile, so the phone gallery files them by the date taken. Photos with a Display P3 / Adobe RGB profile are converted to sRGB on the way in instead of being read as sRGB.
- Originals are no longer deleted by age: `ORIGINALS_DAYS` now defaults to `0` — they go only when space runs out (oldest first). Set a number of days to restore the old behaviour.
- The feed update call returns at most 500 changes at a time and tells the app to ask again; before, a mass redraw of more than 500 frames could skip some.
- `setup.py` checks for Python 3.11+ first; the README now names Ubuntu 24.04+ / Debian 12+ (Ubuntu 22.04 ships Python 3.10).
- “Select day” selected only the frames already loaded into the feed (the feed loads 60 at a time), so a day with ~300 frames came out as 60–80. It now loads the rest of the day first.
- Connecting a Telegram bot from settings no longer puts the token into the error text if Telegram is unreachable.
- The server no longer accepts a session token that it never issued (re-sign-in extends only a known token).
- Internal: the per-frame lock table for full-size renders is fixed-size instead of growing with the feed; the log shows the real version (`git describe`) instead of “v6.0”.
- Fonts (Inter Tight, Lilita One; SIL OFL) are now served by the app itself: the app and public album pages no longer contact Google, so a viewer's address is not sent to a third party. Content-Security-Policy no longer allows Google domains.

## 1.5.4 — 2026-10-07
- Camera app 1.4.4: on Android 2.3 cameras (a6000…) the app crashed with OutOfMemoryError on the first frame and the camera closed it. Android 2.3's HTTPS connection ignores streaming mode and buffers the whole file in memory. There the app now sends each frame itself over the TLS connection, 32 KB at a time, checking the server certificate's name itself; Android 4.1 cameras keep the old path. Checked: a 12 MB frame over port 8443 with a 24 MB memory limit.
- The log never stops the app, even if writing it fails.

## 1.5.3 — 2026-10-07
- Camera app 1.4.3: on the a6000 a `config.txt` written by Windows in lowercase shows up in the folder, but the camera's system says it isn't a file. The app no longer trusts that check: it tries the listed name, `CONFIG.TXT` and `config.txt` and takes the first one that actually opens. If none opens, the log shows for each one whether it exists, its size and the exact error.

## 1.5.2 — 2026-10-07
- Camera app 1.4.2: cameras on Android 2.3 (a6000 and others) see the card only in short DOS names (8.3), so `config.txt.txt` shows up there as `CONFIG~1.TXT`. The app now accepts any `CONFIG*.TXT`. If the file still isn't found, the screen and the log list the files in the folder.

## 1.5.1 — 2026-10-07
- Camera app 1.4.1: finds `PROYAVKA/config.txt` however it was saved — any letter case, `config.txt.txt` (Windows with hidden extensions), UTF-8 with or without BOM, UTF-16 (Notepad "Unicode") — and also looks in other card paths of older cameras.
- If the settings still can't be read, the camera says exactly why: where it looked, or which keys the file has; the hint points to Settings → Camera in the app, not only to the bot.
- A log on the card, `PROYAVKA/log.txt`: camera model and Android version, what was found in config.txt, the server address, the TLS version, every upload and every error with its stack trace, crashes too. No token in it — send it along with a question.

## 1.5 — 2026-10-07

### Films reworked
- **Ten built-in films were rebuilt from scratch** (Super 400 and Portrait 400 stay as they were). Measured on a ColorChecker chart, grey ramps, a landscape and a night scene: the old films had nearly straight tone curves (they all looked like one film), one saturation multiplier for every colour, and a wide red halation veil that turned blue skies and night scenes purple. The new ones have their own curves and colour, modelled on Ultramax, Classic Chrome/Kodachrome, Gold, Velvia, Vision3 250D printed on 2383, CineStill 800T, Acros and Tri-X.
- **Halation like film** (`halo`): an orange ring right at very bright sources and a red glow further out, only against a dark background. A bright sky gets none, so blues no longer turn purple.
- **Colour density** (`dens`): saturated dark colours get deeper, light ones more pastel, as with film dyes; greys untouched.
- Both are new sliders in the film editor. Old film codes and your own films look exactly as before; the previous built-in values are kept in `bot/films_v2.json` (and `films_v1.json`), and the engine still reproduces them.
- After the update, frames with built-in films are redrawn once in the background.

### Thirteen new films
- Colour negative: **Pastel 160** (light, airy, pastel), **Mint 400H** (cool pastel, minty greens), **Punch 100** (punchy negative, deep blue sky).
- Slide: **Chrome 100** (clean, neutral whites), **Classic 64** (warm reds and yellows, dense shadows).
- Cinema: **Cine 50D** (clean daylight cine film, soft highlights).
- Processes: **Hard Neg** (hard contrast, teal-green shadows), **Bleach Bypass** (high contrast, little colour), **Cross Process** (yellow-green highlights, blue shadows), **Instant** (milky shadows, soft focus).
- Black and white: **Red Filter** (dark sky, light skin), **Sepia** (warm toned), **Push 3200** (heavy grain).
- Tuned on real photos: night flash portraits, daylight, greenery, golden hour.

### Film editor: the basics
- A new first tab, **Basics**: exposure, contrast, highlights, shadows, whites, blacks, temperature and tint — applied before the film, like in any photo editor. Exposure rolls bright parts off softly instead of clipping.

### Older Sony cameras (Android 2.3)
- The camera app (1.4) now installs and works on a5000, a5100, a6000, RX100 III, NEX-5T and other cameras of the older generation (Android 2.3). Before, pmca-gui failed with "Error 504 … Invalid contents for install".
- Android 2.3 only knows TLS 1.0, so the server gets a separate port, **8443**, that accepts camera uploads only (its own RSA certificate, old ciphers); the website and the app stay on TLS 1.2+. The app picks the port by itself. Update the server with `setup.py` → Update; allow TCP 8443 if your hosting has a firewall.

### Burger theme
- A third theme next to light and dark: cream, brown and red, rounded buttons, and the app name drawn between two buns (Settings → Theme → Burger).

## 1.4 — 2026-10-06

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

## Unreleased

### Плёнки
- Цвет и тон всех 25 встроенных плёнок заново подогнаны по эталонным кадрам (те же кадры, проявленные настоящими эмуляциями плёнок): кривые, подъём чёрного, тонировка, насыщенность и цветовые полосы. Зерно, халяция, свечение и виньетка прежние. Уже готовые кадры после обновления один раз перерисуются в фоне (версия плёнок 4); прежние значения — в `bot/films_v3.json`.
- Редактор плёнки теперь начинается с выбранной плёнки: кнопка «Создать» в ленте плёнок стала «Править», и редактор открывается со значениями этой плёнки (с кадра на экране, иначе последней выбранной). Список «Основа» сверху в любой момент переключает на другую плёнку; название подстраивается, пока вы не ввели своё. Сохранение всегда создаёт новую вашу плёнку, встроенные остаются как есть.
- Зерно в превью теперь соответствует готовому кадру: лента плёнок, редактор, превью сообщества и вид в приложении (когда рисуется сразу в 1600 px) показывали зерно в 1,3–4 раза сильнее, чем тот же кадр полного размера, уменьшенный до экрана: на малом размере зерно не может быть мельче пикселя. Силу зерна в таких рисунках теперь поправили по замерам (в пределах ~3 % от полноразмерного результата на ровном сером). Готовые файлы и картинки для чата не изменились.

### Альбомы
- Правка по ссылке альбома: тот, у кого есть ссылка, нажимает «Редактировать» и меняет плёнку, силу, засвет, дату, рамку и кадрирование кадров альбома в том же экране, что и в приложении, и скачивает их в полном размере. Меняются сами кадры хозяина, поэтому он видит правки сразу; в его чат и уведомления ничего не уходит. Гость не может добавлять, удалять, отправлять в чат, открывать настройки и видеть другие кадры и свои плёнки. Для новых альбомов включено по умолчанию (галочка при создании), для прежних выключено; хозяин переключает это в окне альбома, выключение сразу закрывает сессии гостей.

### Скорость
- Плёнки со свечением рисуются быстрее при той же картинке: перевод sRGB ↔ линейный свет в шаге свечения идёт по таблицам вместо степени на каждый пиксель (около на четверть быстрее на 1600 px, вдвое — на полном размере), а кадры полного размера считаются полосами в нескольких потоках. От точного расчёта результат отличается не более чем на 1 уровень у долей процента пикселей (меньше шума JPEG).

### Исправления и защита
- Токен Telegram-бота больше не попадает в тексты ошибок, которые видны в чате и пишутся в журнал (раньше при сбое скачивания или отправки туда попадал полный адрес API).
- После просмотра кадра с плёнкой «Оригинал» на сервере без Telegram следующие отрисовки этого кадра получались размером 500 px. Исправлено.
- Вход через Telegram закрыт, если на сервере нет токена бота (подпись можно было подделать пустым ключом); битая `auth_date` — обычная ошибка.
- Подписки на уведомления принимаются только от push-сервисов браузеров (Google, Mozilla, Apple, Microsoft): сервер не шлёт запросы на произвольные адреса.
- Номера удалённых пользователей не используются повторно: старые ссылки на картинки не откроют фото нового человека.
- Сессия в адресе (`?s=`) убрана насовсем; ссылки на картинки используют только `?m=`.
- Итоги пакетных правок больше не занимают два потока загрузки в Telegram (после большого пакета удаление, файлы и лист «Сравнить» могли зависнуть до 15 минут).
- Молчащие соединения закрываются через 2 минуты; отрицательные размеры и `limit`/`offset` отклоняются; битые `.cube` дают понятную ошибку, а не сбой.
- Название альбома с кавычками не ломает теги превью ссылки; файл с токеном бота, ключ уведомлений и хеши паролей камер создаются сразу с закрытыми правами.
- Кадр, у которого правка записана, но не успела нарисоваться до перезапуска сервера, теперь ставится в очередь заново при запуске (раньше он «проявлялся» вечно, а приложение опрашивало сервер каждую секунду).
- Приём переживает сбой посередине: кадр, записанный в базу до переноса файлов, при запуске доводится или возвращается во входящие (раньше оставшийся файл принимался за повтор и удалялся).
- Скачанные и выгруженные файлы теперь несут дату съёмки, камеру и экспозицию (без GPS) и профиль sRGB, поэтому галерея телефона ставит их по дате съёмки. Фото с профилем Display P3 / Adobe RGB при приёме переводятся в sRGB, а не читаются как sRGB.
- Оригиналы больше не удаляются по возрасту: `ORIGINALS_DAYS` по умолчанию `0` — они уходят только когда не хватает места (самые старые первыми). Задайте число дней, чтобы вернуть прежнее поведение.
- Запрос обновлений ленты отдаёт не больше 500 изменений за раз и просит приложение спросить ещё; раньше массовая перерисовка больше 500 кадров могла пропустить часть.
- `setup.py` сначала проверяет Python 3.11+; в README теперь Ubuntu 24.04+ / Debian 12+ (в Ubuntu 22.04 Python 3.10).
- «Выбрать день» выбирал только уже подгруженные в ленту кадры (лента грузится по 60), поэтому день из ~300 кадров выбирался на 60–80. Теперь сначала догружается остаток дня.
- Подключение Telegram-бота из настроек больше не показывает токен в тексте ошибки, если Telegram недоступен.
- Сервер не принимает токен сессии, которого сам не выдавал (повторный вход продлевает только известный токен).
- Внутреннее: таблица замков полного размера фиксированная, а не растёт вместе с лентой; в журнале настоящая версия (`git describe`) вместо «v6.0».
- Шрифты (Inter Tight, Lilita One; лицензия SIL OFL) теперь отдаёт само приложение: приложение и публичные страницы альбомов не обращаются к Google, адрес зрителя не уходит третьим лицам. Политика CSP больше не разрешает домены Google.

## 1.5.4 — 2026-10-07
- Приложение камеры 1.4.4: на камерах с Android 2.3 (a6000…) приложение падало с OutOfMemoryError на первом же кадре, и камера его закрывала. HTTPS-соединение Android 2.3 не включает потоковую отправку и держит весь файл в памяти. Теперь там приложение шлёт кадр само поверх TLS-соединения, по 32 КБ, и само проверяет имя в сертификате сервера; на камерах с Android 4.1 — прежний путь. Проверено: кадр 12 МБ через порт 8443 при пределе памяти 24 МБ.
- Журнал никогда не останавливает приложение, даже если записать его не удалось.

## 1.5.3 — 2026-10-07
- Приложение камеры 1.4.3: на a6000 `config.txt`, записанный Windows строчными буквами, виден в папке, но система камеры говорит, что это не файл. Приложение больше не верит этой проверке: пробует имя из списка, `CONFIG.TXT` и `config.txt` и берёт первый, который действительно открывается. Если не открылся ни один — в журнале по каждому: есть ли он, размер и точная ошибка.

## 1.5.2 — 2026-10-07
- Приложение камеры 1.4.2: камеры на Android 2.3 (a6000 и другие) видят карту только в коротких именах DOS (8.3), и `config.txt.txt` там выглядит как `CONFIG~1.TXT`. Теперь подходит любой `CONFIG*.TXT`. Если файл всё же не нашёлся — на экране и в журнале список файлов в папке.

## 1.5.1 — 2026-10-07
- Приложение камеры 1.4.1: находит `PROYAVKA/config.txt`, как бы его ни сохранили — в любом регистре букв, как `config.txt.txt` (Windows со скрытыми расширениями), в UTF-8 с меткой BOM и без, в UTF-16 («Юникод» в Блокноте), — и ищет его и по другим путям к карте на старых камерах.
- Если настройки всё же не прочитались, камера пишет, почему именно: где искала или какие ключи нашлись в файле; подсказка ведёт в «Настройки» → «Камера» в приложении, а не только в бота.
- Журнал на карте, `PROYAVKA/log.txt`: модель камеры и версия Android, что нашлось в config.txt, адрес сервера, версия TLS, каждая отправка и каждая ошибка со стеком, падения тоже. Токена в нём нет — его можно присылать вместе с вопросом.

## 1.5 — 2026-10-07

### Плёнки заново
- **Десять встроенных плёнок собраны заново** (Super 400 и Portrait 400 — как были). Замеры на мишени ColorChecker, серых шкалах, пейзаже и ночной сцене показали: у прежних тональные кривые были почти прямыми (все плёнки по тону — одна и та же), насыщенность менялась одним множителем на все цвета, а широкая красная вуаль халяции уводила голубое небо и ночь в фиолетовый. У новых — свои кривые и цвет; прототипы — Ultramax, Classic Chrome/Kodachrome, Gold, Velvia, Vision3 250D с печатью на 2383, CineStill 800T, Acros и Tri-X.
- **Халяция как у плёнки** (`halo`): оранжевое кольцо у самого яркого источника и красное свечение дальше, только на тёмном фоне. Яркое небо её не получает — голубое больше не уходит в фиолетовый.
- **Плотность цвета** (`dens`): насыщенные тёмные цвета глубже, светлые — пастельнее, как у красителей плёнки; серое не трогается.
- Оба — новые ползунки в редакторе. Старые коды плёнок и свои плёнки выглядят точно как раньше; прежние значения встроенных — в `bot/films_v2.json` (и `films_v1.json`), движок их по-прежнему воспроизводит.
- После обновления кадры со встроенными плёнками один раз перерисуются в фоне.

### Тринадцать новых плёнок
- Цветной негатив: **Pastel 160** (светлая, воздушная, пастельная), **Mint 400H** (прохладная пастель, мятная зелень), **Punch 100** (сочный негатив, глубокое синее небо).
- Слайд: **Chrome 100** (чистый цвет, нейтральные белые), **Classic 64** (тёплые красные и жёлтые, плотные тени).
- Кино: **Cine 50D** (чистая дневная киноплёнка, мягкие света).
- Процессы: **Hard Neg** (жёсткий контраст, бирюзово-зелёные тени), **Bleach Bypass** (высокий контраст, мало цвета), **Cross Process** (жёлто-зелёные света, синие тени), **Instant** (молочные тени, мягкий фокус).
- Чёрно-белые: **Red Filter** (тёмное небо, светлая кожа), **Sepia** (тёплое тонирование), **Push 3200** (крупное зерно).
- Подобраны на живых фото: ночные портреты со вспышкой, день, зелень, золотой час.

### Редактор плёнки: основное
- Новая первая вкладка **«Основное»**: экспозиция, контраст, света, тени, белые, чёрные, температура и оттенок — до плёнки, как в любом фоторедакторе. Экспозиция сжимает яркое мягко, а не обрезает.

### Старые камеры Sony (Android 2.3)
- Приложение камеры (1.4) теперь ставится и работает на a5000, a5100, a6000, RX100 III, NEX-5T и других камерах старого поколения (Android 2.3). Раньше pmca-gui падал с «Error 504 … Invalid contents for install».
- Android 2.3 знает только TLS 1.0, поэтому у сервера появился отдельный порт **8443**, на котором только приём кадров с камеры (свой RSA-сертификат, старые шифры); сайт и приложение остаются на TLS 1.2+. Нужный порт приложение выбирает само. Обнови сервер через `setup.py` → «Обновить»; если у хостинга файрвол — открой TCP 8443.

### Тема «Бургер»
- Третья тема рядом со светлой и тёмной: кремовый, коричневый и красный, скруглённые кнопки, а название нарисовано между двумя булками («Настройки» → «Тема» → «Бургер»).

## 1.4 — 2026-10-06

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
