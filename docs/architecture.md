# How the code is organised

`bot/filmbot.py` is only the entry point (the service and the setup wizard run it). Everything else is the package `bot/proyavka/`, one module per job. A module imports only the layers below it; a call upwards (or to a name that tests replace) is an import inside the function that needs it.

| Layer | Modules | What they do |
|---|---|---|
| 0 | `config`, `i18n`, `util` | settings from the environment and the data folders; two-language strings; small helpers |
| 1 | `film`, `imaging`, `pwa` | the film engine (curves, colour bands, channel mixing, glow, grain, LUTs, custom films, share codes); opening frames (RAW too), crop, light leaks, date, frame, contact sheets; app icon, manifest, service worker |
| 2 | `database`, `users` | SQLite, schema and migrations, `q`/`run`; users and their settings |
| 3 | `jobs`, `pools` | tasks that run in worker processes (render and files only: they import no database and no Telegram); the process pools |
| 4 | `telegram`, `sessions`, `push` | Telegram API, keyboards, message edits; sessions, sign-in, signed image URLs; Web Push |
| 5 | `scheduler`, `storage`, `photos` | render queues by urgency and round-robin between users, batches, export, the Telegram update queue; usage and cleanup by limits; trash |
| 6 | `state`, `looks`, `albums` | `state.json`; custom films and the community catalog (and the receiver that accepts films from other servers); albums by link |
| 7 | `ingest`, `devices`, `camera` | receiving frames from the camera and the app; devices and the account API; the user's camera |
| 8 | `invites`, `botui` | invitations and users; the bot in the chat |
| 9 | `web` | the web server of the app |

Things worth knowing before you change something:

- **Films** are in `film.py` (`PRESETS`); what a parameter does is in [films](films.md). The old values of the built-in films are kept in `bot/films_v1.json`.
- **Settings that change at run time** (`BOT_TOKEN`, `STORAGE_GB`) are always read and written as `config.NAME`. The other settings are imported by name.
- **Names that tests replace** (`telegram.tg_send`, `sessions.check_init_data`, `ingest.fetch_from_vps`, `camera.cam_helper`, `push.webpush`, `config.WEBAPP_HTML`, the `COMMUNITY_*` switches...) are imported inside functions, so the current value is always seen. Replace them in the module that defines them.
- **Tests** are in `tests/` (`python tests/run_all.py`). They run the whole bot with Telegram mocked and synthetic frames. Import the package modules in a test only after `harness.start()`: `config` reads the environment when it is imported.
