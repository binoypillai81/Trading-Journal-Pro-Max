import { api, h, mount, fmt, signed, toast, loading, chip, table, stateChip } from "./lib.js";
import { ctx, refreshMeta, activeSession, noSession } from "./app.js";
import { renderChart } from "./chart.js";
import { outcomePanel, renderField } from "./review.js";

// Completed Review Mode: direct navigation among COMPLETED/SKIPPED trades of the session.
// It never unlocks or reorders trades in the blind chronological workflow.
export async function completedView(root) {
  mount(root, loading());
  const sessions = await api("GET", "/api/sessions");
  const s = sessions.find((x) => x.id === ctx.sessionId) || (await activeSession());
  if (!s) return mount(root, noSession());
  const list = await api("GET", `/api/sessions/${s.id}/completed`);
  const f = { setup: "", emotion: "", result: "", flag: "", status: "" };
  const uniq = (arr) => [...new Set(arr)].sort();
  const setups = uniq(list.flatMap((x) => x.setups));
  const emotions = uniq(list.flatMap((x) => x.emotions));
  const flags = uniq(list.flatMap((x) => x.psychology_flags));
  const body = h("div", {});
  const sel = (key, label, options) => h("label", { class: "field" }, h("span", { class: "label" }, label),
    h("select", { onChange: (e) => { f[key] = e.target.value; render(); } }, h("option", { value: "" }, "All"), options.map((o) => h("option", { value: o }, o))));
  const render = () => {
    const rows = list.filter((x) => (!f.setup || x.setups.includes(f.setup)) && (!f.emotion || x.emotions.includes(f.emotion))
      && (!f.result || x.result === f.result) && (!f.flag || x.psychology_flags.includes(f.flag)) && (!f.status || x.status === f.status));
    body.replaceChildren(table(["#", "Entry", "Instrument", "State", "Setups", "Emotions", "Confidence", "Result", "Points", "Flags", ""],
      rows.map((x) => h("tr", {},
        h("td", { class: "num" }, x.position), h("td", { class: "mono small" }, x.entry_local.slice(0, 16)), h("td", {}, x.instrument),
        h("td", {}, stateChip(x.status), x.re_add_episodes ? chip("MULTI-ENTRY", "state-current") : null,
          x.exit_kind === "expiry_settlement" ? chip("EXPIRY", "label-belief") : null), h("td", { class: "small" }, x.setups.join(", ")), h("td", { class: "small" }, x.emotions.join(", ")),
        h("td", { class: "num" }, fmt(x.confidence)), h("td", {}, x.result || "—"),
        h("td", { class: `num ${x.gross_points > 0 ? "pos" : x.gross_points < 0 ? "neg" : ""}` }, signed(x.gross_points, 2)),
        h("td", { class: "small" }, x.psychology_flags.join(", ") || (x.override_reason ? `override: ${x.override_reason}` : "")),
        h("td", {}, x.status === "COMPLETED" ? h("a", { href: `#/completed/${x.review_id}` }, "Study") : "")))));
  };
  render();
  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, `Completed review mode · session #${s.id}`), h("h1", {}, "Completed trades"),
      h("p", { class: "small muted" }, "Full trade, journal and outcome for trades already completed. Filtering here does not affect the blind chronological order."))),
    h("div", { class: "panel" }, h("div", { class: "grid-3" },
      sel("status", "State", ["COMPLETED", "SKIPPED_WITH_OVERRIDE"]), sel("setup", "Setup", setups), sel("emotion", "Pre-trade state", emotions),
      sel("result", "Result", ["WIN", "LOSS", "FLAT"]), sel("flag", "Psychology flag", flags))),
    h("div", { class: "panel" }, list.length ? body : h("p", { class: "muted" }, "No completed trades in this session yet.")));
}

