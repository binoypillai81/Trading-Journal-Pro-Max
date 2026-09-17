import { api, h, mount, fmt, signed, toast, errorBox, loading, store, debounce, chip, modal, table } from "./lib.js";
import { ctx, refreshMeta, activeSession, noSession, progressBar } from "./app.js";
import { renderChart } from "./chart.js";

const STEPS = ["Observe", "Setup", "Direction", "Reason", "Expectation", "Risk", "Confidence", "Psychology", "Process", "Lock thesis", "Reveal", "Reflect", "Complete"];

// ------------------------------------------------------------------ form rendering (shared)
function visible(field, answers) {
  if (!field.show_if) return true;
  const [name, val] = field.show_if;
  const vals = Array.isArray(val) ? val : [val];
  const got = answers[name];
  return Array.isArray(got) ? got.some((g) => vals.includes(g)) : vals.includes(got);
}

export function renderField(field, answers, onChange, { errors = {}, prompts = {}, hints = {} } = {}) {
  if (!visible(field, answers)) return null;
  const id = "f-" + field.name;
  const v = answers[field.name];
  let control;
  if (field.type === "text") {
    control = h("textarea", { id, onInput: (e) => onChange(field.name, e.target.value) });
    control.value = v || "";
  } else if (field.type === "number") {
    control = h("input", { id, type: "number", step: "any", value: v ?? "", onInput: (e) => onChange(field.name, e.target.value === "" ? null : Number(e.target.value)) });
  } else if (field.type === "slider") {
    const num = h("input", { type: "number", min: 0, max: 100, value: v ?? "", "aria-label": field.label });
    const range = h("input", { id, type: "range", min: 0, max: 100, value: v ?? 50, "aria-label": field.label });
    range.addEventListener("input", () => { num.value = range.value; onChange(field.name, Number(range.value)); });
    num.addEventListener("input", () => { range.value = num.value; onChange(field.name, num.value === "" ? null : Number(num.value)); });
    control = h("div", { class: "slider-row" }, h("span", { class: "small muted" }, "0"), range, h("span", { class: "small muted" }, "100"), num);
    if (v === null || v === undefined) control.append(h("span", { class: "small muted" }, "not set"));
  } else {
    const multi = field.type === "multi";
    control = h("div", { class: "choices", role: multi ? "group" : "radiogroup", "aria-label": field.label },
      field.options.map((o) => {
        const checked = multi ? (v || []).includes(o) : v === o;
        return h("label", { class: "choice" },
          h("input", { type: multi ? "checkbox" : "radio", name: id, value: o, checked, onChange: (e) => {
            if (multi) {
              let cur = [...(answers[field.name] || [])];
              if (e.target.checked) {
                if (o === "None of these" || o === "Nothing outside my plan") cur = [o];
                else cur = cur.filter((x) => x !== "None of these" && x !== "Nothing outside my plan").concat(o);
              } else cur = cur.filter((x) => x !== o);
              onChange(field.name, cur, true);
            } else onChange(field.name, o, true);
          } }), h("span", {}, o));
      }));
  }
  const required = field.required || (field.required_if && visible({ show_if: field.required_if }, answers));
  return h("div", { class: "field", "data-field": field.name },
    h("label", { class: "field-label", for: id }, field.label, required ? h("span", { class: "req", "aria-label": "required" }, "*") : null),
    hints[field.name] ? h("div", { class: "field-help" }, hints[field.name]) : null,
    control,
    errors[field.name] ? h("div", { class: "field-error" }, errors[field.name]) : null,
    prompts[field.name] ? h("div", { class: "field-prompt" }, "↳ ", prompts[field.name]) : null);
}

function missingRequired(section, answers) {
  const errs = {};
  for (const f of section.fields) {
    if (!visible(f, answers) && !f.required) continue;
    const req = f.required || (f.required_if && visible({ show_if: f.required_if }, answers));
    const v = answers[f.name];
    if (req && (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length) || (typeof v === "string" && !v.trim()))) errs[f.name] = "Required";
  }
  return errs;
}

