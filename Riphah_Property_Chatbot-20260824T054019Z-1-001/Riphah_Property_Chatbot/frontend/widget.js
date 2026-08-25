/* Embeddable widget snippet (scope document, stage 1).
 *
 * One tag on the portal:
 *
 *   <script src="https://assistant.riphahproperties.com/widget.js"
 *           data-portal="riphah-property" defer></script>
 *
 * Two decisions worth knowing about:
 *
 * **Shadow DOM.** The launcher and panel live in a shadow root, so the portal's
 * CSS cannot restyle the widget and the widget's CSS cannot leak into the portal.
 * On a site with a global `* { box-sizing: content-box }` or an aggressive reset,
 * that is the difference between a working widget and a broken one — and it is not
 * a bug you can fix from your own stylesheet.
 *
 * **An iframe for the chat itself.** The conversation runs in an iframe pointing
 * at the assistant's own origin. That keeps the auth and visitor cookies
 * first-party to the assistant, keeps the portal's JavaScript away from the
 * transcript, and means the widget carries no API surface of its own — only a
 * public portal key, which the server validates against the domain whitelist.
 */
(function () {
  "use strict";

  const script = document.currentScript;
  const origin = new URL(script.src).origin;
  const portal = script.dataset.portal || "riphah-property";
  const accent = script.dataset.accent || "#0f5c8c";
  const label = script.dataset.label || "Ask about our projects";

  const host = document.createElement("div");
  host.style.cssText = "position:fixed;z-index:2147483000;right:0;bottom:0;";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont,
           "Segoe UI", Roboto, sans-serif; }
      .launcher {
        position: fixed; right: 20px; bottom: 20px; display: flex; gap: 9px;
        align-items: center; background: ${accent}; color: #fff; border: 0;
        border-radius: 28px; padding: 13px 19px; font-size: 15px; font-weight: 500;
        cursor: pointer; box-shadow: 0 6px 24px rgba(0,0,0,.22);
      }
      .launcher:hover { filter: brightness(1.08); }
      .panel {
        position: fixed; right: 20px; bottom: 20px; width: 400px; height: 620px;
        max-width: calc(100vw - 32px); max-height: calc(100vh - 32px);
        background: #fff; border-radius: 16px; overflow: hidden;
        box-shadow: 0 12px 48px rgba(0,0,0,.28); display: none;
        flex-direction: column;
      }
      .panel.open { display: flex; }
      .bar {
        background: ${accent}; color: #fff; padding: 11px 14px; display: flex;
        align-items: center; gap: 8px; font-size: 14px; font-weight: 500;
        flex: none;
      }
      .bar .spacer { flex: 1; }
      .bar button {
        background: transparent; border: 0; color: #fff; font-size: 19px;
        cursor: pointer; line-height: 1; padding: 2px 6px;
      }
      iframe { border: 0; width: 100%; flex: 1; }
      @media (max-width: 480px) {
        .panel { right: 8px; bottom: 8px; width: calc(100vw - 16px);
                 height: calc(100vh - 16px); border-radius: 12px; }
      }
    </style>
    <button class="launcher" part="launcher">
      <span aria-hidden="true">💬</span><span>${label}</span>
    </button>
    <div class="panel" role="dialog" aria-label="Property assistant">
      <div class="bar">
        <span>Property assistant</span>
        <span class="spacer"></span>
        <button data-act="close" aria-label="Close">×</button>
      </div>
    </div>
  `;

  const launcher = root.querySelector(".launcher");
  const panel = root.querySelector(".panel");
  let frame = null;

  function open() {
    if (!frame) {
      // Created on first open, not on page load: a widget nobody clicks should
      // cost the portal nothing beyond this script.
      frame = document.createElement("iframe");
      const params = new URLSearchParams({ portal, embedded: "1" });
      for (const key of ["utm_source", "utm_medium", "utm_campaign"]) {
        const value = new URLSearchParams(location.search).get(key);
        if (value) params.set(key, value);
      }
      frame.src = `${origin}/?${params}`;
      // "microphone" for voice input; "autoplay" so the spoken reply can play.
      // Autoplay defaults to self-origin only, so a cross-origin widget stays
      // silent without this delegation even after the visitor taps the mic.
      frame.allow = "microphone; autoplay";
      frame.title = "Property assistant";
      panel.appendChild(frame);
    }
    panel.classList.add("open");
    launcher.style.display = "none";
  }

  function close() {
    panel.classList.remove("open");
    launcher.style.display = "flex";
  }

  launcher.addEventListener("click", open);
  root.querySelector('[data-act="close"]').addEventListener("click", close);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panel.classList.contains("open")) close();
  });

  // Minimal public handle, so the portal can open the widget from its own CTA
  // buttons ("Talk to us about this unit") rather than only from the launcher.
  window.RiphahAssistant = { open, close, portal };
})();
