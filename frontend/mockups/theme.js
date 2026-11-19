/* Theme toggle: data-theme on <html>, persisted if storage is available. */
(function () {
  var root = document.documentElement, key = "adp-theme";
  try { var s = localStorage.getItem(key); if (s) root.setAttribute("data-theme", s); } catch (e) {}
  function current() {
    return root.getAttribute("data-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-toggle]");
    if (!b) return;
    var next = current() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    b.setAttribute("aria-pressed", next === "dark");
    try { localStorage.setItem(key, next); } catch (e2) {}
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-review]");
    if (!b) return;
    var li = b.closest("li"), z = document.getElementById("toasts");
    li.querySelector(".status").outerHTML = b.dataset.review === "ok"
      ? '<span class="badge ok status">Approuvé</span>' : '<span class="badge error status">Rejeté</span>';
    li.querySelector(".actions").hidden = true;
    if (z) { z.hidden = false; z.querySelector(".txt").textContent =
      "Décision enregistrée (maquette) : " + li.dataset.col; }
  });
})();
