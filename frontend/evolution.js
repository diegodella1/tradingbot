const $ = (selector) => document.querySelector(selector);
const SVG_NS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 15000;
const policyPalette = ["#00A3FF", "#34D399", "#F59E0B", "#b4c5ff", "#FB7185"];

const state = {
  data: null,
  selectedPositionId: null,
  timer: null,
  failures: 0,
};

function fmtPct(value, digits = 1) {
  return value == null ? "--" : `${(Number(value) * 100).toFixed(digits)}%`;
}

function fmtUsd(value, digits = 4) {
  if (value == null) return "--";
  const number = Number(value);
  return `${number >= 0 ? "+" : "-"}$${Math.abs(number).toFixed(digits)}`;
}

function fmtNumber(value, digits = 2) {
  return value == null ? "--" : Number(value).toFixed(digits);
}

function fmtDate(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function shortVersion(version) {
  if (!version) return "No active policy";
  return version.replace("btc-updown-", "");
}

function colorForPolicy(version, policies) {
  const index = Math.max(0, policies.findIndex((policy) => policy.version === version));
  return policyPalette[index % policyPalette.length];
}

function svgElement(name, attributes = {}, text = null) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  if (text != null) node.textContent = String(text);
  return node;
}

function domElement(name, className, text = null) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function setTone(element, value) {
  element.classList.remove("metric-positive", "metric-negative", "metric-neutral");
  element.classList.add(value > 0 ? "metric-positive" : value < 0 ? "metric-negative" : "metric-neutral");
}

function renderCurrent(data) {
  const current = data.current || {};
  const metrics = current.metrics || {};
  $("#plainSummary").textContent = current.plain_summary || "No current experiment summary.";
  $("#kpiPolicy").textContent = shortVersion(current.policy_version);
  $("#kpiSampleState").textContent = String(current.sample_state || "unknown").replaceAll("_", " ");
  $("#kpiWinRate").textContent = fmtPct(metrics.win_rate);
  $("#kpiInterval").textContent = `95% interval ${fmtPct(metrics.win_rate_ci95_low)}–${fmtPct(metrics.win_rate_ci95_high)}`;
  $("#kpiBreakEven").textContent = fmtPct(metrics.breakeven_win_rate);
  const gap = metrics.win_rate == null || metrics.breakeven_win_rate == null ? null : metrics.win_rate - metrics.breakeven_win_rate;
  $("#kpiGap").textContent = gap == null ? "gap --" : `gap ${gap >= 0 ? "+" : ""}${fmtPct(gap)}`;
  $("#kpiPnl").textContent = fmtUsd(metrics.pnl_usdc);
  setTone($("#kpiPnl"), Number(metrics.pnl_usdc || 0));
  $("#kpiProfitFactor").textContent = `PF ${fmtNumber(metrics.profit_factor)}`;
  $("#kpiFillRate").textContent = fmtPct(current.fill_rate);
  $("#kpiTrades").textContent = `${metrics.trades || 0} settlements`;
}

function chartFrame(svg, series, options) {
  const width = Math.max(760, series.length * 6 + 100);
  const height = options.height || 310;
  const margin = { top: 40, right: 28, bottom: 38, left: 64 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  const x = (index) => margin.left + (series.length <= 1 ? innerWidth / 2 : index / (series.length - 1) * innerWidth);
  const y = (value) => margin.top + (options.max - value) / (options.max - options.min || 1) * innerHeight;

  options.ticks.forEach((tick) => {
    svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: y(tick), y2: y(tick), class: "chart-grid-line" }));
    svg.append(svgElement("text", { x: margin.left - 10, y: y(tick) + 4, "text-anchor": "end", class: "chart-axis-label" }, options.formatTick(tick)));
  });
  svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: height - margin.bottom, y2: height - margin.bottom, class: "chart-axis-line" }));
  svg.append(svgElement("text", { x: margin.left, y: height - 12, class: "chart-axis-label" }, "Older"));
  svg.append(svgElement("text", { x: width - margin.right, y: height - 12, "text-anchor": "end", class: "chart-axis-label" }, "Recent"));
  return { width, height, margin, innerWidth, innerHeight, x, y };
}

