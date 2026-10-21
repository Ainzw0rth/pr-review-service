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
  const labels = {unknown:'Not recorded', pending:'Waiting', unused:'Not used', not_needed:'Not needed', running:'In progress', done:'Complete', success:'Responded', refused:'Declined', failed:'Review failed', skipped:'Skipped · quota', incomplete:'Incomplete evidence', unavailable:'Unavailable', quota:'Out of quota', attention:'Needs attention', blocked:'Blocked', cancelled:'Cancelled', cancelling:'Cancelling', interrupted:'Interrupted'};
  const phaseNames = {intent:'Intent',review:'Review',code:'Code reviewer',security:'Security reviewer',tests:'Test reviewer',specialists:'Specialist reviews',synthesis:'Adjudication'};
  const phaseLabels = {intent:'Extracting intent',review:'Reviewing code',code:'Code review',security:'Security review',tests:'Test review',specialists:'Specialist reviews',synthesis:'Adjudicating findings'};
  const stateLabel = (state,phase) => state === 'running' && Object.hasOwn(phaseLabels,phase) ? phaseLabels[phase] : state === 'failed' && phase === 'intent' ? 'Intent failed' : labels[state] || 'Needs attention';
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
  function card(data, index, model = false, specialistStages = []) {
    const state = Object.hasOwn(labels, data.state) ? data.state : 'attention';
    const node = element('article', `flow-node node-${state}${model ? ' model-node' : ''}`);
    const latestAttempt = model ? data.attempts.at(-1) : null;
    const top = element('div', 'node-topline');
    if (!model && data.id) {
      node.dataset.stageId = data.id;
      node.dataset.state = data.waiting ? 'waiting' : state;
      const previous = graph.querySelector(`[data-stage-id="${data.id}"]`);
      if (previous && previous.dataset.state !== node.dataset.state) top.classList.add('motion-enter');
    }
    top.append(element('span','node-number',String(index + 1).padStart(2,'0')),element('span',`node-state state-${state}`,data.waiting || latestAttempt?.waiting_phases?.includes(latestAttempt?.phase) ? 'Waiting for capacity' : stateLabel(state,latestAttempt?.phase)));
    node.append(top);
    if (model) {
      node.append(element('h4','model-name',data.title),element('p','model-id',data.model));
      const backend = {claude_cli:'Claude Code · subscription',codex_cli:'Codex · subscription',anthropic:'Anthropic API',openai:'OpenAI API'}[data.backend];
      if (backend) node.append(element('p','model-backend',backend));
      const tries = element('ol','model-tries');
      for (const [number, attempt] of data.attempts.entries()) {
        const row = element('li',`model-try try-${attempt.state}`);
        row.dataset.modelAttempt = String(attempt.id);
        row.dataset.phase = attempt.phase || '';
        row.dataset.waiting = String(attempt.waiting_phases?.includes(attempt.phase) === true);
        const line = element('div','try-heading');
        line.append(element('span','try-index',String(number + 1).padStart(2,'0')),element('strong','try-outcome',attempt.waiting_phases?.includes(attempt.phase) ? 'Waiting for capacity' : stateLabel(attempt.state,attempt.phase)),element('span','try-duration',duration(attempt.started_at,attempt.finished_at,attempt.state)));
        const previous = graph.querySelector(`[data-model-attempt="${attempt.id}"]`);
        if (previous && (previous.dataset.phase !== row.dataset.phase || previous.dataset.waiting !== row.dataset.waiting)) {
          line.classList.add('motion-enter');
          if (attempt === latestAttempt) top.classList.add('motion-enter');
        }
        row.append(line,element('p','try-time',shortTime(attempt.started_at)));
        if (attempt.phase === 'intent') row.append(element('p','try-progress',attempt.state !== 'running' ? 'Phase: Intent extraction.' : attempt.evidence_total > 0 && attempt.evidence_read === attempt.evidence_total ? 'All intent evidence supplied; awaiting a validated intent summary.' : 'Extracting requirements before code review.'));
        else if (attempt.phase === 'review') row.append(element('p','try-progress','Phase: Code review.'));
        else if (Object.hasOwn(phaseNames,attempt.phase)) row.append(element('p','try-progress',`Phase: ${phaseNames[attempt.phase]}.`));
        if (attempt.recovery_pass === 1) row.append(element('p','try-progress','Evidence recovery pass 1 of 1 · fresh session'));
        if (attempt.events_received !== null && attempt.events_received !== undefined) row.append(element('p','try-progress',`${attempt.events_received} CLI events received`));
        if (attempt.evidence_total !== null && attempt.evidence_total !== undefined) row.append(element('p','try-progress',`${attempt.evidence_read} / ${attempt.evidence_total} evidence items supplied`));
        if (attempt.usage) {
          const usage = element('details','try-usage');
          usage.dataset.attemptId = String(attempt.id);
          usage.open = Boolean(graph.querySelector(`details[data-attempt-id="${attempt.id}"][open]`));
          usage.append(element('summary','try-label',`Usage · ${attempt.usage.session_count} CLI/API session(s)`));
          usage.querySelector('summary').dataset.flowFocus = `usage-${attempt.id}`;
          const labels = {input_tokens:'Input',cached_input_tokens:'Cache read',cache_creation_input_tokens:'Cache creation',output_tokens:'Output'};
          const show = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unavailable';
          const totals = element('dl','usage-grid');
          for (const [key,label] of Object.entries(labels)) totals.append(element('dt','',label),element('dd','',show(attempt.usage.totals[key])));
          usage.append(totals,element('p','node-detail','Provider-reported tokens. Claude input excludes separately reported cache tokens; Codex cache reads are part of input. Not subscription charges.'));
          if (attempt.usage.passes.length > 1) {
            attempt.usage.passes.forEach((pass,index) => usage.append(element('p','node-detail',`${pass.phase ? (phaseNames[pass.phase] || 'Review') + ' · ' + (pass.recovery_pass ? 'Recovery' : 'Initial') : index ? 'Recovery' : 'Initial'}: input ${show(pass.input_tokens)} · output ${show(pass.output_tokens)} · cache read ${show(pass.cached_input_tokens)}`)));
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
        if (specialistStages.length && attempt.phases?.length) {
          const phases = disclosure(`attempt-phases-${attempt.id}`,'attempt-phases','Recorded reviewer sessions');
          phases.details.append(reviewTree(specialistStages,{model:data,attempt},`attempt-${attempt.id}`));
          row.append(phases.details);
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
  function disclosure(key, className, title) {
    const details = element('details',className);
    details.dataset.flowKey = key;
    const previous = [...graph.querySelectorAll('[data-flow-key]')].find(node => node.dataset.flowKey === key);
    details.open = Boolean(previous?.open);
    const summary = element('summary','',title);
    summary.dataset.flowFocus = key;
    details.append(summary);
    return {details,summary};
  }
  const usageValue = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unavailable';
  function phaseUsage(attempt, phase) {
    const passes = attempt?.usage?.passes.filter(pass => pass.phase === phase) || [];
    const content = element('div','session-usage');
    content.append(element('h5','try-label','Phase usage'));
    if (!passes.length) {
      content.append(element('p','node-detail','Token usage not reported for this phase.'));
      return content;
    }
    const totals = element('dl','usage-grid');
    for (const [key,label] of Object.entries({input_tokens:'Input',output_tokens:'Output',cached_input_tokens:'Cache read',cache_creation_input_tokens:'Cache creation'})) {
      const known = passes.every(pass => Number.isSafeInteger(pass[key]) && pass[key] >= 0);
      totals.append(element('dt','',label),element('dd','',known ? usageValue(passes.reduce((sum,pass) => sum + pass[key],0)) : 'Unavailable'));
    }
    content.append(totals,element('p','node-detail',`${passes.length} reported session(s) · provider-reported tokens, not charges.`));
    return content;
  }
  function phaseActivity(model, attempt, phase, scope) {
    const content = element('div','session-activity');
    content.append(element('h5','try-label','Public phase activity'));
    const events = terminalEvents.filter(event => {
      const sourceMatches = model && event.source === `${model.title.slice(0,80)} / ${phase}`;
      const exactAttempt = event.model_attempt_id !== undefined && String(event.model_attempt_id) === String(attempt?.id);
      if (event.model_attempt_id !== undefined && !exactAttempt) return false;
      if (!(sourceMatches || exactAttempt && event.phase === phase)) return false;
      const at = Date.parse(event.at);
      return attempt && at >= Date.parse(attempt.started_at) && (!attempt.finished_at || at <= Date.parse(attempt.finished_at));
    });
    if (!events.length) {
      content.append(element('p','node-detail','No public activity recorded for this phase in the recent feed.'));
      return content;
    }
    const list = element('ol','phase-activity');
    list.tabIndex = 0;
    list.setAttribute('aria-label',`${phaseNames[phase]} public activity`);
    list.dataset.flowFocus = `activity-${scope}-${phase}`;
    list.dataset.flowScroll = list.dataset.flowFocus;
    for (const event of events) {
      const row = element('li','');
      const time = element('time','',shortTime(event.at));
      time.dateTime = event.at;
      row.append(time,element('span','',event.message));
      list.append(row);
    }
    content.append(list,element('p','node-detail','Recent public progress only · the page retains the latest 100 events.'));
    return content;
  }
  function reviewTree(stages, current, scope = 'current') {
    const tree = element('section','review-tree');
    tree.setAttribute('aria-label','Service-managed reviewer sessions');
    const state = current?.attempt.state || 'pending';
    const phases = {code_review:'code',security_review:'security',test_review:'tests',synthesis:'synthesis'};
    if (Array.isArray(current?.attempt.phases)) {
      stages = stages.map(stage => {
        const recorded = current.attempt.phases.find(item => item.phase === phases[stage.id]);
        return {...stage,state:recorded?.state || 'pending',started_at:recorded?.started_at || null,finished_at:recorded?.finished_at || null,coverage_complete:recorded?.coverage_complete ?? null,waiting:recorded?.waiting === true};
      });
    }
    const root = element('article',`review-orchestrator node-${state}`);
    const heading = element('div','session-heading');
    heading.append(element('h3','','Review orchestrator'),element('span','node-state',state === 'success' ? 'Complete' : stateLabel(state,current?.attempt.phase)));
    root.append(heading,element('p','orchestrator-note','Service-managed sessions'));
    root.append(element('p','session-model',current ? `${current.model.title} · ${current.model.model}` : 'Model not assigned'));
    root.append(element('p','session-elapsed',current ? duration(current.attempt.started_at,current.attempt.finished_at,state) || 'Timing not recorded' : 'Not started'));
    tree.append(root);
    const children = element('ul','review-sessions');
    children.setAttribute('aria-label','Specialist review sessions');
    let adjudication = null;
    for (const stage of stages) {
      const phase = phases[stage.id];
      const state = stage.state;
      const key = `session-${scope}-${phase}`;
      const {details,summary} = disclosure(key,`review-session node-${state}`,'');
      details.dataset.phase = phase;
      details.dataset.state = stage.waiting ? 'waiting' : state;
      details.dataset.stageId = stage.id;
      const title = element('span','session-title',stage.title);
      const status = element('span','node-state',stage.waiting ? 'Waiting for capacity' : state === 'skipped' ? 'Skipped' : labels[state] || 'Needs attention');
      const previous = graph.querySelector(`[data-flow-key="${key}"]`);
      if (previous && previous.dataset.state !== details.dataset.state) status.classList.add('motion-enter');
      const timing = duration(stage.started_at,stage.finished_at || current?.attempt.finished_at,state);
      summary.append(title,status,element('span','session-model',current?.model.model || 'Model not assigned'),element('span','session-elapsed',timing || (state === 'unknown' || stage.started_at ? 'Timing not recorded' : 'Not started')));
      const body = element('div','session-content');
      body.append(element('p','node-detail',stage.detail));
      if (stage.coverage_complete === false) body.append(element('p','node-detail','Assessment delivered. Coverage caveats are retained in the final review.'));
      if (state === 'skipped') body.append(element('p','node-detail','No review was delivered to the orchestrator for this session. See model-attempt history for recorded failures or cancellation.'));
      body.append(phaseUsage(current?.attempt,phase),phaseActivity(current?.model,current?.attempt,phase,scope));
      details.append(body);
      if (phase === 'synthesis') {
        adjudication = element('div','review-adjudication');
        adjudication.append(element('p','review-dependency','After specialist reviews'),details);
      } else {
        const item = element('li','');
        item.append(details); children.append(item);
      }
    }
    tree.append(children);
    if (adjudication) {
      tree.classList.add('has-adjudication');
      tree.append(adjudication);
    }
    return tree;
  }
  function renderFlow(flow) {
    const fragment = document.createDocumentFragment();
    const focused = graph.contains(document.activeElement) ? document.activeElement.dataset.flowFocus : null;
    const focusedGroup = focused ? document.activeElement.closest('[data-flow-key]')?.dataset.flowKey : null;
    const scrollPositions = new Map([...graph.querySelectorAll('[data-flow-scroll]')].map(node => [node.dataset.flowScroll,node.scrollTop]));
    const specialists = flow.stages.filter(n => ['code_review','security_review','test_review','synthesis'].includes(n.id));
    const sessions = flow.models.flatMap(model => model.attempts.map(attempt => ({model,attempt})));
    const current = sessions.reduce((latest,row) => !latest || row.attempt.id > latest.attempt.id ? row : latest,null);
    function overall(nodes) {
      if (!nodes.length) return 'pending';
      if (nodes.some(n => n.state === 'blocked')) return 'blocked';
      if (nodes.some(n => ['running','cancelling'].includes(n.state))) return 'running';
      if (nodes.every(n => ['done','success','not_needed','unused'].includes(n.state))) return 'done';
      if (nodes.some(n => ['attention','failed','refused','unavailable','quota','incomplete','interrupted'].includes(n.state))) return 'attention';
      if (nodes.some(n => n.state === 'cancelled')) return 'cancelled';
      if (nodes.some(n => n.state === 'unknown')) return 'unknown';
      if (nodes.some(n => ['done','success','not_needed'].includes(n.state))) return panel.dataset.jobState === 'running' ? 'running' : 'attention';
      return 'pending';
    }
    const reviewNodes = current?.attempt.phases?.length ? current.attempt.phases.filter(phase => ['code','security','tests','synthesis'].includes(phase.phase)) : specialists;
    const overview = element('ol','review-progress');
    const groups = [
      ['evidence','Evidence','Source, conversations and project agreements.',flow.stages.filter(n => ['files','discussion','docs','graph','intent'].includes(n.id))],
      ['checks','Checks','Checks run against this exact commit.',flow.stages.filter(n => ['checks','tests'].includes(n.id))],
      ['review','Review',flow.model_detail || 'Recorded failure causes and model fallback.',specialists.length ? [...reviewNodes,...(current ? [current.attempt] : [])] : current ? [current.attempt] : flow.legacy ? flow.models : []],
      ['delivery','GitHub','Recorded delivery confirmations; review content stays on GitHub.',flow.stages.filter(n => ['publication','pr_summary','inline_findings','discussion_replies','related_pr_comments','related_issue_comments','labels','sheet'].includes(n.id))],
    ];
    for (const [id,title,description,nodes] of groups) {
      const state = overall(nodes);
      const item = element('li','progress-item');
      const {details,summary} = disclosure(`progress-${id}`,'progress-step','');
      summary.classList.add(`node-${state}`);
      details.dataset.progressId = id;
      details.dataset.state = state;
      const status = element('span','node-state',labels[state] || 'Not started');
      const previous = graph.querySelector(`[data-progress-id="${id}"]`);
      if (previous && previous.dataset.state !== state) status.classList.add('motion-enter');
      summary.append(element('span','progress-title',title),status);
      const content = element('div','progress-content');
      content.append(element('p','lane-description',description));
      if (id === 'review') {
        if (specialists.length) content.append(reviewTree(specialists,current));
        const models = flow.models.filter(model => model.attempts.length || flow.legacy);
        if (models.length) {
          const grid = element('div','node-track model-track');
          models.forEach((model,index) => grid.append(card(model,index,true,specialists)));
          if (specialists.length) {
            const history = disclosure('model-history','disclosure model-history',`Model attempts · ${sessions.length} recorded`);
            history.summary.append(element('span','disclosure-hint','Failures, fallback & total usage'));
            history.details.append(grid); content.append(history.details);
          } else content.append(grid);
        } else if (!specialists.length) content.append(element('p','node-detail','No model session recorded.'));
        if (current && ['attention','failed','quota'].includes(panel.dataset.jobState) && current.attempt.failure_reason) {
          content.append(element('p','review-failure',`${current.attempt.failure_reason} ${current.attempt.next_action || ''}`));
        }
      } else {
        const grid = element('div','node-track');
        nodes.forEach((node,index) => grid.append(card(node,index)));
        if (!nodes.length) grid.append(element('p','node-detail','No stage configured for this attempt.'));
        content.append(grid);
      }
      details.append(content); item.append(details); overview.append(item);
    }
    fragment.append(overview);
    graph.replaceChildren(fragment);
    graph.querySelectorAll('[data-flow-scroll]').forEach(node => {node.scrollTop = scrollPositions.get(node.dataset.flowScroll) || 0;});
    if (focused) {
      const target = [...graph.querySelectorAll('[data-flow-focus]')].find(node => node.dataset.flowFocus === focused) || [...graph.querySelectorAll('[data-flow-key]')].find(node => node.dataset.flowKey === focusedGroup)?.querySelector('summary');
      target?.focus({preventScroll:true});
    }
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
      document.querySelectorAll('[data-review-duration]').forEach(timer => {
        timer.dataset.start = data.started_at || '';
        timer.dataset.finish = data.finished_at || '';
        timer.dataset.status = data.status;
      });
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
        const states = {queued:'Waiting for an available worker',attention:'Paused · see the model nodes and operational note',failed:'Stopped · see the operational note',completed:'Processing finished · open GitHub for the review',cancelled:'Cancelled · no further review work',superseded:'Superseded by a newer review state'};
        text(heartbeat,states[data.status] || 'Review is not running');
      }
      const retryControl = document.querySelector("[data-job-retry-control]");
      if (retryControl) {
        retryControl.hidden = !data.retry_available;
        retryControl.querySelector('[data-retry-label]').textContent = data.delivery_retry ? 'Retry delivery ↻' : 'Retry review ↻';
        retryControl.querySelector('[data-retry-explanation]').textContent = data.delivery_retry ? 'Deliver the saved review without another model call. Existing receipts and current GitHub state are rechecked.' : 'Starts a fresh attempt from the first model. The current commit, reviewer and on review label must still match.';
      }
      let changed = false;
      for (const event of data.events) {
        if(event.id<=latest) continue;
        terminalEvents.push(event); latest=event.id; changed=true;
      }
      terminalEvents=terminalEvents.slice(-100);
      renderFlow(data.flow);
      if(changed || !terminalEvents.length) renderTranscript();
    } catch {
      if (!paused) text(connection,'Connection interrupted · retrying. Nodes may be stale.');
    } finally { clearTimeout(timeout); busy = false; }
  }
  graph.addEventListener('click',event => {
    const summary = event.target.closest('summary');
    if (!summary || !graph.contains(summary) || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    requestAnimationFrame(() => {
      if (summary.parentElement?.open) summary.nextElementSibling?.animate([{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'translateY(0)'}],{duration:180,easing:'ease-out'});
    });
  });
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
