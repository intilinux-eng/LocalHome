/* Messages and confirmations, in the page instead of the browser's own
   alert()/confirm() boxes.

   alert() freezes the whole tab (the dashboard's refreshes included) and
   looks like a browser error rather than part of the app; on a phone's
   home screen it's worse, since it names the site in a system dialog.
   These two do the same job without blocking anything:

     notify("Saved", "ok")            - a short message, gone after a while
     if (await confirmAction(...))    - a yes/no the page draws itself

   Loaded by both pages before their own script (see templates/). */

const TOAST_MS = 6000;

function toastHost() {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  return host;
}

function notify(message, kind = "error") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  // An error interrupts a screen reader; a confirmation waits its turn.
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.textContent = message;
  const close = document.createElement("button");
  close.className = "toast-close";
  close.setAttribute("aria-label", "×");
  close.textContent = "×";
  close.onclick = () => toast.remove();
  toast.appendChild(close);
  toastHost().appendChild(toast);
  setTimeout(() => toast.remove(), TOAST_MS);
  return toast;
}

/* Resolves true only if the person picks the confirming button; Esc,
   the backdrop and Cancel all resolve false. */
function confirmAction(message, options = {}) {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay confirm-overlay";
    overlay.innerHTML = `
      <div class="modal-panel confirm-panel" role="dialog" aria-modal="true">
        <p class="confirm-message"></p>
        <div class="confirm-actions">
          <button class="range-btn confirm-no"></button>
          <button class="tbtn ${options.danger ? "close" : "open"} confirm-yes"></button>
        </div>
      </div>`;
    overlay.querySelector(".confirm-message").textContent = message;
    const no = overlay.querySelector(".confirm-no");
    const yes = overlay.querySelector(".confirm-yes");
    no.textContent = options.cancelLabel || (window.I18N && window.I18N.app && window.I18N.app.cancel) || "Cancel";
    yes.textContent = options.confirmLabel || (window.I18N && window.I18N.app && window.I18N.app.confirm) || "OK";

    const done = answer => {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(answer);
    };
    const onKey = e => {
      if (e.key === "Escape") done(false);
    };
    overlay.addEventListener("click", e => { if (e.target === overlay) done(false); });
    no.onclick = () => done(false);
    yes.onclick = () => done(true);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    yes.focus();
  });
}
