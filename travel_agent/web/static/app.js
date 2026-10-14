// Escapade — front sans build : routage côté navigateur, appels à /api, rafraîchissement régulier.
// Tout texte venant de l'API (prénoms, contenus du LLM) passe par textContent, jamais innerHTML.

const POLL_MS = 3000;
const STYLES = ["Aventure", "Détente", "Culture", "Fête"];
const main = document.getElementById("main");
const toastEl = document.getElementById("toast");

// --- Utilitaires -----------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else if (key === "checked") el.checked = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

function svg(tag, attrs, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) el.setAttribute(key, value);
  append(el, children);
  return el;
}

const euroFormat = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
const eur = (amount) => `${euroFormat.format(Math.round(amount))} €`;
const percentFormat = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const dayFormat = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short" });
const fullDateFormat = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long", year: "numeric" });
const parseDate = (iso) => new Date(`${iso}T12:00:00`);
const shortDate = (iso) => dayFormat.format(parseDate(iso));
const initial = (name) => ([...name.trim()][0] || "?").toUpperCase();
const plural = (count, one, many) => `${count} ${count > 1 ? many : one}`;
const safeUrl = (url) => (/^https?:\/\//i.test(url) ? url : null);
const hostname = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } };

function storage(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key));
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* stockage indisponible (navigation privée…) : on s'en passe */ }
  return null;
}

let toastTimer;
function toast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.className = isError ? "toast error" : "toast";
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 4500);
}

// --- API ---------------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "Pas de connexion. Réessaie dans un instant.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : "Certains champs ne sont pas valides.";
    throw new ApiError(response.status, message);
  }
  return data;
}

// --- Routage et rendu -------------------------------------------------------------------------------

function navigate(path) {
  history.pushState({}, "", path);
  route();
}

window.addEventListener("popstate", () => route());

function route() {
  stopPolling();
  const match = location.pathname.match(/^\/t\/([A-Za-z0-9_-]+)\/?$/);
  if (match) openTrip(match[1]);
  else renderHome();
}

// Remplace le contenu en gardant le focus (et la position du curseur) sur le même élément.
function mount(...nodes) {
  const active = document.activeElement;
  const focus = active?.id && main.contains(active)
    ? { id: active.id, start: active.selectionStart, end: active.selectionEnd }
    : null;
  main.replaceChildren(...nodes.flat(Infinity).filter((node) => node != null && node !== false));
  if (!focus) return;
  const el = document.getElementById(focus.id);
  if (!el) return;
  el.focus({ preventScroll: true });
  try { if (focus.start != null) el.setSelectionRange(focus.start, focus.end); } catch { /* type sans sélection */ }
}

function topbar(tripName) {
  return h("header", { class: "topbar" },
    h("a", { class: "brand", href: "/", onclick: (e) => { e.preventDefault(); navigate("/"); } }, "Escapade"),
    tripName ? h("span", { class: "trip-name" }, tripName) : null);
}

function avatar(member, { size = "", pending = false, check = false } = {}) {
  const classes = ["avatar", size, pending && "pending", check && "check"].filter(Boolean).join(" ");
  return h("span", { class: classes, style: pending ? null : `background:${member.color}`, title: member.name, "aria-hidden": "true" },
    initial(member.name));
}

function button(label, onclick, { kind = "", id = null, busy = false, block = false, small = false, type = "button" } = {}) {
  const classes = ["btn", kind, block && "block", small && "small"].filter(Boolean).join(" ");
  return h("button", { class: classes, type, id, onclick: busy ? null : onclick, "aria-disabled": busy ? "true" : null },
    busy ? "Un instant…" : label);
}

// --- Accueil : créer un voyage ------------------------------------------------------------------------

function renderHome() {
  document.title = "Escapade";
  const recent = storage("escapade_trips") || [];
  let busy = false;

  async function create(event) {
    event.preventDefault();
    if (busy) return;
    const form = event.currentTarget;
    const tripName = form.elements.trip_name.value.trim();
    const organizerName = form.elements.organizer_name.value.trim();
    if (!tripName || !organizerName) {
      toast("Donne un nom au voyage et ton prénom.", true);
      (tripName ? form.elements.organizer_name : form.elements.trip_name).focus();
      return;
    }
    busy = true;
    form.querySelector("button[type=submit]").textContent = "Un instant…";
    try {
      const view = await api("POST", "/trips", { trip_name: tripName, organizer_name: organizerName });
      rememberTrip(view);
      navigate(`/t/${view.id}`);
    } catch (error) {
      toast(error.message, true);
      busy = false;
      form.querySelector("button[type=submit]").textContent = "Créer le voyage →";
    }
  }

  mount(
    topbar(),
    h("section", { class: "stack narrow" },
      h("p", { class: "eyebrow" }, "Nouveau voyage"),
      h("h1", {}, "On part où,", h("br"), "la team ?"),
      h("p", { class: "lead" }, "Chacun donne ses envies, l'IA propose trois pays, le groupe vote. Pas de compte : un lien suffit.")),
    h("form", { class: "card pop stack narrow", onsubmit: create, novalidate: true },
      h("div", { class: "field" },
        h("label", { for: "trip_name" }, "Nom du voyage"),
        h("input", { id: "trip_name", name: "trip_name", type: "text", maxlength: "80", placeholder: "Été 2027 entre potes", autocomplete: "off", required: true })),
      h("div", { class: "field" },
        h("label", { for: "organizer_name" }, "Ton prénom"),
        h("input", { id: "organizer_name", name: "organizer_name", type: "text", maxlength: "40", autocomplete: "given-name", required: true })),
      h("button", { class: "btn primary block", type: "submit" }, "Créer le voyage →")),
    recent.length
      ? h("section", { class: "stack narrow" },
          h("h3", {}, "Tes voyages"),
          h("ul", { class: "member-list" }, recent.map((trip) =>
            h("li", {}, h("a", { href: `/t/${trip.id}`, onclick: (e) => { e.preventDefault(); navigate(`/t/${trip.id}`); } }, trip.name)))))
      : null,
  );
}

function rememberTrip(view) {
  const trips = (storage("escapade_trips") || []).filter((trip) => trip.id !== view.id);
  storage("escapade_trips", [{ id: view.id, name: view.name }, ...trips].slice(0, 8));
}

