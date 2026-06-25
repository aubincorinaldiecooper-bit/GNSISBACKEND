"""A dependency-free single-page UI, served at ``/ui``.

Deliberately one self-contained HTML string (no build step, no framework, no
package data) so it always ships with the service. It talks to the same-origin
API: list/create jobs, watch status + logs, review the diff, and approve or
reject — enough to dogfood from a browser instead of curl.
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>GNSIS</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0d1117; color: #c9d1d9; }
  header { padding: 12px 18px; border-bottom: 1px solid #30363d; display: flex;
           align-items: center; gap: 14px; }
  header h1 { font-size: 16px; margin: 0; letter-spacing: 2px; }
  header input { background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
                 padding: 5px 8px; border-radius: 6px; }
  main { display: grid; grid-template-columns: 360px 1fr; gap: 0; height: calc(100vh - 53px); }
  #left { border-right: 1px solid #30363d; overflow-y: auto; }
  #right { overflow-y: auto; padding: 18px; }
  .job { padding: 10px 14px; border-bottom: 1px solid #21262d; cursor: pointer; }
  .job:hover { background: #161b22; }
  .job.sel { background: #1f6feb22; border-left: 3px solid #1f6feb; }
  .repo { color: #58a6ff; }
  .st { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; }
  .st.awaiting_approval { background: #9e6a03; color: #fff; }
  .st.completed { background: #238636; color: #fff; }
  .st.failed, .st.rejected { background: #da3633; color: #fff; }
  .st.queued, .st.planning, .st.patching, .st.testing, .st.summarizing,
  .st.approved, .st.publishing { background: #30363d; color: #c9d1d9; }
  form { display: grid; gap: 8px; max-width: 720px; }
  input, textarea, select, button { font: inherit; }
  textarea { background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
             border-radius: 6px; padding: 8px; min-height: 70px; }
  input.f, select.f { background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
                      border-radius: 6px; padding: 7px 8px; }
  button { background: #238636; border: 0; color: #fff; padding: 8px 14px;
           border-radius: 6px; cursor: pointer; }
  button.sec { background: #30363d; }
  button.danger { background: #da3633; }
  pre { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
        padding: 12px; overflow-x: auto; white-space: pre-wrap; }
  .diff .add { color: #3fb950; } .diff .del { color: #f85149; }
  .diff .hdr { color: #58a6ff; }
  h2 { font-size: 14px; border-bottom: 1px solid #30363d; padding-bottom: 6px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .muted { color: #8b949e; }
  .log { color: #8b949e; } .log .err { color: #f85149; } .log .warn { color: #d29922; }
</style>
</head>
<body>
<header>
  <h1>GNSIS</h1>
  <span class="muted">self-evolving code agent</span>
  <span style="flex:1"></span>
  <input id="apikey" placeholder="API key (optional)" />
</header>
<main>
  <div id="left"></div>
  <div id="right"></div>
</main>
<script>
const $ = (s, r=document) => r.querySelector(s);
const api = (p, opts={}) => {
  const key = localStorage.getItem("gnsis_key") || "";
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (key) headers["Authorization"] = "Bearer " + key;
  return fetch(p, Object.assign({}, opts, { headers })).then(async r => {
    if (!r.ok) throw new Error((await r.text()) || r.status);
    return r.status === 204 ? null : r.json();
  });
};
let selected = null;

const keyInput = $("#apikey");
keyInput.value = localStorage.getItem("gnsis_key") || "";
keyInput.onchange = () => localStorage.setItem("gnsis_key", keyInput.value.trim());

function esc(s){ return (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function renderDiff(patch){
  if (!patch) return '<span class="muted">no diff yet</span>';
  return esc(patch).split("\n").map(l => {
    let c = "";
    if (l.startsWith("+") && !l.startsWith("+++")) c = "add";
    else if (l.startsWith("-") && !l.startsWith("---")) c = "del";
    else if (l.startsWith("@@") || l.startsWith("diff ")) c = "hdr";
    return c ? `<span class="${c}">${l}</span>` : l;
  }).join("\n");
}

async function loadJobs(){
  const jobs = await api("/jobs?limit=100");
  $("#left").innerHTML = jobs.map(j => `
    <div class="job ${j.id===selected?'sel':''}" onclick="openJob('${j.id}')">
      <div class="row"><span class="repo">${esc(j.repo)}</span>
        <span style="flex:1"></span><span class="st ${j.status}">${j.status}</span></div>
      <div class="muted">${esc(j.instruction).slice(0,80)}</div>
    </div>`).join("") || '<div class="muted" style="padding:14px">no jobs yet</div>';
}

function newJobForm(){
  $("#right").innerHTML = `
    <h2>New job</h2>
    <form onsubmit="return createJob(event)">
      <input class="f" id="repo" placeholder="owner/name" required />
      <input class="f" id="base" placeholder="base branch (default: main)" />
      <textarea id="instr" placeholder="What should the agent change?" required></textarea>
      <div class="row"><button type="submit">Create job</button>
        <span class="muted">runs async; review the diff before it opens a PR</span></div>
    </form>`;
}

async function createJob(e){
  e.preventDefault();
  const body = { repo: $("#repo").value.trim(), instruction: $("#instr").value.trim() };
  const base = $("#base").value.trim(); if (base) body.base_branch = base;
  try {
    const job = await api("/jobs", { method:"POST", body: JSON.stringify(body) });
    await loadJobs(); openJob(job.id);
  } catch(err){ alert("Create failed: " + err.message); }
  return false;
}

async function openJob(id){
  selected = id; await loadJobs();
  const [job, logs] = await Promise.all([api("/jobs/"+id), api("/jobs/"+id+"/logs")]);
  let diff = null;
  try { diff = await api("/jobs/"+id+"/diff"); } catch(_) {}
  const gate = job.status === "awaiting_approval";
  $("#right").innerHTML = `
    <div class="row"><h2 style="flex:1">${esc(job.repo)} <span class="st ${job.status}">${job.status}</span></h2>
      <button class="sec" onclick="openJob('${id}')">↻ refresh</button>
      <button class="sec" onclick="newJobForm()">+ new</button></div>
    <p>${esc(job.instruction)}</p>
    ${job.error ? `<pre class="diff"><span class="del">${esc(job.error)}</span></pre>` : ""}
    ${gate ? `<div class="row">
        <button onclick="decide('${id}','approve')">✓ Approve & open PR</button>
        <button class="danger" onclick="decide('${id}','reject')">✗ Reject</button></div>` : ""}
    <h2>Diff ${diff && diff.files_changed ? `<span class="muted">(${diff.files_changed.length} files)</span>`:""}</h2>
    <pre class="diff">${renderDiff(diff && diff.patch)}</pre>
    <h2>Logs</h2>
    <pre class="log">${logs.map(l => {
      const cls = l.level==="error"?"err":l.level==="warning"?"warn":"";
      return `<span class="${cls}">[${l.phase||"-"}] ${esc(l.message)}</span>`;
    }).join("\n") || "—"}</pre>`;
  // Auto-refresh while the job is still working.
  if (!["completed","failed","rejected","awaiting_approval"].includes(job.status)){
    setTimeout(() => { if (selected===id) openJob(id); }, 3000);
  }
}

async function decide(id, action){
  if (action==="reject" && !confirm("Reject this change?")) return;
  try { await api(`/jobs/${id}/${action}`, { method:"POST", body: JSON.stringify({actor:"ui"}) });
    openJob(id);
  } catch(err){ alert("Failed: " + err.message); }
}

newJobForm(); loadJobs(); setInterval(loadJobs, 5000);
</script>
</body>
</html>
"""
