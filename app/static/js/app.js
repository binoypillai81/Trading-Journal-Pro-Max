import { api, h, mount, fmt, toast, errorBox, loading, store, stateChip, table, chip, modal } from "./lib.js";
import { importTradesView, importMarketView, timestampsView, exclusionsView } from "./importer.js";
import { reviewView } from "./review.js";
import { completedView, completedDetailView } from "./completed.js";
import { analyticsView } from "./analytics.js";

const root = document.getElementById("app");
export const ctx = { meta: null, sessionId: store.get("sessionId") };

export async function refreshMeta() {
  ctx.meta = await api("GET", "/api/meta");
  return ctx.meta;
}

export async function activeSession({ required = true } = {}) {
  const sessions = await api("GET", "/api/sessions");
  let s = sessions.find((x) => x.id === ctx.sessionId && x.status === "ACTIVE");
  if (!s) s = sessions.find((x) => x.status === "ACTIVE");
  if (s) setSession(s.id);
  if (!s && required) return null;
  updatePill(s);
  return s || null;
}

export function setSession(id) {
  ctx.sessionId = id;
  store.set("sessionId", id);
}

function updatePill(s) {
  const pill = document.getElementById("session-pill");
  pill.textContent = s ? `Session #${s.id}${s.name ? " · " + s.name : ""} · ${s.completed}/${s.total} done` : "No active session";
}

export function progressBar(p) {
  const total = Math.max(p.total, 1);
  return h("div", { class: "progress", role: "img", "aria-label": `${p.completed} completed, ${p.skipped || 0} skipped, ${p.remaining} remaining of ${p.total}` },
    h("i", { class: "done", style: { width: `${(100 * p.completed) / total}%` } }),
    h("i", { class: "skip", style: { width: `${(100 * (p.skipped || 0)) / total}%` } }),
    p.current_position ? h("i", { class: "cur", style: { width: `${100 / total}%` } }) : null);
}

