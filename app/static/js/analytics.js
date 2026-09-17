import { api, h, mount, fmt, signed, loading, chip, table, store, errorBox } from "./lib.js";
import { ctx, activeSession } from "./app.js";

const SCOPE_LABELS = {
  session_before_current: "Chronologically completed before the current trade (default)",
  session_up_to: "Completed in this session up to position…",
  session_completed: "All completed in this session",
  all_completed: "Completed across all sessions",
  full_history: "Full imported dataset (includes unreviewed trades)",
};

function rate(v) {
  return v === null || v === undefined ? h("span", { class: "muted" }, "—")
    : h("div", { class: "row gap" }, h("div", { class: "bar", style: { width: "70px" } }, h("i", { style: { width: `${v}%` } })), h("span", { class: "mono small" }, `${v}%`));
}

function statsRow(label, s, extra = []) {
  return h("tr", {}, h("td", {}, h("strong", {}, label), h("div", { class: "small muted" }, s.reliability)),
    h("td", { class: "num" }, s.n), h("td", { class: "num" }, s.pct_of_trades !== null ? `${s.pct_of_trades}%` : "—"),
    h("td", { class: "num" }, `${s.wins}/${s.losses}`), h("td", {}, rate(s.win_rate)),
    h("td", { class: "num" }, signed(s.avg_points)), h("td", { class: "num" }, signed(s.avg_mfe)), h("td", { class: "num" }, signed(s.avg_mae)),
    h("td", {}, rate(s.rule_compliance_rate)), ...extra);
}
const STATS_HEAD = ["Group", "Trades", "% of trades", "W/L", "Win rate", "Avg pts", "Avg MFE", "Avg MAE", "Rules followed"];

let EVIDENCE_INDEX = {};
export function evidenceLinks(positions, index = EVIDENCE_INDEX) {
  if (!positions.length) return h("span", {});
  return h("span", { class: "small mono muted" }, "trades ",
    positions.map((p, i) => [i ? ", " : "", index[String(p)] ? h("a", { href: `#/completed/${index[String(p)]}`, title: "Open this completed review" }, `#${p}`) : `#${p}`]));
}
const evidence = (positions) => evidenceLinks(positions);