// --- Page d'un voyage ------------------------------------------------------------------------------------

const trip = { id: null, view: null, json: "", timer: null, ui: null };

function freshUi() {
  return { busy: false, editing: false, draft: null, formError: null, showErrors: false, vote: null, feedback: "", showFeedback: false, adjust: "", tab: "days" };
}

async function openTrip(id) {
  Object.assign(trip, { id, view: null, json: "", ui: freshUi() });
  mount(topbar(), h("p", { class: "loading" }, "Chargement du voyage…"));
  await refresh();
  schedulePoll();
}

function stopPolling() {
  clearTimeout(trip.timer);
  trip.timer = null;
}

function schedulePoll() {
  stopPolling();
  if (!trip.view || trip.view.status === "done") return;
  trip.timer = setTimeout(async () => {
    if (!document.hidden) await refresh();
    schedulePoll();
  }, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && trip.id && location.pathname.startsWith("/t/")) refresh().then(schedulePoll);
});

async function refresh() {
  try {
    setView(await api("GET", `/trips/${trip.id}`));
  } catch (error) {
    if (error.status === 404) {
      stopPolling();
      mount(topbar(), h("section", { class: "card stack" },
        h("h2", {}, "Voyage introuvable"),
        h("p", {}, "Ce lien ne correspond à aucun voyage. Vérifie qu'il est complet."),
        button("Créer un voyage", () => navigate("/"), { kind: "primary" })));
    } else if (error.status === 0 && !trip.view) {
      mount(topbar(), screenOffline());
    } else if (trip.view) {
      toast(error.message, true);
    }
  }
}

function setView(view, force = false) {
  const json = JSON.stringify(view);
  if (!force && json === trip.json) return;
  trip.json = json;
  trip.view = view;
  if (view.me) rememberTrip(view);
  renderTrip();
}

// Lance une action ; la vue renvoyée par l'API remplace la vue courante.
async function act(request, { success } = {}) {
  if (trip.ui.busy) return;
  trip.ui.busy = true;
  renderTrip();
  try {
    const view = await request();
    trip.ui.busy = false;
    setView(view, true);
    if (success) toast(success);
    schedulePoll();
    return view;
  } catch (error) {
    trip.ui.busy = false;
    renderTrip();
    toast(error.message, true);
    throw error;
  }
}

function renderTrip() {
  const view = trip.view;
  if (!view) return;
  document.title = `${view.name} · Escapade`;
  let screen;
  if (!view.me) screen = view.status === "collecting" ? screenJoin(view) : screenClosed(view);
  else if (view.status === "collecting") screen = screenCollecting(view);
  else if (view.status === "generating" || view.status === "planning") screen = screenWorking(view);
  else if (view.status === "voting") screen = screenVoting(view);
  else if (view.status === "reviewing" || view.status === "done") screen = screenPlan(view);
  else screen = screenError(view);
  mount(topbar(view.name), screen);
}

const organizerOf = (view) => view.members.find((m) => m.is_organizer);

// --- Hors ligne -------------------------------------------------------------------------------------------

function screenOffline() {
  return h("section", { class: "card stack narrow", role: "alert" },
    h("h2", {}, "Tu es hors ligne"),
    h("p", {}, "Le voyage s'affichera dès que la connexion reviendra. Tes réponses déjà envoyées sont gardées."),
    button("Réessayer", () => refresh().then(schedulePoll), { kind: "dark" }));
}

window.addEventListener("online", () => {
  if (trip.id && location.pathname.startsWith("/t/")) refresh().then(schedulePoll);
});

// --- Rejoindre --------------------------------------------------------------------------------------------

function screenJoin(view) {
  const organizer = organizerOf(view);
  async function join(event) {
    event.preventDefault();
    const name = event.currentTarget.elements.name.value.trim();
    if (!name) {
      toast("Indique ton prénom pour rejoindre.", true);
      return;
    }
    await act(() => api("POST", `/trips/${view.id}/members`, { name }), { success: "Bienvenue dans la bande !" }).catch(() => {});
  }
  return [
    h("section", { class: "stack narrow" },
      h("p", { class: "eyebrow" }, `${organizer?.name || "Quelqu'un"} t'invite`),
      h("h1", {}, view.name),
      h("div", { class: "row" },
        h("div", { class: "avatars" }, view.members.map((m) => avatar(m))),
        h("span", { class: "muted" }, `Déjà dans la bande · ${view.members.length}`))),
    h("form", { class: "card pop stack narrow", onsubmit: join, novalidate: true },
      h("div", { class: "field" },
        h("label", { for: "join-name" }, "Ton prénom"),
        h("input", { id: "join-name", name: "name", type: "text", maxlength: "40", autocomplete: "given-name", required: true })),
      h("button", { class: "btn primary block", type: "submit", "aria-disabled": trip.ui.busy ? "true" : null },
        trip.ui.busy ? "Un instant…" : "Rejoindre la bande"),
      h("p", { class: "small muted" }, "Pas de compte : ce navigateur se souviendra de toi.")),
  ];
}

function screenClosed(view) {
  return h("section", { class: "card stack narrow" },
    h("h2", {}, "Ce voyage a déjà commencé"),
    h("p", {}, "Il n'est plus possible de le rejoindre."),
    h("p", { class: "muted" }, "Tu en fais partie ? Ouvre le lien depuis le navigateur que tu as utilisé la première fois : c'est lui qui se souvient de toi."));
}

// --- Étapes 1 et 2 : invitation, envies, attente -------------------------------------------------------

const inviteUrl = (view) => `${location.origin}/t/${view.id}`;

async function copyInvite(view) {
  try {
    await navigator.clipboard.writeText(inviteUrl(view));
    toast("Lien copié !");
  } catch {
    toast(`Copie impossible ici : ${inviteUrl(view)}`, true);
  }
}

async function shareInvite(view, text) {
  try {
    await navigator.share({ title: view.name, text, url: inviteUrl(view) });
  } catch (error) {
    if (error?.name !== "AbortError") copyInvite(view);
  }
}