// ------------------------------------------------------------------ home / sessions
async function homeView() {
  mount(root, loading());
  const [meta, sessions] = await Promise.all([refreshMeta(), api("GET", "/api/sessions")]);
  const active = sessions.filter((s) => s.status === "ACTIVE");
  const hasTrades = meta.counts.trades_in_sequence > 0;
  const hasMarket = meta.coverage.length > 0;
  const current = active.find((s) => s.id === ctx.sessionId) || active[0];
  if (current) setSession(current.id);
  updatePill(current);

  const setup = h("div", { class: "panel" },
    h("div", { class: "eyebrow" }, "Workflow"),
    h("h2", {}, "Import → normalize → confirm order → review blind, one trade at a time"),
    h("ol", { class: "small" },
      h("li", {}, hasMarket ? h("span", {}, "Market data loaded ", chip(meta.coverage.map((c) => `${c.instrument} ${c.timeframe}: ${c.sessions} sessions`).join(" · "))) :
        h("span", {}, h("a", { href: "#/import/market" }, "Import 15-minute (and optionally 1-minute) market data"))),
      h("li", {}, hasTrades ? h("span", {}, `${meta.counts.trades_in_sequence} trades in the chronological sequence `) :
        h("a", { href: "#/import/trades" }, "Import your trades CSV and confirm the chronological order")),
      meta.counts.unresolved ? h("li", {}, h("a", { href: "#/timestamps" }, `${meta.counts.unresolved} trade(s) need a decision`), " — they are kept out of the sequence until resolved") : null,
      meta.counts.without_market_data ? h("li", {}, h("a", { href: "#/exclusions" }, `${meta.counts.without_market_data} trade(s) have no chart data`), " — review them without a chart or exclude them") : null,
      meta.counts.excluded ? h("li", {}, h("a", { href: "#/exclusions" }, `${meta.counts.excluded} trade(s) excluded from review`)) : null,
      h("li", {}, "Start or resume a chronological session below")));

  const newBtn = h("button", { class: current ? "" : "primary", disabled: !hasTrades, onClick: async () => {
    const name = prompt("Optional name for this session (e.g. 'Jan–Mar first pass'):", "");
    if (name === null) return;
    const s = await api("POST", "/api/sessions", { name });
    setSession(s.id);
    location.hash = "#/review";
  } }, "Start a new chronological session");

  const list = sessions.length ? table(["Session", "Started", "Last activity", "Progress", "Current", ""],
    sessions.map((s) => h("tr", {},
      h("td", {}, `#${s.id}`, s.name ? h("div", { class: "small muted" }, s.name) : null, s.status !== "ACTIVE" ? chip(s.status) : null),
      h("td", { class: "small mono" }, s.created_at.replace("T", " ").slice(0, 16), " UTC"),
      h("td", { class: "small mono" }, s.last_activity_at.replace("T", " ").slice(0, 16), " UTC"),
      h("td", { style: { minWidth: "180px" } }, progressBar(s), h("div", { class: "small muted mono" },
        `${s.completed} completed · ${s.skipped} skipped · ${s.remaining} remaining`, s.override_events ? ` · ${s.override_events} override(s)` : "")),
      h("td", { class: "mono" }, s.finished ? "finished" : s.current_position ? `${s.current_position} of ${s.total}` : "—"),
      h("td", {}, h("div", { class: "row gap" },
        s.status === "ACTIVE" && !s.finished ? h("button", { class: s.id === current?.id ? "primary" : "", onClick: async () => {
          await api("POST", `/api/sessions/${s.id}/resume`); setSession(s.id); location.hash = "#/review"; } }, "Resume") : null,
        h("button", { onClick: () => { setSession(s.id); location.hash = "#/queue"; } }, "Queue"),
        h("button", { onClick: () => { setSession(s.id); location.hash = "#/completed"; } }, "Completed"),
        s.status === "ACTIVE" ? h("button", { class: "linkish", onClick: async () => {
          if (!confirm("Archive this session? Its journals are kept; it will no longer be offered for resume.")) return;
          await api("POST", `/api/sessions/${s.id}/archive`); homeView(); } }, "Archive") : null)),
    ))) : h("p", { class: "muted" }, "No sessions yet.");

  mount(root,
    h("div", { class: "page-head" },
      h("div", {}, h("div", { class: "eyebrow" }, "Trading laboratory"), h("h1", {}, "Chronological review sessions"),
        h("p", { class: "muted" }, "Every trade is reviewed in entry-time order, seeing only what existed at the moment of entry.")),
      h("div", { class: "row gap" },
        current && !current.finished ? h("a", { class: "btn primary", href: "#/review" }, `Resume session #${current.id} at trade ${current.current_position}`) : null,
        newBtn)),
    setup,
    h("div", { class: "panel" }, h("h2", {}, "Sessions"),
      h("p", { class: "small muted" }, "Starting a new session never overwrites an earlier one. Completed trades can be studied separately in Completed Review."),
      list));
}

