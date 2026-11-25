/* RoadSense web app. Vanilla JS, hash routing, no build step. All server text is escaped before it touches the DOM:
   agent-written text (rationales, drafts) is untrusted. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = $("#view");
let current = null;
/* Each route renders into its own container: a slow, stale render writes into a detached node, never over the page. */
const stage = () => current;
let maps = [];
let timers = [];

/* The signed-in user. The session cookie is HttpOnly; the CSRF token comes from /api/me and goes on every change. */
const session = { user: null, csrf: "", perms: [], sso: false, authEnabled: true };
const can = (perm) => session.perms.includes(perm);

async function api(path, opts = {}) {
  const headers = opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {};
  if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = session.csrf;
  const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) }, credentials: "same-origin" });
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (r.status === 401 && !path.startsWith("/api/auth/")) { showLogin(); throw new Error("Please sign in"); }
  if (!r.ok) throw new Error(typeof body?.detail === "string" ? body.detail : body?.detail ? JSON.stringify(body.detail) : body || r.statusText);
  return body;
}

/* Success fades on its own; an error stays until dismissed, so a failed send is never missed. Hovering pauses the timer. */
function toast(msg, ok = true) {
  const t = $("#toast");
  t.innerHTML = `${icon(ok ? "check" : "alert")}<span class="toast-msg">${esc(msg)}</span><button type="button" class="toast-x" aria-label="Dismiss">${icon("x")}</button>`;
  t.setAttribute("role", ok ? "status" : "alert");
  t.classList.toggle("err", !ok);
  t.classList.add("show");
  const hide = () => t.classList.remove("show");
  $(".toast-x", t).onclick = hide;
  clearTimeout(toast.t);
  const arm = () => { clearTimeout(toast.t); if (ok) toast.t = setTimeout(hide, 4200); };
  t.onmouseenter = () => clearTimeout(toast.t);
  t.onmouseleave = arm;
  arm();
}

/* ---------- icons (Lucide-style paths) ---------- */
const ICONS = {
  check: '<path d="M20 6L9 17l-5-5"/>', alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>',
  merge: '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 009 9"/>',
  route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 000-7h-11a3.5 3.5 0 010-7H15"/><circle cx="18" cy="5" r="3"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>', inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 00-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 005.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  pin: '<path d="M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/>',
  layers: '<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>', send: '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>',
  user: '<path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/>', cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  sparkle: '<path d="M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2z"/>', shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  upload: '<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>', video: '<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>', building: '<path d="M3 21h18M5 21V10M19 21V10M9 21v-6h6v6M2 10l10-7 10 7"/>',
  arrow: '<path d="M9 18l6-6-6-6"/>', lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>',
  refresh: '<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0114.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0020.5 15"/>',
  ext: '<path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>', plus: '<path d="M12 5v14M5 12h14"/>',
  grid: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  file: '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  map: '<path d="M1 6v16l7-4 8 4 7-4V2l-7 4-8-4-7 4z"/><path d="M8 2v16M16 6v16"/>', activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  moon: '<path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/>',
  cloud: '<path d="M18 10h-1.3A8 8 0 109 20h9a5 5 0 000-10z"/>',
  zoom: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/>',
  keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
};
const icon = (n, cls = "") => `<svg class="${cls}" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[n] || ""}</svg>`;

/* ---------- vocabulary ---------- */
const STATUS = {
  pending_approval: ["Awaiting review", "amber"], approved: ["Approved", "blue"], submitted: ["Sent", "violet"],
  fixed: ["Closed by authority", "green"], rejected: ["Rejected", ""], delivery_failed: ["Delivery failed", "red"],
};
// RoadSense's own check after the authority closes a report, shown next to (never instead of) the authority's status
const VERIFICATION = {
  awaiting: ["Awaiting verification", "amber"], verified: ["Repair verified", "green"], reopened: ["Reopened", "red"],
};
const RESULT = { verified: ["Verified", "green"], not_verified: ["Not verified", "red"], inconclusive: ["Inconclusive", "amber"] };
const verifPill = (s) => { const [t, c] = VERIFICATION[s] || []; return t ? `<span class="pill ${c}"><span class="dot"></span>${esc(t)}</span>` : ""; };
const CLASS_LABELS = { D40: "Pothole", D20: "Alligator crack", D10: "Transverse crack", D00: "Longitudinal crack" };
const CHANNELS = { open311: "Open311 API", cityworks: "Cityworks API", arcgis: "ArcGIS feature service", referral: "Referral package" };
const ROLE_TONE = { admin: "violet", reviewer: "blue", driver: "cyan", viewer: "" };
const statusPill = (s) => { const [t, c] = STATUS[s] || [s, ""]; return `<span class="pill ${c}"><span class="dot"></span>${esc(t)}</span>`; };
const SEVERITY = { high: "red", medium: "amber", low: "" };
const sevPill = (s) => `<span class="pill ${SEVERITY[s] || ""}">${esc(s ? s[0].toUpperCase() + s.slice(1) : "")}</span>`;
const ACTORS = {
  "agent:orchestrator": ["Workflow", "violet", "sparkle"], "agent:vision": ["Vision step", "blue", "eye"],
  "agent:location": ["Location step", "cyan", "pin"], "agent:triage": ["Triage step", "amber", "merge"],
  "agent:filing": ["Drafting step", "green", "send"], "agent:follow_up": ["Follow-up", "green", "wrench"],
  driver: ["Driver", "", "user"], edge: ["Edge device", "", "video"], fallback: ["Rule engine", "", "cpu"],
  scheduler: ["Scheduler", "", "clock"], retention: ["Retention policy", "", "shield"], dedupe: ["Dedupe", "", "merge"],
  "sync:city": ["City data sync", "cyan", "building"], service: ["Service", "", "cpu"],
};
const actor = (a) => ACTORS[a] || (a?.startsWith("mcp:") ? [`${a.slice(4)} tool`, "", "layers"]
  : a?.includes("@") ? [a.replace(/^user:/, ""), "blue", "user"] : a?.startsWith("feed:") ? [`${a.slice(5)} feed`, "cyan", "layers"] : [a, "", "cpu"]);
const avatar = (a) => { const [, c, i] = actor(a); return `<span class="avatar pill ${c}">${icon(i)}</span>`; };
const AUTH_SHORT = { "Halifax Regional Municipality": "Halifax 311", "Nova Scotia Public Works": "NS Public Works" };
const authority = (j) => AUTH_SHORT[j] || j || "Unassigned";
const pct = (x) => `${Math.round((x || 0) * 100)}%`;
const num = (x) => new Intl.NumberFormat().format(x || 0);

function ago(ts) {
  if (!ts) return "";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function parse(detail) { try { return JSON.parse(detail); } catch { return detail; } }

/* What an audit entry means, in plain language */
function describe(a) {
  const d = parse(a.detail);
  const subj = esc((a.subject || "").replace(/^(report|event|trip):/, ""));
  const rationale = typeof d === "string" ? d : d?.rationale || "";
  switch (true) {
    case a.action === "verdict_confirmed": return ["Confirmed real damage", rationale];
    case a.action === "verdict_disputed": return ["Flagged: models disagree on the damage type", rationale];
    case a.action === "verdict_rejected": return ["Rejected as a false alarm", rationale];
    case a.action === "report_created": return [`Opened report #${subj}`, d?.rationale || ""];
    case a.action === "report_merged": return [`Merged into report #${subj}`, d?.distance_m != null ? `Another sighting ${d.distance_m} m from an open report, so no new ticket. ${d?.rationale || ""}` : ""];
    case a.action === "status_approved": return ["Approved the exact payload", "Locked by hash"];
    case a.action === "status_submitted": return [`Sent to the authority`, d?.service_request_id ? `Request ${d.service_request_id} via ${d.channel}` : ""];
    case a.action === "status_fixed": return ["Closed by the authority", "RoadSense will check the spot independently"];
    case a.action === "repair_awaiting_verification": return ["Awaiting independent verification", typeof d === "string" ? d : ""];
    case a.action === "repair_verification_completed": return [`Re-observed: ${(RESULT[d?.result] || [d?.result])[0]}`, d?.reason || ""];
    case a.action === "repair_verified": return ["Repair independently verified", d?.reason || ""];
    case a.action === "repair_not_verified": return ["Repair not verified: damage seen again", d?.reason || ""];
    case a.action === "report_reopened": return ["Reopened by RoadSense", d?.reason || ""];
    case a.action === "status_rejected": return ["Rejected by a reviewer", ""];
    case a.action === "status_delivery_failed": return ["Delivery failed: retries exhausted", d?.last_error || ""];
    case a.action === "report_suppressed": return [`Suppressed a repeat of rejected report #${subj}`, d?.distance_m != null ? `Sighting ${d.distance_m} m from a report a reviewer rejected` : ""];
    case a.action === "report_reclassified": return [`Damage class corrected`, d?.from ? `${d.from} → ${d.to}` : ""];
    case a.action === "severity_disagreement": return ["Agent severity overruled by the rule", d?.agent ? `Agent said ${d.agent}, rule says ${d.rule}: ${d.reason || ""}` : ""];
    case a.action === "jurisdiction_overridden": return ["Agent's authority overruled by map evidence", d?.agent ? `Agent said ${d.agent}; used ${d.used}` : ""];
    case a.action === "external_match": return ["Matched a request the city already has", d?.external ? `${d.external} (${d.source}), ${d.distance_m} m away` : ""];
    case a.action === "update_pushed": return ["Pushed new evidence to the authority", ""];
    case a.action === "purge": return ["Retention policy applied", typeof d === "object" ? Object.entries(d || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ") : ""];
    case a.action === "signed_in": return ["Signed in", ""];
    case a.action === "trip_done": return [`Finished trip ${subj}`, d?.summary || ""];
    case a.action === "plan": return ["Plan", typeof d === "string" ? d : ""];
    case a.action === "decided": {
      // One AI call over a batch of borderline items: what it was asked and what it chose
      const items = (d?.items || []).map((x) => `${x.id}: ${x.verdict || x.jurisdiction || (x.description ? "drafted" : "")}`);
      return [`${d?.title || "AI decision"} (${d?.asked ?? items.length})`, items.join(" · ")];
    }
    case a.action === "invalid_output": return ["AI answer didn't match the format; asked once more", ""];
    case a.action === "answer": {
      // The Follow-up agent answers in JSON; say what it found instead of printing it
      let j = null; try { j = JSON.parse(d?.content || ""); } catch {}
      if (j && typeof j === "object" && "checked" in j) {
        const fixed = (j.fixed || []).length;
        return ["Checked requests sent to the city", `${j.checked} checked · ${fixed ? `${fixed} repaired` : "no repairs yet"}`];
      }
      if (j && Array.isArray(j.reports)) {
        const n = j.reports.length;
        return [`Finished ${n} report${n === 1 ? "" : "s"}`, j.reports.map((x) => x.detail || `#${x.report_id} ${x.outcome || ""}`).join(" · ")];
      }
      if (j && typeof j === "object") {
        const text = j.summary || j.message || Object.entries(j).filter(([, v]) => v !== null && typeof v !== "object").map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ");
        return ["Answer", text];
      }
      return ["Answer", d?.content || ""];
    }
    case a.action === "task": return ["Started a trip", typeof d === "string" ? d : ""];
    case a.action === "verify": return ["Verifier check (RT-DETR-L)", d?.reason || ""];
    case a.action === "recheck": return ["Close-up re-check", d?.reason || ""];
    case a.action === "event_received": return ["Detection uploaded", d?.class ? `${d.class} on trip ${d.trip}` : ""];
    case a.action === "completed_event": return ["Completed a step the agents skipped", ""];
    case a.action === "llm_unavailable": return ["LLM unavailable, rule engine took over", typeof d === "string" ? d : ""];
    case a.action.startsWith("tool:"): return [`Called ${a.action.slice(5)}`, ""];
    default: return [a.action.replace(/_/g, " "), typeof d === "string" ? d : ""];
  }
}

/* ---------- shell ---------- */
const ROUTES = {
  overview: ["Overview", "What the agents found and what reached the road authorities", renderOverview],
  trips: ["Trips", "Upload a dashcam drive; the edge device and agents take it from there", renderTrips],
  review: ["Review", "Nothing is sent until you approve the exact payload", renderReview],
  reports: ["Reports", "Every defect, merged across drivers", renderReports],
  map: ["Map", "Reports on the road network", renderMap],
  conditions: ["Road condition", "Segment-level condition estimate from open defects (not a certified survey)", renderConditions],
  forecast: ["Pothole Risk Forecast", "Where cracks may turn into potholes after freeze-thaw (heuristic, not validated)", renderForecast],
  activity: ["Agent activity", "Every plan, decision and tool call, from the append-only audit log", renderActivity],
  standards: ["Service standards", "Filed reports against each authority's published service standard, with a tamper-evident history", renderStandards],
  authorities: ["Authorities", "Requests delivered to Halifax 311 and Nova Scotia Public Works", renderAuthorities],
  system: ["Trust & system", "Guardrails, privacy and the agent stack", renderSystem],
  users: ["Users", "Who can sign in, and what each role may do", renderUsers],
};

async function router() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const route = ROUTES[path] && (path !== "users" || can("admin")) ? path : "overview";
  const params = new URLSearchParams(qs || "");
  maps.forEach((m) => m.remove()); maps = [];
  timers.forEach(clearInterval); timers = [];
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === route));
  const [title, sub, render] = ROUTES[route];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  $("#crumb-current").textContent = title;
  document.title = `${title} · RoadSense`;
  if ($("#sidebar").classList.contains("open")) setDrawer(false, false);
  document.body.classList.add("loading");
  current = document.createElement("div");
  current.className = "ns-stagger";
  current.innerHTML = `<div class="card card-body sk-page" aria-busy="true" aria-label="Loading"><div class="skeleton" style="width:22%;height:22px"></div><div class="skeleton" style="width:58%;margin-top:16px"></div><div class="skeleton" style="width:46%;margin-top:10px"></div></div>
    <div class="card card-body section"><div class="skeleton" style="width:30%"></div><div class="skeleton" style="width:80%;margin-top:14px"></div><div class="skeleton" style="width:72%;margin-top:10px"></div><div class="skeleton" style="width:64%;margin-top:10px"></div></div>`;
  main.replaceChildren(current);
  const mine = current;
  try { await render(params); } catch (e) { mine.innerHTML = errorState(e); }
  if (mine === current) { document.body.classList.remove("loading"); animateView(mine); }
  refreshChrome();
}

/* ---------- motion: numbers count up, cards get a pointer spotlight, the evidence photo gets a scan line ---------- */
const STILL = matchMedia("(prefers-reduced-motion: reduce)");
function animateView(root) {
  $$(".card", root).forEach((c) => c.classList.add("ns-spot"));
  $$(".detail-photo", root).forEach((img) => {
    if (img.parentElement.classList.contains("ns-scan")) return;
    const wrap = document.createElement("div");
    wrap.className = "ns-scan";
    img.replaceWith(wrap);
    wrap.append(img);
  });
  if (STILL.matches) return;
  $$(".kpi .value, .funnel .n, .ov-num, .ov-stop .n", root).forEach((el) => {
    const node = [...el.childNodes].find((n) => n.nodeType === 3 && /\d/.test(n.textContent));
    if (!node) return;
    const text = node.textContent.trim();
    const target = Number(text.replace(/[^\d.]/g, ""));
    if (!Number.isFinite(target) || target === 0 || /[^\d,.\s%]/.test(text)) return;
    const suffix = text.endsWith("%") ? "%" : "";
    const start = performance.now(), dur = 900;
    const step = (now) => {
      const t = Math.min(1, (now - start) / dur), eased = 1 - Math.pow(1 - t, 3);
      node.textContent = num(Math.round(target * eased)) + suffix;
      if (t < 1 && node.isConnected) requestAnimationFrame(step);
    };
    node.textContent = "0" + suffix;
    requestAnimationFrame(step);
  });
}
document.addEventListener("pointermove", (e) => {
  const card = e.target.closest?.(".ns-spot");
  if (!card) return;
  const r = card.getBoundingClientRect();
  card.style.setProperty("--ns-x", `${e.clientX - r.left}px`);
  card.style.setProperty("--ns-y", `${e.clientY - r.top}px`);
}, { passive: true });

const errorState = (e) => `<div class="card empty"><span class="empty-ic" style="color:var(--red-text)">${icon("alert")}</span><b>Couldn't load this page</b><div class="small">${esc(e.message)}</div><div class="small" style="margin-top:8px">Is the agent service running? Start everything with <code>python scripts/run_all.py</code>.</div><button class="btn btn-sm" style="margin-top:12px" onclick="router()">${icon("refresh")}Retry</button></div>`;
const emptyState = (ic, title, text, action = "") => `<div class="empty"><span class="empty-ic">${icon(ic)}</span><b>${esc(title)}</b><div class="small">${text}</div>${action}</div>`;
const more = (href, label) => `<a href="${href}" class="link-more">${label}${icon("arrow")}</a>`;

async function refreshChrome() {
  try {
    const [pending, sys] = await Promise.all([api("/api/reports?status=pending_approval"), api("/api/system")]);
    $("#review-count").textContent = pending.length || "";
    // The tab title carries the queue size, so a reviewer sees new work from another tab
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = pending.length ? `(${pending.length}) ${base}` : base;
    const llm = sys.health.llm;
    const live = llm.enabled;
    $("#agent-status").innerHTML = `
      <div class="status-line"><span class="live-dot ${live ? "" : "amber"}"></span>${live ? "Agents live" : "Rule engine"}<a href="#/system" class="link-more" style="margin-left:auto" aria-label="System status">${icon("arrow")}</a></div>
      <div class="muted small" style="margin-top:6px">${live ? esc(llm.models[0].split("/").pop().replace(":free", "")) + " · " + esc(llm.mode) : "No LLM key: deterministic pipeline"}</div>
      <div class="faint small">${sys.health.mcp_tools.length} MCP tools · 4 servers</div>`;
    palette.pending = pending;
  } catch { $("#agent-status").innerHTML = `<div class="status-line"><span class="live-dot red"></span>Agent service offline</div><div class="muted small" style="margin-top:4px">Retrying every 15 s</div>`; }
}