function inviteCard(view) {
  const canShare = typeof navigator.share === "function";
  return h("section", { class: "card stack", "aria-labelledby": "invite-title" },
    h("h3", { id: "invite-title" }, "Ton lien d'invitation"),
    h("div", { class: "invite-link" },
      h("code", {}, inviteUrl(view).replace(/^https?:\/\//, "")),
      button("Copier", () => copyInvite(view), { small: true, kind: "dark" })),
    canShare ? button("Partager le lien", () => shareInvite(view, `Rejoins « ${view.name} » sur Escapade !`), { block: true }) : null,
    h("p", { class: "small muted" }, "Pas de compte requis pour rejoindre."));
}

function progressCard(view) {
  const answered = view.members.filter((m) => m.submitted).length;
  const total = view.members.length;
  return h("section", { class: "card soft stack", "aria-labelledby": "progress-title" },
    h("div", { class: "progress-strip" },
      h("div", { class: "avatars" }, view.members.map((m) => avatar(m, { pending: !m.submitted, check: m.submitted }))),
      h("strong", { id: "progress-title" }, `${answered} sur ${total} ${answered > 1 ? "ont" : "a"} répondu`)),
    h("div", { class: "meter", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(answered), "aria-label": "Réponses reçues" },
      h("span", { style: `width:${total ? (answered / total) * 100 : 0}%` })),
    view.group_budget_range_eur
      ? h("p", {}, "Budget du groupe : ", h("strong", {}, `${eur(view.group_budget_range_eur[0])} – ${eur(view.group_budget_range_eur[1])}`), " / pers.")
      : h("p", { class: "small muted" }, "La fourchette de budget du groupe s'affiche à partir de 3 réponses, arrondie à 100 €."));
}

function memberList(view) {
  return h("ul", { class: "member-list", "aria-label": "Membres du voyage" }, view.members.map((m) =>
    h("li", {}, avatar(m), h("span", {}, m.name, m.id === view.me?.id ? " (toi)" : ""),
      m.is_organizer ? h("span", { class: "badge outline" }, "organise") : null,
      h("span", { class: `badge ${m.submitted ? "mint" : "outline"}` }, m.submitted ? "a répondu" : "en attente"))));
}

const DRAFT_FIELDS = [
  ["country", "Pays rêvé", "pref-country"],
  ["budget", "Budget / pers.", "pref-budget"],
  ["start", "Date de départ", "pref-start"],
  ["end", "Date de retour", "pref-end"],
  ["origin", "Ville de départ", "pref-origin"],
];

function draftFor(view) {
  if (trip.ui.draft) return trip.ui.draft;
  const prefs = view.me.preferences;
  trip.ui.draft = prefs
    ? {
        country: prefs.preferred_country,
        budget: String(prefs.budget_eur),
        start: prefs.start_date,
        end: prefs.end_date,
        styles: (prefs.travel_style || "").split(",").map((s) => s.trim()).filter(Boolean),
        origin: prefs.origin_city || "",
      }
    : storage(`escapade_draft_${view.id}`) || { country: "", budget: "", start: "", end: "", styles: [], origin: "" };
  return trip.ui.draft;
}

// Champs manquants ou invalides, dans l'ordre du formulaire.
function draftProblems(draft) {
  const problems = {};
  for (const [key, label] of DRAFT_FIELDS) if (!String(draft[key] || "").trim()) problems[key] = `${label} : à remplir.`;
  if (!problems.budget && !(Number(draft.budget) > 0)) problems.budget = "Le budget doit être un montant positif.";
  const today = new Date().toISOString().slice(0, 10);
  if (!problems.start && draft.start <= today) problems.start = "La date de départ doit être dans le futur.";
  if (!problems.end && !problems.start && draft.end <= draft.start) problems.end = "Le retour doit suivre le départ.";
  return problems;
}

function submitLabel(problems) {
  const count = Object.keys(problems).length;
  return count ? `Encore ${plural(count, "champ", "champs")}` : "Envoyer mes envies";
}

function preferencesForm(view) {
  const draft = draftFor(view);
  const problems = draftProblems(draft);
  const show = trip.ui.showErrors;

  function update(key, value) {
    draft[key] = value;
    storage(`escapade_draft_${view.id}`, draft);
    const submit = document.getElementById("submit-prefs");
    const current = draftProblems(draft);
    if (submit) {
      submit.textContent = submitLabel(current);
      submit.classList.toggle("incomplete", Object.keys(current).length > 0);
    }
  }

  async function submit(event) {
    event.preventDefault();
    const current = draftProblems(draft);
    const firstProblem = DRAFT_FIELDS.find(([key]) => current[key]);
    if (firstProblem) {
      trip.ui.showErrors = true;
      renderTrip();
      const field = document.getElementById(firstProblem[2]);
      field?.scrollIntoView({ block: "center", behavior: "smooth" });
      field?.focus({ preventScroll: true });
      return;
    }
    trip.ui.formError = null;
    try {
      await act(() => api("PUT", `/trips/${view.id}/preferences`, {
        preferred_country: draft.country.trim(),
        budget_eur: Number(draft.budget),
        start_date: draft.start,
        end_date: draft.end,
        travel_style: draft.styles.join(", ") || null,
        origin_city: draft.origin.trim(),
      }), { success: "Tes envies sont envoyées !" });
      trip.ui.editing = false;
      trip.ui.showErrors = false;
      storage(`escapade_draft_${view.id}`, null);
      renderTrip();
    } catch (error) {
      trip.ui.formError = error.message;
      renderTrip();
    }
  }

  const field = (key, label, input, extra = null) => {
    const error = show && problems[key];
    const id = input.id || input.querySelector("input").id;  // le budget est enveloppé (symbole €)
    return h("div", { class: error ? "field invalid" : "field" },
      h("label", { for: id }, h("span", {}, label, h("span", { class: "req", "aria-hidden": "true" }, " *")), extra),
      input,
      error ? h("p", { class: "field-error", id: `${id}-error` }, problems[key]) : null);
  };
  const input = (key, props) => {
    const error = show && problems[key];
    return h("input", {
      ...props, value: draft[key], required: true, "aria-invalid": error ? "true" : null,
      "aria-describedby": error ? `${props.id}-error` : null,
      oninput: (e) => update(key, e.target.value),
    });
  };

  return h("form", { class: "card pop stack", onsubmit: submit, novalidate: true, "aria-labelledby": "prefs-title" },
    h("h2", { id: "prefs-title" }, `Tes envies, ${view.me.name}`),
    trip.ui.formError ? h("p", { class: "field-error", role: "alert" }, trip.ui.formError) : null,
    field("country", "Pays rêvé", input("country", { id: "pref-country", type: "text", maxlength: "60", placeholder: "Portugal", autocomplete: "country-name" })),
    field("budget", "Budget / pers.", h("div", { class: "money" }, input("budget", { id: "pref-budget", type: "number", min: "1", step: "10", inputmode: "numeric", placeholder: "ex. 800" })),
      h("span", { class: "badge" }, "Visible par toi seul·e")),
    h("p", { class: "small muted" }, "Vols compris. Les autres ne voient jamais ton budget, seulement une fourchette arrondie du groupe."),
    h("fieldset", { class: "field", style: "border:0;padding:0;margin:0" },
      h("legend", { class: "label", style: "font-weight:600;margin-bottom:6px;padding:0" }, "Dates"),
      h("div", { class: "dates" },
        field("start", "Départ", input("start", { id: "pref-start", type: "date" })),
        field("end", "Retour", input("end", { id: "pref-end", type: "date" })))),
    h("div", { class: "field" },
      h("span", { class: "label", id: "styles-label" }, h("span", {}, "Ton style"), h("span", { class: "hint" }, "optionnel")),
      h("div", { class: "chips", role: "group", "aria-labelledby": "styles-label" }, STYLES.map((style) =>
        h("button", {
          type: "button", class: "chip", id: `style-${style}`, "aria-pressed": draft.styles.includes(style) ? "true" : "false",
          onclick: () => {
            draft.styles = draft.styles.includes(style) ? draft.styles.filter((s) => s !== style) : [...draft.styles, style];
            update("styles", draft.styles);
            renderTrip();
          },
        }, style)))),
    field("origin", "Départ de", input("origin", { id: "pref-origin", type: "text", maxlength: "60", placeholder: "Lyon", autocomplete: "address-level2" })),
    h("button", {
      class: `btn primary block${Object.keys(problems).length ? " incomplete" : ""}`, type: "submit", id: "submit-prefs",
      "aria-disabled": trip.ui.busy ? "true" : null,
    }, trip.ui.busy ? "Un instant…" : submitLabel(problems)),
    view.me.submitted ? button("Annuler", () => { trip.ui.editing = false; trip.ui.draft = null; trip.ui.showErrors = false; renderTrip(); }, { kind: "ghost", block: true }) : null);
}

function mySummary(view) {
  const prefs = view.me.preferences;
  return h("section", { class: "card mint stack", "aria-labelledby": "summary-title" },
    h("h2", { id: "summary-title" }, "Tes envies sont envoyées ✓"),
    h("dl", { class: "stack", style: "margin:0;gap:6px" },
      h("div", {}, h("dt", { class: "visually-hidden" }, "Pays"), h("dd", { style: "margin:0" }, h("strong", {}, prefs.preferred_country), ` · départ de ${prefs.origin_city}`)),
      h("div", {}, h("dt", { class: "visually-hidden" }, "Dates"), h("dd", { style: "margin:0" }, `Du ${shortDate(prefs.start_date)} au ${shortDate(prefs.end_date)}`)),
      h("div", {}, h("dt", { class: "visually-hidden" }, "Budget"), h("dd", { style: "margin:0" }, `${eur(prefs.budget_eur)} / pers. `, h("span", { class: "badge" }, "rien que pour toi"))),
      prefs.travel_style ? h("div", {}, h("dt", { class: "visually-hidden" }, "Style"), h("dd", { style: "margin:0" }, prefs.travel_style)) : null),
    button("Modifier mes envies", () => { trip.ui.editing = true; trip.ui.draft = null; renderTrip(); }, { small: true }));
}

function waitingCard(view) {
  const pending = view.members.filter((m) => !m.submitted);
  const organizer = organizerOf(view);
  const isOrganizer = view.me.is_organizer;
  const launch = () => act(() => api("POST", `/trips/${view.id}/launch`)).catch(() => {});

  return h("section", { class: "card stack", "aria-labelledby": "waiting-title" },
    pending.length
      ? [
          h("h2", { id: "waiting-title" }, pending.length === 1 ? "Plus qu'un·e !" : `Plus que ${pending.length} !`),
          h("div", { class: "row" }, h("div", { class: "avatars" }, pending.map((m) => avatar(m, { pending: true, size: "lg" }))),
            h("span", {}, pending.map((m) => m.name).join(", "))),
          button(`Relancer ${pending.length === 1 ? pending[0].name : "les retardataires"}`,
            () => shareInvite(view, `On n'attend plus que tes envies pour « ${view.name} » !`), { block: true }),
        ]
      : h("h2", { id: "waiting-title" }, "Toute la bande a répondu !"),
    isOrganizer
      ? [
          button("Lancer l'IA", launch, { kind: "primary", block: true, busy: trip.ui.busy, id: "launch" }),
          pending.length
            ? h("p", { class: "small muted" }, `Tu peux lancer sans attendre : ${pending.map((m) => m.name).join(", ")} ne ${pending.length > 1 ? "seront" : "sera"} pas pris en compte et ne pourra plus rejoindre.`)
            : h("p", { class: "small muted" }, "L'IA va proposer trois pays au groupe (1 à 3 minutes)."),
        ]
      : h("p", { class: "muted" }, `${organizer?.name || "L'organisateur·rice"} lancera l'IA quand la bande sera prête.`));
}

function screenCollecting(view) {
  const editing = !view.me.submitted || trip.ui.editing;
  return h("div", { class: "split" },
    h("div", { class: "aside stack" },
      h("div", { class: "stack" },
        h("p", { class: "eyebrow" }, "Étape 1 · La bande"),
        h("h1", {}, view.me.submitted ? "On attend la bande" : "On part où ?")),
      progressCard(view),
      inviteCard(view),
      h("details", {}, h("summary", {}, `Membres (${view.members.length})`), memberList(view))),
    h("div", { class: "stack" },
      h("p", { class: "eyebrow" }, "Étape 2 · Tes envies"),
      editing ? preferencesForm(view) : [mySummary(view), waitingCard(view)]));
}

// --- Génération en cours --------------------------------------------------------------------------------

function winnerOf(results) {
  if (!results) return null;
  const best = Math.max(...Object.values(results));
  const winners = Object.keys(results).filter((country) => results[country] === best);
  return winners.length === 1 ? winners[0] : null;
}

function resultsView(vote) {
  if (!vote?.results) return null;
  const total = Math.max(1, ...Object.values(vote.results));
  const winner = winnerOf(vote.results);
  return h("div", { class: "results", role: "list", "aria-label": "Résultats du vote" },
    Object.entries(vote.results).map(([country, count]) =>
      h("div", { class: `result-row${country === winner ? " winner" : ""}`, role: "listitem" },
        h("span", {}, country),
        h("span", { class: "result-bar", "aria-hidden": "true" }, h("span", { style: `width:${(count / total) * 100}%` })),
        h("span", {}, h("span", { class: "visually-hidden" }, ": "), String(count), h("span", { class: "visually-hidden" }, " voix")))));
}

function screenWorking(view) {
  const planning = view.status === "planning";
  const steps = view.progress || [];
  const activeIndex = steps.findIndex((step) => !step.done);
  const country = winnerOf(view.vote?.results);
  return h("div", { class: "stack narrow", style: "margin:0 auto;width:100%" },
    h("p", { class: "eyebrow" }, planning ? "Étape 5 · Le plan" : "Étape 3 · Les idées"),
    h("h1", {}, planning ? `Cap sur ${country || "votre destination"} !` : `On mélange vos ${plural(view.vote?.voters_total || view.responses, "envie", "envies")}…`),
    h("section", { class: "card pop stack", "aria-labelledby": "steps-title" },
      h("h3", { id: "steps-title" }, planning ? "On prépare le voyage" : "L'IA cherche des idées"),
      h("ol", { class: "steps", "aria-live": "polite" }, steps.map((step, index) => {
        const state = step.done ? "done" : index === activeIndex ? "active" : "todo";
        return h("li", { class: state },
          h("span", { class: "dot", "aria-hidden": "true" }, step.done ? "✓" : ""),
          h("span", {}, step.label, h("span", { class: "visually-hidden" }, state === "done" ? " : fait" : state === "active" ? " : en cours" : " : à venir")));
      })),
      h("p", { class: "small muted" }, "Durée estimée : 1 à 3 minutes. Tu peux fermer la page : ton lien te ramènera ici.")),
    planning && view.vote?.results ? h("section", { class: "card soft stack" }, h("h3", {}, "Le vote a parlé"), resultsView(view.vote)) : null);
}

// --- Étape 3 : suggestions ----------------------------------------------------------------------------------

// Graphique « Atlas » : budgets estimés sur une même échelle, avec la fourchette du groupe en bande.
function dotplot(suggestions, range) {
  // Dessiné à la largeur réelle du conteneur : le texte garde sa taille sur mobile comme sur desktop.
  const width = Math.max(300, Math.min(main.clientWidth - 48, 760));
  const left = 92, right = 64, rowHeight = 42, top = range ? 30 : 10;
  const height = top + rowHeight * suggestions.length + 30;
  const values = suggestions.map((s) => s.estimated_budget_eur);
  const low = Math.min(...values, range ? range[0] : Infinity);
  const high = Math.max(...values, range ? range[1] : -Infinity);
  const pad = Math.max((high - low) * 0.15, 100);
  const min = Math.max(0, Math.floor((low - pad) / 100) * 100);
  const max = Math.ceil((high + pad) / 100) * 100;
  const x = (value) => left + ((value - min) / (max - min)) * (width - left - right);
  const summary = suggestions.map((s) => `${s.country} ${eur(s.estimated_budget_eur)}`).join(", ")
    + (range ? `. Fourchette du groupe : ${eur(range[0])} à ${eur(range[1])}.` : ".");

  return svg("svg", { class: "dotplot", width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `Budget estimé par personne : ${summary}` },
    range ? svg("rect", { x: x(range[0]), y: top - 8, width: Math.max(2, x(range[1]) - x(range[0])), height: height - top - 22, rx: 8, fill: "#D8F5EA", stroke: "#1F7A5C", "stroke-dasharray": "4 3" }) : null,
    range ? svg("text", { class: "axis", x: (x(range[0]) + x(range[1])) / 2, y: top - 14, "text-anchor": "middle" }, "fourchette du groupe") : null,
    suggestions.map((s, i) => {
      const y = top + rowHeight * i + rowHeight / 2;
      return svg("g", {},
        svg("text", { x: 0, y: y + 5 }, s.country.length > 12 ? `${s.country.slice(0, 11)}…` : s.country),
        svg("line", { x1: left, x2: width - right, y1: y, y2: y, stroke: "rgba(30,27,46,.18)", "stroke-width": 2 }),
        svg("circle", { cx: x(s.estimated_budget_eur), cy: y, r: 9, fill: "#FF6B4A", stroke: "#1E1B2E", "stroke-width": 2 }),
        svg("text", { x: x(s.estimated_budget_eur) + 14, y: y + 5 }, eur(s.estimated_budget_eur)));
    }),
    svg("text", { class: "axis", x: left, y: height - 6, "text-anchor": "start" }, eur(min)),
    svg("text", { class: "axis", x: width - right, y: height - 6, "text-anchor": "end" }, eur(max)));
}

function budgetBadge(suggestion, range) {
  if (range) {
    if (suggestion.in_group_range) return h("span", { class: "badge mint" }, "dans la fourchette");
    return suggestion.estimated_budget_eur < range[0]
      ? h("span", { class: "badge mint" }, "sous la fourchette")
      : h("span", { class: "badge coral" }, "au-dessus de la fourchette");
  }
  return suggestion.fits_everyone_budget
    ? h("span", { class: "badge mint" }, "dans le budget de chacun")
    : h("span", { class: "badge coral" }, "trop cher pour quelqu'un");
}

function countryCard(suggestion, range, index) {
  const sources = suggestion.sources.map(safeUrl).filter(Boolean);
  return h("article", { class: "card country-card", id: `country-${index}`, "aria-labelledby": `country-${index}-name`, tabindex: "-1" },
    h("h3", { id: `country-${index}-name` }, suggestion.country),
    h("div", {}, budgetBadge(suggestion, range)),
    h("p", { class: "price" }, eur(suggestion.estimated_budget_eur), h("small", {}, " / pers. estimé")),
    h("p", { class: "muted small" }, h("strong", {}, "Quand partir : "), suggestion.best_period),
    suggestion.pros.length ? h("ul", { class: "pros", "aria-label": "Points forts" }, suggestion.pros.map((p) => h("li", {}, p))) : null,
    suggestion.cons.length ? h("ul", { class: "cons", "aria-label": "Points faibles" }, suggestion.cons.map((c) => h("li", {}, c))) : null,
    sources.length
      ? h("details", {}, h("summary", {}, plural(sources.length, "source", "sources")),
          h("ul", { class: "sources" }, sources.map((url) => h("li", {}, h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, hostname(url))))))
      : null);
}

function moreIdeas(view) {
  if (!view.me.is_organizer) return null;
  if (!trip.ui.showFeedback) {
    return button("Aucune ne plaît ? Autres idées", () => { trip.ui.showFeedback = true; renderTrip(); document.getElementById("feedback")?.focus(); }, { kind: "ghost" });
  }
  async function send(event) {
    event.preventDefault();
    const feedback = trip.ui.feedback.trim();
    if (!feedback) {
      toast("Dis à l'IA ce qui ne va pas.", true);
      return;
    }
    await act(() => api("POST", `/trips/${view.id}/more-ideas`, { feedback })).catch(() => {});
    trip.ui.feedback = "";
    trip.ui.showFeedback = false;
  }
  return h("form", { class: "card soft stack", onsubmit: send, novalidate: true },
    h("div", { class: "field" },
      h("label", { for: "feedback" }, "Qu'est-ce qui ne va pas ?"),
      h("textarea", { id: "feedback", maxlength: "500", placeholder: "Trop cher, on préfère la montagne…", value: trip.ui.feedback, oninput: (e) => { trip.ui.feedback = e.target.value; } })),
    h("p", { class: "small muted" }, "Les votes en cours sont remis à zéro et l'IA propose trois autres pays."),
    h("div", { class: "actions inline" },
      h("button", { class: "btn dark", type: "submit", "aria-disabled": trip.ui.busy ? "true" : null }, trip.ui.busy ? "Un instant…" : "Demander d'autres idées"),
      button("Annuler", () => { trip.ui.showFeedback = false; renderTrip(); }, { kind: "ghost" })));
}

// --- Étape 4 : vote -----------------------------------------------------------------------------------------

function voteCard(view) {
  const vote = view.vote;
  const organizer = organizerOf(view);
  const isOrganizer = view.me.is_organizer;

  if (vote.tie.length) {
    const tieScore = vote.tie.map((country) => `${country} ${vote.results?.[country] ?? 0}`).join(" – ");
    return h("section", { class: "card sun stack", id: "vote", "aria-labelledby": "vote-title" },
      h("h2", { id: "vote-title" }, "Égalité !"),
      h("p", { class: "big-number", style: "font-size:1.6rem" }, tieScore),
      resultsView(vote),
      isOrganizer
        ? h("div", { class: "actions" },
            button(`Revote éclair (${vote.tie.length} options)`, () => act(() => api("POST", `/trips/${view.id}/tie`, { method: "revote" })).catch(() => {}), { kind: "dark", block: true, busy: trip.ui.busy }),
            button("Laisser le dé décider", () => act(() => api("POST", `/trips/${view.id}/tie`, { method: "random" })).catch(() => {}), { block: true, busy: trip.ui.busy }))
        : h("p", {}, `${organizer?.name || "L'organisateur·rice"} va trancher : second tour ou tirage au sort.`));
  }

  if (!view.me.is_participant) {
    return h("section", { class: "card soft stack", id: "vote" },
      h("h2", {}, "Le vote"),
      h("p", {}, `${vote.votes_cast} sur ${vote.voters_total} ont voté.`),
      h("p", { class: "muted" }, "Tu n'avais pas donné tes envies avant le lancement : tu ne votes pas cette fois."));
  }

  const selected = trip.ui.vote ?? vote.my_vote;
  const changed = selected && selected !== vote.my_vote;
  async function submit(event) {
    event.preventDefault();
    if (!selected) {
      toast("Choisis un pays.", true);
      return;
    }
    await act(() => api("POST", `/trips/${view.id}/votes`, { country: selected }), { success: "Vote enregistré !" }).catch(() => {});
    trip.ui.vote = null;
  }

  return h("form", { class: "card pop stack", id: "vote", onsubmit: submit, "aria-labelledby": "vote-title" },
    h("div", { class: "progress-strip" },
      h("h2", { id: "vote-title" }, vote.round > 1 ? "Second tour !" : "Ton vote, ta voix."),
      h("span", { class: "badge lilac" }, `${vote.votes_cast}/${vote.voters_total} ont voté`)),
    h("div", { class: "avatars", "aria-hidden": "true" }, view.members.filter((m) => m.submitted).map((m) => avatar(m, { pending: !m.voted, check: m.voted }))),
    h("fieldset", { class: "vote-options" },
      h("legend", { class: "visually-hidden" }, "Choisis un pays"),
      vote.options.map((country, index) => {
        const suggestion = view.suggestions?.find((s) => s.country === country);
        return h("div", { class: "vote-option" },
          h("input", {
            type: "radio", name: "vote", id: `vote-${index}`, value: country, checked: selected === country,
            onchange: () => { trip.ui.vote = country; renderTrip(); },
          }),
          h("label", { for: `vote-${index}` },
            h("span", {}, h("span", { class: "name" }, country), h("br"),
              suggestion ? h("span", { class: "small muted" }, `${eur(suggestion.estimated_budget_eur)} · ${suggestion.best_period.split(/[.:;]/)[0].slice(0, 40)}`) : null),
            h("span", { class: "tick", "aria-hidden": "true" }, selected === country ? "✓" : "")));
      })),
    h("p", { class: "small muted" }, "Les résultats restent cachés jusqu'au dernier vote. Tu peux changer d'avis d'ici là."),
    h("button", { class: "btn primary block", type: "submit", "aria-disabled": trip.ui.busy || (!changed && vote.my_vote) ? "true" : null },
      trip.ui.busy ? "Un instant…" : vote.my_vote && !changed ? "Vote enregistré ✓" : vote.my_vote ? "Changer mon vote" : "Valider mon vote"),
    isOrganizer && vote.votes_cast > 0 && vote.votes_cast < vote.voters_total
      ? button("Clore le vote maintenant", () => act(() => api("POST", `/trips/${view.id}/votes/close`)).catch(() => {}), { kind: "ghost", block: true })
      : null);
}

function screenVoting(view) {
  const range = view.group_budget_range_eur;
  const suggestions = view.suggestions || [];
  return [
    h("section", { class: "stack" },
      h("p", { class: "eyebrow" }, "Étape 3 · Les idées"),
      h("h1", {}, `${suggestions.length} idées pour vous ${view.vote.voters_total}`),
      range
        ? h("p", { class: "lead" }, "Budget du groupe : ", h("strong", {}, `${eur(range[0])} – ${eur(range[1])}`), " / pers.")
        : h("p", { class: "lead" }, "Estimations par personne, vols compris.")),
    h("section", { class: "card soft stack", "aria-labelledby": "compare-title" },
      h("h3", { id: "compare-title" }, "Budgets côte à côte"),
      dotplot(suggestions, range),
      h("p", { class: "small muted" }, range ? "Zone verte : fourchette du groupe (arrondie à 100 €). Point : estimation par personne." : "Point : estimation par personne. La fourchette du groupe s'affiche à partir de 3 voyageurs.")),
    h("nav", { class: "country-tabs", "aria-label": "Aller à un pays" }, suggestions.map((s, i) =>
      button(s.country, () => {
        const card = document.getElementById(`country-${i}`);
        card?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "start" });
        card?.focus({ preventScroll: true });
      }, { small: true }))),
    h("div", { class: "cards-scroller" }, suggestions.map((s, i) => countryCard(s, range, i))),
    moreIdeas(view),
    h("p", { class: "eyebrow", style: "margin-top:8px" }, "Étape 4 · Le vote"),
    voteCard(view),
  ];
}

