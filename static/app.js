/* JobMatch AI — frontend logic */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  resumeText: "",
  resumeSkills: [],
  scope: "pakistan",
  workType: "any",
  postedDays: 42,
  directory: [],
  searching: false,
};

/* ---------------------------------------------------------------- helpers */

function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = "toast"), 4200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function relTime(iso) {
  if (!iso) return "date n/a";
  const then = new Date(iso);
  if (isNaN(then)) return "date n/a";
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "1d ago";
  if (days < 7) return days + "d ago";
  if (days < 30) return Math.floor(days / 7) + "w ago";
  return Math.floor(days / 30) + "mo ago";
}

async function readNdjson(response, onLine) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) {
        try { onLine(JSON.parse(line)); } catch { /* skip bad line */ }
      }
    }
  }
  if (buffer.trim()) {
    try { onLine(JSON.parse(buffer.trim())); } catch { /* noop */ }
  }
}

/* ------------------------------------------------------------------- meta */

async function loadMeta(attempt = 0) {
  try {
    const res = await fetch("/api/meta");
    if (!res.ok) throw new Error("not ready");
    const m = await res.json();
    hideWaking();
    state.meta = m;
    $("metaBoards").textContent = m.boards_total;
    $("metaAggs").textContent = m.aggregators.length + m.keyed_active;
    $("metaDir").textContent = m.directory_total;
    $("dirCount").textContent = "(" + m.directory_total + ")";
    $("feedCount").textContent = "(" + (m.aggregators.length + m.keyed_active) + " on)";
    renderFeeds(m);
    for (const s of m.sectors) {
      $("dirSector").insertAdjacentHTML("beforeend",
        `<option value="${esc(s)}">${esc(s)}</option>`);
    }
    for (const c of m.cities) {
      $("dirCity").insertAdjacentHTML("beforeend",
        `<option value="${esc(c)}">${esc(c)}</option>`);
    }
  } catch {
    // Free-tier hosts sleep after idling; the first request wakes the server
    // and can take up to a minute. Keep retrying and say so plainly.
    if (attempt < 15) {
      showWaking(attempt);
      setTimeout(() => loadMeta(attempt + 1), attempt === 0 ? 1500 : 4000);
    } else {
      hideWaking();
      toast("Can't reach the server. Check that it's running.", true);
    }
  }
}

function showWaking(attempt) {
  const el = $("waking");
  el.hidden = false;
  el.innerHTML = attempt === 0
    ? `<span class="spin"></span>Connecting…`
    : `<span class="spin"></span>Waking the server — free hosting sleeps after
       15 minutes idle, so this first load takes up to a minute. It's quick
       from here on.`;
}

function hideWaking() {
  $("waking").hidden = true;
}

/* --------------------------------------------------------- explain a match */

