/* i18n: portugues (pt), ingles (en), espanhol (es) e catalao (ca).
   Carrega o dicionario ativo, traduz data-i18n / data-i18n-ph, expoe t() e
   dispara o idioma para o servidor (preferencia salva por usuario). */
(function (global) {
  "use strict";

  const LANGS = [
    { code: "pt", label: "PT", name: "Portugues" },
    { code: "en", label: "EN", name: "English" },
    { code: "es", label: "ES", name: "Espanol" },
    { code: "ca", label: "CA", name: "Catala" }
  ];

  const cache = {};
  let dict = {};
  let current = localStorage.getItem("csi_lang") || "pt";

  function get(obj, path) {
    return path.split(".").reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj);
  }

  async function load(code) {
    if (cache[code]) { dict = cache[code]; return dict; }
    try {
      const r = await fetch(`i18n/${code}.json`, { cache: "no-cache" });
      if (!r.ok) throw new Error("http " + r.status);
      cache[code] = await r.json();
    } catch (e) {
      cache[code] = code === "pt" ? {} : (cache.pt || {});
    }
    dict = cache[code];
    return dict;
  }

  function t(key, fallback) {
    const v = get(dict, key);
    return v === undefined ? (fallback !== undefined ? fallback : key) : v;
  }

  function apply(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      const v = get(dict, el.dataset.i18n);
      if (v !== undefined) el.textContent = v;
    });
    scope.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      const v = get(dict, el.dataset.i18nPh);
      if (v !== undefined) el.placeholder = v;
    });
    document.documentElement.lang = current;
  }

  function renderSwitches() {
    document.querySelectorAll(".lang-switch").forEach((box) => {
      box.innerHTML = LANGS.map((l) =>
        `<button data-lang="${l.code}" class="${l.code === current ? "is-active" : ""}"
                 title="${l.name}">${l.label}</button>`).join("");
      box.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => global.I18N.use(b.dataset.lang)));
    });
  }

  async function use(code, opts) {
    if (!LANGS.some((l) => l.code === code)) return;
    current = code;
    localStorage.setItem("csi_lang", code);
    await load(code);
    renderSwitches();
    apply(document);
    if (!(opts && opts.silent)) document.dispatchEvent(new CustomEvent("langchange", { detail: code }));
  }

  global.I18N = {
    LANGS, t, apply, use,
    get lang() { return current; },
    async init(code) { await use(code || current, { silent: true }); }
  };
})(window);