// --- Étape 5 : le plan ------------------------------------------------------------------------------------

function budgetBlock(view) {
  const plan = view.plan;
  const travelers = view.vote?.voters_total || 1;
  const hasGroupBudget = plan.group_budget_eur != null;
  const under = hasGroupBudget ? plan.gap_eur <= 0 : plan.within_each_budget;
  const perPersonBudget = hasGroupBudget ? plan.group_budget_eur / travelers : null;

  let pill;
  if (hasGroupBudget) pill = h("span", { class: `pill ${under ? "under" : "over"}` }, under ? `${eur(-plan.gap_eur)} sous le budget` : `+${eur(plan.gap_eur)} au-dessus`);
  else pill = h("span", { class: `pill ${under ? "under" : "over"}` }, under ? "Dans le budget de chacun" : "Hors budget pour quelqu'un");

  let bar = null;
  if (perPersonBudget) {
    const scale = Math.max(plan.per_person_eur, perPersonBudget) * 1.1;
    bar = h("div", { class: `budget-bar${under ? "" : " over"}`, role: "img", "aria-label": `Coût par personne ${eur(plan.per_person_eur)} pour un budget moyen de ${eur(perPersonBudget)}` },
      h("span", { style: `width:${(plan.per_person_eur / scale) * 100}%` }),
      h("span", { class: "marker", style: `left:${(perPersonBudget / scale) * 100}%` }));
  }

  const rows = [
    ...plan.ledger.map((line) => h("tr", {}, h("td", {}, line.label), h("td", {}, eur(line.amount_eur)))),
    h("tr", { class: "total" }, h("td", {}, "Total"), h("td", {}, eur(plan.total_cost_eur))),
  ];
  if (hasGroupBudget) {
    rows.push(h("tr", {}, h("td", {}, "Budget du groupe"), h("td", {}, eur(plan.group_budget_eur))));
    rows.push(h("tr", { class: "gap" }, h("td", {}, "Écart"),
      h("td", { class: under ? "gap-under" : "gap-over" }, `${plan.gap_eur > 0 ? "+" : "−"}${eur(Math.abs(plan.gap_eur))} · ${plan.gap_pct > 0 ? "+" : "−"}${percentFormat.format(Math.abs(plan.gap_pct))} %`)));
  }

  return h("section", { class: "card pop stack budget-block", "aria-labelledby": "budget-title" },
    h("h3", { id: "budget-title", class: "visually-hidden" }, "Budget"),
    h("div", { class: "row", style: "justify-content:space-between" },
      h("div", {}, h("p", { class: "small muted" }, "Coût par pers."), h("p", { class: "big-number" }, eur(plan.per_person_eur))),
      pill),
    bar,
    h("table", { class: "ledger" }, h("caption", { class: "visually-hidden" }, "Détail du budget"), h("tbody", {}, rows)),
    plan.my_share_eur != null
      ? h("p", {}, "Ta part : ", h("strong", {}, eur(plan.my_share_eur)), plan.within_my_budget ? " — dans ton budget ✓" : h("span", { class: "gap-over" }, " — au-dessus de ton budget"))
      : null,
    h("p", { class: "small muted" }, "Vols et hébergement estimés. Transports sur place, activités et repas en plus."));
}

