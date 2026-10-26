// TwoKey dashboard. Plain JS, no build step. Talks only to the FastAPI services.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupees = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
const FINAL = new Set(["RELEASED", "RECALLED"]);
let accounts = [];
let ledgerNow = 0;
let ledgerSkew = 0; // ledger clock minus browser clock, so countdowns survive fast-forward

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const d = data?.detail;
    const msg = typeof d === "object" && !Array.isArray(d) ? `${d.code}: ${d.error}` : Array.isArray(d) ? d.map((e) => e.msg).join("; ") : r.statusText;
    throw new Error(msg);
  }
  return data;
}

function log(msg, kind = "") {
  const li = document.createElement("li");
  li.className = kind;
  li.innerHTML = `<time>${new Date().toLocaleTimeString()}</time>${msg}`;
  $("#log").prepend(li);
}

// ---- tabs -------------------------------------------------------------------
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + b.dataset.tab; });
  if (b.dataset.tab === "ledger") refreshChain();
  if (b.dataset.tab === "sim") loadSimulation();
  history.replaceState(null, "", "#" + b.dataset.tab);
}));

// ---- accounts and payment form ------------------------------------------------
async function refreshAccounts() {
  accounts = await api("/api/accounts");
  const payers = accounts.filter((a) => a.bank === "PayerBank");
  const payees = accounts.filter((a) => a.bank === "PayeeBank" && a.vpa !== "layer.one@payeebank");
  const fill = (sel, list) => {
    const keep = sel.value;
    sel.innerHTML = list.map((a) => `<option value="${esc(a.vpa)}">${esc(a.name)} · ${esc(a.vpa)}</option>`).join("");
    if (list.some((a) => a.vpa === keep)) sel.value = keep;
  };
  fill($("#payer"), payers);
  fill($("#payee"), payees);
  $("#balances").innerHTML = "<tr><th>Account</th><th>Bank</th><th class='num'>Available</th><th class='num'>Held</th></tr>" +
    accounts.filter((a) => a.vpa !== "layer.one@payeebank").map((a) => `<tr><td>${esc(a.name)}<br><span class="mono">${esc(a.vpa)} · ${a.ageDays}d old</span></td>
      <td>${esc(a.bank)}</td><td class="num">${rupees(a.balancePaise)}</td><td class="num">${a.heldPaise ? "<b>" + rupees(a.heldPaise) + "</b>" : "–"}</td></tr>`).join("");
}

function formBody() {
  return { payerVpa: $("#payer").value, payeeVpa: $("#payee").value, amountPaise: Math.round(Number($("#amount").value) * 100), purpose: $("#purpose").value };
}

function decisionHtml(d) {
  const hold = d.holdSeconds ? ` · hold ${fmtDur(d.holdSeconds)}` : " · settles instantly";
  const why = d.reasonCodes.length
    ? d.reasonCodes.map((c, i) => `<span class="code-chip" title="${esc(d.explanations[i])}">${esc(c)}</span>`).join("")
    : d.exemption ? `<span class="code-chip">exempt: ${esc(d.exemption)}</span>` : "";
  return `<span class="tier ${d.tier}">${d.tier}</span>${hold}<br>${why}`;
}

let previewTimer;
function preview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const b = formBody();
    if (!b.amountPaise || b.amountPaise <= 0) { $("#preview").textContent = ""; return; }
    try { $("#preview").innerHTML = "Risk check: " + decisionHtml(await api("/api/risk/score", b)); }
    catch (e) { $("#preview").textContent = e.message; }
  }, 200);
}
["#payer", "#payee", "#amount", "#purpose"].forEach((s) => $(s).addEventListener("input", preview));

async function pay(body) {
  const payee = accounts.find((a) => a.vpa === body.payeeVpa);
  try {
    const r = await api("/api/upi/pay", body);
    const what = r.status === "HELD"
      ? `held until ${new Date((r.holdUntil - ledgerSkew) * 1000).toLocaleTimeString()} on the ledger`
      : "credited instantly";
    log(`${rupees(body.amountPaise)} to ${esc(payee?.name)}: ${decisionHtml(r)} → ${what} <span class="mono">${r.txnRef}</span>`, r.status === "HELD" ? "bad" : "ok");
    return r;
  } catch (e) {
    log(`Payment failed: ${esc(e.message)}`, "bad");
  } finally {
    await refresh();
    preview();
  }
}

