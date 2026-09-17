import { api, h, mount, fmt, toast, errorBox, loading, table, chip, stateChip } from "./lib.js";
import { refreshMeta } from "./app.js";

const COMMON_TZ = ["", "Asia/Kolkata", "UTC", "Europe/London", "America/New_York", "Asia/Singapore", "Asia/Dubai"];

function importTabs(active) {
  return h("div", { class: "tabs" },
    h("button", { class: active === "trades" ? "active" : "", onClick: () => (location.hash = "#/import/trades") }, "1 · Trades"),
    h("button", { class: active === "market" ? "active" : "", onClick: () => (location.hash = "#/import/market") }, "2 · Market data"),
    h("button", { class: active === "ts" ? "active" : "", onClick: () => (location.hash = "#/timestamps") }, "Timestamp issues"),
    h("button", { class: active === "excl" ? "active" : "", onClick: () => (location.hash = "#/exclusions") }, "Data coverage & exclusions"));
}

// ------------------------------------------------------------------ trades
export async function importTradesView(root) {
  const meta = await refreshMeta();
  const state = { upload: null, mapping: {}, options: { naive_timezone: "", date_order: "auto", chart_instrument: "NIFTY", row_mode: "round_trip" }, preview: null };
  const body = h("div", { class: "stack" });
  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, "Import"), h("h1", {}, "Import trades"),
      h("p", { class: "muted small" }, "Either one row per round-trip trade, or a broker tradebook with one row per fill (round trips are rebuilt). Your column names are never assumed — you map them."))),
    importTabs("trades"), body);

  const step1 = () => {
    const input = h("input", { type: "file", accept: ".csv,text/csv" });
    mount(body, h("div", { class: "panel" },
      h("h2", {}, "Step 1 · Choose your trades CSV"),
      h("div", { class: "row gap" }, input, h("button", { class: "primary", onClick: async () => {
        if (!input.files[0]) return toast("Choose a CSV file first", "error");
        const fd = new FormData();
        fd.append("file", input.files[0]);
        try {
          state.upload = await api("POST", "/api/import/trades/upload", undefined, { form: fd });
          state.mapping = { ...state.upload.suggested_mapping };
          state.options.row_mode = state.upload.suggested_row_mode || "round_trip";
          step2(true);
        } catch (e) { toast(e.message, "error"); }
      } }, "Upload"))));
  };

  const step2 = (initial = false) => {
    const up = state.upload;
    const opt = (v, label = v) => h("option", { value: v }, label);
    const fillsMode = state.options.row_mode === "fills";
    const fillLabels = meta.fill_mode_labels || {};
    const hiddenInFills = ["exit_datetime", "exit_date", "exit_time", "exit_price", "pnl", "exec_seq"];
    const modeBox = h("div", { class: "choices", role: "radiogroup", "aria-label": "Row format" },
      [["round_trip", "One row per round-trip trade (entry and exit on the row)"], ["fills", "One row per fill / execution (broker tradebook) — rebuild round trips"]]
        .map(([v, l]) => h("label", { class: "choice" }, h("input", { type: "radio", name: "row_mode", value: v, checked: state.options.row_mode === v,
          onChange: () => { state.options.row_mode = v; if (v === "fills") autoFillMapping(); step2(); } }), h("span", {}, l))));
    const autoFillMapping = () => {
      const lower = Object.fromEntries(up.headers.map((x) => [x.toLowerCase(), x]));
      const pick = (...names) => names.map((n) => lower[n]).find(Boolean);
      const guess = {
        entry_datetime: pick("order_execution_time", "execution_time", "fill_time", "trade_time", "timestamp", "datetime"),
        direction: pick("trade_type", "side", "buy/sell", "transaction_type"), quantity: pick("quantity", "qty", "filled_quantity"),
        entry_price: pick("price", "average_price", "fill_price", "trade_price"), instrument: pick("symbol", "tradingsymbol", "trading_symbol", "instrument"),
        trade_ref: pick("order_id", "trade_id"), underlying: pick("underlying", "name"), option_type: pick("opt_type", "option_type", "instrument_type"),
      };
      for (const [k, v] of Object.entries(guess)) if (v && !state.mapping[k]) state.mapping[k] = v;
      for (const k of hiddenInFills) delete state.mapping[k];
      if (state.mapping.entry_datetime) { delete state.mapping.entry_date; delete state.mapping.entry_time; }
    };
    if (initial && fillsMode) autoFillMapping();
    const mapRows = Object.entries(up.fields).filter(([field]) => !(fillsMode && hiddenInFills.includes(field))).map(([field, baseLabel]) => {
      const label = fillsMode && fillLabels[field] ? fillLabels[field] : baseLabel;
      const sel = h("select", { "aria-label": label, onChange: (e) => { state.mapping[field] = e.target.value || undefined; } },
        opt("", "— not in my file —"), up.headers.map((x) => opt(x)));
      sel.value = state.mapping[field] || "";
      const suggested = up.suggested_mapping[field];
      return h("tr", {}, h("td", {}, h("strong", {}, label), ["entry_datetime", "entry_date"].includes(field) ? h("span", { class: "req" }, "*") : null),
        h("td", {}, sel), h("td", { class: "small muted" }, suggested ? `suggested: ${suggested}` : ""));
    });
    const profiles = Object.entries(up.column_profiles).map(([col, p]) => h("tr", {},
      h("td", { class: "mono" }, col), h("td", {}, chip(p.kind)), h("td", { class: "mono small" }, p.examples.join("  ·  "))));
    const samplesBox = h("div", {});
    const tzSel = h("select", { onChange: (e) => (state.options.naive_timezone = e.target.value) }, COMMON_TZ.map((z) => opt(z, z || "— not confirmed —")));
    tzSel.value = state.options.naive_timezone;
    const dateSel = h("select", { onChange: (e) => (state.options.date_order = e.target.value) },
      opt("auto", "Auto (flag ambiguous dates)"), opt("DMY", "DD/MM/YYYY"), opt("MDY", "MM/DD/YYYY"));
    dateSel.value = state.options.date_order;
    const chartInst = h("input", { type: "text", value: state.options.chart_instrument, onInput: (e) => (state.options.chart_instrument = e.target.value) });

    mount(body,
      h("div", { class: "panel" },
        h("h2", {}, "Row format"), modeBox,
        fillsMode ? h("p", { class: "small muted" }, "Fills are grouped per exact symbol and walked in time order. A trade opens when the position leaves zero and closes when it returns to zero; adds and partial exits stay in the same trade; a fill that flips the position starts a new trade. Entry/exit prices are quantity-weighted averages; P&L is realised, before charges. Positions that never return to zero (e.g. expired options) have no exit.") : null),
      h("div", { class: "panel" },
        h("h2", {}, `Step 2 · Map columns — ${up.row_count} rows`),
        h("p", { class: "small muted" }, "Map either a single entry date+time column, or separate entry date and entry time columns. Unavailable fields can stay blank."),
        table(["Internal field", "Your column", ""], mapRows)),
      h("div", { class: "panel" },
        h("h2", {}, "Timestamps"),
        h("div", { class: "grid-3" },
          h("label", { class: "field" }, h("span", { class: "label" }, "Timezone of timestamps WITHOUT an offset"), tzSel,
            h("div", { class: "field-help" }, "Values with an explicit offset (+05:30, Z) are converted exactly. Values without one are only accepted once you confirm their timezone here.")),
          h("label", { class: "field" }, h("span", { class: "label" }, "Date format"), dateSel),
          h("label", { class: "field" }, h("span", { class: "label" }, "Chart instrument (market data symbol)"), chartInst,
            h("div", { class: "field-help" }, "Used when a trade's symbol doesn't start with a known index name.")))),
      h("div", { class: "panel" },
        h("h2", {}, "Your columns"),
        h("p", { class: "small muted" }, "Numbers and dates are shown as shapes (9 = digit) so exits and P&L are not revealed before review."),
        table(["Column", "Kind", "Shape / examples"], profiles),
        h("div", { class: "row gap", style: { marginTop: ".75rem" } },
          h("button", { class: "linkish", onClick: async () => {
            if (!confirm("Raw rows can show exit prices and P&L, which may bias your later blind review. Show them anyway?")) return;
            const rows = await api("GET", `/api/import/trades/${up.batch_id}/samples`);
            samplesBox.replaceChildren(table(up.headers, rows.map((r) => h("tr", {}, up.headers.map((x) => h("td", { class: "small" }, r[x]))))));
          } }, "Show raw sample rows (may reveal outcomes)")), samplesBox),
      h("div", { class: "row gap end" },
        h("button", { onClick: step1 }, "Start over"),
        h("button", { class: "primary", onClick: async () => {
          try {
            state.preview = await api("POST", `/api/import/trades/${up.batch_id}/stage`, { mapping: state.mapping, options: state.options });
            step3();
          } catch (e) { toast(e.message, "error"); }
        } }, "Normalize & build chronological preview →")));
  };

  const step3 = () => {
    const p = state.preview;
    const ack = h("input", { type: "checkbox", id: "ack-order" });
    const rows = p.rows.map((r) => h("tr", { class: r.status !== "OK" ? "is-current" : "" },
      h("td", { class: "num" }, r.chronological_position),
      h("td", {}, r.instrument, r.chart_instrument !== r.instrument ? h("div", { class: "small muted" }, `chart: ${r.chart_instrument}`) : null),
      h("td", { class: "mono small" }, r.original_entry),
      h("td", { class: "mono" }, r.normalized_entry),
      h("td", { class: "small mono" }, r.tz_source),
      h("td", { class: "num" }, fmt(r.entry_price)),
      h("td", {}, r.direction || "—"),
      h("td", {}, r.trade_ref ?? "—", h("div", { class: "small muted" }, `row ${r.import_row}`)),
      h("td", {}, chip(r.status, r.status === "OK" ? "" : "state-current"), r.warnings.map((w) => h("div", { class: "small muted" }, w)))));
    mount(body,
      h("div", { class: "panel" },
        h("h2", {}, "Step 3 · Chronological preview"),
        h("p", { class: "small" }, `Exchange timezone: `, h("strong", {}, p.exchange_timezone), ` · Ordering: `, h("span", { class: "mono small" }, p.sort_method)),
        p.existing_confirmed_trades ? h("p", { class: "small muted" }, `Positions include ${p.existing_confirmed_trades} previously imported trade(s).`) : null,
        p.interleaves_with_existing ? h("div", { class: "callout callout-warn" }, `${p.interleaves_with_existing} new trade(s) fall before trades already in the sequence. Active sessions will review them at their chronological position; this is logged.`) : null,
        p.blocking_issues.map((b) => h("div", { class: "callout callout-danger" }, h("strong", {}, "Needs your decision: "), b.message)),
        h("p", { class: "small" }, h("strong", {}, `${p.total_rows} trades`), p.row_mode === "fills" ? " rebuilt from fills" : "", " · ",
          Object.entries(p.status_counts || {}).map(([k, v]) => `${k}: ${v}`).join(" · "), p.unresolved_total ? ` · ${p.unresolved_total} unresolved` : ""),
        p.rows_truncated ? h("div", { class: "callout small" }, `Showing ${p.rows.length} of ${p.total_rows} rows (all flagged rows first, then the earliest). The full order is applied on confirmation.`) : null,
        table(["Position", "Instrument", "Original entry value", "Normalized entry", "TZ source", "Entry", "Direction", "Trade ID", "Status"], rows)),
      p.unresolved.length ? h("div", { class: "panel" },
        h("h2", {}, `Chronology unresolved (${p.unresolved_total})`),
        h("p", { class: "small" }, "These rows have no valid entry timestamp. They will be imported but kept OUT of the sequence until you correct or confirm the timestamp."),
        table(["Row", "Trade ID", "Instrument", "Original value", "Issue"], p.unresolved.map((u) => h("tr", {},
          h("td", { class: "num" }, u.import_row), h("td", {}, u.trade_ref ?? "—"), h("td", {}, u.instrument),
          h("td", { class: "mono small" }, u.original_entry || "(empty)"), h("td", {}, stateChip("REQUIRES_TIMESTAMP_REVIEW"), " ", u.issue))))) : null,
      h("div", { class: "panel" },
        h("label", { class: "toggle", for: "ack-order", style: { fontSize: "1rem", color: "var(--ink)" } }, ack, h("strong", {}, "These trades will be reviewed in this chronological order.")),
        h("div", { class: "row gap end", style: { marginTop: ".75rem" } },
          h("button", { onClick: step2 }, "Go Back and Fix Mapping"),
          p.unresolved.length || p.blocking_issues.length ? h("button", { onClick: () => {
            if (p.blocking_issues.length) { step2(); toast("Choose the timezone / date format in the Timestamps section, then rebuild the preview"); }
            else toast("Confirm the import first; unresolved rows are then corrected on the Timestamp issues page");
          } }, "Resolve Timestamp Issues") : null,
          h("button", { class: "primary", disabled: !p.can_confirm, onClick: async () => {
            if (!ack.checked) return toast("Tick the confirmation first", "error");
            try {
              const r = await api("POST", `/api/import/trades/${p.batch_id}/confirm`, { acknowledge_order: true });
              toast(`Imported ${r.trades} trades in chronological order`);
              location.hash = r.unresolved ? "#/timestamps" : "#/";
            } catch (e) { toast(e.message, "error"); }
          } }, "Confirm Chronological Order"))));
  };
  step1();
}

