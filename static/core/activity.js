(() => {
  const panel = document.querySelector('[data-activity-url]');
  if (!panel) return;
  const log = panel.querySelector('.activity-log');
  const connection = panel.querySelector('.activity-connection');
  const heartbeat = panel.querySelector('.activity-heartbeat');
  const pause = panel.querySelector('.activity-pause');
  const graph = panel.querySelector('.review-flow');
  let paused = false, stopped = false, busy = false, latest = 0;
  const labels = {pending:'Waiting', unused:'Not used', running:'In progress', done:'Complete', success:'Responded', refused:'Declined', unavailable:'Unavailable', quota:'Out of quota', attention:'Needs attention', blocked:'Blocked', cancelled:'Cancelled', cancelling:'Cancelling', interrupted:'Interrupted'};
  const utc = value => value ? new Date(value).toISOString().replace('T',' ').replace(/\.\d+Z$/, ' UTC') : '';
  const shortTime = value => value ? new Date(value).toISOString().slice(11,19) + ' UTC' : '';
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
      const tries = element('ol','model-tries');
      for (const [number, attempt] of data.attempts.entries()) {
        const row = element('li',`model-try try-${attempt.state}`);
        const line = element('div','try-heading');
        line.append(element('span','try-index',String(number + 1).padStart(2,'0')),element('strong','try-outcome',labels[attempt.state] || 'Needs attention'),element('span','try-duration',duration(attempt.started_at,attempt.finished_at,attempt.state)));
        if (attempt.failure_reason) row.append(element('p','try-reason',attempt.failure_reason));
        row.append(line,element('p','try-time',`${shortTime(attempt.started_at)}${attempt.category ? ' · ' + attempt.category.replaceAll('_',' ') : ''}`));
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
      ['03','Review with AI','Same request. Ordered fallback on decline or unavailability.',flow.models,true],
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
      set('[data-job-started]',utc(data.started_at) || 'Not started');
      set('[data-job-finished]',utc(data.finished_at) || 'Not finished');
      set('[data-job-retry]',utc(data.next_retry_at) || 'Not scheduled');
      const note = document.querySelector('[data-job-note]');
      note.hidden = !data.reason; text(note.querySelector('p'),data.reason || '');
      text(connection,`${data.status_label}${data.provider ? ' · ' + data.provider : ''}`);
      panel.dataset.jobState = data.status;
      if (data.status === 'running') {
        const age = data.heartbeat_at ? Math.max(0,Math.floor((Date.now() - Date.parse(data.heartbeat_at)) / 1000)) : null;
        text(heartbeat,age === null ? 'Waiting for worker heartbeat' : `Worker heartbeat ${age}s ago${age > 30 ? ' · delayed; worker may be unavailable' : ' · updating every 3s'}`);
      } else if (data.next_retry_at && data.status === 'quota') {
        text(heartbeat,`Next availability check ${utc(data.next_retry_at)} · not a guaranteed reset`);
      } else {
        const states = {queued:'Waiting for the serial worker',attention:'Paused · see the model nodes and operational note',failed:'Stopped · see the operational note',completed:'Processing finished · open GitHub for the review',cancelled:'Cancelled · no further review work',superseded:'Superseded by a newer review state'};
        text(heartbeat,states[data.status] || 'Review is not running');
      }
      const retryControl = document.querySelector("[data-job-retry-control]");
      if (retryControl) retryControl.hidden = !data.retry_available;
      renderFlow(data.flow);
      const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
      for (const event of data.events) {
        if (event.id <= latest) continue;
        log.querySelector('.activity-empty')?.remove();
        const row = element('li','');
        const time = element('time','',utc(event.at)); time.dateTime = event.at;
        row.append(time,element('span','',event.message)); log.append(row); latest = event.id;
      }
      while (log.children.length > 100) log.firstElementChild.remove();
      if (!data.events.length && !latest) text(log.querySelector('.activity-empty'),'No recorded events. Older attempts may not have an activity history.');
      if (follow) log.scrollTop = log.scrollHeight;
    } catch {
      if (!paused) text(connection,'Connection interrupted · retrying. Nodes may be stale.');
    } finally { clearTimeout(timeout); busy = false; }
  }
  pause.addEventListener('click',() => { paused = !paused; text(pause,paused ? 'Resume updates' : 'Pause updates'); pause.setAttribute('aria-pressed',String(paused)); if (paused) text(connection,'Display paused · the review continues independently'); else poll(); });
  document.addEventListener('visibilitychange',() => { if (!document.hidden) poll(); });
  poll(); setInterval(poll,3000);
})();
