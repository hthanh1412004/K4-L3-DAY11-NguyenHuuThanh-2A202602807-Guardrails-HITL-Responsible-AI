const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function setLoading(button, loading, label) {
  button.disabled = loading;
  button.innerHTML = loading ? "Running…" : label;
}

function renderMetrics(state) {
  const metrics = state.metrics || state;
  $("#totalRequests").textContent = metrics.total_requests || 0;
  $("#blockedRequests").textContent = metrics.blocked_requests || 0;
  $("#rateHits").textContent = metrics.rate_limit_hits || 0;
  $("#alertCount").textContent = (metrics.alerts || []).length;
  $("#blockRate").textContent = `${Math.round((metrics.block_rate || 0) * 100)}% block rate`;
}

function renderState(state) {
  $("#modelLabel").textContent = state.model;
  $("#quotaLabel").textContent = `${state.rate_limit.max_requests} req / ${state.rate_limit.window_seconds}s`;
  renderMetrics(state.metrics);
  const rows = state.audit || [];
  $("#auditRows").innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.request_id)}</td>
      <td>${escapeHtml(row.user_id)}</td>
      <td><span class="decision ${row.blocked ? "blocked" : "allowed"}">${row.blocked ? "BLOCKED" : "ALLOWED"}</span></td>
      <td>${escapeHtml(row.layer || "—")}</td>
      <td>${Number(row.latency_ms || 0).toFixed(1)} ms</td>
    </tr>`).join("") : `<tr><td class="table-empty" colspan="5">No requests in this session.</td></tr>`;
  $("#alerts").innerHTML = (state.metrics.alerts || []).map((alert) => `
    <div class="alert">⚠ ${escapeHtml(alert.message)} <strong>${Number(alert.value).toFixed(2)}</strong></div>
  `).join("");
}

async function refreshState() {
  try { renderState(await api("/api/state")); } catch (error) { console.error(error); }
}

function renderPipeline(result) {
  const trace = result.trace.map((step, index) => `
    <article class="trace-step ${step.status}">
      <small>0${index + 1} · ${escapeHtml(step.status)}</small>
      <strong>${escapeHtml(step.name)}</strong>
      <p>${escapeHtml(step.detail)}</p>
    </article>`).join("");
  $("#pipelineResult").className = "result-wrap";
  $("#pipelineResult").innerHTML = `
    <div class="decision-head">
      <span class="decision ${result.blocked ? "blocked" : "allowed"}">${escapeHtml(result.decision)}</span>
      <span class="latency">${result.live ? "LIVE API" : "LOCAL"} · ${result.latency_ms} ms · ${escapeHtml(result.request_id)}</span>
    </div>
    <div class="trace">${trace}</div>
    <div class="response-box"><label>Final response</label><pre>${escapeHtml(result.response)}</pre></div>`;
  renderMetrics(result.metrics);
}

$$('.tab').forEach((button) => button.addEventListener('click', () => {
  $$('.tab').forEach((item) => item.classList.remove('active'));
  $$('.tab-panel').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  $(`#${button.dataset.tab}`).classList.add('active');
  if (button.dataset.tab === 'audit') refreshState();
}));

$$('.chip[data-prompt]').forEach((button) => button.addEventListener('click', () => {
  $('#promptInput').value = button.dataset.prompt;
}));

$$('.egress-preset').forEach((button) => button.addEventListener('click', () => {
  $('#egressUrl').value = button.dataset.url;
  $('#egressPayload').value = button.dataset.payload;
}));

$('#sendPrompt').addEventListener('click', async () => {
  const button = $('#sendPrompt');
  setLoading(button, true, 'Run pipeline <span>→</span>');
  try {
    const result = await api('/api/evaluate', {
      prompt: $('#promptInput').value,
      user_id: $('#userId').value,
      live: $('#liveToggle').checked,
    });
    renderPipeline(result);
  } catch (error) {
    $('#pipelineResult').className = 'result-wrap';
    $('#pipelineResult').innerHTML = `<div class="error-box"><strong>Provider error</strong><br>${escapeHtml(error.message)}</div>`;
  } finally {
    setLoading(button, false, 'Run pipeline <span>→</span>');
    refreshState();
  }
});

$('#liveToggle').addEventListener('change', (event) => {
  const note = $('.live-note');
  note.innerHTML = event.target.checked
    ? '<span class="pulse"></span> Live mode đang bật: prompt an toàn sẽ gọi OpenRouter thật; prompt bị chặn không gọi LLM.'
    : '<span class="status-dot safe"></span> Local mode đang bật: mô phỏng pipeline, không phát sinh API request.';
});

$('#inspectOutput').addEventListener('click', async () => {
  const result = await api('/api/output-filter', { text: $('#outputInput').value });
  const issues = result.issues.map((issue) => `<span class="issue">${escapeHtml(issue)}</span>`).join('');
  $('#outputResult').innerHTML = `
    <div class="status-line"><span class="status-dot ${result.safe ? 'safe' : 'unsafe'}"></span><strong>${result.safe ? 'SAFE' : 'SENSITIVE DATA FOUND'}</strong></div>
    <pre>${escapeHtml(result.redacted)}</pre><div class="issue-list">${issues}</div>`;
});

$('#inspectEgress').addEventListener('click', async () => {
  const result = await api('/api/egress', { destination: $('#egressUrl').value, payload: $('#egressPayload').value });
  $('#egressResult').innerHTML = `
    <div class="status-line"><span class="status-dot ${result.allowed ? 'safe' : 'unsafe'}"></span><strong>${result.decision}</strong></div>
    <pre>host: ${escapeHtml(result.host || 'invalid')}\n${result.reasons.map((reason) => `• ${escapeHtml(reason)}`).join('\n') || '• Destination and payload passed deterministic policy.'}</pre>`;
});

$('#resetSession').addEventListener('click', async () => {
  renderState(await api('/api/reset', {}));
  $('#pipelineResult').className = 'result-wrap empty-state';
  $('#pipelineResult').innerHTML = '<div class="empty-icon">⌁</div><p>Run a prompt to see the decision trace.</p>';
});

refreshState();