/* ---------- Overview ---------- */
async function renderOverview() {
  const view = stage();
  const { stats: s, reports, trips, activity } = await api("/api/overview");
  const by = s.reports || {};
  const total = Object.values(by).reduce((a, b) => a + b, 0);
  const pending = by.pending_approval || 0;
  const confirmed = (s.events?.processed || 0) + (s.events?.verified || 0);
  const sent = (by.submitted || 0) + (by.fixed || 0);
  const province = s.authorities?.["Nova Scotia Public Works"] || 0;

  // 1. The queue: the one thing a reviewer does here, and where every report stands
  const order = ["pending_approval", "approved", "submitted", "delivery_failed", "fixed", "rejected"].filter((k) => by[k]);
  // The bar is a picture of the legend below it; the legend carries the links and the numbers for every reader
  const stack = order.map((k) => `<span class="ov-seg" data-s="${k}" style="flex:${by[k]}"></span>`).join("");
  const legend = order.map((k) => `<li><a href="#/reports?status=${k}" data-s="${k}"><span class="sw"></span>${esc(STATUS[k][0])}<b>${num(by[k])}</b></a></li>`).join("");
  const queue = pending
    ? `<div class="ov-num" aria-hidden="true">${num(pending)}</div>
       <h2 class="ov-lead"><span class="sr-only">${num(pending)} </span>${pending === 1 ? "report waits" : "reports wait"} for your approval</h2>
       <p class="ov-sub">Nothing reaches Halifax 311 or the province until you approve its exact payload.</p>
       <div class="ov-actions"><a class="btn btn-primary btn-lg" href="#/review">${icon("inbox")}Start reviewing</a><a class="btn btn-lg" href="#/map">${icon("map")}See them on the map</a></div>`
    : `<div class="ov-num is-clear" aria-hidden="true">0</div>
       <h2 class="ov-lead">The review queue is clear</h2>
       <p class="ov-sub">Upload a dashcam drive and the agents will bring you the next batch.</p>
       <div class="ov-actions"><a class="btn btn-primary btn-lg" href="#/trips?new=1">${icon("upload")}Upload a trip</a></div>`;

  // 2. The pipeline as a road: four stops, and what fell away between them painted on the asphalt
  const stop = (n, t, note, tone = "") => `<li class="ov-stop ${tone}"><span class="ov-pin"></span><div class="t">${t}</div><div class="n">${num(n)}</div>${note ? `<div class="note">${note}</div>` : ""}</li>`;
  const drop = (n, t) => `<li class="ov-drop ${n ? "" : "is-zero"}" aria-label="${n} ${t}"><span>${n ? "−" + num(n) : "0"}</span>${t}</li>`;

  // 3. Signals: counts that deserve a look, quiet when they are zero
  const sig = (ic, tone, n, label, hint, href) => `<li><a class="ov-sig ${n ? "" : "is-zero"}" href="${href}"><span class="ic ${tone}">${icon(ic)}</span><span class="txt"><b>${label}</b><span>${hint}</span></span><span class="n">${num(n)}</span></a></li>`;
  const signals = [
    sig("eye", "amber", s.class_disputed, "Damage type disputed", "The two vision models disagree", "#/review"),
    sig("route", "cyan", province, "Routed to the province", "100-series highways, not city 311", "#/authorities"),
    sig("merge", "blue", s.multi_driver_reports, "Seen by 2+ drivers", "Independent confirmations", "#/reports"),
    sig("wrench", "green", by.fixed || 0, "Closed by the authority", `${num(s.repair_verification?.verified)} independently verified`, "#/reports?status=fixed"),
    s.repair_verification?.reopened ? sig("alert", "red", s.repair_verification.reopened, "Reopened after closure", "Damage seen again by a later trip", "#/reports?status=fixed") : "",
    s.delivery_failed ? sig("alert", "red", s.delivery_failed, "Deliveries failing", "Retrying with back-off", "#/authorities") : "",
  ].join("");

  view.innerHTML = `
    <section class="card ov-hero">
      <div class="ov-queue">${queue}</div>
      <div class="ov-status">
        <div class="ov-k">${num(total)} reports by status</div>
        <div class="ov-stack" aria-hidden="true">${stack || '<span class="ov-seg empty"></span>'}</div>
        <ul class="ov-legend">${legend}</ul>
      </div>
    </section>
    <section class="card ov-road section" aria-labelledby="ov-road-h">
      <div class="card-head"><h2 id="ov-road-h">From dashcam to the right desk</h2><span class="spacer"></span>
        <span class="desc">${num(s.trips)} trips · ${num(s.llm_calls)} LLM calls · ${num(s.llm_tokens)} tokens · ${pct(s.fallback_rate)} decided by the rule engine</span></div>
      <ol class="ov-lane">
        ${stop(s.detections, "Spotted on the dashcam", "Edge model, faces and plates blurred")}
        ${drop(s.false_alarms_removed, "false alarms dropped")}
        ${stop(confirmed, "Confirmed by a second model", "RT-DETR-L checks every crop; AI judges borderline ones")}
        ${drop(s.duplicates_merged, "merged as duplicates")}
        ${stop(total, "Unique reports", pending ? `${num(pending)} waiting for you` : "None waiting", pending ? "is-hot" : "")}
        ${drop(total - sent, "not sent yet")}
        ${stop(sent, "Sent to an authority", province ? `${num(province)} to the province` : "Via Open311")}
      </ol>
    </section>
    ${impactCard(s.impact)}
    ${repairCard(s.repair_verification)}
    <div class="ov-row section">
      <section class="card ov-map"><div class="card-head"><h2>Where the damage is</h2><span class="spacer"></span>${more("#/map", "Open full map")}</div><div id="mini-map" class="map-mini"></div></section>
      <section class="card"><div class="card-head"><h2>Worth a look</h2></div><ul class="ov-sigs">${signals}</ul></section>
    </div>
    <div class="ov-row section">
      <section class="card"><div class="card-head"><h2>Latest decisions</h2><span class="spacer"></span>${more("#/activity", "Full audit log")}</div>
        ${activity.length ? `<ol class="ov-timeline">${groupActivity(activity).slice(0, 6).map(timelineItem).join("")}</ol>` : emptyState("sparkle", "No decisions yet", "Upload a trip to see the agents work.")}</section>
      <section class="card"><div class="card-head"><h2>Recent trips</h2><span class="spacer"></span>${more("#/trips", "All trips")}</div>
        ${trips.length ? `<ul class="ov-trips">${trips.map(tripItem).join("")}</ul>` : emptyState("video", "No trips yet", "Upload a dashcam video with its GPS track.", `<a class="btn btn-primary" style="margin-top:12px" href="#/trips?new=1">${icon("upload")}New trip</a>`)}</section>
    </div>`;
  drawMap("mini-map", reports, { zoom: 14, interactive: false });
}

/* What the pipeline saved the city: only work that never reached a person is counted. Assumptions shown inline. */
function impactCard(m) {
  if (!m) return "";
  const a = m.assumptions || {};
  const hours = m.staff_minutes_saved / 60;
  const tile = (ic, tone, value, label, hint) => `<div class="card kpi"><div class="label">${label}<span class="ic ${tone}">${icon(ic)}</span></div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  return `<section class="section" aria-labelledby="ov-impact-h">
    <div class="card-head" style="padding-inline:0"><h2 id="ov-impact-h">What this saved the city</h2><span class="spacer"></span>
      <span class="desc">At $${esc(a.cost_per_contact)} and ${esc(a.minutes_per_contact)} min per 311 contact · only work that never reached a person is counted</span></div>
    <div class="grid kpis">
      ${tile("merge", "blue", num(m.duplicate_contacts_avoided), "Duplicate reports avoided", "Repeat sightings merged, never re-filed")}
      ${tile("route", "cyan", num(m.routed_to_province), "Sent to the right desk first", "Provincial roads skip city 311")}
      ${tile("eye", "amber", num(m.false_alarms_filtered), "False alarms filtered", "Before any person looked")}
      ${tile("activity", "green", hours >= 1 ? hours.toFixed(1) + " h" : num(Math.round(m.staff_minutes_saved)) + " min", "311 staff time saved", `$${num(Math.round(m.intake_cost_avoided))} intake cost avoided`)}
    </div></section>`;
}

// Consecutive decisions of the same kind by the same actor read as one line: "Rejected by a reviewer · 4 reports"
function groupActivity(list) {
  const out = [];
  for (const a of list) {
    const last = out[out.length - 1];
    if (last && last.action === a.action && last.actor === a.actor && !a.action.startsWith("trip_")) last.items.push(a);
    else out.push({ ...a, items: [a] });
  }
  return out;
}

function timelineItem(g) {
  const [who] = actor(g.actor);
  const [what, why] = describe(g);
  const ids = g.items.map((a) => a.subject?.startsWith("report:") ? a.subject.slice(7) : null).filter(Boolean);
  const many = g.items.length > 1;
  // One report: the title is the link. A group: a chip per report it touched.
  const title = !many && ids.length ? `<a class="what" href="#/reports?id=${ids[0]}">${esc(what)}${what.includes(`#${ids[0]}`) ? "" : ` · #${ids[0]}`}</a>` : `<span class="what">${esc(what)}</span>`;
  const chips = many ? ids.map((id) => `<a class="ov-chip" href="#/reports?id=${id}">#${id}</a>`).join("") : "";
  return `<li>${avatar(g.actor)}<div class="body"><div class="head">${title}${many ? `<span class="x">×${g.items.length}</span>` : ""}<span class="when">${ago(g.ts)}</span></div>
    <div class="who">${esc(who)}</div>${!many && why ? `<div class="why">${esc(why)}</div>` : ""}${chips ? `<div class="chips">${chips}</div>` : ""}</div></li>`;
}

function tripItem(t) {
  const run = t.run || {};
  const agents = run.mode?.startsWith("agents");
  const waiting = !run.mode && t.processed + t.rejected < t.events;
  const state = agents ? `<span class="pill violet">${icon("sparkle")}Agents</span>` : run.mode ? `<span class="pill">${icon("cpu")}Rule engine</span>`
    : waiting ? `<button class="btn btn-sm" data-process="${esc(t.trip_id)}">${icon("refresh")}Process now</button>` : `<span class="pill green">${icon("check")}Done</span>`;
  const share = t.events ? Math.round((t.processed / t.events) * 100) : 0;
  return `<li><span class="ic">${icon("video")}</span><div class="body"><div class="id mono">${esc(t.trip_id)}</div>
    <div class="meta">${num(t.events)} detections · ${num(t.processed)} confirmed${t.rejected ? ` · ${num(t.rejected)} dropped` : ""} · ${ago(t.started)}</div>
    <div class="ov-meter" aria-hidden="true"><i style="width:${share}%"></i></div></div>${state}</li>`;
}

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-process]");
  if (!b) return;
  b.disabled = true;
  try { await api(`/api/trips/${encodeURIComponent(b.dataset.process)}/process`, { method: "POST" }); toast("Agents started on this trip"); }
  catch (e) { toast(e.message, false); }
});

function tripsTable(trips) {
  if (!trips.length) return emptyState("video", "No trips yet", "Upload a dashcam video with its GPS track.", `<a class="btn btn-primary" style="margin-top:12px" href="#/trips?new=1">${icon("upload")}New trip</a>`);
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Trip</th><th>Detections</th><th>Confirmed</th><th>False alarms</th><th>Run</th><th>When</th></tr></thead><tbody>
    ${trips.map((t) => { const run = t.run || {}; const llm = run.llm || {};
      return `<tr><td><span class="row" style="gap:10px"><span class="avatar pill">${icon("video")}</span><span class="strong mono">${esc(t.trip_id)}</span></span></td><td>${num(t.events)}</td><td>${num(t.processed)}</td><td>${num(t.rejected)}</td>
      <td>${run.mode?.startsWith("agents") ? `<span class="pill violet">${icon("sparkle")}Agents</span> <span class="muted small">${llm.calls || 0} calls · ${run.seconds || "?"} s</span>` : run.mode ? `<span class="pill">${icon("cpu")}Rule engine</span>` : t.processed + t.rejected < t.events ? `<button class="btn btn-sm" data-process="${esc(t.trip_id)}">${icon("refresh")}Process now</button>` : `<span class="pill green">${icon("check")}Done</span>`}</td>
      <td class="muted nowrap">${ago(t.started)}</td></tr>`; }).join("")}</tbody></table></div>`;
}

/* ---------- Map ---------- */
const MAP_COLORS = { pending_approval: "#ffb547", approved: "#7ab8ff", submitted: "#b89cff", fixed: "#5bd18b", rejected: "#8a929e" };
function drawMap(id, reports, { zoom = 15, interactive = true } = {}) {
  const el = document.getElementById(id);
  if (!el || !window.L) return;
  const center = reports.length ? [reports[0].lat, reports[0].lon] : [44.6425, -63.578];
  const m = L.map(el, { zoomControl: interactive, scrollWheelZoom: interactive, attributionControl: true }).setView(center, zoom);
  // Esri canvas basemaps: muted greys so the status markers carry the colour. Dark Gray for Night Shift, Light Gray
  // for Daylight, each with its own street-label layer on top.
  const tone = document.documentElement.dataset.theme === "light" ? "Light" : "Dark";
  const esri = (layer) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${tone}_Gray_${layer}/MapServer/tile/{z}/{y}/{x}`;
  L.tileLayer(esri("Base"), { maxZoom: 19, maxNativeZoom: 16, attribution: "Tiles © Esri, HERE, Garmin, © OpenStreetMap contributors" }).addTo(m);
  L.tileLayer(esri("Reference"), { maxZoom: 19, maxNativeZoom: 16 }).addTo(m);
  const outline = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#fff";
  const single = (r) => L.circleMarker([r.lat, r.lon], { radius: 6 + Math.min(r.confirmations, 4) * 2, color: outline,
    weight: 2, fillColor: MAP_COLORS[r.status] || "#667085", fillOpacity: .95, className: r.status === "pending_approval" ? "ns-pending" : "" })
    .bindPopup(`<b>#${r.id} ${esc(r.class_name)}</b><br>${esc(r.address || "")}<br>${esc(authority(r.jurisdiction))} · ${esc(STATUS[r.status]?.[0] || r.status)}<br>${r.confirmations} driver(s)<br><a href="#/reports?id=${r.id}">Open report</a>`);
  const layer = L.layerGroup().addTo(m);
  // Reports closer than CLUSTER_PX on screen merge into one circle sized by count; it splits again as you zoom in.
  const draw = () => {
    layer.clearLayers();
    for (const group of clusterReports(m, reports)) {
      if (group.length === 1) { layer.addLayer(single(group[0])); continue; }
      layer.addLayer(clusterMarker(m, group));
    }
  };
  m.on("zoomend", draw);
  const pts = reports.map((r) => [r.lat, r.lon]);
  if (pts.length > 1) m.fitBounds(pts, { padding: [30, 30], maxZoom: 16 });
  draw();
  maps.push(m);
  setTimeout(() => m.invalidateSize(), 50);
}

const CLUSTER_PX = 44, CLUSTER_OFF_ZOOM = 18;
function clusterReports(m, reports) {
  if (m.getZoom() >= CLUSTER_OFF_ZOOM) return reports.map((r) => [r]);
  const groups = [];
  for (const r of reports) {
    const p = m.project([r.lat, r.lon]);
    const near = groups.find((g) => g.at.distanceTo(p) <= CLUSTER_PX);
    if (near) { near.items.push(r); near.at = near.at.multiplyBy(near.items.length - 1).add(p).divideBy(near.items.length); }
    else groups.push({ at: p, items: [r] });
  }
  return groups.map((g) => g.items);
}

/* The cluster takes the most urgent status inside it, so a single report awaiting review is never hidden. */
const URGENCY = ["delivery_failed", "pending_approval", "approved", "submitted", "fixed", "rejected"];
function clusterMarker(m, group) {
  const lat = group.reduce((a, r) => a + r.lat, 0) / group.length, lon = group.reduce((a, r) => a + r.lon, 0) / group.length;
  const status = URGENCY.find((s) => group.some((r) => r.status === s)) || group[0].status;
  const size = Math.round(30 + Math.min(Math.sqrt(group.length) * 7, 34));
  const pending = group.filter((r) => r.status === "pending_approval").length;
  const label = `${group.length} reports${pending ? `, ${pending} awaiting review` : ""}`;
  const icon = L.divIcon({ className: "ns-cluster-wrap", iconSize: [size, size],
    html: `<span class="ns-cluster ${status === "pending_approval" ? "is-pending" : ""}" style="--c:${MAP_COLORS[status] || "#667085"};width:${size}px;height:${size}px" aria-label="${esc(label)}">${group.length}</span>` });
  const marker = L.marker([lat, lon], { icon, title: label, keyboard: true, riseOnHover: true });
  marker.on("click", () => {
    const bounds = L.latLngBounds(group.map((r) => [r.lat, r.lon]));
    if (bounds.getNorthEast().equals(bounds.getSouthWest())) m.setView(bounds.getCenter(), CLUSTER_OFF_ZOOM);
    else m.fitBounds(bounds, { padding: [48, 48], maxZoom: CLUSTER_OFF_ZOOM });
  });
  return marker;
}

async function renderMap(params) {
  const view = stage();
  const all = await api("/api/reports");
  const filter = params.get("status") || "all";
  const list = filter === "all" ? all : all.filter((r) => r.status === filter);
  const count = (s) => s === "all" ? all.length : all.filter((r) => r.status === s).length;
  view.innerHTML = `
    <div class="toolbar">
      <div class="tabs" role="group" aria-label="Show reports by status">${["all", "pending_approval", "submitted", "fixed"].map((s) => `<button type="button" aria-pressed="${s === filter}" class="${s === filter ? "on" : ""}" data-s="${s}">${s === "all" ? "All" : STATUS[s][0]}<span class="n">${count(s)}</span></button>`).join("")}</div>
    </div>
    <div class="card"><div class="card-head"><div class="legend">${Object.entries(MAP_COLORS).map(([k, c]) => `<span><i style="background:${c}"></i>${STATUS[k][0]}</span>`).join("")}</div><span class="spacer"></span><span class="desc">Marker size = number of drivers</span></div><div id="big-map" class="map"></div></div>`;
  $$(".tabs button").forEach((b) => b.onclick = () => location.hash = `#/map?status=${b.dataset.s}`);
  drawMap("big-map", list);
}

