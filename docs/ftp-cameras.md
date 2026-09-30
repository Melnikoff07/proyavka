# Cameras with FTP upload — step by step

**English** · [Русский](ftp-cameras.ru.md)

Many recent cameras can send every shot to an FTP server by themselves: Sony A7 IV, A7C II, A7CR, A7R V, A1, A9 III, FX3/FX30, ZV-E1 and others, plus many Fujifilm, Canon and Nikon models. Proyavka accepts frames over **FTPS** — FTP with encryption, so neither the password nor the photos travel in plain text.

Menu names below are Sony's. Other brands use similar ones.

## 1. Connection details

Send **/camera** to your bot in Telegram. It replies with:
- the server address, user (`camera`) and password;
- **cacert.pem** — the root certificate the camera uses to verify it talks to your server and not an impostor.

## 2. Import the certificate

Without this step a camera with secure mode enabled refuses to connect.

1. Save `cacert.pem` from the chat into the **root** of the memory card (next to `DCIM`, not inside it). The file name must stay exactly `cacert.pem`.
2. Put the card into **slot 1**.
3. **MENU → Network → Network Option → Import Root Certificate → FTP Function → OK.**

The certificate is cleared when the camera's network settings are reset — import it again then.

## 3. Set up the server

**MENU → Network → Transfer/Remote → FTP Transfer Func. → Server Setting → Server 1:**

| Item | Value |
|---|---|
| Display Name | anything, e.g. `Proyavka` |
| Host Name | the address from /camera, like `1-2-3-4.sslip.io` |
| Secure Protocol | **On** |
| Root Certificate Error | **Connect Not Allowed** |
| Port | `21` |
| Passive Mode | **On** |
| Directory | `upload` |
| User / Password | `camera` and the password from /camera |

## 4. Turn on automatic transfer

- **FTP Transfer Function → On**, **Server 1** selected.
- **Auto FTP Transfer → On** — each shot is sent right after it's taken.
- **Transfer file type: JPEG only** (the bot handles JPEG and HEIF; sending RAW is slow and pointless).
- Connect the camera to Wi-Fi: **MENU → Network → Wi-Fi → Access Point Set.** — phone hotspot or home.

Take a shot — half a minute later it's in the bot.

## If it doesn't connect

| Symptom | Check |
|---|---|
| Certificate error | Was `cacert.pem` imported? Was it in the root of the card in slot 1? |
| Login error | User `camera` (lowercase), password from /camera without spaces |
| Connects but no files arrive | Passive Mode **On**, directory `upload` |
| All set but no network | Is the camera on a Wi-Fi with internet? Is phone hotspot sharing enabled? |

Menu reference — [Sony FTP Help Guide](https://helpguide.sony.net/di/ftp_2110/v1/en/contents/TP1000657906.html).
