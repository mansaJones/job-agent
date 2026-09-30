// Job Agent overlay — a thin status bar with Rescan / Done / Abandon.
// Injected by apply_client/browser.py on every navigation of the application tab.
// Buttons call window.__jobAgentSignal(kind), exposed by Playwright.
// Lives in a shadow root so page styles can't break it (and vice versa).
(() => {
  if (window.top !== window) return;
  if (document.getElementById("job-agent-overlay")) return;

  const host = document.createElement("div");
  host.id = "job-agent-overlay";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      .bar { position: fixed; left: 50%; transform: translateX(-50%); z-index: 2147483647;
             display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
             max-width: calc(100vw - 24px); padding: 8px 12px; border-radius: 10px;
             background: #111827; color: #f9fafb; font: 13px/1.4 -apple-system, "Segoe UI", sans-serif;
             box-shadow: 0 6px 24px rgba(0,0,0,.35); }
      .title { font-weight: 700; color: #a5b4fc; }
      .count { white-space: nowrap; }
      .count b { color: #86efac; } .count.flag b { color: #fde047; }
      .msg { color: #d1d5db; max-width: 460px; }
      button { border: 0; border-radius: 6px; padding: 5px 10px; font: inherit; cursor: pointer; }
      .done { background: #16a34a; color: white; } .abandon { background: #dc2626; color: white; }
      .rescan { background: #374151; color: #f9fafb; } .min { background: transparent; color: #9ca3af; }
      .collapsed .hide-when-min { display: none; }
    </style>
    <div class="bar">
      <span class="title">Job Agent</span>
      <span class="hide-when-min count">Filled <b id="filled">0</b></span>
      <span class="hide-when-min count flag">Needs you <b id="flagged">0</b></span>
      <span class="hide-when-min msg" id="msg">Starting…</span>
      <button class="hide-when-min rescan" id="rescan" title="Fill the fields on this step">Rescan</button>
      <button class="hide-when-min done" id="done" title="You submitted the application yourself">Done ✓</button>
      <button class="hide-when-min abandon" id="abandon" title="Skip this application">Abandon ✗</button>
      <button class="min" id="min" title="Minimize">–</button>
    </div>`;
  const bar = root.querySelector(".bar");

  // Sit below any fixed/sticky header so we never cover the page's own controls
  const placeBar = () => {
    let offset = 8;
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      if (el === host) continue;
      const s = getComputedStyle(el);
      if (s.position !== "fixed" && s.position !== "sticky") continue;
      const r = el.getBoundingClientRect();
      if (r.top <= 1 && r.height > 0 && r.height < 200 && r.width > window.innerWidth * 0.5)
        offset = Math.max(offset, r.bottom + 8);
    }
    bar.style.top = `${offset}px`;
  };

  const signal = (kind) => {
    if (typeof window.__jobAgentSignal === "function") window.__jobAgentSignal(kind);
  };
  // Two-click confirm inside the bar — native confirm() dialogs are auto-dismissed
  // by Playwright, so they can't be used here.
  const armed = {};
  const confirmClick = (id, idleText, armedText, kind) => {
    const btn = root.getElementById(id);
    btn.addEventListener("click", () => {
      if (armed[id]) {
        clearTimeout(armed[id]);
        armed[id] = null;
        btn.textContent = idleText;
        signal(kind);
        return;
      }
      btn.textContent = armedText;
      armed[id] = setTimeout(() => { armed[id] = null; btn.textContent = idleText; }, 4000);
    });
  };
  confirmClick("done", "Done ✓", "I submitted it — confirm ✓", "done");
  confirmClick("abandon", "Abandon ✗", "Really abandon? ✗", "abandon");
  root.getElementById("rescan").addEventListener("click", () => signal("rescan"));
  root.getElementById("min").addEventListener("click", () => {
    bar.classList.toggle("collapsed");
    root.getElementById("min").textContent = bar.classList.contains("collapsed") ? "+" : "–";
  });

  window.__jobAgentUpdate = (filled, flagged, message) => {
    root.getElementById("filled").textContent = String(filled);
    root.getElementById("flagged").textContent = String(flagged);
    if (message !== undefined && message !== null) root.getElementById("msg").textContent = message;
    placeBar();
  };

  (document.body || document.documentElement).appendChild(host);
  placeBar();
  window.addEventListener("resize", placeBar);
})();