function policyRanges(series) {
  const ranges = [];
  series.forEach((point, index) => {
    const latest = ranges.at(-1);
    if (!latest || latest.version !== point.policy_version) {
      ranges.push({ version: point.policy_version, start: index, end: index });
    } else {
      latest.end = index;
    }
  });
  return ranges;
}

function drawPolicyBands(svg, frame, series, policies) {
  policyRanges(series).forEach((range) => {
    const left = frame.x(range.start) - 3;
    const right = frame.x(range.end) + 3;
    const color = colorForPolicy(range.version, policies);
    svg.prepend(svgElement("rect", {
      x: left, y: frame.margin.top, width: Math.max(6, right - left), height: frame.innerHeight,
      fill: color, opacity: 0.035,
    }));
    svg.append(svgElement("text", {
      x: left + 7, y: frame.margin.top - 13, fill: color, class: "chart-era-label",
    }, shortVersion(range.version)));
  });
}

function groupedPaths(series, valueKey, frame) {
  const groups = [];
  series.forEach((point, index) => {
    const value = point[valueKey];
    if (value == null) return;
    let group = groups.at(-1);
    if (!group || group.version !== point.policy_version) {
      group = { version: point.policy_version, values: [] };
      groups.push(group);
    }
    group.values.push([frame.x(index), frame.y(Number(value))]);
  });
  return groups;
}

function pathData(values) {
  return values.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
}

function makePointInteractive(circle, point) {
  circle.setAttribute("tabindex", "0");
  circle.setAttribute("role", "button");
  circle.setAttribute("aria-label", `${shortVersion(point.policy_version)} trade ${point.trade_number_policy}, ${point.outcome}, win rate ${fmtPct(point.win_rate)}, PnL ${fmtUsd(point.cumulative_pnl_usdc)}`);
  const select = () => selectPoint(point.position_id);
  circle.addEventListener("click", select);
  circle.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select();
    }
  });
}

function renderWinRateChart(data) {
  const svg = $("#winRateChart");
  const series = data.series || [];
  if (!series.length) return renderEmptyChart(svg, "Waiting for first verified settlement");
  const frame = chartFrame(svg, series, { min: 0, max: 1, ticks: [0, 0.25, 0.5, 0.68, 0.75, 1], formatTick: (v) => fmtPct(v, 0) });
  drawPolicyBands(svg, frame, series, data.policies || []);

  const targetY = frame.y(Number(data.target?.reference_win_rate || 0.68));
  svg.append(svgElement("line", { x1: frame.margin.left, x2: frame.width - frame.margin.right, y1: targetY, y2: targetY, class: "chart-target-line" }));
  svg.append(svgElement("text", { x: frame.width - frame.margin.right - 6, y: targetY - 7, "text-anchor": "end", class: "chart-target-label" }, "68% reference"));

  groupedPaths(series, "breakeven_win_rate", frame).forEach((group) => {
    svg.append(svgElement("path", { d: pathData(group.values), class: "chart-breakeven-path" }));
  });
  groupedPaths(series, "win_rate", frame).forEach((group) => {
    svg.append(svgElement("path", { d: pathData(group.values), fill: "none", stroke: colorForPolicy(group.version, data.policies || []), class: "chart-result-path" }));
  });
  series.forEach((point, index) => {
    const selected = point.position_id === state.selectedPositionId;
    const circle = svgElement("circle", {
      cx: frame.x(index), cy: frame.y(Number(point.win_rate)), r: selected ? 6 : 3,
      class: `chart-point ${point.outcome === "WON" ? "chart-point-win" : "chart-point-loss"}${selected ? " chart-point-selected" : ""}`,
    });
    makePointInteractive(circle, point);
    svg.append(circle);
  });
}

