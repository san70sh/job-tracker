// A list of short text entries ("chips") with suggestions: Chips.mount(element, options).
// Suggestions are only shown, never required: any text typed and confirmed (Enter or comma) becomes a chip.
//   options: {value: [], suggestions: {top: [{value, count}], more: [...]}, placeholder, onChange(values)}
//   returns: {get(), set(values), setSuggestions(s)}
window.Chips = {
  mount(host, opts = {}) {
    let values = [...(opts.value || [])], sug = opts.suggestions || {top: [], more: []}, showMore = false;
    host.classList.add("chips-input");
    host.innerHTML = '<div class="chips-box"><span class="chips-list"></span><input type="text" autocomplete="off" aria-label="Add a filter"></div><div class="chips-menu" hidden></div>';
    const box = host.querySelector(".chips-box"), list = host.querySelector(".chips-list"), input = host.querySelector("input"), menu = host.querySelector(".chips-menu");
    const same = (a, b) => a.trim().toLowerCase() === b.trim().toLowerCase();
    const changed = () => { draw(); if (opts.onChange) opts.onChange([...values]); };

    function add(text) {
      const t = text.trim();
      if (t && !values.some(v => same(v, t))) { values.push(t); changed(); }
      input.value = ""; drawMenu();
    }
    function remove(i) { values.splice(i, 1); changed(); }

    function draw() {
      list.innerHTML = values.map((v, i) => `<span class="chip-item">${esc(v)}<button type="button" data-i="${i}" aria-label="Remove ${esc(v)}">×</button></span>`).join("");
      input.placeholder = values.length ? "" : (opts.placeholder || "");  // the hint is only for an empty field
    }
    // The menu floats over the page (fixed), so opening it never resizes the dialog or gets clipped by its scroll area.
    function placeMenu() {
      if (menu.hidden) return;
      const r = box.getBoundingClientRect(), room = window.innerHeight - r.bottom - 12, above = room < 160 && r.top > room;
      menu.style.left = r.left + "px"; menu.style.width = r.width + "px";
      menu.style.maxHeight = Math.max(120, Math.min(220, above ? r.top - 12 : room)) + "px";
      menu.style.top = above ? "auto" : r.bottom + 4 + "px";
      menu.style.bottom = above ? window.innerHeight - r.top + 4 + "px" : "auto";
    }
    function drawMenu() {
      const typed = input.value.trim().toLowerCase();
      const pool = typed ? [...sug.top, ...sug.more] : (showMore ? [...sug.top, ...sug.more] : sug.top);  // typing searches every option
      const options = pool.filter(o => !values.some(v => same(v, o.value)) && (!typed || o.value.toLowerCase().includes(typed)));
      const hiddenCount = typed || showMore ? 0 : sug.more.filter(o => !values.some(v => same(v, o.value))).length;
      menu.innerHTML = options.map(o => `<button type="button" data-v="${esc(o.value)}"><span>${esc(o.value)}</span>${o.count != null ? `<span class="n">${o.count}</span>` : ""}</button>`).join("")
        + (hiddenCount ? `<button type="button" class="more" data-more>${hiddenCount} more…</button>` : "")
        + (typed && !options.some(o => same(o.value, typed)) ? `<button type="button" data-v="${esc(input.value.trim())}" class="free">Add “${esc(input.value.trim())}”</button>` : "");
      menu.hidden = !menu.innerHTML;
      placeMenu();
    }

    box.addEventListener("click", () => input.focus());
    list.addEventListener("click", e => { const b = e.target.closest("button[data-i]"); if (b) remove(+b.dataset.i); });
    menu.addEventListener("mousedown", e => e.preventDefault());  // keep the input focused while choosing
    menu.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.hasAttribute("data-more")) { showMore = true; drawMenu(); } else add(b.dataset.v);
    });
    window.addEventListener("resize", placeMenu);
    window.addEventListener("scroll", placeMenu, true);  // any scroll area, including the dialog's
    input.addEventListener("input", drawMenu);
    input.addEventListener("focus", drawMenu);
    input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); menu.hidden = true; showMore = false; });  // typed text is kept, not lost
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); if (input.value.trim()) add(input.value); }
      else if (e.key === "Backspace" && !input.value && values.length) remove(values.length - 1);
      else if (e.key === "Escape" && !menu.hidden) { e.stopPropagation(); menu.hidden = true; }  // closes the menu, not the dialog behind it
    });

    draw();
    return {
      get: () => [...values, ...(input.value.trim() && !values.some(v => same(v, input.value)) ? [input.value.trim()] : [])],
      set: v => { values = [...v]; draw(); },
      setSuggestions: s => { sug = s || {top: [], more: []}; showMore = false; },
    };
  },
};
