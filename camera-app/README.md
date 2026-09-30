# Proyavka app for Sony cameras

**English** · [Русский](README.ru.md)

A "Send new" button for Sony cameras with PlayMemories Camera Apps (Android 4.1 inside): a5000/a5100, a6000/a6300/a6500, a7 II / a7R II / a7S II, RX100 III–V, RX10 II/III and others from the [compatibility list](https://openmemories.readthedocs.io/devices.html). Step-by-step install — [docs/sony-app.md](../docs/sony-app.md).

## Usage

1. Once: **Proyavka → Wi-Fi** → pick a network → enter the password. The app checks the connection and remembers the network.
2. Shoot **JPEG** or **RAW+JPEG** (RAW is not sent).
3. **Menu → Application → Proyavka → Send new.** The app turns Wi-Fi on, connects to one of the saved networks, sends the frames, shows the result and turns Wi-Fi off. Trash button — cancel, MENU — exit.

Sent files are logged on the card (`PROYAVKA/sent.txt`) — nothing is sent twice, each card has its own log. Delete that file to send everything again.

## Settings on the card

`PROYAVKA/config.txt` is created by the `setup.py` wizard; the bot sends it on `/camera`:

```properties
url = https://1-2-3-4.sslip.io
token = receiver-token
lang = en                  # en / ru; if missing — the camera's language
# prefix = a6300_          file name prefix, the camera model by default

# optional: networks can be pre-filled here instead of typing them on the camera
wifi1.ssid = iPhone
wifi1.pass = hotspot-password
```

Networks added on the camera are stored next to it in `PROYAVKA/wifi.txt` and take priority over `config.txt`. A backslash in a password is written doubled: `\\`.

## Why it works this way

- **The app keeps the networks itself.** The camera stores its Wi-Fi list on a RAM disk and doesn't always restore it from backup — hence "the camera forgot my Wi-Fi". The app re-adds the saved networks on every launch.
- **Wi-Fi is on only while sending** and turned off on exit — no battery drain, and background processes don't survive on these cameras anyway.
- **HTTPS with bundled root certificates.** Android 4.1 has no Let's Encrypt roots and TLS 1.2 is disabled; the app ships ISRG X1/X2/YE/YR roots and enables TLS 1.2 itself. If your server's certificate isn't from Let's Encrypt, add its root to `certs/` and rebuild.
- **Integrity check.** The server returns the SHA-1 of the received file and the app compares it with its own; on mismatch it retries, up to three times.

## Build

Needs Windows and PowerShell; the script downloads everything else (~300 MB into `.tools`):

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

The result is `Proyavka.apk`. A signing key is created on the first build (`proyavka.keystore`). **Keep it:** an update installs over the existing app only if signed with the same key. It never goes into git.

`-Config my.properties` embeds settings into the APK (for a personal build without a file on the card); `config.txt` on the card still wins.
