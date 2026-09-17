// Candlestick chart rendering (TradingView Lightweight Charts v4).
// The chart only draws what the API returned; in blind mode the API never returns post-entry data.
import { h, fmt } from "./lib.js";

const LC = window.LightweightCharts;

// Exchange-local wall-clock string → chart time (seconds, rendered as UTC so the axis shows exchange time)
function t(local) {
  const [d, tm] = local.split(" ");
  const [y, mo, da] = d.split("-").map(Number);
  const [hh, mi, ss] = tm.split(":").map(Number);
  return Date.UTC(y, mo - 1, da, hh, mi, ss || 0) / 1000;
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const PIVOT_COLORS = { P: "--pivot-p", R1: "--pivot-r", R2: "--pivot-r", R3: "--pivot-r", S1: "--pivot-s", S2: "--pivot-s", S3: "--pivot-s" };

export function renderChart(container, data, opts = {}) {
  const toggles = Object.assign({ ema: true, pivots: true, prevDay: false, dayOpen: false }, opts.toggles || {});
  const wrap = h("div", { class: "chart-wrap" });
  const canvas = h("div", { class: "chart-canvas" });
  const legend = h("div", { class: "chart-legend" });
  const toolbar = h("div", { class: "chart-toolbar" });
  wrap.append(toolbar, canvas, legend);
  container.replaceChildren(wrap);

  if (!data.candles.length && !(data.after_candles || []).length) {
    canvas.append(h("div", { class: "chart-empty" }, "No market data available for this moment.",
      h("br"), h("a", { href: "#/import/market" }, "Import 15-minute market data")));
    return { destroy() {} };
  }

  const chart = LC.createChart(canvas, {
    autoSize: true,
    layout: { background: { color: css("--chart-bg") }, textColor: css("--muted"), fontFamily: css("--font-mono") },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--line") },
    timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false, rightOffset: 6 },
    crosshair: { mode: 0 },
    localization: { priceFormatter: (p) => fmt(Number(p.toFixed(2))) },
  });

  const revealed = data.mode === "REVEALED";
  const up = css("--up"), down = css("--down");
  const candles = revealed ? [...data.candles, ...data.after_candles] : [...data.candles];
  const series = chart.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false });
  const E = data.cutoff_epoch;
  const rows = candles.map((c) => {
    const row = { time: t(c.local), open: c.open, high: c.high, low: c.low, close: c.close };
    if (revealed && c.epoch + 900 > E) {
      const col = c.close >= c.open ? css("--after-up") : css("--after-down");
      Object.assign(row, { color: col, wickColor: col, borderColor: col });
    }
    return row;
  });
  if (!revealed && data.forming_candle) {
    const f = data.forming_candle;
    const col = css("--forming");
    rows.push({ time: t(f.local), open: f.open, high: f.high, low: f.low, close: f.close, color: col, wickColor: col, borderColor: col });
  }
  series.setData(rows);

  const barTimes = rows.map((r) => r.time);
  const bySession = {};
  for (const c of candles) (bySession[c.session_date] ||= []).push(c);
  if (!revealed && data.forming_candle) (bySession[data.forming_candle.session_date] ||= []).push(data.forming_candle);

  if (toggles.ema && data.ema.points.length) {
    const s = chart.addLineSeries({ color: css("--ema"), lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: `EMA ${data.ema.length}` });
    s.setData(data.ema.points.map((p) => {
      const c = candles.find((x) => x.epoch === p.epoch);
      return c ? { time: t(c.local), value: p.value } : null;
    }).filter(Boolean));
  }

  if (toggles.pivots && data.pivots.sets.length) {
    for (const name of ["R3", "R2", "R1", "P", "S1", "S2", "S3"]) {
      const pts = [];
      for (const set of data.pivots.sets) {
        for (const c of bySession[set.session_date] || []) pts.push({ time: t(c.local), value: set.levels[name] });
      }
      pts.sort((a, b) => a.time - b.time);
      if (!pts.length) continue;
      const s = chart.addLineSeries({ color: css(PIVOT_COLORS[name]), lineWidth: name === "P" ? 2 : 1, lineStyle: name === "P" ? 0 : 2,
        lineType: 1, priceLineVisible: false, lastValueVisible: true, title: name, crosshairMarkerVisible: false });
      s.setData(pts);
    }
  }

  const today = data.pivots.sets.find((p) => p.session_date === data.cutoff_local.slice(0, 10));
  if (toggles.prevDay && today) {
    series.createPriceLine({ price: today.prior_day.high, color: css("--muted"), lineStyle: 1, lineWidth: 1, title: "PDH" });
    series.createPriceLine({ price: today.prior_day.low, color: css("--muted"), lineStyle: 1, lineWidth: 1, title: "PDL" });
    series.createPriceLine({ price: today.prior_day.close, color: css("--muted"), lineStyle: 3, lineWidth: 1, title: "PDC" });
  }
  if (toggles.dayOpen && data.day_open) {
    series.createPriceLine({ price: data.day_open, color: css("--muted"), lineStyle: 3, lineWidth: 1, title: "Open" });
  }

  // Entry marker at the candle containing the entry
  const entryBarTime = data.forming_candle ? t(data.forming_candle.local) : (data.candles.length ? t(data.candles[data.candles.length - 1].local) : null);
  const markers = [];
  const entryTime = data.cutoff_local.slice(11, 16);
  if (entryBarTime) {
    markers.push({ time: entryBarTime, position: "aboveBar", color: css("--accent"), shape: "arrowDown",
      text: revealed ? `ENTRY ${entryTime} · after this: unknown at the time` : `ENTRY ${entryTime} · chart ends here` });
  }
  if (data.entry.reference_price) {
    series.createPriceLine({ price: data.entry.reference_price, color: css("--accent"), lineStyle: 2, lineWidth: 1,
      title: data.entry.price_basis === "chart" ? "Entry" : "Index at entry" });
  }
  if (revealed && data.exit && data.exit.epoch) {
    const exitBar = candles.filter((c) => c.epoch <= data.exit.epoch).pop();
    if (exitBar) markers.push({ time: t(exitBar.local), position: "belowBar", color: css("--reveal"), shape: "arrowUp", text: "EXIT" });
    const lv = data.thesis_levels || {};
    if (lv.target_price) series.createPriceLine({ price: lv.target_price, color: css("--up"), lineStyle: 1, lineWidth: 1, title: "Your target" });
    if (lv.stop_price) series.createPriceLine({ price: lv.stop_price, color: css("--down"), lineStyle: 1, lineWidth: 1, title: "Your stop" });
    if (lv.invalidation_price) series.createPriceLine({ price: lv.invalidation_price, color: css("--warn"), lineStyle: 1, lineWidth: 1, title: "Invalidation" });
  }
  markers.sort((a, b) => a.time - b.time);
  series.setMarkers(markers);

  // Show the last ~90 bars, keeping the entry in view
  const n = barTimes.length;
  const entryIdx = entryBarTime ? barTimes.indexOf(entryBarTime) : n - 1;
  const to = revealed ? n + 3 : n + 5;
  const from = Math.max(0, Math.min(entryIdx - 60, to - 90));
  chart.timeScale().setVisibleLogicalRange({ from, to });

  // Toolbar toggles
  const mk = (key, label) => h("label", { class: "toggle" },
    h("input", { type: "checkbox", checked: toggles[key], onChange: (e) => { opts.onToggle && opts.onToggle({ ...toggles, [key]: e.target.checked }); } }),
    label);
  toolbar.append(
    h("span", { class: "chart-title" }, `${data.instrument} · 15m`),
    mk("ema", `EMA ${data.ema.length}`), mk("pivots", `Pivots (${data.pivots.method})`), mk("prevDay", "Prev-day H/L/C"), mk("dayOpen", "Day open"),
    h("span", { class: revealed ? "mode-badge revealed" : "mode-badge blind" }, revealed ? "REVEALED" : "BLIND · cut at " + data.cutoff_local.slice(0, 16)));

  legend.append(...[
    h("span", {}, h("i", { class: "sw", style: { background: css("--ema") } }), `EMA ${data.ema.length} (completed candles only)`),
    h("span", {}, h("i", { class: "sw", style: { background: css("--pivot-p") } }), "Pivot P"),
    h("span", {}, h("i", { class: "sw", style: { background: css("--pivot-r") } }), "R1–R3"),
    h("span", {}, h("i", { class: "sw", style: { background: css("--pivot-s") } }), "S1–S3"),
    !revealed && data.forming_candle ? h("span", {}, h("i", { class: "sw", style: { background: css("--forming") } }), "Forming candle (partial, up to entry)") : null,
    revealed ? h("span", {}, h("i", { class: "sw", style: { background: css("--after-up") } }), "Candles after entry (not known at the time)") : null,
  ].filter(Boolean));

  return { destroy() { chart.remove(); } };
}