/* ---------- Reports ---------- */
async function renderReports(params) {
  const view = stage();
  const id = params.get("id");
  if (id) return renderReportPage(id);
  const filter = params.get("status") || "all";
  const [all, trips] = await Promise.all([api("/api/reports"), api("/api/trips").catch(() => [])]);
  const list = filter === "all" ? all : all.filter((r) => r.status === filter);
  const count = (s) => all.filter((r) => r.status === s).length;
  // Group by the trip that first saw each defect, newest trip first
  const started = Object.fromEntries(trips.map((t) => [t.trip_id, t.started]));
  const groups = Object.entries(list.reduce((g, r) => ((g[r.trip_id || "unknown"] ||= []).push(r), g), {}))
    .sort(([a], [b]) => String(started[b] || "").localeCompare(String(started[a] || "")) || b.localeCompare(a));
  const tally = (rs) => Object.entries(rs.reduce((m, r) => ((m[r.status] = (m[r.status] || 0) + 1), m), {}))
    .map(([s, n]) => `<span class="tally" data-s="${s}"><i></i>${n} ${esc((STATUS[s]?.[0] || s).toLowerCase())}</span>`).join("");
  const row = (r) => `<tr class="click" tabindex="0" data-id="${r.id}" data-q="${esc(`${r.class_name} ${r.address || ""} #${r.id} ${authority(r.jurisdiction)} ${r.trip_id || ""}`.toLowerCase())}"><td class="c-thumb"><img class="thumb" loading="lazy" src="/api/media/${encodeURIComponent(r.best_event_id)}.jpg?crop=1" alt=""></td>
        <td class="c-name"><span class="nowrap"><span class="strong">${esc(r.class_name)}</span> <span class="faint small mono">#${r.id}</span></span><div class="chip-row" style="margin-top:4px">${flags(r)}</div></td>
        <td class="c-loc">${esc(r.address || "")}</td><td class="c-auth nowrap">${esc(authority(r.jurisdiction))}</td><td class="c-sev">${sevPill(r.severity)}</td>
        <td class="c-drv" data-label="Drivers">${r.confirmations}</td><td class="c-pri mono" data-label="Priority">${r.priority}</td><td class="c-status">${statusPill(r.status)}</td><td class="c-go">${icon("arrow", "row-arrow")}</td></tr>`;
  const group = ([trip, rs]) => `<tbody class="trip-group" data-trip="${esc(trip)}">
      <tr class="group-row"><th colspan="9" scope="rowgroup"><button type="button" class="group-toggle" aria-expanded="true">
        ${icon("arrow", "chev")}<span class="avatar pill">${icon("video")}</span><span class="g-title"><b class="mono">${esc(trip)}</b><span class="g-meta">${started[trip] ? ago(started[trip]) + " · " : ""}<span class="g-n">${rs.length}</span> report${rs.length === 1 ? "" : "s"}</span></span>
        <span class="g-tally">${tally(rs)}</span></button></th></tr>
      ${rs.map(row).join("")}</tbody>`;
  view.innerHTML = `
    <div class="toolbar">
      <div class="tabs" role="group" aria-label="Show reports by status">${["all", "pending_approval", "submitted", "fixed", "rejected"].map((s) => `<button type="button" aria-pressed="${s === filter}" class="${s === filter ? "on" : ""}" data-s="${s}">${s === "all" ? "All" : STATUS[s][0]}<span class="n">${s === "all" ? all.length : count(s)}</span></button>`).join("")}</div>
      <span class="spacer"></span>
      <label class="input-icon">${icon("search")}<span class="sr-only">Filter reports</span><input class="input" id="report-q" type="search" placeholder="Filter by damage, street or #id" value="${esc(params.get("q") || "")}"><span class="kbd input-kbd" aria-hidden="true">/</span></label>
    </div>
    <div class="card card-body flush">${list.length ? `<div class="table-wrap"><table class="table"><thead><tr><th></th><th>Damage</th><th>Location</th><th>Authority</th><th>Severity</th><th>Drivers</th><th>Priority</th><th>Status</th><th></th></tr></thead>
      ${groups.map(group).join("")}</table></div>
        <div class="card-foot" id="report-foot">${list.length} report${list.length === 1 ? "" : "s"} from ${groups.length} trip${groups.length === 1 ? "" : "s"}</div>`
      : emptyState("inbox", "No reports here", "Reports appear after a trip is processed.")}</div>`;
  $$(".tabs button").forEach((b) => b.onclick = () => {
    const term = $("#report-q")?.value.trim();
    location.hash = `#/reports?status=${b.dataset.s}${term ? "&q=" + encodeURIComponent(term) : ""}`;
  });
  $$("tr.click").forEach((tr) => {
    tr.onclick = () => location.hash = `#/reports?id=${tr.dataset.id}`;
    tr.onkeydown = (e) => { if (e.key === "Enter") tr.onclick(); };
  });
  // A collapsed trip hides its rows; search still looks inside it and reopens a group that has a match
  const apply = () => {
    const term = $("#report-q").value.trim().toLowerCase();
    let shown = 0, tripsShown = 0;
    $$(".trip-group").forEach((g) => {
      const btn = $(".group-toggle", g), open = btn.getAttribute("aria-expanded") === "true";
      let hits = 0;
      $$("tr.click", g).forEach((tr) => { const hit = !term || tr.dataset.q.includes(term); hits += hit; tr.hidden = !hit || (!open && !term); });
      g.hidden = !hits;
      $(".g-n", g).textContent = term ? `${hits} of ${$$("tr.click", g).length}` : $$("tr.click", g).length;
      shown += hits; tripsShown += !!hits;
    });
    if ($("#report-foot")) $("#report-foot").textContent = term ? `${shown} of ${list.length} reports, in ${tripsShown} trip${tripsShown === 1 ? "" : "s"}` : `${list.length} report${list.length === 1 ? "" : "s"} from ${groups.length} trip${groups.length === 1 ? "" : "s"}`;
  };
  $$(".group-toggle").forEach((b) => b.onclick = () => { b.setAttribute("aria-expanded", b.getAttribute("aria-expanded") !== "true"); apply(); });
  // The filter lives in the URL, so Back from a report returns to the same filtered list
  const q = $("#report-q");
  q.oninput = () => {
    apply();
    const qs = new URLSearchParams({ ...(filter !== "all" ? { status: filter } : {}), ...(q.value.trim() ? { q: q.value.trim() } : {}) }).toString();
    history.replaceState(null, "", `#/reports${qs ? "?" + qs : ""}`);
  };
  if (q.value) apply();
}

const flags = (r) => [
  r.class_disputed ? `<span class="pill amber">Class disputed</span>` : "",
  r.confirmations > 1 ? `<span class="pill blue">${icon("merge")}${r.confirmations} drivers</span>` : "",
].join("");

async function renderReportPage(id) {
  const view = stage();
  const d = await api(`/api/reports/${id}`);
  $("#crumb-current").textContent = `Reports / #${d.report.id}`;
  view.innerHTML = `<a href="#/reports" class="btn btn-ghost btn-sm" style="margin-left:-10px"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>All reports</a><div class="card section">${reportDetail(d)}</div>`;
  bindDetail(d, () => renderReportPage(id));
}

/* ---------- Report detail (shared by Review and Reports) ---------- */
function reportDetail(d) {
  const r = d.report, e = d.events.find((x) => x.id === r.best_event_id) || d.events[0] || {};
  const p = d.payload;
  const trail = d.trail.filter((a) => /^(verdict_|report_|status_|verify|recheck|payload|repair_(awaiting|verified|not_verified))/.test(a.action));
  const pending = r.status === "pending_approval";
  const review = can("review");
  const failed = r.status === "delivery_failed" || (r.status === "approved" && r.last_error);
  const trend = d.deterioration || {};
  const city = (d.external || []).filter((x) => x.source === "city_sr");
  const feeds = (d.external || []).filter((x) => x.source !== "city_sr");
  return `
    <div class="card-head"><h2>${esc(r.class_name)} <span class="muted">#${r.id}</span></h2>${statusPill(r.status)}${verifPill(r.verification_status)}<span class="spacer"></span><span class="muted small">${esc(r.address || "")}</span></div>
    <div class="card-body detail">
      <div class="dgrid">
        <div><button type="button" class="photo-btn" data-zoom="${esc(`${r.class_name} #${r.id} · ${r.address || ""}`)}" aria-label="Enlarge the photo of ${esc(r.class_name)} #${r.id}">
            <img class="detail-photo" src="/api/media/${encodeURIComponent(r.best_event_id)}.jpg" alt="Privacy-blurred frame of the damage"><span class="photo-hint">${icon("zoom")}Enlarge</span></button>
          <div class="small muted" style="margin-top:6px">${icon("shield", "")} Everything except the damage was blurred on the device before upload.</div></div>
        <div class="dside">
          ${r.class_disputed ? `<div class="callout amber">${icon("alert")}<div><b>The two models disagree on the damage type.</b> YOLOv8m's label is kept; please check the photo.</div></div>` : ""}
          
          ${r.confirmations > 1 ? `<div class="callout blue">${icon("merge")}<div><b>Seen by ${r.confirmations} independent drivers.</b> Repeat sightings were merged instead of filed again.</div></div>` : ""}
          ${city.length ? `<div class="callout amber">${icon("building")}<div><b>The city already has a request here</b> (${city.map((x) => esc(x.id.replace(/^city:/, "#"))).join(", ")}${city[0].status ? ", " + esc(city[0].status) : ""}). Approving files a duplicate unless it adds new evidence.</div></div>` : ""}
          ${feeds.length ? `<div class="callout blue">${icon("layers")}<div><b>Corroborated by ${feeds.length} third-party sighting(s)</b>: ${[...new Set(feeds.map((x) => esc(x.source)))].join(", ")}.</div></div>` : ""}
          ${failed ? `<div class="callout red">${icon("alert")}<div><b>Delivery ${r.status === "delivery_failed" ? "failed" : "will be retried"}</b> after ${r.delivery_attempts} attempt(s): ${esc(r.last_error || "")}${r.next_attempt_at ? `<div class="small">Next automatic attempt at ${esc(new Date(r.next_attempt_at).toLocaleTimeString())}</div>` : ""}${review ? `<div style="margin-top:8px"><button class="btn btn-sm" id="resend">${icon("refresh")}Resend now</button></div>` : ""}</div></div>` : ""}
          <div class="facts">
            <div class="fact"><div class="k">Severity</div><div class="v">${sevPill(r.severity)}</div></div>
            <div class="fact"><div class="k">Est. width</div><div class="v">${r.class_code === "D40" && r.width_m ? Math.round(r.width_m * 100) + " cm" : r.class_code === "D40" ? "–" : "n/a for cracks"}</div></div>
            <div class="fact"><div class="k">Edge confidence</div><div class="v">${e.confidence != null ? e.confidence.toFixed(2) : "–"}</div></div>
            <div class="fact"><div class="k">Verifier confidence</div><div class="v">${e.verified_confidence != null ? e.verified_confidence.toFixed(2) : e.recheck_confidence != null ? e.recheck_confidence.toFixed(2) + " (close-up)" : "–"}</div></div>
            <div class="fact"><div class="k">Position</div><div class="v">${e.position_uncertainty_m ? "± " + e.position_uncertainty_m + " m" : "car position"}</div></div>
            <div class="fact"><div class="k">Goes to</div><div class="v">${esc(authority(r.jurisdiction))}</div></div>
            <div class="fact"><div class="k">Severity band</div><div class="v">${r.severity_band ? `<span class="pill mono">${esc(r.severity_band)}</span>` : "–"}</div></div>
            <div class="fact"><div class="k">Repair target</div><div class="v">${r.repair_target_days ? `${r.repair_target_days} days` : "–"}</div></div>
            <div class="fact"><div class="k">Trend</div><div class="v">${trend.passes > 1 ? `${esc(trend.trend)} <span class="muted small">(${trend.passes} passes)</span>` : `<span class="muted">${trend.passes || 0} pass</span>`}</div></div>
            ${r.approved_by ? `<div class="fact"><div class="k">Approved by</div><div class="v small">${esc(r.approved_by)}</div></div>` : ""}
          </div>
          ${pending && review ? `<div class="reclass"><label class="small muted" for="reclass">Wrong damage type?</label><select id="reclass" class="input input-sm">${Object.entries(CLASS_LABELS).map(([c, n]) => `<option value="${c}" ${c === r.class_code ? "selected" : ""}>${esc(n)} (${c})</option>`).join("")}</select><button class="btn btn-sm" id="reclass-save" disabled>Correct type</button></div>` : ""}
        </div>
      </div>
      <div class="dgrid dgrid-even">
        <div><h3 class="h3">Why the agents decided this</h3>
          ${trail.length ? `<ul class="timeline">${trail.map((a) => { const [what, why] = describe(a); return `<li>${avatar(a.actor)}<div class="who">${esc(what)} <span class="muted small">· ${esc(actor(a.actor)[0])} · ${ago(a.ts)}</span></div>${why ? `<div class="say">${esc(why).slice(0, 400)}</div>` : ""}</li>`; }).join("")}</ul>` : `<div class="muted">No decisions recorded.</div>`}
          ${r.rationale ? `<div class="callout blue" style="margin-top:4px">${icon("route")}<div><b>Routing:</b> ${esc(r.rationale)}</div></div>` : ""}
        </div>
        <div><h3 class="h3">${pending ? "What you are approving" : "What the authority received"}</h3>
          <div class="payload"><dl>
            <dt>To</dt><dd><b>${esc(p.authority)}</b> <span class="pill">${esc(CHANNELS[p.channel] || p.channel)}</span></dd>
            <dt>Service</dt><dd class="mono">${esc(p.service_code)}</dd>
            <dt>Location</dt><dd>${esc(p.address_string)}<div class="muted mono">${p.lat}, ${p.long}</div></dd>
            <dt>Description</dt><dd>${esc(p.description)}</dd>
            <dt>Photo</dt><dd><span class="muted">Privacy-blurred frame · signed link, valid ${esc(String(Math.max(0, Math.round((Number(new URL(p.media_url).searchParams.get("exp") || 0) * 1000 - Date.now()) / 864e5))))} more days</span></dd>
            <dt>Attributes</dt><dd>${esc(p.attributes.severity)} severity · ${p.attributes.confirmations} confirmation(s)</dd>
          </dl><div class="hash">${icon("lock")}<span>SHA-256 <code>${esc(d.payload_hash.slice(0, 16))}…</code> ${pending ? "is locked when you approve: any later change blocks sending." : r.service_request_id ? `· request <b>${esc(r.service_request_id)}</b>` : ""}</span></div></div>
        </div>
      </div>
    </div>
    ${verificationCard(d.verification)}
    ${(d.deliveries || []).length ? `<div class="card-body" style="padding-top:0"><h3 class="h3">Deliveries</h3><ul class="feed">${d.deliveries.map((x) => `<li>${avatar(x.ok ? "agent:filing" : "fallback")}<div style="flex:1;min-width:0"><span class="what">${esc(x.kind === "update" ? "Update" : "Submission")} via ${esc(CHANNELS[x.channel] || x.channel || "?")}</span> ${x.ok ? `<span class="pill green">ok</span>` : `<span class="pill red">failed</span>`}<div class="why">${esc(x.detail || "")}</div></div><span class="when">${ago(x.ts)}</span></li>`).join("")}</ul></div>` : ""}
    ${pending && review ? `<div class="actionbar"><span class="review-pos" id="review-pos" hidden></span><span class="muted small"><span class="kbd">J</span>/<span class="kbd">K</span> next/previous. Sending always needs a click.</span><span class="spacer"></span>
      <button class="btn btn-danger" id="reject">${icon("x")}Reject</button><button class="btn btn-primary btn-lg" id="approve">${icon("send")}Approve &amp; send to ${esc(authority(r.jurisdiction))}</button></div>` : ""}
    ${pending && !review ? `<div class="actionbar"><span class="muted small">${icon("lock")} Only reviewers can approve or reject reports. You are signed in as ${esc(session.user?.role || "")}.</span></div>` : ""}`;
}

/* Before and after: the original detection next to the latest re-observation, and why the repair was (not) verified */
function verificationCard(v) {
  if (!v) return "";
  const img = (ref) => {
    if (!ref) return "";
    const i = ref.indexOf(":"), kind = ref.slice(0, i), id = ref.slice(i + 1);
    return kind === "pass" ? `/api/media/pass/${encodeURIComponent(id)}.jpg` : `/api/media/${encodeURIComponent(id)}.jpg`;
  };
  const when = (ts) => ts ? esc(new Date(ts).toLocaleString()) : "–";
  const where = (o) => o && o.lat != null ? `<span class="mono">${o.lat.toFixed(5)}, ${o.lon.toFixed(5)}</span>` : `<span class="muted">not kept</span>`;
  const o = v.original || {}, l = v.latest;
  const side = (title, src, alt, rows) => `<figure class="ba-side"><figcaption class="h3">${title}</figcaption>
      ${src ? `<button type="button" class="photo-btn" data-zoom="${esc(alt)}" aria-label="Enlarge: ${esc(alt)}"><img class="detail-photo" src="${src}" alt="${esc(alt)}"><span class="photo-hint">${icon("zoom")}Enlarge</span></button>`
            : `<div class="ba-empty muted">${icon("clock")}No trip has passed this spot since the closure</div>`}
      <dl class="ba-facts">${rows.map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join("")}</dl></figure>`;
  const tone = { verified: "green", reopened: "red", awaiting: "amber" }[v.state] || "blue";
  const head = { verified: "The repair was independently verified", reopened: "The damage is still there: RoadSense reopened this report",
    awaiting: l ? "Not verified yet: the evidence so far is inconclusive" : "Waiting for a vehicle to pass the spot again" }[v.state];
  const hist = v.history || [];
  const pill = (res) => `<span class="pill ${(RESULT[res] || [])[1] || ""}">${esc((RESULT[res] || [res])[0])}</span>`;
  return `<div class="card-body" style="padding-top:0"><h3 class="h3">Independent repair verification</h3>
    <div class="callout ${tone}">${icon(v.state === "reopened" ? "alert" : v.state === "verified" ? "wrench" : "clock")}<div><b>${esc(head)}.</b> ${esc(v.reason || "")}
      <div class="small muted">The authority's status stays “${esc((STATUS[v.authority_status] || [v.authority_status])[0])}” (closed ${when(v.closed_at)}); this check is RoadSense's own.</div></div></div>
    <div class="ba">
      ${side("Original detection", img(o.image), "Original detection", [["Detected", when(o.captured_at)], ["Location", where(o)], ["Class", esc(CLASS_LABELS[o.class_code] || o.class_code || "–")], ["Confidence", o.confidence != null ? o.confidence.toFixed(2) : "–"]])}
      ${side("Latest follow-up", l ? img(l.image) : "", "Latest follow-up observation", l ? [["Observed", when(l.observed_at)], ["Location", where(l)], ["Distance", l.distance_m != null ? `${l.distance_m} m from the original` : "–"], ["Result", pill(l.result)]] : [["Result", "–"]])}
    </div>
    ${hist.length ? `<div class="ba-hist"><table class="table"><thead><tr><th>When</th><th>Observation</th><th>Distance</th><th>Seen</th><th>Result</th><th>Why</th></tr></thead><tbody>${hist.map((h) => `<tr><td class="small">${ago(h.verified_at)}</td><td class="small">${esc(h.observation_kind === "pass" ? "Clean-pass frame" : "Detection")} · trip ${esc(h.trip_id || "")}</td><td class="small">${h.distance_m != null ? h.distance_m + " m" : "–"}</td><td class="small">${h.observed_class ? esc(h.observed_class) + (h.confidence != null ? " " + h.confidence.toFixed(2) : "") : "nothing"}</td><td>${pill(h.result)}</td><td class="small">${esc(h.reason)}<div class="muted mono" title="SHA-256 of the evidence">${esc(h.evidence_hash.slice(0, 12))}…</div></td></tr>`).join("")}</tbody></table></div>` : ""}
  </div>`;
}

/* Repair Verification Rate: verified / conclusively re-observed. Not-yet-seen repairs are not failures. */
function repairCard(m) {
  if (!m || !m.closed_by_authority) return "";
  const tile = (ic, tone, value, label, hint) => `<div class="card kpi"><div class="label">${label}<span class="ic ${tone}">${icon(ic)}</span></div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  return `<section class="section" aria-labelledby="ov-repair-h">
    <div class="card-head" style="padding-inline:0"><h2 id="ov-repair-h">Are closed repairs really fixed?</h2><span class="spacer"></span>
      <span class="desc">Rate = verified ÷ conclusively re-observed (verified + reopened) · repairs nobody has driven past yet are not counted as failures</span></div>
    <div class="grid kpis">
      ${tile("wrench", "green", m.verification_rate == null ? "–" : `${m.verification_rate}%`, "Repair verification rate", `${num(m.verified)} of ${num(m.eligible)} conclusively re-observed`)}
      ${tile("clock", "amber", num(m.awaiting_verification), "Awaiting verification", `${num(m.not_yet_reobserved)} not re-observed yet · ${num(m.awaiting_inconclusive)} inconclusive so far`)}
      ${tile("wrench", "green", num(m.verified), "Repairs verified", "Clean passes from different trips")}
      ${tile("alert", "red", num(m.not_verified), "Not verified · reopened", "The damage was detected again")}
      ${tile("eye", "blue", num(m.inconclusive_verifications), "Inconclusive checks", "Blurred, dark, out of view or a weak detection")}
    </div></section>`;
}

