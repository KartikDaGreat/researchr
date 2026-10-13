// researchr. Drag a card LEFT to keep it (it goes to your brief), RIGHT to set it aside.
(() => {
  const $ = (s) => document.querySelector(s);
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };
  const qs = new URLSearchParams(location.search);
  if (qs.get("token")) { store.set("token", qs.get("token")); history.replaceState(null, "", location.pathname + location.hash); }
  const ownerToken = store.get("token") || "";
  // anonymous per-device session: you only ever see research started from this browser
  let session = store.get("session");
  if (!session) {
    const a = new Uint8Array(24); crypto.getRandomValues(a);
    session = Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
    store.set("session", session);
  }

  const KIND = {
    strategy: "Game plan", insight: "Portrait", theme: "Something to talk about", overlap: "Common ground",
    inference: "A read on them", questions: "Questions to ask", caution: "Handle with care",
  };
  const BRIEF = [["strategy", "Your game plan"], ["insight", "Who they are"], ["theme", "Things to talk about"],
    ["overlap", "Common ground"], ["inference", "Reads on them"], ["questions", "Questions to ask"], ["caution", "Handle with care"]];

  let cfg = { recaptcha_site_key: "", verification: true };
  let state = { runs: [], current: null, cards: [], kept: [], skipped: 0, history: [], tab: "triage",
    seenDone: new Set(JSON.parse(store.get("seenDone") || "[]")), kicked: {} };
  let pollTimer = null;

  // ---------------------------------------------------------------- api
  const authHeaders = () => ({ "X-Session": session, ...(ownerToken ? { "X-Token": ownerToken } : {}) });
  const authQuery = () => `session=${encodeURIComponent(session)}${ownerToken ? `&token=${encodeURIComponent(ownerToken)}` : ""}`;
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...authHeaders(), ...(opts.headers || {}) } });
    if (!res.ok) {
      let msg = `Something went wrong (${res.status}).`;
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  }
  // On Vercel a run advances in chained steps; if a chain dropped (stale lease), restart it.
  function kickIfStale(r) {
    if (!r || r.status !== "running" || !r.stale) return;
    if (state.kicked[r.id] && Date.now() - state.kicked[r.id] < 30000) return;
    state.kicked[r.id] = Date.now();
    fetch(`/api/runs/${r.id}/tick`, { method: "POST", headers: authHeaders(), keepalive: true }).catch(() => {});
  }
  let swipeChain = Promise.resolve();  // decisions are read-modify-write on the server: send one at a time
  const sendDecision = (rid, cid, decision) => (swipeChain = swipeChain.then(() =>
    api(`/api/runs/${rid}/cards/${cid}`, { method: "POST", body: JSON.stringify({ decision }) }).catch(() => {})));

  // ---------------------------------------------------------------- utils
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (p) => `${Math.round(p * 100)}%`;
  const dotColor = (p) => (p == null ? "var(--aside)" : p >= 0.75 ? "var(--pine)" : p >= 0.5 ? "#C99A2E" : "var(--red)");
  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`; if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function toast(title, line, onClick) {
    const t = $("#toast"); t.innerHTML = `<b>${esc(title)}</b><p>${esc(line)}</p>`; t.classList.remove("hidden");
    t.onclick = () => { t.classList.add("hidden"); onClick && onClick(); };
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 6000);
    if (navigator.vibrate) navigator.vibrate([25, 40, 25]);
  }

  // ---------------------------------------------------------------- config + captcha
  async function loadConfig() {
    try { cfg = await (await fetch("/api/config")).json(); } catch {}
    if (cfg.recaptcha_site_key && !ownerToken) {
      const s = document.createElement("script");
      s.src = `https://www.google.com/recaptcha/api.js?render=${encodeURIComponent(cfg.recaptcha_site_key)}`;
      s.async = true; document.head.appendChild(s);
    } else { $("#legal").textContent = "Takes about five minutes."; }
    if (!cfg.verification && !ownerToken) {
      $("#form-note").textContent = "New research is paused while human verification is being set up.";
    }
  }
  function captchaToken() {
    if (!cfg.recaptcha_site_key || ownerToken) return Promise.resolve("");
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      (function wait() {
        if (window.grecaptcha?.execute) {
          grecaptcha.ready(() => grecaptcha.execute(cfg.recaptcha_site_key, { action: "research" }).then(resolve, reject));
        } else if (Date.now() - t0 > 8000) reject(new Error("Couldn't load the human check. Check your connection and try again."));
        else setTimeout(wait, 150);
      })();
    });
  }

  // ---------------------------------------------------------------- routing
  window.addEventListener("hashchange", route);
  function route() {
    const m = location.hash.match(/^#\/p\/(\w+)(?:\/(\w+))?/);
    if (m) openRun(m[1], m[2] || "triage"); else openHome();
  }

  // ---------------------------------------------------------------- home
  async function openHome() {
    state.current = null;
    $("#view-run").classList.add("hidden"); $("#view-home").classList.remove("hidden");
    document.title = "researchr: know who you're meeting";
    try { await refreshRuns(); } catch (e) { /* offline: keep the form usable */ }
    renderPeople();
  }
  async function refreshRuns() {
    const prev = new Map(state.runs.map((r) => [r.id, r.status]));
    state.runs = await api("/api/runs");
    for (const r of state.runs) {
      kickIfStale(r);
      if (r.status === "done" && prev.get(r.id) === "running") notifyDone(r);
    }
    schedulePoll();
  }
  function notifyDone(r) {
    if (state.seenDone.has(r.id)) return;
    state.seenDone.add(r.id); store.set("seenDone", JSON.stringify([...state.seenDone]));
    toast(`${r.title} is ready`, `${r.pending} cards to sort. Tap to open.`, () => (location.hash = `#/p/${r.id}`));
    try { if (window.Notification?.permission === "granted") new Notification(`${r.title} is ready`, { body: `${r.pending} cards to sort` }); } catch {}
  }
  function renderPeople() {
    const ul = $("#run-list");
    $("#empty-people").classList.toggle("hidden", state.runs.length > 0);
    ul.innerHTML = state.runs.map((r) => {
      const name = r.title || r.name;
      let msg = r.subtitle || r.goal || "", side = `<span>${ago(r.created)}</span>`, ink = "";
      if (r.status === "running") { msg = r.message || "Researching"; ink = `<div class="run-ink"><span style="width:${Math.max(3, r.pct || 0)}%"></span></div>`; side = ""; }
      else if (r.status === "error") msg = "Stopped before finishing. Open for details.";
      else if (r.pending) side = `<b>${r.pending}</b>to sort`;
      else side = `<b>${r.kept || 0}</b>kept`;
      return `<li class="run-item" data-id="${r.id}" tabindex="0" role="link">
        <span class="run-name">${esc(name)}</span><span class="run-side">${side}</span>
        <span class="run-msg">${esc(msg)}</span>${ink}</li>`;
    }).join("");
    ul.querySelectorAll(".run-item").forEach((li) => {
      const go = () => (location.hash = `#/p/${li.dataset.id}`);
      li.addEventListener("click", go);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    const running = state.runs.some((r) => r.status === "running") || state.current?.status === "running";
    if (running) pollTimer = setTimeout(async () => {
      try { await refreshRuns(); if (state.current) await refreshCurrent(); else renderPeople(); } catch {}
    }, state.current ? 2500 : 4000);
  }

  // ---------------------------------------------------------------- new research
  const form = $("#new-form");
  form.me.value = store.get("about") || "";
  if (form.me.value) form.querySelector(".about").open = true;
  form.me.addEventListener("input", () => store.set("about", form.me.value));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#form-error"), btn = $("#btn-start");
    err.classList.add("hidden");
    if (!form.name.value.trim()) { form.name.focus(); return; }
    btn.disabled = true; btn.textContent = "Checking you're human";
    try {
      const captcha = await captchaToken();
      btn.textContent = "Starting";
      const body = { name: form.name.value, hints: form.hints.value, goal: form.goal.value, me: form.me.value, captcha };
      const { id } = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
      form.name.value = form.hints.value = form.goal.value = "";
      try { if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } catch {}
      await refreshRuns(); location.hash = `#/p/${id}`;
    } catch (ex) {
      err.textContent = ex.message; err.classList.remove("hidden");
    } finally { btn.disabled = false; btn.textContent = "Start research"; }
  });

  // ---------------------------------------------------------------- one person
  async function openRun(id, tab) {
    $("#view-home").classList.add("hidden"); $("#view-run").classList.remove("hidden");
    window.scrollTo(0, 0);
    if (state.current?.id !== id) { state.current = { id }; state.history = []; state.cards = []; state.kept = []; state.skipped = 0; }
    try { await refreshCurrent(true); } catch (e) { toast("Couldn't open this research", e.message); location.hash = ""; return; }
    setTab(tab);
  }
  async function refreshCurrent(force = false) {
    const wasRunning = state.current.status === "running";
    const r = await api(`/api/runs/${state.current.id}`);
    state.current = r;
    kickIfStale(r);
    document.title = `${r.title || r.name} · researchr`;
    $("#run-name").textContent = r.title || r.name;
    $("#run-sub").textContent = r.subtitle || r.goal || "";
    const running = r.status === "running";
    $("#progress").classList.toggle("hidden", !running);
    $("#error").classList.toggle("hidden", r.status !== "error");
    $(".seg").classList.toggle("hidden", r.status !== "done");
    if (r.status === "error") $("#error-text").textContent = r.error || "Unknown error";
    if (running) renderLive(r);
    if (r.status === "done" && (force || wasRunning)) {
      if (wasRunning) notifyDone(r);
      await loadCards();
    }
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", r.status !== "done" || t.id !== `tab-${state.tab}`));
    schedulePoll();
  }
  async function loadCards() {
    const rid = state.current.id;
    const [pending, kept, skipped] = await Promise.all([
      api(`/api/runs/${rid}/cards?which=pending`), api(`/api/runs/${rid}/cards?which=kept`), api(`/api/runs/${rid}/cards?which=skipped`)]);
    state.cards = pending; state.kept = kept; state.skipped = skipped.length;
    renderStack(); updateCounts();
  }
  function updateCounts() {
    $("#cnt-pending").textContent = state.cards.length || "";
    $("#cnt-kept").textContent = state.kept.length || "";
    $("#n-keep").textContent = state.kept.length;
    $("#n-skip").textContent = state.skipped;
    $("#a-undo").disabled = state.history.length === 0;
    if (state.tab === "brief") renderBrief();
  }
  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".seg button").forEach((b) => { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", b.dataset.tab === tab); });
    const busy = state.current?.status !== "done";
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", busy || t.id !== `tab-${tab}`));
    if (busy) return;
    if (tab === "brief") renderBrief();
    if (tab === "report") {
      const base = `/api/runs/${state.current.id}/report`, q = `?${authQuery()}`;
      $("#report-frame").src = `${base}.html${q}`; $("#open-html").href = `${base}.html${q}`; $("#dl-md").href = `${base}.md${q}`;
    }
  }
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => (location.hash = `#/p/${state.current.id}/${b.dataset.tab}`)));
  document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => (location.hash = `#/p/${state.current.id}/${b.dataset.goto}`)));
  $("#btn-back").onclick = () => (location.hash = "");
  $("#btn-del").onclick = async () => {
    if (!state.current) return;
    if (!window.confirm(`Delete your research on ${state.current.title || state.current.name}? This can't be undone.`)) return;
    await api(`/api/runs/${state.current.id}`, { method: "DELETE" }); location.hash = "";
  };

  // ---------------------------------------------------------------- live research view
  const PHASES = [
    { name: "Identify", stages: ["identity", "plan"] },
    { name: "Search", stages: ["search", "triage"] },
    { name: "Read", stages: ["fetch", "extract", "snippets"] },
    { name: "Verify", stages: ["merge"] },
    { name: "Interpret", stages: ["infer", "portrait"] },
    { name: "Plan", stages: ["converse", "advise", "summarize", "done"] },
  ];
  const ORDER = PHASES.flatMap((p) => p.stages);
  let seenNotes = new Set(), seenRun = null;

  function renderLive(r) {
    const L = r.live || {}, c = L.counts || {}, stage = L.stage || "identity";
    const at = ORDER.indexOf(stage);
    const paused = (r.message || "").startsWith("Paused");
    // tabs: each step shows its own running count
    const phaseCount = [
      L.identity ? `${pct(L.identity.confidence)} sure` : "",
      c.sources ? `${c.sources} sources` : (L.queries?.total ? `${L.queries.done}/${L.queries.total} searches` : ""),
      c.pages_total ? `${c.pages_read}/${c.pages_total} pages` : "",
      c.facts ? `${c.facts} facts` : (c.raw_facts ? `${c.raw_facts} notes` : ""),
      c.insights ? `${c.insights} themes` : (c.inferences ? `${c.inferences} reads` : ""),
      c.themes ? `${c.themes} topics` : "",
    ];
    $("#phases").innerHTML = PHASES.map((p, i) => {
      const first = ORDER.indexOf(p.stages[0]), last = ORDER.indexOf(p.stages[p.stages.length - 1]);
      const cls = at > last ? "done" : at >= first ? "active" : "";
      return `<li class="phase ${cls}" ${cls === "active" ? 'aria-current="step"' : ""}><span class="ph-name">${p.name}</span><span class="ph-n">${cls ? esc(phaseCount[i]) : ""}</span></li>`;
    }).join("");

    $("#live-foot").textContent = paused
      ? "Paused runs pick up again when this page is open, so check back in a few minutes."
      : "It keeps going if you leave. You'll find it under Your people.";
    $("#pbar-fill").style.width = `${Math.max(3, r.pct || 0)}%`;
    $("#pmsg").textContent = r.stage === "queued" ? "Waiting for a free slot" : paused ? r.message : (r.message || "Getting started");

    // who we think this is
    const id = L.identity;
    $("#who-conf").innerHTML = id ? `<b>${pct(id.confidence)}</b> sure it's them` : "";
    if (id) {
      $("#who-body").innerHTML = `<div class="who-name">${esc(id.name)}</div>
        <div class="who-role">${esc([id.role, id.org].filter(Boolean).join(" at "))}${id.location ? `, ${esc(id.location)}` : ""}</div>
        ${id.clues?.length ? `<div class="who-clues">${id.clues.map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : ""}
        ${id.ambiguous ? `<div class="who-warn">There may be more than one person with this name. ${esc(id.note)}</div>` : ""}`;
    } else {
      const q = L.queries?.recent?.[0];
      $("#who-body").innerHTML = `<div class="who-name">${esc(r.title || r.name)}</div>
        <div class="who-wait">${q ? `Searching <q>${esc(q)}</q>` : "Searching the web to work out which person you mean."}</div>`;
    }

    // notes: new ones get inked onto their line
    if (seenRun !== r.id) { seenNotes = new Set(); seenRun = r.id; }
    const notes = L.notes || [];
    $("#feed-count").textContent = c.facts ? `${c.facts} verified` : c.raw_facts ? `${c.raw_facts} collected` : "";
    $("#feed").innerHTML = notes.length ? notes.map((n) => {
      const fresh = !seenNotes.has(n.text);
      const meta = n.conf != null ? `<b>${pct(n.conf)}</b> confidence, from ${esc(n.d)}` : `from ${esc(n.d)}${n.ok ? ", quote checked on the page" : ""}`;
      return `<li class="${fresh ? "fresh" : ""}"><span class="tick ${n.ok ? "" : "no"}" aria-hidden="true">${n.ok ? "\u2713" : "\u2022"}</span>
        <span class="ft">${esc(n.text)}</span><span class="fm">${meta}</span></li>`;
    }).join("") : `<li class="feed-empty">Notes appear here as pages are read.</li>`;
    notes.forEach((n) => seenNotes.add(n.text));

    // sources: namesakes get struck through once they're ruled out
    $("#src-count").textContent = c.sources ? (c.ruled_out ? `${c.ruled_out} of ${c.sources} ruled out` : `${c.sources}`) : "";
    $("#desk").innerHTML = (L.sources || []).map((s) => {
      const st = s.out ? "not them" : s.read ? "read" : s.p != null ? `${pct(s.p)} match` : "found";
      return `<li class="${s.out ? "out" : ""}"><a href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.t)}"><b>${esc(s.d)}</b> <span>${esc(s.t)}</span></a>
        <span class="st ${s.read ? "read" : ""}">${st}</span></li>`;
    }).join("") || `<li><span class="who-wait">Nothing yet.</span></li>`;

    // portrait taking shape
    const shape = L.insights || [], reads = L.reads || [];
    $("#shape").classList.toggle("hidden", !shape.length && !reads.length);
    $("#shape-list").innerHTML = shape.map((i) => `<li>${esc(i.title)}<span>${esc(i.kind)}</span></li>`).join("");
    $("#reads").innerHTML = reads.map((x) => `<li><b>${pct(x.p)}</b>${esc(x.text)}</li>`).join("");
  }

  // ---------------------------------------------------------------- cards
  function strength(c) {
    if (c.confidence == null) return "";
    const word = c.kind === "inference" ? "likely" : c.kind === "overlap" ? "overlap" : "evidence";
    return `<span class="strength"><b>${pct(c.confidence)}</b> ${word}</span>`;
  }
  function kindLabel(c) {
    const k = KIND[c.kind] || "Note";
    if (c.kind === "insight" && c.category) return `${k}: ${c.category.replace(/_/g, " ")}`;
    return k;
  }
  function cardHTML(c) {
    const points = (c.points || []).length ? `<ul class="points">${c.points.map((p) => `<li><span class="dot" style="background:${dotColor(p.confidence)}"></span>
        <span>${esc(p.text)}${p.confidence != null ? `<span class="pc">${pct(p.confidence)}</span>` : ""}</span></li>`).join("")}</ul>` : "";
    const notes = (c.sections || []).map((s) => `<div class="note"><div class="note-label">${esc(s.label)}</div><div class="note-text">${esc(s.text)}</div></div>`).join("");
    const srcs = (c.sources || []).length
      ? `<div class="sources">${c.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>`).join("")}</div>` : "";
    return `<div class="mark keep">Keep</div><div class="mark skip">Set aside</div>
      <div class="card-head"><span class="kind">${esc(kindLabel(c))}</span>${strength(c)}</div>
      <div class="card-body ruled"><h3 class="card-title">${esc(c.title)}</h3>
        ${c.summary ? `<p class="card-summary">${esc(c.summary)}</p>` : ""}${points}${notes}${srcs}</div>`;
  }
  function renderStack() {
    const stack = $("#stack"); stack.innerHTML = "";
    const top3 = state.cards.slice(0, 3);
    const empty = top3.length === 0;
    $("#done-msg").classList.toggle("hidden", !empty);
    stack.classList.toggle("hidden", empty);
    $(".hint").classList.toggle("hidden", empty);
    top3.reverse().forEach((c, i, arr) => {
      const el = document.createElement("article");
      const depth = arr.length - 1 - i;
      el.className = "card index-card" + (depth === 1 ? " behind" : depth === 2 ? " behind2" : "");
      el.innerHTML = cardHTML(c);
      el.dataset.id = c.id;
      if (depth === 0) { el.setAttribute("aria-label", c.title); attachDrag(el, c); }
      stack.appendChild(el);
    });
  }
  function attachDrag(el, card) {
    let sx = 0, sy = 0, dx = 0, axis = null, active = false, decided = false;
    const keepMark = el.querySelector(".mark.keep"), skipMark = el.querySelector(".mark.skip");
    el.addEventListener("pointerdown", (e) => {
      if (e.target.closest("a")) return;
      active = true; axis = null; sx = e.clientX; sy = e.clientY; dx = 0;
    });
    el.addEventListener("pointermove", (e) => {
      if (!active) return;
      const mx = e.clientX - sx, my = e.clientY - sy;
      if (!axis && Math.hypot(mx, my) > 8) {
        axis = Math.abs(mx) > Math.abs(my) ? "x" : "y";  // vertical scrolls the card, horizontal sorts it
        if (axis === "x") { el.setPointerCapture(e.pointerId); el.classList.add("dragging"); }
      }
      if (axis !== "x") return;
      dx = mx;
      el.style.transform = `translateX(${dx}px) rotate(${dx / 24}deg)`;
      keepMark.style.opacity = Math.min(1, Math.max(0, -dx / 80));
      skipMark.style.opacity = Math.min(1, Math.max(0, dx / 80));
    });
    const end = () => {
      if (!active || decided) return;
      active = false;
      if (axis !== "x") return;
      el.classList.remove("dragging");
      const threshold = Math.min(110, window.innerWidth * 0.24);
      if (dx < -threshold) { decided = true; decide(card, "keep", el); }
      else if (dx > threshold) { decided = true; decide(card, "skip", el); }
      else { el.style.transform = ""; keepMark.style.opacity = skipMark.style.opacity = 0; }
    };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }
  function decide(card, decision, el) {
    el = el || document.querySelector(`.card[data-id="${card.id}"]`);
    const pile = decision === "keep" ? $("#a-keep") : $("#a-skip");
    if (el) {  // the card flies down into its pile
      const p = pile.getBoundingClientRect(), r = el.getBoundingClientRect();
      const tx = p.left + p.width / 2 - (r.left + r.width / 2), ty = p.top - (r.top + r.height / 2);
      el.style.transform = `translate(${tx}px, ${ty}px) scale(.12) rotate(${decision === "keep" ? -14 : 14}deg)`;
      el.style.opacity = "0";
    }
    setTimeout(() => { pile.classList.remove("bump"); void pile.offsetWidth; pile.classList.add("bump"); }, 200);
    if (navigator.vibrate) navigator.vibrate(decision === "keep" ? 16 : 8);
    state.cards = state.cards.filter((c) => c.id !== card.id);
    if (decision === "keep") state.kept.push({ ...card, decision }); else state.skipped++;
    state.history.push({ card, decision });
    setTimeout(renderStack, 260);
    updateCounts();
    sendDecision(state.current.id, card.id, decision);
  }
  $("#a-keep").onclick = () => state.cards[0] && decide(state.cards[0], "keep");
  $("#a-skip").onclick = () => state.cards[0] && decide(state.cards[0], "skip");
  $("#a-undo").onclick = () => {
    const h = state.history.pop(); if (!h) return;
    if (h.decision === "keep") state.kept = state.kept.filter((k) => k.id !== h.card.id); else state.skipped = Math.max(0, state.skipped - 1);
    state.cards.unshift(h.card); renderStack(); updateCounts();
    sendDecision(state.current.id, h.card.id, null);
  };
  document.addEventListener("keydown", (e) => {
    if (state.tab !== "triage" || !state.current || e.target.closest("input,textarea")) return;
    if (e.key === "ArrowLeft") $("#a-keep").click();
    if (e.key === "ArrowRight") $("#a-skip").click();
    if (e.key === "z" && (e.ctrlKey || e.metaKey)) $("#a-undo").click();
  });

  // ---------------------------------------------------------------- brief
  function renderBrief() {
    const kept = state.kept || [];
    if (!kept.length) {
      $("#brief").innerHTML = `<p class="brief-empty">Your brief fills up as you keep cards. Drag cards left in Sort to keep them.</p>`;
      return;
    }
    const groups = {};
    kept.forEach((c) => (groups[c.kind] ||= []).push(c));
    $("#brief").innerHTML = BRIEF.filter(([k]) => groups[k]).map(([k, title]) => `
      <section class="brief-group"><h2>${title}</h2>
      ${groups[k].map((c) => {
        const more = (c.points || []).length + (c.sections || []).length;
        return `<details class="brief-card index-card"><summary>
            <div class="card-head"><span class="bt">${esc(c.title)}</span>${strength(c)}</div>
            <div class="ruled">${c.summary ? `<p class="bs">${esc(c.summary)}</p>` : ""}${more ? `<span class="more">Show details</span>` : ""}</div>
          </summary>
          <div class="ruled">
            ${(c.points || []).length ? `<ul class="points">${c.points.map((p) => `<li><span class="dot" style="background:${dotColor(p.confidence)}"></span><span>${esc(p.text)}</span></li>`).join("")}</ul>` : ""}
            ${(c.sections || []).map((s) => `<div class="note"><div class="note-label">${esc(s.label)}</div><div class="note-text">${esc(s.text)}</div></div>`).join("")}
            <button class="unkeep" data-unkeep="${c.id}">Remove from brief</button>
          </div></details>`;
      }).join("")}</section>`).join("");
    $("#brief").querySelectorAll("[data-unkeep]").forEach((b) => b.addEventListener("click", () => {
      const id = Number(b.dataset.unkeep);
      state.kept = state.kept.filter((k) => k.id !== id); state.skipped++;
      updateCounts(); renderBrief();
      sendDecision(state.current.id, id, "skip");
    }));
  }

  loadConfig().then(route);
})();
