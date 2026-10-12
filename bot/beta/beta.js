"use strict";
const $ = (id) => document.getElementById(id);
const CONTACT = /^\w+$/.test((window.BETA || {}).contact || "") ? window.BETA.contact : "";
const DEV_KEY = "proyavka.device";
const UNTIL = +(window.BETA || {}).until || 0;            // конец беты (секунды), 0 — без срока
// на iPhone Safari и значок на экране «Домой» не делят хранилище: вход из одного не виден в другом
const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
const STANDALONE = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function stored(k, v) {
  try {
    if (v === undefined) return localStorage.getItem(k);
    if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
  } catch (e) { return null; }
}
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- язык и тема (те же ключи, что у приложения) ---------- */
let LANG = stored("proyavka.beta.lang") || (/^(ru|uk|be|kk)\b/i.test(navigator.language || "") ? "ru" : "en");
// типографика: короткие предлоги, союзы и частицы не висят в конце строки, тире не начинает строку, число не отрывается
// от единицы (то же делает сервер со статичным текстом страницы — beta.typo)
const SHORT = "в|во|и|с|со|к|ко|о|об|у|а|на|по|не|ни|за|из|от|до|для|без|при|но|же|ли|да|то|или|как|что|это|a|an|the|of|to|in|on|at|by|or|and|is|it|no|for";
const NB = "\u00a0";
const TY = [[new RegExp(`(?<=^|[\\s(«“])(${SHORT}) `, "giu"), "$1" + NB], [/ —/g, NB + "—"], [/(\d) (?=[^\d\s—–-])/g, "$1" + NB]];
const ty = (s) => String(s).split(/(<[^>]*>)/).map((part, i) => (i % 2 ? part : TY.reduce((t, [re, to]) => t.replace(re, to), part))).join("");
const T = (ru, en) => ty(LANG === "en" ? en : ru);
function setLang(l) {
  LANG = l;
  document.documentElement.lang = l;
  document.documentElement.classList.toggle("en", l === "en");
  document.querySelectorAll("[data-lang]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === l)));
  document.title = T("Проявка — открытая бета", "Proyavka — open beta");
  $("themeBtn").setAttribute("aria-label", T("Светлая или тёмная тема", "Light or dark theme"));
  tickUntil();
  renderFilms(); renderStep(step, true); renderStatus();
  if (!$("panel").hidden) renderPanel();
}
document.querySelectorAll("[data-lang]").forEach((b) => (b.onclick = () => { stored("proyavka.beta.lang", b.dataset.lang); setLang(b.dataset.lang); }));
function applyTheme() {
  const pref = stored("proyavka.theme") || "auto";
  const dark = pref === "auto" ? matchMedia("(prefers-color-scheme: dark)").matches : pref === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
applyTheme();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
$("themeBtn").onclick = () => {          // светлая ⇄ тёмная; совпала с системной — снова «как в системе»
  const dark = document.documentElement.dataset.theme !== "dark";
  stored("proyavka.theme", dark === matchMedia("(prefers-color-scheme: dark)").matches ? "auto" : dark ? "dark" : "light");
  applyTheme();
};
/* сколько осталось до конца беты: дни и часы:минуты:секунды */
function tickUntil() {
  if (!UNTIL) return;
  const s = Math.max(0, Math.round(UNTIL - Date.now() / 1000));
  const p2 = (n) => String(n).padStart(2, "0");
  const end = new Date((UNTIL - 1) * 1000).toLocaleDateString(LANG === "en" ? "en-GB" : "ru-RU", { day: "numeric", month: "long", year: "numeric" });
  $("until").innerHTML = s ? `${T("До конца беты", "Beta ends in")} <b class="num">${Math.floor(s / 86400)}${T("\u00a0д", "d")} ${p2(Math.floor(s % 86400 / 3600))}:${p2(Math.floor(s % 3600 / 60))}:${p2(s % 60)}</b>`
                           : T("Бета закончилась", "The beta is over");
  $("until").title = T(`Бета работает до ${end}`, `The beta runs until ${end}`);
  $("until").hidden = false;
}
setInterval(tickUntil, 1000);
document.querySelectorAll(".tgl").forEach((a) => (a.href = `https://t.me/${CONTACT}`));
if (!CONTACT) document.querySelectorAll(".tg").forEach((e) => e.remove());
addEventListener("scroll", () => $("top").classList.toggle("scrolled", scrollY > 8), { passive: true });

/* ---------- до / после ---------- */
const FILMS = [
  ["super400", "Super 400", "зеленоватые тени, бодрый цвет мыльницы", "greenish shadows, punchy point-and-shoot colour"],
  ["portrait400", "Portrait 400", "тёплая кожа, мягкий контраст, пастель", "warm skin, soft contrast, pastel"],
  ["golden200", "Golden 200", "тёплый жёлтый, сочные красные и жёлтые", "warm yellow, rich reds and yellows"],
  ["cine250", "Cine 250D", "кинонегатив: бирюзовые тени, мягкие света", "cine negative: teal shadows, soft highlights"],
  ["across100", "Across 100", "гладкая ч/б, тонкое зерно, длинные полутона", "smooth b&w, fine grain, long midtones"],
  ["expired", "Expired", "выцветший цвет, тёплые света, много зерна", "faded colour, warm highlights, lots of grain"],
];
let film = 0;
function renderFilms() {
  $("films").replaceChildren(...FILMS.map(([k, name], i) => {
    const b = document.createElement("button");
    b.textContent = name;
    b.setAttribute("aria-pressed", String(i === film));
    b.onclick = () => pickFilm(i);
    return b;
  }));
  const f = FILMS[film];
  $("filmDesc").textContent = `${f[1]} — ${T(f[2], f[3])}. ` + T("Плёнку можно сменить в любой момент — оригинал хранится.", "Switch the film any time — the original is kept.");
}
function pickFilm(i) {
  film = i;
  const img = $("after"), f = FILMS[i];
  img.classList.add("loading");
  const pre = new Image();
  pre.onload = pre.onerror = () => { img.src = pre.src; img.classList.remove("loading"); };
  pre.src = `/beta/film/${f[0]}.jpg`;
  $("afterLbl").textContent = f[1];
  renderFilms();
}
FILMS.slice(1).forEach(([k]) => { const im = new Image(); im.src = `/beta/film/${k}.jpg`; });   // заранее, чтобы переключение было мгновенным
(function compare() {
  const box = $("compare");
  let drag = false, nudged = false;
  const at = (x) => { const r = box.getBoundingClientRect(); box.style.setProperty("--p", Math.max(0, Math.min(100, ((x - r.left) / r.width) * 100)) + "%"); };
  box.addEventListener("pointerdown", (e) => { drag = true; nudged = true; box.setPointerCapture(e.pointerId); at(e.clientX); });
  box.addEventListener("pointermove", (e) => { if (drag) at(e.clientX); });
  box.addEventListener("pointerup", () => (drag = false));
  box.addEventListener("pointercancel", () => (drag = false));
  // подсказка: шторка сама качнётся, пока её не тронули
  new IntersectionObserver((es, o) => {
    if (!es[0].isIntersecting || nudged || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    o.disconnect();
    const t0 = performance.now();
    const tick = (t) => {
      if (nudged) return;
      const k = Math.min(1, (t - t0) / 2200);
      box.style.setProperty("--p", (50 + Math.sin(k * Math.PI * 2) * 22 * (1 - k)) + "%");
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, { threshold: .6 }).observe(box);
})();

/* ---------- гайд: Lottie по сценам ---------- */
const SCENE = 96;
const STEPS = [
  ["Поставь приложение на камеру", "Install the app on the camera",
   "Один раз, по USB, минут за десять: pmca-gui ставит «Проявку» на Sony с PlayMemories. Камерам с FTP приложение не нужно.",
   "Once, over USB, about ten minutes: pmca-gui puts Proyavka on Sony cameras with PlayMemories apps. FTP cameras don't need it."],
  ["config.txt на карту, Wi‑Fi — один раз", "config.txt on the card, Wi‑Fi once",
   "Файл настроек — в Проявке: ☰ → Настройки → Камера. В приложении на камере выбираешь сеть: домашнюю или раздачу с телефона.",
   "Get the settings file in Proyavka: ☰ → Settings → Camera. In the camera app, pick a network — home Wi‑Fi or your phone's hotspot."],
  ["Снимай и отправляй", "Shoot and send",
   "Снимаешь как обычно, потом «Проявка → Отправить новые»: камера сама включает Wi‑Fi и отправляет новые кадры по HTTPS.",
   "Shoot as usual, then “Proyavka → Send new”: the camera turns on Wi‑Fi and sends new frames over HTTPS."],
  ["Сервер проявляет плёнкой", "The server develops it on film",
   "Цвет, зерно, халяция и оверлеи. Плёнка подбирается по сцене — или ставишь свою по умолчанию.",
   "Colour, grain, halation and overlays. The film is picked by scene — or set your own default."],
  ["Кадр уже в телефоне", "The frame is on your phone",
   "Через полминуты — в ленте и уведомлением. Меняй плёнку, кадрируй, скачивай, делись альбомом.",
   "Half a minute later it's in your feed, with a notification. Change the film, crop, download, share an album."],
];
let step = 0, anim = null, paused = false;
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
$("steps").replaceChildren(...STEPS.map((s, i) => {
  const b = document.createElement("button");
  b.setAttribute("aria-label", `${i + 1}`);
  b.innerHTML = "<i><b></b></i>";
  b.onclick = () => { if (anim) { anim.goToAndPlay(i * SCENE + 1, true); if (paused) anim.pause(); } renderStep(i); };
  return b;
}));
function renderStep(i, quiet) {
  if (i === step && !quiet && $("stepText").childElementCount) return;
  step = i;
  const s = STEPS[i];
  $("stepText").innerHTML = `<div class="step-n cap">${T("Шаг", "Step")} ${i + 1} / ${STEPS.length}</div>` +
    `<h3 class="step-t">${esc(T(s[0], s[1]))}</h3><p class="step-p">${esc(T(s[2], s[3]))}</p>`;
  if (!quiet) $("stepText").classList.remove("step-anim"), void $("stepText").offsetWidth, $("stepText").classList.add("step-anim");
}
function progress(frame) {
  const bars = $("steps").querySelectorAll("b");
  bars.forEach((b, i) => (b.style.width = Math.max(0, Math.min(1, (frame - i * SCENE) / SCENE)) * 100 + "%"));
  const i = Math.min(STEPS.length - 1, Math.floor(frame / SCENE));
  if (i !== step) renderStep(i);
}
renderStep(0, true);
if (window.lottie) {
  anim = lottie.loadAnimation({ container: $("stage"), renderer: "svg", loop: true, autoplay: false, path: (window.BETA || {}).guide || "/beta/guide.json",
                                rendererSettings: { preserveAspectRatio: "xMidYMid meet" } });
  anim.addEventListener("enterFrame", (e) => progress(e.currentTime));
  anim.addEventListener("DOMLoaded", () => {
    $("stage").appendChild($("play"));
    if (reduce) { paused = true; anim.goToAndStop(SCENE - 10, true); setPlay(); }
  });
  new IntersectionObserver((es) => { if (!paused) es[0].isIntersecting ? anim.play() : anim.pause(); }, { threshold: .3 }).observe($("stage"));
}
function setPlay() { $("play").textContent = paused ? "▶" : "❚❚"; $("play").setAttribute("aria-label", paused ? "Play" : "Pause"); }
$("play").onclick = () => { paused = !paused; paused ? anim.pause() : anim.play(); setPlay(); };

/* ---------- появление при прокрутке ---------- */
const rv = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); rv.unobserve(e.target); } }), { rootMargin: "0px 0px -8% 0px" });
document.querySelectorAll(".rv").forEach((el, i) => { el.style.transitionDelay = (el.classList.contains("feat") ? (i % 4) * 70 : 0) + "ms"; rv.observe(el); });