function bindDetail(d, after) {
  const id = d.report.id;
  const approve = $("#approve"), reject = $("#reject");
  if (approve) approve.onclick = async () => {
    approve.disabled = true; reject && (reject.disabled = true); approve.innerHTML = `${icon("refresh", "spin")}Sending…`;
    try {
      const job = await api(`/api/reports/${id}/approve`, { method: "POST", body: JSON.stringify({ payload_hash: d.payload_hash }) });
      const res = job.result || {};
      if (res.service_request_id) toast(`Sent to ${authority(res.authority)}: request ${res.service_request_id}`);
      else toast(`Not sent: ${res.error || job.error || "unknown error"}`, false);
    } catch (e) { toast(e.message, false); }
    after(); refreshChrome();
  };
  // Rejecting also suppresses every repeat sighting, so it takes a second click; the arm expires after 4 s
  if (reject) reject.onclick = async () => {
    if (!reject.classList.contains("is-armed")) {
      reject.classList.add("is-armed");
      reject.innerHTML = `${icon("x")}Click again to reject`;
      clearTimeout(reject.t);
      reject.t = setTimeout(() => { reject.classList.remove("is-armed"); reject.innerHTML = `${icon("x")}Reject`; }, 4000);
      return;
    }
    clearTimeout(reject.t);
    reject.disabled = true; approve && (approve.disabled = true);
    try { await api(`/api/reports/${id}/reject`, { method: "POST" }); toast("Rejected: it will not be sent, and repeat sightings are suppressed."); } catch (e) { toast(e.message, false); }
    after(); refreshChrome();
  };
  const sel = $("#reclass"), save = $("#reclass-save");
  if (sel && save) {
    sel.onchange = () => { save.disabled = sel.value === d.report.class_code; };
    save.onclick = async () => {
      save.disabled = true;
      try { await api(`/api/reports/${id}/class`, { method: "POST", body: JSON.stringify({ class_code: sel.value }) }); toast(`Damage type set to ${CLASS_LABELS[sel.value]}. Review the payload again.`); }
      catch (e) { toast(e.message, false); }
      after(true);
    };
  }
  const resend = $("#resend");
  if (resend) resend.onclick = async () => {
    resend.disabled = true; resend.innerHTML = `${icon("refresh", "spin")}Sending…`;
    try {
      const job = await api(`/api/reports/${id}/resend`, { method: "POST" });
      const res = job.result || {};
      if (res.service_request_id) toast(`Sent: request ${res.service_request_id}`); else toast(`Still failing: ${res.error || job.error || "unknown error"}`, false);
    } catch (e) { toast(e.message, false); }
    after(true);
  };
}

