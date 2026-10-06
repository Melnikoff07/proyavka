# Films: how they work and how to make your own

**English** · [Русский](films.ru.md)

A film in Proyavka is not an image file or a downloaded LUT — it's a dozen numbers in `PRESETS` in [`bot/proyavka/film.py`](../bot/proyavka/film.py). From them the bot builds a 3D LUT for colour on the fly and adds glow, grain and vignette on top.

## Try it

```bash
.venv/bin/python bot/try_films.py photo.jpg              # all films on one sheet -> photo_films.jpg
.venv/bin/python bot/try_films.py photo.jpg my_film      # your film, full size
.venv/bin/python bot/try_films.py photo.jpg my_film 150  # at 150 % strength
```

Change a number → run → look. No Telegram or server needed. When happy, restart the bot: `sudo systemctl restart proyavka-bot`.

## Your own LUT, no code

Have a LUT from Lightroom, Resolve or a pack you bought? Send the `.cube` file to the bot or tap "Your LUT" at the end of the film list in Proyavka. It shows up among the films under photos, in Proyavka, in batch edits and in `/film`; strength blends it with the original, leaks, date and border work as usual. Only the person who uploaded a LUT can see it. List and delete: `/luts` (in Proyavka, long-press the LUT). 3D LUTs of size 2 to 65, up to 16 MB and 30 per person. Unlike Proyavka's films, a LUT is colour only: no grain, halation or vignette.

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
| `hue` | `(0, 0, 0, 0, 0, 0)` | hue shift in degrees for six colour bands: red, yellow, green, cyan, blue, magenta; the other bands and greys stay put | `(0, 0, -22, 0, 0, 0)` — greens lean yellow, the classic "film green" |
| `bsat` | `(1, 1, 1, 1, 1, 1)` | saturation multiplier for the same six bands | `(1, 1, 0.8, 1, 1.25, 1)` — calmer greens, richer blues |
| `mix` | `(0, 0, 0, 0, 0, 0)` | channel mixing, six numbers: how much red takes from green and from blue, green from red and from blue, blue from red and from green. The diagonal is adjusted so greys stay grey | `(0, 0.06, 0, -0.05, 0.04, 0)` — a cross-processed cast |
| `grain_shadow` | `0` | extra grain in the shadows, like a negative; 0 — as before | 0.3 … 0.8 |
| `linear` | `0` | 1 — halation and bloom are computed in linear light: a wider, softer glow proportional to how bright the source is; slower on full-size exports | 0 or 1 |
| `halo` | `0` | 1 — film-like halation: an orange ring right at very bright sources and a red glow further out, only against a dark background; a bright sky or wall gets none, so blues don't turn purple. 0 — the old wide red veil | 1 for new films |
| `dens` | `0` | colour density, as with dyes: saturated dark colours get deeper and darker, light ones more pastel; greys untouched | 0.3 … 0.8 |

The **Strength** buttons (25–150 %) blend the film with the original; grain and vignette scale with it too.

## Automatic choice

With "Auto by scene" the bot looks at brightness, colour and time of shot in `auto_pick()`: night → `night800`, warm light → `amber_neg`, overcast → `muted_chrome`, landscape → `vivid50`, everything else → `street_neg`. Add your film there if you want it chosen automatically.

## Light leaks

Leaks live in `LEAKS` (name and description) and `leak_layer()` (how each one is drawn). A new leak needs a bit of drawing code — see how `orb` or `streak` are made.