$("#pay-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#pay-form .primary");
  btn.disabled = true;
  await pay(formBody());
  btn.disabled = false;
});

const SCENARIOS = {
  nephew: ["meena@payerbank", "ravi.nephew@payeebank", 40000, 1],
  mule: ["meena@payerbank", "safe.verify@payeebank", 40000, 1],
  landlord: ["meena@payerbank", "landlord.sharma@payeebank", 40000, 1],
  smurf: ["arjun@payerbank", "job.offer.desk@payeebank", 9500, 4],
};
document.querySelectorAll("[data-scn]").forEach((b) => b.addEventListener("click", async () => {
  const [payer, payee, amt, times] = SCENARIOS[b.dataset.scn];
  $("#payer").value = payer; $("#payee").value = payee; $("#amount").value = amt; $("#purpose").value = "P2P";
  b.disabled = true;
  for (let i = 0; i < times; i++) await pay({ payerVpa: payer, payeeVpa: payee, amountPaise: amt * 100, purpose: "P2P" });
  b.disabled = false;
}));

// ---- held credits -------------------------------------------------------------
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

const ACTIONS = {
  report: ["/api/helpline/report", "Report to 1930"],
  payerApprove: ["/api/bank/PayerBank/approve-release", "Approve early release"],
  payerDispute: ["/api/bank/PayerBank/dispute", "Raise dispute"],
  payeeConfirm: ["/api/bank/PayeeBank/confirm-recall", "Confirm recall"],
  payeeApprove: ["/api/bank/PayeeBank/approve-release", "Approve early release"],
  payeeDispute: ["/api/bank/PayeeBank/dispute", "Raise dispute"],
  npciRelease: ["/api/npci/resolve", "Rule: release to payee"],
  npciRecall: ["/api/npci/resolve", "Rule: return to payer"],
  "unilateral-release": ["/api/attack/unilateral-release", "Payee bank releases alone"],
  "forged-endorsement": ["/api/attack/forged-endorsement", "Payee bank forges payer signature"],
  "payer-confirms-own-recall": ["/api/attack/payer-confirms-own-recall", "Payer bank confirms its own recall"],
  "replay-recall": ["/api/attack/replay-recall", "Replay the recall request"],
};

function btn(key, ref, enabled) {
  return `<button data-act="${key}" data-ref="${esc(ref)}" ${enabled ? "" : "disabled"}>${ACTIONS[key][1]}</button>`;
}

function creditHtml(c) {
  const s = c.state, live = !FINAL.has(s), pending = s === "PENDING";
  const left = c.holdUntil - ledgerNow;
  const timer = pending ? (left > 0 ? `releases in <b>${fmtDur(left)}</b>` : "hold ended, keeper releasing…") : "";
  const who = c.bankView ? `${esc(c.bankView.payerVpa)} → ${esc(c.bankView.payeeName)}` : "from an earlier demo session";
  const approvals = c.releaseApprovals.length ? ` · release approved by ${c.releaseApprovals.map(esc).join(", ")}` : "";
  const events = c.events.map((e) => `<li><span class="mono">${new Date((e.at - ledgerSkew) * 1000).toLocaleTimeString()}</span> <b>${esc(e.fn)}</b> by ${esc(e.creator)}
    · endorsed by ${e.endorsers.map(esc).join(" + ")} ${e.from ? `· ${esc(e.from)} → ${esc(e.to)}` : ""} ${e.note ? `· <i>${esc(e.note)}</i>` : ""}</li>`).join("");
  return `<div class="credit ${live ? "" : "final"}" data-ref="${esc(c.txnRef)}">
    <div class="credit-head">
      <span><span class="amount">${rupees(c.amountPaise)}</span> <span class="tier ${c.tier}">${c.tier}</span> <span class="mono">${esc(c.txnRef)}</span></span>
      <span class="state ${s}">${s}</span>
    </div>
    <div class="meta">${who} · ${c.reasonCodes.map((r) => `<span class="code-chip">${esc(r)}</span>`).join("")} ${timer ? "· " + timer : ""}${approvals}</div>
    ${live ? `<div class="actions">
      <div class="role payer"><b>Victim / payer bank</b>${btn("report", c.txnRef, pending)}${btn("payerApprove", c.txnRef, pending)}${btn("payerDispute", c.txnRef, pending || s === "RECALL_REQUESTED")}</div>
      <div class="role payee"><b>Payee bank</b>${btn("payeeConfirm", c.txnRef, s === "RECALL_REQUESTED")}${btn("payeeApprove", c.txnRef, pending)}${btn("payeeDispute", c.txnRef, pending || s === "RECALL_REQUESTED")}</div>
      ${s === "DISPUTED" ? `<div class="role npci"><b>NPCI observer</b>${btn("npciRelease", c.txnRef, true)}${btn("npciRecall", c.txnRef, true)}</div>` : ""}
      <div class="role attack"><b>Attacks (expected to fail)</b>${btn("unilateral-release", c.txnRef, true)}${btn("forged-endorsement", c.txnRef, true)}${btn("payer-confirms-own-recall", c.txnRef, true)}${btn("replay-recall", c.txnRef, true)}</div>
    </div>` : ""}
    <details><summary>Audit trail (${c.events.length} event${c.events.length === 1 ? "" : "s"})</summary><ol class="events">${events}</ol></details>
  </div>`;
}