/* ---------- Review inbox ---------- */
async function renderReview(params) {
  const view = stage();
  const pending = await api("/api/reports?status=pending_approval");
  if (!pending.length) {
    view.innerHTML = `<div class="card">${emptyState("check", "You're all caught up", "New reports appear here after each trip. Nothing is sent to a road authority until you approve it.", `<a class="btn" style="margin-top:12px" href="#/reports">See all reports</a>`)}</div>`;
    return;
  }
  const current = Number(params.get("id")) || pending[0].id;
  view.innerHTML = `<div class="inbox">
      <div class="card"><div class="card-head"><h2>Awaiting review</h2><span class="pill amber">${pending.length}</span></div>
        <div class="inbox-list">${pending.map((r) => `<div class="inbox-item ${r.id === current ? "on" : ""}" data-id="${r.id}">
          <img class="thumb" loading="lazy" src="/api/media/${encodeURIComponent(r.best_event_id)}.jpg?crop=1" alt="">
          <div style="min-width:0"><div class="title">${esc(r.class_name)} <span class="muted small">#${r.id}</span></div>
          <div class="muted small">${esc(r.address || "")} · ${esc(authority(r.jurisdiction))}</div>
          <div class="meta">${sevPill(r.severity)}${flags(r)}</div></div></div>`).join("")}</div></div>
      <div class="card" id="detail"><div class="card-body"><div class="skeleton" style="width:50%"></div></div></div></div>`;
  const ids = pending.map((r) => r.id);
  const open = async (id) => {
    if (view !== stage()) return; // the user has already moved to another page
    $$(".inbox-item").forEach((x) => x.classList.toggle("on", Number(x.dataset.id) === id));
    history.replaceState(null, "", `#/review?id=${id}`);
    const d = await api(`/api/reports/${id}`);
    if (view !== stage()) return;
    $("#detail").innerHTML = reportDetail(d);
    const pos = $("#review-pos"), at = ids.indexOf(id) + 1;
    if (pos) { pos.hidden = false; pos.innerHTML = `<b>${at}</b> of ${ids.length}<span class="review-track" aria-hidden="true"><i style="width:${(at / ids.length) * 100}%"></i></span>`; }
    animateView($("#detail"));
    $(`.inbox-item[data-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
    bindDetail(d, (stay) => renderReview(new URLSearchParams(`id=${stay ? id : ids[ids.indexOf(id) + 1] || ""}`)));
    reviewKeys.current = id;
  };
  $$(".inbox-item").forEach((x) => {
    x.tabIndex = 0;
    x.setAttribute("role", "button");
    x.onclick = () => open(Number(x.dataset.id));
    x.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); x.onclick(); } };
  });
  reviewKeys.ids = ids;
  reviewKeys.open = open;
  open(current);
}
const reviewKeys = { ids: [], current: null, open: null };
document.addEventListener("keydown", (ev) => {
  if (!location.hash.startsWith("#/review") || ev.target.matches("input, textarea, select") || ev.ctrlKey || ev.metaKey || $("dialog[open]") || !palette.el.hidden) return;
  const k = ev.key.toLowerCase(), i = reviewKeys.ids.indexOf(reviewKeys.current);
  // navigation only: approving or rejecting is never bound to a single key (it sends a report to a road authority)
  if (k === "j" && i < reviewKeys.ids.length - 1) reviewKeys.open(reviewKeys.ids[i + 1]);
  else if (k === "k" && i > 0) reviewKeys.open(reviewKeys.ids[i - 1]);
});

/* ---------- evidence lightbox: the reviewer's only look at the damage, so let them see it properly ---------- */
function openLightbox(src, caption) {
  let dlg = $("#lightbox");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "lightbox";
    dlg.className = "lightbox";
    dlg.setAttribute("aria-label", "Evidence photo");
    dlg.innerHTML = `<div class="lb-bar"><span class="lb-cap"></span><span class="spacer"></span><span class="muted small hide-sm">Click the photo to zoom · <span class="kbd">Esc</span> close</span>
        <button type="button" class="btn btn-ghost icon-btn lb-close" aria-label="Close">${icon("x")}</button></div>
      <div class="lb-stage"><img alt="Privacy-blurred frame of the damage, enlarged"></div>`;
    document.body.append(dlg);
    const img = $("img", dlg), stageEl = $(".lb-stage", dlg);
    $(".lb-close", dlg).onclick = () => dlg.close();
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener("close", () => { stageEl.classList.remove("is-zoomed"); dlg.opener?.focus?.(); });
    // Zoom 2.5× around the point clicked, and follow the pointer while zoomed
    const aim = (e) => { const r = img.getBoundingClientRect(); img.style.transformOrigin = `${((e.clientX - r.left) / r.width) * 100}% ${((e.clientY - r.top) / r.height) * 100}%`; };
    img.onclick = (e) => { aim(e); stageEl.classList.toggle("is-zoomed"); };
    img.onpointermove = (e) => { if (stageEl.classList.contains("is-zoomed")) aim(e); };
  }
  dlg.opener = document.activeElement;
  $("img", dlg).src = src;
  $(".lb-cap", dlg).textContent = caption || "";
  dlg.showModal();
  $(".lb-close", dlg).focus();
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".photo-btn");
  if (b) openLightbox($("img", b).src, b.dataset.zoom);
});

/* ---------- keyboard shortcuts sheet (?) ---------- */
const SHORTCUTS = [
  ["Anywhere", [[["Ctrl", "K"], "Search, jump to a page or a report"], [["?"], "Show these shortcuts"], [["/"], "Filter the list on this page"], [["Esc"], "Close a dialog or the menu"]]],
  ["Review", [[["J"], "Next report"], [["K"], "Previous report"], [["Enter"], "Open the focused report"]]],
  ["Never on a key", [[[], "Approving and rejecting always need a click: they decide what reaches a road authority"]]],
];
function openShortcuts() {
  let dlg = $("#shortcuts");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "shortcuts";
    dlg.className = "sheet";
    dlg.setAttribute("aria-labelledby", "shortcuts-h");
    const mod = $("#mod-key").textContent;
    dlg.innerHTML = `<div class="card-head"><h2 id="shortcuts-h">Keyboard shortcuts</h2><span class="spacer"></span><button type="button" class="btn btn-ghost icon-btn" aria-label="Close">${icon("x")}</button></div>
      <div class="card-body sheet-body">${SHORTCUTS.map(([g, rows]) => `<section><h3 class="h3">${esc(g)}</h3><dl>${rows.map(([keys, what]) =>
        `<div><dt>${keys.length ? keys.map((k) => `<span class="kbd">${esc(k === "Ctrl" ? mod : k)}</span>`).join("") : icon("lock")}</dt><dd>${esc(what)}</dd></div>`).join("")}</dl></section>`).join("")}</div>`;
    document.body.append(dlg);
    $(".card-head button", dlg).onclick = () => dlg.close();
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  }
  if (!dlg.open) dlg.showModal();
}
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || !session.user || !palette.el.hidden || $("dialog[open]")) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  if (e.key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); openShortcuts(); }
  else if (e.key === "/") {
    // The page's own filter if it has one, otherwise the palette
    e.preventDefault();
    const field = $("#view input[type=search]");
    if (field) { field.focus(); field.select(); } else palette.open();
  }
});

/* ---------- Trips (upload + live progress) ---------- */
async function renderTrips(params) {
  const view = stage();
  const [trips, jobs] = await Promise.all([api("/api/trips"), api("/api/jobs")]);
  const running = jobs.find((j) => j.status === "running");
  view.innerHTML = `
    <div class="grid two">
      <div class="card"><div class="card-head"><h2>New trip</h2><span class="spacer"></span><span class="desc">The laptop plays the in-car device</span></div>
        <form class="card-body grid" id="trip-form" style="gap:18px">
          <label class="dropzone" id="drop"><span class="dz-icon">${icon("upload")}</span><b id="drop-title">Drop a dashcam video here, or click to choose</b><span class="muted small">MP4, MOV, AVI or MKV · up to 500 MB</span>
            <input type="file" id="video" accept=".mp4,.mov,.avi,.mkv,video/*" class="sr-only" required></label>
          <fieldset class="gps-set">
            <legend>GPS track</legend>
            <label class="switch"><input type="checkbox" id="simulate" checked aria-controls="gps"><span>No GPS file: simulate a drive along Spring Garden Rd, Halifax</span></label>
            <div class="file-pick" id="gps-pick">
              <label class="btn" for="gps">${icon("pin")}Choose GPS file</label>
              <input type="file" id="gps" accept=".gpx,.csv" class="sr-only">
              <span class="file-name" id="gps-name">No file chosen</span>
              <button type="button" class="btn btn-ghost btn-sm" id="gps-clear" hidden aria-label="Remove GPS file">${icon("x")}</button>
            </div>
            <span class="muted small" id="gps-hint">GPX, or CSV with <code>time,lat,lon</code>. Phone GPS logger apps export GPX.</span>
          </fieldset>
          <div style="display:flex;gap:10px;align-items:center"><button class="btn btn-primary btn-lg" id="start">${icon("upload")}Analyse trip</button>
            <span class="muted small">Only blurred frames of damage leave the device.</span></div>
        </form></div>
      <div class="card"><div class="card-head"><h2>How a trip is processed</h2></div><div class="card-body"><ul class="pipeline">
        <li>${avatar("edge")}<div><b>Edge device</b><div class="muted small">YOLOv8m at 5 frames/s, BoT-SORT tracking, best frame per defect, blur everything but the damage, place it on the road.</div></div></li>
        <li>${avatar("agent:vision")}<div><b>Vision</b><div class="muted small">RT-DETR-L second opinion and close-up re-check. The AI judges only borderline detections: real damage or false alarm.</div></div></li>
        <li>${avatar("agent:location")}<div><b>Location</b><div class="muted small">Road class and route number decide Halifax 311 or Nova Scotia Public Works; the AI picks only when the rules are unsure.</div></div></li>
        <li>${avatar("agent:triage")}<div><b>Triage</b><div class="muted small">Merges repeat sightings within 15 m; independent drivers raise priority. Plain rules, no AI.</div></div></li>
        <li>${avatar("agent:filing")}<div><b>Drafting</b><div class="muted small">The AI writes the description the city reads; nothing is sent until you approve it.</div></div></li></ul></div></div>
    </div>
    <div id="job" class="section"></div>
    <div class="card section"><div class="card-head"><h2>All trips</h2><span class="pill">${trips.length}</span></div>${tripsTable(trips)}</div>`;
  const video = $("#video"), drop = $("#drop");
  video.onchange = () => { $("#drop-title").textContent = video.files[0]?.name || "Drop a dashcam video here, or click to choose"; };
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => { video.files = e.dataTransfer.files; video.onchange(); });
  // The GPS picker only means something when the drive is not simulated: grey it out (and drop any file) while it is
  const gps = $("#gps"), sim = $("#simulate");
  const syncGps = () => {
    const off = sim.checked;
    if (off) gps.value = "";
    gps.disabled = off;
    $("#gps-pick").classList.toggle("is-disabled", off);
    $("#gps-pick label").setAttribute("aria-disabled", off);
    $("#gps-name").textContent = gps.files[0]?.name || (off ? "Not needed while simulating" : "No file chosen");
    $("#gps-clear").hidden = !gps.files[0];
    $("#gps-hint").innerHTML = off ? "Turn off simulation to attach a GPX or CSV track from the drive."
      : "GPX, or CSV with <code>time,lat,lon</code>. Phone GPS logger apps export GPX.";
  };
  sim.onchange = syncGps;
  gps.onchange = syncGps;
  $("#gps-clear").onclick = () => { gps.value = ""; syncGps(); };
  syncGps();
  $("#trip-form").onsubmit = async (ev) => {
    ev.preventDefault();
    if (!video.files[0]) return toast("Choose a dashcam video first.", false);
    const fd = new FormData();
    fd.append("video", video.files[0]);
    if (!sim.checked && !gps.files[0]) return toast("Choose a GPS file, or turn simulation back on.", false);
    if (gps.files[0]) fd.append("gps", gps.files[0]);
    fd.append("simulate", sim.checked);
    $("#start").disabled = true;
    try {
      const job = await uploadTrip(fd, video.files[0]);
      // The reviewer may have moved on while it uploaded: only draw live progress if this page is still showing
      if ($("#job") && view === stage()) watchJob(job.id); else toast("Trip uploaded. The agents are processing it; see Trips for progress.");
    }
    catch (e) { if (e.name !== "AbortError") toast(e.message, false); if ($("#start")) $("#start").disabled = false; }
  };
  if (running) watchJob(running.id);
}

/* A dashcam file can be hundreds of MB: show bytes sent, time left and a Cancel, and warn before the tab closes mid-upload.
   fetch() has no upload progress, so this one call uses XHR. */
function uploadTrip(fd, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const mb = (b) => (b / 1048576).toFixed(b < 10485760 ? 1 : 0);
    const started = performance.now();
    const guard = (e) => { e.preventDefault(); e.returnValue = ""; };
    const done = () => { window.removeEventListener("beforeunload", guard); };
    const clearJob = () => { const j = $("#job"); if (j) j.innerHTML = ""; };
    window.addEventListener("beforeunload", guard);
    $("#job").innerHTML = `<div class="card"><div class="card-head"><h2>Uploading ${esc(file.name)}</h2><span class="pill blue">Uploading</span><span class="spacer"></span>
        <button type="button" class="btn btn-sm" id="upload-cancel">${icon("x")}Cancel upload</button></div>
      <div class="card-body"><div class="upload-row"><span class="mono" id="upload-pct">0%</span><span class="muted small" id="upload-meta">${mb(file.size)} MB to send</span></div>
        <div class="progress" role="progressbar" aria-label="Upload progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="upload-bar"><i></i></div></div></div>`;
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable || !$("#upload-bar")) return;
      const p = e.loaded / e.total, secs = (performance.now() - started) / 1000;
      const left = p > 0.02 ? Math.max(0, Math.round(secs / p - secs)) : null;
      $("#upload-bar i").style.width = `${p * 100}%`;
      $("#upload-bar").setAttribute("aria-valuenow", Math.round(p * 100));
      $("#upload-pct").textContent = `${Math.round(p * 100)}%`;
      $("#upload-meta").textContent = p >= 1 ? "Sent. The edge device is starting…"
        : `${mb(e.loaded)} of ${mb(e.total)} MB${left != null ? ` · about ${left < 60 ? left + " s" : Math.ceil(left / 60) + " min"} left` : ""}`;
    };
    xhr.onload = () => {
      done();
      let body = xhr.responseText; try { body = JSON.parse(body); } catch {}
      if (xhr.status === 401) { showLogin(); return reject(new Error("Please sign in")); }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      clearJob();
      reject(new Error(typeof body?.detail === "string" ? body.detail : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => { done(); clearJob(); reject(new Error("Upload failed: the connection dropped. Check the network and try again.")); };
    xhr.onabort = () => { done(); clearJob(); toast("Upload cancelled."); const e = new Error("cancelled"); e.name = "AbortError"; reject(e); };
    $("#upload-cancel").onclick = () => xhr.abort();
    xhr.open("POST", "/api/trips");
    xhr.setRequestHeader("X-CSRF-Token", session.csrf);
    xhr.withCredentials = true;
    xhr.send(fd);
  });
}

const STEPS = [["edge", "Edge device", "video"], ["upload", "Upload", "upload"], ["agents", "Agents", "sparkle"], ["done", "Ready to review", "check"]];
function watchJob(id) {
  const tick = async () => {
    let j;
    try { j = await api(`/api/jobs/${id}`); } catch { return; }
    const at = STEPS.findIndex(([k]) => k === j.step);
    const edge = j.edge || {}, res = j.result || {}, llm = res.llm || {};
    $("#job").innerHTML = `<div class="card"><div class="card-head"><h2>Trip ${esc(j.trip_id)}</h2>${j.status === "failed" ? `<span class="pill red">Failed</span>` : j.status === "done" ? `<span class="pill green">Done</span>` : `<span class="pill blue">Running</span>`}<span class="spacer"></span><span class="muted small">${esc(j.video)} · GPS: ${esc(j.gps)}</span></div>
      <div class="card-body"><div class="steps">${STEPS.map(([k, t, ic], i) => `<div class="step ${i === at && j.status === "running" ? "on" : ""} ${i < at || j.status === "done" ? "ok" : ""}"><div class="t">${icon(i < at || j.status === "done" ? "check" : ic)}${t}</div><div class="d">${i === at ? esc(j.detail || "") : i < at ? stepDone(k, j) : ""}</div>${i === at && j.status === "running" ? `<div class="progress"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>` : ""}</div>`).join("")}</div>
      ${j.status === "failed" ? `<div class="callout red section">${icon("alert")}<div>${esc(j.error)}</div></div>` : ""}
      ${j.status === "done" ? `<div class="callout green section">${icon("sparkle")}<div><b>${res.mode?.startsWith("agents") ? `Agents (${esc(llm.model || "")}, ${llm.calls || 0} LLM calls, ${res.seconds} s)` : "Rule engine"}</b><div>${esc(res.summary || "")}</div></div></div>
        <div style="display:flex;gap:10px;margin-top:14px"><a class="btn btn-primary" href="#/review">${icon("inbox")}Review reports</a><a class="btn" href="#/activity?trip=${encodeURIComponent(j.trip_id)}">See agent trace</a></div>
        <div id="trip-gallery" class="gallery section"></div>` : ""}</div></div>`;
    if (j.status !== "running") {
      clearInterval(t);
      $("#start") && ($("#start").disabled = false);
      if (j.status === "done") {
        const { events } = await api(`/api/trips/${encodeURIComponent(j.trip_id)}`);
        $("#trip-gallery").innerHTML = events.map((e) => `<figure><img loading="lazy" src="/api/media/${encodeURIComponent(e.id)}.jpg?crop=1" alt=""><figcaption><span>${esc(e.class_name)}</span>${e.status === "rejected" ? `<span class="pill">Rejected</span>` : e.class_disputed ? `<span class="pill amber">Disputed</span>` : `<span class="pill green">Confirmed</span>`}</figcaption></figure>`).join("");
        refreshChrome();
      }
    }
  };
  const t = setInterval(tick, 1500);
  timers.push(t);
  tick();
}
const stepDone = (k, j) => k === "edge" && j.edge ? `${j.edge.events} defects · ${j.edge.detector_fps} FPS` : k === "upload" && j.upload ? `${j.upload.accepted} frames uploaded` : "";

/* ---------- Agent activity ---------- */
async function renderActivity(params) {
  const view = stage();
  const all = await api("/api/activity?limit=600");
  const who = params.get("actor") || "all";
  const trip = params.get("trip");
  const tools = params.get("tools") === "1";
  const agentsOnly = all.filter((a) => a.actor.startsWith("agent:") || a.actor === "driver" || a.actor === "fallback");
  let list = who === "all" ? agentsOnly : agentsOnly.filter((a) => a.actor === who);
  if (trip) list = list.filter((a) => (a.subject || "").includes(trip) || (a.detail || "").includes(trip));
  const hiddenTools = tools ? 0 : list.filter((a) => a.action.startsWith("tool:")).length;
  if (!tools) list = collapseRuns(list.filter((a) => !a.action.startsWith("tool:") && !(a.actor === "agent:follow_up" && a.action === "task")));
  const PAGE = 50;
  const query = (o) => "#/activity?" + new URLSearchParams(Object.entries({ actor: who, trip, tools: tools ? "1" : "", ...o }).filter(([, v]) => v && v !== "all")).toString();
  const groups = ["all", "agent:orchestrator", "agent:vision", "agent:location", "agent:triage", "agent:filing", "agent:follow_up", "driver"];
  view.innerHTML = `
    <div class="toolbar"><div class="tabs" role="group" aria-label="Show activity by agent">${groups.map((g) => `<button type="button" aria-pressed="${g === who}" class="${g === who ? "on" : ""}" data-g="${g}">${g === "all" ? "All" : esc(actor(g)[0].replace(/ (agent|step)$/, ""))}</button>`).join("")}</div>
    ${trip ? `<span class="pill blue">Trip ${esc(trip)} <a href="${query({ trip: "" })}" aria-label="Clear trip filter" style="display:inline-flex;margin-left:2px">${icon("x")}</a></span>` : ""}<span class="spacer"></span>
      <label class="switch small"><input type="checkbox" id="show-tools" ${tools ? "checked" : ""}><span>Show tool calls${hiddenTools ? ` (${num(hiddenTools)})` : ""}</span></label></div>
    <div class="card">${list.length ? `<ul class="feed" id="act-feed">${list.slice(0, PAGE).map(activityItem).join("")}</ul>
      <div class="card-foot act-foot"><span id="act-count">${num(Math.min(PAGE, list.length))} of ${num(list.length)}</span>${list.length > PAGE ? `<button class="btn btn-sm" id="act-more">Show ${num(Math.min(PAGE, list.length - PAGE))} more</button>` : ""}</div>`
      : emptyState("sparkle", "No agent activity yet", "Process a trip with an OpenRouter key to see the agents plan and decide.")}</div>`;
  $$(".tabs button").forEach((b) => b.onclick = () => location.hash = query({ actor: b.dataset.g }));
  $("#show-tools").onchange = (e) => location.hash = query({ tools: e.target.checked ? "1" : "" });
  let shown = PAGE;
  const moreBtn = $("#act-more");
  if (moreBtn) moreBtn.onclick = () => {
    $("#act-feed").insertAdjacentHTML("beforeend", list.slice(shown, shown + PAGE).map(activityItem).join(""));
    shown += PAGE;
    $("#act-count").textContent = `${num(Math.min(shown, list.length))} of ${num(list.length)}`;
    if (shown >= list.length) moreBtn.remove(); else moreBtn.textContent = `Show ${num(Math.min(PAGE, list.length - shown))} more`;
  };
}

// Back-to-back entries that say the same thing (the scheduler's routine checks) read as one line with a count
function collapseRuns(list) {
  const out = [];
  for (const a of list) {
    const key = a.actor + "|" + describe(a).join("|");
    const last = out[out.length - 1];
    if (last && last._key === key) { last._n += 1; last._oldest = a.ts; continue; }
    out.push({ ...a, _key: key, _n: 1, _oldest: a.ts });
  }
  return out;
}

function activityItem(a) {
  const [who] = actor(a.actor);
  const [what, why] = describe(a);
  const d = parse(a.detail);
  const isTool = a.action.startsWith("tool:");
  const tool = isTool && typeof d === "object" ? `<details class="tool"><summary>Arguments and result</summary><pre>${esc(JSON.stringify(d, null, 2)).slice(0, 4000)}</pre></details>` : "";
  const times = a._n > 1 ? `<span class="x">×${a._n}</span>` : "";
  const when = a._n > 1 ? `${ago(a._oldest)} – ${ago(a.ts)}` : ago(a.ts);
  return `<li>${avatar(a.actor)}<div style="min-width:0;flex:1"><div><span class="what">${esc(what)}</span>${times} <span class="who">· ${esc(who)}</span></div>${why && !isTool ? `<div class="why">${esc(why).slice(0, 900)}</div>` : ""}${tool}</div><span class="when">${when}</span></li>`;
}

/* ---------- Authorities ---------- */
async function renderAuthorities() {
  const view = stage();
  const a = await api("/api/authorities");
  view.innerHTML = `
    <div class="grid kpis">
      <div class="card kpi"><div class="label">Halifax 311 requests<span class="ic violet">${icon("building")}</span></div><div class="value">${a.open311.length}</div><div class="hint">${a.open311.filter((r) => r.status === "closed").length} closed (repaired)</div></div>
      <div class="card kpi"><div class="label">NS Public Works referrals<span class="ic cyan">${icon("route")}</span></div><div class="value">${a.referrals.length}</div><div class="hint">100-series highways and provincial roads</div></div>
      <div class="card kpi"><div class="label">Driver notifications<span class="ic green">${icon("wrench")}</span></div><div class="value">${a.notifications.length}</div><div class="hint">"Your pothole was fixed"</div></div>
    </div>
    <div class="card section"><div class="card-head"><h2>Halifax 311</h2><span class="desc">Open311 GeoReport v2</span><span class="pill">Mock server</span><span class="spacer"></span>
      <a class="btn btn-sm" href="${esc(a.open311_inbox)}" target="_blank" rel="noopener">${icon("ext")}City staff inbox</a><button class="btn btn-primary btn-sm" id="follow">${icon("refresh")}Check for repairs</button></div>
      ${a.open311.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Request</th><th>Service</th><th>Location</th><th>Description</th><th>Status</th><th>Updated</th></tr></thead><tbody>
      ${a.open311.map((r) => `<tr><td class="mono">${esc(r.service_request_id)}</td><td class="mono">${esc(r.service_code)}</td><td>${esc(r.address)}</td><td style="max-width:420px">${esc(r.description)}</td><td>${r.status === "closed" ? `<span class="pill green">Repaired</span>` : `<span class="pill violet">Open</span>`}</td><td class="muted">${ago(r.updated_datetime)}</td></tr>`).join("")}</tbody></table></div>`
      : emptyState("building", "Nothing sent to Halifax yet", "Approve a report in Review to send it.")}
      <div class="card-foot">Halifax has no public Open311 API today, so the demo files to a local GeoReport v2 server. Mark a request repaired in the city inbox, then check for repairs.</div></div>
    <div class="card section"><div class="card-head"><h2>Nova Scotia Public Works</h2><span class="desc">Referrals</span><span class="spacer"></span><span class="muted small">No public API: 1-844-696-7737 · dpw-occ@novascotia.ca</span></div>
      ${a.referrals.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Referral</th><th>Location</th><th>Description</th></tr></thead><tbody>${a.referrals.map((r) => `<tr><td class="mono">${esc(r.id)}</td><td>${esc(r.address)}</td><td>${esc(r.description)}</td></tr>`).join("")}</tbody></table></div>` : emptyState("route", "No provincial referrals yet", "Damage on Highway 102 and other provincial roads is routed here.")}</div>
    <div class="card section"><div class="card-head"><h2>Driver notifications</h2></div>${a.notifications.length ? `<ul class="feed">${a.notifications.map((n) => `<li>${avatar("agent:follow_up")}<div style="flex:1">${esc(n.message)}</div><span class="when">${ago(n.ts)}</span></li>`).join("")}</ul>` : emptyState("wrench", "No repairs confirmed yet", "When a city closes a request, the Follow-up agent tells the drivers who reported it.")}</div>`;
  $("#follow").onclick = async () => {
    $("#follow").disabled = true;
    try { const j = await api("/api/follow-up", { method: "POST" }); const r = j.result || {}; toast(`Checked ${r.checked || 0} request(s); ${(r.fixed || []).length} repaired`); }
    catch (e) { toast(e.message, false); }
    renderAuthorities();
  };
}

/* ---------- Trust & system ---------- */
async function renderSystem() {
  const view = stage();
  const s = await api("/api/system");
  const h = s.health, llm = s.llm || {};
  const servers = {};
  h.mcp_tools.forEach((t) => (servers[t.split("_")[0]] ||= []).push(t));
  const NAMES = { detector: "Vision · detector MCP", geo: "Location · geo MCP", reports: "Triage · reports MCP", city: "Filing · municipality MCP" };
  const guard = (t, d, ok = true) => `<li class="${ok ? "" : "is-warn"}">${icon(ok ? "check" : "alert")}<div><b>${t}</b><div class="muted small">${d}</div></div></li>`;
  const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d/;
  const label = (k) => { const t = k.replace(/_/g, " "); return t[0].toUpperCase() + t.slice(1); };
  const val = (v) => typeof v === "string" && ISO.test(v) ? `<time datetime="${esc(v)}" title="${esc(new Date(v).toLocaleString())}">${ago(v)}</time>`
    : esc(typeof v === "object" ? JSON.stringify(v) : String(v));
  const model = (m) => `<span title="${esc(m)}">${esc(m.split("/").pop().replace(/:free$/, ""))}</span>`;
  const dep = s.deployment || {}, warn = dep.warnings || [];
  const kv = (o, suffix = "") => Object.entries(o || {}).map(([k, v]) => `<div class="fact"><div class="k">${esc(label(k) + suffix)}</div><div class="v small">${val(v)}</div></div>`).join("");
  view.innerHTML = `
    ${warn.length ? `<div class="callout amber section-tight">${icon("alert")}<div><b>${warn.length} setting(s) are not production-ready</b><ul class="warn-list">${warn.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div></div>` : `<div class="callout green section-tight">${icon("check")}<div><b>Production checklist passed</b>: secrets set, sign-in on, data residency enforced, public evidence links.</div></div>`}
    <div class="grid two">
      <div class="card"><div class="card-head"><h2>Guardrails enforced in code</h2></div><div class="card-body"><ul class="list-check">
        ${guard("Nothing is sent without reviewer approval", "The city tool refuses any report that isn't approved, whatever an agent asks. Only the reviewer and admin roles can approve.")}
        ${guard("The reviewer approves the exact payload", "Approval stores the payload and its SHA-256; delivery sends that snapshot and refuses if it was altered.")}
        ${guard("Rules decide severity and authority", "The agent's severity and jurisdiction are checked against the rules and map evidence; disagreements are logged.")}
        ${guard("Failed deliveries are retried", "Backoff from 1 minute to 6 hours, then the report is flagged for a manual resend.")}
        ${guard("AI only where judgement helps", "A code workflow runs every step; the AI judges borderline detections, unsure jurisdictions and drafts descriptions, in batches.")}
        ${guard("The AI never calls tools", "Each step uses its own allow-listed tools, enforced in code. The AI's typed answer is checked: unknown ids or choices outside the candidates are dropped and the rule decides.")}
        ${guard("No confirmation without evidence", "Damage is confirmed only with a second model's support; disagreements go to a human.")}
        ${guard("Append-only audit log", "Every plan, verdict and tool call is recorded; updates and deletes are blocked by the database.")}
        ${guard("Circuit breaker", "If an LLM fails or is rate-limited, a rule engine finishes the job over the same tools.")}
        ${guard("Kill switch", "ROADSENSE_AGENTS_ENABLED=0 turns the LLM agents off instantly.")}
      </ul></div></div>
      <div class="grid" style="align-content:start">
        <div class="card"><div class="card-head"><h2>Privacy by design</h2></div><div class="card-body"><ul class="list-check">
          ${guard("Redaction on the device, checked on the server", `Everything except the damage is blurred before upload; the server re-checks every frame${dep.privacy?.face_plate_model ? " and runs a face/plate model" : ""}.`)}
          ${guard("No images to the LLM", "Agents get text metadata only: ids, classes, confidences, positions.")}
          ${guard("Signed, expiring evidence links", `The authority fetches the photo with a link valid for ${dep.privacy?.media_link_days || "?"} days; nothing else is public.`)}
          ${guard(dep.residency?.mode === "ca" ? "Canadian data residency enforced" : "Data residency not enforced", dep.residency?.mode === "ca" ? "LLM calls only go to allow-listed Canadian endpoints; free prompt-logging models are refused." : "Set ROADSENSE_DATA_RESIDENCY=ca and a Canadian no-retention LLM endpoint before municipal use (PIIDPA / FOIPOP).", dep.residency?.mode === "ca")}
          ${guard("Retention", `${dep.retention?.keep_raw_video ? "Raw video is kept (development setting)" : "Raw video and GPS deleted after processing"}; rejected photos after ${dep.retention?.rejected_media_days} days; car positions after ${dep.retention?.car_position_days} days.`)}
        </ul></div></div>
        <div class="card"><div class="card-head"><h2>Agent stack</h2><span class="spacer"></span>${h.llm.enabled ? `<span class="pill green"><span class="dot"></span>Live</span>` : `<span class="pill amber">Rule engine</span>`}</div><div class="card-body facts">
          <div class="fact"><div class="k">Mode</div><div class="v">${esc(h.llm.mode)}</div></div>
          <div class="fact"><div class="k">Primary model</div><div class="v small">${model(h.llm.models[0])}</div></div>
          <div class="fact"><div class="k">Fallbacks</div><div class="v small">${h.llm.models.slice(1).map(model).join("<br>") || "None"}</div></div>
          <div class="fact"><div class="k">Quota</div><div class="v small">${llm.configured ? (llm.free_tier ? "Free tier · 50 req/day" : "Paid credits") : "No key"}</div></div>
        </div></div>
      </div>
    </div>
    <div class="grid two section">
      <div class="card"><div class="card-head"><h2>Delivery channels</h2><span class="spacer"></span><span class="muted small">city requests: ${dep.city_requests?.configured ? `${dep.city_requests.count} synced` : "not configured"}</span></div><div class="card-body facts">${Object.entries(dep.channels || {}).map(([a, c]) => `<div class="fact"><div class="k">${esc(authority(a))}</div><div class="v small">${esc(CHANNELS[c] || c)}</div></div>`).join("")}</div>
        ${can("admin") && dep.city_requests?.configured ? `<div class="card-foot"><button class="btn btn-sm" id="sync-city">${icon("refresh")}Sync city requests now</button></div>` : ""}</div>
      <div class="card"><div class="card-head"><h2>Jobs and scheduler</h2><span class="spacer"></span>${dep.scheduler?.enabled ? `<span class="pill green"><span class="dot"></span>Scheduler on</span>` : `<span class="pill amber">Scheduler off</span>`}</div><div class="card-body facts">${kv(dep.jobs, " jobs")}${kv(dep.scheduler?.last_run, " last run")}<div class="fact"><div class="k">Sign-in</div><div class="v small">${s.auth?.enabled ? (s.auth.sso ? "SSO + password" : "Password") : "Disabled"}</div></div></div></div>
    </div>
    <div class="card section"><div class="card-head"><h2>MCP servers and tools</h2><span class="spacer"></span><span class="muted small">${h.mcp_tools.length} tools over stdio</span></div>
      <div class="card-body grid kpis">${Object.entries(servers).map(([k, tools]) => `<div class="server-card"><div class="name">${icon("layers")}${esc(NAMES[k] || k)}<span class="spacer"></span><span class="faint small">${tools.length}</span></div><div class="chip-row">${tools.map((t) => `<span class="pill mono">${esc(t)}</span>`).join("")}</div></div>`).join("")}</div></div>`;
  const sync = $("#sync-city");
  if (sync) sync.onclick = async () => {
    sync.disabled = true;
    try { const j = await api("/api/external/sync", { method: "POST" }); const r = j.result || {}; toast(r.error ? `Sync failed: ${r.error}` : `City requests: ${r.added || 0} new, ${r.updated || 0} updated`, !r.error); }
    catch (e) { toast(e.message, false); }
    renderSystem();
  };
}

/* ---------- Road condition (segment estimate) ---------- */
async function renderConditions() {
  const view = stage();
  const c = await api("/api/conditions");
  const segs = c.segments || [];
  if (!segs.length) { view.innerHTML = `<div class="card">${emptyState("map", "No open defects yet", "Segment scores appear once trips produce reports. Each 100 m of road is scored from the defects on it.")}</div>`; return; }
  const tone = { good: "green", fair: "amber", poor: "red", "very poor": "red" };
  const avg = segs.reduce((a, x) => a + x.condition_index, 0) / segs.length;
  view.innerHTML = `
    <div class="grid kpis">
      <div class="card kpi"><div class="label">Segments with defects<span class="ic blue">${icon("map")}</span></div><div class="value">${segs.length}</div><div class="hint">100 m bins along each road</div></div>
      <div class="card kpi"><div class="label">Poor or worse<span class="ic red">${icon("alert")}</span></div><div class="value">${segs.filter((x) => x.condition_index < 55).length}</div><div class="hint">Index below 55</div></div>
      <div class="card kpi"><div class="label">Average index<span class="ic green">${icon("activity")}</span></div><div class="value">${avg.toFixed(0)}</div><div class="hint">Of segments with open defects</div></div>
    </div>
    <div class="callout blue section-tight">${icon("alert")}<div><b>An estimate, not a certified survey.</b> ${esc(c.method)}. Use it to rank where to look; HRM's Pavement Quality Index survey remains the record.</div></div>
    <div class="card section"><div class="card-head"><h2>Worst segments first</h2></div><div class="table-wrap"><table class="table"><thead><tr><th>Road</th><th>Defects</th><th>Potholes</th><th style="width:28%">Condition index</th><th>Rating</th><th>Reports</th></tr></thead><tbody>
      ${segs.map((x) => `<tr><td><b>${esc(x.road)}</b><div class="muted small mono">${x.lat.toFixed(5)}, ${x.lon.toFixed(5)}</div></td><td>${x.defects}</td><td>${x.potholes}</td>
        <td><div class="meter-row"><div class="meter"><span style="width:${x.condition_index}%" class="${tone[x.rating] || ""}"></span></div><span class="mono small">${x.condition_index.toFixed(0)}</span></div></td><td><span class="pill ${tone[x.rating] || ""}">${esc(x.rating)}</span></td>
        <td>${x.report_ids.slice(0, 6).map((id) => `<a href="#/reports?id=${id}" class="mono small">#${id}</a>`).join(" ")}</td></tr>`).join("")}</tbody></table></div></div>`;
}

/* ---------- Service standards (deadlines against published standards + duty-of-care ledger) ---------- */
const SLA = {
  overdue: ["Overdue", "red"], approaching_deadline: ["Approaching deadline", "amber"], awaiting_response: ["Awaiting response", "blue"],
  within_standard: ["Within standard", "green"], unable_to_determine: ["Unable to determine", ""],
  completed_after_target: ["Completed after target", "red"], completed_within_target: ["Completed within target", "green"],
};
const slaPill = (s) => { const [t, c] = SLA[s] || [s, ""]; return `<span class="pill ${c}"><span class="dot"></span>${esc(t)}</span>`; };
const LEDGER_EVENTS = {
  report_submitted: ["Submitted to the authority", "send"], acknowledged: ["Acknowledged by the authority", "check"],
  inspection_started: ["Inspection started", "eye"], repair_started: ["Repair started", "wrench"],
  repair_completed: ["Repair completed", "check"], report_closed: ["Report closed", "lock"], report_reopened: ["Report reopened", "refresh"],
  authority_status_changed: ["Authority status changed", "building"], deadline_approaching: ["Deadline approaching", "clock"],
  deadline_passed: ["Deadline passed", "alert"], standard_verified: ["Service standard verified", "shield"],
  repair_verified: ["Repair verified by RoadSense", "check"], repair_not_verified: ["Damage seen again after closure", "alert"],
};
const STAGE_LABEL = { submitted: "Submitted", acknowledged: "Acknowledged", inspection: "Inspection", repair_started: "Repair started", repair_completed: "Repaired", closed: "Closed", reopened: "Reopened" };
const SS_FILTERS = ["authority", "neighbourhood", "damage", "priority", "status", "date_from", "date_to"];
const unitShort = (u) => (u === "calendar_days" ? "days" : "business days");
const inTz = (ts, tz) => ts ? new Date(ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short", timeZone: tz || undefined }) : "–";
const dueText = (i) => i.days_left == null ? "" : i.days_left < 0 ? `${-i.days_left} ${unitShort(i.time_unit)} late` : i.days_left === 0 ? "due today" : `${i.days_left} ${unitShort(i.time_unit)} left`;

async function renderStandards(params) {
  const view = stage();
  const id = params.get("id");
  const q = new URLSearchParams([...params].filter(([k, v]) => SS_FILTERS.includes(k) && v));
  const [d, cfg, t] = await Promise.all([api(`/api/service-standards?${q}`), api("/api/service-standards/rules"),
    id && can("review") ? api(`/api/service-standards/reports/${encodeURIComponent(id)}`) : null]);
  const m = d.metrics, admin = can("admin");
  const tile = (ic, tone, value, label, hint, s) => `<a class="card kpi" ${s ? `href="#/standards?${new URLSearchParams({ ...Object.fromEntries(q), status: s })}"` : ""}><div class="label">${esc(label)}<span class="ic ${tone}">${icon(ic)}</span></div><div class="value">${value}</div><div class="hint">${hint}</div></a>`;
  const opt = (name, values, label, fmt = (v) => v) => `<label>${esc(label)}<select class="input input-sm" name="${name}"><option value="">Any</option>${values.map((v) => `<option value="${esc(v)}" ${q.get(name) === v ? "selected" : ""}>${esc(fmt(v))}</option>`).join("")}</select></label>`;
  const rowHtml = (i) => `<tr class="click" tabindex="0" data-id="${i.report_id}"><td><span class="strong">${esc(i.class_name)}</span> <span class="faint small mono">#${i.report_id}</span><div class="muted small">${esc(i.address || "")}</div></td>
      <td class="nowrap">${esc(authority(i.authority))}</td><td>${esc(i.neighbourhood || "–")}</td><td>${i.priority ? `<span class="pill mono">${esc(i.priority)}</span>` : "–"}</td>
      <td>${esc(STAGE_LABEL[i.stage] || i.stage)}${i.reopened_count ? ` <span class="pill amber">reopened ×${i.reopened_count}</span>` : ""}<div class="muted small">${esc(i.authority_status || "")}${i.authority_notes ? " · " + esc(i.authority_notes) : ""}</div></td>
      <td class="nowrap">${i.next_due ? `${esc(inTz(i.next_due, "America/Halifax"))}<div class="small ${i.days_left < 0 ? "late" : "muted"}">${esc(i.next_due_kind)} · ${esc(dueText(i))}</div>` : i.completed_at || i.closed_at ? `<span class="muted small">done ${esc(inTz(i.completed_at || i.closed_at, "America/Halifax"))}</span>` : "–"}</td>
      <td>${slaPill(i.status)}${i.provisional ? `<div class="small muted">provisional: ${esc((SLA[i.provisional] || [i.provisional])[0].toLowerCase())}</div>` : ""}${i.response_on_time === false ? `<div class="small muted">response was late</div>` : ""}</td><td>${icon("arrow", "row-arrow")}</td></tr>`;
  const ruleScope = (r) => [r.road_category && `road ${r.road_category}`, r.damage_category && (CLASS_LABELS[r.damage_category] || r.damage_category), r.priority && `band ${r.priority}`].filter(Boolean).map(esc).join(" · ") || `<span class="muted">any report</span>`;
  const verPill = (v) => `<span class="pill ${v === "verified" ? "green" : v === "approved" ? "blue" : "amber"}">${esc(v)}</span>`;
  view.innerHTML = `
    <div class="callout blue">${icon("shield")}<div><b>${esc(d.disclaimer)}</b> ${esc(d.method)}</div></div>
    ${id ? `<div class="card section" id="ss-timeline">${t ? timelineCard(t) : `<div class="card-body muted">${icon("lock")} The report timeline names staff and authority records: reviewers and admins only.</div>`}</div>` : ""}
    <div class="grid kpis section">
      ${tile("layers", "blue", num(m.active), "Active reports", `${num(m.total)} tracked in this view`)}
      ${tile("check", "green", num(m.within_standard), "Within standard", `+ ${num(m.awaiting_response)} awaiting a response`, "within_standard")}
      ${tile("clock", "amber", num(m.approaching_deadline), "Approaching deadline", "Inside the warning window", "approaching_deadline")}
      ${tile("alert", "red", num(m.overdue), "Overdue", `${num(m.response_late)} responded after the response target`, "overdue")}
      ${tile("check", "green", num(m.completed_within_target), "Completed within target", "Repaired or closed on time", "completed_within_target")}
      ${tile("alert", "red", num(m.completed_after_target), "Completed after target", "Repaired or closed late", "completed_after_target")}
      ${tile("activity", "violet", m.avg_response_days == null ? "–" : `${m.avg_response_days} d`, "Average response time", m.responded ? `Submission to first response · ${m.responded} report(s) · ${m.avg_response_hours} h` : "No responses yet")}
      ${tile("wrench", "cyan", m.avg_completion_days == null ? "–" : `${m.avg_completion_days} d`, "Average repair time", m.repaired ? `Repaired reports only (${m.repaired}) · ${m.avg_completion_hours} h` : "Unresolved reports are not counted")}
      ${tile("search", "", num(m.unable_to_determine), "Unable to determine", "No rule, or an unverified one", "unable_to_determine")}
    </div>
    <div class="card section"><form class="card-body form-row" id="ss-filters">
      ${opt("authority", d.options.authority, "Authority", authority)}${opt("neighbourhood", d.options.neighbourhood, "Neighbourhood")}
      ${opt("damage", d.options.class_code, "Damage", (c) => CLASS_LABELS[c] || c)}${opt("priority", d.options.priority, "Priority (band)")}
      ${opt("status", Object.keys(SLA), "Tracking status", (s) => SLA[s][0])}
      <label>Submitted from<input class="input input-sm" type="date" name="date_from" value="${esc(q.get("date_from") || "")}"></label>
      <label>to<input class="input input-sm" type="date" name="date_to" value="${esc(q.get("date_to") || "")}"></label>
      ${q.toString() ? `<a class="btn btn-sm" href="#/standards">${icon("x")}Clear</a>` : ""}</form>
      ${d.items.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Report</th><th>Authority</th><th>Neighbourhood</th><th>Priority</th><th>Stage · authority status</th><th>Next deadline (Halifax time)</th><th>Tracking</th><th></th></tr></thead><tbody>${d.items.map(rowHtml).join("")}</tbody></table></div>`
        : emptyState("clock", "Nothing tracked here", q.toString() ? "No filed report matches these filters." : "The service clock starts when an approved report reaches its authority.")}
      <details class="card-body"><summary class="small">How each tracking status is decided</summary><dl class="small">${Object.entries(d.definitions).map(([k, v]) => `<dt style="margin-top:8px">${slaPill(k)}</dt><dd style="margin:4px 0 0">${esc(v)}</dd>`).join("")}</dl></details></div>
    <div class="card section"><div class="card-head"><h2>Service standards</h2><span class="pill">${cfg.rules.length}</span><span class="spacer"></span><span class="muted small">Editing a rule never moves deadlines already started: each report keeps the snapshot it was filed under.</span></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>Authority</th><th>Applies to</th><th>Response</th><th>Completion</th><th>Effective</th><th>Source</th><th>Verification</th><th>Reports</th>${admin ? "<th></th>" : ""}</tr></thead><tbody>
      ${cfg.rules.map((r) => `<tr data-rule="${r.id}"><td><b>${esc(authority(r.authority))}</b><div class="muted small">${esc(r.jurisdiction || "")}</div></td><td>${ruleScope(r)}</td>
        <td>${r.response_days == null ? "–" : `${r.response_days} ${unitShort(r.time_unit)}`}</td><td>${r.completion_days == null ? "–" : `${r.completion_days} ${unitShort(r.time_unit)}`}</td>
        <td class="small nowrap">${esc(r.effective_from)} → ${esc(r.effective_to || "open")}</td><td class="small">${r.source_url ? `<a href="${esc(r.source_url)}" target="_blank" rel="noopener noreferrer">official page${icon("ext")}</a>` : `<span class="muted">none on file</span>`}${r.notes ? `<div class="muted small" title="${esc(r.notes)}">${esc(r.notes.slice(0, 90))}${r.notes.length > 90 ? "…" : ""}</div>` : ""}</td>
        <td>${verPill(r.verification)}${r.last_reviewed ? `<div class="muted small">reviewed ${esc(r.last_reviewed)}${r.approved_by ? " · " + esc(r.approved_by) : ""}</div>` : ""}</td><td class="mono">${r.reports}</td>
        ${admin ? `<td class="nowrap"><select class="input input-sm ver" aria-label="Verification of rule ${r.id}">${cfg.verifications.map((v) => `<option ${v === r.verification ? "selected" : ""}>${v}</option>`).join("")}</select>${r.effective_to ? "" : ` <button class="btn btn-sm retire" type="button">Retire</button>`}</td>` : ""}</tr>`).join("")}</tbody></table></div>
      ${admin ? `<form class="card-body form-row" id="ss-new-rule" autocomplete="off">
        <label>Authority<select class="input input-sm" name="authority">${cfg.authorities.map((a) => `<option>${esc(a)}</option>`).join("")}</select></label>
        <label>Road category<input class="input input-sm" name="road_category" placeholder="any (e.g. primary)"></label>
        <label>Damage<select class="input input-sm" name="damage_category"><option value="">Any</option>${cfg.damage_categories.map((c) => `<option value="${c}">${esc(CLASS_LABELS[c] || c)}</option>`).join("")}</select></label>
        <label>Priority<select class="input input-sm" name="priority"><option value="">Any</option>${cfg.priorities.map((p) => `<option>${p}</option>`).join("")}</select></label>
        <label>Response<input class="input input-sm" name="response_days" type="number" min="0" placeholder="days"></label>
        <label>Completion<input class="input input-sm" name="completion_days" type="number" min="0" placeholder="days"></label>
        <label>Unit<select class="input input-sm" name="time_unit">${cfg.units.map((u) => `<option value="${u}">${unitShort(u)}</option>`).join("")}</select></label>
        <label>Effective from<input class="input input-sm" type="date" name="effective_from" required></label>
        <label>Official source<input class="input input-sm" name="source_url" type="url" placeholder="https://…"></label>
        <label>Verification<select class="input input-sm" name="verification">${cfg.verifications.map((v) => `<option>${v}</option>`).join("")}</select></label>
        <button class="btn btn-primary btn-sm" type="submit">${icon("plus")}Add rule</button></form>` : ""}</div>
    <div class="card section"><div class="card-head"><h2>Holidays</h2><span class="pill">${cfg.holidays.length}</span><span class="spacer"></span><span class="muted small">Skipped by business-day deadlines. Configure observed dates; changes apply to reports filed afterwards.</span></div>
      <div class="card-body chip-row">${cfg.holidays.map((h) => `<span class="pill" data-day="${esc(h.day)}" data-auth="${esc(h.authority)}"><span class="mono">${esc(h.day)}</span> ${esc(h.name)}${h.authority ? ` · ${esc(authority(h.authority))}` : ""}${admin ? ` <button type="button" class="btn-ghost rm" aria-label="Remove ${esc(h.name)} ${esc(h.day)}">${icon("x")}</button>` : ""}</span>`).join(" ")}</div>
      ${admin ? `<form class="card-body form-row" id="ss-new-holiday"><label>Date<input class="input input-sm" type="date" name="day" required></label><label>Name<input class="input input-sm" name="name" required maxlength="80"></label>
        <label>Authority<select class="input input-sm" name="authority"><option value="">All authorities</option>${cfg.authorities.map((a) => `<option>${esc(a)}</option>`).join("")}</select></label><button class="btn btn-sm" type="submit">${icon("plus")}Add holiday</button></form>` : ""}</div>`;

  $("#ss-filters").onchange = (ev) => {
    const f = new URLSearchParams([...new FormData(ev.currentTarget)].filter(([, v]) => v));
    location.hash = `#/standards${f.toString() ? "?" + f : ""}`;
  };
  $$("tr[data-id]", view).forEach((tr) => {
    tr.onclick = () => { const f = new URLSearchParams(q); f.set("id", tr.dataset.id); location.hash = `#/standards?${f}`; };
    tr.onkeydown = (e) => { if (e.key === "Enter") tr.onclick(); };
  });
  if (t) bindTimeline(t, () => renderStandards(params));
  if (!admin) return;
  const patch = async (rid, body, done) => { try { await api(`/api/service-standards/rules/${rid}`, { method: "PATCH", body: JSON.stringify(body) }); toast(done); } catch (e) { toast(e.message, false); } renderStandards(params); };
  $$("tr[data-rule]", view).forEach((tr) => {
    const rid = tr.dataset.rule;
    $(".ver", tr).onchange = (e) => patch(rid, { verification: e.target.value }, `Rule ${rid} is now ${e.target.value}`);
    const retire = $(".retire", tr);
    if (retire) retire.onclick = () => patch(rid, { effective_to: new Date().toISOString().slice(0, 10) }, `Rule ${rid} retired from today`);
  });
  const post = (form, path, done) => form.onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries([...new FormData(ev.target)].filter(([, v]) => v !== ""));
    try { await api(path, { method: "POST", body: JSON.stringify(f) }); toast(done); renderStandards(params); } catch (e) { toast(e.message, false); }
  };
  post($("#ss-new-rule"), "/api/service-standards/rules", "Rule added");
  post($("#ss-new-holiday"), "/api/service-standards/holidays", "Holiday added");
  $$("[data-day] .rm", view).forEach((b) => b.onclick = async () => {
    const chip = b.closest("[data-day]");
    try { await api(`/api/service-standards/holidays/${encodeURIComponent(chip.dataset.day)}${chip.dataset.auth ? "?authority=" + encodeURIComponent(chip.dataset.auth) : ""}`, { method: "DELETE" }); toast("Holiday removed"); renderStandards(params); } catch (e) { toast(e.message, false); }
  });
}

