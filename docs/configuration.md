# Settings, files and logs

**English** · [Русский](configuration.ru.md)

Everything runs on **your** server with **your** bot. Nothing goes through the author's servers: your photos, tokens and passwords never leave your machines.

The easy way to change settings is `python3 setup.py` → menu. This page is for those who want to know what's inside and edit by hand.

## Where things are

When installed on a VPS as root, the program lives in `/opt/proyavka`; on a home machine — wherever you cloned it.

| Path | What it is | Edit? |
|---|---|---|
| `config.env` | all bot settings (see below) | yes, then restart the bot |
| `camera-config/config.txt` | settings file for the Sony camera app; the bot sends it on `/camera` | via the wizard |
| `bot/filmbot.py` | the bot and image processing; films are in `PRESETS` | yes — see [films](films.md) |
| `bot/webapp.html` | the Mini App (one HTML file) | yes |
| `relay/` | server setup scripts and the camera upload receiver | rarely |
| data folder (`BASE_DIR`) | originals, processed frames, previews, database | no |

Data folder: `/var/lib/proyavka/data` on a VPS, `~/proyavka-data` on a home machine. The admin's frames are right in it (`originals`, `work`, `views`, `thumbs`), invited users' frames are in `users/<id>/`.

## config.env

One `NAME=value` per line. After editing: `sudo systemctl restart proyavka-bot`.

| Setting | Default | Meaning |
|---|---|---|
| `LANGUAGE` | set by the wizard | interface language: `en` or `ru` |
| `BOT_TOKEN` | — | token from @BotFather |
| `CHAT_ID` | — | your Telegram id: you are the bot's admin, others join via `/invite` |
| `WEBAPP_URL` | — | Mini App address, `https://<server>/` |
| `PROJECT_URL` | this repo | where the guide links in `/camera` point |
| `STORAGE_GB` | `20` | disk space your frames may use; oldest originals are removed first |
| `USER_STORAGE_GB` | `5` | limit for invited users; change it per person in `/users` |
| `DAILY_UPLOAD_LIMIT` | `300` | frames per 24 hours an invited user may send (the admin has no limit); `0` — no limit |
| `MAX_MEGAPIXELS` | `120` | larger images are rejected right away — protection against "bombs" that blow up in memory |
| `CLEANUP_MINUTES` | `15` | how often storage limits are checked (also right after frames arrive) |
| `UPLOAD_MAX_MB` | `50` | max size of one file uploaded with "+" in the Mini App (nginx has the same limit) |
| `CHAT_PACE_SECONDS` | `1` | background chat edits (batches, deletes) at most once per this many seconds, as Telegram requires |
| `ORIGINALS_DAYS` | `14` | keep originals this long; after that a frame stays viewable but its film can't be changed |
| `MIN_FREE_GB` | `5` | always leave this much free disk space |
| `FAST_WORKERS` | CPUs − 1 (max 3) | processes for chat images and previews |
| `HEAVY_WORKERS` | `1` | processes for full-size exports |
| `WORKER_NICE` | `10` | processing priority (higher — gentler to other programs) |
| `WORK_EDGE` | `2560` | long side of the image sent to the chat |
| `VIEW_EDGE` | `1600` | long side of the image in the Mini App |
| `FULL_EDGE` | `0` | long side of "File" exports; `0` — camera's full size |
| `VPS` | `local` | where frames arrive: `local` (same server) or `user@host` (home machine + VPS) |
| `SSH_KEY`, `REMOTE_DIR` | — | for the home + VPS setup: key and upload folder on the VPS |
| `REMOTE_USERS_DIR` | `/srv/camera/u` | camera folders of invited users on the receiving server |
| `SETTLE_SECONDS` | `15` | a file is taken only after it hasn't changed for this long (fallback polling) |

Limits can also be set at install time: `python3 setup.py --storage-gb=0.1 --user-storage-gb=2 --daily-limit=100` (on an existing install it saves them and restarts the bot).

Every 10 minutes the bot clears what it doesn't take from the receiving server: RAW, non-photos and files in subfolders (older than 10 minutes), unfinished uploads (older than 2 hours). So nobody can fill the disk with junk over FTP.

Lines under `setup wizard` (`MODE`, `SERVER_IP`, `DOMAIN`, `CAMERA_TOKEN`, `FTP_PASS`…) are used by the wizard only. **Don't publish `config.env`** — it contains your tokens and passwords.

## Services and logs

| Service | What it does |
|---|---|
| `proyavka-bot` | the bot, processing and the Mini App web server |
| `proyavka-recv` | receiver for the Sony camera app (HTTPS upload); invited users' tokens are stored as hashes in `/etc/proyavka/camera-tokens` |
| `proyavka-tunnel` | home + VPS setup only: Mini App tunnel to the VPS |
| `nginx`, `vsftpd` | HTTPS and FTPS for cameras; FTP logins are in `/etc/proyavka/ftp.passwd` (pam_pwdfile), each with its own folder |

The bot adds and removes invited users' cameras itself: `sudo /usr/local/lib/proyavka/proyavka-user add|del u<id>`. The sudo rule (`/etc/sudoers.d/proyavka`) lets the bot run only this script.

```bash
sudo journalctl -u proyavka-bot -f        # live bot log
sudo systemctl restart proyavka-bot       # restart after editing
systemctl status proyavka-bot proyavka-recv nginx vsftpd
```

Or `python3 setup.py` → "Status".

## How a frame travels

1. The camera uploads the file to `/srv/camera/upload` on the server (invited users: `/srv/camera/u/u<id>/upload`): FTPS from cameras with FTP, HTTPS from the Sony app. Phone photos go straight to the bot via "+" in the Mini App.
2. The bot notices it instantly (`inotifywait`), takes the file and checks it's a complete JPEG.
3. It picks a film (automatically by scene or the owner's default), renders the chat version and sends it to the frame owner in Telegram.
4. The original stays in the data folder while there's space and `ORIGINALS_DAYS` hasn't passed — so you can change film, strength, leaks and export full size.
5. A deleted frame disappears from the feed and the chat, but its files stay in the trash: bring it back with `/trash` in the chat. When space nears the limit, the trash is emptied first (oldest deletions first), then old originals.

## Try films without Telegram

```bash
.venv/bin/python bot/try_films.py photo.jpg            # sheet with all films -> photo_films.jpg
.venv/bin/python bot/try_films.py photo.jpg night800   # one film, full size
```

Handy while tuning your own film — see [films](films.md).