function duration(min) {
  if (min === null || min === undefined) return "—";
  if (min < 60) return `${fmt(Math.round(min * 10) / 10)} min`;
  const d = Math.floor(min / 1440), hrs = Math.floor((min % 1440) / 60), m = Math.round(min % 60);
  return [d ? `${d}d` : "", hrs ? `${hrs}h` : "", m ? `${m}m` : ""].filter(Boolean).join(" ") + (d ? " (calendar time)" : "");
}

// ------------------------------------------------------------------ outcome rendering (shared)
export function outcomePanel(oc) {
  const metric = (k, v, cls = "") => h("div", { class: "metric" }, h("div", { class: "k" }, k), h("div", { class: `v ${cls}` }, v));
  const pn = (v) => (v === null || v === undefined ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
  const verdict = (item) => (oc.thesis_vs_reality.find((r) => r.item === item) || {}).verdict || "—";
  const basisChip = (b) => chip(b, b === "FACT" ? "label-fact" : b.startsWith("USER") ? "label-belief" : "label-pattern");
  return h("div", { class: "stack fade-in" },
    h("div", { class: "callout callout-reveal" }, h("strong", {}, "This is information that was NOT available when the trade was taken.")),
    oc.multi_entry ? h("div", { class: "callout callout-warn small" }, chip("MULTI-ENTRY", "state-current"),
      ` You added to this position ${oc.re_add_episodes} time${oc.re_add_episodes > 1 ? "s" : ""} after partially exiting. `,
      `The blind view showed your first order (${fmt(oc.entry.first_order_quantity)} @ ${fmt(oc.entry.first_order_price)}); `,
      `the average entry across all ${oc.entry.opening_fills} opening fills is ${fmt(oc.entry.price)}. This review covers the whole position.`) : null,
    oc.exit.kind === "expiry_settlement" && oc.exit.settlement ? h("div", { class: "callout small" }, chip("HELD TO EXPIRY", "label-belief"),
      ` No closing fill: ${fmt(oc.exit.settlement.settled_quantity)} settled at expiry ${oc.exit.settlement.expiry_date} at `,
      `${fmt(oc.exit.settlement.settlement_price)} (${oc.exit.settlement.type} intrinsic value, ${oc.exit.settlement.underlying} ${fmt(oc.exit.settlement.underlying_value)}). `,
      h("span", { class: "muted" }, oc.exit.settlement.underlying_value_source)) : null,
    h("div", { class: "metric-grid" },
      metric(oc.entry.opening_fills > 1 ? "First order" : "Entry", `${fmt(oc.entry.opening_fills > 1 ? oc.entry.first_order_price : oc.entry.price)} @ ${(oc.entry.time || "").slice(11, 16)}`),
      metric(oc.exit.kind === "expiry_settlement" ? "Exit (expiry)" : "Exit", oc.exit.price !== null ? `${fmt(oc.exit.price)} @ ${(oc.exit.time || "").slice(0, 16)}` : "—"),
      metric(oc.entry.opening_fills > 1 ? "Avg entry" : "Entry fills", oc.entry.opening_fills > 1 ? `${fmt(oc.entry.price)} (${oc.entry.opening_fills} fills)` : "1"),
      metric("Direction", `${oc.direction || "—"}${oc.market_bias ? " · needs " + oc.market_bias : ""}`),
      metric("Gross points", signed(oc.gross_points, 2), pn(oc.gross_points)),
      metric("% move", oc.pct_move !== null ? signed(oc.pct_move, 2) + "%" : "—", pn(oc.pct_move)),
      metric("P&L", oc.pnl !== null ? signed(oc.pnl, 0) : "—", pn(oc.pnl)),
      metric("Duration", duration(oc.duration_minutes)),
      metric("MFE (index)", signed(oc.mfe), "pos"),
      metric("MAE (index)", signed(oc.mae), "neg"),
      metric("Max excursion", oc.max_excursion !== null && oc.max_excursion !== undefined ? fmt(Number(oc.max_excursion.toFixed(1))) : "—"),
      metric("Direction call", verdict("Direction")),
      metric("Expected move", verdict("Expected move")),
      metric("Expected time", verdict("Expected time")),
      oc.target_reached !== undefined ? metric("Target", oc.target_reached ? "Reached" : "Not reached") : null,
      oc.stop_reached !== undefined ? metric("Stop", oc.stop_reached ? "Would have been hit" : "Not hit") : null),
    h("p", { class: "small muted" }, `P&L source: ${oc.pnl_source}. MFE/MAE measured on the chart instrument from the index level at entry (${fmt(oc.reference_price)}), using ${oc.path_timeframe || "no"} bars. Thesis window ends ${oc.thesis_window_end || "—"}.`),
    h("h3", {}, "Thesis vs reality"),
    table(["Item", "My expectation", "Actual", "Assessment", "Basis"], oc.thesis_vs_reality.map((r) => h("tr", {},
      h("td", {}, h("strong", {}, r.item)), h("td", {}, r.expectation), h("td", { class: "mono small" }, r.actual), h("td", {}, r.verdict), h("td", {}, basisChip(r.basis))))),
    oc.limitations.length ? h("details", { class: "group" }, h("summary", {}, `Data limitations (${oc.limitations.length})`), h("ul", { class: "small" }, oc.limitations.map((l) => h("li", {}, l)))) : null);
}

// What the trader knew about their day at this entry (trades that closed earlier today only).
function dayPanel(d) {
  if (!d) return null;
  const pnl = d.realised_day_pnl;
  const first = d.first_trade_of_day && !d.closed_before_entry;
  const cls = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "";
  const bits = [];
  if (!first) bits.push(`${d.closed_before_entry} closed today (${d.wins}W / ${d.losses}L${d.flat ? ` / ${d.flat} flat` : ""})`);
  if (d.consecutive_losses >= 2) bits.push(`${d.consecutive_losses} losses in a row`);
  if (d.open_at_entry) bits.push(`${d.open_at_entry} position${d.open_at_entry > 1 ? "s" : ""} still open`);
  if (d.entered_earlier_today > d.closed_before_entry) bits.push(`${d.entered_earlier_today} trade${d.entered_earlier_today > 1 ? "s" : ""} entered earlier today`);
  if (d.pnl_unknown) bits.push(`${d.pnl_unknown} with unknown P&L`);
  if (d.includes_skipped_trades) bits.push(`includes ${d.includes_skipped_trades} skipped trade${d.includes_skipped_trades > 1 ? "s" : ""}`);
  return h("div", { class: "day-panel" },
    h("div", { class: "fact" }, h("div", { class: "k" }, "Day P&L before this entry"),
      h("div", { class: `v ${cls}` }, first ? "First trade of the day" : pnl === null ? "unknown" : signed(pnl, 0))),
    bits.length ? h("div", { class: "small muted" }, bits.join(" · ")) : null,
    h("div", { class: "small muted", title: d.basis }, "Realised from trades closed today before this entry; before charges. Open positions not included."));
}

function observationCards(list) {
  if (!list || !list.length) return h("p", { class: "small muted" }, "No repeated patterns involving this trade yet — observations appear once similar trades accumulate.");
  return list.map((o) => h("div", { class: "panel obs" },
    h("div", { class: "row gap between" }, chip(o.status, o.status.startsWith("REPEATED") ? "state-current" : "label-pattern"), h("span", { class: "small muted" }, o.reliability)),
    h("p", {}, h("strong", {}, o.summary)),
    h("ul", { class: "small" }, o.facts.map((f) => h("li", {}, f))),
    o.common_reasoning.length ? h("p", { class: "small" }, "Common stated reasoning: ", o.common_reasoning.join(", ")) : null,
    o.potential_recurring_issues.length ? h("div", { class: "small" }, chip("POSSIBLE INTERPRETATION", "label-interp"), h("ul", {}, o.potential_recurring_issues.map((x) => h("li", {}, x)))) : null,
    h("p", { class: "small muted" }, `Evidence: trades #${o.evidence_positions.join(", #")} · ${o.scope}`)));
}

// ------------------------------------------------------------------ main view
export async function reviewView(root) {
  mount(root, loading("Reconstructing the moment…"));
  const meta = ctx.meta || (await refreshMeta());
  const session = await activeSession();
  if (!session) return mount(root, noSession());
  const sid = session.id;
  let cur = await api("GET", `/api/sessions/${sid}/current`);
  if (cur.finished) return mount(root, finishedScreen(cur));
  if (!cur.review || cur.review.status === "UNREVIEWED") {
    await api("POST", `/api/sessions/${sid}/current/start`);
    cur = await api("GET", `/api/sessions/${sid}/current`);
  }
  let chartHandle = null;
  const rv = cur.review;
  const trade = cur.trade;
  const status = rv.status;
  const stepIndex = { IN_PROGRESS: 0, THESIS_LOCKED: 10, OUTCOME_REVEALED: 11 }[status];

  // ---- chronology header
  const sectionKey = `section.${rv.id}`;
  let sectionIdx = store.get(sectionKey, 0);
  const stepper = h("div", { class: "stepper", "aria-label": "Review stages" });
  const renderStepper = (now) => stepper.replaceChildren(...STEPS.map((s, i) => h("span", { class: [i < now ? "done" : "", i === now ? "now" : "", i >= 10 ? "after" : ""].join(" ") }, s)));
  renderStepper(status === "IN_PROGRESS" ? sectionIdx : stepIndex);
  const header = h("div", { class: "panel" },
    h("div", { class: "chrono" },
      h("div", {}, h("div", { class: "eyebrow" }, "Chronological review"), h("div", { class: "chrono-pos" }, `Trade ${cur.position} `, h("small", {}, `of ${cur.total}`))),
      h("div", {}, progressBar({ total: cur.total, completed: session.completed, skipped: session.skipped, remaining: cur.total - session.completed - session.skipped, current_position: cur.position }),
        h("div", { class: "chrono-meta" },
          h("span", {}, "current ", h("b", {}, trade.entry_local.slice(0, 16))),
          h("span", {}, "previous ", h("b", {}, cur.previous ? cur.previous.entry_local.slice(0, 16) : "— (first trade)")),
          h("span", {}, "next ", h("b", {}, !cur.next ? "— (last trade)" : cur.next.hidden ? "🔒 locked until this review is completed" : cur.next.entry_local.slice(0, 16))),
          h("span", {}, h("b", {}, session.completed), " completed · ", h("b", {}, cur.remaining), " remaining"))),
      h("div", { class: "row gap" }, h("a", { class: "btn", href: "#/queue" }, "Queue"))),
    stepper);

  // ---- chart + facts (left)
  const chartBox = h("div", {});
  const limits = h("div", {});
  const toggles = store.get("chartToggles", { ema: true, pivots: true, prevDay: false, dayOpen: false });
  const drawChart = async () => {
    try {
      const data = await api("GET", `/api/reviews/${rv.id}/chart`);
      if (chartHandle) chartHandle.destroy();
      chartHandle = renderChart(chartBox, data, { toggles, onToggle: (t) => { Object.assign(toggles, t); store.set("chartToggles", toggles); drawChart(); } });
      limits.replaceChildren(data.limitations.length ? h("details", { class: "group" }, h("summary", { class: "small" }, `What this chart can and cannot show (${data.limitations.length})`),
        h("ul", { class: "small" }, data.limitations.map((l) => h("li", {}, l)))) : "");
    } catch (e) { chartBox.replaceChildren(errorBox(e)); }
  };
  const fact = (k, v) => h("div", { class: "fact" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  const d = new Date(trade.entry_local.replace(" ", "T"));
  const niceDate = d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
  const left = h("div", { class: "stack" },
    h("div", { class: "panel" },
      status === "IN_PROGRESS" ? h("div", { class: "standing" }, `You are now standing at ${trade.entry_local.slice(11, 16)} on ${niceDate}. What do you see?`) : null,
      h("div", { class: "facts" },
        fact("Instrument", trade.instrument), fact("Date", trade.entry_local.slice(0, 10)), fact("Entry time", trade.entry_local.slice(11, 19)),
        fact("Entry price", fmt(trade.entry_price)), fact("Direction", trade.direction || "not in data"), trade.quantity ? fact("Quantity", fmt(trade.quantity)) : null),
      dayPanel(cur.day_context),
      h("p", { class: "blind-note" }, `Timestamp source: ${trade.entry_tz_source} · original value “${trade.entry_original}”`),
      cur.hindsight_cautions.map((c) => h("div", { class: "callout callout-warn small" }, "⚠ ", c)),
      !cur.market_data_available && ["IN_PROGRESS", "UNREVIEWED"].includes(status) ? h("div", { class: "callout callout-warn small" },
        h("strong", {}, `No ${trade.chart_instrument} market data for ${trade.entry_local.slice(0, 10)}. `),
        "You can review this trade without a chart, or ",
        h("button", { class: "linkish", onClick: () => excludeDialog(sid, meta, "No market data for this instrument") }, "exclude it from review"), ".") : null,
      status !== "OUTCOME_REVEALED" ? h("p", { class: "blind-note" }, "No exit or P&L for this trade. No future candles. No information from later trades — the server has not sent any.") : null),
    h("div", { class: "panel" }, chartBox, limits),
    cur.process_context ? h("div", { class: "panel" }, h("h3", {}, "Process context — your earlier completed journals"),
      table(["#", "Entry", "Setups", "One sentence", "Result"], cur.process_context.map((p) => h("tr", {},
        h("td", { class: "num" }, p.position), h("td", { class: "mono small" }, p.entry_local.slice(0, 16)), h("td", { class: "small" }, (p.setups || []).join(", ")),
        h("td", { class: "small" }, p.one_sentence), h("td", { class: "small" }, p.result))))) : null);

  const right = h("div", { class: "stack sticky-col" });
  const outcomeBox = h("div", {});
  mount(root, header, h("div", { class: "review-grid" }, h("div", { class: "stack" }, left, outcomeBox), right),
    h("p", { class: "small muted row gap end", style: { marginTop: "1.5rem" } },
      ["IN_PROGRESS", "UNREVIEWED"].includes(status) ? h("button", { class: "linkish", onClick: () => excludeDialog(sid, meta) }, "Exclude this trade from review…") : null,
      h("button", { class: "linkish", onClick: () => overrideDialog(sid, meta) }, "Chronological-review exception (administrative override)…")));
  drawChart();

  // ---- stage panels (right)
  if (status === "IN_PROGRESS") {
    const answers = { ...rv.draft };
    const saveDraft = debounce(async () => {
      try { await api("PUT", `/api/reviews/${rv.id}/draft`, answers); } catch (e) { toast(e.message, "error"); }
    }, 600);
    let serverPrompts = {};
    const sections = meta.questionnaire.thesis;
    const hints = trade.direction ? { direction: `Your trade data says ${trade.direction}.` } : {};

    const renderSection = (errors = {}) => {
      store.set(sectionKey, sectionIdx);
      renderStepper(sectionIdx);
      const sec = sections[sectionIdx];
      const onChange = (name, value, rerender) => { answers[name] = value; saveDraft(); if (rerender) renderSection(); };
      right.replaceChildren(h("div", { class: "panel fade-in" },
        h("div", { class: "eyebrow" }, `Blind reconstruction · ${sectionIdx + 1} / ${sections.length}`),
        h("h2", {}, sec.title),
        h("p", { class: "small muted" }, "Answer as of the entry moment. Draft saves automatically; nothing is final until you lock the thesis."),
        sec.fields.map((f) => renderField(f, answers, onChange, { errors, prompts: serverPrompts, hints })),
        h("div", { class: "form-nav" },
          h("button", { disabled: sectionIdx === 0, onClick: () => { sectionIdx--; renderSection(); } }, "← Back"),
          sectionIdx < sections.length - 1
            ? h("button", { class: "primary", onClick: async () => {
                const errs = missingRequired(sec, answers);
                if (Object.keys(errs).length) return renderSection(errs);
                try { serverPrompts = (await api("POST", `/api/reviews/${rv.id}/validate`, answers)).prompts; } catch { /* ignore */ }
                const here = sec.fields.filter((f) => serverPrompts[f.name]).length;
                if (here && !answers.__promptSeen?.[sec.id]) {
                  answers.__promptSeen = { ...(answers.__promptSeen || {}), [sec.id]: true };
                  toast("Some answers could be more specific — see the prompts. Continue again to move on.");
                  return renderSection();
                }
                sectionIdx++; renderSection();
              } }, "Next →")
            : h("button", { class: "accent", onClick: () => { const errs = missingRequired(sec, answers); if (Object.keys(errs).length) return renderSection(errs); reviewThesis(); } }, "Review my original thesis →"))));
    };

    const reviewThesis = async () => {
      const clean = { ...answers }; delete clean.__promptSeen;
      const v = await api("POST", `/api/reviews/${rv.id}/validate`, clean);
      renderStepper(9);
      const errFields = Object.keys(v.errors);
      const ack = h("input", { type: "checkbox", id: "spec-ack", checked: !!answers.specificity_acknowledged });
      const confirmBox = h("input", { type: "checkbox", id: "confirm-thesis" });
      const lockBtn = h("button", { class: "accent", disabled: !!errFields.length, onClick: async () => {
        if (!confirmBox.checked) return toast("Tick the confirmation first", "error");
        if (Object.keys(v.prompts).length && !ack.checked) return toast("Make the flagged answers more specific, or confirm you have been as specific as you honestly can", "error");
        clean.specificity_acknowledged = ack.checked;
        lockBtn.disabled = true;
        try {
          await api("POST", `/api/reviews/${rv.id}/lock`, { answers: clean, confirm: true });
          toast("Thesis locked. It can never be changed.");
          reviewView(root);
        } catch (e) { toast(e.message, "error"); lockBtn.disabled = false; }
      } }, "Confirm Thesis");
      right.replaceChildren(h("div", { class: "panel fade-in" },
        h("div", { class: "eyebrow" }, "Mandatory original thesis"),
        h("h2", {}, "Is this what you believed at the moment of entry?"),
        errFields.length ? h("div", { class: "callout callout-danger" }, "Missing or invalid: ",
          errFields.map((f) => h("button", { class: "linkish", onClick: () => { sectionIdx = sections.findIndex((s) => s.fields.some((x) => x.name === f)); renderSection(v.errors); } }, f)).reduce((a, b) => [a, ", ", b])) : null,
        Object.keys(v.prompts).length ? h("div", { class: "callout callout-warn" },
          h("strong", {}, "Journal quality: some answers are vague."),
          h("ul", { class: "small" }, Object.entries(v.prompts).map(([f, p]) => h("li", {}, h("button", { class: "linkish", onClick: () => { sectionIdx = sections.findIndex((s) => s.fields.some((x) => x.name === f)); serverPrompts = v.prompts; renderSection(); } }, f), ": ", p))),
          h("label", { class: "toggle", for: "spec-ack" }, ack, "I have been as specific as I honestly can (recorded)")) : null,
        h("div", { class: "snapshot", tabindex: "0" }, v.snapshot_text),
        h("label", { class: "toggle", for: "confirm-thesis", style: { marginTop: ".8rem", color: "var(--ink)" } }, confirmBox, h("strong", {}, "Confirm that this represents what you believed at the time of entry.")),
        h("div", { class: "form-nav" }, h("button", { onClick: () => renderSection() }, "Go Back and Edit"), lockBtn)));
    };

    renderSection();
  } else if (status === "THESIS_LOCKED") {
    right.replaceChildren(h("div", { class: "panel fade-in" },
      h("div", { class: "row between" }, h("div", { class: "eyebrow" }, "Original thesis · locked"), chip("THESIS_LOCKED", "state-thesis_locked")),
      h("p", { class: "small muted mono" }, `locked ${rv.thesis.locked_at} · sha256 ${rv.thesis.sha256.slice(0, 12)}…`),
      h("div", { class: "snapshot" }, rv.thesis.text)),
      h("div", { class: "reveal-zone fade-in" },
        h("p", {}, "Everything so far was limited to what existed at ", h("strong", {}, trade.entry_local.slice(11, 16)), "."),
        h("p", { class: "small" }, "Revealing shows the candles after entry, the exit, P&L and excursions. Your thesis above cannot be changed afterwards."),
        h("button", { class: "reveal-btn", onClick: async (e) => {
          e.target.disabled = true;
          try { await api("POST", `/api/reviews/${rv.id}/reveal`); reviewView(root); } catch (err) { toast(err.message, "error"); e.target.disabled = false; }
        } }, "REVEAL WHAT HAPPENED")));
  } else if (status === "OUTCOME_REVEALED") {
    const oc = await api("GET", `/api/reviews/${rv.id}/outcome`);
    outcomeBox.replaceChildren(h("div", { class: "panel" }, h("h2", {}, "What actually happened"), outcomePanel(oc)));
    const answers = { ...rv.post_draft };
    const saveDraft = debounce(() => api("PUT", `/api/reviews/${rv.id}/post-draft`, answers).catch((e) => toast(e.message, "error")), 600);
    let prompts = {};
    const renderPost = (errors = {}) => {
      const onChange = (name, value, rerender) => { answers[name] = value; saveDraft(); if (rerender) renderPost(errors); };
      const ack = h("input", { type: "checkbox", id: "post-ack", checked: !!answers.specificity_acknowledged, onChange: (e) => { answers.specificity_acknowledged = e.target.checked; saveDraft(); } });
      right.replaceChildren(
        h("details", { class: "group" }, h("summary", {}, "Your locked original thesis"), h("div", { class: "snapshot small" }, rv.thesis.text)),
        h("div", { class: "panel fade-in" },
          h("div", { class: "eyebrow" }, "After the outcome"),
          meta.questionnaire.post.map((sec) => [h("h2", {}, sec.title), sec.fields.map((f) => renderField(f, answers, onChange, { errors, prompts }))]),
          Object.keys(prompts).length ? h("label", { class: "toggle", for: "post-ack" }, ack, "I have been as specific as I honestly can") : null,
          h("div", { class: "form-nav" }, h("span", {}),
            h("button", { class: "primary", onClick: async (e) => {
              const v = await api("POST", `/api/reviews/${rv.id}/post-validate`, answers);
              prompts = v.prompts;
              if (Object.keys(v.errors).length) { toast("Complete the required fields", "error"); return renderPost(v.errors); }
              if (Object.keys(prompts).length && !answers.specificity_acknowledged) { toast("Some answers are vague — see the prompts", "error"); return renderPost(); }
              e.target.disabled = true;
              try {
                const done = await api("POST", `/api/reviews/${rv.id}/complete`, answers);
                store.set(sectionKey, 0);
                mount(root, completionScreen(done, session));
                document.getElementById("continue-chrono")?.focus();
              } catch (err) { toast(err.message, "error"); e.target.disabled = false; }
            } }, "Complete this trade"))));
    };
    renderPost();
  }
  return () => { if (chartHandle) chartHandle.destroy(); };
}

function completionScreen(done, session) {
  return h("div", { class: "completion fade-in" },
    h("div", { class: "eyebrow" }, `Session #${session.id}`),
    h("div", { class: "big" }, "TRADE COMPLETED"),
    h("p", { class: "standing" }, `You have completed Trade ${done.completed_position} of ${done.total}.`),
    done.finished ? h("p", {}, "That was the last trade in the sequence.")
      : h("p", {}, "Next chronological trade: ", h("strong", { class: "mono" }, done.next.entry_local.slice(0, 16))),
    done.hindsight_caution ? h("div", { class: "callout callout-warn small" }, "⚠ ", done.hindsight_caution) : null,
    h("div", { class: "row gap", style: { justifyContent: "center", margin: "1.25rem 0" } },
      !done.finished ? h("button", { id: "continue-chrono", class: "primary", onClick: () => { location.hash = "#/review"; window.dispatchEvent(new HashChangeEvent("hashchange")); } }, "Continue Chronologically") : null,
      h("a", { class: "btn", href: "#/queue" }, "Return to Review Queue")),
    h("h2", { style: { textAlign: "left", marginTop: "2rem" } }, "Observations from what you wrote"),
    h("p", { class: "small muted", style: { textAlign: "left" } }, "Candidate observations only — the tool does not create rules. Based solely on trades already reviewed."),
    observationCards(done.observations));
}

function finishedScreen(cur) {
  return h("div", { class: "completion" }, h("div", { class: "big" }, "SEQUENCE COMPLETE"),
    h("p", {}, `All ${cur.total} trades in session #${cur.session.id} are completed or skipped.`),
    h("div", { class: "row gap", style: { justifyContent: "center" } }, h("a", { class: "btn primary", href: "#/analytics" }, "Open analytics"), h("a", { class: "btn", href: "#/completed" }, "Study completed trades")));
}

function excludeDialog(sid, meta, presetReason) {
  const reasons = meta.exclusion_reasons || ["No market data for this instrument", "Data error in the trade record", "Not a real trade (transfer, adjustment, test)", "Other"];
  const sel = h("select", {}, reasons.map((r) => h("option", { value: r, selected: r === presetReason }, r)));
  const note = h("textarea", { placeholder: "Optional note (required for Other)" });
  modal("Exclude this trade from review", h("div", {},
    h("p", { class: "small" }, "The trade leaves the chronological sequence and analytics. It is not deleted: restore it any time from Import → Data coverage & exclusions. Its result still counts toward that day's P&L context. The exclusion is logged."),
    h("label", { class: "field" }, h("span", { class: "label" }, "Reason"), sel),
    h("label", { class: "field" }, h("span", { class: "label" }, "Note"), note)),
    (close) => [h("button", { onClick: close }, "Cancel"), h("button", { class: "primary", onClick: async () => {
      try {
        const r = await api("POST", `/api/sessions/${sid}/current/exclude`, { reason: sel.value, note: note.value });
        close();
        if (r.refused_reviewed) return toast("This trade's thesis is already locked, so it can't be excluded", "error");
        toast("Trade excluded (logged). Moving to the next trade.");
        location.hash = "#/review"; window.dispatchEvent(new HashChangeEvent("hashchange"));
      } catch (e) { toast(e.message, "error"); }
    } }, "Exclude trade")]);
}

function overrideDialog(sid, meta) {
  if (!meta.settings.override_enabled) {
    return modal("Administrative override is disabled", h("p", {}, "Chronological order is enforced. To skip the current trade you must first enable the override in Settings; every use is logged permanently."),
      (close) => [h("button", { onClick: close }, "Cancel"), h("a", { class: "btn primary", href: "#/settings", onClick: close }, "Open settings")]);
  }
  const reason = h("textarea", { placeholder: "Why must this trade be skipped? (e.g. duplicate row, not my trade, data error)" });
  const conf = h("input", { type: "text", placeholder: "Type SKIP to confirm" });
  modal("Chronological-review exception", h("div", {},
    h("p", { class: "small" }, "The current trade will be marked SKIPPED_WITH_OVERRIDE and the next trade unlocked. Its outcome is not revealed. The reason, time and trade are logged permanently."),
    h("label", { class: "field" }, h("span", { class: "label" }, "Reason"), reason), h("label", { class: "field" }, h("span", { class: "label" }, "Confirmation"), conf)),
    (close) => [h("button", { onClick: close }, "Cancel"), h("button", { class: "danger", onClick: async () => {
      try {
        await api("POST", `/api/sessions/${sid}/current/skip`, { reason: reason.value, confirmation: conf.value.trim() });
        close(); toast("Skipped with override (logged)"); location.hash = "#/review"; window.dispatchEvent(new HashChangeEvent("hashchange"));
      } catch (e) { toast(e.message, "error"); }
    } }, "Skip this trade")]);
}
