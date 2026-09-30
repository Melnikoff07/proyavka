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

Data folder: `/var/lib/proyavka/data` on a VPS, `~/proyavka-data` on a home machine.

## config.env

One `NAME=value` per line. After editing: `sudo systemctl restart proyavka-bot`.

| Setting | Default | Meaning |
|---|---|---|
| `LANGUAGE` | set by the wizard | interface language: `en` or `ru` |
| `BOT_TOKEN` | — | token from @BotFather |
| `CHAT_ID` | — | the only chat the bot talks to (yours) |
| `WEBAPP_URL` | — | Mini App address, `https://<server>/` |
| `PROJECT_URL` | this repo | where the guide links in `/camera` point |
| `STORAGE_GB` | `20` | disk space frames may use; oldest originals are removed first |
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
| `SETTLE_SECONDS` | `15` | a file is taken only after it hasn't changed for this long (fallback polling) |

Lines under `setup wizard` (`MODE`, `SERVER_IP`, `DOMAIN`, `CAMERA_TOKEN`, `FTP_PASS`…) are used by the wizard only. **Don't publish `config.env`** — it contains your tokens and passwords.

## Services and logs

| Service | What it does |
|---|---|
| `proyavka-bot` | the bot, processing and the Mini App web server |
| `proyavka-recv` | receiver for the Sony camera app (HTTPS upload) |
| `proyavka-tunnel` | home + VPS setup only: Mini App tunnel to the VPS |
| `nginx`, `vsftpd` | HTTPS and FTPS for cameras |

```bash
sudo journalctl -u proyavka-bot -f        # live bot log
sudo systemctl restart proyavka-bot       # restart after editing
systemctl status proyavka-bot proyavka-recv nginx vsftpd
```

Or `python3 setup.py` → "Status".

## How a frame travels

1. The camera uploads the file to `/srv/camera/upload` on the server (FTPS from cameras with FTP, HTTPS from the Sony app).
2. The bot notices it instantly (`inotifywait`), takes the file and checks it's a complete JPEG.
3. It picks a film (automatically by scene or your default), renders the chat version and sends it to Telegram.
4. The original stays in the data folder while there's space and `ORIGINALS_DAYS` hasn't passed — so you can change film, strength, leaks and export full size.

## Try films without Telegram

```bash
.venv/bin/python bot/try_films.py photo.jpg            # sheet with all films -> photo_films.jpg
.venv/bin/python bot/try_films.py photo.jpg night800   # one film, full size
```

Handy while tuning your own film — see [films](films.md).
