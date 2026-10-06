# Community films

`looks.json` is the catalog every Proyavka server downloads (once an hour). Each entry is a film: a name, an author and about twenty numbers (`p`).

- `sample.jpg` — the shared sample photo (no metadata). The app shows "before" from it.
- `looks/<id>.jpg` — what the film looks like on the sample (900 px). The app serves these as they are, so showing the catalog never renders anything on the user's server.
- `looks/<id>-before.jpg` — optional: the author's own photo without the film. If it exists, the before/after slider uses it instead of the sample.

Add a film that someone suggested (an issue with a `proyavka-look:1:…` code):

    python community/add_look.py "proyavka-look:1:…" [--id name] [--ru "…"] [--en "…"] [--photo their-photo.jpg]

The script validates the code, renders the preview (on the sample, or on `--photo`) and appends the entry to `looks.json`. `python community/add_look.py --rebuild` redraws all previews, e.g. after the rendering changed. Commit `looks.json` and `looks/`, push — servers pick it up within an hour.
