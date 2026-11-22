// Autonomous Russian legal consultant agent UI:
// 1. Auto-index uploaded documents immediately (TXT, MD, DOCX, PDF) + 1-click "Разобрать пакет (эссе и риски)" banner.
// 2. Agent Execution mode toggle ("Режим агента") so assignments produce ready DOCX files in Results + structured report + option buttons.
// 3. Local Russian Legal Knowledge Base dialog + toggle for open Russian legal web search in Model Settings.

let agentModeEnabled = true;

(function initAgentConsultantUI() {
  const style = document.createElement('style');
  style.textContent = `
    .agent-banner{max-width:960px;margin:0 auto 14px;background:#f0f5fc;border:1px solid #bdd0e6;border-radius:7px;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
    .agent-banner b{display:block;font-size:13px;color:#1f3b5c}
    .agent-banner small{display:block;font-size:11px;color:#52667d;margin-top:2px}
    .agent-banner .agent-banner-actions{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
    .agent-card{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}
    .agent-steps{font-size:11px;color:#4a5d73;background:#f7f9fc;border:1px solid #dde5ef;border-radius:5px;padding:9px 12px;margin-bottom:12px}
    .agent-steps summary{cursor:pointer;font-weight:600;color:#2b4c73}
    .agent-steps ul{margin:6px 0 2px;padding-left:18px}
    .agent-options{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
    .agent-options button{background:#edf4fc;border-color:#adc5e0;color:#1f436e;font-size:12px;font-weight:500;padding:7px 12px;text-align:left}
    .agent-options button:hover{background:#dfeaf8}
    .agent-badge{font-size:10px;background:#e8f1fb;color:#28517d;border:1px solid #bcd1ea;border-radius:3px;padding:2px 7px;text-transform:none;letter-spacing:0}
  `;
  document.head.append(style);

  // Add Legal KB button in left rail footer
  const railFoot = $('.railfoot');
  if (railFoot && !$('#legalKbButton')) {
    const kbBtn = document.createElement('button');
    kbBtn.id = 'legalKbButton';
    kbBtn.textContent = 'Правовая база РФ и практика';
    kbBtn.style.display = 'block';
    kbBtn.onclick = openLegalKbDialog;
    railFoot.prepend(kbBtn);
  }

  // Add Agent Mode toggle button in composer bottom bar + Intake button in Documents section
  const deepBtn = $('#deep');
  if (deepBtn && !$('#agentModeBtn')) {
    const agentBtn = document.createElement('button');
    agentBtn.type = 'button';
    agentBtn.id = 'agentModeBtn';
    agentBtn.className = 'mode';
    agentBtn.setAttribute('aria-pressed', 'true');
    agentBtn.textContent = 'Агент-исполнитель';
    agentBtn.title = 'Включено: Юрий сам читает все разделы документов, сверяется с базой права РФ, создаёт готовые DOCX в «Результаты» и отчитывается';
    agentBtn.onclick = () => {
      if (generationBusy) return;
      agentModeEnabled = !agentModeEnabled;
      agentBtn.setAttribute('aria-pressed', String(agentModeEnabled));
      toast(agentModeEnabled ? 'Режим агента включён: Юрий будет выполнять поручения под ключ и создавать DOCX.' : 'Включён обычный режим текстового чата.');
    };
    deepBtn.before(agentBtn);
  }

  const addDocsBtn = $('#addDocuments');
  if (addDocsBtn && !$('#analyzePackageDocsBtn')) {
    const analyzeBtn = document.createElement('button');
    analyzeBtn.type = 'button';
    analyzeBtn.id = 'analyzePackageDocsBtn';
    analyzeBtn.textContent = '✦ Разобрать пакет (эссе и риски)';
    analyzeBtn.onclick = () => startAgentRun('intake');
    addDocsBtn.before(analyzeBtn);
  }
})();

// Wrap addFiles to automatically extract text locally right after upload
const baseAddFiles = addFiles;
addFiles = async function(files, targetId) {
  const pBefore = state?.projects.find(x => x.id === targetId);
  const existingIds = new Set((pBefore?.documents || []).map(d => d.id));
  await baseAddFiles(files, targetId);
  const pAfter = state?.projects.find(x => x.id === targetId);
  if (!pAfter || pAfter.archived) return;
  const added = pAfter.documents.filter(d => !existingIds.has(d.id) && ['TXT', 'MD', 'DOCX', 'PDF'].includes(d.ext));
  if (!added.length) return;
  indexingDocument = true;
  let readyCount = 0;
  try {
    saveBadge('Локальное чтение загруженных документов…');
    for (const d of added) {
      try {
        const res = await api('/api/projects/' + encodeURIComponent(pAfter.id) + '/documents/' + encodeURIComponent(d.id) + '/index', { method: 'POST' });
        if (res.status === 'ready') readyCount++;
      } catch {}
    }
  } finally {
    indexingDocument = false;
    saveBadge('Сохранено на диске');
  }
  if (current().id === pAfter.id) {
    await refreshDocumentStatus(pAfter.id);
    renderChat();
  }
  if (readyCount > 0) {
    toast(`Файлы прочитаны автоматически (${readyCount} из ${added.length}). Нажмите «Разобрать пакет документов», чтобы получить эссе и варианты решений.`);
  }
};