function flightItem(flight) {
  const range = flight.price_range_eur;
  return h("li", { class: "card soft stack" },
    h("div", { class: "row", style: "justify-content:space-between" },
      h("strong", {}, `${flight.origin} → ${flight.destination}`),
      h("span", { class: "price", style: "font-size:1.2rem" }, eur(flight.price_eur))),
    flight.origin_airports?.length
      ? h("p", { class: "small muted" }, `${flight.origin_airports.join(", ")} → ${flight.destination_airports.join(", ")}`)
      : null,
    h("p", { class: "small" }, `Aller ${shortDate(flight.departure_date)} · retour ${shortDate(flight.return_date)}${flight.airline ? ` · ${flight.airline}` : ""}`),
    flight.stub ? h("span", { class: "badge outline" }, "Prix factice (démo)") : null,
    flight.estimated && range ? h("p", { class: "small muted" }, `Estimation · fourchette habituelle ${eur(range[0])} – ${eur(range[1])}`) : null);
}

function hotelItem(hotel) {
  const sources = (hotel.sources || []).map(safeUrl).filter(Boolean);
  return h("li", { class: "card soft stack" },
    h("div", { class: "row", style: "justify-content:space-between" },
      h("strong", {}, hotel.name),
      h("span", { class: "price", style: "font-size:1.2rem" }, eur(hotel.price_eur))),
    hotel.check_in ? h("p", { class: "small" }, `Du ${shortDate(hotel.check_in)} au ${shortDate(hotel.check_out)} · pour tout le groupe`) : null,
    hotel.notes ? h("p", {}, hotel.notes) : null,
    sources.length ? h("details", {}, h("summary", {}, plural(sources.length, "source", "sources")),
      h("ul", { class: "sources" }, sources.map((url) => h("li", {}, h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, hostname(url)))))) : null);
}