export async function completedDetailView(root, rid) {
  mount(root, loading());
  const meta = ctx.meta || (await refreshMeta());
  const r = await api("GET", `/api/reviews/${rid}`);
  const chartBox = h("div", {});
  let handle = null;
  const toggles = { ema: true, pivots: true, prevDay: false, dayOpen: false };
  const draw = async () => {
    const data = await api("GET", `/api/reviews/${rid}/chart`);
    if (handle) handle.destroy();
    handle = renderChart(chartBox, data, { toggles, onToggle: (t) => { Object.assign(toggles, t); draw(); } });
  };
  const t = r.trade;
  const reviseAnswers = { ...(r.revisions.filter((x) => x.section === "post_outcome").pop()?.payload || r.post.answers) };
  const reviseBox = h("div", {});
  const renderRevise = () => reviseBox.replaceChildren(
    ...meta.questionnaire.post.flatMap((sec) => sec.fields.map((f) => renderField(f, reviseAnswers, (n, v, re) => { reviseAnswers[n] = v; if (re) renderRevise(); }))),
    h("label", { class: "field" }, h("span", { class: "label" }, "Why are you revising?"), h("input", { type: "text", id: "rev-reason" })),
    h("button", { class: "primary", onClick: async () => {
      try {
        await api("POST", `/api/reviews/${rid}/revise`, { section: "post_outcome", answers: reviseAnswers, reason: document.getElementById("rev-reason").value });
        toast("Revision saved; the original is preserved"); completedDetailView(root, rid);
      } catch (e) { toast(e.message, "error"); }
    } }, "Save as revision"));
  renderRevise();
  const note = h("textarea", { placeholder: "Annotation about the thesis (the thesis itself stays unchanged)" });

  mount(root,
    h("div", { class: "page-head" },
      h("div", {}, h("div", { class: "eyebrow" }, `Completed review · trade #${t.chrono_seq}`), h("h1", {}, `${t.instrument} · ${t.entry_local.slice(0, 16)}`),
        h("p", { class: "small muted mono" }, `review ${r.id} · started ${r.started_at} · locked ${r.thesis_locked_at} · revealed ${r.outcome_revealed_at} · completed ${r.completed_at}`)),
      h("a", { class: "btn", href: "#/completed" }, "← Completed list")),
    timelinePanel(r),
    h("div", { class: "review-grid" },
      h("div", { class: "stack" }, h("div", { class: "panel" }, chartBox), h("div", { class: "panel" }, h("h2", {}, "Outcome"), outcomePanel(r.post.outcome))),
      h("div", { class: "stack" },
        h("div", { class: "panel" }, h("div", { class: "row between" }, h("h2", {}, "Original thesis"), chip("IMMUTABLE", "label-belief")),
          h("p", { class: "small muted mono" }, `sha256 ${r.thesis.sha256}`), h("div", { class: "snapshot" }, r.thesis.text)),
        h("div", { class: "panel" }, h("div", { class: "row between" }, h("h2", {}, "Original post-outcome reflection"), chip("IMMUTABLE", "label-belief")),
          meta.questionnaire.post.flatMap((sec) => sec.fields).filter((f) => r.post.answers[f.name] && (!Array.isArray(r.post.answers[f.name]) || r.post.answers[f.name].length))
            .map((f) => h("div", { class: "field" }, h("div", { class: "field-label" }, f.label), h("div", {}, Array.isArray(r.post.answers[f.name]) ? r.post.answers[f.name].join(", ") : r.post.answers[f.name])))),
        r.revisions.length ? h("div", { class: "panel" }, h("h2", {}, `Revisions (${r.revisions.length})`),
          h("p", { class: "small muted" }, "Later edits are stored separately so hindsight drift can be studied."),
          r.revisions.map((x) => h("details", { class: "group" }, h("summary", {}, `${x.created_at} · ${x.section}${x.reason ? " · " + x.reason : ""}`),
            h("pre", { class: "snapshot small" }, JSON.stringify(x.payload, null, 2))))) : null,
        h("details", { class: "group" }, h("summary", {}, "Revise post-outcome reflection"), reviseBox),
        h("details", { class: "group" }, h("summary", {}, "Annotate the thesis"), note,
          h("button", { style: { marginTop: ".5rem" }, onClick: async () => {
            try { await api("POST", `/api/reviews/${rid}/revise`, { section: "thesis_annotation", answers: { note: note.value } }); toast("Annotation saved"); completedDetailView(root, rid); }
            catch (e) { toast(e.message, "error"); }
          } }, "Save annotation")))));
  draw();
  return () => handle && handle.destroy();
}

// Spec §40: the life of one trade, from the market before entry to the next trade being unlocked.
function timelinePanel(r) {
  const t = r.trade, th = r.thesis.answers, po = r.post.answers, oc = r.post.outcome, ctx = r.entry_context || {};
  const utc = (s) => (s ? s.replace("T", " ").slice(0, 16) + " UTC" : "—");
  const steps = [
    ["Market before entry", `${t.chart_instrument} ${ctx.trend_last_hour ? `· ${ctx.trend_last_hour} last hour` : ""}${ctx.above_ema !== undefined ? ` · ${ctx.above_ema ? "above" : "below"} EMA` : ""}${ctx.nearest_pivot ? ` · nearest pivot ${ctx.nearest_pivot}` : ""}`, "market"],
    ["Trade decision", th.one_sentence || th.primary_reason || "—", "decision"],
    ["Entry", `${(t.entry_local || "").slice(0, 16)} · ${t.direction || "—"} · first order ${fmt(t.initial_entry_price ?? t.entry_price)}`, "entry"],
    ["Original thesis locked", utc(r.thesis_locked_at), "locked"],
    ["Outcome revealed", `${utc(r.outcome_revealed_at)} · ${oc.result || "—"}${oc.exit && oc.exit.kind === "expiry_settlement" ? " · held to expiry" : ""}`, "reveal"],
    ["Post-trade analysis", `${po.hindsight_assessment || "—"}`, "analysis"],
    ["Psychology tagging", po.psychology_influenced === "No" ? "No influence identified" : (po.psychology_flags || []).join(", ") || po.psychology_influenced || "—", "psych"],
    ["Lessons / observations", po.lesson || po.flawed_part || "—", "lesson"],
    ["Next chronological trade unlocked", utc(r.completed_at), "next"],
  ];
  return h("div", { class: "panel" }, h("h2", {}, "Trade timeline"),
    h("ol", { class: "timeline" }, steps.map(([k, v], i) => h("li", {}, h("span", { class: "tl-dot" }, String(i + 1)),
      h("div", {}, h("div", { class: "tl-title" }, k), h("div", { class: "small muted" }, v))))));
}