// ------------------------------------------------------------------ market data
export async function importMarketView(root) {
  const meta = await refreshMeta();
  const body = h("div", { class: "stack" });
  const coverage = meta.coverage.length ? table(["Instrument", "Timeframe", "Bars", "Sessions", "First", "Last"],
    meta.coverage.map((c) => h("tr", {}, h("td", {}, c.instrument), h("td", {}, c.timeframe), h("td", { class: "num" }, fmt(c.bars)),
      h("td", { class: "num" }, c.sessions), h("td", { class: "mono small" }, c.first), h("td", { class: "mono small" }, c.last))))
    : h("p", { class: "muted" }, "No market data yet.");

  const fields = ["timestamp", "time", "open", "high", "low", "close", "volume"];
  const form = { instrument: "NIFTY", timeframe: "auto", naive_timezone: "Asia/Kolkata", label: "open",
    mapping: { timestamp: "timestamp", open: "open", high: "high", low: "low", close: "close", volume: "volume" } };
  const result = h("div", {});
  const fileInput = h("input", { type: "file", accept: ".csv,text/csv" });
  const pathInput = h("input", { type: "text", placeholder: "~/path/to/nifty_15min.csv" });
  const mapBox = h("div", { class: "grid-3" });
  const renderMap = (headers) => mapBox.replaceChildren(...fields.map((f) => {
    const el = headers ? h("select", { onChange: (e) => (form.mapping[f] = e.target.value || undefined) },
      h("option", { value: "" }, "—"), headers.map((x) => h("option", { value: x }, x)))
      : h("input", { type: "text", value: form.mapping[f] || "", onInput: (e) => (form.mapping[f] = e.target.value || undefined) });
    el.value = form.mapping[f] || "";
    return h("label", { class: "field" }, h("span", { class: "label" }, f + (["timestamp", "open", "high", "low", "close"].includes(f) ? " *" : "")), el,
      f === "time" ? h("div", { class: "field-help" }, "only if time is in a separate column") : null);
  }));
  renderMap(null);
  const sel = (key, options) => { const s = h("select", { onChange: (e) => (form[key] = e.target.value) }, options.map(([v, l]) => h("option", { value: v }, l))); s.value = form[key]; return s; };

  const show = (summary) => result.replaceChildren(h("div", { class: "callout" },
    h("strong", {}, `Imported ${fmt(summary.imported)} ${summary.timeframe} bars for ${summary.instrument}`), ` (${summary.first} → ${summary.last})`,
    h("div", { class: "small mono" }, `rejected: ${summary.rejected_bad_timestamp} bad timestamps, ${summary.rejected_bad_values} bad values, ${summary.rejected_invalid_ohlc} invalid OHLC · duplicates collapsed: ${summary.duplicates_collapsed} · timezone: ${summary.timezone_source} · bar label: ${summary.bar_label}`),
    summary.warnings.map((w) => h("div", { class: "callout callout-warn small" }, w))));

  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, "Import"), h("h1", {}, "Market data"),
      h("p", { class: "muted small" }, "15-minute OHLC is required for the chart. 1-minute data is optional but lets the tool rebuild the candle that was forming at your exact entry time and measure excursions precisely."))),
    importTabs("market"),
    h("div", { class: "panel" }, h("h2", {}, "Loaded"), coverage),
    h("div", { class: "panel" },
      h("h2", {}, "Import a file"),
      h("div", { class: "grid-3" },
        h("label", { class: "field" }, h("span", { class: "label" }, "Instrument symbol"), h("input", { type: "text", value: form.instrument, onInput: (e) => (form.instrument = e.target.value) })),
        h("label", { class: "field" }, h("span", { class: "label" }, "Timeframe"), sel("timeframe", [["auto", "Detect"], ["15m", "15 minutes"], ["1m", "1 minute"]])),
        h("label", { class: "field" }, h("span", { class: "label" }, "Timezone of timestamps without offset"), sel("naive_timezone", COMMON_TZ.map((z) => [z, z || "— not confirmed —"]))),
        h("label", { class: "field" }, h("span", { class: "label" }, "Bar timestamp marks the bar's"), sel("label", [["open", "OPEN (10:30 = 10:30–10:45)"], ["close", "CLOSE (10:45 = 10:30–10:45)"]]),
          h("div", { class: "field-help" }, "Critical for the temporal cutoff. If unsure, check whether sessions start at 09:15 (open) or 09:30 (close)."))),
      h("h3", {}, "Column mapping"), mapBox,
      h("div", { class: "grid-2" },
        h("div", {}, h("h3", {}, "Option A · upload (files up to ~50 MB)"),
          h("div", { class: "row gap" }, fileInput,
            h("button", { onClick: async () => {
              if (!fileInput.files[0]) return toast("Choose a file", "error");
              const fd = new FormData(); fd.append("file", fileInput.files[0]);
              const p = await api("POST", "/api/import/market/preview", undefined, { form: fd });
              Object.assign(form.mapping, p.suggested_mapping); renderMap(p.headers);
              toast(`${p.row_count} rows; columns detected`);
            } }, "Read columns"),
            h("button", { class: "primary", onClick: async () => {
              if (!fileInput.files[0]) return toast("Choose a file", "error");
              const fd = new FormData();
              fd.append("file", fileInput.files[0]); fd.append("mapping", JSON.stringify(form.mapping)); fd.append("instrument", form.instrument);
              fd.append("timeframe", form.timeframe); fd.append("naive_timezone", form.naive_timezone); fd.append("label", form.label);
              result.replaceChildren(loading("Importing…"));
              try { show(await api("POST", "/api/import/market/commit", undefined, { form: fd })); refreshMeta(); } catch (e) { result.replaceChildren(errorBox(e)); }
            } }, "Import"))),
        h("div", {}, h("h3", {}, "Option B · local file path (large files)"),
          h("div", { class: "row gap" }, pathInput,
            h("button", { class: "primary", onClick: async () => {
              result.replaceChildren(loading("Importing… large 1-minute files can take a minute"));
              try { show(await api("POST", "/api/import/market/from-path", { ...form, path: pathInput.value.trim() })); } catch (e) { result.replaceChildren(errorBox(e)); }
            } }, "Import from path")))),
      result));
}