async function explainJob(index, btn) {
  const job = state.jobs[index];
  const box = document.getElementById("explain-" + index);
  if (!job || !box) return;

  if (!box.hidden && box.dataset.done === "1") {   // toggle closed
    box.hidden = true;
    btn.textContent = "Why this match?";
    return;
  }
  if (box.dataset.done === "1") {                  // already fetched
    box.hidden = false;
    btn.textContent = "Hide";
    return;
  }

  box.hidden = false;
  box.innerHTML = `<span class="spin"></span>Reading the posting against your resume…`;
  btn.disabled = true;

  try {
    const res = await fetch("/api/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume_text: state.resumeText, job }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't generate that.");
    box.textContent = data.explanation;
    box.dataset.done = "1";
    btn.textContent = "Hide";
  } catch (e) {
    box.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------- job feeds */

const FEED_BLURBS = {
  remotive: "Curated remote roles across engineering, design and support.",
  arbeitnow: "Open job board feed, Europe-heavy with remote listings.",
  jobicy: "Remote-first board covering tech, marketing and ops.",
  himalayas: "Remote roles with clear location eligibility per job.",
  remoteok: "High-volume remote board, strong on engineering.",
  weworkremotely: "Long-running remote board, published as an open feed.",
};

function renderFeeds(m) {
  $("feedsOn").innerHTML = [
    `<div class="feed">
      <span class="pill on">Live</span>
      <div><div class="nm">${m.boards_total} company boards</div>
        <div class="desc">Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
          Recruitee, BambooHR and Breezy — read straight from each company's own
          hiring system. ${m.boards_pk} of them are Pakistan-linked.</div>
        <div class="kv"><b>Coverage</b> Pakistan + global remote-friendly · <b>Cost</b> free, no key</div>
      </div>
      <button class="go" type="button" onclick="document.getElementById('verifyBtn').click()">Check them ↻</button>
    </div>`,
    ...m.aggregators.map((a) => `<div class="feed">
      <span class="pill on">Live</span>
      <div><div class="nm">${esc(a)}</div>
        <div class="desc">${esc(FEED_BLURBS[a] || "Open remote job feed.")}</div>
        <div class="kv"><b>Coverage</b> remote worldwide, filtered to Pakistan-eligible in PK scope · <b>Cost</b> free, no key</div>
      </div><span></span>
    </div>`),
  ].join("");

  $("feedActiveStat").textContent = `${m.keyed_active}/${m.keyed_sources.length} connected`;
  $("feedsKeyed").innerHTML = m.keyed_sources.map((k) => `<div class="feed">
    <span class="pill ${k.active ? "on" : "off"}">${k.active ? "Connected" : "Needs key"}</span>
    <div>
      <div class="nm">${esc(k.label)}</div>
      <div class="desc">${esc(k.note)}</div>
      <div class="kv"><b>Coverage</b> ${esc(k.coverage)} · <b>Cost</b> ${esc(k.cost)} ·
        <b>Key${k.keys.length > 1 ? "s" : ""}</b> ${k.keys.map(esc).join(", ")}</div>
    </div>
    <a class="go" href="${esc(k.signup)}" target="_blank" rel="noopener">
      ${k.active ? "Docs ↗" : "Get key ↗"}</a>
  </div>`).join("");

  $("feedsBlocked").innerHTML = m.restricted_sites.map((s) => `<div class="feed">
    <span class="pill blocked">Link only</span>
    <div>
      <div class="nm">${esc(s.name)}</div>
      <div class="desc">${esc(s.why)}</div>
      <div class="kv"><b>How JobMatch handles it</b> ${esc(s.route)}</div>
    </div><span></span>
  </div>`).join("");
}

/* ------------------------------------------------------------ quick links */

function refreshQuickLinks() {
  const kw = $("keywords").value.trim() || "software engineer";
  const city = $("city").value.trim();
  const q = encodeURIComponent(kw);
  const links = [
    ["Rozee.pk", `https://www.rozee.pk/job/jsearch/q/${encodeURIComponent(kw)}`],
    ["Indeed PK", `https://pk.indeed.com/jobs?q=${q}&l=${encodeURIComponent(city || "Pakistan")}`],
    ["LinkedIn", `https://www.linkedin.com/jobs/search/?keywords=${q}&location=${encodeURIComponent(city ? city + ", Pakistan" : "Pakistan")}`],
    ["Mustakbil", `https://www.mustakbil.com/jobs/search?q=${q}`],
    ["Bayt", `https://www.bayt.com/en/pakistan/jobs/?text=${q}`],
    ["Glassdoor", `https://www.glassdoor.com/Search/results.htm?keyword=${q}`],
  ];
  $("quickLinks").innerHTML = links.map(([name, url]) =>
    `<a href="${url}" target="_blank" rel="noopener">${name} ↗</a>`).join("");
}

/* ----------------------------------------------------------------- resume */

function setResume(data) {
  state.resumeText = data.text;
  state.resumeSkills = data.skills || [];
  $("resumeText").value = data.text;
  $("loaded").hidden = false;
  $("loadedName").textContent = data.source_name;
  $("loadedMeta").textContent = `${data.words} words · ${data.skills.length} skills`;
  $("resumeStat").hidden = false;
  $("resumeStat").textContent = `${data.skills.length} skills · ${data.words} words`;
  $("skills").innerHTML = data.skills.map(
    (s) => `<span class="chip">${esc(s)}</span>`).join("");
}

async function uploadFile(file) {
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    toast("That file is over 5 MB — paste the text instead.", true);
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  $("loadedName").textContent = "Reading " + file.name + "…";
  $("loaded").hidden = false;
  try {
    const res = await fetch("/api/resume", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't parse that file.");
    setResume(data);
  } catch (e) {
    $("loaded").hidden = true;
    toast(e.message, true);
  }
}

async function useTypedText() {
  const text = $("resumeText").value.trim();
  if (text.length < 40) {
    toast("Add a bit more text first — at least a few lines.", true);
    return;
  }
  const fd = new FormData();
  fd.append("text", text);
  try {
    const res = await fetch("/api/resume", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't use that text.");
    setResume(data);
    $("editor").classList.remove("open");
    toast("Resume text updated.");
  } catch (e) {
    toast(e.message, true);
  }
}

function wireResume() {
  const drop = $("drop");
  const fileInput = $("fileInput");

  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", () => uploadFile(fileInput.files[0]));

  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => uploadFile(e.dataTransfer.files[0]));

  $("modeUpload").addEventListener("click", () => {
    drop.hidden = false;
    $("editor").classList.remove("open");
    $("modeUpload").className = "btn-solid";
    $("modePaste").className = "btn-ghost";
  });
  $("modePaste").addEventListener("click", () => {
    drop.hidden = true;
    $("editor").classList.add("open");
    $("modeUpload").className = "btn-ghost";
    $("modePaste").className = "btn-solid";
    $("resumeText").focus();
  });

  $("editBtn").addEventListener("click", () => {
    $("editor").classList.toggle("open");
    if ($("editor").classList.contains("open")) $("resumeText").focus();
  });
  $("closeEditorBtn").addEventListener("click", () => $("editor").classList.remove("open"));
  $("useTextBtn").addEventListener("click", useTypedText);
}

/* ---------------------------------------------------------- seg + slider */

function wireSeg(id, onPick) {
  const seg = $(id);
  seg.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      seg.querySelectorAll("button").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      onPick(btn.dataset.v);
    });
  });
}

