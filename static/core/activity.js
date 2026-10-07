(() => {
  const panel = document.querySelector('[data-activity-url]');
  if (!panel) return;
  const log = panel.querySelector('.activity-log');
  const connection = panel.querySelector('.activity-connection');
  const heartbeat = panel.querySelector('.activity-heartbeat');
  const pause = panel.querySelector('.activity-pause');
  const followButton = panel.querySelector('[data-terminal-follow]');
  let following = true;
  const graph = panel.querySelector('.review-flow');
  let paused = false, stopped = false, busy = false, latest = 0;
  let transcriptMode = "cli", terminalEvents = [];
  const cliKinds = new Set(["cli_output_text","cli_output_tool","cli_output_done","cli_output_failed","cli_output_status"]);
  const labels = {pending:'Waiting', unused:'Not used', running:'In progress', done:'Complete', success:'Responded', refused:'Declined', failed:'Review failed', skipped:'Skipped · quota', incomplete:'Incomplete evidence', unavailable:'Unavailable', quota:'Out of quota', attention:'Needs attention', blocked:'Blocked', cancelled:'Cancelled', cancelling:'Cancelling', interrupted:'Interrupted'};
  const jakartaFormat = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Jakarta', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  });
  const jakarta = value => {
    if (!value) return '';
    const parts = Object.fromEntries(jakartaFormat.formatToParts(new Date(value)).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} WIB`;
  };
  const shortTime = value => value ? jakarta(value).slice(11) : '';
  const text = (element, value) => { if (element.textContent !== value) element.textContent = value; };
  const set = (selector, value) => document.querySelectorAll(selector).forEach(element => text(element, value));
  const element = (tag, className, value) => { const el = document.createElement(tag); el.className = className; if (value !== undefined) el.textContent = value; return el; };
  function duration(start, finish, state) {
    if (!start || (!finish && !['running','cancelling'].includes(state))) return '';
    const seconds = Math.max(0, Math.floor(((finish ? Date.parse(finish) : Date.now()) - Date.parse(start)) / 1000));
    return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
  }
  function card(data, index, model = false) {
    const state = Object.hasOwn(labels, data.state) ? data.state : 'attention';
    const node = element('article', `flow-node node-${state}${model ? ' model-node' : ''}`);
    const top = element('div', 'node-top');
    top.append(element('span','node-number',String(index + 1).padStart(2,'0')),element('span',`node-state state-${state}`,labels[state]));
    node.append(top);
    if (model) {
      node.append(element('h4','model-name',data.title),element('p','model-id',data.model));
      const backend = {claude_cli:'Claude Code · subscription',codex_cli:'Codex · subscription',anthropic:'Anthropic API',openai:'OpenAI API'}[data.backend];
      if (backend) node.append(element('p','model-backend',backend));
      const tries = element('ol','model-tries');
      for (const [number, attempt] of data.attempts.entries()) {
        const row = element('li',`model-try try-${attempt.state}`);
        const line = element('div','try-heading');
        line.append(element('span','try-index',String(number + 1).padStart(2,'0')),element('strong','try-outcome',labels[attempt.state] || 'Needs attention'),element('span','try-duration',duration(attempt.started_at,attempt.finished_at,attempt.state)));
        row.append(line,element('p','try-time',shortTime(attempt.started_at)));
        if (attempt.recovery_pass === 1) row.append(element('p','try-progress','Evidence recovery pass 1 of 1 · fresh session'));
        if (attempt.events_received !== null && attempt.events_received !== undefined) row.append(element('p','try-progress',`${attempt.events_received} CLI events received`));
        if (attempt.evidence_total !== null && attempt.evidence_total !== undefined) row.append(element('p','try-progress',`${attempt.evidence_read} / ${attempt.evidence_total} evidence items supplied`));
        if (attempt.usage) {
          const usage = element('details','try-usage');
          usage.dataset.attemptId = String(attempt.id);
          usage.open = Boolean(graph.querySelector(`details[data-attempt-id="${attempt.id}"][open]`));
          usage.append(element('summary','try-label',`Usage · ${attempt.usage.session_count} CLI/API session(s)`));
          const labels = {input_tokens:'Input',cached_input_tokens:'Cache read',cache_creation_input_tokens:'Cache creation',output_tokens:'Output'};
          const show = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unavailable';
          const totals = element('dl','usage-grid');
          for (const [key,label] of Object.entries(labels)) totals.append(element('dt','',label),element('dd','',show(attempt.usage.totals[key])));
          usage.append(totals,element('p','node-detail','Provider-reported tokens. Claude input excludes separately reported cache tokens; Codex cache reads are part of input. Not subscription charges.'));
          if (attempt.usage.passes.length > 1) {
            attempt.usage.passes.forEach((pass,index) => usage.append(element('p','node-detail',`${index ? 'Recovery' : 'Initial'}: input ${show(pass.input_tokens)} · output ${show(pass.output_tokens)} · cache read ${show(pass.cached_input_tokens)}`)));
          }
          row.append(usage);
        }
        if (attempt.failure_reason) {
          const diagnosis = element('div','try-diagnosis');
          diagnosis.append(element('strong','try-label','Why it stopped'),element('p','try-reason',attempt.failure_reason));
          if (attempt.next_action) diagnosis.append(element('strong','try-label','What happened next'),element('p','try-next',attempt.next_action));
          if (attempt.retry_at) diagnosis.append(element('p','try-next',`Next availability check: ${jakarta(attempt.retry_at)} (not a guaranteed quota reset).`));
          row.append(diagnosis);
        }
        tries.append(row);
      }
      node.append(tries);
      if (!data.attempts.length) node.append(element('p','node-detail',state === 'pending' ? 'Runs if the previous model cannot review.' : state === 'unused' ? 'No call made.' : 'Recorded before per-model history was available.'));
    } else {
      const value = data.unit ? (data.count === null ? '—' : String(data.count)) : state === 'done' ? '✓' : state === 'blocked' ? '!' : state === 'running' ? '↗' : '—';
      const metric = element('div','node-metric');
      metric.append(element('strong','node-value',value));
      if (data.unit) metric.append(element('span','node-unit',data.unit));
      node.append(metric,element('h4','node-title',data.title),element('p','node-detail',data.detail));
      const elapsed = duration(data.started_at,data.finished_at,state);
      if (elapsed) node.append(element('span','node-duration',elapsed));
    }
    return node;
  }
  function renderFlow(flow) {
    const fragment = document.createDocumentFragment();
    const sections = [
      ['01','Gather evidence','Source, conversations and project agreements.',flow.stages.filter(n => ['files','discussion','docs'].includes(n.id)),false],
      ['02','Verify the change','Checks run against this exact commit.',flow.stages.filter(n => ['checks','tests'].includes(n.id)),false],
      ['03','Review with AI',flow.model_detail || 'Recorded failure causes and model fallback.',flow.models,true],
      ['04','Deliver the review','Results belong on GitHub, not in this dashboard.',flow.stages.filter(n => ['publication','sheet'].includes(n.id)),false],
    ];
    for (const [number,title,description,nodes,model] of sections) {
      const section = element('section','flow-lane');
      const heading = element('div','lane-heading');
      const label = element('div','lane-label');
      label.append(element('span','lane-number',number),element('h3','lane-title',title));
      heading.append(label,element('p','lane-description',description));
      const grid = element('div',`node-track${model ? ' model-track' : ''}`);
      nodes.forEach((node,index) => grid.append(card(node,index,model)));
      if (!nodes.length) grid.append(element('p','node-detail','No model connection configured.'));
      section.append(heading,grid); fragment.append(section);
    }
    graph.replaceChildren(fragment);
    panel.querySelector('.flow-legacy').hidden = !flow.legacy;
  }
  function renderTranscript() {
    const visible = terminalEvents.filter(event => transcriptMode === 'cli' ? cliKinds.has(event.kind) : !cliKinds.has(event.kind));
    const fragment = document.createDocumentFragment();
    for (const event of visible) {
      const row = element('li',event.kind || '');
      const time = element('time','',jakarta(event.at)); time.dateTime=event.at;
      const content = element('div','terminal-content');
      if(event.source) content.append(element('span','terminal-source',event.source));
      content.append(element('span','terminal-message',event.message));
      row.append(time,content); fragment.append(row);
    }
    if(!visible.length) fragment.append(element('li','activity-empty', transcriptMode === 'cli' ? 'No CLI transcript recorded for this attempt yet. New CLI reviews show public commentary and tool calls here; silent periods are normal.' : 'No worker events recorded.'));
    log.replaceChildren(fragment);
    if(following) log.scrollTop=log.scrollHeight;
  }
  async function poll() {
    if (paused || stopped || busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(),10000);
    try {
      const response = await fetch(panel.dataset.activityUrl,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (response.redirected || [401,403,404].includes(response.status)) {
        stopped = true; text(connection,'Feed unavailable. Sign in again or check workspace access.'); return;
      }
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      if (paused) return;
      set('[data-job-status]',`${data.status_label}.`);
      document.querySelectorAll('[data-job-badge]').forEach(badge => { text(badge,data.status_label); badge.className = `status status-${data.status}`; });
      set('[data-job-provider]',data.provider || 'Not assigned');
      set('[data-job-started]',jakarta(data.started_at) || 'Not started');
      set('[data-job-finished]',jakarta(data.finished_at) || 'Not finished');
      set('[data-job-retry]',jakarta(data.next_retry_at) || 'Not scheduled');
      const note = document.querySelector('[data-job-note]');
      note.hidden = !data.reason; text(note.querySelector('p'),data.reason || '');
      text(connection,`${data.status_label}${data.provider ? ' · ' + data.provider : ''}`);
      panel.dataset.jobState = data.status;
      if (data.status === 'running') {
        const age = data.heartbeat_at ? Math.max(0,Math.floor((Date.now() - Date.parse(data.heartbeat_at)) / 1000)) : null;
        text(heartbeat,age === null ? 'Waiting for worker heartbeat' : `Worker heartbeat ${age}s ago${age > 30 ? ' · delayed; worker may be unavailable' : ' · updating every 3s'}`);
      } else if (data.next_retry_at && data.status === 'quota') {
        text(heartbeat,`Next availability check ${jakarta(data.next_retry_at)} · not a guaranteed reset`);
      } else {
        const states = {queued:'Waiting for the serial worker',attention:'Paused · see the model nodes and operational note',failed:'Stopped · see the operational note',completed:'Processing finished · open GitHub for the review',cancelled:'Cancelled · no further review work',superseded:'Superseded by a newer review state'};
        text(heartbeat,states[data.status] || 'Review is not running');
      }
      const retryControl = document.querySelector("[data-job-retry-control]");
      if (retryControl) {
        retryControl.hidden = !data.retry_available;
        retryControl.querySelector('[data-retry-label]').textContent = data.delivery_retry ? 'Retry delivery ↻' : 'Retry review ↻';
        retryControl.querySelector('[data-retry-explanation]').textContent = data.delivery_retry ? 'Owner only · deliver the saved review without another model call. Existing receipts and current GitHub state are rechecked.' : 'Owner only · starts a fresh attempt from the first model. The current commit, reviewer and on review label must still match.';
      }
      renderFlow(data.flow);
      let changed = false;
      for (const event of data.events) {
        if(event.id<=latest) continue;
        terminalEvents.push(event); latest=event.id; changed=true;
      }
      terminalEvents=terminalEvents.slice(-100);
      if(changed || !terminalEvents.length) renderTranscript();
    } catch {
      if (!paused) text(connection,'Connection interrupted · retrying. Nodes may be stale.');
    } finally { clearTimeout(timeout); busy = false; }
  }
  panel.querySelectorAll('[data-terminal-mode]').forEach(button => button.addEventListener('click', () => {
    transcriptMode=button.dataset.terminalMode;
    panel.querySelectorAll('[data-terminal-mode]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));
    renderTranscript();
  }));
  followButton.addEventListener('click', () => {following=!following; followButton.textContent=following?'Following ↓':'Follow latest ↓'; followButton.setAttribute('aria-pressed',String(following)); if(following)log.scrollTop=log.scrollHeight;});
  log.addEventListener('wheel', () => {if(following){following=false;followButton.textContent='Follow latest ↓';followButton.setAttribute('aria-pressed','false');}}, {passive:true});
  pause.addEventListener('click',() => { paused = !paused; text(pause,paused ? 'Resume updates' : 'Pause updates'); pause.setAttribute('aria-pressed',String(paused)); if (paused) text(connection,'Display paused · the review continues independently'); else poll(); });
  document.addEventListener('visibilitychange',() => { if (!document.hidden) poll(); });
  poll(); setInterval(poll,3000);
})();
