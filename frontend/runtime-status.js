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
      let response = await fetch(`/api/healthz?ts=${Date.now()}`, { cache: "no-store" });
      let health;
      if (response.ok) {
        health = await response.json();
      } else {
        // Older proxies may expose status but not the dedicated health route.
        response = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`health HTTP ${response.status}; status HTTP ${response.status}`);
        const status = await response.json();
        health = { ...status, ok: true, feed_task_alive: true, btc_age_seconds: null };
      }
      const observing = health.policy_mode === "observe" || health.policy_mode === "unmanaged";
      const feedOk = health.ok && health.feed_task_alive !== false && (health.btc_age_seconds == null || Number(health.btc_age_seconds) < 30);
      strip.className = `runtime-status-strip ${observing ? "runtime-status-observe" : feedOk ? "runtime-status-active" : "runtime-status-warn"}`;
      title.textContent = observing ? "NO TRADE · Observer activo" : feedOk ? "Paper policy activa" : "Paper loop necesita atención";
      detail.textContent = observing ? "Colecta señales y aprende en modo recomendación; no hay entradas ni órdenes habilitadas." : feedOk ? "Ejecución limitada a paper; las métricas no representan dinero real." : (health.last_feed_error || "Feed o loop degradado; ejecución bloqueada.");
      meta.textContent = `feed ${age(health.btc_age_seconds)} · reconnects ${health.feed_reconnects ?? "--"}`;
    } catch (error) {
      try {
        const fallback = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store" });
        if (fallback.ok) {
          const status = await fallback.json();
          strip.className = "runtime-status-strip runtime-status-observe";
          title.textContent = status.policy_mode === "observe" ? "NO TRADE · Observer activo" : "Paper runtime conectado";
          detail.textContent = "Health detallado no está publicado por el proxy; estado operativo leído desde status.";
          meta.textContent = "fallback /api/status";
          return;
        }
      } catch (_) { /* keep the explicit blocked state below */ }
      strip.className = "runtime-status-strip runtime-status-warn";
      title.textContent = "Estado no verificable";
      detail.textContent = "No se pudo leer health ni status; asumir ejecución bloqueada hasta recuperar señal.";
      meta.textContent = error.message;
    }
  }
  refresh();
  setInterval(refresh, 15000);
})();