function wireControls() {
  wireSeg("scopeSeg", (v) => (state.scope = v));
  wireSeg("workSeg", (v) => (state.workType = v));
  wireSeg("postedSeg", (v) => (state.postedDays = v ? parseInt(v, 10) : null));

  const slider = $("minMatch");
  const updateSlider = () => {
    const v = parseInt(slider.value, 10);
    slider.style.setProperty("--fill", (v / 90) * 100 + "%");
    $("minMatchVal").textContent = v === 0 ? "any" : v + "%+";
  };
  slider.addEventListener("input", updateSlider);
  updateSlider();

  $("keywords").addEventListener("input", refreshQuickLinks);
  $("city").addEventListener("input", refreshQuickLinks);
  refreshQuickLinks();
}

/* ------------------------------------------------------------ source board */

const board = {
  reset(total, title) {
    $("board").classList.add("open");
    $("boardTitle").innerHTML = `<span class="spin"></span>${esc(title)}`;
    $("boardTotal").textContent = total;
    $("boardDone").textContent = "0";
    $("boardJobs").textContent = "0";
    $("progressBar").style.width = "0%";
    $("signal").innerHTML = "";
    this.done = 0; this.total = total; this.jobs = 0;
  },
  line(status) {
    this.done += 1;
    this.jobs += status.count || 0;
    $("boardDone").textContent = this.done;
    $("boardJobs").textContent = this.jobs;
    $("progressBar").style.width = ((this.done / this.total) * 100) + "%";
    const head = $("resultsHead");
    if (this.jobs > 0) {
      head.hidden = false;
      $("resultsN").textContent = this.jobs;
      $("resultsLabel").textContent = "jobs found so far — still searching";
    }

    const cls = status.ok ? (status.count ? "ok" : "warn") : "fail";
    const mark = status.ok ? (status.count ? "✓" : "○") : "✕";
    const info = status.ok
      ? (status.count ? `${status.count} jobs` : "0 open roles right now")
      : (status.error || "failed");
    const sig = $("signal");
    sig.insertAdjacentHTML("beforeend",
      `<div class="ln ${cls}"><span class="st">${mark}</span>` +
      `<span class="src">${esc(status.label)}</span>` +
      `<span class="info">— ${esc(info)}</span></div>`);
    sig.scrollTop = sig.scrollHeight;
  },
  finish(title) {
    $("boardTitle").textContent = title;
  },
};

/* ----------------------------------------------------------------- search */

function bandOf(score) {
  if (score == null) return ["none", "–", ""];
  if (score >= 70) return ["strong", score + "%", "strong"];
  if (score >= 50) return ["good", score + "%", "good"];
  if (score >= 30) return ["fair", score + "%", "fair"];
  return ["weak", score + "%", "weak"];
}