function timelineCard(t) {
  const c = t.clock, r = t.report, rule = t.rule, tz = c?.timezone || "America/Halifax", ok = t.integrity.ok;
  const entry = (e) => {
    const [label, ic] = LEDGER_EVENTS[e.event_type] || [e.event_type.replace(/_/g, " "), "file"];
    const x = e.data || {};
    const move = x.previous_status && x.previous_status !== x.new_status ? `${esc(STAGE_LABEL[x.previous_status] || x.previous_status)} → ${esc(STAGE_LABEL[x.new_status] || x.new_status)} · ` : "";
    return `<li><span class="avatar pill">${icon(ic)}</span><div class="who">${esc(label)} <span class="muted small">· ${esc(inTz(x.event_at || e.ts, tz))} · ${esc(e.actor)} · ${esc(x.source || "")}</span></div>
      <div class="say">${move}${x.tracking_after ? `tracking: ${esc((SLA[x.tracking_after] || [x.tracking_after])[0].toLowerCase())}` : ""}${x.authority_status ? ` · authority: ${esc(x.authority_status)}${x.authority_notes ? " (" + esc(x.authority_notes) + ")" : ""}` : ""}${x.note ? `<div>${esc(x.note)}</div>` : ""}</div>
      <div class="small faint mono" title="SHA-256 of this entry, chained to the previous one">${icon("lock")} ${esc(e.hash.slice(0, 16))}… ← ${esc(e.prev_hash.slice(0, 8))}…</div></li>`;
  };
  return `<div class="card-head"><h2>${esc(r.class_name)} <span class="muted">#${r.id}</span> · duty-of-care ledger</h2>${t.tracking ? slaPill(t.tracking.status) : ""}<span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/reports?id=${r.id}">Open report${icon("arrow")}</a><a class="btn btn-sm btn-ghost" href="#/standards" aria-label="Close the timeline">${icon("x")}</a></div>
    <div class="card-body">
      <div class="callout ${ok ? "green" : "red"}">${icon(ok ? "lock" : "alert")}<div><b>${ok ? "Integrity check passed" : "Integrity check FAILED"}</b> · ${t.integrity.entries} entr${t.integrity.entries === 1 ? "y" : "ies"}${t.integrity.head ? ` · chain head <code>${esc(t.integrity.head.slice(0, 16))}…</code>` : ""}. ${ok ? "Every entry's SHA-256 hash and its link to the previous entry were recomputed and match." : t.integrity.problems.map((p) => esc(`#${p.id ?? "?"}: ${p.problem}`)).join("; ")}</div></div>
      ${c ? `<div class="facts" style="margin-top:12px">
        <div class="fact"><div class="k">Authority</div><div class="v">${esc(authority(c.authority))}</div></div>
        <div class="fact"><div class="k">Submitted</div><div class="v small">${esc(inTz(c.submitted_at, tz))}</div></div>
        <div class="fact"><div class="k">Response due</div><div class="v small">${esc(inTz(c.response_due, tz))}</div></div>
        <div class="fact"><div class="k">Completion due</div><div class="v small">${esc(inTz(c.completion_due, tz))}</div></div>
        <div class="fact"><div class="k">Stage</div><div class="v">${esc(STAGE_LABEL[c.stage] || c.stage)}</div></div>
        <div class="fact"><div class="k">Standard</div><div class="v small">${rule ? `#${esc(rule.id)} · ${esc(rule.completion_days ?? "–")} ${esc(unitShort(rule.time_unit))} · ${esc(rule.verification)}${c.rule_verified_at ? " (verified later)" : ""}` : "none"}</div></div>
      </div>
      <div class="small muted" style="margin-top:8px">${esc(t.tracking?.reason || "")}${c.note ? ` · ${esc(c.note)}` : ""}. Times in ${esc(tz)}.${rule?.calendar?.holidays_in_window?.length ? ` Holidays skipped: ${rule.calendar.holidays_in_window.map(esc).join(", ")}.` : ""}</div>` : `<div class="muted" style="margin-top:12px">Not filed with an authority yet: no service clock.</div>`}
      <h3 class="h3" style="margin-top:16px">History</h3>
      ${t.ledger.length ? `<ul class="timeline">${t.ledger.map(entry).join("")}</ul>` : `<div class="muted">No ledger entries.</div>`}
      ${c ? `<form class="form-row" id="ss-event" style="margin-top:8px"><label>Record what the authority told you<select class="input input-sm" name="event">${t.manual_events.map((e) => `<option value="${e}">${esc((LEDGER_EVENTS[e] || [e])[0])}</option>`).join("")}</select></label>
        <label>When (your local time)<input class="input input-sm" type="datetime-local" name="at"></label><label>Note<input class="input input-sm" name="note" maxlength="500" placeholder="e.g. phoned 311, ref 12345"></label>
        <button class="btn btn-sm btn-primary" type="submit">${icon("plus")}Add to ledger</button></form><div class="small muted" style="margin-top:6px">Entries are append-only: a mistake is corrected by a new entry, never by editing an old one.</div>` : ""}
    </div>`;
}

function bindTimeline(t, after) {
  const form = $("#ss-event");
  if (!form) return;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(form));
    const body = { event: f.event, note: f.note || null, ...(f.at ? { at: new Date(f.at).toISOString() } : {}) };
    try { await api(`/api/service-standards/reports/${t.report.id}/events`, { method: "POST", body: JSON.stringify(body) }); toast("Added to the ledger"); after(); } catch (e) { toast(e.message, false); }
  };
}