/* ---------- слоты ---------- */
let S = null, me = null, joined = null, busy = false;
const left = (sec) => {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return T(`${d} д ${h} ч`, `${d}d ${h}h`);
  if (h) return T(`${h} ч ${m} мин`, `${h}h ${m}m`);
  return T(`${Math.max(1, m)} мин`, `${Math.max(1, m)} min`);
};
const until = (ts) => new Date(ts * 1000).toLocaleString(LANG === "en" ? "en-GB" : "ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json", ...(me ? { "X-Token": me.token } : {}) }, body: JSON.stringify(body) }
                                   : { headers: me ? { "X-Token": me.token } : {} });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { status: r.status });
  return j;
}
async function loadStatus() {
  try { S = await api("/api/beta"); } catch (e) { S = null; }
  renderStatus();
  if (!$("panel").hidden) renderPanel();
}
function renderStatus() {
  if (!S) return;
  const free = S.free;
  $("factSlots").textContent = `${free} / ${S.total}`;
  $("finalCount").textContent = T(`Слоты · занято ${S.total - free} из ${S.total}`, `Slots · ${S.total - free} of ${S.total} taken`);
  const cells = [];
  for (let i = 0; i < Math.min(S.total, 120); i++) {
    const c = document.createElement("i");
    if (i >= S.users.length) c.className = "free";
    cells.push(c);
  }
  $("mini").replaceChildren(...cells);
  document.querySelectorAll("[data-join]").forEach((b) => {
    const mine = joined || me;
    b.querySelector(".ru").textContent = mine ? "Открыть мою Проявку" : free ? "Занять слот в бете" : "Посмотреть слоты";
    b.querySelector(".en").textContent = mine ? "Open my Proyavka" : free ? "Join the beta" : "See the slots";
  });
}
// уже в бете на этом устройстве? — тем же ключом, что и приложение
async function whoAmI() {
  const key = stored(DEV_KEY);
  if (!key) return;
  try { me = await api("/api/auth", { device: key }); } catch (e) { me = null; }
}
function renderPanel() {
  if (!S) { $("pNote").textContent = T("Не удалось получить слоты. Обнови страницу.", "Couldn't load the slots. Reload the page."); return; }
  const mine = joined || (me && me.expires);
  $("pNote").textContent = T(`По ${Math.round(S.slot_mb)} МБ на ${S.days} дн. Занятые — с никами участников и временем, которое у них осталось.`,
                             `${Math.round(S.slot_mb)} MB for ${S.days} days each. Taken slots show the nickname and the time left.`);
  $("stats").innerHTML = `<div><b>${S.free}</b><span class="cap muted">${T("свободно", "free")}</span></div>` +
    `<div><b>${S.total - S.free}</b><span class="cap muted">${T("занято", "taken")}</span></div>` +
    `<div><b>${S.total}</b><span class="cap muted">${T("всего", "total")}</span></div>`;
  const span = S.days * 86400;
  const cells = [];
  S.users.forEach((u, i) => {
    const d = document.createElement("div");
    d.className = "slot" + (u.me ? " me" : "");
    d.style.animationDelay = Math.min(i, 30) * 18 + "ms";
    d.innerHTML = `<span class="no cap">№${String(i + 1).padStart(2, "0")}${u.me ? " · " + T("ты", "you") : ""}</span>` +
      `<span><div class="nick">${esc(u.name)}</div><div class="left">${left(u.left)}</div></span>` +
      `<span class="bar" style="width:${Math.max(2, Math.min(100, (u.left / span) * 100))}%"></span>`;
    cells.push(d);
  });
  for (let i = S.users.length; i < Math.min(S.total, 120); i++) {
    const d = document.createElement("div");
    const next = i === S.users.length && !mine;
    d.className = "slot free" + (next ? " next" : "");
    d.style.animationDelay = Math.min(i, 30) * 18 + "ms";
    d.innerHTML = `<span class="no cap">№${String(i + 1).padStart(2, "0")}</span><span class="nick">${next ? T("твой →", "yours →") : T("свободно", "free")}</span>`;
    if (next) d.onclick = () => $("nick") && $("nick").focus();
    cells.push(d);
  }
  $("slots").replaceChildren(...cells);
  const bottom = $("pBottom");
  if (mine) {
    const name = joined ? joined.name : (S.users.find((u) => u.me) || {}).name || "";
    const exp = joined ? joined.expires : me.expires;
    bottom.innerHTML = `<div class="done"><h4>${joined ? T(`Готово, ${esc(name)}!`, `You're in, ${esc(name)}!`) : T(`Ты уже в бете${name ? " — " + esc(name) : ""}`, `You're already in${name ? " — " + esc(name) : ""}`)}</h4>` +
      `<p>${T(`Слот твой до ${until(exp)}. Камеру подключишь в ☰ → Настройки → Камера; фото с телефона — кнопкой «+ Добавить».`,
              `The slot is yours until ${until(exp)}. Connect the camera in ☰ → Settings → Camera; phone photos — with “+ Add”.`)}</p>` +
      (joined ? `<p class="keep">${T("Вход хранится в этом браузере — почты и пароля нет. Чтобы открыть Проявку на другом устройстве: ☰ → Настройки → «Привязать устройство».",
                                     "Your sign-in lives in this browser — there is no email or password. To open Proyavka on another device: ☰ → Settings → Link a device.") +
                (IOS && !STANDALONE ? " " + T("На iPhone значок на экране «Домой» не видит вход из Safari — привяжи его так же.",
                                              "On iPhone the Home Screen icon does not see the Safari sign-in — link it the same way.") : "")}</p>` : "") +
      `<a class="btn" href="/">${T("Открыть Проявку", "Open Proyavka")} <span class="arr">→</span></a></div>`;
    return;
  }
  if (me) {                 // устройство уже вошло в Проявку (не в бете) — второй аккаунт поверх не заводим
    bottom.innerHTML = `<div class="done"><h4>${T("Это устройство уже вошло в Проявку", "This device is already signed in to Proyavka")}</h4>` +
      `<p>${T("Слот беты не нужен — у тебя уже есть аккаунт.", "You don't need a beta slot — you already have an account.")}</p>` +
      `<a class="btn" href="/">${T("Открыть Проявку", "Open Proyavka")} <span class="arr">→</span></a></div>`;
    return;
  }
  if (!S.free) {
    bottom.innerHTML = `<div class="done"><h4>${T("Свободных слотов нет", "No free slots")}</h4><p>${S.next ? T(`Ближайший освободится через ${left(S.next)}. Загляни позже.`, `The next one frees up in ${left(S.next)}. Check back later.`) : T("Загляни позже.", "Check back later.")}</p></div>`;
    return;
  }
  if (!$("nick")) {
    bottom.innerHTML = `<div class="form"><div class="field"><input id="nick" maxlength="24" autocomplete="nickname" enterkeyhint="go" spellcheck="false">` +
      `<button class="btn acc" id="take"></button></div>` +
      `<label class="agree"><input type="checkbox" id="agree"><span id="agreeText"></span></label>` +
      `<div class="error" id="perr" role="alert"></div><p class="keep" id="iosTip" hidden></p><p class="fine" id="fine"></p></div>`;
    $("take").onclick = take;
    $("nick").onkeydown = (e) => { if (e.key === "Enter") take(); };
  }
  $("agreeText").innerHTML = T(`Мне есть 16 лет, я принимаю <a href="/beta/terms" target="_blank">условия и политику конфиденциальности</a>`,
                               `I am 16 or older and accept the <a href="/beta/terms" target="_blank">terms and privacy policy</a>`);
  $("nick").placeholder = T("Твой ник", "Your nickname");
  if (IOS && !STANDALONE) {         // занять слот лучше сразу в приложении со значка: из Safari вход туда не перейдёт
    $("iosTip").hidden = false;
    $("iosTip").textContent = T("На iPhone: сначала в Safari «Поделиться» → «На экран „Домой“», открой значок и займи слот там — Safari и значок не делят вход. Если страница открыта внутри Telegram или Instagram, этого пункта нет: сперва «Открыть в Safari».",
                                "On iPhone: first in Safari tap Share → Add to Home Screen, open the icon and take the slot there — Safari and the icon don't share the sign-in. Inside Telegram or Instagram that option is missing: choose Open in Safari first.");
  }
  $("take").textContent = T(`Занять №${String(S.users.length + 1).padStart(2, "0")}`, `Take #${String(S.users.length + 1).padStart(2, "0")}`);
  $("fine").textContent = T(`Ник увидят все на этой странице. Через ${S.days} дн. аккаунт и кадры удалятся — скачай нужное заранее.`,
                            `Everyone on this page will see your nickname. After ${S.days} days the account and frames are deleted — download what you need beforehand.`);
}
async function take() {
  if (busy) return;
  const name = $("nick").value.trim();
  $("perr").textContent = "";
  if (name.length < 2) { $("perr").textContent = T("Ник — от 2 до 24 символов", "A nickname is 2 to 24 characters"); return $("nick").focus(); }
  if (!$("agree").checked) { $("perr").textContent = T("Отметь согласие с условиями", "Please accept the terms"); return $("agree").focus(); }
  busy = true;
  $("take").disabled = true;
  try {
    const ua = navigator.userAgent;
    const device = /iPhone|iPad/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Browser";
    const a = await api("/api/beta/join", { name, lang: LANG, device, accept: true });
    stored(DEV_KEY, a.device);
    me = a;
    joined = { name, expires: a.expires || Date.now() / 1000 + S.days * 86400 };
    await loadStatus();
    renderStatus();
  } catch (e) {
    $("perr").textContent = e.message;
    if (e.status === 403) loadStatus();
  } finally {
    busy = false;
    if ($("take")) $("take").disabled = false;
  }
}
let lastFocus = null;
function openPanel() {
  if (joined || me) { location.href = "/"; return; }
  lastFocus = document.activeElement;
  $("scrim").hidden = $("panel").hidden = false;
  renderPanel();
  requestAnimationFrame(() => { $("scrim").classList.add("open"); $("panel").classList.add("open"); });
  document.body.style.overflow = "hidden";
  loadStatus();
  setTimeout(() => { if ($("nick") && !matchMedia("(pointer: coarse)").matches) $("nick").focus(); }, 350);
}
function closePanel() {
  $("scrim").classList.remove("open"); $("panel").classList.remove("open");
  document.body.style.overflow = "";
  setTimeout(() => { $("scrim").hidden = $("panel").hidden = true; }, 380);
  if (lastFocus) lastFocus.focus();
}
document.querySelectorAll("[data-join]").forEach((b) => (b.onclick = openPanel));
$("close").onclick = closePanel;
$("scrim").onclick = closePanel;
addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("panel").hidden) closePanel(); });
if (location.hash === "#join") openPanel();

setLang(LANG);
whoAmI().then(loadStatus);
setInterval(() => { if (!document.hidden) loadStatus(); }, 30000);