function renderJobs(jobs) {
  const box = $("results");
  if (!jobs.length) {
    box.innerHTML = "";
    $("emptyState").style.display = "";
    const r = state.lastRun;

    // Blaming the filters is only honest when something was actually collected.
    // If every source came back empty or failed, the filters are innocent and
    // pointing at them sends you to fiddle with the wrong thing.
    if (r && r.total_collected === 0) {
      let why =
        "<b>No jobs were collected — this is not a filter problem.</b><br>" +
        `${r.sources_ok + r.sources_empty} sources answered, ${r.sources_failed} failed, ` +
        "and none returned a single job.<br>";
      if (r.sample_errors && r.sample_errors.length) {
        why += "<br><b>What the sources said:</b><br>" +
          r.sample_errors.map(esc).join("<br>");
      } else {
        why += "<br>Every source replied normally with zero open roles, " +
          "which usually means the board slugs are wrong. " +
          "Open <code>/api/sources/verify</code> to check them.";
      }
      $("emptyState").innerHTML = why;
    } else if (r) {
      $("emptyState").innerHTML =
        `<b>Collected ${r.total_collected} jobs, but none survived your filters.</b><br>` +
        "Try widening “Posted within”, clearing the city, clearing keywords, " +
        "or lowering the minimum match.";
    } else {
      $("emptyState").innerHTML =
        "<b>No jobs made it through your filters.</b><br>" +
        "Try widening “Posted within”, clearing the city, or lowering the minimum match.";
    }
    $("resultsHead").hidden = true;
    return;
  }
  $("emptyState").style.display = "none";
  $("resultsHead").hidden = false;
  $("resultsN").textContent = jobs.length;
  $("resultsLabel").textContent = state.totalFound > jobs.length
    ? `shown of ${state.totalFound} matches — best first`
    : "jobs after filters — sorted by match";

  state.jobs = jobs;
  box.innerHTML = jobs.map((j, i) => {
    const [cls, pct, bandLabel] = bandOf(j.match_score);
    const skills = (j.matched_skills || []).map(
      (s) => `<span class="chip hit">${esc(s)}</span>`).join("");
    return `<article class="job">
      <div class="score ${cls}">
        <div class="pct">${pct}</div>
        ${bandLabel ? `<div class="band">${bandLabel}</div>` : ""}
      </div>
      <div class="body">
        <h3>${esc(j.title)}</h3>
        <div class="line2">
          <span>${esc(j.company || "—")}</span>
          <span>${esc(j.location)}</span>
          <span>${relTime(j.posted_at)}</span>
          <span class="src-chip">${esc(j.source)}</span>
        </div>
        ${skills ? `<div class="mskills">${skills}</div>` : ""}
      </div>
      <div class="apply">
        <a class="btn-solid" href="${esc(j.url)}" target="_blank" rel="noopener">Apply ↗</a>
        ${state.meta && state.meta.explain_enabled
          ? `<button class="btn-ghost explain-btn" data-i="${i}" type="button">Why this match?</button>`
          : ""}
      </div>
      <div class="explain" id="explain-${i}" hidden></div>
    </article>`;
  }).join("");
}

function showSkeletons(n = 6) {
  $("emptyState").style.display = "none";
  $("results").innerHTML = Array.from({ length: n }, () => `
    <article class="job skeleton">
      <div class="score"><div class="sk sk-pct"></div><div class="sk sk-band"></div></div>
      <div class="body">
        <div class="sk sk-title"></div>
        <div class="sk sk-line"></div>
        <div class="mskills">
          <div class="sk sk-chip"></div><div class="sk sk-chip"></div><div class="sk sk-chip"></div>
        </div>
      </div>
      <div class="apply"><div class="sk sk-btn"></div></div>
    </article>`).join("");
}

async function runSearch() {
  if (state.searching) return;
  state.searching = true;
  const btn = $("searchBtn");
  btn.disabled = true;
  btn.textContent = "Searching…";
  $("resultsHead").hidden = true;
  showSkeletons();

  const payload = {
    resume_text: state.resumeText,
    keywords: $("keywords").value.trim(),
    scope: state.scope,
    city: $("city").value.trim(),
    work_type: state.workType,
    posted_within_days: state.postedDays,
    min_match: parseInt($("minMatch").value, 10),
  };

  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok || !res.body) throw new Error("Search failed to start (HTTP " + res.status + ").");

    let gotDone = false;
    await readNdjson(res, (msg) => {
      if (msg.type === "begin") {
        board.reset(msg.total_sources, "Contacting sources…");
      } else if (msg.type === "source") {
        board.line(msg.status);
      } else if (msg.type === "done") {
        gotDone = true;
        state.totalFound = msg.total_found;
        state.lastRun = msg;
        board.finish(
          `Done — ${msg.sources_ok} sources had jobs, ${msg.sources_empty} were empty, ` +
          `${msg.sources_failed} failed` +
          (msg.boards_from_cache ? `, ${msg.boards_from_cache} boards from cache` : "") +
          `. Collected ${msg.total_collected}, ${msg.total_found} left after filters.`
        );
        $("matchCount").textContent = "(" + msg.total_found + ")";
        renderJobs(msg.jobs);
        if (!state.resumeText && msg.total_found) {
          toast("Tip: load your resume to get match scores on these jobs.");
        }
      }
    });

    // The stream can end without a final message if the server restarts
    // mid-search. Never leave the spinner running.
    if (!gotDone) {
      board.finish("Search ended early — the connection dropped.");
      renderJobs([]);
      toast("The search was cut short. Try again.", true);
    }
  } catch (e) {
    toast(e.message || "Search failed — check the backend logs.", true);
    board.finish("Search stopped early.");
  } finally {
    state.searching = false;
    btn.disabled = false;
    btn.textContent = "Find matching jobs";
  }
}