async function refreshCredits() {
  const r = await api("/api/credits");
  ledgerNow = r.now;
  ledgerSkew = r.now - Date.now() / 1000;
  const box = $("#credits");
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.closest(".credit").dataset.ref));
  box.innerHTML = r.credits.length ? r.credits.slice(0, 20).map(creditHtml).join("")
    : `<div class="empty">No held credits yet. Send the "safe verification account" payment to create one.</div>`;
  box.querySelectorAll(".credit").forEach((el) => {
    if (open.has(el.dataset.ref)) el.querySelector("details").open = true;
  });
  const latest = r.credits[0];
  if (latest) {
    const { bankView, ...shared } = latest;
    $("#shared-view").textContent = JSON.stringify({ ...shared, events: `[${shared.events.length} events]` }, null, 2);
    $("#bank-view").textContent = JSON.stringify({ txnRef: latest.txnRef, ...bankView }, null, 2);
  }
}

$("#credits").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const key = b.dataset.act, ref = b.dataset.ref;
  const [path, label] = ACTIONS[key];
  const body = { txnRef: ref };
  if (key === "payeeDispute") body.reason = "Payee customer says this payment was genuine";
  if (key === "payerDispute") body.reason = "Payee bank has not confirmed the recall";
  if (key === "npciRelease") body.outcome = "RELEASE";
  if (key === "npciRecall") body.outcome = "RECALL";
  if (key === "report") body.description = "Victim reports a fake police call";
  b.disabled = true;
  try {
    const r = await api(path, body);
    if (path.includes("/attack/")) {
      log(r.blocked ? `🛡 <b>${label}</b>: blocked by the ledger, ${esc(r.code)}: ${esc(r.error)}. Credit is still <b>${esc(r.state)}</b>.`
        : `⚠ <b>${label}</b> was not blocked (state ${esc(r.state)})`, r.blocked ? "ok" : "bad");
    } else if (key === "report") {
      log(`1930 complaint ${esc(r.complaint)}: <b>${esc(r.outcome)}</b>${r.message ? " · " + esc(r.message) : ""}`, r.outcome === "RECALL_REQUESTED" ? "ok" : "bad");
    } else {
      const e = r.events[r.events.length - 1];
      log(`<b>${label}</b> on <span class="mono">${esc(ref)}</span> → <b>${esc(r.state)}</b> (endorsed by ${e.endorsers.map(esc).join(" + ")})`, "ok");
    }
  } catch (e) {
    log(`<b>${label}</b> rejected: ${esc(e.message)}`, "bad");
  }
  await refresh();
});

$("#clock-ctl").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-adv]");
  if (!b) return;
  try {
    const r = await api("/api/dev/advance", { seconds: Number(b.dataset.adv) });
    log(`Ledger clock moved forward ${fmtDur(Number(b.dataset.adv))}. Keeper settled: ${r.settled.length ? r.settled.map(esc).join(", ") : "nothing due"}`, "ok");
  } catch (e) { log(esc(e.message), "bad"); }
  await refresh();
});

