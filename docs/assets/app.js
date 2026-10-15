/* Cashu Mint Directory — table, filters, stats, and mint submission dialog. */
(function () {
  "use strict";

  const REPO = "ye0man/cashu-mint-directory";
  const ISSUE_TEMPLATE = "suggest-mint.yml";

  const NUT_DESCRIPTIONS = {
    0: "Cryptography and models",
    1: "Mint public keys",
    2: "Keysets and fees",
    3: "Swapping tokens",
    4: "Minting tokens",
    5: "Melting tokens",
    6: "Mint info",
    7: "Token state check",
    8: "Overpaid Lightning fees",
    9: "Signature restore",
    10: "Spending conditions",
    11: "Pay-to-Pubkey (P2PK)",
    12: "DLEQ proofs",
    13: "Deterministic secrets",
    14: "Hashed Timelock Contracts (HTLCs)",
    15: "Partial multi-path payments (MPP)",
    16: "Animated QR codes",
    17: "WebSocket subscriptions",
    18: "Payment requests",
    19: "Cached responses",
    20: "Signature on mint quote",
    21: "Clear authentication",
    22: "Blind authentication",
    23: "Payment method: BOLT11",
    24: "HTTP 402 Payment Required",
    25: "Payment method: BOLT12",
    26: "Payment request Bech32m encoding",
    27: "Nostr mint backup",
    28: "Pay-to-Blinded-Key (P2BK)",
    29: "Batched mint",
    30: "Payment method: Onchain"
  };

  const KNOWN_METHODS = { bolt11: "BOLT11", bolt12: "BOLT12", onchain: "Onchain" };

  // ------------------------------------------------------------------ helpers

  const $ = (sel, root = document) => root.querySelector(sel);

  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
  }

  function safeHttpUrl(url) {
    try {
      const u = new URL(String(url));
      return u.protocol === "https:" || u.protocol === "http:" ? u.href : "";
    } catch (e) {
      return "";
    }
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (e) { return String(url || ""); }
  }

  function capitalizeFirst(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
  }

  function semverParts(v) {
    return String(v || "").replace(/^[^0-9]*/, "").replace(/-.*/, "").split(".").map(n => parseInt(n, 10) || 0);
  }

  function semverCompare(a, b) {
    const va = semverParts(a), vb = semverParts(b);
    for (let i = 0; i < Math.max(va.length, vb.length); i++) {
      const d = (va[i] || 0) - (vb[i] || 0);
      if (d !== 0) return d;
    }
    return String(a || "").localeCompare(String(b || ""));
  }

  function relativeTime(date) {
    const secs = Math.round((Date.now() - date.getTime()) / 1000);
    if (secs < 90) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 90) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  function median(nums) {
    if (!nums.length) return 0;
    const s = [...nums].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : Math.round((s[mid - 1] + s[mid]) / 2);
  }

  function methodNames(list) {
    return (list || []).map(m => String(m.method || "").toLowerCase()).filter(Boolean);
  }

  function methodLabel(name) {
    return KNOWN_METHODS[name] || capitalizeFirst(name);
  }

  /** Normalize a user-entered mint URL the same way scripts/mint_submission.py does. */
  function normalizeMintUrl(raw) {
    let s = String(raw || "").trim();
    if (!s) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = "https://" + s;
    let u;
    try { u = new URL(s); } catch (e) { return null; }
    if (u.protocol !== "https:") return null;
    if (u.username || u.password || u.search || u.hash) return null;
    let path = u.pathname.replace(/\/+$/, "").replace(/\/v1\/info$/i, "").replace(/\/+$/, "");
    if (!/^[A-Za-z0-9._~/-]*$/.test(path)) return null;
    const host = u.host.toLowerCase();
    if (!/^[a-z0-9.-]+\.[a-z]{2,}(:\d{1,5})?$/.test(host) || host.endsWith(".onion")) return null;
    return `https://${host}${path}`;
  }

  // ------------------------------------------------------------------ formatters

  function logoFormatter(cell) {
    const row = cell.getRow().getData();
    const url = safeHttpUrl(cell.getValue()) || "cashu_liquid.jpg";
    return `<img class="mint-logo" src="${escapeHtml(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" title="${escapeHtml(row.description || "")}" onerror="this.onerror=null;this.src='cashu_liquid.jpg';">`;
  }

  function mintFormatter(cell) {
    const row = cell.getRow().getData();
    const url = safeHttpUrl(row.url);
    const tooltip = escapeHtml(row.description_long || row.description || "");
    const name = escapeHtml(cell.getValue() || hostOf(row.url));
    const host = escapeHtml(hostOf(row.url));
    const link = url ? `<a class="mint-name" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${tooltip}">${name}</a>` : `<span class="mint-name">${name}</span>`;
    return `<div class="mint-cell">${link}<span class="mint-host">${host}</span></div>`;
  }

  function statusFormatter(cell) {
    const row = cell.getRow().getData();
    const online = cell.getValue() === "online";
    const seen = row.last_seen ? `Last probed ${new Date(row.last_seen).toUTCString()}` : "";
    return `<span class="status ${online ? "status-online" : "status-offline"}" title="${escapeHtml(seen)}">${online ? "Online" : "Offline"}</span>`;
  }

  function scoreFormatter(cell) {
    const value = cell.getValue();
    if (value === "" || value == null) return "";
    const cls = value >= 80 ? "score-high" : value >= 50 ? "score-mid" : "score-low";
    const reasons = (cell.getRow().getData().stale_reasons || []).map(r => r.replace(/_/g, " ")).join(", ");
    return `<span class="score ${cls}" title="${escapeHtml(reasons ? "Lowered by: " + reasons : "No stale signals")}">${value}<span class="score-bar"><span style="width:${Math.max(0, Math.min(100, value))}%"></span></span></span>`;
  }

  function unitsFormatter(cell) {
    const units = cell.getRow().getData().units || [];
    return units.map(u => `<span class="chip unit-${escapeHtml(String(u).toLowerCase().replace(/[^a-z0-9]/g, ""))}">${escapeHtml(u)}</span>`).join("");
  }

  function makeMethodsFormatter(disabledField) {
    return function (cell) {
      const row = cell.getRow().getData();
      const methods = cell.getValue() || [];
      const disabled = Boolean(row[disabledField]);
      const chips = methods.map(m => {
        const name = String(m.method || "").toLowerCase();
        const kind = KNOWN_METHODS[name] ? `method-${name}` : "method-fiat";
        const units = (m.units || []).join(", ");
        const title = `${methodLabel(name)}${units ? " — units: " + units : ""}${disabled ? " (disabled)" : ""}`;
        return `<span class="chip method ${kind}${disabled ? " method-disabled" : ""}" title="${escapeHtml(title)}">${escapeHtml(methodLabel(name))}</span>`;
      });
      if (disabled) chips.push('<span class="chip chip-disabled">disabled</span>');
      return chips.join("");
    };
  }

  function nutsFormatter(cell) {
    const nuts = cell.getValue() || [];
    if (!nuts.length) return '<span class="muted">&mdash;</span>';
    const title = nuts.map(n => `NUT-${String(n).padStart(2, "0")}${NUT_DESCRIPTIONS[n] ? " " + NUT_DESCRIPTIONS[n] : ""}`).join("\n");
    return `<span class="nut-count" title="${escapeHtml(title)}">${nuts.length}</span>`;
  }

  function makeVersionFormatter(latestByImpl) {
    return function (cell) {
      const row = cell.getRow().getData();
      const v = cell.getValue();
      if (!v) return '<span class="muted">&mdash;</span>';
      const latest = latestByImpl[row.implementation] && latestByImpl[row.implementation] === v;
      return `<span class="version">${escapeHtml(v)}</span>${latest ? '<span class="version-latest" title="Newest version seen for this implementation">latest</span>' : ""}`;
    };
  }

  function isValidEmail(email) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(email)); }
  function isValidNostr(key) { return /^npub1[ac-hj-np-z02-9]{58}$/.test(String(key)); }

  function otherContactUrl(method, info) {
    const m = String(method).toLowerCase();
    const i = String(info).trim();
    if (m === "telegram") return `https://t.me/${encodeURIComponent(i.replace(/^@/, "").replace(/^https?:\/\/t\.me\//i, ""))}`;
    if (m === "github") return `https://github.com/${encodeURIComponent(i.replace(/^@/, "").replace(/^https?:\/\/github\.com\//i, ""))}`;
    if (m === "matrix") return `https://matrix.to/#/${encodeURIComponent(i)}`;
    if (m === "website" || m === "url" || m === "web") return safeHttpUrl(i.startsWith("http") ? i : `https://${i}`) || null;
    return null;
  }

  function contactLinks(row) {
    const links = [];
    if (row.email && isValidEmail(row.email)) links.push({ label: "Email", url: `mailto:${row.email}` });
    if (row.x) {
      const handle = String(row.x).trim();
      const url = /^https?:\/\/(twitter|x)\.com\//i.test(handle) ? handle : `https://x.com/${encodeURIComponent(handle.replace(/^@/, ""))}`;
      links.push({ label: "X", url });
    }
    if (row.nostr && isValidNostr(row.nostr)) links.push({ label: "Nostr", url: `https://njump.me/${row.nostr}` });
    if (row.other_contact) {
      let misc = 1;
      String(row.other_contact).split(";").map(s => s.trim()).filter(Boolean).forEach(segment => {
        const idx = segment.indexOf(":");
        const method = idx > 0 ? segment.slice(0, idx).trim() : "";
        const info = idx > 0 ? segment.slice(idx + 1).trim() : segment;
        const url = method ? otherContactUrl(method, info) : null;
        if (url) links.push({ label: capitalizeFirst(method), url });
        else links.push({ label: `Misc ${misc++}`, title: segment });
      });
    }
    return links;
  }

  function contactFormatter(cell) {
    const links = contactLinks(cell.getRow().getData());
    if (!links.length) return '<span class="muted">&mdash;</span>';
    return `<span class="contact-cell">${links.map(l => l.url
      ? `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener">${escapeHtml(l.label)}</a>`
      : `<span title="${escapeHtml(l.title)}">${escapeHtml(l.label)}</span>`).join("")}</span>`;
  }

  // ------------------------------------------------------------------ multi-select

  /**
   * Build an accessible checkbox dropdown inside `container`.
   * options: [{ value, label, key?, count? }]
   */
  function createMultiSelect(container, options, onChange) {
    const placeholder = container.dataset.placeholder || "Any";
    const labelledBy = container.parentElement.querySelector(".small-caps")?.id;
    const selected = new Set();
    const labelByValue = new Map(options.map(o => [String(o.value), o.label]));

    container.innerHTML = `
      <button class="multi-select-trigger" type="button" aria-haspopup="true" aria-expanded="false"${labelledBy ? ` aria-labelledby="${labelledBy} ${container.id}-value"` : ""}>
        <span class="label placeholder" id="${container.id}-value">${escapeHtml(placeholder)}</span>
      </button>
      <div class="multi-select-dropdown" role="group"></div>
      <div class="selected-tags"></div>`;

    const trigger = $(".multi-select-trigger", container);
    const triggerLabel = $(".label", container);
    const dropdown = $(".multi-select-dropdown", container);
    const tags = $(".selected-tags", container);

    options.forEach(opt => {
      const row = document.createElement("label");
      row.className = "multi-select-option";
      row.innerHTML = `<input type="checkbox" value="${escapeHtml(opt.value)}">`
        + (opt.key ? `<span class="opt-key">${escapeHtml(opt.key)}</span>` : "")
        + `<span>${escapeHtml(opt.label)}</span>`
        + (opt.count != null ? `<span class="opt-count">${opt.count}</span>` : "");
      const cb = row.querySelector("input");
      cb.addEventListener("change", () => {
        cb.checked ? selected.add(String(opt.value)) : selected.delete(String(opt.value));
        render();
        onChange();
      });
      dropdown.appendChild(row);
    });

    if (!options.length) dropdown.innerHTML = '<div class="multi-select-option muted">No values</div>';

    function render() {
      tags.innerHTML = "";
      selected.forEach(value => {
        const label = labelByValue.get(value) || value;
        const short = label.length > 22 ? label.slice(0, 21) + "…" : label;
        const tag = document.createElement("span");
        tag.className = "selected-tag";
        tag.innerHTML = `${escapeHtml(short)}<button type="button" aria-label="Remove ${escapeHtml(label)}">&times;</button>`;
        tag.querySelector("button").addEventListener("click", e => {
          e.stopPropagation();
          selected.delete(value);
          sync();
          onChange();
        });
        tags.appendChild(tag);
      });
      if (selected.size === 0) {
        triggerLabel.textContent = placeholder;
        triggerLabel.classList.add("placeholder");
      } else {
        triggerLabel.textContent = selected.size === 1 ? (labelByValue.get([...selected][0]) || "1 selected") : `${selected.size} selected`;
        triggerLabel.classList.remove("placeholder");
      }
    }

    function sync() {
      dropdown.querySelectorAll("input").forEach(cb => { cb.checked = selected.has(cb.value); });
      render();
    }

    function setOpen(open) {
      container.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", String(open));
    }

    trigger.addEventListener("click", e => {
      e.stopPropagation();
      const willOpen = !container.classList.contains("open");
      document.querySelectorAll(".multi-select.open").forEach(el => {
        el.classList.remove("open");
        el.querySelector(".multi-select-trigger").setAttribute("aria-expanded", "false");
      });
      setOpen(willOpen);
      if (willOpen) dropdown.querySelector("input")?.focus();
    });

    container.addEventListener("keydown", e => {
      if (e.key === "Escape" && container.classList.contains("open")) {
        setOpen(false);
        trigger.focus();
      } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && container.classList.contains("open")) {
        const inputs = [...dropdown.querySelectorAll("input")];
        const i = inputs.indexOf(document.activeElement);
        const next = inputs[(i + (e.key === "ArrowDown" ? 1 : -1) + inputs.length) % inputs.length];
        if (next) { e.preventDefault(); next.focus(); }
      }
    });

    document.addEventListener("click", e => { if (!container.contains(e.target)) setOpen(false); });
    container.addEventListener("focusout", e => { if (!container.contains(e.relatedTarget) && e.relatedTarget) setOpen(false); });

    return {
      values: () => [...selected],
      clear: () => { selected.clear(); sync(); }
    };
  }

  // ------------------------------------------------------------------ stats

  function renderStats(data) {
    const online = data.filter(m => m.status === "online");
    const pct = data.length ? Math.round((online.length / data.length) * 100) : 0;

    const implCounts = {};
    online.forEach(m => { const k = m.implementation || "Unknown"; implCounts[k] = (implCounts[k] || 0) + 1; });
    const implSorted = Object.entries(implCounts).sort((a, b) => b[1] - a[1]);

    const units = [...new Set(online.flatMap(m => m.units || []))].sort();

    const hasMethod = (m, name) => methodNames(m.mint_methods).includes(name) || methodNames(m.melt_methods).includes(name);
    const bolt12 = online.filter(m => hasMethod(m, "bolt12")).length;
    const onchain = online.filter(m => hasMethod(m, "onchain")).length;

    const seen = data.map(m => Date.parse(m.last_seen)).filter(Number.isFinite);
    const last = seen.length ? new Date(Math.max(...seen)) : null;

    const cards = [
      { label: "Mints listed", value: data.length, detail: `${data.length - online.length} offline` },
      { label: "Online now", value: `${online.length} <small>${pct}%</small>`, detail: "responding to /v1/info", cls: "good" },
      { label: "Implementations", value: implSorted.length, detail: implSorted.map(([k, v]) => `${k} ${v}`).join(" · ") },
      { label: "Units", value: units.length, detail: units.join(" · ") || "—" },
      { label: "BOLT12 support", value: bolt12, detail: `onchain: ${onchain}` },
      { label: "Median freshness", value: median(online.map(m => m.stale_score || 0)), detail: "online mints, 0–100" },
      { label: "Last probe", value: last ? relativeTime(last) : "—", detail: last ? last.toISOString().replace("T", " ").slice(0, 16) + " UTC" : "" }
    ];

    $("#stats").innerHTML = cards.map(c => `
      <div class="stat">
        <p class="small-caps">${escapeHtml(c.label)}</p>
        <p class="stat-value${c.cls ? " " + c.cls : ""}">${c.value}</p>
        <p class="stat-detail" title="${escapeHtml(c.detail)}">${escapeHtml(c.detail)}</p>
      </div>`).join("");

    if (last) {
      const el = $("#last-probe");
      el.textContent = last.toISOString().replace("T", " ").slice(0, 16) + " UTC";
    }
  }

  // ------------------------------------------------------------------ CSV accessors

  const methodsAccessor = value => (value || []).map(m => `${m.method}${m.units && m.units.length ? " (" + m.units.join("/") + ")" : ""}`).join(", ");
  const listAccessor = value => (value || []).join(", ");
  const contactAccessor = (value, row) => contactLinks(row).map(l => l.url ? l.url.replace(/^mailto:/, "") : l.title).join(", ");

  // ------------------------------------------------------------------ main

  function countBy(data, fn) {
    const counts = new Map();
    data.forEach(m => fn(m).forEach(v => counts.set(v, (counts.get(v) || 0) + 1)));
    return counts;
  }

  function init(data) {
    data.sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base" }));
    renderStats(data);

    // Latest version per implementation, computed from the data itself.
    const latestByImpl = {};
    data.forEach(m => {
      if (!m.implementation || !m.version) return;
      const cur = latestByImpl[m.implementation];
      if (!cur || semverCompare(m.version, cur) > 0) latestByImpl[m.implementation] = m.version;
    });

    // Option lists with counts.
    const nutCounts = countBy(data, m => m.nuts || []);
    const nutOptions = [...nutCounts.keys()].sort((a, b) => a - b).map(n => ({
      value: String(n), key: String(n).padStart(2, "0"), label: NUT_DESCRIPTIONS[n] || `NUT-${n}`, count: nutCounts.get(n)
    }));

    const unitCounts = countBy(data, m => m.units || []);
    const unitOptions = [...unitCounts.keys()].sort().map(u => ({ value: u, label: u, count: unitCounts.get(u) }));

    const implCounts = countBy(data, m => (m.implementation ? [m.implementation] : []));
    const implOptions = [...implCounts.keys()].sort((a, b) => a.localeCompare(b)).map(i => ({ value: i, label: i, count: implCounts.get(i) }));

    const versionKey = m => `${m.implementation}|${m.version}`;
    const versionCounts = countBy(data, m => (m.implementation && m.version ? [versionKey(m)] : []));
    const versionOptions = [...versionCounts.keys()].sort((a, b) => {
      const [ia, va] = a.split("|"), [ib, vb] = b.split("|");
      return ia.localeCompare(ib) || semverCompare(vb, va);
    }).map(k => {
      const [impl, ver] = k.split("|");
      return { value: k, label: `${impl} ${ver}`, count: versionCounts.get(k) };
    });

    const mintCounts = countBy(data, m => methodNames(m.mint_methods));
    const mintOptions = [...mintCounts.keys()].sort().map(k => ({ value: k, label: methodLabel(k), count: mintCounts.get(k) }));
    const meltCounts = countBy(data, m => methodNames(m.melt_methods));
    const meltOptions = [...meltCounts.keys()].sort().map(k => ({ value: k, label: methodLabel(k), count: meltCounts.get(k) }));

    const table = new Tabulator("#mints-table", {
      data,
      layout: "fitColumns",
      responsiveLayout: "collapse",
      responsiveLayoutCollapseStartOpen: false,
      movableColumns: true,
      pagination: true,
      paginationSize: 50,
      paginationSizeSelector: [25, 50, 100, true],
      paginationCounter: "rows",
      placeholder: "No mints match these filters.",
      // Data is pre-sorted by name, so equal freshness scores stay alphabetical (sort is stable).
      initialSort: [{ column: "stale_score", dir: "desc" }],
      rowFormatter: row => {
        row.getElement().classList.toggle("is-offline", row.getData().status !== "online");
      },
      columns: [
        { formatter: "responsiveCollapse", width: 30, minWidth: 30, hozAlign: "center", headerSort: false, resizable: false, download: false, responsive: 0 },
        { title: "", field: "icon_url", formatter: logoFormatter, headerSort: false, hozAlign: "center", width: 52, minWidth: 52, resizable: false, download: false, responsive: 0 },
        { title: "Mint", field: "name", formatter: mintFormatter, sorter: "string", widthGrow: 2, minWidth: 150, responsive: 0 },
        { title: "URL", field: "url", visible: false, download: true },
        { title: "Status", field: "status", formatter: statusFormatter, sorter: "string", width: 104, minWidth: 104, responsive: 0 },
        { title: "Type", field: "implementation", sorter: "string", widthGrow: 0.9, minWidth: 100, responsive: 2 },
        { title: "Version", field: "version", formatter: makeVersionFormatter(latestByImpl), sorter: semverCompare, widthGrow: 0.9, minWidth: 110, responsive: 3 },
        { title: "Units", field: "units", cssClass: "cell-wrap", formatter: unitsFormatter, sorter: (a, b) => (a || []).join().localeCompare((b || []).join()), accessorDownload: listAccessor, widthGrow: 0.8, minWidth: 90, responsive: 2 },
        { title: "Mint via", titleDownload: "Mint methods", cssClass: "cell-wrap", field: "mint_methods", formatter: makeMethodsFormatter("nut4_disabled"), headerTooltip: "NUT-04 mint methods", sorter: (a, b) => (a || []).length - (b || []).length, accessorDownload: methodsAccessor, widthGrow: 1, minWidth: 120, responsive: 1 },
        { title: "Melt via", titleDownload: "Melt methods", cssClass: "cell-wrap", field: "melt_methods", formatter: makeMethodsFormatter("nut5_disabled"), headerTooltip: "NUT-05 melt methods", sorter: (a, b) => (a || []).length - (b || []).length, accessorDownload: methodsAccessor, widthGrow: 1, minWidth: 120, responsive: 1 },
        { title: "NUTs", field: "nuts", formatter: nutsFormatter, headerTooltip: "Optional NUTs supported (hover a count for the list)", sorter: (a, b) => (a || []).length - (b || []).length, accessorDownload: listAccessor, hozAlign: "center", width: 78, minWidth: 78, responsive: 3 },
        { title: "Fresh", titleDownload: "Freshness", field: "stale_score", formatter: scoreFormatter, headerTooltip: "Freshness score, 0–100", sorter: "number", width: 104, minWidth: 104, responsive: 1 },
        { title: "Contact", field: "email", cssClass: "cell-wrap", formatter: contactFormatter, accessorDownload: contactAccessor, headerSort: false, widthGrow: 1.1, minWidth: 120, responsive: 4 }
      ]
    });

    // Filter state ------------------------------------------------------
    const els = {
      search: $("#search"),
      minScore: $("#min-score"),
      minScoreOut: $("#min-score-out"),
      latestOnly: $("#latest-only"),
      includeOffline: $("#include-offline"),
      count: $("#result-count")
    };

    let built = false;
    const refresh = () => { if (built) table.refreshFilter(); };

    const selects = {
      nuts: createMultiSelect($("#ms-nuts"), nutOptions, refresh),
      units: createMultiSelect($("#ms-units"), unitOptions, refresh),
      impl: createMultiSelect($("#ms-impl"), implOptions, refresh),
      version: createMultiSelect($("#ms-version"), versionOptions, refresh),
      mint: createMultiSelect($("#ms-mint"), mintOptions, refresh),
      melt: createMultiSelect($("#ms-melt"), meltOptions, refresh)
    };

    function rowFilter(m) {
      if (!els.includeOffline.checked && m.status !== "online") return false;
      if ((m.stale_score || 0) < Number(els.minScore.value)) return false;
      if (els.latestOnly.checked && !(m.version && latestByImpl[m.implementation] === m.version)) return false;

      const nuts = selects.nuts.values();
      if (nuts.length && !nuts.every(n => (m.nuts || []).includes(Number(n)))) return false;

      const units = selects.units.values();
      if (units.length && !units.some(u => (m.units || []).includes(u))) return false;

      const impl = selects.impl.values();
      if (impl.length && !impl.includes(m.implementation)) return false;

      const ver = selects.version.values();
      if (ver.length && !ver.includes(versionKey(m))) return false;

      const mint = selects.mint.values();
      if (mint.length && !mint.some(x => methodNames(m.mint_methods).includes(x))) return false;

      const melt = selects.melt.values();
      if (melt.length && !melt.some(x => methodNames(m.melt_methods).includes(x))) return false;

      const term = els.search.value.trim().toLowerCase();
      if (term) {
        const hay = [m.name, m.url, m.implementation, m.version, m.description].join(" ").toLowerCase();
        if (!hay.includes(term)) return false;
      }
      return true;
    }

    function updateCount() {
      const shown = table.getDataCount("active");
      els.count.textContent = `${shown} of ${data.length} mints`;
    }

    table.on("tableBuilt", () => {
      built = true;
      table.setFilter(rowFilter);
      updateCount();
    });
    table.on("dataFiltered", () => setTimeout(updateCount, 0));

    els.search.addEventListener("input", refresh);
    els.latestOnly.addEventListener("change", refresh);
    els.includeOffline.addEventListener("change", refresh);
    els.minScore.addEventListener("input", () => {
      els.minScoreOut.textContent = els.minScore.value;
      refresh();
    });

    $("#clear-filters").addEventListener("click", () => {
      Object.values(selects).forEach(s => s.clear());
      els.search.value = "";
      els.minScore.value = 0;
      els.minScoreOut.textContent = "0";
      els.latestOnly.checked = false;
      els.includeOffline.checked = false;
      refresh();
    });

    // Downloads ---------------------------------------------------------
    $("#dl-csv").addEventListener("click", () => table.download("csv", "cashu-mints.csv"));
    $("#dl-json").addEventListener("click", () => {
      const rows = table.getData("active");
      const blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "cashu-mints.json";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    });

    initSuggestDialog(data);
  }

  // ------------------------------------------------------------------ suggest a mint (#1)

  function initSuggestDialog(data) {
    const dialog = $("#suggest-dialog");
    const urlInput = $("#suggest-url");
    const notes = $("#suggest-notes");
    const check = $("#suggest-check");
    const known = new Set(data.map(m => normalizeMintUrl(m.url)).filter(Boolean));

    const setCheck = (cls, msg) => { check.className = `suggest-check ${cls}`; check.textContent = msg; };

    function validateLocal() {
      const url = normalizeMintUrl(urlInput.value);
      if (!url) { setCheck("err", "Enter a valid https:// mint URL (no query string or credentials)."); return null; }
      if (known.has(url)) { setCheck("warn", `${url} is already in the directory.`); return null; }
      return url;
    }

    $("#suggest-open").addEventListener("click", () => {
      const s = $("#search").value.trim();
      if (!urlInput.value && normalizeMintUrl(s) && /\./.test(s)) urlInput.value = s;
      setCheck("", "");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else window.open(`https://github.com/${REPO}/issues/new?template=${ISSUE_TEMPLATE}`, "_blank", "noopener");
      urlInput.focus();
    });

    $("#suggest-cancel").addEventListener("click", () => dialog.close());
    urlInput.addEventListener("input", () => setCheck("", ""));

    $("#suggest-test").addEventListener("click", async () => {
      const url = validateLocal();
      if (!url) return;
      setCheck("", `Probing ${url}/v1/info …`);
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      try {
        const res = await fetch(`${url}/v1/info`, { signal: ctrl.signal, headers: { Accept: "application/json" } });
        const info = await res.json();
        if (!res.ok || !info || typeof info.nuts !== "object") throw new Error(`HTTP ${res.status}`);
        const optional = Object.keys(info.nuts).filter(k => Number(k) > 6).length;
        setCheck("ok", `✓ ${info.name || url} responds${info.version ? " — " + info.version : ""} (${optional} optional NUTs).`);
      } catch (err) {
        setCheck("warn", "Couldn't verify from your browser (offline, or CORS blocked). You can still submit — the bot checks server-side.");
      } finally {
        clearTimeout(timer);
      }
    });

    $("#suggest-form").addEventListener("submit", e => {
      e.preventDefault();
      const url = validateLocal();
      if (!url) return;
      const params = new URLSearchParams({
        template: ISSUE_TEMPLATE,
        title: `Add mint: ${hostOf(url)}`,
        mint_url: url,
        notes: notes.value.trim()
      });
      window.open(`https://github.com/${REPO}/issues/new?${params}`, "_blank", "noopener");
      dialog.close();
      urlInput.value = "";
      notes.value = "";
    });
  }

  // ------------------------------------------------------------------ boot

  fetch("mints.json", { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(init)
    .catch(err => {
      $("#mints-table").innerHTML = `<p class="load-error">Failed to load mint data: ${escapeHtml(err.message)}</p>`;
    });
})();