function renderPnlChart(data) {
  const svg = $("#pnlChart");
  const series = data.series || [];
  if (!series.length) return renderEmptyChart(svg, "No PnL evidence yet");
  const values = series.map((point) => Number(point.global_pnl_usdc || 0));
  const rawMin = Math.min(0, ...values);
  const rawMax = Math.max(0, ...values);
  const padding = Math.max(0.25, (rawMax - rawMin) * 0.12);
  const min = rawMin - padding;
  const max = rawMax + padding;
  const ticks = [min, (min + max) / 2, max];
  const frame = chartFrame(svg, series, { min, max, ticks, height: 280, formatTick: (v) => fmtUsd(v, 1) });
  drawPolicyBands(svg, frame, series, data.policies || []);
  if (min < 0 && max > 0) svg.append(svgElement("line", { x1: frame.margin.left, x2: frame.width - frame.margin.right, y1: frame.y(0), y2: frame.y(0), class: "chart-zero-line" }));
  const valuesPath = series.map((point, index) => [frame.x(index), frame.y(Number(point.global_pnl_usdc || 0))]);
  svg.append(svgElement("path", { d: pathData(valuesPath), class: "chart-pnl-path" }));
  series.forEach((point, index) => {
    const selected = point.position_id === state.selectedPositionId;
    const circle = svgElement("circle", {
      cx: frame.x(index), cy: frame.y(Number(point.global_pnl_usdc || 0)), r: selected ? 6 : 2.5,
      class: `chart-point ${point.outcome === "WON" ? "chart-point-win" : "chart-point-loss"}${selected ? " chart-point-selected" : ""}`,
    });
    makePointInteractive(circle, point);
    svg.append(circle);
  });
}

function renderEmptyChart(svg, message) {
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 760 240");
  svg.setAttribute("width", "760");
  svg.setAttribute("height", "240");
  svg.append(svgElement("text", { x: 380, y: 120, "text-anchor": "middle", class: "chart-empty" }, message));
}

function addDetailMetric(container, label, value, tone = "") {
  const wrapper = domElement("div", "detail-metric");
  wrapper.append(domElement("dt", "", label));
  wrapper.append(domElement("dd", tone, value));
  container.append(wrapper);
}

function renderPointDetail(point) {
  if (!point) return;
  $("#pointDetailTitle").textContent = `${shortVersion(point.policy_version)} · trade ${point.trade_number_policy}`;
  $("#detailOutcome").textContent = point.outcome;
  $("#detailOutcome").className = `evidence-badge ${point.outcome === "WON" ? "evidence-win" : "evidence-loss"}`;
  $("#detailWhy").textContent = point.why_it_matters || "Verified paper settlement.";
  const metrics = $("#detailMetrics");
  metrics.replaceChildren();
  addDetailMetric(metrics, "Settled", fmtDate(point.occurred_at));
  addDetailMetric(metrics, "Trade PnL", fmtUsd(point.trade_pnl_usdc), Number(point.trade_pnl_usdc) >= 0 ? "metric-positive" : "metric-negative");
  addDetailMetric(metrics, "Policy PnL", fmtUsd(point.cumulative_pnl_usdc), Number(point.cumulative_pnl_usdc) >= 0 ? "metric-positive" : "metric-negative");
  addDetailMetric(metrics, "Win rate", fmtPct(point.win_rate));
  addDetailMetric(metrics, "Break-even", fmtPct(point.breakeven_win_rate));
  addDetailMetric(metrics, "Entry price", fmtPct(point.entry_price));
  addDetailMetric(metrics, "Profit factor", fmtNumber(point.profit_factor));
  addDetailMetric(metrics, "Max drawdown", fmtUsd(-Number(point.max_drawdown_usdc || 0)));
}

function selectPoint(positionId) {
  state.selectedPositionId = Number(positionId);
  const point = state.data?.series?.find((item) => item.position_id === state.selectedPositionId);
  renderPointDetail(point);
  renderWinRateChart(state.data);
  renderPnlChart(state.data);
}