// ------------------------------------------------------------------ timestamp issues
export async function timestampsView(root) {
  const list = await api("GET", "/api/trades/unresolved");
  const meta = await refreshMeta();
  const contractIssues = list.filter((u) => u.issue_kind === "contract_first_sell");
  const tsIssues = list.filter((u) => u.issue_kind !== "contract_first_sell");

  const byContract = new Map();
  for (const u of contractIssues) {
    const key = `${u.batch_id}|${u.instrument}`;
    if (!byContract.has(key)) byContract.set(key, { batch_id: u.batch_id, instrument: u.instrument, trades: [] });
    byContract.get(key).trades.push(u);
  }
  const resolve = async (c, decision) => {
    const label = decision === "real_short" ? "keep these trades as real shorts" : "treat the first sell as closing a position opened before the file, and re-pair the rest";
    if (!confirm(`${c.instrument}: ${label}? This is logged.`)) return;
    try {
      const r = await api("POST", `/api/import/trades/${c.batch_id}/resolve-contract`, { symbol: c.instrument, decision });
      toast(`${c.instrument}: ${r.trades} trade(s) now in the sequence${r.excluded_fill_rows.length ? `; excluded file row ${r.excluded_fill_rows.join(", ")}` : ""}`);
      timestampsView(root);
    } catch (e) { toast(e.message, "error"); }
  };
  const contractPanel = byContract.size ? h("div", { class: "panel" },
    h("h2", {}, `Contracts that start with a sell (${byContract.size})`),
    h("p", { class: "small" }, "The first fill for these contracts is a SELL. Either it was a real short, or the opening BUY is missing from the file (the position was opened before the file starts). Until you decide, the contract's trades stay out of the chronological sequence."),
    h("p", { class: "small muted" }, "Evidence: a real short normally leaves the position flat by the end of the file; a missing opening buy leaves a leftover position that never closes. Positions held to expiry can blur this, so check the fills if unsure."),
    table(["Contract", "Trades as currently paired", "Evidence", "Decision"], [...byContract.values()].map((c) => {
      const ev = c.trades[0].evidence || {};
      const pos = (v) => (v > 0 ? "+" : "") + fmt(v);
      const sugg = { real_short: "Suggests: real short", missing_opening_buy: "Suggests: opening buy missing", unclear: "Unclear — check the fills" }[ev.suggestion] || "—";
      return h("tr", {},
        h("td", { class: "mono" }, c.instrument),
        h("td", { class: "small" }, c.trades.map((t) => h("div", {}, `${t.entry_local ? t.entry_local.slice(0, 16) : "—"} · ${t.direction || "—"}`))),
        h("td", { class: "small" },
          h("div", {}, `As a real short: ends ${pos(ev.as_is_end_position)}, flat ${ev.as_is_times_flat}×`),
          h("div", {}, `If opening buy missing: ends ${pos(ev.missing_buy_end_position)}, flat ${ev.missing_buy_times_flat}×`),
          h("div", {}, h("strong", {}, sugg))),
        h("td", {}, h("div", { class: "row gap" },
          h("button", { class: ev.suggestion === "real_short" ? "primary" : "", onClick: () => resolve(c, "real_short") }, "Real short — keep as is"),
          h("button", { class: ev.suggestion === "missing_opening_buy" ? "primary" : "", onClick: () => resolve(c, "missing_opening_buy") }, "Opening buy missing — re-pair"))));
    }))) : null;

  const rows = tsIssues.map((u) => {
    const value = h("input", { type: "text", placeholder: "YYYY-MM-DD HH:MM:SS" });
    const tz = h("input", { type: "text", value: meta.settings.exchange_timezone });
    const reason = h("input", { type: "text", placeholder: "how you confirmed it" });
    const fixable = u.issue_kind !== "fill_excluded";
    return h("tr", {},
      h("td", {}, u.trade_ref ?? "—", h("div", { class: "small muted" }, `row ${u.import_row}`)),
      h("td", {}, u.instrument),
      h("td", { class: "mono small" }, u.entry_original || "(empty)", h("div", {}, stateChip("REQUIRES_TIMESTAMP_REVIEW")), h("div", { class: "small muted" }, u.chrono_issue)),
      fixable ? h("td", {}, value) : h("td", { class: "small muted", colspan: 3 }, "A single fill can't be placed on its own: fix the row in the CSV and re-import."),
      fixable ? h("td", {}, tz) : null, fixable ? h("td", {}, reason) : null,
      h("td", {}, fixable ? h("button", { class: "primary", onClick: async () => {
        try {
          const r = await api("POST", `/api/trades/${u.trade_id}/resolve-timestamp`, { value: value.value, timezone: tz.value, reason: reason.value });
          toast(`Placed in sequence at ${r.normalized}`); timestampsView(root);
        } catch (e) { toast(e.message, "error"); }
      } }, "Confirm timestamp") : null));
  });
  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, "Import"), h("h1", {}, "Chronology unresolved"),
      h("p", { class: "muted small" }, "These trades are excluded from the chronological sequence until resolved. Every decision and correction is recorded."))),
    importTabs("ts"),
    contractPanel,
    h("div", { class: "panel" }, h("h2", {}, "Timestamp issues"), tsIssues.length ? table(["Trade", "Instrument", "Original", "Correct entry time", "Timezone", "Reason", ""], rows)
      : h("p", {}, "No unresolved timestamps. ", h("a", { href: "#/" }, "Back to sessions"))));
}

