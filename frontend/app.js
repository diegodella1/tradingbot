const $ = (selector) => document.querySelector(selector);

const state = {
  latest: null,
  refreshing: false,
};

const fmtUsd = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
};

const fmtNum = (value, digits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
};

const fmtPct = (value, digits = 1) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
};

const fmtCents = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(2)}c`;
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const secondsToEnd = (endTime) => {
  if (!endTime) return null;
  return Math.max(0, Math.floor((new Date(endTime).getTime() - Date.now()) / 1000));
};

const fmtSeconds = (seconds) => {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "--";
  const mins = String(Math.floor(seconds / 60)).padStart(2, "0");
  const secs = String(Math.floor(seconds % 60)).padStart(2, "0");
  return `${mins}:${secs}`;
};

function statusText(ok) {
  const cls = ok ? "text-profit-emerald" : "text-warning-amber";
  return `<span class="font-data text-sm ${cls}">${ok ? "OK" : "CHECK"}</span>`;
}

function latestRejection(rejections, type) {
  return (rejections || []).find((item) => item.market_type === type) || null;
}

function drawBtcCandles(candles) {
  const canvas = $("#btcSparkline");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, width, height);

  const visible = (candles || []).slice(-20);
  if (!visible.length) {
    ctx.fillStyle = "rgba(218, 226, 253, 0.45)";
    ctx.font = `${12 * dpr}px JetBrains Mono`;
    ctx.fillText("waiting for 1m candles", 8 * dpr, height / 2);
    return;
  }

  const highs = visible.map((item) => Number(item.high));
  const lows = visible.map((item) => Number(item.low));
  let max = Math.max(...highs);
  let min = Math.min(...lows);
  if (max === min) {
    max += 1;
    min -= 1;
  }
  const pad = (max - min) * 0.12;
  max += pad;
  min -= pad;
  const plotTop = 5 * dpr;
  const plotBottom = height - 7 * dpr;
  const plotHeight = Math.max(1, plotBottom - plotTop);
  const y = (price) => plotBottom - ((price - min) / (max - min)) * plotHeight;
  const slot = width / visible.length;
  const bodyWidth = Math.max(2 * dpr, Math.min(9 * dpr, slot * 0.55));

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1 * dpr;
  for (let i = 0; i < 3; i += 1) {
    const gy = plotTop + (plotHeight / 4) * (i + 1);
    ctx.beginPath();
    ctx.moveTo(0, gy);
    ctx.lineTo(width, gy);
    ctx.stroke();
  }

  visible.forEach((candle, index) => {
    const open = Number(candle.open);
    const high = Number(candle.high);
    const low = Number(candle.low);
    const close = Number(candle.close);
    const x = slot * index + slot / 2;
    const up = close >= open;
    const color = up ? "#34D399" : "#FB7185";
    const wickTop = y(high);
    const wickBottom = y(low);
    const bodyTop = y(Math.max(open, close));
    const bodyBottom = y(Math.min(open, close));
    const bodyHeight = Math.max(1.5 * dpr, bodyBottom - bodyTop);

    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.2 * dpr;
    ctx.beginPath();
    ctx.moveTo(x, wickTop);
    ctx.lineTo(x, wickBottom);
    ctx.stroke();
    ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
  });
}

function updateCharts(data) {
  drawBtcCandles(data.btc_candles_1m || []);
}

function progressForMarket(market, intervalSeconds) {
  const remaining = market?.end_time ? secondsToEnd(market.end_time) : market?.seconds_to_close;
  if (remaining === null || remaining === undefined) return 100;
  const elapsed = Math.max(0, intervalSeconds - remaining);
  const pct = Math.max(0, Math.min(100, (elapsed / intervalSeconds) * 100));
  return 100 - pct;
}

function renderTimers() {
  const data = state.latest;
  const markets = data?.markets || [];
  const byType = Object.fromEntries(markets.map((market) => [market.type, market]));
  const five = byType["5m"];
  const fifteen = byType["15m"];
  $("#close5m").textContent = fmtSeconds(five?.end_time ? secondsToEnd(five.end_time) : five?.seconds_to_close);
  $("#close15m").textContent = fmtSeconds(fifteen?.end_time ? secondsToEnd(fifteen.end_time) : fifteen?.seconds_to_close);
  if ($("#progress5m")) $("#progress5m").style.strokeDashoffset = String(progressForMarket(five, 300));
  if ($("#progress15m")) $("#progress15m").style.strokeDashoffset = String(progressForMarket(fifteen, 900));
}

function renderMarkets(markets, rejections = []) {
  const byType = Object.fromEntries((markets || []).map((market) => [market.type, market]));
  renderTimers();
  $("#activeMarkets").innerHTML = ["5m", "15m"].map((type) => {
    const market = byType[type];
    if (!market) {
      const rejection = latestRejection(rejections, type);
      return `
        <div class="market-card border-t-warning-amber">
          <span class="font-data text-sm bg-surface-variant px-2 py-1 rounded text-on-surface-variant">${type} Market</span>
          <h3 class="market-title">No verified real market</h3>
          <p class="text-sm text-outline mt-3">${escapeHtml(rejection?.reason || "Discovery pending or rejected by safety filters.")}</p>
          <p class="text-xs text-outline mt-2 truncate-2">${escapeHtml(rejection?.question || "")}</p>
        </div>
      `;
    }
    const up = market.up_bid !== undefined ? { best_bid: market.up_bid, best_ask: market.up_ask } : (market.snapshots || []).find((item) => item.side === "UP") || market.snapshots?.[0] || {};
    const down = market.down_bid !== undefined ? { best_bid: market.down_bid, best_ask: market.down_ask } : (market.snapshots || []).find((item) => item.side === "DOWN") || market.snapshots?.[1] || {};
    const spread = [up, down].map((book) => {
      if (book.best_bid === null || book.best_bid === undefined || book.best_ask === null || book.best_ask === undefined) return null;
      return Math.max(0, Number(book.best_ask) - Number(book.best_bid));
    }).find((value) => value !== null);
    const meta = market.signal?.metadata || {};
    const signal = market.signal?.action || "HOLD";
    const accent = signal === "BUY_UP" ? "border-t-profit-emerald glow-up" : signal === "BUY_DOWN" ? "border-t-loss-rose glow-down" : "border-t-btc-blue";
    const edge = Number(meta.edge ?? market.signal?.edge);
    const netEdge = Number(meta.net_edge ?? meta.edge ?? market.signal?.edge);
    const probability = meta.estimated_probability;
    const price = meta.market_price;
    const kelly = meta.kelly_fraction;
    const size = market.signal?.size_usdc ?? meta.recommended_size_usdc;
    const confidence = market.signal?.confidence;
    const sideClass = signal === "BUY_UP" ? "text-profit-emerald border-profit-emerald/30 bg-profit-emerald/15" : signal === "BUY_DOWN" ? "text-loss-rose border-loss-rose/30 bg-loss-rose/15" : "text-btc-blue border-btc-blue/30 bg-btc-blue/15";
    const edgeClass = Number.isFinite(netEdge) ? (netEdge >= 0 ? "text-profit-emerald" : "text-loss-rose") : "text-on-surface";
    return `
      <div class="market-card ${accent} group">
        <div class="absolute top-0 right-0 p-3 opacity-10 group-hover:opacity-20 transition-opacity">
          <span class="material-symbols-outlined text-[64px] text-btc-blue">candlestick_chart</span>
        </div>
        <div class="relative z-10">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <span class="font-data text-sm bg-surface-variant px-2 py-1 rounded text-on-surface-variant">${type} Market</span>
              <h3 class="market-title truncate-2">${escapeHtml(market.question || "--")}</h3>
            </div>
            <span class="px-3 py-1 rounded-full border text-xs uppercase tracking-wide font-bold ${sideClass}">${escapeHtml(signal)}</span>
          </div>
          <div class="data-grid mt-5">
            <div class="data-cell"><div class="data-cell-label">UP bid / ask</div><div class="data-cell-value text-profit-emerald">${fmtNum(up.best_bid)} / ${fmtNum(up.best_ask)}</div></div>
            <div class="data-cell"><div class="data-cell-label">DOWN bid / ask</div><div class="data-cell-value text-loss-rose">${fmtNum(down.best_bid)} / ${fmtNum(down.best_ask)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Win Probability</div><div class="data-cell-value">${fmtPct(probability)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Market Price</div><div class="data-cell-value">${fmtNum(price, 3)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Net Edge</div><div class="data-cell-value ${edgeClass}">${fmtCents(netEdge)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Confidence</div><div class="data-cell-value">${fmtPct(confidence)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Kelly</div><div class="data-cell-value">${fmtPct(kelly, 2)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Suggested Size</div><div class="data-cell-value">${fmtUsd(size)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Spread</div><div class="data-cell-value">${fmtNum(spread, 3)}</div></div>
            <div class="data-cell"><div class="data-cell-label">Liquidity</div><div class="data-cell-value">${fmtUsd(market.liquidity)}</div></div>
            <div class="col-span-2 data-cell"><div class="data-cell-label">Last Decision</div><div class="data-cell-value text-xs">${escapeHtml(market.signal?.reason || market.risk || "--")}</div></div>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function marketSignalById(markets) {
  return Object.fromEntries((markets || []).filter((market) => market.market_id).map((market) => [market.market_id, market.signal || {}]));
}

function renderPositions(positions, markets = []) {
  const open = (positions || []).filter((pos) => pos.status === "OPEN");
  const pending = (positions || []).filter((pos) => pos.status === "EXPIRED_UNKNOWN");
  const signals = marketSignalById(markets);
  $("#openPositionCount").textContent = `${open.length} open positions`;
  $("#pendingSettlementCount").textContent = `${pending.length} pending settlements`;
  if (!positions?.length) {
    $("#openPositions").innerHTML = `<div class="empty-state">No paper positions yet. Bot will show current side, stake, mark, chance and PnL after first simulated fill.</div>`;
    return;
  }
  $("#openPositions").innerHTML = positions.slice(0, 6).map((pos) => {
    const pnl = Number(pos.unrealized_pnl_usdc || pos.realized_pnl_usdc || 0);
    const pnlClass = pnl >= 0 ? "text-profit-emerald" : "text-loss-rose";
    const signal = signals[pos.market_id] || {};
    const chance = signal.metadata?.estimated_probability;
    const edge = signal.metadata?.edge;
    return `
      <div class="border-b border-white/5 pb-4 last:border-0 last:pb-0">
        <div class="flex items-center justify-between gap-3">
          <span class="font-data text-on-surface">${escapeHtml(pos.side || "TOKEN")}</span>
          <span class="font-data ${pnlClass}">${fmtUsd(pnl)}</span>
        </div>
        <p class="text-xs text-outline mt-1 truncate-2">${escapeHtml(pos.status || "OPEN")} · ${escapeHtml(pos.settlement_status || "")} · ${escapeHtml(pos.market_type || "")} · ${escapeHtml(pos.question || "")}</p>
        <div class="grid grid-cols-2 gap-2 mt-2 text-xs text-outline">
          <span>Cost ${fmtUsd(pos.cost_usdc)}</span>
          <span>Value ${fmtUsd(pos.current_value_usdc)}</span>
          <span>Avg ${fmtNum(pos.avg_price, 3)}</span>
          <span>Mark ${fmtNum(pos.mark_price, 3)}</span>
          <span>Chance ${fmtPct(chance)}</span>
          <span>Edge ${fmtCents(edge)}</span>
        </div>
      </div>
    `;
  }).join("");
}

function renderSafety(safety) {
  if (!$("#safetyGates")) return;
  const ordered = [...(safety || [])].sort((a, b) => Number(a.ok) - Number(b.ok));
  $("#safetyGates").innerHTML = ordered.map((item) => `
    <div class="flex justify-between items-center gap-3">
      <span class="text-sm text-outline">${escapeHtml(item.name)}</span>
      <span class="text-right">${statusText(item.ok)} <span class="block text-[10px] text-outline">${escapeHtml(item.detail || "")}</span></span>
    </div>
  `).join("");
}

function renderOperations(activity) {
  if (!$("#operationsTable")) return;
  if (!activity?.length) {
    $("#operationsTable").innerHTML = `<tr><td class="p-4 text-outline" colspan="3">No operations logged yet.</td></tr>`;
    return;
  }
  $("#operationsTable").innerHTML = activity.slice(0, 5).map((event) => `
    <tr class="hover:bg-surface-container-high/50 transition-colors">
      <td class="text-on-surface">${escapeHtml(event.kind)}</td>
      <td class="text-on-surface-variant truncate-2">${escapeHtml(event.summary)}</td>
      <td class="text-outline text-right whitespace-nowrap">${escapeHtml(event.created_at || "--")}</td>
    </tr>
  `).join("");
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const response = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store", headers: authHeaders() });
    const data = await response.json();
    state.latest = data;
    const btc = data.btc?.price ? data.btc : {
      price: data.paper_state?.btc?.current_price,
      created_at: data.paper_state?.btc?.price_timestamp,
      fresh: Boolean(data.paper_state?.btc?.price_timestamp),
    };
    const btcFresh = Boolean(btc?.fresh);
    const pnl = data.performance?.realized_pnl_usdc || 0;

    $("#serverStatus").textContent = data.paper_state?.status || "pending";
    $("#serverDot").className = `status-dot ${btcFresh ? "dot-ok pulse-live" : "dot-warn"}`;
    $("#navStatus").textContent = data.paper_state?.status || "sync pending";
    $("#modeLabel").textContent = data.live_trading_enabled ? "Live Enabled" : "Paper / Locked";
    const wallet = data.performance?.paper_wallet || {};
    $("#bankrollLabel").textContent = `${fmtUsd(wallet.available_cash_usdc ?? data.config?.paper_bankroll_usdc)} available`;
    $("#btcPrice").textContent = fmtUsd(btc?.price);
    $("#btcFreshDot").className = `status-dot ${btcFresh ? "dot-ok pulse-live" : "dot-warn"}`;
    $("#btcFreshness").innerHTML = btcFresh
      ? `<span class="material-symbols-outlined text-[16px]">check_circle</span> Fresh ${btc.created_at}`
      : `<span class="material-symbols-outlined text-[16px]">sync_problem</span> Pending/stale feed`;
    $("#totalOrders").textContent = `${data.performance?.total_fills ?? 0} fills`;
    $("#paperVolume").textContent = fmtUsd(data.performance?.paper_volume_usdc || 0);
    $("#paperPnl").textContent = fmtUsd(pnl);
    $("#paperPnl").className = `metric-value ${pnl >= 0 ? "text-profit-emerald" : "text-loss-rose"}`;
    const unrealized = data.performance?.unrealized_pnl_usdc || 0;
    $("#unrealizedPnl").textContent = fmtUsd(unrealized);
    $("#unrealizedPnl").className = `metric-value ${unrealized >= 0 ? "text-profit-emerald" : "text-loss-rose"}`;
    $("#settledWinRate").textContent = fmtPct(data.performance?.win_rate);
    $("#settledTrades").textContent = `${data.performance?.settled_trades ?? 0} settled trades`;
    $("#openExposure").textContent = fmtUsd(data.performance?.open_exposure_usdc || 0);
    $("#pendingSettlementValue").textContent = fmtUsd(data.performance?.pending_settlement_usdc || 0);
    $("#updatedAt").textContent = data.generated_at || "--";

    renderMarkets(data.markets || [], data.discovery_rejections || []);
    renderPositions(data.performance?.positions || [], data.markets || []);
    updateCharts(data);
  } catch (error) {
    $("#serverStatus").textContent = `api error: ${error.message}`;
  } finally {
    state.refreshing = false;
  }
}

async function forceSettlement() {
  const button = $("#forceSettlementButton");
  const status = $("#settlementStatus");
  if (!button || button.disabled) return;
  button.disabled = true;
  status.textContent = "Refreshing Gamma results and settling verified pending positions...";
  try {
    const response = await fetch(`/api/settlements/force?ts=${Date.now()}`, {
      method: "POST",
      cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: "{}",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const settledNow = data.settlement?.settled_now ?? 0;
    const pendingAfter = data.settlement?.pending_after ?? 0;
    const refreshed = data.refreshed_markets ?? 0;
    const errors = data.refresh_errors?.length ? ` · ${data.refresh_errors[0]}` : "";
    const pending = data.pending_details?.[0];
    if (settledNow === 0 && pendingAfter > 0 && pending) {
      const prices = (pending.outcome_prices || []).map((value) => fmtNum(value, 3)).join(" / ");
      status.textContent = `Still pending: ${pending.reason}${prices ? ` (${prices})` : ""}. Refreshed ${refreshed} markets${errors}`;
    } else {
      status.textContent = `Settled ${settledNow}. Pending ${pendingAfter}. Refreshed ${refreshed} markets${errors}`;
    }
    await refresh();
  } catch (error) {
    status.textContent = `Settlement force failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

if ($("#refreshButton")) $("#refreshButton").addEventListener("click", refresh);
$("#forceSettlementButton").addEventListener("click", forceSettlement);
refresh();
setInterval(renderTimers, 1000);
setInterval(refresh, 2000);
