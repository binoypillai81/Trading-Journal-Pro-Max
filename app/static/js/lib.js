// Small DOM + API helpers shared by all views.

export async function api(method, url, body, { form } = {}) {
  const opts = { method, headers: {} };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.code = data && data.code;
    throw err;
  }
  return data;
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function mount(root, ...children) {
  root.replaceChildren(...children.flat(Infinity).filter(Boolean));
}

export function fmt(v, digits = 2) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString("en-IN");
    return v.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
  }
  return String(v);
}

export function signed(v, digits = 1) {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : "") + fmt(Number(v.toFixed(digits)), digits);
}

export function toast(msg, kind = "info") {
  const box = document.getElementById("toasts");
  const t = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 3500);
}

export function errorBox(err) {
  return h("div", { class: "callout callout-danger" }, h("strong", {}, "Something went wrong. "), err.message || String(err));
}

export function loading(text = "Loading…") {
  return h("div", { class: "loading" }, text);
}

export const store = {
  get(k, d = null) { try { const v = localStorage.getItem("ttm." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ttm." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

export function modal(title, body, actions) {
  const back = h("div", { class: "modal-back" });
  const close = () => back.remove();
  const box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("h2", {}, title), body, h("div", { class: "row gap end" }, ...actions(close)));
  back.append(box);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  document.body.append(back);
  const first = box.querySelector("input, textarea, button");
  if (first) first.focus();
  return close;
}

export function chip(text, kind = "") {
  return h("span", { class: `chip ${kind}` }, text);
}

export const STATE_LABEL = {
  COMPLETED: "Completed", CURRENT: "Current", LOCKED: "Locked", SKIPPED_WITH_OVERRIDE: "Skipped (override)",
  REQUIRES_TIMESTAMP_REVIEW: "Chronology unresolved", UNREVIEWED: "Unreviewed", IN_PROGRESS: "In progress",
  THESIS_LOCKED: "Thesis locked", OUTCOME_REVEALED: "Outcome revealed",
};

export function stateChip(state) {
  return chip(STATE_LABEL[state] || state, "state-" + state.toLowerCase());
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function table(headers, rows, { cls = "" } = {}) {
  return h("div", { class: "table-wrap" },
    h("table", { class: cls },
      h("thead", {}, h("tr", {}, headers.map((x) => h("th", {}, x)))),
      h("tbody", {}, rows)));
}
