"""Приложение как установленная программа: значок, манифест, service worker и политика безопасности страницы."""

import io
from PIL import Image, ImageDraw, ImageFilter

from .i18n import L


ICONS = {}


def app_icon(size):
    """Значок приложения: светлое пятно с ореолом халяции на чёрном (рисуется один раз)."""
    if size not in ICONS:
        s = size * 2
        c, r = s / 2, s * 0.24
        glow = Image.new("RGB", (s, s))
        ImageDraw.Draw(glow).ellipse((c - r * 1.25, c - r * 1.25, c + r * 1.25, c + r * 1.25), fill=(255, 72, 24))
        im = glow.filter(ImageFilter.GaussianBlur(s * 0.07))
        ImageDraw.Draw(im).ellipse((c - r, c - r, c + r, c + r), fill=(255, 248, 236))
        buf = io.BytesIO()
        im.resize((size, size), Image.LANCZOS).save(buf, "PNG", optimize=True)
        ICONS[size] = buf.getvalue()
    return ICONS[size]


def manifest():
    name = L("Проявка", "Proyavka")
    icons = [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"} for n in (192, 512)]
    return {"name": name, "short_name": name, "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#000000", "theme_color": "#000000", "icons": icons}


# Сервис-воркер нужен, чтобы «Проявку» можно было поставить как приложение. Хранит только саму страницу —
# на случай, если сеть пропала; кадры и API идут мимо него.
SW_JS = """const CACHE = "proyavka-shell-v1";
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (x) {}
  e.waitUntil(self.registration.showNotification(d.title || "Proyavka", {
    body: d.body || "", tag: d.tag || "proyavka", renotify: true, icon: "/icon-192.png", badge: "/icon-192.png",
    data: { url: d.url || "/" } }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) if ("focus" in c) return c.focus();
    return self.clients.openWindow((e.notification.data && e.notification.data.url) || "/");
  }));
});
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate" || new URL(e.request.url).pathname !== "/") return;
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
    return res;
  }).catch(() => caches.match("/")));
});
"""


# Страница — из одного файла, поэтому скрипт и стили встроенные; зато грузить что-то с чужих адресов и
# отправлять куда-то, кроме своего сервера, ей нельзя.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
       "style-src 'self' 'unsafe-inline'; font-src 'self'; "
       "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")