const PLAN_TABS = [["days", "Jour par jour"], ["flights", "Vols"], ["hotels", "Hôtels"]];

function planTabs(view) {
  const plan = view.plan;
  const select = (tab, focus = false) => {
    trip.ui.tab = tab;
    renderTrip();
    if (focus) document.getElementById(`tab-${tab}`)?.focus();
  };
  const onKey = (event) => {
    const index = PLAN_TABS.findIndex(([key]) => key === trip.ui.tab);
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (step) select(PLAN_TABS[(index + step + PLAN_TABS.length) % PLAN_TABS.length][0], true);
  };
  let panel;
  if (trip.ui.tab === "flights") panel = h("ul", { class: "stack", style: "list-style:none;margin:0;padding:0" }, plan.flights.map(flightItem));
  else if (trip.ui.tab === "hotels") panel = h("ul", { class: "stack", style: "list-style:none;margin:0;padding:0" }, plan.hotels.map(hotelItem));
  else panel = h("ol", { class: "days" }, plan.itinerary.map((day) =>
    h("li", { class: "day" },
      h("span", { class: "num", "aria-hidden": "true" }, `J${day.day}`),
      h("div", { class: "stack", style: "gap:4px" },
        h("strong", {}, `Jour ${day.day}`),
        h("ul", {}, day.activities.map((activity) => h("li", {}, activity)))))));

  return h("section", { class: "stack" },
    h("div", { class: "tabs", role: "tablist", "aria-label": "Détails du plan", onkeydown: onKey },
      PLAN_TABS.map(([key, label]) => h("button", {
        type: "button", role: "tab", id: `tab-${key}`, "aria-controls": "plan-panel",
        "aria-selected": trip.ui.tab === key ? "true" : "false", tabindex: trip.ui.tab === key ? "0" : "-1",
        onclick: () => select(key),
      }, label))),
    h("div", { id: "plan-panel", role: "tabpanel", "aria-labelledby": `tab-${trip.ui.tab}`, tabindex: "0" }, panel));
}

