/* A part of the page updated in place, as an app does - not rebuilt.

   morph(el, html): the new HTML is laid over what's there and only what
   differs changes - an attribute, a text, a row that came or went. The
   nodes stay: a button keeps its focus and its transition (a switch
   slides, it doesn't blink), a fan keeps turning, a field being typed in
   keeps what's typed, and nothing flashes as if the page had reloaded.
   What's new fades in.

   steady(render, anchor): holds the page still around a render. When
   something above what you're looking at grows or shrinks, the page is
   moved to make up for it: the element touched (or the one in the middle
   of the screen) is put back where it was. Browsers do it themselves
   (scroll anchoring) but Safari doesn't, and the others pick their own
   element to hold - often one above what was tapped. A page that lets
   steady() do it switches theirs off (overflow-anchor: none). */
"use strict";

(function () {
  const LOOKAHEAD = 8;          // how far ahead a node that moved is looked for
  const reducedMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  function key(node) {
    return node.nodeType === 1 ? node.id || node.getAttribute("data-key") || "" : "";
  }

  function firstClass(node) {
    const names = (node.getAttribute("class") || "").trim();
    return names ? names.split(/\s+/)[0] : "";
  }

  // The same thing in the same place: one is updated into the other.
  function same(a, b) {
    if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) return false;
    if (a.nodeType !== 1) return true;
    return key(a) === key(b) && firstClass(a) === firstClass(b);
  }

  // A button waiting for the board (aria-busy) stays waiting through a
  // refresh: it's its action that ends it - a refresh that enabled it
  // again let the same action be sent twice.
  const HELD_WHILE_BUSY = new Set(["aria-busy", "disabled"]);

  function syncAttributes(a, b) {
    const busy = a.getAttribute("aria-busy") === "true";
    for (const attr of [...a.attributes]) {
      if (busy && HELD_WHILE_BUSY.has(attr.name)) continue;
      if (!b.hasAttributeNS(attr.namespaceURI, attr.localName)) a.removeAttributeNS(attr.namespaceURI, attr.localName);
    }
    for (const attr of b.attributes) {
      if (busy && HELD_WHILE_BUSY.has(attr.name)) continue;
      if (a.getAttributeNS(attr.namespaceURI, attr.localName) !== attr.value) {
        a.setAttributeNS(attr.namespaceURI, attr.name, attr.value);
      }
    }
  }

  // A field's value is its own once touched: the attribute no longer says
  // it. Never under someone using it.
  function syncControl(a, b) {
    if (a === document.activeElement) return;
    if (a.tagName === "INPUT") {
      if (a.type === "checkbox" || a.type === "radio") a.checked = b.hasAttribute("checked");
      else if (a.value !== (b.getAttribute("value") || "")) a.value = b.getAttribute("value") || "";
    } else if (a.tagName === "TEXTAREA") {
      if (a.value !== b.textContent) a.value = b.textContent;
    } else if (a.tagName === "SELECT") {
      const picked = [...b.querySelectorAll("option")].findIndex(o => o.hasAttribute("selected"));
      if (picked >= 0 && a.selectedIndex !== picked) a.selectedIndex = picked;
    }
  }

  function morphNode(a, b, inserted) {
    if (a.nodeType !== 1) {
      if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
      return;
    }
    syncAttributes(a, b);
    if (a.tagName === "TEXTAREA") {
      syncControl(a, b);
      return;
    }
    morphChildren(a, b, inserted);
    if (a.tagName === "INPUT" || a.tagName === "SELECT") syncControl(a, b);
  }

  // Walks the new children in order: each one updates the old node in its
  // place when they're the same kind, or one a little further on (what's
  // in between went), or comes in new. Whatever's left over goes. The
  // result is always exactly the new HTML - only the work differs. A text
  // (the spaces between tags, mostly) is never looked for further on:
  // that would drop the elements in between to keep a space.
  function morphChildren(from, to, inserted) {
    let a = from.firstChild;
    let b = to.firstChild;
    while (b) {
      const nextB = b.nextSibling;
      if (a && same(a, b)) {
        morphNode(a, b, inserted);
        a = a.nextSibling;
      } else {
        let match = null;
        const k = key(b);
        for (let probe = a && b.nodeType === 1 ? a.nextSibling : null, i = 0; probe && (k || i < LOOKAHEAD); probe = probe.nextSibling, i++) {
          if (same(probe, b)) { match = probe; break; }
        }
        if (match) {
          while (a !== match) { const gone = a; a = a.nextSibling; from.removeChild(gone); }
          morphNode(match, b, inserted);
          a = match.nextSibling;
        } else {
          from.insertBefore(b, a);          // moved out of the template
          if (b.nodeType === 1) inserted.push(b);
        }
      }
      b = nextB;
    }
    while (a) { const gone = a; a = a.nextSibling; from.removeChild(gone); }
  }

  function appear(nodes) {
    if (reducedMotion && reducedMotion.matches) return;
    nodes.forEach(node => {
      if (typeof node.animate === "function") {
        node.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" });
      }
    });
  }

  function morph(el, html) {
    if (!el) return [];
    const template = document.createElement("template");
    template.innerHTML = html;
    const inserted = [];
    morphChildren(el, template.content, inserted);
    appear(inserted);
    return inserted;
  }

  // A text set only when it changed: re-setting the same one would make a
  // screen reader say it again (aria-live) every refresh.
  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  // ---------------------------------------------------- holding still --

  // The browser holds the page itself when it can and is let to. Read off
  // the page, not guessed from the browser: Safari doesn't know the
  // property at all, and a page (or a test) may switch it off.
  function nativeAnchoring() {
    const root = document.scrollingElement || document.documentElement;
    return getComputedStyle(root).overflowAnchor === "auto" && getComputedStyle(document.body).overflowAnchor === "auto";
  }

  const BLOCKS = "[data-anchor], .hero, .zone, .zone-live > *, .zone-plan, .block, .tile, .season-device, section, main > *";

  // What's being looked at: the smallest block across the line two fifths
  // down the screen - the middle, where the eyes are, a little up.
  function viewAnchor() {
    const line = window.innerHeight * 0.4;
    let best = null;
    let bestHeight = Infinity;
    document.querySelectorAll(BLOCKS).forEach(el => {
      const box = el.getBoundingClientRect();
      if (box.height && box.top <= line && box.bottom >= line && box.height < bestHeight) {
        best = el;
        bestHeight = box.height;
      }
    });
    return best;
  }

  function fixed(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (getComputedStyle(n).position === "fixed") return true;
    }
    return false;
  }

  function steady(render, anchor) {
    if (nativeAnchoring()) return render();
    const touched = anchor && anchor.isConnected && !fixed(anchor) ? anchor : null;
    // At the top, with nothing touched, the top stays the top: holding
    // whatever was mid-screen while the page fills in opened it scrolled
    // half-way down.
    if (!touched && window.scrollY < 1) return render();
    const start = touched || viewAnchor();
    // The element and the ones around it: if it's the one that goes, the
    // nearest that stays is held instead.
    const chain = [];
    for (let n = start; n && n !== document.body; n = n.parentElement) chain.push([n, n.getBoundingClientRect().top]);
    const result = render();
    const held = chain.find(([node]) => node.isConnected);
    if (held) {
      const moved = held[0].getBoundingClientRect().top - held[1];
      if (Math.abs(moved) >= 1) window.scrollBy(0, moved);
    }
    return result;
  }

  window.morph = morph;
  window.setText = setText;
  window.steady = steady;
})();
