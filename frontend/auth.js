// Shared dashboard auth: captures ?token=... into localStorage and exposes
// authHeaders() for API calls. The backend rejects /api/* without the token.
(function captureTokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token");
  if (!fromUrl) return;
  localStorage.setItem("dashboard_token", fromUrl);
  params.delete("token");
  const query = params.toString();
  history.replaceState(null, "", window.location.pathname + (query ? `?${query}` : ""));
})();

function dashboardToken() {
  return localStorage.getItem("dashboard_token") || "";
}

function authHeaders(extra = {}) {
  const token = dashboardToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra };
}