async function ensureReadyPinnedDocuments(p) {
  const pinned = p.documents.filter(d => d.pinned && ['TXT', 'MD', 'DOCX', 'PDF'].includes(d.ext));
  if (!pinned.length) return [];
  const statusResp = await api('/api/projects/' + encodeURIComponent(p.id) + '/documents');
  const byId = Object.fromEntries((statusResp.documents || []).map(d => [d.id, d]));
  const readyIds = [];
  for (const d of pinned) {
    let st = byId[d.id];
    if (!st || st.status === 'added' || st.status === 'stale') {
      try {
        st = await api('/api/projects/' + encodeURIComponent(p.id) + '/documents/' + encodeURIComponent(d.id) + '/index', { method: 'POST' });
      } catch {}
    }
    if (st && st.status === 'ready') readyIds.push(d.id);
  }
  return readyIds.slice(0, 16);
}

async function startAgentRun(mode = 'execute', customPrompt = null) {
  if (indexingDocument) {
    toast('Дождитесь завершения чтения документа.');
    return;
  }
  if (generationBusy || startingRequest || !writable()) return;
  if (!modelConfig?.hasKey || !modelConfig?.modelB || !modelConfig?.consent) {
    await openModelSettings();
    return;
  }
  const p = current();
  const t = currentThread();
  if (p.tab !== 'chat') switchTab('chat');

  const promptText = (customPrompt !== null ? customPrompt : $('#input').value).trim();
  if (mode === 'execute' && !promptText) {
    toast('Опишите поручение для Юрия или нажмите «Разобрать пакет документов».');
    return;
  }

  let docIds = [];
  if (p.documents.some(d => d.pinned)) {
    if (!t.useDocuments) {
      const ok = confirm('Для анализа документов проекта нужно разрешить передачу их текста в API модели в этом обсуждении. Включить разрешение и продолжить?');
      if (!ok) return;
      t.useDocuments = true;
      $('#docConsent').checked = true;
      syncChatContext();
      await persist();
    }
    docIds = await ensureReadyPinnedDocuments(p);
    if (mode === 'intake' && !docIds.length) {
      toast('Нет готовых к чтению закреплённых документов (поддерживаются TXT, MD, DOCX, текстовый PDF).');
      return;
    }
  } else if (mode === 'intake') {
    toast('Сначала загрузите и закрепите документы в проект.');
    return;
  }

  const requestId = uid();
  try {
    await flush();
    startingRequest = true;
    applyReadOnly();
    const res = await api('/api/agent/run', {
      method: 'POST',
      body: JSON.stringify({
        requestId,
        projectId: p.id,
        threadId: t.id,
        mode,
        text: promptText,
        deep: !!t.deep,
        revision,
        documentIds: docIds,
        documentsConsent: !!t.useDocuments,
      }),
    });
    streamingId = res.requestId;
    generationBusy = true;
    startingRequest = false;
    if (customPrompt === null) {
      t.draft = '';
      if (currentThread().id === t.id) $('#input').value = '';
    }
    t.scroll = null;
    await loadServerState();
    followModelStream(streamingId);
  } catch (e) {
    startingRequest = false;
    generationBusy = false;
    streamingId = null;
    applyReadOnly();
    toast(e.message);
  }
}

// Intercept regular send() when Agent Mode is active and it is not a local calendar-only command
const baseSendModelMessage = sendModelMessage;
sendModelMessage = async function() {
  if (generationBusy || !agentModeEnabled) {
    return baseSendModelMessage();
  }
  const content = $('#input').value.trim();
  if (!content) return;
  try {
    const route = await api('/api/chat/route', { method: 'POST', body: JSON.stringify({ text: content }) });
    if (route.local) return baseSendModelMessage();
  } catch {}
  return startAgentRun('execute', null);
};

