/* Shared runtime truth: every page must expose whether the loop may trade. */
(function () {
  const main = document.querySelector(".app-main");
  if (!main) return;
  const strip = document.createElement("section");
  strip.id = "runtimeStatusStrip";
  strip.className = "runtime-status-strip runtime-status-loading";
  strip.setAttribute("role", "status");
  strip.innerHTML = '<span class="runtime-status-mark" aria-hidden="true"></span><div><strong id="runtimeStatusTitle">Checking runtime policy…</strong><p id="runtimeStatusDetail">Reading paper loop health and execution gates.</p></div><code id="runtimeStatusMeta">--</code>';
  const content = main.querySelector(".app-content");
  if (content) content.prepend(strip);
  const title = strip.querySelector("#runtimeStatusTitle");
  const detail = strip.querySelector("#runtimeStatusDetail");
  const meta = strip.querySelector("#runtimeStatusMeta");
  const age = (value) => value == null ? "--" : `${Number(value).toFixed(1)}s`;
  async function refresh() {
    try {
      const response = await fetch(`/api/healthz?ts=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const health = await response.json();
      const observing = health.policy_mode === "observe" || health.policy_mode === "unmanaged";
      const feedOk = health.ok && health.feed_task_alive !== false && (health.btc_age_seconds == null || Number(health.btc_age_seconds) < 30);
      strip.className = `runtime-status-strip ${observing ? "runtime-status-observe" : feedOk ? "runtime-status-active" : "runtime-status-warn"}`;
      title.textContent = observing ? "NO TRADE · Observer activo" : feedOk ? "Paper policy activa" : "Paper loop necesita atención";
      detail.textContent = observing ? "Colecta señales y aprende en modo recomendación; no hay entradas ni órdenes habilitadas." : feedOk ? "Ejecución limitada a paper; las métricas no representan dinero real." : (health.last_feed_error || "Feed o loop degradado; ejecución bloqueada.");
      meta.textContent = `feed ${age(health.btc_age_seconds)} · reconnects ${health.feed_reconnects ?? "--"}`;
    } catch (error) {
      strip.className = "runtime-status-strip runtime-status-warn";
      title.textContent = "Estado no verificable";
      detail.textContent = "No se pudo leer el health endpoint; asumir ejecución bloqueada hasta recuperar señal.";
      meta.textContent = error.message;
    }
  }
  refresh();
  setInterval(refresh, 15000);
})();
