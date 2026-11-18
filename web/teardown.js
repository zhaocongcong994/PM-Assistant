(() => {
  const url = '/api/teardown/runs';
  const phases = {
    journey: '用户旅程',
    contracts: 'Agent 契约',
    prompts: '参考 Prompt',
    architecture: '全景架构',
    summary: '拆解汇总',
  };
  const statusText = {running: '拆解中', review: '等待确认', failed: '待重试', complete: '已完成'};
  const statusMessage = {
    running: '可以切换到项目工作区，拆解会在后台继续。',
    review: '核对用户旅程和截图证据，确认后开始生成其余报告。',
    complete: '拆解已完成，可以查看或下载报告。',
  };
  const mib = 1024 * 1024;
  const maxFiles = 50;
  const maxTotalBytes = 100 * mib;
  const maxImageBytes = 10 * mib;
  const maxZipBytes = 100 * mib;
  const pageSize = 10;
  let run = null;
  let draftFiles = [];
  let draftArchive = null;
  let busy = false;
  let initialLoad = false;
  let activePhase = 'journey';
  let evidencePage = 0;
  let timer = null;
  let importRequest = null;
  let disposeReport = null;

  function notice(message = '') {
    $('teardownNotice').textContent = message;
    $('teardownNotice').classList.toggle('hidden', !message);
  }

  function archiveSize(bytes) {
    return bytes < mib ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / mib).toFixed(1)} MiB`;
  }

  function renderModels() {
    const model = $('teardownModel');
    const current = model.value;
    const options = config.providers.filter(provider => provider.has_key).map(provider => {
      const group = document.createElement('optgroup');
      group.label = provider.name;
      for (const name of provider.models) {
        group.append(new Option(name, modelValue({provider_id: provider.id, model: name})));
      }
      return group;
    });
    model.replaceChildren(new Option('请选择支持图片输入的模型', ''), ...options);
    model.value = current;
    if (!model.value && run) model.value = modelValue(run.model_selection);
    renderRetryModels();
    renderControls();
  }

  function renderRetryModels() {
    const select = $('teardownRetryModel');
    if (!select) return;
    const selected = select.value || modelValue(run.model_selection);
    select.replaceChildren(...Array.from($('teardownModel').children, option => option.cloneNode(true)));
    select.value = selected;
    if (!select.value) select.selectedIndex = 0;
  }

  function renderControls() {
    const total = draftFiles.reduce((size, item) => size + item.file.size, 0);
    const canAddImages = !busy && !draftArchive && draftFiles.length < maxFiles && total < maxTotalBytes;
    const canAddArchive = !busy && !draftArchive && !draftFiles.length;
    $('teardownAddFiles').disabled = $('teardownFiles').disabled = !canAddImages;
    $('teardownAddArchive').disabled = $('teardownArchive').disabled = !canAddArchive;
    $('teardownClearFiles').disabled = busy;
    $('teardownClearFiles').classList.toggle('hidden', !draftFiles.length && !draftArchive);
    $('teardownDraftStatus').textContent = draftArchive
      ? `已选 1 个 ZIP · ${archiveSize(draftArchive.size)}`
      : `已选 ${draftFiles.length} / ${maxFiles} 张 · ${(total / mib).toFixed(1)} / 100 MiB`;
    $('teardownStart').disabled = busy || (!draftFiles.length && !draftArchive) || !$('teardownModel').value;
    $('teardownHistory').disabled = busy;
    const retryModel = $('teardownRetryModel');
    if (retryModel) retryModel.disabled = busy;
    $('teardownForm').classList.toggle('hidden', Boolean(run));
    $('teardownRun').classList.toggle('hidden', !run);
    for (const button of $('teardownRun').querySelectorAll('[data-action]')) {
      button.disabled = busy || (button.dataset.action === 'import' && run?.status !== 'complete');
    }
  }

  function setRun(next) {
    if (next?.id !== run?.id) {
      activePhase = Object.keys(next?.results || {}).at(-1) || 'journey';
      evidencePage = 0;
    } else if (next?.status === 'complete' && run.status !== 'complete') {
      activePhase = 'summary';
    }
    run = next;
    $('teardownRun').innerHTML = run ? runMarkup(run) : '';
    $('teardownHistory').value = run?.id || '';
    renderRetryModels();
    renderControls();
    loadReport();
    schedulePoll();
  }

  async function refreshHistory() {
    const data = await request(url);
    const history = $('teardownHistory');
    const options = data.runs.map(item => new Option(
      `${item.title} · ${statusText[item.status] || item.status}`, item.id
    ));
    history.replaceChildren(new Option('新建拆解', ''), ...options);
    history.classList.toggle('hidden', !options.length);
    history.value = run?.id || '';
    return data.runs;
  }

  async function loadHistory() {
    if (initialLoad) return;
    initialLoad = true;
    try {
      const runs = await refreshHistory();
      if (runs.length && !run && !draftFiles.length && !draftArchive && !busy && !$('teardownName').value) {
        await openRun(runs[0].id);
      }
    } catch (error) {
      initialLoad = false;
      notice(error.message);
    }
  }

  async function openRun(id) {
    if (!id) {
      notice();
      setRun(null);
      $('teardownName').focus();
      return;
    }
    busy = true;
    renderControls();
    notice();
    try {
      const data = await request(`${url}/${id}`);
      setRun(data.run);
    } catch (error) {
      notice(error.message);
      $('teardownHistory').value = run?.id || '';
    } finally {
      busy = false;
      renderControls();
    }
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (teardownVisible && run?.status === 'running') timer = setTimeout(poll, 1000);
  }

  async function poll() {
    const current = run;
    if (!teardownVisible || current?.status !== 'running') return;
    try {
      const data = await request(`${url}/${current.id}`);
      if (run !== current) return;
      notice();
      const next = data.run;
      if (next.status !== current.status || next.phase !== current.phase || next.upload_progress !== current.upload_progress
          || Object.keys(next.results).length !== Object.keys(current.results).length) {
        setRun(next);
        if (next.status !== 'running') await refreshHistory();
      }
    } catch (error) {
      if (run === current) notice(error.message);
    } finally {
      schedulePoll();
    }
  }

  function renderDraftFiles() {
    if (draftArchive) {
      $('teardownDraftFiles').innerHTML = `<li class="teardown-draft-archive">
        <strong>ZIP · ${escapeHtml(draftArchive.name)}</strong>
        <button type="button" class="icon-button" data-remove-archive aria-label="移除 ZIP">×</button>
      </li>`;
      renderControls();
      return;
    }
    $('teardownDraftFiles').innerHTML = draftFiles.map((item, index) => {
      const name = escapeHtml(item.file.name);
      const label = `<span><b>${index + 1}. ${name}</b></span>`;
      const preview = item.preview
        ? `<button type="button" class="teardown-preview" data-preview="${index}"><img alt="" loading="lazy" src="${item.preview}">${label}</button>`
        : `<div class="teardown-preview teardown-filename">${label}</div>`;
      return `<li>
        ${preview}
        <div class="teardown-file-actions">
          <button type="button" class="icon-button" data-move="-1" data-index="${index}" aria-label="上移 ${name}" ${index === 0 ? 'disabled' : ''}>↑</button>
          <button type="button" class="icon-button" data-move="1" data-index="${index}" aria-label="下移 ${name}" ${index === draftFiles.length - 1 ? 'disabled' : ''}>↓</button>
          <button type="button" class="icon-button" data-remove="${index}" aria-label="移除 ${name}">×</button>
        </div>
      </li>`;
    }).join('');
    renderControls();
  }

  function addFiles(files) {
    if (draftArchive) return;
    notice();
    let error = '';
    let total = draftFiles.reduce((size, item) => size + item.file.size, 0);
    const extensions = $('teardownFiles').accept.split(',');
    for (const file of files) {
      const extension = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
      if (!file.size || file.size > maxImageBytes || !extensions.includes(extension)) {
        error = '请选择受支持的图片格式，每张最多 10 MiB。';
        continue;
      }
      if (draftFiles.length >= maxFiles) {
        error = '已达到 50 张截图上限。';
        break;
      }
      if (total + file.size > maxTotalBytes) {
        error = '截图总大小不能超过 100 MiB。';
        continue;
      }
      const canPreview = /\.(png|apng|jpe?g|jfif|webp|gif|bmp|ico|avif)$/i.test(file.name);
      draftFiles.push({file, preview: canPreview ? URL.createObjectURL(file) : null});
      total += file.size;
    }
    renderDraftFiles();
    notice(error);
  }

  function addArchive(file) {
    if (!file || draftArchive || draftFiles.length) return;
    if (!file.size || !/\.zip$/i.test(file.name) || file.size > maxZipBytes) {
      notice('请选择不超过 100 MiB 的 ZIP。');
      return;
    }
    draftArchive = file;
    notice();
    renderDraftFiles();
  }

  function clearDraft() {
    for (const {preview} of draftFiles) {
      if (preview) URL.revokeObjectURL(preview);
    }
    draftFiles = [];
    draftArchive = null;
    renderDraftFiles();
  }

  async function submit(action) {
    if (busy) return;
    busy = true;
    notice();
    $('teardownStart').textContent = '正在上传…';
    renderControls();
    try {
      let body = {};
      let endpoint = `${url}/${run?.id}/${action}`;
      if (action === 'start') {
        endpoint = url;
        const metadata = {
          title: $('teardownName').value.trim(),
          model_selection: parseModel($('teardownModel').value),
        };
        body = new FormData();
        body.append('metadata', JSON.stringify(metadata));
        if (draftArchive) body.append('archive', draftArchive);
        else for (const {file} of draftFiles) body.append('screenshots', file);
      } else if (action === 'retry') {
        const selection = parseModel($('teardownRetryModel').value);
        if (!selection) throw new Error('请选择重试使用的模型。');
        body.model_selection = selection;
      }
      const result = await request(endpoint, {method: 'POST', body: action === 'start' ? body : JSON.stringify(body)});
      if (action === 'start') {
        clearDraft();
        $('teardownName').value = '';
      }
      setRun(result.run);
      await refreshHistory();
    } catch (error) {
      notice(error.message);
    } finally {
      busy = false;
      $('teardownStart').textContent = '开始拆解';
      renderControls();
    }
  }

  function showImportDialog() {
    if (busy || run?.status !== 'complete') return;
    importRequest = {runId: run.id, requestId: crypto.randomUUID()};
    $('teardownImportProjectName').textContent = run.project_title;
    $('teardownImportError').classList.add('hidden');
    $('teardownImportDialog').showModal();
  }

  async function importProject() {
    const active = importRequest;
    if (busy || !active) return;
    const confirm = $('teardownImportConfirm');
    const cancel = $('teardownImportCancel');
    const error = $('teardownImportError');
    busy = confirm.disabled = cancel.disabled = true;
    error.classList.add('hidden');
    confirm.textContent = '正在导入…';
    renderControls();
    try {
      const {session: project} = await request(`${url}/${active.runId}/project`, {
        method: 'POST', body: JSON.stringify({request_id: active.requestId}),
      });
      $('teardownImportDialog').close();
      openImportedProject(project);
    } catch (failure) {
      error.textContent = failure.message;
      error.classList.remove('hidden');
    } finally {
      busy = confirm.disabled = cancel.disabled = false;
      confirm.textContent = '确认';
      renderControls();
    }
  }

  function showScreenshot(source, label) {
    $('teardownImageTitle').textContent = label;
    $('teardownImage').src = source;
    $('teardownImage').alt = label;
    $('teardownImageDialog').showModal();
  }

  function showEvidence(current, key) {
    const screenshot = current.screenshots.find(item => item.id === key);
    if (!screenshot) return;
    showScreenshot(`${url}/${current.id}/screenshots/${screenshot.id}`, `${screenshot.id} · ${screenshot.name}`);
  }

  function loadReport() {
    disposeReport?.();
    disposeReport = null;
    const panel = $('teardownReport');
    const current = run;
    if (!panel || !current?.results[activePhase]?.length) return;

    panel.classList.add('teardown-results-preview');
    disposeReport = mountReadOnlyReport(
      panel, `${url}/${current.id}/reports/${activePhase}`, `${phases[activePhase]} HTML 报告`,
      link => {
        if (!link.dataset.evidence) return false;
        showEvidence(current, link.dataset.evidence);
        return true;
      },
    );
  }

  function evidenceMarkup(current) {
    if (current.upload_archive) {
      const archive = current.upload_archive;
      return `<a class="teardown-archive-link" href="${url}/${current.id}/source.zip" download>
        <strong>${escapeHtml(archive.name)}</strong>
        <span>ZIP · ${archiveSize(archive.size)}</span>
      </a>`;
    }
    const pageCount = Math.ceil(current.screenshots.length / pageSize);
    const start = evidencePage * pageSize;
    const cards = current.screenshots.slice(start, start + pageSize).map(item => `<button type="button" class="teardown-evidence-item" data-evidence="${item.id}" aria-label="查看截图 ${item.id}">
      <img src="${url}/${current.id}/screenshots/${item.id}" alt="" loading="lazy"><span><b>${item.id}</b> ${escapeHtml(item.name)}</span>
    </button>`).join('');
    const pagination = pageCount > 1 ? `<nav class="teardown-evidence-pages" aria-label="截图证据分页">
      <button type="button" class="icon-button" data-evidence-page="-1" aria-label="上一页" ${evidencePage === 0 ? 'disabled' : ''}>‹</button>
      <span aria-live="polite" tabindex="-1">${evidencePage + 1} / ${pageCount} 页</span>
      <button type="button" class="icon-button" data-evidence-page="1" aria-label="下一页" ${evidencePage === pageCount - 1 ? 'disabled' : ''}>›</button>
    </nav>` : '';
    return `<div class="teardown-evidence-list">${cards}</div>${pagination}`;
  }

  function runMarkup(current) {
    const finished = Object.keys(current.results);
    const steps = Object.entries(phases).map(([key, label]) => `<li>
      <button type="button" data-phase-key="${key}" ${finished.includes(key) ? '' : 'disabled'}
        ${key === activePhase && finished.includes(key) ? 'aria-pressed="true"' : 'aria-pressed="false"'}
        class="${key === current.phase && current.status === 'running' ? 'teardown-step-running' : ''}">${label}</button>
    </li>`).join('');
    const progress = current.status === 'running' && current.upload_progress
      ? `已读取 ${current.upload_progress} / ${current.screenshots.length} 张截图。`
      : '';
    const actions = finished.length ? `<a class="secondary-button" href="${url}/${current.id}/download.zip" download>下载报告</a>
      <button type="button" class="secondary-button" data-action="import" ${current.status === 'complete' ? '' : 'disabled'}>带入项目工作区</button>` : '';
    const completionAction = current.status === 'review' ? '<button type="button" class="send-button" data-action="confirm">确认旅程并继续</button>' : '';
    const retry = current.status === 'failed' ? `<div class="teardown-retry">
      <label>重试模型<select id="teardownRetryModel"></select></label>
      <button type="button" class="secondary-button" data-action="retry">重试当前阶段</button></div>` : '';
    return `<section class="teardown-card teardown-run-head">
      <div class="teardown-run-top"><div><div class="teardown-run-title"><h2>${escapeHtml(current.title)}</h2><span data-status="${current.status}">${statusText[current.status]}</span></div><p>${current.screenshots.length} 张截图 · ${escapeHtml(current.model_selection.model)}</p></div>
        <div class="teardown-actions">${actions}</div></div>
      <ol class="teardown-steps" aria-label="拆解阶段">${steps}</ol>
      <div class="teardown-status" aria-live="polite"><p>${current.status === 'running' ? '<span class="spinner" aria-hidden="true"></span>' : ''}${escapeHtml(current.error || progress || statusMessage[current.status])}</p>${completionAction}</div>
      ${retry}
    </section>
    <div class="teardown-result-grid">
      <section id="teardownReport" class="teardown-card teardown-results" aria-label="分析成果">${finished.length ? '<p>正在加载报告…</p>' : '<p>首份用户旅程会在这里显示。</p>'}</section>
      <aside class="teardown-card teardown-evidence"><h3>截图证据</h3><div id="teardownEvidence">${evidenceMarkup(current)}</div></aside>
    </div>`;
  }

  $('teardownForm').addEventListener('submit', event => {
    event.preventDefault();
    submit('start');
  });
  $('teardownAddFiles').addEventListener('click', () => $('teardownFiles').click());
  $('teardownAddArchive').addEventListener('click', () => $('teardownArchive').click());
  $('teardownFiles').addEventListener('change', event => {
    if (!event.target.disabled) addFiles(event.target.files);
    event.target.value = '';
  });
  $('teardownArchive').addEventListener('change', event => {
    if (!event.target.disabled) addArchive(event.target.files[0]);
    event.target.value = '';
  });
  $('teardownClearFiles').addEventListener('click', () => {
    clearDraft();
    notice();
    $('teardownAddFiles').focus();
  });
  $('teardownModel').addEventListener('change', renderControls);
  $('teardownDraftFiles').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || busy) return;
    if (button.hasAttribute('data-preview')) {
      const item = draftFiles[Number(button.dataset.preview)];
      showScreenshot(item.preview, item.file.name);
    } else if (button.hasAttribute('data-remove-archive')) {
      clearDraft();
      notice();
      $('teardownAddArchive').focus();
    } else if (button.hasAttribute('data-remove')) {
      const [item] = draftFiles.splice(Number(button.dataset.remove), 1);
      if (item.preview) URL.revokeObjectURL(item.preview);
      renderDraftFiles();
    } else {
      const from = Number(button.dataset.index);
      const to = from + Number(button.dataset.move);
      [draftFiles[from], draftFiles[to]] = [draftFiles[to], draftFiles[from]];
      renderDraftFiles();
      $('teardownDraftFiles').querySelector(`[data-index="${to}"][data-move="${button.dataset.move}"]:not(:disabled)`)?.focus();
    }
  });
  $('teardownHistory').addEventListener('change', event => openRun(event.target.value));
  $('teardownImportConfirm').addEventListener('click', importProject);
  $('teardownImportDialog').addEventListener('cancel', event => {
    if (busy) event.preventDefault();
  });
  $('teardownImportDialog').addEventListener('close', event => {
    if (!event.currentTarget.open) importRequest = null;
  });
  $('teardownRun').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || !run) return;
    if (button.dataset.evidence) {
      showEvidence(run, button.dataset.evidence);
    } else if (button.dataset.evidencePage) {
      const direction = Number(button.dataset.evidencePage);
      evidencePage += direction;
      $('teardownEvidence').innerHTML = evidenceMarkup(run);
      const pagination = $('teardownRun').querySelector('.teardown-evidence-pages');
      const next = pagination?.querySelector(`[data-evidence-page="${direction}"]:not(:disabled)`) || pagination?.querySelector('span');
      next?.focus();
    } else if (button.dataset.phaseKey) {
      activePhase = button.dataset.phaseKey;
      setRun(run);
      $('teardownRun').querySelector(`[data-phase-key="${activePhase}"]`)?.focus();
    } else if (button.dataset.action === 'import') {
      showImportDialog();
    } else if (button.dataset.action) {
      submit(button.dataset.action);
    }
  });
  document.addEventListener('nexus:models', renderModels);
  function syncTeardown() {
    if (teardownVisible) loadHistory();
    schedulePoll();
  }
  document.addEventListener('nexus:workbench', syncTeardown);
  renderModels();
  syncTeardown();
})();
