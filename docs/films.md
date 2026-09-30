# Films: how they work and how to make your own

**English** · [Русский](films.ru.md)

A film in Proyavka is not an image file or a downloaded LUT — it's a dozen numbers in `PRESETS` in [`bot/filmbot.py`](../bot/filmbot.py). From them the bot builds a 3D LUT for colour on the fly and adds glow, grain and vignette on top.

## Try it

```bash
.venv/bin/python bot/try_films.py photo.jpg              # all films on one sheet -> photo_films.jpg
.venv/bin/python bot/try_films.py photo.jpg my_film      # your film, full size
.venv/bin/python bot/try_films.py photo.jpg my_film 150  # at 150 % strength
```

Change a number → run → look. No Telegram or server needed. When happy, restart the bot: `sudo systemctl restart proyavka-bot`.

## Add a film

Copy any block in `PRESETS` and give it a new key (lowercase, no spaces):

```python
"my_film": dict(
    name="My Film 400", when=L("улица, вечер", "street, evening"),
    desc=L("тёплые тени, мягкий контраст", "warm shadows, soft contrast"),
    contrast=0.3, lift=(0.04, 0.03, 0.02), sat=0.9,
    shadow_tint=(0.02, 0.0, -0.02), high_tint=(0.02, 0.01, 0.0),
    halation=0.35, grain=0.045),
```

Only list what differs from the defaults (`DEFAULTS` just above `PRESETS`). The new film appears everywhere: buttons under photos, the "Compare" sheet, `/film`, the Mini App. `when` and `desc` take two languages via `L("ru", "en")`.

Don't rename existing keys — the database stores them for every frame. If you must, add the old key to `OLD_KEYS`.

## Parameters

Colour is applied in this order: saturation → gamma → contrast → highlight shoulder → tints → black/white levels. Values in parentheses are `(red, green, blue)`.

| Parameter | Default | What it does | Try |
|---|---|---|---|
| `sat` | `0.9` | saturation; 1 — unchanged | 0.6 muted … 1.35 vivid |
| `gamma` | `(1, 1, 1)` | per-channel midtone gamma; above 1 pulls that colour out of the midtones | `(1, 0.97, 0.98)` — slightly more green and blue |
| `contrast` | `0.35` | S-curve strength | 0.2 flat … 0.7 punchy |
| `shoulder` | `0.78` | where highlights start to roll off; lower — softer highlights | 0.7 … 0.85 |
| `shadow_tint` | `(0, 0, 0)` | colour added to shadows | `(-0.03, 0.01, 0.02)` — teal shadows |
| `high_tint` | `(0, 0, 0)` | colour added to highlights | `(0.04, 0.02, -0.03)` — warm highlights |
| `lift` | `(0.03, 0.03, 0.03)` | black level per channel: faded, milky blacks | `(0.05, 0.035, 0.02)` — warm faded blacks |
| `top` | `0.975` | white level; below 1 — matte highlights | 0.93 for a faded look |
| `bw` | `None` | black & white: channel mix instead of colour | `(0.25, 0.6, 0.15)` soft, `(0.45, 0.45, 0.1)` darker skies, like an orange filter |
| `halation` | `0.3` | red glow around bright areas, like light scattering in film | 0.15 subtle … 0.9 night film |
| `hal_thr` | `0.72` | brightness where halation starts; lower — more things glow | 0.6 … 0.8 |
| `bloom` | `0.08` | soft haze over highlights | 0 … 0.15 |
| `soften` | `0.5` | slight blur against digital crispness (in pixels at 3000 px) | 0 … 1 |
| `grain` | `0.04` | grain strength | 0.025 fine … 0.08 heavy |
| `grain_size` | `1.6` | grain size (pixels at 3000 px) | 1.3 fine … 2 coarse |
| `grain_color` | `0.3` | share of colour noise in the grain; 0 — monochrome grain | 0 … 0.5 |
| `vignette` | `0.2` | corner darkening | 0 … 0.3 |

The **Strength** buttons (25–150 %) blend the film with the original; grain and vignette scale with it too.

## Automatic choice

With "Auto by scene" the bot looks at brightness, colour and time of shot in `auto_pick()`: night → `night800`, warm light → `amber_neg`, overcast → `muted_chrome`, landscape → `vivid50`, everything else → `street_neg`. Add your film there if you want it chosen automatically.

## Light leaks

Leaks live in `LEAKS` (name and description) and `leak_layer()` (how each one is drawn). A new leak needs a bit of drawing code — see how `orb` or `streak` are made.