/* ---------- Pothole Risk Forecast (freeze-thaw heuristic) ---------- */
const RISK = { high: ["High", "red", "H"], medium: ["Medium", "amber", "M"], low: ["Low", "green", "L"] };
const RISK_COLORS = { high: "#f0616d", medium: "#ffb547", low: "#5bd18b" };
const FACTOR_SHORT = { crack_severity: "Cracks", recent_freeze_thaw: "Past freeze-thaw", forecast_freeze_thaw: "Forecast freeze-thaw",
  moisture: "Moisture", road_class: "Road class", pothole_history: "History" };
const riskPill = (c, insufficient) => { const [t, tone] = RISK[c] || [c, ""]; return `<span class="pill ${tone}"><span class="dot"></span>${esc(t)}</span>${insufficient ? ` <span class="pill" title="Data quality below the minimum: never rated High">insufficient data</span>` : ""}`; };
const damageText = (d) => Object.entries(d?.classes || {}).map(([c, n]) => `${esc(CLASS_LABELS[c] || c)}${n > 1 ? ` ×${n}` : ""}`).join(", ") || "–";
const ftText = (f) => {
  const past = f.recent_available ? `${f.recent_confirmed ?? 0} past${f.recent_potential ? ` (+${f.recent_potential} possible)` : ""}` : "past: no data";
  const ahead = f.forecast_available ? `${f.forecast_potential ?? 0} forecast` : "forecast: no data";
  return `${past} · ${ahead}`;
};
const whereText = (s) => s.municipality || (s.location && !s.location.startsWith("near ") ? s.location : "") || "Location unknown";

function drawRiskMap(id, segs) {
  const el = document.getElementById(id);
  if (!el || !window.L) return;
  const center = segs.length ? [segs[0].lat, segs[0].lon] : [44.6425, -63.578];
  const m = L.map(el, { scrollWheelZoom: true }).setView(center, 14);
  const tone = document.documentElement.dataset.theme === "light" ? "Light" : "Dark";
  const esri = (layer) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${tone}_Gray_${layer}/MapServer/tile/{z}/{y}/{x}`;
  L.tileLayer(esri("Base"), { maxZoom: 19, maxNativeZoom: 16, attribution: "Tiles © Esri, HERE, Garmin, © OpenStreetMap contributors" }).addTo(m);
  L.tileLayer(esri("Reference"), { maxZoom: 19, maxNativeZoom: 16 }).addTo(m);
  // The letter and size carry the category as well as the colour, so it reads without colour vision
  for (const s of segs) {
    const [label, , letter] = RISK[s.category] || ["", "", "?"];
    const size = s.category === "high" ? 26 : s.category === "medium" ? 22 : 18;
    L.marker([s.lat, s.lon], { title: `${s.road_name}: ${label} risk`, icon: L.divIcon({ className: "", iconSize: [size, size],
      html: `<span class="risk-pin risk-${esc(s.category)}" style="--c:${RISK_COLORS[s.category] || "#667085"};width:${size}px;height:${size}px">${letter}</span>` }) })
      .bindPopup(`<b>${esc(s.road_name)}</b><br>${esc(label)} risk · score ${s.score.toFixed(2)}<br>${esc(damageText(s.existing_damage))}<br><a href="#/forecast?segment=${encodeURIComponent(s.segment_id)}">Open segment</a>`)
      .addTo(m);
  }
  const pts = segs.map((s) => [s.lat, s.lon]);
  if (pts.length > 1) m.fitBounds(pts, { padding: [30, 30], maxZoom: 16 });
  maps.push(m);
  setTimeout(() => m.invalidateSize(), 50);
}

async function renderForecast(params) {
  const view = stage();
  const seg = params.get("segment");
  if (seg) return renderForecastSegment(seg);
  const [f, ev, hist] = await Promise.all([api("/api/forecast"), api("/api/forecast/evaluation").catch(() => null), api("/api/forecast/history").catch(() => null)]);
  const cat = params.get("risk") || "all", place = params.get("where") || "";
  const all = f.segments || [];
  const places = [...new Set(all.map(whereText))].sort();
  const list = all.filter((s) => (cat === "all" || s.category === cat) && (!place || whereText(s) === place));
  const run = f.run;
  const runBtn = can("review") ? `<button class="btn btn-sm" id="fc-run">${icon("refresh")}Run forecast now</button>` : "";
  const banner = `<div class="callout amber section-tight">${icon("alert")}<div><b>Heuristic estimate, not a validated prediction.</b> ${esc(f.disclaimer)} Weights: ${Object.entries(f.weights).map(([k, w]) => `${esc(FACTOR_SHORT[k] || k)} ${Math.round(w * 100)}%`).join(" · ")}.</div></div>`;
  if (!run) {
    view.innerHTML = `${banner}<div class="card">${emptyState("activity", "No forecast yet", "A forecast scores every road segment with crack sightings once trips produce them. The scheduler runs it every few hours.", runBtn)}</div>`;
    bindForecastRun();
    return;
  }
  const s = f.summary;
  const weatherNote = { ok: "ECCC weather available", partial: "Some ECCC weather missing", unavailable: "ECCC weather unavailable", none: "No segments needed weather" }[run.weather_status] || run.weather_status;
  view.innerHTML = `${banner}
    <div class="grid kpis">
      <div class="card kpi"><div class="label">Segments analysed<span class="ic blue">${icon("map")}</span></div><div class="value">${s.total}</div><div class="hint">Road segments with crack sightings</div></div>
      <div class="card kpi"><div class="label">High risk<span class="ic red">${icon("alert")}</span></div><div class="value">${s.high}</div><div class="hint">Score ≥ ${f.thresholds.high}</div></div>
      <div class="card kpi"><div class="label">Medium risk<span class="ic amber">${icon("activity")}</span></div><div class="value">${s.medium}</div><div class="hint">${s.insufficient_data ? `${s.insufficient_data} with insufficient data` : `Score ≥ ${f.thresholds.medium}`}</div></div>
      <div class="card kpi"><div class="label">Low risk<span class="ic green">${icon("check")}</span></div><div class="value">${s.low}</div><div class="hint">Below ${f.thresholds.medium}</div></div>
    </div>
    <div class="card section"><div class="card-head"><h2>Forecast</h2><span class="desc">Generated ${esc(new Date(run.created_at).toLocaleString())} · next ${run.horizon_days} days (${esc(run.period_start)} to ${esc(run.period_end)}) · ${esc(weatherNote)}</span><span class="spacer"></span>${runBtn}</div>
      <div class="card-body"><div class="toolbar" style="margin-bottom:12px">
        <div class="tabs" role="group" aria-label="Filter by risk category">${["all", "high", "medium", "low"].map((c) => `<button type="button" aria-pressed="${c === cat}" class="${c === cat ? "on" : ""}" data-c="${c}">${c === "all" ? "All" : RISK[c][0]}<span class="n">${c === "all" ? all.length : s[c]}</span></button>`).join("")}</div>
        <label class="small muted">Location <select class="input input-sm" id="fc-where"><option value="">All locations</option>${places.map((p) => `<option ${p === place ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>
      </div>
      <div class="legend" style="margin-bottom:8px">${Object.entries(RISK).map(([k, [t, , l]]) => `<span><span class="risk-pin risk-${k}" style="--c:${RISK_COLORS[k]};width:16px;height:16px;font-size:9px;vertical-align:middle;margin-right:6px">${l}</span>${t}</span>`).join("")}</div>
      <div id="risk-map" class="map" style="height:360px;min-height:300px"></div></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>Road segment</th><th>Location</th><th>Existing damage</th><th>Freeze-thaw days</th><th style="width:16%">Risk score</th><th>Category</th><th>Main factors</th></tr></thead><tbody>
      ${list.length ? list.map((x) => `<tr class="click" data-seg="${esc(x.segment_id)}" tabindex="0"><td><b>${esc(x.road_name)}</b><div class="muted small mono">${esc(x.segment_id)}</div></td>
        <td>${esc(whereText(x))}</td><td>${damageText(x.existing_damage)}<div class="muted small">${x.existing_damage.observations} sighting(s), ${x.existing_damage.trips} trip(s)</div></td>
        <td class="small">${esc(ftText(x.freeze_thaw))}</td>
        <td><div class="meter-row"><div class="meter"><span style="width:${Math.round(x.score * 100)}%" class="${RISK[x.category]?.[1] || ""}"></span></div><span class="mono small">${x.score.toFixed(2)}</span></div><div class="muted small">data quality ${pct(x.data_quality)}</div></td>
        <td>${riskPill(x.category, x.insufficient_data)}</td>
        <td class="small">${x.main_factors.map((m) => esc(FACTOR_SHORT[m.factor] || m.label)).join(", ") || "–"}</td></tr>`).join("")
        : `<tr><td colspan="7">${emptyState("search", "No segments match", "Try another risk category or location.")}</td></tr>`}</tbody></table></div></div>
    <div class="grid two section">
      ${forecastEvaluationCard(ev)}
      ${forecastHistoryCard(hist)}
    </div>`;
  $$(".tabs button", view).forEach((b) => b.onclick = () => location.hash = `#/forecast?risk=${b.dataset.c}${place ? `&where=${encodeURIComponent(place)}` : ""}`);
  $("#fc-where", view).onchange = (e) => location.hash = `#/forecast?risk=${cat}${e.target.value ? `&where=${encodeURIComponent(e.target.value)}` : ""}`;
  $$("tr[data-seg]", view).forEach((tr) => { const go = () => location.hash = `#/forecast?segment=${encodeURIComponent(tr.dataset.seg)}`; tr.onclick = go; tr.onkeydown = (e) => { if (e.key === "Enter") go(); }; });
  bindForecastRun();
  drawRiskMap("risk-map", list);
}

function forecastEvaluationCard(ev) {
  if (!ev) return `<div class="card"><div class="card-head"><h2>How the forecast has done</h2></div><div class="card-body muted">Evaluation unavailable.</div></div>`;
  const fact = (k, v) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;
  return `<div class="card"><div class="card-head"><h2>How the forecast has done</h2><span class="spacer"></span>${ev.validated ? `<span class="pill green">${ev.with_outcome} outcomes</span>` : `<span class="pill amber">not validated</span>`}</div><div class="card-body">
    ${ev.message ? `<div class="callout blue section-tight">${icon("alert")}<div>${esc(ev.message)}</div></div>` : ""}
    <div class="facts">${fact("High-risk predictions", ev.high_predictions)}${fact("Potholes that developed", ev.confirmed_potholes)}${fact("High and confirmed", ev.high_confirmed)}
      ${fact("False alarms", ev.false_alarms)}${fact("Missed", ev.missed)}${fact("No trip passed", ev.not_observed)}${fact("Still in period", ev.pending)}
      ${fact("Precision (High)", ev.validated && ev.high_precision != null ? pct(ev.high_precision) : "<span class=\"muted\">not yet</span>")}</div>
    ${(ev.runs || []).length > 1 ? `<h3 class="h3" style="margin-top:14px">By run</h3><div class="table-wrap"><table class="table"><thead><tr><th>Run</th><th>Kind</th><th>High</th><th>Confirmed</th><th>False alarms</th><th>Missed</th></tr></thead><tbody>${ev.runs.slice(0, 8).map((r) => `<tr><td class="mono small">#${r.run_id} · ${esc(new Date(r.created_at).toLocaleDateString())}</td><td>${esc(r.kind)}</td><td>${r.high_predictions}</td><td>${r.high_confirmed}</td><td>${r.false_alarms}</td><td>${r.missed}</td></tr>`).join("")}</tbody></table></div>` : ""}
  </div></div>`;
}

function forecastHistoryCard(h) {
  if (!h) return `<div class="card"><div class="card-head"><h2>Deterioration history</h2></div><div class="card-body muted">History unavailable.</div></div>`;
  return `<div class="card"><div class="card-head"><h2>Deterioration history</h2><span class="spacer"></span>${h.sufficient ? `<span class="pill green">enough data</span>` : `<span class="pill amber">insufficient data</span>`}</div><div class="card-body">
    ${h.message ? `<div class="callout blue section-tight">${icon("alert")}<div>${esc(h.message)}</div></div>` : ""}
    <div class="facts"><div class="fact"><div class="k">Segments</div><div class="v">${h.segments}</div></div>
      <div class="fact"><div class="k">Seen on 2+ trips</div><div class="v">${h.segments_seen_on_2_or_more_trips}</div></div>
      <div class="fact"><div class="k">Crack → pothole</div><div class="v">${h.crack_to_pothole_pairs}</div></div>
      <div class="fact"><div class="k">Median days</div><div class="v">${h.sufficient && h.median_days_crack_to_pothole != null ? h.median_days_crack_to_pothole : "–"}</div></div></div>
  </div></div>`;
}

function bindForecastRun() {
  const b = $("#fc-run");
  if (!b) return;
  b.onclick = async () => {
    b.disabled = true; b.innerHTML = `${icon("refresh", "spin")}Running…`;
    try {
      const job = await api("/api/forecast/run?force=true", { method: "POST" });
      if (job.status === "failed") throw new Error(job.error || "forecast failed");
      toast(job.status === "done" ? `Forecast updated: ${job.result?.segments ?? 0} segment(s)` : "Forecast queued");
      router();
    } catch (e) { toast(e.message, false); b.disabled = false; b.innerHTML = `${icon("refresh")}Run forecast now`; }
  };
}

async function renderForecastSegment(id) {
  const view = stage();
  const d = await api(`/api/forecast/segments/${encodeURIComponent(id)}`);
  const s = d.segment, p = d.prediction, det = d.deterioration;
  $("#crumb-current").textContent = `Pothole risk / ${s.road_name}`;
  const back = `<a href="#/forecast" class="btn btn-ghost btn-sm" style="margin-left:-10px">${icon("arrow", "flip")}All segments</a>`;
  const factorRow = (f) => `<tr><td><b>${esc(f.label)}</b><div class="muted small">${esc(Object.entries(f.raw || {}).filter(([k, v]) => v !== null && typeof v !== "object" && k !== "start" && k !== "end").map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · "))}</div></td>
    <td class="mono">${f.available ? f.value.toFixed(2) : `<span class="muted">no data</span>`}</td><td class="mono">${pct(f.weight)}</td><td class="mono">${f.contribution.toFixed(3)}</td><td class="mono">${pct(f.coverage)}</td></tr>`;
  const wx = [...(d.weather || [])].reverse().map((w) => `<tr><td class="mono small nowrap">${esc(w.date)}<div class="muted">${esc(w.kind)}</div></td><td class="mono">${w.t_min ?? "–"}</td><td class="mono">${w.t_max ?? "–"}</td>
    <td class="mono small">${w.precip_mm != null ? `${w.precip_mm} mm` : w.precip_chance != null ? `${w.precip_chance}%` : w.precip_expected ? "expected" : "–"}${w.snow_cm ? ` · ${w.snow_cm} cm snow` : ""}</td>
    <td>${w.status === "confirmed" ? `<span class="pill red">freeze-thaw</span>` : w.status === "potential" ? `<span class="pill amber">possible</span>` : w.status === "missing" ? `<span class="pill">no data</span>` : `<span class="muted small">none</span>`}</td></tr>`).join("");
  view.innerHTML = `${back}
    <div class="callout amber section-tight" style="margin-top:8px">${icon("alert")}<div>${esc(d.disclaimer)}</div></div>
    <div class="card"><div class="card-head"><h2>${esc(s.road_name)} <span class="muted mono small">${esc(s.id)}</span></h2>${p ? riskPill(p.category, p.insufficient_data) : ""}<span class="spacer"></span><span class="muted small">${esc(whereText(s))} · ${esc(s.road_class || "road class unknown")}</span></div>
      <div class="card-body">
        ${p ? `<div class="callout blue section-tight">${icon("sparkle")}<div><b>Why:</b> ${esc(p.explanation)}</div></div>
        <div class="facts"><div class="fact"><div class="k">Risk score</div><div class="v">${p.score.toFixed(2)}</div></div><div class="fact"><div class="k">Data quality</div><div class="v">${pct(p.data_quality)}</div></div>
          <div class="fact"><div class="k">Period</div><div class="v small">${esc(p.period_start)} → ${esc(p.period_end)}</div></div><div class="fact"><div class="k">Predicted</div><div class="v small">${esc(new Date(p.predicted_at).toLocaleString())}</div></div></div>
        <h3 class="h3" style="margin-top:16px">Contributing factors</h3>
        <div class="table-wrap"><table class="table"><thead><tr><th>Factor</th><th>Value (0–1)</th><th>Weight</th><th>Contribution</th><th>Data coverage</th></tr></thead><tbody>${p.factors.map(factorRow).join("")}</tbody></table></div>
        <h3 class="h3" style="margin-top:16px">Data sources</h3><ul class="small">${Object.entries(p.data_sources).map(([k, v]) => `<li><b>${esc(k.replace(/_/g, " "))}:</b> ${esc(v)}</li>`).join("")}</ul>` : `<div class="muted">No live prediction for this segment yet.</div>`}
      </div></div>
    <div class="grid two section">
      <div class="card"><div class="card-head"><h2>Crack and pothole sightings</h2><span class="desc">${det ? `${det.observations} sighting(s) on ${det.trips} trip(s)` : ""}</span></div><div class="card-body">
        ${det ? `<div class="facts section-tight"><div class="fact"><div class="k">First crack</div><div class="v small">${det.first_crack_at ? esc(new Date(det.first_crack_at).toLocaleDateString()) : "–"}</div></div>
          <div class="fact"><div class="k">First pothole</div><div class="v small">${det.first_pothole_at ? esc(new Date(det.first_pothole_at).toLocaleDateString()) : "none seen"}</div></div>
          <div class="fact"><div class="k">Crack → pothole</div><div class="v">${det.days_crack_to_pothole != null ? `${det.days_crack_to_pothole} days` : "–"}</div></div>
          <div class="fact"><div class="k">Class changes</div><div class="v small">${det.class_changes.map((c) => esc(c.class_code)).join(" → ")}</div></div></div>
          ${det.sufficient ? "" : `<div class="callout blue section-tight">${icon("alert")}<div>Seen on one trip only: there is no deterioration trend to show yet.</div></div>`}` : ""}
        <ul class="feed">${(d.events || []).map((e) => `<li><img src="/api/media/${encodeURIComponent(e.id)}.jpg" alt="Privacy-blurred frame of ${esc(CLASS_LABELS[e.class_code] || e.class_code)}" loading="lazy" style="width:88px;height:56px;object-fit:cover;border-radius:6px;flex:none" onerror="this.style.visibility='hidden'">
          <div style="flex:1;min-width:0"><b>${esc(CLASS_LABELS[e.class_code] || e.class_code)}</b> <span class="muted small">confidence ${Number(e.confidence).toFixed(2)}</span>
          <div class="muted small">${esc(new Date(e.captured_at).toLocaleString())} · trip ${esc(e.trip_id)}${e.report_id ? ` · <a href="#/reports?id=${e.report_id}">report #${e.report_id}</a>` : ""}</div></div></li>`).join("")}</ul>
      </div></div>
      <div class="card"><div class="card-head"><h2>Weather (ECCC)</h2><span class="desc">Forecast and station records, newest first</span></div><div class="card-body flush">
        ${wx ? `<div class="table-wrap" style="max-height:420px;overflow:auto"><table class="table compact"><thead><tr><th>Date</th><th>Min °C</th><th>Max °C</th><th>Precip.</th><th>Freeze/thaw</th></tr></thead><tbody>${wx}</tbody></table></div>`
          : `<div class="card-body">${emptyState("cloud", "No weather data stored", "ECCC data was unavailable for this segment; nothing was estimated in its place.")}</div>`}
      </div></div>
    </div>
    ${(d.history || []).length ? `<div class="card section"><div class="card-head"><h2>Earlier predictions and outcomes</h2></div><div class="table-wrap"><table class="table"><thead><tr><th>Predicted</th><th>Kind</th><th>Category</th><th>Score</th><th>Period</th><th>Outcome</th></tr></thead><tbody>
      ${d.history.map((h) => `<tr><td class="small">${esc(new Date(h.predicted_at).toLocaleString())}</td><td>${esc(h.run_kind)}</td><td>${riskPill(h.category)}</td><td class="mono">${h.score.toFixed(2)}</td><td class="small">${esc(h.period_start)} → ${esc(h.period_end)}</td><td>${esc((h.outcome || "pending").replace(/_/g, " "))}</td></tr>`).join("")}</tbody></table></div></div>` : ""}`;
}

/* ---------- Users (admin) ---------- */
async function renderUsers() {
  const view = stage();
  const users = await api("/api/users");
  const roles = ["admin", "reviewer", "driver", "viewer"];
  view.innerHTML = `
    <div class="card"><div class="card-head"><h2>Add a user</h2><span class="desc">Password sign-in. With single sign-on, people are added on their first sign-in.</span></div>
      <form class="card-body form-row" id="new-user" autocomplete="off">
        <label>Email<input class="input" name="email" type="email" required maxlength="120"></label>
        <label>Name<input class="input" name="name" maxlength="80"></label>
        <label>Role<select class="input" name="role">${roles.map((r) => `<option ${r === "viewer" ? "selected" : ""}>${r}</option>`).join("")}</select></label>
        <label>Initial password<input class="input" name="password" type="password" minlength="12" autocomplete="new-password" placeholder="12+ characters, Aa1"></label>
        <button class="btn btn-primary" type="submit">${icon("plus")}Add user</button>
      </form></div>
    <div class="card section"><div class="card-head"><h2>Users</h2><span class="pill">${users.length}</span><span class="spacer"></span><span class="muted small">admin · reviewer (approve) · driver (upload) · viewer (read)</span></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>User</th><th>Role</th><th>Sign-in</th><th>Last sign-in</th><th>Status</th></tr></thead><tbody>
      ${users.map((u) => `<tr data-id="${u.id}"><td><b>${esc(u.name || u.email)}</b><div class="muted small">${esc(u.email)}</div></td>
        <td><select class="input input-sm role" aria-label="Role of ${esc(u.email)}" ${u.id === session.user?.id ? "disabled" : ""}>${roles.map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
        <td class="small">${u.sso ? "Single sign-on" : "Password"}</td><td class="muted small">${u.last_login ? ago(u.last_login) : "never"}</td>
        <td>${u.id === session.user?.id ? `<span class="pill blue">You</span>` : `<button class="btn btn-sm ${u.active ? "" : "btn-primary"} toggle">${u.active ? "Disable" : "Enable"}</button>`}</td></tr>`).join("")}</tbody></table></div></div>`;
  $("#new-user").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    if (!f.password) delete f.password;
    try { await api("/api/users", { method: "POST", body: JSON.stringify(f) }); toast(`Added ${f.email}`); renderUsers(); } catch (e) { toast(e.message, false); }
  };
  $$("tr[data-id]").forEach((tr) => {
    const id = tr.dataset.id;
    const role = $(".role", tr), toggle = $(".toggle", tr);
    if (role) role.onchange = async () => { try { await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify({ role: role.value }) }); toast("Role updated"); } catch (e) { toast(e.message, false); renderUsers(); } };
    if (toggle) toggle.onclick = async () => { try { await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify({ active: toggle.textContent === "Enable" }) }); renderUsers(); } catch (e) { toast(e.message, false); } };
  });
}