export async function analyticsView(root) {
  mount(root, loading());
  const session = await activeSession({ required: false });
  const q = { scope: store.get("an.scope", "session_before_current"), upto: store.get("an.upto", 10), ack: false, tab: store.get("an.tab", "overview") };
  if (!session && q.scope.startsWith("session")) q.scope = "all_completed";
  const content = h("div", {});
  const scopeBanner = h("div", {});

  const load = async () => {
    content.replaceChildren(loading());
    const params = new URLSearchParams({ scope: q.scope });
    if (session) params.set("session_id", session.id);
    if (q.scope === "session_up_to") params.set("upto", q.upto);
    if (q.scope === "full_history") params.set("acknowledge_full_history", q.ack);
    try {
      const a = await api("GET", `/api/analytics?${params}`);
      EVIDENCE_INDEX = a.evidence_index || {};
      scopeBanner.replaceChildren(h("div", { class: `callout ${q.scope === "full_history" ? "callout-warn" : ""}` },
        h("div", { class: "eyebrow" }, "Scope"), h("strong", {}, a.scope.label),
        h("div", { class: "small mono" }, `${a.scope.trades} trades · ${a.scope.journaled_trades} with journals`,
          a.scope.excluded_open_at_current_entry ? ` · ${a.scope.excluded_open_at_current_entry} excluded (still open at the current entry)` : ""),
        a.scope.warnings.map((w) => h("div", { class: "small" }, "⚠ ", w))));
      renderTabs(a);
    } catch (e) {
      scopeBanner.replaceChildren();
      content.replaceChildren(errorBox(e));
    }
  };

  const renderTabs = (a) => {
    const tabs = [["overview", "Overview & setups"], ["calibration", "Prediction calibration"], ["dashboard", "Psychology dashboard"], ["psychology", "Psychology conditions & tags"],
      ["why", "Why did I take this trade?"], ["contradictions", "Process consistency"], ["patterns", "Repeated behaviour"]];
    const body = h("div", {});
    const bar = h("div", { class: "tabs", role: "tablist" }, tabs.map(([k, l]) => h("button", { role: "tab", class: q.tab === k ? "active" : "", "aria-selected": q.tab === k,
      onClick: () => { q.tab = k; store.set("an.tab", k); renderTabs(a); } }, l)));
    const o = a.overview;
    const views = {
      overview: () => [
        h("div", { class: "metric-grid" },
          ...[["Trades", o.n], ["Wins / losses", `${o.wins} / ${o.losses}`], ["Win rate", o.win_rate !== null ? `${o.win_rate}%` : "—"], ["Avg points", signed(o.avg_points)],
            ["Direction calls correct", o.direction_correct_rate !== null ? `${o.direction_correct_rate}%` : "—"], ["Rules followed (stated)", o.rule_compliance_rate !== null ? `${o.rule_compliance_rate}%` : "—"]]
            .map(([k, v]) => h("div", { class: "metric" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v)))),
        h("h2", { style: { marginTop: "1rem" } }, "By declared setup"),
        a.setups.length ? table([...STATS_HEAD, "Avg expected move", "Avg confidence", "Avg setup quality"],
          a.setups.map((s) => statsRow(s.setup, s, [h("td", { class: "num" }, fmt(s.avg_expected_move)), h("td", { class: "num" }, fmt(s.avg_confidence)), h("td", { class: "num" }, fmt(s.avg_setup_quality))])))
          : h("p", { class: "muted" }, "No completed journals in this scope."),
      ],
      calibration: () => {
        const c = a.calibration;
        return [
          h("div", { class: "grid-2" },
            h("div", {}, h("h2", {}, "Confidence vs outcome"), table(["Confidence", "Trades", "W/L", "Win rate", "Direction correct", "Avg pts"],
              c.confidence_buckets.map((b) => h("tr", {}, h("td", { class: "mono" }, b.bucket), h("td", { class: "num" }, b.n), h("td", { class: "num" }, `${b.wins}/${b.losses}`), h("td", {}, rate(b.win_rate)), h("td", {}, rate(b.direction_correct_rate)), h("td", { class: "num" }, signed(b.avg_points)))))),
            h("div", {}, h("h2", {}, "Setup quality vs outcome"), table(["Setup quality", "Trades", "W/L", "Win rate", "Direction correct", "Avg pts"],
              c.setup_quality_buckets.map((b) => h("tr", {}, h("td", { class: "mono" }, b.bucket), h("td", { class: "num" }, b.n), h("td", { class: "num" }, `${b.wins}/${b.losses}`), h("td", {}, rate(b.win_rate)), h("td", {}, rate(b.direction_correct_rate)), h("td", { class: "num" }, signed(b.avg_points))))))),
          h("h2", { style: { marginTop: "1rem" } }, "Expected move vs actual"),
          h("p", { class: "small" }, `Expected move achieved in ${c.expected_vs_actual.achieved_rate ?? "—"}% of trades · median actual/expected ratio ${c.expected_vs_actual.median_actual_to_expected_ratio ?? "—"}`),
          c.expected_vs_actual.trades.length ? table(["#", "Expected (pts)", "Actual max favourable in thesis window", "MFE while in trade", "Achieved"],
            c.expected_vs_actual.trades.map((x) => h("tr", {}, h("td", { class: "num" }, x.position), h("td", { class: "num" }, `+${fmt(x.expected)}`), h("td", { class: "num" }, signed(x.actual_max_favourable)), h("td", { class: "num" }, signed(x.mfe_in_trade)), h("td", {}, x.achieved ? "yes" : "no")))) : null,
          h("div", { class: "grid-2", style: { marginTop: "1rem" } },
            h("div", { class: "panel" }, h("h3", {}, "Timing expectations"), h("p", {}, h("strong", {}, c.timing.summary)),
              h("ul", { class: "small" }, Object.entries(c.timing.counts).map(([k, v]) => h("li", {}, `${k}: ${v}`)))),
            h("div", { class: "panel" }, h("h3", {}, "Invalidation quality"),
              h("p", { class: "small" }, `${c.invalidation.with_price_level} of ${c.invalidation.journaled_trades} theses had a measurable invalidation level (${c.invalidation.pct_measurable ?? "—"}%).`),
              h("p", { class: "small" }, `Price traded through the level in ${c.invalidation.traded_through_level}; position held after invalidation in ${c.invalidation.held_after_invalidation} `, evidence(c.invalidation.positions_held_after_invalidation)),
              h("p", { class: "small muted" }, c.invalidation.note))),
        ];
      },
      dashboard: () => [
        h("p", { class: "callout small" }, a.psychology_dashboard.note, ` Based on ${a.psychology_dashboard.journaled_trades} journaled trades.`),
        table([...STATS_HEAD, "Identified by"], a.psychology_dashboard.items.map((s) =>
          statsRow(s.item, s, [h("td", { class: "small muted" }, s.identified_by, s.n ? h("div", {}, evidence(s.positions)) : null)]))),
      ],
      psychology: () => [
        h("p", { class: "callout small" }, a.psychology.note),
        h("h2", {}, "Pre-trade states (recorded before the outcome)"),
        a.psychology.pre_trade_states.length ? table(STATS_HEAD, a.psychology.pre_trade_states.map((s) => statsRow(s.tag, s))) : h("p", { class: "muted" }, "None yet."),
        h("div", { class: "small muted" }, a.psychology.pre_trade_states.slice(0, 3).map((s) => h("div", {}, s.wording))),
        h("h2", { style: { marginTop: "1rem" } }, "Conditions"),
        table(STATS_HEAD, a.psychology.conditions.map((s) => statsRow(s.condition, s))),
        h("h2", { style: { marginTop: "1rem" } }, "Post-outcome psychology flags"),
        a.psychology.post_trade_flags.length ? table(STATS_HEAD, a.psychology.post_trade_flags.map((s) => statsRow(s.tag, s))) : h("p", { class: "muted" }, "None flagged yet."),
      ],
      why: () => [
        h("p", { class: "small muted" }, a.why.note),
        h("div", { class: "grid-2" },
          h("div", {}, h("h2", {}, "What I said the setup was"), groups(a.why.declared_setups)),
          h("div", {}, h("h2", {}, "What my written reasons actually mention"), groups(a.why.stated_reason_themes))),
      ],
      contradictions: () => a.contradictions.length ? a.contradictions.map((c) => h("div", { class: "panel obs" },
        h("div", { class: "row between gap" }, chip(c.label, "state-current"), h("span", { class: "small muted" }, `${c.basis} · ${c.reliability}`)),
        h("h3", { style: { marginTop: ".5rem" } }, c.title), h("p", {}, c.detail), evidence(c.evidence_positions)))
        : h("p", { class: "muted" }, "No potential inconsistencies detected in this scope."),
      patterns: () => a.patterns.length ? a.patterns.map((p) => h("div", { class: "panel obs" },
        h("div", { class: "row between gap" }, chip(p.label, "label-pattern"), h("span", { class: "small muted" }, `${p.reliability} · ${p.scope}`)),
        h("h3", { style: { marginTop: ".5rem" } }, p.title), h("p", {}, p.detail),
        p.interpretation ? h("p", { class: "small" }, chip(p.interpretation_label, "label-interp"), " ", p.interpretation) : null, evidence(p.evidence_positions)))
        : h("p", { class: "muted" }, "No repeated behaviour detected yet in this scope."),
    };
    body.replaceChildren(...[views[q.tab]()].flat());
    content.replaceChildren(bar, body);
  };

  const groups = (list) => list.length ? list.map((g) => h("details", { class: "group" },
    h("summary", {}, `${g.group} — ${g.count} trade${g.count === 1 ? "" : "s"}`, h("span", { class: "small muted" }, ` · win rate ${g.win_rate ?? "—"}% · ${g.reliability}`)),
    table(["#", "Entry", "One sentence", "Result"], g.trades.map((t) => h("tr", {}, h("td", { class: "num" }, t.position), h("td", { class: "mono small" }, (t.entry_local || "").slice(0, 16)),
      h("td", { class: "small" }, t.one_sentence || t.primary_reason || "—"), h("td", {}, t.result || "—"))))))
    : h("p", { class: "muted" }, "None yet.");

  const scopeSel = h("select", { onChange: (e) => { q.scope = e.target.value; store.set("an.scope", q.scope); q.ack = false; renderControls(); load(); } },
    Object.entries(SCOPE_LABELS).map(([k, l]) => h("option", { value: k, disabled: !session && k.startsWith("session") }, l)));
  scopeSel.value = q.scope;
  const controls = h("div", { class: "row gap" });
  const renderControls = () => controls.replaceChildren(
    h("label", { class: "field grow" }, h("span", { class: "label" }, "Analyse"), scopeSel),
    q.scope === "session_up_to" ? h("label", { class: "field" }, h("span", { class: "label" }, "Up to position"),
      h("input", { type: "number", min: 1, value: q.upto, onChange: (e) => { q.upto = Number(e.target.value); store.set("an.upto", q.upto); load(); } })) : null,
    q.scope === "full_history" ? h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: q.ack, onChange: (e) => { q.ack = e.target.checked; load(); } }),
      "I understand this includes outcomes of trades I have not reviewed yet") : null);
  renderControls();

  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, session ? `Session #${session.id}` : "No active session"), h("h1", {}, "Process analytics"),
      h("p", { class: "small muted" }, "Observations about the decision process, not a P&L dashboard. Associations are not causes."))),
    h("div", { class: "panel" }, controls, scopeBanner),
    h("div", { class: "panel" }, content));
  load();
}
