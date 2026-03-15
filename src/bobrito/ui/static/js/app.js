/* Bobrito Web UI — minimal client-side helpers */

// Notify HTMX about Alpine.js-driven DOM changes
document.addEventListener("alpine:init", () => {
  window.Alpine.store("ui", { ready: true });
});

// Log HTMX errors to console for debugging
document.body.addEventListener("htmx:responseError", (evt) => {
  console.warn("[Bobrito UI] HTMX response error:", evt.detail.xhr.status, evt.detail.pathInfo.requestPath);
});

// Fade-in animation on HTMX swap
document.body.addEventListener("htmx:afterSwap", (evt) => {
  evt.detail.target.style.opacity = "0";
  requestAnimationFrame(() => {
    evt.detail.target.style.transition = "opacity 200ms ease-in";
    evt.detail.target.style.opacity = "1";
  });
});