// ------------------------------------------------------------------ queue
async function queueView() {
  mount(root, loading());
  const s = await activeSession();
  if (!s) return mount(root, noSession());
  const q = await api("GET", `/api/sessions/${s.id}/queue?window=60`);
  const rows = q.items.map((i) => h("tr", { class: i.queue_state === "CURRENT" ? "is-current" : i.queue_state === "LOCKED" ? "is-locked" : "" },
    h("td", { class: "num" }, i.position),
    h("td", { class: "mono" }, i.entry_local),
    h("td", {}, i.instrument),
    h("td", {}, stateChip(i.queue_state), i.queue_state === "CURRENT" && i.review_state !== "UNREVIEWED" ? h("span", { class: "small muted" }, " ", i.review_state.replace(/_/g, " ").toLowerCase()) : null),
    h("td", {}, i.queue_state === "CURRENT" ? h("a", { href: "#/review" }, "Open blind review") :
      i.queue_state === "COMPLETED" && i.review_id ? h("a", { href: `#/completed/${i.review_id}` }, "Study") :
      i.queue_state === "LOCKED" ? h("span", { class: "small muted" }, "🔒 unlocks after the current trade") : "")));
  mount(root,
    h("div", { class: "page-head" },
      h("div", {}, h("div", { class: "eyebrow" }, `Session #${s.id}`), h("h1", {}, "Chronological review queue"),
        h("p", { class: "small muted" }, "Sorted by normalized entry time only. Outcomes are never shown here.")),
      h("a", { class: "btn primary", href: "#/review" }, "Continue chronologically")),
    h("div", { class: "panel" }, progressBar(s),
      h("div", { class: "chrono-meta" }, h("span", {}, h("b", {}, s.completed), " completed"), h("span", {}, h("b", {}, s.skipped), " skipped"),
        h("span", {}, h("b", {}, s.remaining), " remaining"), h("span", {}, "sort: ", q.sort_method))),
    q.unresolved.length ? h("div", { class: "panel callout-danger" }, h("h3", {}, "Chronology unresolved"),
      h("p", {}, `${q.unresolved.length} trade(s) have no valid entry timestamp and are not in the sequence. `, h("a", { href: "#/timestamps" }, "Resolve them"))) : null,
    h("div", { class: "panel" },
      q.total_items > q.items.length ? h("p", { class: "small muted" }, `Showing positions ${q.items[0]?.position}–${q.items[q.items.length - 1]?.position} of ${q.total_items} around the current trade.`) : null,
      table(["#", "Entry (exchange time)", "Instrument", "State", ""], rows)));
}

export function noSession() {
  return h("div", { class: "panel" }, h("h2", {}, "No active chronological session"),
    h("p", {}, "Start a session from the ", h("a", { href: "#/" }, "Sessions"), " page (after importing trades)."));
}

