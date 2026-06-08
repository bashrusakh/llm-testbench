  const state = {
    discoveredProviders: [],
    manualProviders: [],
    activeProviderId: null,
    models: [],
    filteredModels: [],
    providerModels: {},
    providerModelSelection: {},
    includedProviderIds: new Set(),
    history: [],
    historySelection: new Set(),
    jobId: null,
    pollTimer: null,
    activeHistoryJobId: null,
    activeBenchmarkType: 'speed',
    // jobId of the live run we are polling/can stop. Kept separate from the
    // history view so that opening a *finished* run from history never
    // hijacks an in-progress run's Stop button.
    liveJobId: null,
    // Pending benchmark payloads to run one after another (e.g. Speed then SQL
    // when both tests are checked). pollJob starts the next one on completion;
    // Stop clears this so the queue does not continue.
    jobQueue: [],
    isBenchmarkRunning: false,
    // Speed view mode: 'aggregated' (default) or 'raw'
    speedViewMode: 'aggregated',
  };

  // --- Helpers shared by renders, status updates, and event wiring ---

  function setStatusBoth(message, type = 'info') {
    setStatus($('jobStatus'), message, type);
    setStatus($('mainJobStatus'), message, type);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function getActiveJobId() {
    // Live run takes precedence; the history view only takes over when
    // the user has explicitly opened a *finished* job.
    return state.liveJobId || state.jobId;
  }

  function emptyState(text, opts = {}) {
    const padding = opts.padding ? ` style="padding:${opts.padding};"` : '';
    const cellTag = opts.colspan
      ? `<tr><td colspan="${opts.colspan}" class="empty-state"${padding}>${text}</td></tr>`
      : `<div class="empty-state"${padding}>${text}</div>`;
    return cellTag;
  }

  const formatters = {
    ms(value) {
      return value != null ? Number(value).toFixed(2) : '—';
    },
    msAsSeconds(value) {
      return value != null ? `${(Number(value) / 1000).toFixed(2)}s` : '-';
    },
    tps(value) {
      return value != null ? Number(value).toFixed(2) : 'n/a';
    },
    tpsFixed(value) {
      return value != null ? Number(value).toFixed(2) : '—';
    },
    number(value, decimals = 2) {
      return value != null ? Number(value).toFixed(decimals) : '-';
    },
    percent(value) {
      return value != null ? `${Number(value).toFixed(1)}%` : '-';
    },
  };

  const OUTCOME_META = {
    pass: { className: 'pass', icon: '✓', label: 'Pass' },
    fail: { className: 'fail', icon: '✗', label: 'Fail' },
    error: { className: 'error', icon: '!', label: 'Error' },
  };
  function outcomeMeta(outcome) {
    return OUTCOME_META[outcome] || OUTCOME_META.error;
  }

  // Single source of truth for the Start/Stop buttons, driven by job status.
  // status === null or a terminal status -> idle (Start on, Stop off).
  // queued/running/stopping -> live (Start off, Stop on).
  function applyButtonState(status) {
    const startBtn = $('startBtn');
    const stopBtn = $('stopBtn');
    const discoverBtn = $('discoverBtn');
    const live = status === 'queued' || status === 'running' || status === 'stopping';
    state.isBenchmarkRunning = live;
    if (startBtn) startBtn.disabled = live;
    if (stopBtn) stopBtn.disabled = !live;
    if (discoverBtn) discoverBtn.disabled = live || !getBaseUrlValue();
    syncModelSelectionLock();
  }

  function syncModelSelectionLock() {
    const disabled = state.isBenchmarkRunning;
    ['modelSearch', 'selectAllBtn', 'selectNoneBtn'].forEach(id => {
      const node = $(id);
      if (node) node.disabled = disabled;
    });
    const modelListEl = $('modelList');
    if (modelListEl) {
      modelListEl.querySelectorAll('input[type="checkbox"]').forEach(checkbox => {
        checkbox.disabled = disabled;
      });
    }
  }

  const MANUAL_PROVIDER_PRESETS = {
    'generic-openai': {
      label: '',
      base_url: '',
      provider: 'openai-compatible',
      hint: 'Remote OpenAI-compatible endpoint. Use this for arbitrary cloud vendors. Localhost should be auto-discovered.',
    },
    openrouter: {
      label: 'OpenRouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider: 'openai-compatible',
      hint: 'OpenRouter cloud endpoint. API key required.',
    },
    groq: {
      label: 'Groq',
      base_url: 'https://api.groq.com/openai/v1',
      provider: 'openai-compatible',
      hint: 'Groq OpenAI-compatible cloud endpoint. API key required.',
    },
    together: {
      label: 'Together',
      base_url: 'https://api.together.ai/v1',
      provider: 'openai-compatible',
      hint: 'Together AI OpenAI-compatible cloud endpoint. API key required.',
    },
  };

  const $ = (id) => document.getElementById(id);

  const STORAGE_KEYS = {
    manualProviders: 'llmSpeedTest.manualProviders.v1',
  };

  function normalizeBaseUrlForKey(rawValue) {
    return String(rawValue || '').trim().replace(/\/+$/, '').toLowerCase();
  }

  function providerKey(baseUrl, provider = 'auto') {
    return `${String(provider || 'auto').toLowerCase()}|${normalizeBaseUrlForKey(baseUrl)}`;
  }

  function hashString(value) {
    let hash = 5381;
    const text = String(value || '');
    for (let i = 0; i < text.length; i += 1) {
      hash = ((hash << 5) + hash) ^ text.charCodeAt(i);
    }
    return (hash >>> 0).toString(36);
  }

  function stableProviderId(source, baseUrl, provider = 'auto') {
    return `${source}-${hashString(providerKey(baseUrl, provider))}`;
  }

  function dedupeProviders(providers) {
    const seen = new Set();
    const out = [];
    (providers || []).forEach(provider => {
      const key = providerKey(provider.base_url, provider.provider);
      if (!provider.base_url || seen.has(key)) return;
      seen.add(key);
      out.push(provider);
    });
    return out;
  }

  function saveManualProviders() {
    try {
      const payload = dedupeProviders(state.manualProviders).map(provider => ({
        id: stableProviderId('manual', provider.base_url, provider.provider),
        label: provider.label || provider.base_url,
        base_url: provider.base_url,
        provider: provider.provider || 'openai-compatible',
        api_key: provider.api_key || '',
        source: 'manual',
        preset: provider.preset || 'generic-openai',
      }));
      localStorage.setItem(STORAGE_KEYS.manualProviders, JSON.stringify(payload));
    } catch (error) {
      console.warn('Failed to save manual providers:', error);
    }
  }

  function loadStoredManualProviders() {
    try {
      const raw = localStorage.getItem(STORAGE_KEYS.manualProviders);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return;
      state.manualProviders = dedupeProviders(parsed.map(provider => ({
        id: stableProviderId('manual', provider.base_url, provider.provider),
        label: provider.label || provider.base_url,
        base_url: String(provider.base_url || '').replace(/\/+$/, ''),
        provider: provider.provider || 'openai-compatible',
        api_key: String(provider.api_key || ''),
        source: 'manual',
        preset: provider.preset || 'generic-openai',
      })));
      state.manualProviders.forEach(provider => state.includedProviderIds.add(provider.id));
    } catch (error) {
      console.warn('Failed to load manual providers:', error);
    }
  }

  function apiUrl(path) {
    if (window.location.protocol === 'file:' || !window.location.origin || window.location.origin === 'null') {
      return `http://127.0.0.1:8765${path}`;
    }
    return path;
  }

  function setStatus(element, message, type = 'info') {
    if (!element) return;
    element.textContent = message;
    element.className = `status-message ${type}`;
  }

  function formatNumber(value, decimals = 2) {
    return value != null ? Number(value).toFixed(decimals) : '-';
  }

  function formatMillisecondsAsSeconds(value) {
    return value != null ? `${(Number(value) / 1000).toFixed(2)}s` : '-';
  }

  function formatTps(value) {
    return value != null ? Number(value).toFixed(2) : 'n/a';
  }

  function setCurrentOperation(job) {
    const el = $('currentOperation');
    if (!el) return;
    const progress = job && job.progress ? job.progress : {};
    const message = progress.current_message || '';
    if (!message || job.status === 'completed' || job.status === 'failed' || job.status === 'stopped') {
      el.classList.add('hidden');
      el.textContent = 'Current operation: idle';
      return;
    }
    const phase = progress.current_phase ? ` [${progress.current_phase}]` : '';
    el.textContent = `Current operation: ${message}${phase}`;
    el.className = 'status-message info';
  }

  // Which benchmark types are checked, in run order (speed first, then sql).
  function selectedBenchmarkTypes() {
    const types = [];
    if ($('typeSpeed') && $('typeSpeed').checked) types.push('speed');
    if ($('typeSql') && $('typeSql').checked) types.push('sql');
    return types;
  }

  function isSpeedBenchmarkMode() {
    return !!($('typeSpeed') && $('typeSpeed').checked);
  }

  // True when SQL is among the selected tests (controls SQL-specific fields).
  function isSqlBenchmarkMode() {
    return !!($('typeSql') && $('typeSql').checked);
  }

  // Map the two thinking checkboxes to the backend's off|on|both. Returns null
  // if neither is checked (caller treats as a validation error).
  function getThinkingMode() {
    const off = $('thinkOff') && $('thinkOff').checked;
    const on = $('thinkOn') && $('thinkOn').checked;
    if (off && on) return 'both';
    if (on) return 'on';
    if (off) return 'off';
    return null;
  }

  function getReasoningEffort() {
    return $('reasoningEffort') ? $('reasoningEffort').value : 'disabled';
  }

  function parseQuestionIds(rawValue) {
    const trimmed = String(rawValue || '').trim();
    if (!trimmed) return null;
    const ids = trimmed
      .split(',')
      .map(part => part.trim())
      .filter(Boolean)
      .map(part => Number(part));
    if (!ids.length || ids.some(id => !Number.isInteger(id) || id <= 0)) {
      throw new Error('Question IDs must be a comma-separated list of positive integers');
    }
    return ids;
  }

  function benchmarkTypeLabel(type) {
    if (type === 'sql') return 'SQL Accuracy';
    return 'Speed';
  }

  function setResultsTableMode(benchmarkType) {
    const sqlContainer = $('sqlResultsContainer');
    const speedContainer = $('speedResultsContainer');
    if (benchmarkType === 'sql') {
      if (sqlContainer) sqlContainer.style.display = '';
      if (speedContainer) speedContainer.style.display = 'none';
    } else {
      if (sqlContainer) sqlContainer.style.display = 'none';
      if (speedContainer) speedContainer.style.display = '';
    }
  }

  function resetSummaryForMode(benchmarkType) {
    const sqlMode = benchmarkType === 'sql';
    const labels = [
      ['successfulRunsLabel', sqlMode ? 'Questions Passed' : 'Successful Runs'],
      ['bestLatencyLabel', sqlMode ? 'Row Matches' : 'Best TTFT'],
      ['bestPpLabel', sqlMode ? 'Column Matches' : 'Best PP'],
      ['bestDecodeLabel', sqlMode ? 'First Row Matches' : 'Best Decode'],
      ['activeModelLabel', sqlMode ? 'Active SQL Model' : 'Active Model'],
    ];
    labels.forEach(([id, text]) => { const el = $(id); if (el) el.textContent = text; });

    const hint = $('resultsHint');
    if (hint) {
      hint.textContent = sqlMode
        ? 'SQL mode checks semantic correctness against AdventureWorks answers: row count, columns, and first row when available.'
        : 'TTFT = time to first token. Total = full generation time. PP = prompt tokens per second when the backend reports prompt-eval timing.';
    }

    ['bestLatency', 'bestPp', 'bestDecode'].forEach(id => { const el = $(id); if (el) el.textContent = '-'; });
    const sr = $('successfulRuns'); if (sr) sr.textContent = '0';
    const am = $('activeModel'); if (am) am.textContent = '-';
  }

  function updateBenchmarkModeUi() {
    const sqlOn = isSqlBenchmarkMode();
    const speedOn = isSpeedBenchmarkMode();
    const primaryType = sqlOn ? 'sql' : 'speed';
    state.activeBenchmarkType = primaryType;

    // Speed-only settings (mode, runtime knobs) need speed to be selected.
    document.querySelectorAll('.speed-only-setting').forEach(node => {
      node.classList.toggle('hidden', !speedOn);
    });

    // SQL-only fields appear whenever SQL is selected.
    const qig = $('questionIdsGroup');
    if (qig) qig.classList.toggle('hidden', !sqlOn);
    const tmg = $('thinkingModeGroup');
    if (tmg) tmg.classList.toggle('hidden', !sqlOn);
    const reg = $('reasoningEffortGroup');
    if (reg) reg.classList.toggle('hidden', !sqlOn);
    const qtg = $('questionTimeoutGroup');
    if (qtg) qtg.classList.toggle('hidden', !sqlOn);

    const sqlHint = $('sqlModeHint');
    if (sqlHint) sqlHint.classList.toggle('hidden', !sqlOn);

    const panelTitle = $('benchmarkPanelTitle');
    if (panelTitle) panelTitle.textContent = sqlOn ? '🧮 SQL Model Selection' : '📝 Select models to benchmark';

    setResultsTableMode(primaryType);
    resetSummaryForMode(primaryType);
    applyCapabilityGating();
    updateActionButtons();
  }

  function getSelectedModelsForProvider(providerId) {
    return Array.from(state.providerModelSelection[providerId] || []);
  }

  function setSelectedModelsForProvider(providerId, models) {
    state.providerModelSelection[providerId] = new Set(models);
  }

  function includedProviders() {
    return allProviders().filter(provider => state.includedProviderIds.has(provider.id));
  }

  function allProviders() {
    return [...state.discoveredProviders, ...state.manualProviders];
  }

  function getProviderById(providerId) {
    return allProviders().find(provider => provider.id === providerId) || null;
  }

  function applyProviderToConfig(_provider) {
    // Config card removed — provider data lives in state only.
  }

  function selectedManualPreset() {
    const presetEl = $('manualProviderPreset');
    const presetId = presetEl ? presetEl.value : 'generic-openai';
    return MANUAL_PROVIDER_PRESETS[presetId] || MANUAL_PROVIDER_PRESETS['generic-openai'];
  }

  function applyManualPreset() {
    const preset = selectedManualPreset();
    const labelEl = $('manualProviderLabel');
    const baseUrlEl = $('manualProviderBaseUrl');
    const hintEl = $('manualProviderHint');
    if (labelEl) labelEl.value = preset.label;
    if (baseUrlEl) baseUrlEl.value = preset.base_url;
    if (hintEl) hintEl.textContent = preset.hint;
  }

  function normalizeCloudBaseUrl(rawValue) {
    const value = String(rawValue || '').trim();
    if (!value) return '';
    if (/^[a-z]+:\/\//i.test(value)) return value.replace(/\/+$/, '');
    return `https://${value}`.replace(/\/+$/, '');
  }

  function isLocalHostname(hostname) {
    const normalized = String(hostname || '').trim().toLowerCase();
    return normalized === 'localhost' || normalized === '127.0.0.1' || normalized === '::1' || normalized === '[::1]';
  }

  function isManualCloudUrlAllowed(baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      return !isLocalHostname(parsed.hostname);
    } catch {
      return false;
    }
  }

  function renderEndpoints() {
    const endpointListEl = $('endpointList');
    if (!endpointListEl) return;

    const providers = allProviders();
    endpointListEl.innerHTML = '';

    if (!providers.length) {
      endpointListEl.innerHTML = emptyState('No providers available yet');
      return;
    }

    providers.forEach((endpoint) => {
      const card = document.createElement('div');
      card.className = `endpoint-card ${state.activeProviderId === endpoint.id ? 'selected' : ''}`;
      const sourceLabel = endpoint.source === 'manual' ? 'Manual' : 'Discovered';
      const included = state.includedProviderIds.has(endpoint.id) ? 'checked' : '';
      const modelCount = (state.providerModels[endpoint.id] || []).length;
      const selectedCount = getSelectedModelsForProvider(endpoint.id).length;
      const removeButton = endpoint.source === 'manual'
        ? `<button type="button" class="btn-small endpoint-delete-btn" data-provider-remove="${escapeHtml(endpoint.id)}" title="Remove endpoint">Remove</button>`
        : '';

      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; gap:12px; align-items:flex-start;">
          <div style="min-width:0; flex:1;">
            <div style="font-weight: 600; margin-bottom: 4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(endpoint.label || endpoint.base_url)}">${escapeHtml(endpoint.label || endpoint.base_url)}</div>
            <div style="font-size: 0.8rem; color: var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(endpoint.base_url)}">${escapeHtml(endpoint.base_url)}</div>
          </div>
          <div style="display:flex; flex-direction:column; gap:6px; align-items:flex-end; flex-shrink:0;">
            <label style="display:flex; align-items:center; gap:8px; margin:0; text-transform:none; letter-spacing:0; color:var(--text-main);">
              <input type="checkbox" data-provider-include="${escapeHtml(endpoint.id)}" style="width:auto; margin:0;" ${included}>
              Include
            </label>
            ${removeButton}
          </div>
        </div>
        <div class="provider-meta">
          <span>${escapeHtml(endpoint.provider)}</span>
          <span>${escapeHtml(sourceLabel)}</span>
          <span>${selectedCount}/${modelCount} models</span>
        </div>
      `;

      card.addEventListener('click', (event) => {
        if (event.target.closest('input[data-provider-include], [data-provider-remove]')) return;
        selectEndpoint(endpoint.id);
      });
      endpointListEl.appendChild(card);
    });
  }

  function selectEndpoint(providerId) {
    state.activeProviderId = providerId;
    const endpoint = getProviderById(providerId);
    if (!endpoint) return;

    renderEndpoints();
    applyProviderToConfig(endpoint);
    loadModelsForActiveProvider();
    applyCapabilityGating();
    setStatus($('discoverStatus'), `Selected ${endpoint.base_url}. Ready to discover models.`, 'success');
    updateActionButtons();
  }

  function removeEndpoint(providerId) {
    const provider = getProviderById(providerId);
    if (!provider || provider.source !== 'manual') return;

    state.manualProviders = state.manualProviders.filter(item => item.id !== providerId);
    state.includedProviderIds.delete(providerId);
    delete state.providerModels[providerId];
    delete state.providerModelSelection[providerId];
    saveManualProviders();

    if (state.activeProviderId === providerId) {
      const nextProvider = allProviders()[0] || null;
      state.activeProviderId = nextProvider ? nextProvider.id : null;
      if (nextProvider) applyProviderToConfig(nextProvider);
      else {
        state.models = [];
        state.filteredModels = [];
      }
      loadModelsForActiveProvider();
    }

    renderEndpoints();
    updateActionButtons();
    setStatus($('endpointStatus'), `Removed endpoint ${provider.label || provider.base_url}.`, 'info');
  }

  function addManualProvider() {
    const preset = selectedManualPreset();
    const labelEl = $('manualProviderLabel');
    const baseUrlEl = $('manualProviderBaseUrl');
    const apiKeyEl = $('manualProviderApiKey');

    const label = labelEl ? labelEl.value.trim() : '';
    const baseUrl = normalizeCloudBaseUrl(baseUrlEl ? baseUrlEl.value : '');
    const apiKey = apiKeyEl ? apiKeyEl.value : '';
    const providerType = preset.provider || 'openai-compatible';

    if (!baseUrl) {
      setStatus($('endpointStatus'), 'Manual provider base URL is required.', 'error');
      return;
    }

    if (!isManualCloudUrlAllowed(baseUrl)) {
      setStatus($('endpointStatus'), 'Manual provider presets are for remote cloud endpoints only. Use Scan Local for localhost.', 'error');
      return;
    }

    const existingIndex = state.manualProviders.findIndex(provider => providerKey(provider.base_url, provider.provider) === providerKey(baseUrl, providerType));
    const existingProvider = existingIndex >= 0 ? state.manualProviders[existingIndex] : null;
    const manualProvider = {
      id: stableProviderId('manual', baseUrl, providerType),
      label: label || preset.label || baseUrl,
      base_url: baseUrl,
      provider: providerType,
      api_key: apiKey || existingProvider?.api_key || '',
      source: 'manual',
      preset: $('manualProviderPreset') ? $('manualProviderPreset').value : 'generic-openai',
    };

    if (existingIndex >= 0) {
      state.manualProviders[existingIndex] = { ...state.manualProviders[existingIndex], ...manualProvider };
      setStatus($('endpointStatus'), `Updated manual provider ${manualProvider.label}.`, 'success');
    } else {
      state.manualProviders.push(manualProvider);
      setStatus($('endpointStatus'), `Added manual provider ${manualProvider.label}.`, 'success');
    }

    state.manualProviders = dedupeProviders(state.manualProviders);
    state.includedProviderIds.add(manualProvider.id);
    saveManualProviders();
    renderEndpoints();
    selectEndpoint(manualProvider.id);

    // Reset form
    if (labelEl) labelEl.value = '';
    if (baseUrlEl) baseUrlEl.value = '';
    if (apiKeyEl) apiKeyEl.value = '';
    const presetEl = $('manualProviderPreset');
    if (presetEl) presetEl.value = 'generic-openai';
    applyManualPreset();

    // Hide the form
    const form = $('manualProviderForm');
    if (form) form.classList.add('hidden');
  }

  function getBaseUrlValue() {
    const provider = getProviderById(state.activeProviderId);
    return provider ? (provider.base_url || '').trim() : '';
  }

  function renderModels(models, providerId = state.activeProviderId) {
    if (providerId) {
      const normalizedModels = [...models];
      state.providerModels[providerId] = normalizedModels;
      const hasStoredSelection = Object.prototype.hasOwnProperty.call(state.providerModelSelection, providerId);
      const currentSelection = state.providerModelSelection[providerId] || new Set();
      const nextSelection = hasStoredSelection
        ? normalizedModels.filter(model => currentSelection.has(model))
        : normalizedModels;
      setSelectedModelsForProvider(providerId, nextSelection);
    }
    state.models = [...models];
    state.filteredModels = [...models];
    updateModelCount();
    filterModels();
  }

  function loadModelsForActiveProvider() {
    const providerId = state.activeProviderId;
    const models = providerId ? (state.providerModels[providerId] || []) : [];
    state.models = [...models];
    state.filteredModels = [...models];
    filterModels();
  }

  function filterModels() {
    const searchEl = $('modelSearch');
    const modelListEl = $('modelList');
    if (!modelListEl) return;

    const search = searchEl ? searchEl.value.toLowerCase() : '';
    state.filteredModels = state.models.filter(model => model.toLowerCase().includes(search));

    modelListEl.innerHTML = '';

    if (!state.filteredModels.length) {
      modelListEl.innerHTML = emptyState('No models match your search');
      updateModelCount();
      return;
    }

    const selectedSet = new Set(getSelectedModelsForProvider(state.activeProviderId));
    state.filteredModels.forEach(model => {
      const item = document.createElement('label');
      item.className = 'model-item';
      const checked = selectedSet.has(model) ? 'checked' : '';
      const disabled = state.isBenchmarkRunning ? 'disabled' : '';
      item.innerHTML = `
        <input type="checkbox" value="${escapeHtml(model)}" ${checked} ${disabled}>
        <span>${escapeHtml(model)}</span>
      `;
      modelListEl.appendChild(item);
    });
    updateModelCount();
  }

  function updateModelCount() {
    const el = $('modelCount');
    const selected = getSelectedModelsForProvider(state.activeProviderId).length;
    if (el) el.textContent = `${selected} / ${state.models.length} selected`;
    renderEndpoints();
  }

  function setModelCheckedForActiveProvider(model, checked) {
    const providerId = state.activeProviderId;
    if (!providerId || !model) return;
    const selected = new Set(getSelectedModelsForProvider(providerId));
    if (checked) selected.add(model);
    else selected.delete(model);
    setSelectedModelsForProvider(providerId, Array.from(selected));
  }

  function aggregateSelectedModels() {
    return Array.from(new Set(includedProviders().flatMap(provider => getSelectedModelsForProvider(provider.id))));
  }

  function selectedSqlProvider() {
    return getProviderById(state.activeProviderId) || includedProviders()[0] || null;
  }

  function exactlyOneIncludedProvider() {
    return includedProviders().length === 1;
  }

  function updateActionButtons() {
    const hasBaseUrl = Boolean(getBaseUrlValue());
    const hasIncludedProviders = includedProviders().length > 0;
    const hasSelectedModels = aggregateSelectedModels().length > 0;
    const types = selectedBenchmarkTypes();
    const sqlProvider = selectedSqlProvider();
    const sqlModels = sqlProvider ? getSelectedModelsForProvider(sqlProvider.id) : [];
    const sqlReady = hasBaseUrl && exactlyOneIncludedProvider() && sqlProvider && sqlModels.length > 0;
    const speedReady = hasBaseUrl && hasIncludedProviders && hasSelectedModels;

    // Every selected test must be runnable; at least one must be selected.
    let ready = types.length > 0;
    if (types.includes('speed')) ready = ready && speedReady;
    if (types.includes('sql')) ready = ready && sqlReady;

    const discoverBtn = $('discoverBtn');
    const startBtn = $('startBtn');
    if (discoverBtn) discoverBtn.disabled = state.isBenchmarkRunning || !hasBaseUrl;
    if (startBtn) startBtn.disabled = state.isBenchmarkRunning || !ready;
    syncModelSelectionLock();
  }

  function escapeHtml(text) {
    return String(text)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  async function scanEndpoints() {
    setStatus($('endpointStatus'), 'Scanning local endpoints...', 'info');
    try {
      const response = await fetch(apiUrl('/api/endpoints/scan'));
      const data = await response.json();
      if (data.status !== 'ok') throw new Error(data.error?.message || 'Scan failed');

      state.discoveredProviders = dedupeProviders((data.endpoints || []).map((endpoint) => {
        const providerType = endpoint.provider || 'auto';
        return {
          ...endpoint,
          id: stableProviderId('scan', endpoint.base_url, providerType),
          provider: providerType,
          source: 'scan',
          api_key: '',
        };
      }));

      state.discoveredProviders.forEach(provider => state.includedProviderIds.add(provider.id));
      renderEndpoints();
      if (state.discoveredProviders.length) {
        const activeProvider = getProviderById(state.activeProviderId);
        if (!activeProvider) {
          selectEndpoint(state.discoveredProviders[0].id);
        } else {
          renderEndpoints();
        }
        setStatus($('endpointStatus'), `Found ${state.discoveredProviders.length} discovered provider(s).`, 'success');
      } else {
        renderEndpoints();
        setStatus($('endpointStatus'), 'No local endpoints detected', 'error');
      }
      updateActionButtons();
    } catch (error) {
      setStatus($('endpointStatus'), `Scan failed: ${error.message}`, 'error');
      updateActionButtons();
    }
  }

  async function discoverModels() {
    const providerTargets = includedProviders();
    if (!providerTargets.length) {
      setStatus($('discoverStatus'), 'Select at least one provider with Include before discovery.', 'error');
      return;
    }

    setStatus($('discoverStatus'), `Discovering models for ${providerTargets.length} provider(s)...`, 'info');
    const modelListEl = $('modelList');
    if (modelListEl) modelListEl.innerHTML = emptyState('Loading models...');
    state.models = [];
    state.filteredModels = [];
    updateModelCount();
    updateActionButtons();
    try {
      for (const providerTarget of providerTargets) {
        const payload = {
          base_url: providerTarget.base_url,
          provider: providerTarget.provider,
          api_key: providerTarget.api_key || '',
        };
        const response = await fetch(apiUrl('/api/models/discover'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (data.status !== 'ok') throw new Error(`${providerTarget.label || providerTarget.base_url}: ${data.error?.message || 'Discovery failed'}`);
        renderModels(data.models || [], providerTarget.id);
      }
      loadModelsForActiveProvider();
      setStatus($('discoverStatus'), `Loaded models for ${providerTargets.length} provider(s).`, 'success');
      updateActionButtons();
    } catch (error) {
      state.models = [];
      state.filteredModels = [];
      if (modelListEl) modelListEl.innerHTML = emptyState('No models loaded yet');
      updateModelCount();
      setStatus($('discoverStatus'), `Discovery failed: ${error.message}`, 'error');
      updateActionButtons();
    }
  }

  function runtimeCapabilityHints(providerType) {
    if (providerType === 'ollama') {
      return {
        maxConcurrentPredictions: false,
        mtp: false,
        kCacheQuantization: false,
        vCacheQuantization: false,
        batchSize: false,
        flashAttn: false,
      };
    }
    return {
      maxConcurrentPredictions: true,
      mtp: true,
      kCacheQuantization: true,
      vCacheQuantization: true,
      batchSize: true,
      flashAttn: true,
    };
  }

  function applyCapabilityGating() {
    const provider = getProviderById(state.activeProviderId);
    const providerType = provider?.provider || 'auto';
    const hints = runtimeCapabilityHints(providerType);
    const sqlMode = isSqlBenchmarkMode();
    const fields = [
      ['maxConcurrentPredictions', hints.maxConcurrentPredictions],
      ['mtp', hints.mtp],
      ['kCacheQuantization', hints.kCacheQuantization],
      ['vCacheQuantization', hints.vCacheQuantization],
      ['batchSize', hints.batchSize],
      ['flashAttn', hints.flashAttn],
    ];
    fields.forEach(([id, enabled]) => {
      const node = $(id);
      if (!node) return; // element doesn't exist in DOM — skip gracefully
      node.disabled = sqlMode || !enabled;
      if (node.disabled) {
        if (node.tagName === 'SELECT') node.value = '';
        else node.value = '';
      }
    });
  }

  function applySpeedPreset(name) {
    const presets = {
      smoke:       { maxTokens: 256,  repeatCount: 1, warmupRuns: 0 },
      balanced:    { maxTokens: 1024, repeatCount: 3, warmupRuns: 1 },
      leaderboard: { maxTokens: 4096, repeatCount: 5, warmupRuns: 1 },
    };
    const p = presets[name];
    if (!p) return;
    const set = (id, val) => { const el = $(id); if (el) el.value = val; };
    set('maxTokens',   p.maxTokens);
    set('repeatCount', p.repeatCount);
    set('warmupRuns',  p.warmupRuns);
  }

  function buildSpeedPayload() {
    const baseUrl = getBaseUrlValue();
    if (!baseUrl) throw new Error('Base URL is required. Scan endpoints or enter one manually.');
    const activeProvider = getProviderById(state.activeProviderId);
    const models = aggregateSelectedModels();
    if (!models.length) throw new Error('Select at least one model across included providers');
    const targets = includedProviders()
      .map(provider => ({
        provider_id: provider.id,
        provider_label: provider.label || provider.base_url,
        base_url: provider.base_url,
        provider_type: provider.provider || 'auto',
        api_key: provider.api_key || '',
        models: getSelectedModelsForProvider(provider.id),
      }))
      .filter(target => target.base_url && target.models.length);
    const numVal = (id, def) => {
      const el = $(id);
      return el && el.value !== '' ? Number(el.value) : def;
    };
    return {
      benchmark_type: 'speed',
      base_url: baseUrl,
      provider: activeProvider?.provider || 'auto',
      api_key: activeProvider?.api_key || '',
      models,
      targets,
      mode: $('mode') ? $('mode').value : 'sequential',
      timeout_ms: numVal('timeoutMs', 12000) * 1000,
      prompt: $('prompt') ? $('prompt').value : '',
      max_tokens: numVal('maxTokens', 4096),
      repeat_count: numVal('repeatCount', 3),
      warmup_runs: numVal('warmupRuns', 1),
    };
  }

  function buildSqlPayload() {
    const sqlProvider = selectedSqlProvider();
    if (!sqlProvider) throw new Error('Select a provider for SQL benchmark');
    if (!exactlyOneIncludedProvider()) throw new Error('SQL benchmark requires exactly one included provider');
    const models = getSelectedModelsForProvider(sqlProvider.id);
    if (!models.length) throw new Error('Select at least one model for SQL benchmark on the active provider');
    const thinkingMode = getThinkingMode();
    if (!thinkingMode) throw new Error('Select at least one Thinking Mode (Off and/or On)');
    return {
      benchmark_type: 'sql',
      base_url: sqlProvider.base_url,
      provider: sqlProvider.provider || 'auto',
      api_key: sqlProvider.api_key || '',
      models: models,
      thinking_mode: thinkingMode,
      reasoning_effort: getReasoningEffort(),
      question_ids: parseQuestionIds($('questionIds') ? $('questionIds').value : ''),
      timeout_ms: (Number($('timeoutMs') ? $('timeoutMs').value : '12000') || 12000) * 1000,
      question_timeout_ms: (Number($('questionTimeoutMs') && $('questionTimeoutMs').value !== '' ? $('questionTimeoutMs').value : 0) || 0) * 1000,
    };
  }

  // One payload per selected test, in run order (speed, sql).
  function buildBenchmarkPayloads() {
    const types = selectedBenchmarkTypes();
    if (!types.length) throw new Error('Select at least one test (Speed or SQL Accuracy)');
    return types.map(t => t === 'sql' ? buildSqlPayload() : buildSpeedPayload());
  }

  async function startBenchmark() {
    let payloads;
    try {
      // Build all payloads up front so a validation error stops everything
      // before any job is launched.
      payloads = buildBenchmarkPayloads();
    } catch (error) {
      setStatusBoth(`Start failed: ${error.message}`, 'error');
      return;
    }
    const startBtn = $('startBtn');
    if (startBtn) startBtn.disabled = true;
    state.jobQueue = payloads.slice();
    startNextQueuedJob();
  }

  // Launch the next payload in the queue. Called by startBenchmark and by
  // pollJob when a job finishes. Stops (and re-enables Start) when the queue
  // empties or a launch fails.
  async function startNextQueuedJob() {
    const payload = state.jobQueue.shift();
    if (!payload) {
      applyButtonState(null);
      return;
    }
    const benchmarkType = payload.benchmark_type || 'speed';
    const remaining = state.jobQueue.length;
    const queueNote = remaining ? ` (${remaining} more queued)` : '';
    try {
      state.activeBenchmarkType = benchmarkType;
      setResultsTableMode(benchmarkType);
      resetSummaryForMode(benchmarkType);
      setStatusBoth(`Starting ${benchmarkTypeLabel(benchmarkType)} benchmark...${queueNote}`, 'info');
      const op = $('currentOperation');
      if (op) {
        op.className = 'status-message info';
        op.textContent = `Current operation: submitting ${benchmarkTypeLabel(benchmarkType)} request...`;
      }

      const response = await fetch(apiUrl('/api/benchmark/start'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (data.status !== 'ok') throw new Error(data.error?.message || 'Failed to start');

      state.jobId = data.job_id;
      state.liveJobId = data.job_id;
      // Clear any open history view so pollJob renders the live run instead of
      // the stale finished job the user previously opened.
      state.activeHistoryJobId = null;
      try { sessionStorage.setItem('llmSpeedTest.jobId', state.jobId); } catch (_) {}
      applyButtonState('running');

      const resultsBodyEl = $('resultsBody');
      if (resultsBodyEl) {
        const colspan = benchmarkType === 'sql' ? 10 : benchmarkType === 'speed' ? 11 : 12;
        resultsBodyEl.innerHTML = emptyState('Benchmark in progress...', { colspan });
      }
      pollJob();
    } catch (error) {
      // Abandon the rest of the queue on a launch failure.
      state.jobQueue = [];
      applyButtonState(null);
      setStatusBoth(`Start failed: ${error.message}`, 'error');
    }
  }

  async function stopBenchmark() {
    // Cancelling means cancelling the whole run, so drop anything queued first.
    state.jobQueue = [];
    const targetId = getActiveJobId();
    if (!targetId) { applyButtonState(null); return; }
    try {
      await fetch(apiUrl(`/api/benchmark/${targetId}/stop`), { method: 'POST' });
      setStatusBoth('Stop requested...', 'info');
    } catch (error) {
      setStatusBoth(`Start failed: ${error.message}`, 'error');
    }
  }

  function renderHistory() {
    const historyBodyEl = $('historyBody');
    const historyStatusEl = $('historyStatus');
    if (!historyBodyEl) return;

    historyBodyEl.innerHTML = '';
    if (!state.history.length) {
      historyBodyEl.innerHTML = emptyState('No saved benchmark history yet', { colspan: 9 });
      if (historyStatusEl) historyStatusEl.textContent = 'No saved benchmark history';
      updateHistorySelectionUi();
      return;
    }
    state.history.forEach(item => {
      const row = document.createElement('tr');
      const checked = state.historySelection.has(item.job_id) ? 'checked' : '';
      const resultCount = Array.isArray(item.results) ? item.results.length : 0;
      const distinctModelCount = item.request?.benchmark_type === 'sql'
        ? new Set((item.results || []).map(r => r.model).filter(Boolean)).size
        : null;
      const errorCount = Array.isArray(item.errors) ? item.errors.length : 0;
      const providerLabel = item.results?.[0]?.provider_label || item.request?.base_url || '-';
      const endpoint = item.results?.[0]?.endpoint || item.request?.base_url || '-';
      const benchmarkType = item.request?.benchmark_type || 'speed';
      const jsonlUrl = apiUrl(`/api/benchmark/${encodeURIComponent(item.job_id)}/results.jsonl`);
      const csvUrl = apiUrl(`/api/benchmark/${encodeURIComponent(item.job_id)}/results.csv`);
      const tsvUrl = apiUrl(`/api/benchmark/${encodeURIComponent(item.job_id)}/results.tsv`);
      const manifestUrl = apiUrl(`/api/benchmark/${encodeURIComponent(item.job_id)}/manifest.json`);
      const summaryUrl = apiUrl(`/api/benchmark/${encodeURIComponent(item.job_id)}/summary.json`);
      const actions = `
        <button type="button" class="btn-small" data-history-open="${escapeHtml(item.job_id)}">Open</button>
        <a class="btn-small" href="${escapeHtml(jsonlUrl)}" download="${escapeHtml(item.job_id)}.results.jsonl">JSONL</a>
        <a class="btn-small" href="${escapeHtml(csvUrl)}" download="${escapeHtml(item.job_id)}.results.csv">CSV</a>
        <a class="btn-small" href="${escapeHtml(tsvUrl)}" download="${escapeHtml(item.job_id)}.results.tsv">TSV</a>
        <a class="btn-small" href="${escapeHtml(manifestUrl)}" download="${escapeHtml(item.job_id)}.manifest.json">Manifest</a>
        <a class="btn-small" href="${escapeHtml(summaryUrl)}" download="${escapeHtml(item.job_id)}.summary.json">Summary</a>
      `;
      row.innerHTML = `
        <td><input type="checkbox" data-job-id="${escapeHtml(item.job_id)}" ${checked}></td>
        <td>${escapeHtml(providerLabel)}</td>
        <td>${escapeHtml(endpoint)}</td>
        <td>${escapeHtml(item.started_at || item.created_at || '-')}</td>
        <td>${escapeHtml(benchmarkTypeLabel(benchmarkType))}</td>
        <td>${escapeHtml(item.status || '-')}</td>
        <td>${distinctModelCount !== null ? `${distinctModelCount} model${distinctModelCount === 1 ? '' : 's'}` : resultCount}</td>
        <td>${errorCount}</td>
        <td>${actions}</td>
      `;
      historyBodyEl.appendChild(row);
    });
    if (historyStatusEl) historyStatusEl.textContent = `Loaded ${state.history.length} saved benchmark run(s)`;
    updateHistorySelectionUi();
  }

  function updateHistorySelectionUi() {
    const allCheckbox = $('selectAllHistory');
    const clearSelectedBtn = $('clearSelectedHistoryBtn');
    const total = state.history.length;
    const selected = state.historySelection.size;
    if (allCheckbox) {
      allCheckbox.checked = total > 0 && selected === total;
      allCheckbox.indeterminate = selected > 0 && selected < total;
    }
    if (clearSelectedBtn) clearSelectedBtn.disabled = selected === 0;
  }

  let _openHistoryJobInFlight = false;
  let _openJobGeneration = 0;

  function closeHistoryView() {
    state.activeHistoryJobId = null;
    // Reset results table to speed mode, clear both containers
    state.activeBenchmarkType = 'speed';
    setResultsTableMode('speed');
    const resultsBodyEl = $('resultsBody');
    if (resultsBodyEl) {
      resultsBodyEl.innerHTML = '<tr><td colspan="12" class="empty-state">No benchmark results yet</td></tr>';
    }
    const speedC = $('speedResultsContainer');
    if (speedC) speedC.innerHTML = '';
    const sqlContainer = $('sqlResultsContainer');
    if (sqlContainer) {
      sqlContainer.innerHTML = '';
      sqlContainer.style.display = 'none';
    }
    // Reset summary
    resetSummaryForMode('speed');
    const progressFill = $('progressFill');
    const progressText = $('progressText');
    if (progressFill) progressFill.style.width = '0%';
    if (progressText) progressText.textContent = '0 / 0';
    // Hide close button
    const closeBtn = $('closeHistoryViewBtn');
    if (closeBtn) closeBtn.classList.add('hidden');
    // If a live run is still going, keep Stop active and point jobId back at it;
    // otherwise return to the idle Ready state.
    if (state.liveJobId) {
      state.jobId = state.liveJobId;
      applyButtonState('running');
    } else {
      state.jobId = null;
      applyButtonState(null);
    }
    setStatusBoth('Ready', 'info');
    // Reset speed view to aggregated so the next open starts clean
    state.speedViewMode = 'aggregated';
    try { sessionStorage.setItem('llmSpeedTest.speedViewMode', 'aggregated'); } catch (_) {}
    const speedToggle = $('speedViewToggle');
    if (speedToggle) speedToggle.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.getAttribute('data-view') === 'aggregated')
    );
  }

  async function openHistoryJob(jobId) {
    if (state.activeHistoryJobId === jobId) return;  // already open
    if (_openHistoryJobInFlight) return;              // debounce rapid clicks
    _openHistoryJobInFlight = true;
    const gen = ++_openJobGeneration;
    try {
    const job = state.history.find(item => item.job_id === jobId);
    if (!job) return;
    state.activeHistoryJobId = jobId;
    state.jobId = jobId;
    state.activeBenchmarkType = job.request?.benchmark_type || 'speed';
    // Reset speed view to aggregated so each job starts with a clean toggle
    state.speedViewMode = 'aggregated';
    try { sessionStorage.setItem('llmSpeedTest.speedViewMode', 'aggregated'); } catch (_) {}
    const speedToggle = $('speedViewToggle');
    if (speedToggle) speedToggle.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.getAttribute('data-view') === 'aggregated')
    );
    // Show close button
    const closeBtn = $('closeHistoryViewBtn');
    if (closeBtn) closeBtn.classList.remove('hidden');
    setResultsTableMode(state.activeBenchmarkType);
    renderResults(job, job.request?.benchmark_type || 'speed');
    updateSummary(job, job.request?.benchmark_type || 'speed');
    const completed = job.progress?.completed || 0;
    const total = job.progress?.total || 0;
    const percent = total > 0 ? (completed / total) * 100 : 0;
    const progressFill = $('progressFill');
    const progressText = $('progressText');
    if (progressFill) progressFill.style.width = `${percent}%`;
    if (progressText) progressText.textContent = `${completed} / ${total}`;

    // If the on-disk record looks live, confirm with the server and ATTACH:
    // enable Stop and start polling so the user can control the running job
    // they just opened. Previously this view was always static, which is why
    // Stop stayed disabled for an active run opened from history.
    const maybeLive = job.status === 'running' || job.status === 'queued' || job.status === 'stopping';
    if (maybeLive) {
      try {
        const resp = await fetch(apiUrl(`/api/benchmark/${jobId}`));
        if (gen !== _openJobGeneration) return;  // stale, another open superseded us
        const data = await resp.json();
        const liveStatus = data?.job?.status;
        if (data.status === 'ok' && (liveStatus === 'running' || liveStatus === 'queued' || liveStatus === 'stopping')) {
          state.liveJobId = jobId;
          applyButtonState(liveStatus);
          try { sessionStorage.setItem('llmSpeedTest.jobId', jobId); } catch (_) {}
          if (!state.pollTimer) pollJob();
          setStatusBoth(`Attached to running benchmark ${jobId.slice(0,8)}… (${completed}/${total}).`, 'info');
          return;
        }
      } catch (_) { /* fall through to static view */ }
    }
    setStatusBoth(`Loaded saved benchmark ${jobId} (${job.status || 'unknown'}).`, 'info');
    const historyStatusEl = $('historyStatus');
    if (historyStatusEl) historyStatusEl.textContent = `Opened: ${jobId.slice(0,8)}… (${job.status || 'unknown'})`;
    } finally {
      _openHistoryJobInFlight = false;
    }
  }

  // Attach the UI to a confirmed-live job: render its progress, enable Stop,
  // start polling. Shared by the sessionStorage path and the /active fallback.
  function attachToLiveJob(jobId, job) {
    state.jobId = jobId;
    state.liveJobId = jobId;
    state.activeBenchmarkType = job.request?.benchmark_type || 'speed';
    setResultsTableMode(state.activeBenchmarkType);
    renderResults(job, state.activeBenchmarkType);
    updateSummary(job, state.activeBenchmarkType);
    const completed = job.progress?.completed || 0;
    const total = job.progress?.total || 0;
    const pct = total > 0 ? (completed / total) * 100 : 0;
    const pf = $('progressFill');
    const pt = $('progressText');
    if (pf) pf.style.width = `${pct}%`;
    if (pt) pt.textContent = `${completed} / ${total}`;
    applyButtonState(job.status);
    try { sessionStorage.setItem('llmSpeedTest.jobId', jobId); } catch (_) {}
    setStatusBoth(`Restored job ${jobId.slice(0,8)}… (${completed}/${total})`, 'info');
    if (!state.pollTimer) pollJob();
  }

  async function restoreActiveJob() {
    const isLive = (s) => s === 'running' || s === 'queued' || s === 'stopping';
    let savedJobId = null;
    try { savedJobId = sessionStorage.getItem('llmSpeedTest.jobId'); } catch (_) {}

    // 1) Fast path: per-tab hint from sessionStorage.
    if (savedJobId) {
      try {
        const resp = await fetch(apiUrl(`/api/benchmark/${savedJobId}`));
        if (resp.ok) {
          const data = await resp.json();
          const job = data?.job;
          if (data.status === 'ok' && job && isLive(job.status)) {
            attachToLiveJob(savedJobId, job);
            return;
          }
        }
        // Stale or finished — drop the hint and fall through to the server list.
        try { sessionStorage.removeItem('llmSpeedTest.jobId'); } catch (_) {}
      } catch (_) { /* server unreachable */ return; }
    }

    // 2) Fallback: ask the server for any live job (survives new tab / lost
    //    sessionStorage). This is what makes Stop work after a plain reload.
    try {
      const resp = await fetch(apiUrl('/api/benchmark/active'));
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.status !== 'ok' || !Array.isArray(data.active) || !data.active.length) return;
      const activeId = data.active[0].job_id;
      const detail = await fetch(apiUrl(`/api/benchmark/${activeId}`));
      if (!detail.ok) return;
      const dj = await detail.json();
      if (dj.status === 'ok' && dj.job && isLive(dj.job.status)) {
        attachToLiveJob(activeId, dj.job);
      }
    } catch (_) { /* server unreachable */ }
  }

  async function loadHistory() {
    const historyStatusEl = $('historyStatus');
    const historyBodyEl = $('historyBody');
    if (historyStatusEl) historyStatusEl.textContent = 'Loading saved history...';
    try {
      const response = await fetch(apiUrl('/api/benchmark/results'));
      const data = await response.json();
      if (data.status !== 'ok') throw new Error(data.error?.message || 'Failed to load history');
      state.history = Array.isArray(data.results) ? data.results : [];
      state.historySelection = new Set();
      renderHistory();
    } catch (error) {
      if (historyBodyEl) historyBodyEl.innerHTML = emptyState('Failed to load saved benchmark history', { colspan: 9 });
      if (historyStatusEl) historyStatusEl.textContent = `History load failed: ${error.message}`;
    }
  }

  async function clearHistory({ all = false } = {}) {
    const job_ids = all ? [] : Array.from(state.historySelection);
    if (!all && !job_ids.length) return;
    const historyStatusEl = $('historyStatus');
    if (historyStatusEl) historyStatusEl.textContent = all ? 'Clearing all saved history...' : 'Clearing selected history...';
    try {
      const response = await fetch(apiUrl('/api/benchmark/results/clear'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(all ? { all: true } : { job_ids }),
      });
      const data = await response.json();
      if (data.status !== 'ok') throw new Error(data.error?.message || 'Failed to clear history');
      await loadHistory();
    } catch (error) {
      if (historyStatusEl) historyStatusEl.textContent = `History clear failed: ${error.message}`;
    }
  }

  // Per-row diff cache for the speed results table. Keyed by
  // (provider, model, run_index) so polls that bring back the same
  // results produce zero DOM mutations. The cache is cleared by the
  // outer `innerHTML = ''` resets in `renderResults`; subsequent
  // re-renders within the same DOM lifetime are no-ops.
  const _speedRowCache = new Map();
  function _speedRowKey(result) {
    return `${result.provider_id || ''}/${result.model || ''}/${result.run_index}`;
  }
  function _speedRowVersion(result) {
    // Hash only the columns that appear in the row; cheaper than JSON.stringify.
    return [
      result.success,
      result.total_time_ms,
      result.ttft_ms,
      result.prefill_tps,
      result.decode_tps,
      result.prompt_tokens,
      result.completion_tokens,
      result.error || '',
    ].join('|');
  }
  function _clearSpeedRowCache() { _speedRowCache.clear(); }

  function renderSpeedResults(results) {
    const resultsBodyEl = $('resultsBody');
    if (!resultsBodyEl) return;
    // The outer renderResults path may have just innerHTML=''ed the table;
    // cached row refs would be detached. Reap stale entries.
    for (const [k, v] of _speedRowCache) {
      if (!v.row.isConnected) _speedRowCache.delete(k);
    }
    const seen = new Set();
    const sorted = [...results].sort((a, b) => (b.decode_tps || 0) - (a.decode_tps || 0));
    sorted.forEach(result => {
      const key = _speedRowKey(result);
      seen.add(key);
      const version = _speedRowVersion(result);
      const cached = _speedRowCache.get(key);
      if (cached && cached.version === version) return;
      const row = document.createElement('tr');
      let statusBadge = '<span class="status-badge error" title="Failed" aria-label="Failed">✗</span>';
      if (result.success === true) {
        statusBadge = '<span class="status-badge success" title="Success" aria-label="Success">✓</span>';
      } else if (result.success === 'stopped') {
        statusBadge = '<span class="status-badge stopped" title="Stopped" aria-label="Stopped">■</span>';
      }
      row.innerHTML = `
        <td>${escapeHtml(result.provider_label || result.provider_id || '-')}</td>
        <td>${escapeHtml(result.model)}</td>
        <td>${result.run_index}</td>
        <td>${statusBadge}</td>
        <td>${formatMillisecondsAsSeconds(result.total_time_ms)}</td>
        <td>${formatMillisecondsAsSeconds(result.ttft_ms)}</td>
        <td>${formatTps(result.prefill_tps)}</td>
        <td>${formatTps(result.decode_tps)}</td>
        <td>${result.prompt_tokens || '-'}</td>
        <td>${result.completion_tokens || '-'}</td>
        <td class="text-error">${escapeHtml(result.error || '')}</td>
      `;
      if (cached && cached.row.isConnected) {
        resultsBodyEl.replaceChild(row, cached.row);
      } else {
        resultsBodyEl.appendChild(row);
      }
      _speedRowCache.set(key, { row, version });
    });
    // Remove cached rows that are no longer in results.
    for (const [key, cached] of _speedRowCache) {
      if (!seen.has(key) && cached.row.isConnected) {
        cached.row.remove();
      }
      if (!seen.has(key)) _speedRowCache.delete(key);
    }
  }

  // --- Speed Aggregated View ---
  const DECODE_THRESHOLDS = {
    good: 50,   // >= 50 tok/s
    mid: 20,    // 20-50 tok/s
    poor: 0     // < 20 tok/s
  };

  function getDecodeThresholdClass(value) {
    if (value === null || value === undefined) return 'none';
    if (value >= DECODE_THRESHOLDS.good) return 'good';
    if (value >= DECODE_THRESHOLDS.mid) return 'mid';
    return 'poor';
  }

  function formatTpsFixed(value) {
    return value != null ? Number(value).toFixed(2) : '—';
  }

  function formatMsFixed(value) {
    return value != null ? Number(value).toFixed(2) : '—';
  }

  function buildSparkline(runs) {
    if (!runs || !runs.length) return '<span class="text-muted">—</span>';
    const maxDecode = Math.max(...runs.map(r => r.decode_tps || 0).filter(v => v > 0), 1);
    const bars = runs.map(r => {
      const val = r.decode_tps || 0;
      const heightPct = val > 0 ? Math.max(10, (val / maxDecode) * 100) : 0;
      const cls = getDecodeThresholdClass(val);
      return `<div class="sparkline-bar__bar ${cls}" style="height: ${heightPct}%;" title="Run ${r.run_index}: ${formatTpsFixed(val)} tok/s"></div>`;
    }).join('');
    return `<div class="sparkline-bar" title="Per-run decode throughput">${bars}</div>`;
  }

  function renderAggregatedSpeedResults(job) {
    const container = $('speedResultsContainer');
    if (!container) return;
    const aggregates = job.aggregated_speed;
    if (!aggregates || !aggregates.length) {
      container.innerHTML = `
        <div class="empty-state" style="padding:32px;">
          <div style="font-size:1.2rem; margin-bottom:8px;">📊</div>
          <div>No completed speed runs to aggregate yet</div>
        </div>`;
      return;
    }

    let html = `
      <table class="speed-aggregated-table">
        <thead>
          <tr>
            <th style="width:32px;"></th>
            <th>Model</th>
            <th>Provider</th>
            <th style="width:100px;">Runs</th>
            <th style="width:140px;">Avg Decode (tok/s)</th>
            <th style="width:100px;">Avg TTFT (s)</th>
            <th style="width:100px;">Avg Total (s)</th>
            <th style="width:100px;">Min/Max Decode</th>
            <th style="width:100px;">Sparkline</th>
          </tr>
        </thead>
        <tbody>
    `;

    const sorted = [...aggregates].sort((a, b) => (b.avg_decode_tps || 0) - (a.avg_decode_tps || 0));
    sorted.forEach((agg, idx) => {
      const thresholdClass = getDecodeThresholdClass(agg.avg_decode_tps);
      const runCountHtml = `
        <span class="run-count">
          <span class="success">${agg.success_count}</span> / ${agg.run_count}
          ${agg.fail_count > 0 ? `<span class="fail">(${agg.fail_count} failed)</span>` : ''}
        </span>`;
      const minMaxHtml = agg.min_decode_tps !== null && agg.max_decode_tps !== null
        ? `${formatTpsFixed(agg.min_decode_tps)} / ${formatTpsFixed(agg.max_decode_tps)}`
        : '—';
      const sparklineHtml = buildSparkline(agg.runs);

      html += `
        <tr class="aggregate-row" data-model-idx="${idx}">
          <td><button class="expand-toggle" data-expand="${idx}" aria-label="Expand details">▼</button></td>
          <td><strong>${escapeHtml(agg.model)}</strong></td>
          <td>${escapeHtml(agg.provider_label || '-')}</td>
          <td>${runCountHtml}</td>
          <td><span class="decode-badge ${thresholdClass}">${formatTpsFixed(agg.avg_decode_tps)}</span></td>
          <td>${formatMillisecondsAsSeconds(agg.avg_ttft_ms)}</td>
          <td>${agg.avg_total_time_ms != null ? (agg.avg_total_time_ms / 1000).toFixed(2) : '—'}</td>
          <td class="text-muted">${minMaxHtml}</td>
          <td class="sparkline-cell">${sparklineHtml}</td>
        </tr>
        <tr class="run-detail-panel" data-detail="${idx}">
          <td colspan="9">
            <table class="run-detail-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Status</th>
                  <th>Decode (tok/s)</th>
                  <th>TTFT (ms)</th>
                  <th>Total (s)</th>
                  <th>PP (t/s)</th>
                  <th>Prompt Tok</th>
                  <th>Completion Tok</th>
                </tr>
              </thead>
              <tbody>
      `;

      agg.runs.forEach(run => {
        const thrClass = getDecodeThresholdClass(run.decode_tps);
        html += `
          <tr>
            <td>${run.run_index}</td>
            <td><span class="status-badge success">✓</span></td>
            <td><span class="decode-badge ${thrClass}">${formatTpsFixed(run.decode_tps)}</span></td>
            <td>${formatMillisecondsAsSeconds(run.ttft_ms)}</td>
            <td>${run.total_time_ms != null ? (run.total_time_ms / 1000).toFixed(2) : '—'}</td>
            <td>${formatTpsFixed(run.prefill_tps)}</td>
            <td>${run.prompt_tokens || '—'}</td>
            <td>${run.completion_tokens || '—'}</td>
          </tr>
        `;
      });

      html += `
              </tbody>
            </table>
          </td>
        </tr>
      `;
    });

    html += `
        </tbody>
      </table>
    `;

    container.innerHTML = html;

    // Attach expand listeners
    container.querySelectorAll('.expand-toggle').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const idx = btn.getAttribute('data-expand');
        const detailRow = container.querySelector(`.run-detail-panel[data-detail="${idx}"]`);
        const isExpanded = btn.classList.toggle('expanded');
        if (detailRow) detailRow.classList.toggle('visible', isExpanded);
        const aggregateRow = container.querySelector(`.aggregate-row[data-model-idx="${idx}"]`);
        if (aggregateRow) aggregateRow.classList.toggle('expanded', isExpanded);
      });
    });
  }

  function syncSqlMatrixLayout(root = document) {
    const scrollers = root.querySelectorAll ? root.querySelectorAll('.sql-result-scroll') : [];
    scrollers.forEach(scroller => {
      const questionCount = Number(scroller.dataset.questionCount || 0);
      if (!questionCount) return;

      const questionColumnWidth = parseFloat(getComputedStyle(scroller).getPropertyValue('--sql-question-column-width')) || 30;
      const minModelColumnWidth = 300;
      const maxModelColumnWidth = 640;
      const availableForModel = Math.floor(scroller.clientWidth - (questionCount * questionColumnWidth));
      const modelColumnWidth = Math.max(
        minModelColumnWidth,
        Math.min(maxModelColumnWidth, availableForModel)
      );

      scroller.style.setProperty('--sql-model-column-width', `${modelColumnWidth}px`);
    });
  }

  function getSqlFloatingTooltip() {
    let tooltip = document.getElementById('sqlFloatingTooltip');
    if (!tooltip) {
      tooltip = document.createElement('div');
      tooltip.id = 'sqlFloatingTooltip';
      tooltip.className = 'sql-floating-tooltip';
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function hideSqlFloatingTooltip() {
    const tooltip = document.getElementById('sqlFloatingTooltip');
    if (!tooltip) return;
    tooltip.classList.remove('visible');
  }

  function positionSqlFloatingTooltip(cell, tooltip = getSqlFloatingTooltip()) {
    if (!cell || !tooltip || !tooltip.classList.contains('visible')) return;

    const rect = cell.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const margin = 12;
    const gap = 8;

    const tooltipWidth = Math.min(tooltipRect.width || 0, Math.max(0, viewportWidth - margin * 2));
    const tooltipHeight = tooltipRect.height || 0;

    let left = rect.left + rect.width / 2 - tooltipWidth / 2;
    left = Math.max(margin, Math.min(left, viewportWidth - tooltipWidth - margin));

    let top = rect.top - tooltipHeight - gap;
    if (top < margin) top = rect.bottom + gap;
    if (top + tooltipHeight > viewportHeight - margin) {
      top = Math.max(margin, viewportHeight - tooltipHeight - margin);
    }

    tooltip.style.left = `${Math.round(left)}px`;
    tooltip.style.top = `${Math.round(top)}px`;
  }

  function showSqlFloatingTooltip(cell) {
    const source = cell ? cell.querySelector('.sql-cell-tooltip') : null;
    if (!source) return;
    const tooltip = getSqlFloatingTooltip();
    tooltip.innerHTML = source.innerHTML;
    tooltip.classList.add('visible');
    positionSqlFloatingTooltip(cell, tooltip);
  }


  function extractQuant(modelName) {
    // Unsloth Dynamic GGUF: UD_IQ1_S, UD_Q2_K_XL, etc.
    const unslothDynamic = modelName.match(/\bUD_(?:I?Q\d[\w_]*|[A-Z0-9_]+)\b/i);
    if (unslothDynamic) return unslothDynamic[0].toUpperCase();
    // GGUF: Q4_K_M, Q8_0, IQ3_M, IQ4_XS, etc.
    const gguf = modelName.match(/\b(IQ\d[\w]*|Q\d[\w]*)\b/i);
    if (gguf) return gguf[1].toUpperCase();
    // Named formats
    const named = modelName.match(/\b(AWQ|GPTQ|QAT|EETQ|BNB|NF4|UNSLOTH|DYNAMIC)\b/i);
    if (named) return named[1].toUpperCase();
    // Numeric bit-width: fp16, bf16, f16, f32, int4, int8, 4bit, 8bit
    const bits = modelName.match(/\b(fp16|bf16|f16|f32|int4|int8|\d+bit)\b/i);
    if (bits) return bits[1].toLowerCase();
    // Ollama tag after colon: llama3:8b-q4_k_m → q4_k_m
    const ollamaTag = modelName.match(/:[^:]*?(q\d[\w_]*)$/i);
    if (ollamaTag) return ollamaTag[1].toUpperCase();
    return null;
  }
  function renderSqlResults(results) {
    const container = $('sqlResultsContainer');
    if (!container) return;
    if (!results.length) {
      container.innerHTML = emptyState('No SQL benchmark results yet', { padding: '32px' });
      return;
    }

    // Detail view is now a modal rendered outside the table, so matrix re-renders no longer append panels below the heatmap.

    const difficultyOrder = ['trivial', 'easy', 'medium', 'hard'];
    const difficultyRank = (value) => {
      const normalized = String(value || '').trim().toLowerCase();
      const index = difficultyOrder.indexOf(normalized);
      return index >= 0 ? index : difficultyOrder.length;
    };
    const difficultyLabel = (value) => {
      const normalized = String(value || '').trim().toLowerCase();
      return difficultyOrder.includes(normalized) ? normalized : 'unknown';
    };
    const compareQuestionIds = (a, b) => {
      const an = Number(a);
      const bn = Number(b);
      if (Number.isFinite(an) && Number.isFinite(bn) && an !== bn) return an - bn;
      return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
    };

    // Collect question metadata, then sort by difficulty group and by question number inside the group.
    const questionMap = new Map();
    results.forEach(r => {
      if (r.question_id == null) return;
      const key = String(r.question_id);
      const current = questionMap.get(key);
      const nextDifficulty = difficultyLabel(r.difficulty);
      if (!current || difficultyRank(nextDifficulty) < difficultyRank(current.difficulty)) {
        questionMap.set(key, { id: r.question_id, difficulty: nextDifficulty });
      }
    });

    const questionMeta = Array.from(questionMap.values()).sort((a, b) => {
      const rankDiff = difficultyRank(a.difficulty) - difficultyRank(b.difficulty);
      if (rankDiff !== 0) return rankDiff;
      return compareQuestionIds(a.id, b.id);
    });
    const allQuestionIds = questionMeta.map(item => item.id);
    const groupStartByQid = new Set();
    const groups = [];
    questionMeta.forEach((item, index) => {
      const label = difficultyLabel(item.difficulty);
      if (index === 0 || groups[groups.length - 1].label !== label) {
        groups.push({ label, count: 1 });
        groupStartByQid.add(String(item.id));
      } else {
        groups[groups.length - 1].count += 1;
      }
    });

    const byModel = {};
    results.forEach(r => {
      const m = r.model || 'unknown';
      const tm = r.thinking_mode && r.thinking_mode !== 'off' ? ` [${r.thinking_mode}]` : '';
      const key = m + tm;
      if (!byModel[key]) byModel[key] = {};
      byModel[key][String(r.question_id)] = r;
    });

    // Determine status per cell: 'pass' | 'fail' | 'error'
    // pass = success:true
    // error = success:false AND error contains backend/execution/setup/parser failure markers
    // fail = success:false AND not error (result mismatch)
    function cellStatus(r) {
      if (r.success === true) return 'pass';
      const err = (r.error || '').toLowerCase();
      if (err.includes('failed') || err.includes('retry') || err.includes('empty') || err.includes('setup') || err.includes('aborted') || err.includes('callback') || err.includes('parser error'))
        return 'error';
      return 'fail';
    }

    // Build fixed-step matrix: first column stays sticky, each question column keeps the same width.
    const questionColGroup = allQuestionIds.map(() => '<col class="sql-question-col">').join('');
    let html = `<div class="sql-result-scroll" data-question-count="${allQuestionIds.length}"><table class="sql-result-table"><colgroup><col class="sql-model-col">${questionColGroup}</colgroup><thead>`;
    html += '<tr class="sql-category-row"><th class="sql-model-header" rowspan="2">Model</th>';
    groups.forEach(group => {
      html += `<th class="sql-category-header" colspan="${group.count}">${escapeHtml(group.label)}</th>`;
    });
    html += '</tr><tr class="sql-question-row">';
    questionMeta.forEach(meta => {
      const groupClass = groupStartByQid.has(String(meta.id)) ? ' sql-group-start' : '';
      html += `<th class="${groupClass.trim()}" title="Q${meta.id} · ${escapeHtml(meta.difficulty)}">Q${meta.id}</th>`;
    });
    html += '</tr></thead><tbody>';

    // Sort models by number of passed questions (best on top); tie-break by
    // name so ordering stays stable.
    const passedCount = (modelKey) => {
      const mr = byModel[modelKey];
      return allQuestionIds.filter(qid => mr[String(qid)] && mr[String(qid)].success === true).length;
    };
    const modelNames = Object.keys(byModel).sort((a, b) => {
      const diff = passedCount(b) - passedCount(a);
      if (diff !== 0) return diff;
      return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
    });
    modelNames.forEach(modelKey => {
      const modelResults = byModel[modelKey];
      const passed = allQuestionIds.filter(qid => modelResults[String(qid)] && modelResults[String(qid)].success === true).length;
      const total = allQuestionIds.length;
      const countClass = passed === total ? 'all-pass' : passed > 0 ? 'partial' : 'none';
      // modelKey may be "model-name" or "model-name [thinking]"
      const model = modelKey;

      const quantLabel = extractQuant(model);
      html += '<tr class="sql-result-model-row"><td class="sql-result-model-name">';
      html += `<div class="name-text" title="${escapeHtml(model)}">${escapeHtml(model)}`;
      if (quantLabel) html += `<span class="sql-quant-badge">${escapeHtml(quantLabel)}</span>`;
      html += `</div>`;
      html += `<span class="sql-result-count ${countClass}">${passed}/${total}</span>`;
      html += '</td>';

      questionMeta.forEach(meta => {
        const qid = meta.id;
        const cellClass = groupStartByQid.has(String(qid)) ? ' class="sql-group-start"' : '';
        const r = modelResults[String(qid)];
        if (!r) {
          html += `<td${cellClass}></td>`;
          return;
        }
        const st = cellStatus(r);
        const qNum = r.question_id || qid;
        const attempts = r.attempts || 1;
        const inTok = (r.input_tokens || 0).toLocaleString();
        const outTok = (r.output_tokens || 0).toLocaleString();
        const diff = difficultyLabel(r.difficulty || meta.difficulty);

        if (st === 'pass') {
          const tip = `<span class="sql-tooltip-status pass">PASS</span> — Q${qNum} [${diff}] — ${attempts} attempt(s) — ${inTok} in / ${outTok} out`;
          html += `<td${cellClass}><div class="sql-result-cell pass" data-qid="${qNum}" data-model-key="${escapeHtml(model)}" data-model="${escapeHtml(r.model || model)}" data-thinking="${escapeHtml(r.thinking_mode || '')}"><span class="sql-cell-tooltip">${tip}</span></div></td>`;
        } else if (st === 'error') {
          const errShort = (r.error || 'Unknown error').slice(0, 120);
          const tip = `<span class="sql-tooltip-status error">ERROR</span> — Q${qNum} [${diff}]<br>${escapeHtml(errShort)}`;
          html += `<td${cellClass}><div class="sql-result-cell error" data-qid="${qNum}" data-model-key="${escapeHtml(model)}" data-model="${escapeHtml(r.model || model)}" data-thinking="${escapeHtml(r.thinking_mode || '')}"><span class="sql-cell-tooltip">${tip}</span></div></td>`;
        } else {
          const errShort = (r.error || 'Result mismatch').slice(0, 120);
          const rc = r.row_count_match === true ? '✓ rows' : '✗ rows';
          const col = r.columns_match === true ? '✓ cols' : '✗ cols';
          const fr = r.first_row_match === true ? '✓ first' : '✗ first';
          // Surface the first concrete value diff (expected green / actual red).
          let diffLine = '';
          const dlist = Array.isArray(r.first_row_diffs) ? r.first_row_diffs : [];
          if (dlist.length) {
            const d0 = dlist[0];
            const more = dlist.length > 1 ? ` (+${dlist.length - 1})` : '';
            diffLine = `<br><span style="opacity:0.85;">${escapeHtml(String(d0.column ?? ''))}:</span> ` +
              `<span class="sql-tip-exp">${escapeHtml(String(d0.expected ?? '—'))}</span> ` +
              `<span style="opacity:0.6;">→</span> ` +
              `<span class="sql-tip-act">${escapeHtml(String(d0.actual ?? '—'))}</span>${escapeHtml(more)}`;
          } else if (r.row_count_match === false) {
            diffLine = `<br><span style="opacity:0.85;">rows:</span> ` +
              `<span class="sql-tip-exp">${escapeHtml(String(r.expected_row_count ?? '—'))}</span> ` +
              `<span style="opacity:0.6;">→</span> ` +
              `<span class="sql-tip-act">${escapeHtml(String(r.actual_row_count ?? '—'))}</span>`;
          }
          const tip = `<span class="sql-tooltip-status fail">FAIL</span> — Q${qNum} [${diff}] — ${attempts} attempt(s)<br>${escapeHtml(errShort)}<br>${rc} · ${col} · ${fr}${diffLine}`;
          html += `<td${cellClass}><div class="sql-result-cell fail" data-qid="${qNum}" data-model-key="${escapeHtml(model)}" data-model="${escapeHtml(r.model || model)}" data-thinking="${escapeHtml(r.thinking_mode || '')}"><span class="sql-cell-tooltip">${tip}</span></div></td>`;
        }
      });
      html += '</tr>';
    });

    html += '</tbody></table></div></div>';
    container.innerHTML = html;
    syncSqlMatrixLayout(container);
    requestAnimationFrame(() => syncSqlMatrixLayout(container));


    // Cell interactions
    container.querySelectorAll('.sql-result-cell').forEach(cell => {
      cell.addEventListener('mouseenter', () => showSqlFloatingTooltip(cell));
      cell.addEventListener('mousemove', () => positionSqlFloatingTooltip(cell));
      cell.addEventListener('mouseleave', hideSqlFloatingTooltip);
      cell.addEventListener('click', () => {
        hideSqlFloatingTooltip();
        const qid = cell.getAttribute('data-qid');
        const rawModel = cell.getAttribute('data-model');
        const thinkingMode = cell.getAttribute('data-thinking') || '';
        const result = results.find(r =>
          String(r.question_id) === String(qid) &&
          (r.model === rawModel) &&
          ((r.thinking_mode || '') === thinkingMode)
        );
        if (result) openSqlDetailModal(result);
      });
    });
    const sqlScroller = container.querySelector('.sql-result-scroll');
    if (sqlScroller) sqlScroller.addEventListener('scroll', hideSqlFloatingTooltip, { passive: true });
  }

  function sqlDetailStatus(result) {
    if (result && result.success === true) return 'pass';
    const err = String((result && result.error) || '').toLowerCase();
    if (err.includes('failed') || err.includes('retry') || err.includes('empty') || err.includes('setup') || err.includes('aborted') || err.includes('callback') || err.includes('parser error')) {
      return 'error';
    }
    return 'fail';
  }

  function sqlDetailValue(result, keys, fallback = '') {
    for (const key of keys) {
      const value = result ? result[key] : undefined;
      if (value !== undefined && value !== null && value !== '') return value;
    }
    return fallback;
  }

  function normalizeSqlDetailList(value) {
    if (Array.isArray(value)) return value.map(item => String(item).trim()).filter(Boolean);
    if (typeof value === 'string') {
      return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
    }
    return [];
  }

  function inferTablesFromSql(sqlText) {
    const tables = new Set();
    const sql = String(sqlText || '');
    const regex = /\b(?:from|join)\s+(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|([a-zA-Z_][\w.$]*))/gi;
    let match;
    while ((match = regex.exec(sql)) !== null) {
      const table = (match[1] || match[2] || match[3] || match[4] || '').trim();
      if (table) tables.add(table.replace(/^dbo\./i, ''));
    }
    return Array.from(tables);
  }

  function sqlDetailTables(result) {
    const direct = [
      ...normalizeSqlDetailList(result?.tables_used),
      ...normalizeSqlDetailList(result?.tables),
      ...normalizeSqlDetailList(result?.table_names),
    ];
    if (direct.length) return Array.from(new Set(direct));
    return inferTablesFromSql(`${result?.generated_sql || ''}\n${result?.expected_sql || ''}`);
  }

  function formatSqlDetailPayload(payload) {
    if (payload === undefined || payload === null || payload === '') return '';
    if (typeof payload === 'string') return payload;
    try {
      return JSON.stringify(payload, null, 2);
    } catch {
      return String(payload);
    }
  }

  function sqlDetailQuestionText(result) {
    const value = sqlDetailValue(result, [
      'question',
      'question_text',
      'prompt',
      'user_prompt',
      'nl_query',
      'natural_language_query',
      'description',
      'request_text',
    ], '');
    return String(value || '').trim();
  }

  function sqlDetailConversationPayload(result) {
    return sqlDetailValue(result, [
      'conversation',
      'conversation_history',
      'full_conversation',
      'messages',
      'chat_messages',
      'llm_messages',
      'raw_messages',
      'llm_calls',
      'llm_call_log',
      'call_log',
      'call_logs',
      'calls',
      'trace',
      'full_trace',
      'debug_trace',
      'llm_trace',
      'tool_trace',
      'tool_calls_detail',
      'model_response',
      'assistant_response',
      'raw_response',
    ], null);
  }

  function sqlDetailExecutionPayload(result) {
    return sqlDetailValue(result, [
      'tool_result',
      'tool_results',
      'execution_result',
      'query_result',
      'result_preview',
      'actual_result',
      'actual_rows',
      'rows',
    ], null);
  }

  function renderSqlDetailTags(items) {
    if (!items || !items.length) return '<div class="sql-detail-tags"><span class="sql-detail-tag">No table metadata</span></div>';
    return `<div class="sql-detail-tags">${items.map(item => `<span class="sql-detail-tag">${escapeHtml(item)}</span>`).join('')}</div>`;
  }

  function sqlDetailCodeBlock(label, sql, key, tables) {
    const tableText = tables && tables.length ? `Tables: ${tables.join(', ')}` : 'SQL';
    const code = String(sql || '').trim();
    return `
      <div class="sql-detail-code-card">
        <div class="sql-detail-code-meta">
          <span>${escapeHtml(tableText)}</span>
          <button type="button" class="sql-detail-copy-btn" data-sql-copy-target="${escapeHtml(key)}">Copy ${escapeHtml(label)}</button>
        </div>
        <pre class="sql-detail-code" data-sql-code="${escapeHtml(key)}"><code>${code ? escapeHtml(code) : '<span class="sql-detail-empty-code">No SQL available in result payload</span>'}</code></pre>
      </div>
    `;
  }

  function sqlDetailCheckIcon(value) {
    if (value === true) return '<span class="sql-check-icon pass">✓</span>';
    if (value === false) return '<span class="sql-check-icon fail">×</span>';
    return '<span class="sql-check-icon unknown">?</span>';
  }

  function sqlDetailCheckText(value) {
    if (value === true) return 'Passed';
    if (value === false) return 'Failed';
    return 'Not reported';
  }

  function sqlDetailCountPair(result, expectedKeys, actualKeys) {
    const expected = sqlDetailValue(result, expectedKeys, null);
    const actual = sqlDetailValue(result, actualKeys, null);
    if (expected === null && actual === null) return '—';
    return `${actual ?? '—'} / ${expected ?? '—'}`;
  }

  function sqlDetailColsCountPair(result) {
    const exp = Array.isArray(result.expected_columns) ? result.expected_columns.length : null;
    const act = Array.isArray(result.actual_columns) ? result.actual_columns.length : null;
    if (exp === null && act === null) return '—';
    return `${act ?? '—'} / ${exp ?? '—'}`;
  }

  function sqlDetailCheckClass(value) {
    if (value === true) return 'pass';
    if (value === false) return 'fail';
    return 'unknown';
  }

  function renderSqlDetailChecksInline(result) {
    const checks = [
      ['Rows', result.row_count_match, sqlDetailCountPair(result, ['expected_row_count'], ['actual_row_count'])],
      ['Columns', result.columns_match, sqlDetailColsCountPair(result)],
      ['First row', result.first_row_match, Array.isArray(result.first_row_diffs) && result.first_row_diffs.length ? `${result.first_row_diffs.length} diff(s)` : ''],
    ];
    return `
      <span class="sql-detail-check-strip" aria-label="SQL checks">
        ${checks.map(([label, value, detail]) => {
          const cls = sqlDetailCheckClass(value);
          const icon = value === true ? '✓' : value === false ? '×' : '?';
          const title = `${label}: ${sqlDetailCheckText(value)}${detail ? ` · ${detail}` : ''}`;
          return `<span class="sql-detail-check-pill ${cls}" title="${escapeHtml(title)}">${icon} ${escapeHtml(label)}${detail ? ` <span style="opacity:0.7;margin-left:2px;">${escapeHtml(detail)}</span>` : ''}</span>`;
        }).join('')}
      </span>
    `;
  }

  function sqlDetailConversationCount(payload) {
    if (Array.isArray(payload)) return payload.length;
    if (payload && typeof payload === 'object') {
      for (const key of ['messages', 'calls', 'llm_calls', 'conversation', 'trace', 'steps']) {
        if (Array.isArray(payload[key])) return payload[key].length;
      }
    }
    return null;
  }

  function renderSqlDetailConversation(result) {
    const payload = sqlDetailConversationPayload(result);
    if (!Array.isArray(payload) || !payload.length) {
      return `<details class="sql-detail-conversation">
        <summary>▸ Conversation / trace</summary>
        <div class="sql-detail-conversation-empty">No conversation data available in this result.</div>
      </details>`;
    }
    let callNum = 0;
    let html = '<div class="conv-wrap">';
    for (let i = 0; i < payload.length; i++) {
      const msg = payload[i];
      const role = msg.role || '';
      if (role === 'user') {
        html += `<div class="conv-msg conv-user-msg"><div class="conv-label conv-user-label">USER</div><div class="conv-text">${escapeHtml(String(msg.content || ''))}</div></div>`;
      } else if (role === 'assistant') {
        const toolCalls = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];
        const visibleToolCalls = toolCalls.filter(tc => {
          const fn = tc.function || {};
          const name = String(fn.name || '');
          const rawArgs = String(fn.arguments || '{}').trim();
          return !(name === 'results_ok' && (rawArgs === '{}' || rawArgs === ''));
        });
        const textContent = (msg.content || '').trim();
        if (textContent || visibleToolCalls.length) {
          callNum++;
          html += `<div class="conv-call-header">LLM CALL ${callNum}</div>`;
          if (textContent) {
            html += `<div class="conv-msg conv-thinking"><div class="conv-label conv-thinking-label">THINKING</div><div class="conv-text">${escapeHtml(textContent)}</div></div>`;
          }
          for (const tc of visibleToolCalls) {
            const fn = tc.function || {};
            const args = fn.arguments || '{}';
            let argsFormatted = args;
            try { argsFormatted = JSON.stringify(JSON.parse(args), null, 2); } catch(e) {}
            html += `<div class="conv-msg conv-tool-call"><div class="conv-label conv-tool-call-label">Tool call: ${escapeHtml(fn.name || 'unknown')}</div><pre class="conv-tool-code">${escapeHtml(argsFormatted)}</pre></div>`;
          }
        }
      } else if (role === 'tool') {
        html += `<div class="conv-msg conv-tool-result"><div class="conv-label conv-tool-result-label">TOOL RESULT</div><div class="conv-text">${escapeHtml(String(msg.content || ''))}</div></div>`;
      }
    }
    html += '</div>';
    return `<details class="sql-detail-conversation" open>
      <summary>▾ Conversation (${callNum} LLM call${callNum === 1 ? '' : 's'}, ${payload.length} message${payload.length === 1 ? '' : 's'})</summary>
      ${html}
    </details>`;
  }

  // Compact, color-coded mismatch summary shown near the TOP of the modal so
  // the failing check and its expected (green) vs actual (red) values are
  // visible without scrolling to the footer. Returns '' for passing results.
  function renderSqlMismatchSummary(result) {
    if (!result || result.success === true) return '';
    // Only meaningful for result-mismatch ('fail'); execution/setup errors are
    // already shown via the error banner.
    if (sqlDetailStatus(result) !== 'fail') return '';

    const fmt = (v) => v === null || v === undefined || v === '' ? '—' : String(v);
    const chip = (label, ok) =>
      `<span class="sql-mismatch-chip ${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'} ${escapeHtml(label)}</span>`;

    const chips = [
      chip('Rows', result.row_count_match === true),
      chip('Columns', result.columns_match === true),
      chip('First row', result.first_row_match === true),
    ].join('');

    const lines = [];

    // Row count mismatch
    if (result.row_count_match === false) {
      lines.push(`<div class="sql-mismatch-line"><span class="lbl">Rows</span>` +
        `<span class="sql-mismatch-val sql-mismatch-exp">expected ${escapeHtml(fmt(result.expected_row_count))}</span>` +
        `<span class="sql-mismatch-val sql-mismatch-act">got ${escapeHtml(fmt(result.actual_row_count))}</span></div>`);
    }
    // Column mismatch
    if (result.columns_match === false) {
      const exp = Array.isArray(result.expected_columns) ? result.expected_columns.join(', ') : '—';
      const act = Array.isArray(result.actual_columns) ? result.actual_columns.join(', ') : '—';
      lines.push(`<div class="sql-mismatch-line"><span class="lbl">Columns</span>` +
        `<span class="sql-mismatch-val sql-mismatch-exp">expected: ${escapeHtml(exp)}</span></div>` +
        `<div class="sql-mismatch-line"><span class="lbl"></span>` +
        `<span class="sql-mismatch-val sql-mismatch-act">got: ${escapeHtml(act)}</span></div>`);
    }
    // First-row value diffs -> compact table
    const diffs = Array.isArray(result.first_row_diffs) ? result.first_row_diffs : [];
    let diffTable = '';
    if (diffs.length) {
      diffTable = '<table class="sql-mismatch-table"><thead><tr>' +
        '<th>Column</th><th>Expected</th><th>Actual</th></tr></thead><tbody>' +
        diffs.map(d =>
          `<tr><td class="sql-mismatch-col">${escapeHtml(String(d.column ?? '—'))}</td>` +
          `<td class="sql-mismatch-exp">${escapeHtml(fmt(d.expected))}</td>` +
          `<td class="sql-mismatch-act">${escapeHtml(fmt(d.actual))}</td></tr>`
        ).join('') +
        '</tbody></table>';
    }

    if (!lines.length && !diffTable) {
      // Generic mismatch with no structured detail — show the raw error.
      if (!result.error) return '';
      lines.push(`<div class="sql-mismatch-line"><span class="sql-mismatch-val sql-mismatch-act">${escapeHtml(String(result.error))}</span></div>`);
    }

    return `<div class="sql-mismatch-summary">
      <div class="sql-mismatch-head"><span>Result mismatch</span>${chips}</div>
      ${lines.join('')}
      ${diffTable}
    </div>`;
  }

  function renderSqlDetailDiffs(result) {
    const diffs = Array.isArray(result.first_row_diffs) ? result.first_row_diffs : [];
    if (!diffs.length) return '';
    let diffHtml = '<div class="sql-detail-card"><div class="sql-detail-section-title" style="margin-top:0;">First Row Diffs</div>';
    diffHtml += '<table class="sql-detail-diff-table"><thead><tr><th>Column</th><th>Expected</th><th>Actual</th></tr></thead><tbody>';
    diffs.forEach(d => {
      diffHtml += `<tr><td>${escapeHtml(d.column || '-')}</td><td class="diff-val">${escapeHtml(String(d.expected ?? ''))}</td><td class="diff-val">${escapeHtml(String(d.actual ?? ''))}</td></tr>`;
    });
    diffHtml += '</tbody></table></div>';
    return diffHtml;
  }

  function renderSqlDetailMeta(result) {
    const items = [];
    const att = result.attempts || 0;
    const tc = result.tool_calls || 0;
    if (att) items.push(['Attempts', att]);
    if (tc) items.push(['Tool calls', tc]);
    const inTok = result.input_tokens || 0;
    const outTok = result.output_tokens || 0;
    if (inTok || outTok) items.push(['Tokens', `${inTok.toLocaleString()} in / ${outTok.toLocaleString()} out`]);
    if (result.cost) items.push(['Cost', `$${Number(result.cost).toFixed(4)}`]);
    if (!items.length) return '';
    return `<div class="sql-run-meta-strip">${items.map(([l,v]) =>
      `<span><strong>${escapeHtml(String(l))}</strong> ${escapeHtml(String(v))}</span>`
    ).join('')}</div>`;
  }

  function closeSqlDetailModal() {
    const overlay = document.getElementById('sqlDetailModalOverlay');
    if (overlay) overlay.remove();
    document.removeEventListener('keydown', handleSqlDetailModalEscape);
  }

  function handleSqlDetailModalEscape(event) {
    if (event.key === 'Escape') closeSqlDetailModal();
  }

  async function copySqlDetailText(text, button) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        area.remove();
      }
      if (button) {
        const oldText = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = oldText; }, 900);
      }
    } catch (error) {
      if (button) button.textContent = 'Copy failed';
    }
  }

  function openSqlDetailModal(result) {
    closeSqlDetailModal();

    const status = sqlDetailStatus(result);
    const statusLabel = status === 'pass' ? 'pass' : status === 'error' ? 'error' : 'fail';
    const model = result.model || 'unknown model';
    const difficulty = result.difficulty || 'unknown';
    const questionId = result.question_id || '-';
    const questionText = sqlDetailQuestionText(result) || 'No question text available in this result payload.';
    const tables = sqlDetailTables(result);
    const generatedSql = result.generated_sql || result.sql || '';
    const expectedSql = result.expected_sql || result.canonical_sql || '';
    const execution = formatSqlDetailPayload(sqlDetailExecutionPayload(result));

    const overlay = document.createElement('div');
    overlay.id = 'sqlDetailModalOverlay';
    overlay.className = 'sql-detail-modal-overlay';
    overlay.innerHTML = `
      <div class="sql-detail-modal" role="dialog" aria-modal="true" aria-label="SQL result details">
        <div class="sql-detail-modal-header">
          <div>
            <h2 class="sql-detail-modal-title" title="${escapeHtml(model)}">${escapeHtml(model)}</h2>
            <div class="sql-detail-modal-subtitle">
              <span>Q${escapeHtml(questionId)}</span>
              <span>·</span>
              <span>${escapeHtml(difficulty)}</span>
              <span>·</span>
              <span class="sql-detail-modal-status ${escapeHtml(status)}">${escapeHtml(statusLabel)}</span>
              ${renderSqlDetailChecksInline(result)}
            </div>
          </div>
          <button type="button" class="sql-detail-modal-close" data-sql-modal-close aria-label="Close SQL details">×</button>
        </div>
        <div class="sql-detail-modal-body">
          ${renderSqlDetailMeta(result)}
          <p class="sql-detail-question">${escapeHtml(questionText)}</p>

          <div class="sql-detail-section-title">Tables Used</div>
          ${renderSqlDetailTags(tables)}

          ${result.error ? `<div class="sql-detail-error">${escapeHtml(result.error)}</div>` : ''}
          ${renderSqlMismatchSummary(result)}

          <div class="sql-detail-tabs">
            <button type="button" class="sql-detail-tab active" data-sql-tab="model">Model SQL</button>
            <button type="button" class="sql-detail-tab" data-sql-tab="canonical">Canonical SQL</button>
          </div>
          <div class="sql-detail-tab-panel active" data-sql-tab-panel="model">
            ${sqlDetailCodeBlock('Model SQL', generatedSql, 'model', tables)}
          </div>
          <div class="sql-detail-tab-panel" data-sql-tab-panel="canonical">
            ${sqlDetailCodeBlock('Canonical SQL', expectedSql, 'canonical', tables)}
          </div>

          ${renderSqlDetailConversation(result)}

          <div class="sql-detail-grid">
            <div>
              ${execution ? `
                <div class="sql-detail-card">
                  <div class="sql-detail-section-title" style="margin-top:0;">Execution Result</div>
                  <pre class="sql-detail-code" style="max-height:260px;"><code>${escapeHtml(execution)}</code></pre>
                </div>
              ` : ''}
            </div>

          </div>
        </div>
      </div>
    `;

    overlay.addEventListener('click', (event) => {
      if (event.target === overlay || event.target.closest('[data-sql-modal-close]')) closeSqlDetailModal();
    });
    overlay.querySelectorAll('.sql-detail-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const key = tab.getAttribute('data-sql-tab');
        overlay.querySelectorAll('.sql-detail-tab').forEach(item => item.classList.toggle('active', item === tab));
        overlay.querySelectorAll('.sql-detail-tab-panel').forEach(panel => {
          panel.classList.toggle('active', panel.getAttribute('data-sql-tab-panel') === key);
        });
      });
    });
    overlay.querySelectorAll('[data-sql-copy-target]').forEach(button => {
      button.addEventListener('click', () => {
        const key = button.getAttribute('data-sql-copy-target');
        const code = overlay.querySelector(`[data-sql-code="${key}"]`);
        copySqlDetailText(code ? code.textContent : '', button);
      });
    });

    document.body.appendChild(overlay);
    document.addEventListener('keydown', handleSqlDetailModalEscape);
  }

  function syncSpeedToggleButton() {
    const speedToggle = $('speedViewToggle');
    if (speedToggle) speedToggle.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.getAttribute('data-view') === (state.speedViewMode || 'aggregated'))
    );
  }

  function renderResults(job, benchmarkType = state.activeBenchmarkType || 'speed') {
    // Cache the latest job so other code paths (e.g. the speed view
    // toggle) can answer "does this job have aggregates?" synchronously
    // without doing another fetch. The toggle handler also re-fetches the
    // job before re-rendering, so a stale cache is self-correcting on
    // the next click.
    state.lastJob = job;
    const results = job?.results || [];
    setResultsTableMode(benchmarkType);
    const resultsBodyEl = $('resultsBody');
    const speedToggle = $('speedViewToggle');
    const speedC = $('speedResultsContainer');
    const sqlC = $('sqlResultsContainer');

    if (!results.length) {
      if (benchmarkType === 'sql') {
        if (sqlC) { sqlC.innerHTML = emptyState('No SQL benchmark results yet', { padding: '32px' }); sqlC.style.display = ''; }
        if (speedC) speedC.style.display = 'none';
      } else {
        if (resultsBodyEl) resultsBodyEl.innerHTML = emptyState('No results yet', { colspan: 12 });
        if (speedC) speedC.style.display = '';
        if (sqlC) sqlC.style.display = 'none';
      }
      if (speedToggle) speedToggle.classList.add('hidden');
      return;
    }

    if (speedToggle) speedToggle.classList.remove('hidden');

    if (benchmarkType === 'sql') {
      if (speedC) speedC.style.display = 'none';
      if (sqlC) sqlC.style.display = '';
      renderSqlResults(results);
    } else {
      if (sqlC) sqlC.style.display = 'none';
      if (speedC) speedC.style.display = '';

      const hasAggregates = job.aggregated_speed && job.aggregated_speed.length;
      const viewMode = state.speedViewMode || 'aggregated';

      if (viewMode === 'raw' || !hasAggregates) {
        // Raw view, OR aggregated view but no pre-computed aggregates
        // (e.g. a saved run loaded from history whose record was written
        // before the backend started persisting aggregated_speed, or a run
        // that hasn't completed any successful result yet). Fall back to
        // the raw table so the user sees their data instead of a dead
        // placeholder. When we fall back, sync the toggle to 'raw' so the
        // Aggregated/Individual button reflects what's actually shown.
        if (!hasAggregates && viewMode !== 'raw') {
          // Fall back silently; caller is responsible for syncing toggle via syncSpeedToggleButton()
        }
        // Restore the structural table the placeholder would otherwise wipe:
        // renderAggregatedSpeedResults replaces speedResultsContainer's
        // innerHTML with a fresh <table>; the original <tbody id="resultsBody">
        // we depend on for the raw view is gone after that. Rebuild it.
        _ensureRawResultsTable();
        if (resultsBodyEl) resultsBodyEl.innerHTML = '';
        _clearSpeedRowCache();
        renderSpeedResults(results);
      } else {
        // Aggregated view, with aggregates available.
        if (resultsBodyEl) resultsBodyEl.innerHTML = '';
        renderAggregatedSpeedResults(job);
      }
    }
  }

  // Rebuild the raw <table> inside #speedResultsContainer after
  // renderAggregatedSpeedResults has replaced it with its own aggregated
  // <table class="speed-aggregated-table">. The raw view depends on a
  // <tbody id="resultsBody"> being present; if it isn't, create one and
  // the matching <table><thead> with the same column layout as
  // index.html. Idempotent: returns immediately if the structure is
  // already there.
  function _ensureRawResultsTable() {
    const speedC = $('speedResultsContainer');
    if (!speedC) return;
    if (speedC.querySelector('table > tbody#resultsBody')) return;
    speedC.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Model</th>
            <th>Run</th>
            <th>Status</th>
            <th>Total (s)</th>
            <th>TTFT (s)</th>
            <th>PP (t/s)</th>
            <th>Decode (t/s)</th>
            <th>Prompt Tok</th>
            <th>Completion Tok</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody id="resultsBody" aria-busy="false" aria-live="polite"></tbody>
      </table>
    `;
  }

  function formatActiveModelDisplay(providerLabel, modelName) {
    const provider = String(providerLabel || '').trim();
    const model = String(modelName || '').trim();
    if (provider && model) return `${provider}/${model}`;
    return model || provider || '-';
  }

  function updateSummary(job, benchmarkType = state.activeBenchmarkType || 'speed') {
    resetSummaryForMode(benchmarkType);
    const sr = $('successfulRuns');
    const am = $('activeModel');
    if (am) {
      am.textContent = formatActiveModelDisplay(job.progress?.current_provider_label, job.progress?.current_model);
    }

    if (benchmarkType === 'sql') {
      const successful = (job.results || []).filter(r => r.success);
      if (sr) sr.textContent = successful.length;
      const bl = $('bestLatency'); if (bl) bl.textContent = String((job.results || []).filter(r => r.row_count_match === true).length);
      const bp = $('bestPp'); if (bp) bp.textContent = String((job.results || []).filter(r => r.columns_match === true).length);
      const bd = $('bestDecode'); if (bd) bd.textContent = String((job.results || []).filter(r => r.first_row_match === true).length);
      return;
    }

    // Speed benchmark: use aggregated data if available
    const aggregates = job.aggregated_speed;
    if (aggregates && aggregates.length) {
      const totalSuccessful = aggregates.reduce((sum, a) => sum + a.success_count, 0);
      const totalRuns = aggregates.reduce((sum, a) => sum + a.run_count, 0);
      if (sr) sr.textContent = `${totalSuccessful} / ${totalRuns}`;

      // Best avg decode across models
      const avgDecodeValues = aggregates.map(a => a.avg_decode_tps).filter(v => v != null);
      const avgTtftValues = aggregates.map(a => a.avg_ttft_ms).filter(v => v != null);
      const avgTotalValues = aggregates.map(a => a.avg_total_time_ms).filter(v => v != null);

      const bl = $('bestLatency');
      if (bl) bl.textContent = avgTtftValues.length ? formatMillisecondsAsSeconds(Math.min(...avgTtftValues)) : 'n/a';

      const bp = $('bestPp');
      const avgPpValues = aggregates.map(a => a.avg_prefill_tps).filter(v => v != null);
      if (bp) bp.textContent = avgPpValues.length ? `${formatNumber(Math.max(...avgPpValues), 1)} t/s` : 'n/a';

      const bd = $('bestDecode');
      if (bd) bd.textContent = avgDecodeValues.length ? `${formatNumber(Math.max(...avgDecodeValues), 1)} t/s` : 'n/a';
    } else {
      // Fallback to raw results
      const successful = (job.results || []).filter(r => r.success);
      if (sr) sr.textContent = successful.length;

      if (successful.length) {
        const ttftValues = successful.map(r => r.ttft_ms).filter(v => v != null);
        const ppValues = successful.map(r => r.prefill_tps).filter(v => v != null);
        const decodeValues = successful.map(r => r.decode_tps).filter(v => v != null);

        const bl = $('bestLatency'); if (bl) bl.textContent = ttftValues.length ? formatMillisecondsAsSeconds(Math.min(...ttftValues)) : 'n/a';
        const bp = $('bestPp'); if (bp) bp.textContent = ppValues.length ? `${formatNumber(Math.max(...ppValues), 1)} t/s` : 'n/a';
        const bd = $('bestDecode'); if (bd) bd.textContent = decodeValues.length ? `${formatNumber(Math.max(...decodeValues), 1)} t/s` : 'n/a';
      }
    }
  }

  async function pollJob() {
    // Always poll the live run, not whatever the history view points at.
    const liveId = getActiveJobId();
    if (!liveId) return;

    try {
      const response = await fetch(apiUrl(`/api/benchmark/${liveId}`));
      const data = await response.json();
      if (data.status !== 'ok') throw new Error(data.error?.message || 'Poll failed');

      const job = data.job;
      // If the user has opened a *different* finished run in the history view,
      // keep polling/Stop alive but don't clobber what they're looking at.
      const viewingOther = state.activeHistoryJobId && state.activeHistoryJobId !== liveId;

      const completed = job.progress.completed || 0;
      const total = job.progress.total || 0;
      const percent = total > 0 ? (completed / total) * 100 : 0;

      // Cheap fingerprint of the parts that drive the UI. A 1-second poll
      // tick that returns the same fingerprint does no DOM work.
      const resultsFingerprint = (job.results || []).length + ':' +
        ((job.results || []).slice(-1)[0]?.timestamp || '') + ':' +
        (job.aggregated_speed ? job.aggregated_speed.length : 0) + ':' +
        job.status + ':' + completed;
      if (resultsFingerprint === state._lastResultsFingerprint) {
        applyButtonState(job.status);
        state.pollTimer = setTimeout(pollJob, 1000);
        return;
      }
      state._lastResultsFingerprint = resultsFingerprint;

      if (!viewingOther) {
        state.activeBenchmarkType = job.request?.benchmark_type || 'speed';
        renderResults(job, job.request?.benchmark_type || 'speed');
        updateSummary(job, job.request?.benchmark_type || 'speed');
        setCurrentOperation(job);

        const progressFill = $('progressFill');
        const progressText = $('progressText');
        if (progressFill) progressFill.style.width = `${percent}%`;
        if (progressText) progressText.textContent = `${completed} / ${total}`;
      }

      let statusType = 'info';
      if (job.status === 'completed') statusType = 'success';
      if (job.status === 'failed') statusType = 'error';

      setStatusBoth(`Status: ${job.status}. ${completed}/${total} complete.`, statusType);

      if (job.status === 'completed' || job.status === 'failed' || job.status === 'stopped') {
        if (!viewingOther) setCurrentOperation(job);
        try { sessionStorage.removeItem('llmSpeedTest.jobId'); } catch (_) {}
        state.liveJobId = null;
        state.pollTimer = null;
        stopPolling();
        loadHistory();
        // A stopped job means the user cancelled — drop any queued jobs.
        if (job.status === 'stopped') state.jobQueue = [];
        // Otherwise chain to the next queued test (e.g. SQL after Speed).
        if (state.jobQueue.length) {
          startNextQueuedJob();
        } else {
          applyButtonState(job.status);
        }
        return;
      }

      applyButtonState(job.status);
      state.pollTimer = setTimeout(pollJob, 1000);
    } catch (error) {
      // Transient poll failure (network blip, server 5xx). Do NOT touch
      // `state.jobQueue` here -- only `job.status === 'stopped'` (handled
      // above) should clear the queue. Dropping the queue on a fetch
      // error silently loses any queued follow-up jobs the user just
      // submitted (e.g. SQL after Speed).
      applyButtonState(null);
      state.liveJobId = null;
      stopPolling();
      try { sessionStorage.removeItem('llmSpeedTest.jobId'); } catch (_) {}
      setStatusBoth(`Polling failed: ${error.message}`, 'error');
    }
  }

  // Page-unload cleanup: stop any in-flight poll timer so it doesn't fire
  // after the document is gone. pagehide is the modern equivalent of
  // beforeunload and fires on bfcache as well.
  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('beforeunload', stopPolling);

  // Delegated click handler for [data-action="<fn>:<arg>"] buttons.
  // Replaces inline `onclick="fn('arg')"` so the markup stays declarative
  // and works under strict CSP.
  document.addEventListener('click', (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const [fnName, arg] = target.getAttribute('data-action').split(':', 2);
    const fn = window[fnName];
    if (typeof fn === 'function') {
      event.preventDefault();
      fn(arg);
    }
  });

  // ── Event listeners ──

  const endpointListEl = $('endpointList');
  if (endpointListEl) {
    endpointListEl.addEventListener('change', (event) => {
      const checkbox = event.target.closest('input[data-provider-include]');
      if (!checkbox) return;
      const providerId = checkbox.getAttribute('data-provider-include');
      if (checkbox.checked) state.includedProviderIds.add(providerId);
      else state.includedProviderIds.delete(providerId);
      updateActionButtons();
      renderEndpoints();
    });
    endpointListEl.addEventListener('click', (event) => {
      const removeBtn = event.target.closest('[data-provider-remove]');
      if (!removeBtn) return;
      event.preventDefault();
      event.stopPropagation();
      removeEndpoint(removeBtn.getAttribute('data-provider-remove'));
    });
  }

  const scanBtn = $('scanBtn');
  if (scanBtn) scanBtn.addEventListener('click', scanEndpoints);

  const manualProviderPreset = $('manualProviderPreset');
  if (manualProviderPreset) manualProviderPreset.addEventListener('change', applyManualPreset);

  const addManualBtn = $('addManualBtn');
  if (addManualBtn) {
    addManualBtn.addEventListener('click', () => {
      const form = $('manualProviderForm');
      if (form) form.classList.toggle('hidden');
    });
  }

  const manualProviderSubmit = $('manualProviderSubmit');
  if (manualProviderSubmit) manualProviderSubmit.addEventListener('click', addManualProvider);

  const discoverBtn = $('discoverBtn');
  if (discoverBtn) discoverBtn.addEventListener('click', discoverModels);

  const startBtn = $('startBtn');
  if (startBtn) startBtn.addEventListener('click', startBenchmark);

  const stopBtn = $('stopBtn');
  if (stopBtn) stopBtn.addEventListener('click', stopBenchmark);

  const refreshHistoryBtn = $('refreshHistoryBtn');
  if (refreshHistoryBtn) refreshHistoryBtn.addEventListener('click', loadHistory);

  const closeHistoryViewBtn = $('closeHistoryViewBtn');
  if (closeHistoryViewBtn) closeHistoryViewBtn.addEventListener('click', closeHistoryView);

  const clearSelectedHistoryBtn = $('clearSelectedHistoryBtn');
  if (clearSelectedHistoryBtn) clearSelectedHistoryBtn.addEventListener('click', () => clearHistory({ all: false }));

  const clearAllHistoryBtn = $('clearAllHistoryBtn');
  if (clearAllHistoryBtn) clearAllHistoryBtn.addEventListener('click', () => clearHistory({ all: true }));

  const selectAllHistory = $('selectAllHistory');
  if (selectAllHistory) {
    selectAllHistory.addEventListener('change', (event) => {
      state.historySelection = event.target.checked
        ? new Set(state.history.map(item => item.job_id))
        : new Set();
      renderHistory();
    });
  }

  const historyBodyEl = $('historyBody');
  if (historyBodyEl) {
    historyBodyEl.addEventListener('change', (event) => {
      const checkbox = event.target.closest('input[type="checkbox"][data-job-id]');
      if (!checkbox) return;
      const jobId = checkbox.getAttribute('data-job-id');
      if (checkbox.checked) state.historySelection.add(jobId);
      else state.historySelection.delete(jobId);
      updateHistorySelectionUi();
    });
    historyBodyEl.addEventListener('click', (event) => {
      const openBtn = event.target.closest('[data-history-open]');
      if (!openBtn) return;
      openHistoryJob(openBtn.getAttribute('data-history-open'));
    });
  }

  const modelSearchEl = $('modelSearch');
  if (modelSearchEl) modelSearchEl.addEventListener('input', filterModels);

  ['typeSpeed', 'typeSql'].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', updateBenchmarkModeUi);
  });
  ['thinkOff', 'thinkOn'].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', updateActionButtons);
  });

  const questionIdsEl = $('questionIds');
  if (questionIdsEl) questionIdsEl.addEventListener('input', updateActionButtons);

  const selectAllBtn = $('selectAllBtn');
  if (selectAllBtn) {
    selectAllBtn.addEventListener('click', (event) => {
      event.preventDefault();
      if (state.isBenchmarkRunning) return;
      const modelListEl = $('modelList');
      if (modelListEl) modelListEl.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = true);
      const selected = new Set(getSelectedModelsForProvider(state.activeProviderId));
      state.filteredModels.forEach(model => selected.add(model));
      setSelectedModelsForProvider(state.activeProviderId, Array.from(selected));
      updateModelCount();
      updateActionButtons();
    });
  }

  const selectNoneBtn = $('selectNoneBtn');
  if (selectNoneBtn) {
    selectNoneBtn.addEventListener('click', (event) => {
      event.preventDefault();
      if (state.isBenchmarkRunning) return;
      const modelListEl = $('modelList');
      if (modelListEl) modelListEl.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
      setSelectedModelsForProvider(state.activeProviderId, []);
      updateModelCount();
      updateActionButtons();
    });
  }

  const modelListEl = $('modelList');
  if (modelListEl) {
    modelListEl.addEventListener('change', (event) => {
      if (state.isBenchmarkRunning) return;
      const checkbox = event.target.closest('input[type="checkbox"]');
      if (!checkbox) return;
      setModelCheckedForActiveProvider(checkbox.value, checkbox.checked);
      updateModelCount();
      updateActionButtons();
    });
  }

  // Speed view toggle
  let _speedToggleGeneration = 0;
  const speedToggleEl = $('speedViewToggle');
  if (speedToggleEl) {
    speedToggleEl.addEventListener('click', (event) => {
      const btn = event.target.closest('button[data-view]');
      if (!btn) return;
      const view = btn.getAttribute('data-view');
      if (view !== 'aggregated' && view !== 'raw') return;

      // If the user wants aggregated view but the cached job has no
      // aggregates (e.g. an old saved run loaded from history), don't
      // optimistically flip the toggle. Otherwise the button briefly
      // shows "Aggregated" before the async re-render flips it back.
      // The re-render below will still happen and update the toggle
      // via the fallback in renderResults.
      const hasCachedAggregates = !!(state.lastJob && state.lastJob.aggregated_speed && state.lastJob.aggregated_speed.length);
      if (view !== 'aggregated' || hasCachedAggregates) {
        state.speedViewMode = view;
        try { sessionStorage.setItem('llmSpeedTest.speedViewMode', view); } catch (_) {}
        speedToggleEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.getAttribute('data-view') === view));
      }

      // Re-render current results with new view
      const jobId = getActiveJobId();
      if (jobId) {
        const gen = ++_speedToggleGeneration;
        if (state.lastJob && state.lastJob.job_id === jobId) {
          renderResults(state.lastJob, state.lastJob.request?.benchmark_type || 'speed');
        } else {
          fetch(apiUrl(`/api/benchmark/${jobId}`))
            .then(r => r.json())
            .then(data => {
              if (gen !== _speedToggleGeneration) return;  // stale
              if (data.status === 'ok' && data.job) {
                renderResults(data.job, data.job.request?.benchmark_type || 'speed');
              }
            });
        }
      }
    });
  }

  // Load persisted speed view mode
  try {
    const savedView = sessionStorage.getItem('llmSpeedTest.speedViewMode');
    if (savedView === 'aggregated' || savedView === 'raw') {
      state.speedViewMode = savedView;
      const speedToggleEl = $('speedViewToggle');
      if (speedToggleEl) {
        speedToggleEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.getAttribute('data-view') === savedView));
      }
    }
  } catch (_) {}

  window.addEventListener('resize', () => { hideSqlFloatingTooltip(); syncSqlMatrixLayout(document); });
  window.addEventListener('scroll', hideSqlFloatingTooltip, true);

  // ── Init ──
  loadStoredManualProviders();
  if (state.manualProviders.length) {
    selectEndpoint(state.manualProviders[0].id);
  } else {
    renderEndpoints();
  }
  updateActionButtons();
  applyManualPreset();
  updateBenchmarkModeUi();
  loadHistory();
  restoreActiveJob();
  scanEndpoints();

