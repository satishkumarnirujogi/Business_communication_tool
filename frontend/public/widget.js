/*
 * Clinic Assistant chat widget. Add one line to any website:
 *
 *   <script src="https://YOUR-ASSISTANT-HOST/widget.js" async
 *           data-label="Chat with us" data-color="#0f9d94" data-lang="de"></script>
 *
 * It adds a floating chat button; the chat itself runs in an iframe from the assistant's own server,
 * so the website's styles and scripts can't interfere with it (and it can't touch the website).
 */
(function () {
  var script = document.currentScript;
  if (!script || window.__clinicChatWidget) return;
  window.__clinicChatWidget = true;

  var origin = new URL(script.src, location.href).origin;
  var color = script.getAttribute("data-color") || "#0f9d94";
  var label = script.getAttribute("data-label") || "Chat";
  var lang = script.getAttribute("data-lang") || "";
  var side = script.getAttribute("data-position") === "left" ? "left" : "right";

  var css =
    ".cc-btn{position:fixed;bottom:20px;" + side + ":20px;z-index:2147483646;height:56px;min-width:56px;padding:0 18px 0 16px;" +
    "border:0;border-radius:28px;background:" + color + ";color:#fff;font:600 15px/1 Inter,system-ui,sans-serif;" +
    "display:flex;align-items:center;gap:8px;box-shadow:0 8px 24px rgba(15,23,42,.18);cursor:pointer}" +
    ".cc-btn:focus-visible{outline:3px solid rgba(15,157,148,.4);outline-offset:2px}" +
    ".cc-frame{position:fixed;bottom:88px;" + side + ":20px;z-index:2147483647;width:380px;height:min(640px,calc(100vh - 110px));" +
    "border:0;border-radius:18px;box-shadow:0 18px 50px rgba(15,23,42,.25);background:#fff;display:none}" +
    ".cc-open .cc-frame{display:block}" +
    "@media (max-width:480px){.cc-frame{inset:0;width:100%;height:100%;border-radius:0}.cc-open .cc-btn{display:none}}";
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement("div");
  var icon = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  var button = document.createElement("button");
  button.className = "cc-btn";
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = icon + "<span></span>";
  button.querySelector("span").textContent = label;

  var frame = document.createElement("iframe");
  frame.className = "cc-frame";
  frame.title = label;
  frame.setAttribute("allow", "clipboard-write");
  var loaded = false;

  function toggle(open) {
    if (open && !loaded) {
      frame.src = origin + "/?embed=1" + (lang ? "&lang=" + encodeURIComponent(lang) : "");
      loaded = true;
    }
    root.classList.toggle("cc-open", open);
    button.setAttribute("aria-expanded", String(open));
  }

  button.addEventListener("click", function () { toggle(!root.classList.contains("cc-open")); });
  window.addEventListener("message", function (e) {
    if (e.origin === origin && e.data && e.data.type === "clinic-chat:close") toggle(false);
  });

  root.appendChild(frame);
  root.appendChild(button);
  (document.body || document.documentElement).appendChild(root);
})();
