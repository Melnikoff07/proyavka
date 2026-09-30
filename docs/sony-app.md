# Proyavka app for Sony — step by step

**English** · [Русский](sony-app.ru.md)

For Sony cameras with PlayMemories Camera Apps: a5000/a5100, a6000/a6300/a6500, a7 II / a7R II / a7S II, RX100 III–V, RX10 II/III and others from the [compatibility list](https://openmemories.readthedocs.io/devices.html). If your camera menu has an **Application** section, it most likely works.

You need a Windows or macOS computer, a USB cable and 10 minutes. The camera firmware is not modified: the app installs like the official PlayMemories apps and can be removed in the camera menu.

## 1. Download two files

- **Proyavka.apk** — from this project's [Releases](../../../releases/latest) page.
- **pmca-gui** — the tool that installs apps on Sony cameras:
  - Windows: [pmca-gui-v0.18-win.exe](https://github.com/ma1co/Sony-PMCA-RE/releases/download/v0.18/pmca-gui-v0.18-win.exe)
  - macOS: [pmca-gui-v0.18-osx.dmg](https://github.com/ma1co/Sony-PMCA-RE/releases/download/v0.18/pmca-gui-v0.18-osx.dmg) (a Mac may need the [Sony driver](https://support.d-imaging.sony.co.jp/mac/driver/11/ja/); close Photos, Dropbox and Google Drive — they grab the USB device)

Windows may warn that the program is unknown: "More info" → "Run anyway". It's an open-source project, [source on GitHub](https://github.com/ma1co/Sony-PMCA-RE).

## 2. Connect the camera

1. On the camera: **Menu → Setup → USB Connection → MTP** (Mass Storage works too).
2. Connect the camera to the computer and turn it on. The camera shows "USB connecting".

## 3. Install the app

1. Start **pmca-gui**.
2. Open the **Install app** tab.
3. Choose **Select an apk**, click **Open apk...** and pick the downloaded `Proyavka.apk`.
4. Click **Install selected app** and wait for "Task completed successfully". The camera shows the installation progress.
5. Unplug the cable.

The app is now in **Menu → Application → Application List → Proyavka**.

## 4. Put the settings on the card

1. Send **/camera** to your bot in Telegram — it replies with `config.txt`.
2. Put the memory card into the computer (or connect the camera in Mass Storage mode).
3. In the **root** of the card create a folder `PROYAVKA` (next to `DCIM`) and put `config.txt` into it.

```
memory card
├── DCIM
└── PROYAVKA
    └── config.txt
```

## 5. Add Wi-Fi — once

1. Turn on the hotspot on your phone (and/or be near your home Wi-Fi).
2. **Menu → Application → Proyavka → Wi-Fi.** A list of nearby networks appears.
3. Select a network with the wheel, press the center button, type the password with the on-screen keyboard → **Connect**.
4. If the password is right — "Saved". The network gets a ✓ and from now on the camera connects to it by itself.

You can save several networks (phone and home) — the camera uses whichever is nearby. Select a saved network again to connect, change the password or forget it (or press the trash button). Networks are stored on the card in `PROYAVKA/wifi.txt` and survive even when the camera "forgets" Wi-Fi.

## 6. Shoot

1. Image quality: **JPEG** or **RAW+JPEG** (RAW files are not sent).
2. **Menu → Application → Proyavka → Send new** (the center button — it's selected already).
3. The app turns Wi-Fi on, sends the new frames, shows "Done" and turns Wi-Fi off. Half a minute later the frames are in the bot. **MENU** — exit, **trash** while sending — cancel.

The app speaks the language set in the wizard (`lang` in `config.txt`); without it, it follows the camera's language.

## Troubleshooting

| On the camera screen | What to do |
|---|---|
| "No settings…" | `PROYAVKA/config.txt` is missing on the card — check the folder and file name |
| "Wi-Fi is not set up" | Open Wi-Fi and pick a network (step 5) |
| "No Wi-Fi. Can't see: …" | Turn on the phone hotspot; if you changed the password — Wi-Fi → network → "Re-enter password" |
| No keyboard appears | Press the center button on the password field |
| "the server rejected the token" | `config.txt` is outdated — get a new one with /camera |
| "No new frames" | Everything is sent already. To send again, delete `PROYAVKA/sent.txt` |
| pmca-gui can't see the camera | Try the other USB mode (MTP ↔ Mass Storage), another cable or port; close apps that open the camera |