// ---- ledger tab -------------------------------------------------------------------
async function refreshChain() {
  const r = await api("/api/chain");
  $("#chain-status").className = "pill " + (r.valid ? "ok" : "bad");
  $("#chain-status").textContent = r.valid ? `hash chain valid · ${r.height} blocks` : "hash chain BROKEN";
  $("#chain").innerHTML = "<tr><th>#</th><th>Function</th><th>Credit</th><th>Submitted by</th><th>Endorsed by</th><th>Hash</th></tr>" +
    r.blocks.slice().reverse().map((b) => `<tr><td>${b.height}</td><td><b>${esc(b.fn)}</b></td><td class="mono">${esc(b.txnRef)}</td>
      <td>${esc(b.creator)}</td><td>${b.endorsers.map(esc).join(" + ")}</td><td class="mono" title="prev ${esc(b.prevHash)}">${esc(b.hash.slice(0, 16))}…</td></tr>`).join("");
}

// ---- simulator tab ------------------------------------------------------------------
function chart(points) {
  const W = 600, H = 340, L = 52, R = 70, T = 16, B = 44;
  const sx = (x) => (x <= 0.1 ? x / 0.1 : 1 + Math.log10(x / 0.1)); // symlog, linear below 0.1%
  const xmax = sx(Math.max(50, ...points.map((p) => p.genuineDelayedPct)));
  const X = (x) => L + (sx(x) / xmax) * (W - L - R);
  const Y = (y) => T + (1 - y) * (H - T - B);
  const color = { blanket: "var(--amber)", twokey: "var(--cyan)", statusQuo: "#8a94a6" };
  let g = "";
  for (const y of [0, 0.25, 0.5, 0.75, 1]) g += `<line x1="${L}" x2="${W - R}" y1="${Y(y)}" y2="${Y(y)}" stroke="#e2e8f0"/><text x="${L - 6}" y="${Y(y) + 4}" text-anchor="end" font-size="11" fill="#5b6577">${y * 100}%</text>`;
  for (const x of [0, 0.1, 1, 10, 40]) g += `<line x1="${X(x)}" x2="${X(x)}" y1="${T}" y2="${H - B}" stroke="#eef2f7"/><text x="${X(x)}" y="${H - B + 16}" text-anchor="middle" font-size="11" fill="#5b6577">${x}%</text>`;
  const bl = points.filter((p) => p.policy === "blanket").sort((a, b) => a.genuineDelayedPct - b.genuineDelayedPct);
  g += `<polyline fill="none" stroke="var(--amber)" stroke-width="2" points="${bl.map((p) => `${X(p.genuineDelayedPct)},${Y(p.recoveryRatio)}`).join(" ")}"/>`;
  for (const p of points) {
    const x = X(p.genuineDelayedPct), y = Y(p.recoveryRatio);
    const mark = p.policy === "twokey" ? `<rect x="${x - 5}" y="${y - 5}" width="10" height="10" transform="rotate(45 ${x} ${y})" fill="${color[p.policy]}"/>`
      : `<circle cx="${x}" cy="${y}" r="5" fill="${color[p.policy]}"/>`;
    g += `<g><title>${esc(p.label)}: recovers ${pct(p.recoveryRatio)} of fraud value, delays ${p.genuineDelayedPct.toFixed(2)}% of genuine transfers</title>${mark}
      <text x="${x + 8}" y="${y + 4}" font-size="10" fill="#334155">${esc(p.label.replace("blanket ", "").replace("TwoKey ", ""))}</text></g>`;
  }
  g += `<text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle" font-size="12" fill="#0f172a">Genuine P2P transfers delayed (log scale)</text>`;
  g += `<text transform="translate(14 ${(T + H - B) / 2}) rotate(-90)" text-anchor="middle" font-size="12" fill="#0f172a">Fraud value recoverable</text>`;
  g += `<g font-size="11"><circle cx="${L + 14}" cy="${T + 8}" r="5" fill="var(--amber)"/><text x="${L + 24}" y="${T + 12}">blanket lag</text>
    <rect x="${L + 104}" y="${T + 3}" width="10" height="10" transform="rotate(45 ${L + 109} ${T + 8})" fill="var(--cyan)"/><text x="${L + 120}" y="${T + 12}">TwoKey variants</text>
    <circle cx="${L + 234}" cy="${T + 8}" r="5" fill="#8a94a6"/><text x="${L + 244}" y="${T + 12}">status quo</text></g>`;
  return `<svg viewBox="0 0 ${W} ${H}">${g}</svg>`;
}