function renderPolicies(data) {
  const container = $("#policyEras");
  container.replaceChildren();
  (data.policies || []).slice().reverse().forEach((policy) => {
    const metrics = policy.metrics || {};
    const card = domElement("article", `policy-era-card${policy.is_active ? " policy-era-active" : ""}`);
    const header = domElement("div", "policy-era-header");
    const titleWrap = domElement("div");
    titleWrap.append(domElement("span", "policy-era-label", policy.is_active ? "Active paper policy" : "Historical policy"));
    titleWrap.append(domElement("h3", "", shortVersion(policy.version)));
    header.append(titleWrap, domElement("span", `evidence-badge evidence-${policy.status === "stopped" ? "loss" : "neutral"}`, policy.status));
    card.append(header);
    const grid = domElement("dl", "policy-era-metrics");
    addDetailMetric(grid, "Settlements", metrics.trades || 0);
    addDetailMetric(grid, "WR", fmtPct(metrics.win_rate));
    addDetailMetric(grid, "Break-even", fmtPct(metrics.breakeven_win_rate));
    addDetailMetric(grid, "PnL", fmtUsd(metrics.pnl_usdc), Number(metrics.pnl_usdc || 0) >= 0 ? "metric-positive" : "metric-negative");
    addDetailMetric(grid, "PF", fmtNumber(metrics.profit_factor));
    addDetailMetric(grid, "Fill rate", fmtPct(policy.fill_rate));
    card.append(grid);
    const reason = domElement("p", "policy-era-reason", policy.rejection_reason || (policy.is_active ? "Collecting guarded paper evidence." : "Historical cohort."));
    card.append(reason);
    container.append(card);
  });
}

function appendJsonBlock(parent, title, value) {
  if (!value || !Object.keys(value).length) return;
  const label = domElement("h4", "milestone-detail-title", title);
  const pre = domElement("pre", "milestone-json", JSON.stringify(value, null, 2));
  parent.append(label, pre);
}

function renderMilestones(data) {
  const list = $("#milestoneList");
  list.replaceChildren();
  const milestones = (data.milestones || []).slice().reverse();
  $("#milestoneCount").textContent = `${milestones.length} events`;
  milestones.forEach((event) => {
    const item = domElement("li", "milestone-item");
    const rail = domElement("div", `milestone-marker milestone-${event.event_type}`);
    const content = domElement("div", "milestone-content");
    const meta = domElement("div", "milestone-meta");
    meta.append(domElement("time", "", fmtDate(event.occurred_at)));
    meta.append(domElement("span", `source-badge source-${event.source}`, event.source));
    if (event.policy_version) meta.append(domElement("span", "milestone-policy", shortVersion(event.policy_version)));
    content.append(meta, domElement("h3", "", event.title), domElement("p", "milestone-summary", event.summary));
    if (event.reason || Object.keys(event.metrics || {}).length || Object.keys(event.config_delta || {}).length || event.evidence_sha256) {
      const details = domElement("details", "milestone-details");
      details.append(domElement("summary", "", "Technical evidence"));
      const body = domElement("div", "milestone-detail-body");
      if (event.reason) body.append(domElement("p", "milestone-reason", event.reason));
      if (event.evidence_sha256) body.append(domElement("p", "milestone-hash", `Evidence SHA-256: ${event.evidence_sha256}`));
      appendJsonBlock(body, "Metrics", event.metrics);
      appendJsonBlock(body, "Configuration delta", event.config_delta);
      details.append(body);
      content.append(details);
    }
    item.append(rail, content);
    list.append(item);
  });
}

function render(data) {
  state.data = data;
  $("#topbarUpdated").textContent = `Ledger updated ${fmtDate(data.generated_at)}`;
  $("#ledgerDot").className = "status-dot dot-ok";
  $("#ledgerStatus").textContent = "Auditable";
  renderCurrent(data);
  if (!state.selectedPositionId && data.series?.length) state.selectedPositionId = data.series.at(-1).position_id;
  if (state.selectedPositionId && !data.series?.some((point) => point.position_id === state.selectedPositionId)) {
    state.selectedPositionId = data.series?.at(-1)?.position_id || null;
  }
  renderWinRateChart(data);
  renderPnlChart(data);
  renderPointDetail(data.series?.find((point) => point.position_id === state.selectedPositionId));
  renderPolicies(data);
  renderMilestones(data);
}

function schedule(delay = REFRESH_MS) {
  clearTimeout(state.timer);
  if (!document.hidden) state.timer = setTimeout(refresh, delay);
}

async function refresh() {
  try {
    const response = await fetch(`/api/evolution?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.failures = 0;
    render(data);
  } catch (error) {
    state.failures += 1;
    $("#ledgerDot").className = "status-dot dot-bad";
    $("#ledgerStatus").textContent = "Retrying";
    $("#topbarUpdated").textContent = `Evolution error: ${error.message}`;
  } finally {
    schedule(Math.min(REFRESH_MS * (2 ** state.failures), 120000));
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(state.timer);
  else refresh();
});

refresh();