/* ----------------------------------------------------------- verify boards */

async function verifySources() {
  const btn = $("verifyBtn");
  btn.disabled = true;
  showPanel("matches");
  try {
    const res = await fetch("/api/sources/verify");
    if (!res.ok || !res.body) throw new Error("Verification failed to start.");
    await readNdjson(res, (msg) => {
      if (msg.type === "begin") board.reset(msg.total, "Verifying company boards…");
      else if (msg.type === "source") board.line(msg.status);
      else if (msg.type === "done")
        board.finish(`Board check done — ${msg.alive}/${msg.total} boards are live. Results are cached for the next search.`);
    });
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* -------------------------------------------------------------- directory */

async function loadDirectory() {
  try {
    const d = await (await fetch("/api/directory")).json();
    state.directory = d.companies;
    renderDirectory();
  } catch {
    toast("Couldn't load the company directory.", true);
  }
}

function renderDirectory() {
  const q = $("dirSearch").value.trim().toLowerCase();
  const sector = $("dirSector").value;
  const city = $("dirCity").value;

  let rows = state.directory;
  if (sector) rows = rows.filter((r) => r.sector === sector);
  if (city) rows = rows.filter((r) => r.city === city);
  if (q) rows = rows.filter((r) =>
    r.name.toLowerCase().includes(q) ||
    r.sector.toLowerCase().includes(q) ||
    r.city.toLowerCase().includes(q));

  $("dirRows").innerHTML = rows.slice(0, 600).map((r) => {
    const searchUrl = "https://www.google.com/search?q=" +
      encodeURIComponent(`"${r.name}" careers jobs`);
    const site = r.website
      ? `<a href="https://${esc(r.website)}" target="_blank" rel="noopener">Website ↗</a>`
      : "";
    return `<div class="dir-row">
      <span class="nm">${esc(r.name)}</span>
      <span class="sec">${esc(r.sector)}</span>
      <span class="ct">${esc(r.city)}</span>
      <span class="links">${site}
        <a href="${searchUrl}" target="_blank" rel="noopener">Find jobs ↗</a>
      </span>
    </div>`;
  }).join("");

  $("dirFoot").textContent = rows.length + " companies shown. " +
    "Every entry links to a careers search that always works, plus the official site where known.";
}

/* ------------------------------------------------------------------- tabs */

function showPanel(which) {
  const panels = { matches: "panelMatches", directory: "panelDirectory", feeds: "panelFeeds" };
  const tabs = { matches: "tabMatches", directory: "tabDirectory", feeds: "tabFeeds" };
  for (const [name, panelId] of Object.entries(panels)) {
    $(panelId).hidden = name !== which;
    $(tabs[name]).classList.toggle("on", name === which);
  }
}

/* ------------------------------------------------------------------- init */

document.addEventListener("DOMContentLoaded", () => {
  wireResume();
  wireControls();
  loadMeta();
  loadDirectory();

  $("results").addEventListener("click", (e) => {
    const btn = e.target.closest(".explain-btn");
    if (btn) explainJob(parseInt(btn.dataset.i, 10), btn);
  });

  $("searchBtn").addEventListener("click", runSearch);
  $("verifyBtn").addEventListener("click", verifySources);
  $("tabMatches").addEventListener("click", () => showPanel("matches"));
  $("tabDirectory").addEventListener("click", () => showPanel("directory"));
  $("tabFeeds").addEventListener("click", () => showPanel("feeds"));
  $("whyLinksBtn").addEventListener("click", () => {
    showPanel("feeds");
    $("panelFeeds").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  ["dirSearch", "dirSector", "dirCity"].forEach((id) =>
    $(id).addEventListener("input", renderDirectory));
});