// Render intake banner in Chat when documents exist and no intake essay has been run yet
const baseRenderChatForAgent = renderChat;
renderChat = function() {
  baseRenderChatForAgent();
  const p = current();
  const t = currentThread();
  const box = $('#messages');
  if (!box || p.archived) return;
  const pinnedDocs = p.documents.filter(d => d.pinned);
  const hasAgentIntake = t.messages.some(m => m.agentMeta?.mode === 'intake');
  if (pinnedDocs.length > 0 && !hasAgentIntake && !generationBusy) {
    const banner = document.createElement('div');
    banner.className = 'agent-banner';
    banner.innerHTML =
      '<div><b>Пакет документов готов к агентному разбору (' + pinnedDocs.length + ' док.)</b>' +
      '<small>Юрий прочитает весь пакет целиком, составит краткое эссе, найдёт пробелы/нестыковки по праву РФ и предложит решения на выбор.</small></div>' +
      '<div class="agent-banner-actions"><button type="button" class="primary" data-start-intake>✦ Разобрать пакет документов</button></div>';
    banner.querySelector('[data-start-intake]').onclick = () => startAgentRun('intake');
    box.prepend(banner);
  }
};

// Enhance assistant message rendering for agentReport messages (steps, created DOCX links, calendar proposals, option buttons)
const prevAgentMessageElement = messageElement;
messageElement = function(m) {
  const el = prevAgentMessageElement(m);
  if (m.role !== 'assistant' || !m.agentMeta) return el;

  const head = el.querySelector('.messagehead');
  if (head) {
    const badge = document.createElement('span');
    badge.className = 'agent-badge';
    badge.textContent = m.agentMeta.mode === 'intake' ? 'Первичный разбор пакета' : 'Отчёт агента-исполнителя';
    head.append(badge);
  }

  const bubble = el.querySelector('.bubble');
  if (!bubble) return el;

  const meta = m.agentMeta;
  if (Array.isArray(meta.steps) && meta.steps.length) {
    const details = document.createElement('details');
    details.className = 'agent-steps';
    details.innerHTML =
      '<summary>Выполненные шаги агента (' + meta.steps.length + ')</summary><ul>' +
      meta.steps.map(s => '<li>' + esc(s.label) + '</li>').join('') +
      '</ul>';
    const foot = bubble.querySelector('.messagefoot');
    if (foot) foot.after(details);
    else bubble.prepend(details);
  }

  if (Array.isArray(meta.createdResults) && meta.createdResults.length) {
    const wrap = document.createElement('div');
    wrap.className = 'agent-card';
    wrap.innerHTML = '<b style="font-size:12px;display:block;margin-bottom:6px">Подготовленные документы в «Результатах»:</b>';
    for (const r of meta.createdResults) {
      const row = document.createElement('div');
      row.className = 'resultlink';
      row.innerHTML =
        '<span class="filemark">DOCX</span>' +
        '<div><b>' + esc(r.name) + '</b><small>' + esc(r.documentType || 'Черновик DOCX') +
        (r.unresolvedPlaceholders?.length ? ' · уточнить полей: ' + r.unresolvedPlaceholders.length : ' · сохранён на диске') +
        '</small></div>' +
        '<button type="button" data-open-agent-res="' + esc(r.id) + '">Открыть</button>' +
        '<button type="button" class="primary" data-dl-agent-res="' + esc(r.id) + '">Скачать DOCX</button>';
      row.querySelector('[data-open-agent-res]').onclick = () => openResult(r.id);
      row.querySelector('[data-dl-agent-res]').onclick = () => downloadDOCXResult(current().id, r.id, 'docx');
      wrap.append(row);
    }
    bubble.append(wrap);
  }

  if (Array.isArray(meta.eventProposals) && meta.eventProposals.length) {
    const evWrap = document.createElement('div');
    evWrap.className = 'seclink';
    meta.eventProposals.forEach(prop => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = '＋ В календарь: ' + (prop.title || 'Контрольный срок') + (prop.date ? ' (' + prop.date + ')' : '');
      btn.onclick = () => openEventDialog(current().id, null, { ...prop });
      evWrap.append(btn);
    });
    bubble.append(evWrap);
  }

  if (m.status === 'complete' && Array.isArray(meta.options) && meta.options.length) {
    const optWrap = document.createElement('div');
    optWrap.className = 'agent-card';
    optWrap.innerHTML = '<b style="font-size:12px;display:block;color:#244366">Предлагаемые решения на выбор (нажмите для выполнения или напишите свою задачу ниже):</b>';
    const btns = document.createElement('div');
    btns.className = 'agent-options';
    meta.options.forEach(opt => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = '→ ' + opt.label;
      b.title = opt.prompt;
      b.disabled = generationBusy || current().archived;
      b.onclick = () => startAgentRun('execute', opt.prompt);
      btns.append(b);
    });
    optWrap.append(btns);
    bubble.append(optWrap);
  }

  return el;
};