/* ---------- sign-in ---------- */
function showLogin(message = "") {
  const box = $("#login");
  if (!box.hidden && !message) return;
  session.user = null;
  box.hidden = false;
  document.body.classList.add("signed-out");
  box.innerHTML = `<form class="login-card card" id="login-form">
      <div class="workspace" style="padding:0;margin-bottom:4px"><div class="brand-mark"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M8 21l3-18M16 21l-3-18M12 6v2M12 11v2M12 16v2"/></svg></div><div class="workspace-text"><div class="brand-name">RoadSense</div><div class="brand-sub">Sign in to the Halifax pilot</div></div></div>
      ${message ? `<div class="callout red">${icon("alert")}<div>${esc(message)}</div></div>` : ""}
      <label>Email<input class="input" name="email" type="email" autocomplete="username" required></label>
      <label>Password<input class="input" name="password" type="password" autocomplete="current-password" required></label>
      <button class="btn btn-primary btn-lg" type="submit">${icon("lock")}Sign in</button>
      ${session.sso ? `<div class="or"><span>or</span></div><a class="btn btn-lg" href="/auth/oidc/login">${icon("building")}Sign in with your organization</a>` : ""}
      <button class="btn btn-ghost btn-sm login-back" type="button" id="login-back">← Back to home</button>
    </form>`;
  $("#login-back").onclick = () => { box.hidden = true; box.innerHTML = ""; showLanding(); };
  $("#login-form input[name=email]").focus();
  $("#login-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify(f) });
      await loadSession();
      box.hidden = true; box.innerHTML = ""; hideLanding(); document.body.classList.remove("signed-out");
      $("#sign-out").hidden = !session.authEnabled;
      router();
    } catch (e) { showLogin(e.message); }
  };
}

/* ---------- public home, shown before sign-in ---------- */
function showLanding() {
  const page = $("#landing");
  if (!page) { showLogin(); return; }
  session.user = null;
  document.body.classList.add("signed-out");
  page.hidden = false;
  page.scrollTop = 0;
  const open = () => { hideLanding(); showLogin(); };
  $$("#landing [data-signin]").forEach((b) => { b.onclick = open; });
  $$("#landing [data-sso]").forEach((b) => { b.onclick = () => { if (session.sso) location.href = "/auth/oidc/login"; else open(); }; });
  $$('#landing a[href^="#"]').forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); page.querySelector(a.getAttribute("href"))?.scrollIntoView({ behavior: "smooth" }); };
  });
  wireLandingMotion(page);
}

/* Pointer and scroll effects for the home page. Wired once; CSS turns them off under reduced motion. */
function wireLandingMotion(page) {
  if (page.dataset.wired) return;
  page.dataset.wired = "1";
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // hero: the camera view tilts toward the pointer and a headlight follows it
  const tilt = $("#ld-tilt"), scene = tilt?.querySelector(".ld-scene");
  if (tilt && scene && !still) {
    tilt.addEventListener("pointermove", (e) => {
      const r = scene.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      scene.style.setProperty("--ld-rx", `${(x - 0.5) * 10}deg`);
      scene.style.setProperty("--ld-ry", `${(0.5 - y) * 8}deg`);
      scene.style.setProperty("--ld-hx", `${x * 100}%`);
      scene.style.setProperty("--ld-hy", `${y * 100}%`);
    });
    tilt.addEventListener("pointerleave", () => ["--ld-rx", "--ld-ry", "--ld-hx", "--ld-hy"].forEach((p) => scene.style.removeProperty(p)));
  }

  // primary buttons lean toward the pointer
  if (!still) $$("#landing .ld-magnet").forEach((b) => {
    b.addEventListener("pointermove", (e) => {
      const r = b.getBoundingClientRect();
      b.style.setProperty("--ld-mx", `${(e.clientX - r.left - r.width / 2) * 0.18}px`);
      b.style.setProperty("--ld-my", `${(e.clientY - r.top - r.height / 2) * 0.3}px`);
    });
    b.addEventListener("pointerleave", () => { b.style.removeProperty("--ld-mx"); b.style.removeProperty("--ld-my"); });
  });

  // cards: a soft spotlight under the pointer
  $$("#landing .ld-spot").forEach((c) => c.addEventListener("pointermove", (e) => {
    const r = c.getBoundingClientRect();
    c.style.setProperty("--ld-sx", `${e.clientX - r.left}px`);
    c.style.setProperty("--ld-sy", `${e.clientY - r.top}px`);
  }));

  // before/after redaction slider
  const cmp = $("#ld-compare"), range = $("#ld-cmp-range");
  if (cmp && range) range.addEventListener("input", () => cmp.style.setProperty("--ld-cut", `${range.value}%`));

  // sections fade up as they enter
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) { en.target.classList.add("is-in"); io.unobserve(en.target); }
  }), { root: page, threshold: 0.15 });
  $$("#landing .ld-reveal").forEach((el) => io.observe(el));

  // scroll: top progress line, and the car driving through the four steps
  const bar = $("#ld-progress"), journey = $("#ld-journey"), stops = $$("#landing .ld-stop");
  let ticking = false;
  const onScroll = () => {
    ticking = false;
    const max = page.scrollHeight - page.clientHeight;
    bar?.style.setProperty("--ld-p", max > 0 ? (page.scrollTop / max).toFixed(4) : 0);
    if (!journey) return;
    const r = journey.getBoundingClientRect(), vh = page.clientHeight;
    const j = still ? 1 : Math.min(1, Math.max(0, (vh * 0.85 - r.top) / (r.height + vh * 0.35)));
    journey.style.setProperty("--ld-j", j.toFixed(4));
    stops.forEach((s, i) => s.classList.toggle("is-lit", j >= (i + 0.35) / stops.length));
  };
  page.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();
}

function hideLanding() { const page = $("#landing"); if (page) page.hidden = true; }

async function loadSession() {
  const me = await (await fetch("/api/me", { credentials: "same-origin" })).json();
  session.sso = me.sso; session.authEnabled = me.auth_enabled;
  if (!me.authenticated) { session.user = null; session.perms = []; return false; }
  Object.assign(session, { user: me.user, csrf: me.csrf, perms: me.permissions });
  const u = me.user;
  $("#user-chip").innerHTML = `<span class="avatar pill ${ROLE_TONE[u.role] || ""}">${esc((u.name || u.email)[0].toUpperCase())}</span><span class="user-text hide-sm"><span class="user-name">${esc(u.name || u.email)}</span><span class="user-role">${esc(u.role)}</span></span>`;
  $("#user-chip").hidden = false;
  $("#user-menu-email").textContent = u.email;
  $$("[data-perm]").forEach((el) => { el.hidden = !can(el.dataset.perm); });
  return true;
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  location.hash = "#/overview"; location.reload();
}

/* ---------- command palette (Ctrl/⌘ K) ---------- */
const palette = {
  el: $("#palette"), q: $("#palette-q"), list: $("#palette-list"), pending: [], items: [], sel: 0, opener: null,
  commands() {
    const nav = [["overview", "grid"], ["trips", "video"], ["review", "inbox"], ["reports", "file"], ["map", "map"],
      ["conditions", "activity"], ["forecast", "alert"], ["activity", "activity"], ["standards", "clock"], ["authorities", "building"], ["system", "shield"],
      ...(can("admin") ? [["users", "user"]] : [])]
      .map(([r, ic]) => ({ group: "Go to", label: ROUTES[r][0], hint: ROUTES[r][1], icon: ic, run: () => (location.hash = `#/${r}`) }));
    const acts = [
      { group: "Actions", label: "New trip", hint: "Upload a dashcam video", icon: "plus", run: () => { location.hash = "#/trips"; setTimeout(() => $("#video")?.click(), 300); } },
      { group: "Actions", label: "Toggle theme", hint: "Light / dark", icon: "moon", run: toggleTheme },
      ...(session.authEnabled && session.user ? [{ group: "Actions", label: "Sign out", hint: session.user.email, icon: "lock", run: signOut }] : []),
      { group: "Actions", label: "Keyboard shortcuts", hint: "?", icon: "keyboard", run: openShortcuts },
    ];
    const reports = this.pending.map((r) => ({ group: "Awaiting review", label: `${r.class_name} #${r.id}`, hint: r.address || "", icon: "inbox", run: () => (location.hash = `#/review?id=${r.id}`) }));
    // Typing a report number ("42" or "#42") offers a direct jump, even to reports that are not waiting for review
    const n = this.q.value.trim().match(/^#?(\d{1,7})$/)?.[1];
    const jump = n ? [{ group: "Jump to", label: `Open report #${n}`, hint: "Any status", icon: "file", always: true, run: () => (location.hash = `#/reports?id=${n}`) }] : [];
    return [...jump, ...nav, ...acts, ...reports];
  },
  open() {
    this.opener = document.activeElement;
    this.el.hidden = false; this.q.value = ""; this.sel = 0; this.render(); this.q.focus();
  },
  close() { this.el.hidden = true; this.opener?.focus?.(); },
  render() {
    const term = this.q.value.trim().toLowerCase();
    this.items = this.commands().filter((c) => c.always || !term || `${c.label} ${c.hint} ${c.group}`.toLowerCase().includes(term));
    this.sel = Math.min(this.sel, Math.max(this.items.length - 1, 0));
    let group = "";
    this.list.innerHTML = this.items.length ? this.items.map((c, i) => {
      const head = c.group !== group ? `<li class="palette-group" role="presentation">${esc((group = c.group))}</li>` : "";
      return `${head}<li class="palette-item" role="option" id="pal-${i}" data-i="${i}" aria-selected="${i === this.sel}">${icon(c.icon)}<span>${esc(c.label)}</span><span class="hint">${esc(c.hint)}</span></li>`;
    }).join("") : `<li class="palette-empty">No results for “${esc(term)}”</li>`;
    this.q.setAttribute("aria-activedescendant", this.items.length ? `pal-${this.sel}` : "");
    $(`#pal-${this.sel}`)?.scrollIntoView({ block: "nearest" });
  },
  run(i) { const c = this.items[i]; if (!c) return; this.close(); c.run(); },
};
palette.q.oninput = () => { palette.sel = 0; palette.render(); };
palette.q.onkeydown = (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); palette.sel = (palette.sel + 1) % Math.max(palette.items.length, 1); palette.render(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palette.sel = (palette.sel - 1 + palette.items.length) % Math.max(palette.items.length, 1); palette.render(); }
  else if (e.key === "Enter") { e.preventDefault(); palette.run(palette.sel); }
  else if (e.key === "Escape") palette.close();
};
palette.list.onclick = (e) => { const li = e.target.closest(".palette-item"); if (li) palette.run(Number(li.dataset.i)); };
palette.list.onmousemove = (e) => { const li = e.target.closest(".palette-item"); if (li && Number(li.dataset.i) !== palette.sel) { palette.sel = Number(li.dataset.i); palette.render(); } };
palette.el.onclick = (e) => { if (e.target === palette.el) palette.close(); };
$("#open-palette").onclick = () => palette.open();
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !$("dialog[open]")) { e.preventDefault(); palette.el.hidden ? palette.open() : palette.close(); }
  else if (e.key === "Escape" && !$("#user-menu").hidden) setUserMenu(false, true);
  else if (e.key === "Escape" && $("#sidebar").classList.contains("open")) setDrawer(false);
});
if (/Mac|iPhone|iPad/.test(navigator.platform)) $("#mod-key").textContent = "⌘";

function toggleTheme() {
  const dark = document.documentElement.dataset.theme !== "light"; // Night Shift is the default; Daylight is opt-in
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("rs-theme", document.documentElement.dataset.theme); } catch (e) {}
  if (maps.length && session.user) router(); // map markers take their outline from the theme
}

/* ---------- boot ---------- */
/* Phone drawer: while it is off-screen its links are inert (not tabbable); opening it moves focus in, closing hands it back */
const PHONE = matchMedia("(max-width: 760px)");
function setDrawer(open, restore = true) {
  const sb = $("#sidebar"), had = sb.contains(document.activeElement);
  sb.classList.toggle("open", open);
  sb.inert = PHONE.matches && !open;
  $("#menu").setAttribute("aria-expanded", open);
  if (open) ($("#nav a.active") || $("#nav a"))?.focus();
  else if (had && restore && PHONE.matches) $("#menu").focus();
}
PHONE.addEventListener("change", () => setDrawer(false, false));
setDrawer(false, false);
/* Account menu: a disclosure (button + links), closed by Escape, an outside click or focus leaving it */
function setUserMenu(open, restore = false) {
  const m = $("#user-menu");
  m.hidden = !open;
  $("#user-chip").setAttribute("aria-expanded", open);
  if (open) $("a:not([hidden]), button", m)?.focus();
  else if (restore) $("#user-chip").focus();
}
$("#menu").onclick = () => setDrawer(!$("#sidebar").classList.contains("open"));
$("#scrim").onclick = () => setDrawer(false);
$("#theme").onclick = toggleTheme;
$("#shortcuts-btn").onclick = openShortcuts;
$("#user-chip").onclick = () => setUserMenu($("#user-menu").hidden);
$("#user-menu").addEventListener("focusout", (e) => { if (!e.relatedTarget?.closest?.("#user-menu, #user-chip")) setUserMenu(false); });
$("#user-menu").addEventListener("click", (e) => { if (e.target.closest("a")) setUserMenu(false); });
document.addEventListener("click", (e) => { if (!e.target.closest("#user-chip, #user-menu") && !$("#user-menu").hidden) setUserMenu(false); });
$("#sign-out").onclick = signOut;
window.addEventListener("hashchange", () => { if (session.user) router(); });
(async () => {
  try {
    if (!(await loadSession())) { if (location.hash.includes("signin")) showLogin(); else showLanding(); return; }
  } catch (e) { main.innerHTML = errorState(e); return; }
  $("#sign-out").hidden = !session.authEnabled;
  if (location.hash.includes("new=1")) setTimeout(() => $("#video")?.click(), 300);
  router();
  setInterval(() => { if (session.user) refreshChrome(); }, 15000);
})();