// ------------------------------------------------------------------ settings
async function settingsView() {
  mount(root, loading());
  const meta = await refreshMeta();
  const s = { ...meta.settings };
  const save = async (changes) => {
    try { Object.assign(s, await api("PUT", "/api/settings", changes)); toast("Saved"); settingsView(); } catch (e) { toast(e.message, "error"); }
  };
  const num = (key, label, help, attrs = {}) => h("label", { class: "field" }, h("span", { class: "label" }, label),
    h("input", { type: "number", value: s[key], ...attrs, onChange: (e) => save({ [key]: Number(e.target.value) }) }), help ? h("div", { class: "field-help" }, help) : null);
  const bool = (key, label, help) => h("label", { class: "field" }, h("span", { class: "toggle" },
    h("input", { type: "checkbox", checked: s[key], onChange: (e) => save({ [key]: e.target.checked }) }), h("strong", {}, label)),
    help ? h("div", { class: "field-help" }, help) : null);

  const rulesBox = h("div", {});
  const rules = [...(s.declared_rules || [])];
  const renderRules = () => {
    rulesBox.replaceChildren(
      rules.length ? h("ul", {}, rules.map((r, i) => h("li", {}, `${meta.declared_rule_types[r.type]}`, r.value !== undefined && r.value !== null ? `: ${Array.isArray(r.value) ? r.value.join(", ") : r.value}` : "",
        " ", h("button", { class: "linkish", onClick: () => { rules.splice(i, 1); save({ declared_rules: rules }); } }, "remove")))) : h("p", { class: "muted small" }, "No declared rules yet."));
  };
  renderRules();
  const typeSel = h("select", {}, Object.entries(meta.declared_rule_types).map(([k, v]) => h("option", { value: k }, v)));
  const valInput = h("input", { type: "text", placeholder: "value (number, or comma-separated list)" });
  const addRule = h("button", { onClick: () => {
    const type = typeSel.value;
    let value = valInput.value.trim();
    if (["avoid_emotions", "only_setups"].includes(type)) value = value.split(",").map((x) => x.trim()).filter(Boolean);
    else if (value) value = Number(value);
    else value = null;
    rules.push({ type, value });
    save({ declared_rules: rules });
  } }, "Add rule");

  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, "Configuration"), h("h1", {}, "Settings"))),
    h("div", { class: "grid-2" },
      h("div", { class: "panel" }, h("h2", {}, "Time & chronology"),
        h("label", { class: "field" }, h("span", { class: "label" }, "Exchange timezone"),
          h("input", { type: "text", value: s.exchange_timezone, onChange: (e) => { if (confirm("Change the exchange timezone? Display times are recomputed; true instants and chronological order are unchanged. The change is logged.")) save({ exchange_timezone: e.target.value.trim() }); } }),
          h("div", { class: "field-help" }, "IANA name, default Asia/Kolkata.")),
        h("div", { class: "callout small" }, h("strong", {}, "Chronological ordering: "), meta.sort_method),
        bool("show_next_timestamp", "Show the next trade's entry time during blind review", "Off by default: knowing when you traded next can influence how you describe the current trade."),
        bool("process_context_enabled", "Process-context view", "Shows your own earlier completed journals (never outcomes of trades still open at the current entry)."),
      ),
      h("div", { class: "panel" }, h("h2", {}, "Chart"),
        num("ema_length", "EMA length (15-minute candles)", "EMA is computed on completed candles available at the review time only.", { min: 2, max: 500 }),
        h("label", { class: "field" }, h("span", { class: "label" }, "Pivot method"),
          h("select", { onChange: (e) => save({ pivot_method: e.target.value }) },
            Object.keys(meta.pivot_formulas).map((k) => h("option", { value: k, selected: k === s.pivot_method }, k))),
          h("div", { class: "field-help mono" }, meta.pivot_formulas[s.pivot_method]),
          h("div", { class: "field-help" }, meta.pivot_source)),
        num("lookback_sessions", "Earlier sessions shown before the entry session", null, { min: 0, max: 20 }),
        num("context_threshold_pct", "“Near a level” threshold for consistency checks (% of price)", "Used to check stated EMA/pivot/previous-day interactions against the chart at entry.", { step: 0.05, min: 0.01 }),
      ),
      h("div", { class: "panel" }, h("h2", {}, "Administrative override"),
        h("p", { class: "small" }, "When enabled, the current trade can be skipped with a written reason. Every use is permanently logged as a chronological-review exception."),
        bool("override_enabled", "Enable chronological override", null)),
      h("div", { class: "panel" }, h("h2", {}, "Declared trading rules"),
        h("p", { class: "small muted" }, "Used only to detect potential process inconsistencies. The tool never creates rules for you."),
        rulesBox, h("div", { class: "row gap" }, typeSel, valInput, addRule))));
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, homeView, "home"],
  [/^#\/review$/, reviewView, "review"],
  [/^#\/queue$/, queueView, "queue"],
  [/^#\/completed$/, completedView, "completed"],
  [/^#\/completed\/(\d+)$/, completedDetailView, "completed"],
  [/^#\/analytics$/, analyticsView, "analytics"],
  [/^#\/import\/trades$/, importTradesView, "import"],
  [/^#\/import\/market$/, importMarketView, "import"],
  [/^#\/timestamps$/, timestampsView, "import"],
  [/^#\/exclusions$/, exclusionsView, "import"],
  [/^#\/settings$/, settingsView, "settings"],
];

let cleanup = null;
async function route() {
  const hash = location.hash || "#/";
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  for (const [re, view, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
      try {
        const c = await view(root, ...m.slice(1));
        if (typeof c === "function") cleanup = c;
      } catch (e) {
        console.error(e);
        mount(root, errorBox(e));
      }
      root.focus({ preventScroll: true });
      return;
    }
  }
  mount(root, h("div", { class: "panel" }, "Not found. ", h("a", { href: "#/" }, "Home")));
}

window.addEventListener("hashchange", route);
activeSession({ required: false }).catch(() => {});
route();