function showSim(r) {
  const h = r.headline;
  $("#sim-disclaimer").textContent = `${r.disclaimer} ${r.params.n.toLocaleString("en-IN")} transfers, prevalence ${pct(r.params.prevalence, 2)}, mule cash-out median ${r.params.cashoutMedianMinutes} min, seed ${r.params.seed}.`;
  $("#sim-chart").innerHTML = chart(h.frontier);
  const kpi = (label, v, pass) => `<div class="kpi ${pass === undefined ? "" : pass ? "pass" : "fail"}"><b>${v}</b><span>${label}</span></div>`;
  $("#sim-headline").innerHTML = `<h3>Default TwoKey (30 min / 4 h) vs blanket lag (≥ ₹10,000, 4 h)</h3><div class="kpis">
    ${kpi("Recovery vs blanket (target ≥ 80%)", pct(h.twokeyRecoveryVsBlanket), h.meetsRecoveryTarget)}
    ${kpi("Genuine delays vs blanket (target ≤ 10%)", pct(h.twokeyDelayVsBlanket), h.meetsFrictionTarget)}
    ${kpi("Recovery: status quo / blanket / TwoKey", `${pct(h.statusQuo.recoveryRatio, 0)} / ${pct(h.blanket.recoveryRatio, 0)} / ${pct(h.twokey.recoveryRatio, 0)}`)}
    ${kpi("Genuine transfers delayed: blanket / TwoKey", `${h.blanket.genuineDelayed.toLocaleString("en-IN")} / ${h.twokey.genuineDelayed.toLocaleString("en-IN")}`)}
  </div>`;
  if (r.sweep) {
    $("#sweep").innerHTML = "<tr><th>Mule signal</th><th>Prevalence</th><th>Cash-out median</th><th class='num'>Status quo</th><th class='num'>Blanket</th><th class='num'>TwoKey</th><th class='num'>Recovery vs blanket</th><th class='num'>Delays vs blanket</th></tr>" +
      r.sweep.map((s) => `<tr><td>${s.signal}</td><td>${pct(s.prevalence, 2)}</td><td>${s.cashoutMedianMinutes} min</td><td class="num">${pct(s.statusQuo)}</td><td class="num">${pct(s.blanket)}</td><td class="num">${pct(s.twokey)}</td>
        <td class="num">${pct(s.recoveryVsBlanket)}</td><td class="num">${pct(s.delayVsBlanket)}</td></tr>`).join("");
  }
}

let simLoaded = false;
async function loadSimulation() {
  if (simLoaded) return;
  try { showSim(await api("/api/simulation")); simLoaded = true; }
  catch (e) { $("#sim-disclaimer").textContent = e.message; }
}

$("#sim-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target), b = ev.target.querySelector("button");
  b.disabled = true; b.textContent = "Running…";
  try {
    const r = await api("/api/simulation", { n: Number(f.get("n")), prevalence: Number(f.get("prevalence")) / 100, cashoutMedianMinutes: Number(f.get("cash")), seed: Number(f.get("seed")) });
    showSim(r);
  } catch (e) { $("#sim-disclaimer").textContent = e.message; }
  b.disabled = false; b.textContent = "Re-run simulation";
});

// ---- polling ------------------------------------------------------------------------
async function refresh() {
  try {
    await Promise.all([refreshAccounts(), refreshCredits()]);
    if (!$("#tab-ledger").hidden) await refreshChain();
  } catch (e) { /* surfaced by the health pill */ }
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    $("#ledger-pill").className = "pill " + (health.ledger ? "ok" : "bad");
    $("#ledger-pill").textContent = health.ledger ? "ledger connected" : "ledger offline";
    $("#clock-ctl").hidden = !health.devClock;
  } catch {
    $("#ledger-pill").className = "pill bad";
    $("#ledger-pill").textContent = "services offline";
  }
}

setInterval(() => {
  // Tick countdowns locally between polls.
  ledgerNow = Date.now() / 1000 + ledgerSkew;
}, 1000);

(async () => {
  await refreshHealth();
  await refresh();
  document.querySelector(`.tabs button[data-tab="${location.hash.slice(1)}"]`)?.click();
  preview();
  setInterval(refreshHealth, 10000);
  setInterval(refresh, 3000);
})();