function reviewActions(view) {
  const organizer = organizerOf(view);
  if (!view.me.is_organizer) {
    return h("section", { class: "card sun stack" },
      h("h3", {}, "Plan en relecture"),
      h("p", {}, `${organizer?.name || "L'organisateur·rice"} relit le plan avant de le valider.`));
  }
  async function adjust(event) {
    event.preventDefault();
    const feedback = trip.ui.adjust.trim();
    if (!feedback) {
      toast("Dis à l'IA quoi ajuster.", true);
      return;
    }
    await act(() => api("POST", `/trips/${view.id}/plan/adjust`, { feedback })).catch(() => {});
    trip.ui.adjust = "";
  }
  return h("section", { class: "card sun stack", "aria-labelledby": "review-title" },
    h("h3", { id: "review-title" }, "Ce plan vous va ?"),
    button("Valider ce plan", () => act(() => api("POST", `/trips/${view.id}/plan/approve`), { success: "Plan validé !" }).catch(() => {}), { kind: "dark", block: true, busy: trip.ui.busy }),
    h("form", { class: "stack", onsubmit: adjust, novalidate: true },
      h("div", { class: "field" },
        h("label", { for: "adjust" }, "Ou demander un ajustement"),
        h("textarea", { id: "adjust", maxlength: "500", placeholder: "Un hôtel avec piscine, moins de musées…", value: trip.ui.adjust, oninput: (e) => { trip.ui.adjust = e.target.value; } })),
      h("button", { class: "btn block", type: "submit", "aria-disabled": trip.ui.busy ? "true" : null }, "Demander l'ajustement")));
}

