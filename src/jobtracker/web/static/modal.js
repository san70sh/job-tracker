// A modal dialog over a blurred page: the shell shared by the job details and the board editor.
// It knows nothing about either. Behaviour the caller adds goes in through hooks:
//   beforeClose() -> false cancels the close (unsaved edits)    onKey(event) -> true when the caller handled the key
//   onClose()     -> runs after it closed                       label: the dialog's accessible name
window.Modal = (opts = {}) => {
  const el = document.createElement("div");
  el.className = "jm"; el.hidden = true;
  el.innerHTML = `<div class="jm-panel" role="dialog" aria-modal="true" aria-label="${opts.label || "Dialog"}" tabindex="-1"><div class="jm-body"></div></div>`;
  document.body.appendChild(el);
  const panel = el.querySelector(".jm-panel"), body = el.querySelector(".jm-body");
  let isOpen = false, lastFocus = null, downOnBackdrop = false;

  function open() {
    lastFocus = document.activeElement;
    el.hidden = false; isOpen = true; document.body.style.overflow = "hidden";  // the page behind stops scrolling
    panel.focus();
  }
  // force skips beforeClose (the caller already asked). Returns whether it closed.
  function close(force = false) {
    if (!isOpen) return false;
    if (!force && opts.beforeClose && opts.beforeClose() === false) return false;
    el.hidden = true; isOpen = false; document.body.style.overflow = "";
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
    if (opts.onClose) opts.onClose();
    return true;
  }

  // a click on the blurred area closes, but not when a text selection merely ends there
  el.addEventListener("mousedown", e => { downOnBackdrop = e.target === el; });
  el.addEventListener("click", e => { if (e.target === el && downOnBackdrop) close(); });
  document.addEventListener("keydown", e => {
    if (!isOpen) return;
    if (opts.onKey && opts.onKey(e)) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;  // keep focus inside the dialog
    const f = [...el.querySelectorAll("a[href], button:not([disabled]), input, select, textarea")].filter(x => x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  return {el, panel, body, open, close, isOpen: () => isOpen};
};