// Inject open Russian legal web search checkbox into openModelSettings
const baseOpenModelSettings = openModelSettings;
openModelSettings = async function() {
  await baseOpenModelSettings();
  const consentInput = $('#apiConsent');
  if (!consentInput || $('#webLegalSearchEnabled')) return;
  const label = document.createElement('label');
  label.innerHTML =
    '<input id="webLegalSearchEnabled" type="checkbox"> Разрешить агенту поиск норм и судебной практики по открытым правовым сайтам РФ (pravo.gov.ru, vsrf.ru, sudact.ru, consultant.ru, garant.ru)';
  consentInput.closest('label').before(label);
  $('#webLegalSearchEnabled').checked = !!modelConfig?.webLegalSearchEnabled;

  const origSaveClick = $('#saveModels').onclick;
  $('#saveModels').onclick = async () => {
    try {
      await api('/api/models/settings', {
        method: 'POST',
        body: JSON.stringify({ webLegalSearchEnabled: $('#webLegalSearchEnabled').checked }),
      });
    } catch {}
    return origSaveClick();
  };
};

// Local Russian Legal Knowledge Base management dialog
async function openLegalKbDialog() {
  try {
    const data = await api('/api/legal-kb');
    const entries = data.entries || [];
    showDialog(
      'Правовая база РФ и судебная практика',
      '<p class="note">Локальный справочник кодексов РФ, постановлений Пленума ВС РФ и ваших подборок судебной практики. Агент Юрий автоматически сверяется с этой базой (и при включённой опции в настройках моделей — с открытыми правовыми сайтами РФ).</p>' +
        '<details style="margin:10px 0"><summary style="cursor:pointer;font-weight:600;color:var(--accent)">＋ Добавить статью кодекса / позицию ВС РФ / судебный акт</summary>' +
        '<label>Заголовок<input type="text" id="kbTitle" maxlength="300" placeholder="Например: Постановление Пленума ВС РФ № 54 — уступка требования"></label>' +
        '<div class="formrow"><label>Категория<input type="text" id="kbCategory" maxlength="120" value="Судебная практика ВС РФ"></label>' +
        '<label>Источник / реквизиты<input type="text" id="kbCitation" maxlength="300" placeholder="ГК РФ ст. 382–390; Пленум ВС РФ № 54"></label></div>' +
        '<label>Текст нормы или правовой позиции<textarea id="kbBody" maxlength="100000" style="min-height:120px"></textarea></label>' +
        '<button type="button" class="primary" id="saveKbEntry">Сохранить в базу</button><p class="note" id="kbFeedback"></p></details>' +
        '<div style="display:flex;gap:8px;margin:12px 0"><input type="text" id="kbSearchInput" placeholder="Проверить поиск по правовой базе…" style="flex:1"><button type="button" id="kbSearchBtn">Найти</button></div>' +
        '<div id="kbList">' +
        entries
          .map(
            e =>
              '<div class="activity"><b>' +
              esc(e.title) +
              '</b> <small>' +
              esc(e.category + ' · ' + e.citation) +
              '</small><p class="note" style="margin:4px 0">' +
              esc(e.preview || e.snippet || '') +
              '</p>' +
              (e.builtin ? '' : '<button type="button" class="quiet" data-del-kb="' + esc(e.id) + '">Удалить</button>') +
              '</div>'
          )
          .join('') +
        '</div>',
      true
    );

    $('#saveKbEntry').onclick = async () => {
      try {
        $('#saveKbEntry').disabled = true;
        await api('/api/legal-kb', {
          method: 'POST',
          body: JSON.stringify({
            title: $('#kbTitle').value,
            category: $('#kbCategory').value,
            citation: $('#kbCitation').value,
            body: $('#kbBody').value,
          }),
        });
        toast('Правовая позиция добавлена в базу Юрия.');
        await openLegalKbDialog();
      } catch (err) {
        if ($('#kbFeedback')) $('#kbFeedback').textContent = err.message;
        if ($('#saveKbEntry')) $('#saveKbEntry').disabled = false;
      }
    };

    $('#kbSearchBtn').onclick = async () => {
      const q = $('#kbSearchInput').value.trim();
      const res = await api('/api/legal-kb' + (q ? '?q=' + encodeURIComponent(q) : ''));
      $('#kbList').innerHTML = (res.entries || []).length
        ? res.entries
            .map(
              e =>
                '<div class="activity"><b>' +
                esc(e.title) +
                '</b> <small>' +
                esc((e.category || '') + ' · ' + (e.citation || '')) +
                '</small><p class="note" style="margin:4px 0">' +
                esc(e.preview || e.snippet || '') +
                '</p></div>'
            )
            .join('')
        : '<p class="note">Совпадений не найдено.</p>';
    };

    $$('[data-del-kb]').forEach(btn => {
      btn.onclick = async () => {
        try {
          await api('/api/legal-kb/' + encodeURIComponent(btn.dataset.delKb), { method: 'DELETE' });
          await openLegalKbDialog();
        } catch (err) {
          toast(err.message);
        }
      };
    });
  } catch (e) {
    toast(e.message);
  }
}