// ------------------------------------------------------------------ data coverage & exclusions
export async function exclusionsView(root) {
  const [meta, cov, excluded] = await Promise.all([refreshMeta(), api("GET", "/api/trades/coverage"), api("GET", "/api/trades/excluded")]);
  const reasons = meta.exclusion_reasons;
  const reasonSel = h("select", {}, reasons.map((r) => h("option", { value: r }, r)));
  const note = h("input", { type: "text", placeholder: "Optional note (required for Other)" });
  const doExclude = async (body, label) => {
    if (!confirm(`Exclude ${label} from review? They leave the sequence and analytics, can be restored later, and the action is logged.`)) return;
    try {
      const r = await api("POST", "/api/trades/exclude", { ...body, reason: reasonSel.value, note: note.value });
      toast(`Excluded ${r.excluded} trade(s)${r.refused_reviewed ? `; ${r.refused_reviewed} already reviewed and kept` : ""}`);
      exclusionsView(root);
    } catch (e) { toast(e.message, "error"); }
  };
  const restore = async (body, label) => {
    try { const r = await api("POST", "/api/trades/restore", body); toast(`Restored ${r.restored} trade(s) to the sequence`); exclusionsView(root); }
    catch (e) { toast(e.message, "error"); }
  };

  const coveragePanel = h("div", { class: "panel" },
    h("h2", {}, `Trades without chart data (${cov.trades_without_data} of ${cov.active_trades})`),
    cov.groups.length ? [
      h("p", { class: "small" }, "These trades have no imported 15-minute bars for their chart instrument on the entry day, so their blind review has no chart.",
        cov.first_positions_without_data.length ? ` The earliest are at sequence positions ${cov.first_positions_without_data.join(", ")}.` : ""),
      h("div", { class: "grid-3" },
        h("label", { class: "field" }, h("span", { class: "label" }, "Reason for exclusion"), reasonSel),
        h("label", { class: "field" }, h("span", { class: "label" }, "Note"), note)),
      table(["Chart instrument", "Trades", "First entry", "Last entry", ""], cov.groups.map((g) => h("tr", {},
        h("td", { class: "mono" }, g.instrument),
        h("td", { class: "num" }, g.trades, g.unresolved ? h("div", { class: "small muted" }, `${g.unresolved} awaiting a decision`) : null),
        h("td", { class: "mono small" }, (g.first || "").slice(0, 16)), h("td", { class: "mono small" }, (g.last || "").slice(0, 16)),
        h("td", {}, h("button", { onClick: () => doExclude({ no_market_data: true, instrument: g.instrument }, `${g.trades} ${g.instrument} trade(s) without data`) }, `Exclude ${g.trades}`))))),
      h("div", { class: "row gap end", style: { marginTop: ".75rem" } },
        h("button", { class: "primary", onClick: () => doExclude({ no_market_data: true }, `all ${cov.trades_without_data} trades without chart data`) },
          `Exclude all ${cov.trades_without_data} trades without chart data`)),
    ] : h("p", { class: "muted" }, "Every trade in the sequence has chart data on its entry day."));

  const excludedPanel = h("div", { class: "panel" },
    h("div", { class: "row between" }, h("h2", {}, `Excluded trades (${excluded.length})`),
      excluded.length ? h("button", { onClick: () => { if (confirm(`Restore all ${excluded.length} excluded trades to the sequence?`)) restore({ all: true }); } }, "Restore all") : null),
    excluded.length ? table(["Entry", "Instrument", "Direction", "Reason", "Excluded", ""], excluded.map((x) => h("tr", {},
      h("td", { class: "mono small" }, (x.entry_local || "—").slice(0, 16)), h("td", { class: "mono small" }, x.instrument),
      h("td", {}, x.direction || "—"), h("td", { class: "small" }, x.exclusion_reason), h("td", { class: "mono small" }, (x.excluded_at || "").replace("T", " ").slice(0, 16)),
      h("td", {}, h("button", { class: "linkish", onClick: () => restore({ trade_ids: [x.trade_id] }) }, "Restore")))))
      : h("p", { class: "muted" }, "No trades are excluded."));

  mount(root,
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "eyebrow" }, "Import"), h("h1", {}, "Data coverage & exclusions"),
      h("p", { class: "muted small" }, "Exclude trades you can't review meaningfully, for example when there is no market data. Excluded trades are not deleted, still count toward day P&L context, and can be restored. Trades whose thesis is already locked can't be excluded."))),
    importTabs("excl"), coveragePanel, excludedPanel);
}