function screenPlan(view) {
  const plan = view.plan;
  const reviewing = view.status === "reviewing";
  const dates = [...plan.flights.map((f) => f.departure_date), ...plan.hotels.map((hotel) => hotel.check_in)].filter(Boolean).sort();
  const ends = [...plan.flights.map((f) => f.return_date), ...plan.hotels.map((hotel) => hotel.check_out)].filter(Boolean).sort();
  return [
    h("section", { class: "hero" },
      h("p", { class: "eyebrow", style: "color:var(--ink)" }, reviewing ? "Étape 5 · Le plan (brouillon)" : "Étape 5 · Le plan"),
      h("h1", {}, `${plan.selected_country} !`),
      h("p", { style: "font-weight:600" },
        dates.length ? `${shortDate(dates[0])} → ${fullDateFormat.format(parseDate(ends[ends.length - 1]))} · ` : "",
        plural(view.vote?.voters_total || 1, "voyageur", "voyageurs"))),
    reviewing ? reviewActions(view) : null,
    h("div", { class: "split" },
      h("div", { class: "aside stack" },
        budgetBlock(view),
        view.vote?.results ? h("section", { class: "card soft stack", "aria-labelledby": "results-title" }, h("h3", { id: "results-title" }, "Le vote a parlé"), resultsView(view.vote)) : null),
      planTabs(view)),
  ];
}

// --- Erreur ---------------------------------------------------------------------------------------------------

function screenError(view) {
  const organizer = organizerOf(view);
  return h("section", { class: "card coral stack narrow", role: "alert" },
    h("h2", {}, "Oups."),
    h("p", {}, view.error || "L'agent n'a pas pu finir. Tes réponses sont gardées."),
    view.me.is_organizer && view.can_retry
      ? button("Réessayer", () => act(() => api("POST", `/trips/${view.id}/retry`)).catch(() => {}), { kind: "dark", block: true, busy: trip.ui.busy })
      : h("p", { class: "muted" }, `${organizer?.name || "L'organisateur·rice"} peut relancer l'agent. Tes réponses sont gardées.`));
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (trip.view?.status === "voting") renderTrip(); }, 200);
});

// Service worker (PWA) : installation et accès hors ligne à l'interface. Sans lui, le site marche quand même.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* navigation privée, HTTP non local… */ });
}

route();
