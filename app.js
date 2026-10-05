(function () {
  'use strict';

  var POLL_MS = 1500;
  var STATIC_MODE = window.HANGAUGE_STATIC === true;
  var TERMINAL_TEST_STATUSES = { completed: true, stopped: true, failed: true, interrupted: true };
  var RUNNING_TEST_STATUSES = { running: true, stopping: true };
  var SCOPE_LABELS = {
    all: '全量 360 题，可能产生较高费用',
    per_task: '每个任务首题，约 20 题',
    one: '首题 1 题'
  };

  var state = {
    dataset: null,
    runs: [],
    run: null,
    status: {
      active_run_id: null,
      storage_path: '',
      baseline_run_id: null,
      baseline_name: '',
      read_only: STATIC_MODE,
      app_name: 'HanGauge',
      bundled_run_ids: []
    },
    pollTimer: null,
    busyCount: 0,
    loadingSeq: 0,
    listSeq: 0,
    caseSeq: 0,
    casePanels: {},
    launching: false,
    grading: false,
    unloading: false
  };
  var el = {};

  document.addEventListener('DOMContentLoaded', function () {
    collectElements([
      'downloadQuestions', 'downloadRun', 'globalStatus', 'runSelect', 'newName', 'newNotes',
      'renameRun', 'gradeRun', 'deleteRun', 'runMeta', 'statCases', 'statAnswered',
      'statGraded', 'statGradeStatus', 'taskRows', 'domainRows',
      'candidateBaseUrl', 'candidateModel', 'candidateApiKey', 'candidateTemperature',
      'candidateMaxTokens', 'candidateThinking', 'graderBaseUrl', 'graderModel',
      'graderApiKey', 'graderTemperature', 'graderMaxTokens', 'graderThinking',
      'testScope', 'saveSettings', 'runTest', 'stopTest',
      'testStatus', 'testProgress', 'storageInfo', 'baselineInfo',
      'abilityRunName', 'abilityHighlights', 'abilityScores',
      'overallScoreLabel', 'overallScore', 'overallScoreNote', 'domainScores',
      'comparisonSummary', 'comparisonTaskHead', 'comparisonTaskRows',
      'comparisonDomainHead', 'comparisonDomainRows'
    ]);

    if (el.runSelect) el.runSelect.addEventListener('change', function () { loadRun(el.runSelect.value); });
    if (el.runTest) el.runTest.addEventListener('click', startTest);
    if (el.stopTest) el.stopTest.addEventListener('click', stopSelectedTest);
    if (el.renameRun) el.renameRun.addEventListener('click', renameRun);
    if (el.gradeRun) el.gradeRun.addEventListener('click', gradeExistingRun);
    if (el.deleteRun) el.deleteRun.addEventListener('click', deleteRun);
    if (el.testScope) el.testScope.addEventListener('change', renderTestHint);
    if (el.saveSettings) el.saveSettings.addEventListener('click', saveSettings);
    window.addEventListener('beforeunload', function () {
      state.unloading = true;
      clearPollTimer();
    });
    renderMode();

    boot();
  });

  function collectElements(ids) {
    ids.forEach(function (id) { el[id] = document.getElementById(id); });
  }

  function beginBusy() {
    state.busyCount += 1;
    updateControls();
  }

  function endBusy() {
    state.busyCount = Math.max(0, state.busyCount - 1);
    updateControls();
  }

  function isBusy() { return state.busyCount > 0; }

  function setMessage(node, message, kind) {
    if (!node) return;
    node.textContent = message || '';
    node.className = 'status-line' + (kind ? ' ' + kind : '');
  }

  function api(path, options) {
    var opts = options || {};
    var method = (opts.method || 'GET').toUpperCase();
    if (STATIC_MODE) {
      if (method !== 'GET') {
        return Promise.reject(new Error('公开静态页面为只读模式，不能创建、评分、修改或删除评测。'));
      }
      path = staticPathFor(path);
    } else if (path.charAt(0) === '/') {
      path = path.slice(1);
    }
    opts.headers = opts.headers || {};
    if (opts.body && !opts.headers['Content-Type']) opts.headers['Content-Type'] = 'application/json';
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (err) { data = { error: text }; }
        }
        if (!res.ok) {
          throw new Error((data && data.error) || ('HTTP ' + res.status));
        }
        return data;
      });
    }).catch(function (err) {
      throw new Error('请求失败：' + err.message);
    });
  }

  function staticPathFor(path) {
    var clean = path.charAt(0) === '/' ? path.slice(1) : path;
    var parts = clean.split('?');
    var route = parts[0];
    var query = parts[1] || '';
    if (route === 'api/dataset') return 'dataset.json';
    if (route === 'api/status') return 'status.json';
    if (route === 'api/settings') return 'settings.json';
    if (route === 'api/runs') return 'runs.json';
    var casesMatch = route.match(/^api\/runs\/([^/]+)\/cases$/);
    if (casesMatch) {
      var taskId = new URLSearchParams(query).get('task_id');
      if (!taskId) throw new Error('静态逐题明细缺少task_id。');
      return 'runs/' + encodeURIComponent(decodeURIComponent(casesMatch[1])) + '/cases/' + encodeURIComponent(taskId) + '.json';
    }
    var runMatch = route.match(/^api\/runs\/([^/]+)$/);
    if (runMatch) return 'runs/' + encodeURIComponent(decodeURIComponent(runMatch[1])) + '.json';
    throw new Error('静态页面没有这个接口映射：' + path);
  }

  function boot() {
    beginBusy();
    setDownloadLinks(null);
    setMessage(el.globalStatus, '正在加载题目、设置和已保存评测…');
    Promise.all([
      api('api/dataset'),
      api('api/runs'),
      api('api/settings'),
      api('api/status')
    ])
      .then(function (results) {
        state.dataset = results[0];
        state.runs = Array.isArray(results[1]) ? results[1] : [];
        fillSettings(results[2] || {});
        applyStatus(results[3] || {});
        renderMode();
        renderRunSelector();
        renderComparison();
        var requestedId = new URLSearchParams(location.search).get('run');
        var selected = state.runs.find(function (run) { return run.id === requestedId; });
        var active = state.status.active_run_id && state.runs.find(function (run) { return run.id === state.status.active_run_id; });
        var baseline = state.status.baseline_run_id && state.runs.find(function (run) { return run.id === state.status.baseline_run_id; });
        var latestScored = state.runs.find(function (run) {
          return !isBaselineRun(run) && run.graded > 0 && run.grading && run.grading.status === 'completed';
        });
        var firstRun = selected || active || latestScored || baseline || state.runs[0];
        if (firstRun) return loadRun(firstRun.id, true);
        state.run = null;
        state.casePanels = {};
        renderRun();
        setMessage(el.globalStatus, state.status.read_only ? '未找到已保存评测。' : '还没有已保存评测，请先运行测试。', state.status.read_only ? 'error' : 'warn');
      })
      .then(function () {
        if (state.run) setMessage(el.globalStatus, selectedRunNotice(state.run));
        schedulePollingIfNeeded();
      })
      .catch(function (err) { setMessage(el.globalStatus, err.message, 'error'); })
      .finally(function () {
        endBusy();
        renderTestHint();
      });
  }

  function fillSettings(settings) {
    var candidate = settings && settings.candidate ? settings.candidate : (settings || {});
    var grader = settings && settings.grader ? settings.grader : {};
    setInputValue(el.candidateBaseUrl, candidate.base_url || '');
    setInputValue(el.candidateModel, candidate.model || '');
    setInputValue(el.candidateTemperature, candidate.temperature == null ? 0 : candidate.temperature);
    setInputValue(el.candidateMaxTokens, candidate.max_tokens == null ? 4096 : candidate.max_tokens);
    setInputValue(el.candidateThinking, thinkingToValue(candidate.enable_thinking));
    setInputValue(el.testScope, candidate.scope || settings.scope || 'all');
    setInputValue(el.graderBaseUrl, grader.base_url || '');
    setInputValue(el.graderModel, grader.model || '');
    setInputValue(el.graderTemperature, grader.temperature == null ? 0 : grader.temperature);
    setInputValue(el.graderMaxTokens, grader.max_tokens == null ? 768 : grader.max_tokens);
    setInputValue(el.graderThinking, thinkingToValue(grader.enable_thinking));
    clearSecretInputs();
  }

  function applyStatus(status) {
    state.status = {
      active_run_id: status && typeof status.active_run_id === 'string' ? status.active_run_id : null,
      storage_path: status && typeof status.storage_path === 'string' ? status.storage_path : '',
      baseline_run_id: status && typeof status.baseline_run_id === 'string' ? status.baseline_run_id : null,
      baseline_name: status && typeof status.baseline_name === 'string' ? status.baseline_name : '',
      read_only: STATIC_MODE || !!(status && status.read_only),
      app_name: status && status.app_name ? status.app_name : 'HanGauge',
      bundled_run_ids: status && Array.isArray(status.bundled_run_ids) ? status.bundled_run_ids : []
    };
    renderStorageInfo();
  }

  function renderMode() {
    var readOnly = !!(state.status && state.status.read_only);
    var nodes = document.querySelectorAll('[data-mutable-only]');
    Array.prototype.forEach.call(nodes, function (node) {
      node.classList.toggle('hidden', readOnly);
    });
    setDownloadLinks(state.run);
    updateControls();
  }

  function setDownloadLinks(run) {
    if (el.downloadQuestions) {
      el.downloadQuestions.href = STATIC_MODE ? 'dataset.json' : 'api/dataset';
    }
    if (el.downloadRun && run && run.id) {
      el.downloadRun.href = runDownloadHref(run.id);
      el.downloadRun.download = (run.id || 'hangauge-run') + '.json';
    }
  }

  function runDownloadHref(runId) {
    var encoded = encodeURIComponent(runId);
    return STATIC_MODE ? ('runs/' + encoded + '.json') : ('api/runs/' + encoded);
  }

  function selectedRunNotice(run) {
    if (!run) return '';
    if (isBaselineRun(run)) return '当前选择参考基线：100分只作对照，不是竞争模型成绩。';
    if (isProtectedRun(run)) return '当前选择内置只读结果：显示真实得分，可展开逐题明细。';
    return '';
  }

  function clearSecretInputs() {
    if (el.candidateApiKey) el.candidateApiKey.value = '';
    if (el.graderApiKey) el.graderApiKey.value = '';
  }

  function collectPublicSettings(requireCandidate) {
    return {
      candidate: collectCandidateConfig(requireCandidate),
      grader: collectGraderConfig(false, false)
    };
  }

  function collectCandidateConfig(required) {
    var baseUrl = valueOf(el.candidateBaseUrl).trim();
    var model = valueOf(el.candidateModel).trim();
    if (required && !baseUrl) throw new Error('请填写候选模型Base URL。');
    if (required && !model) throw new Error('请填写候选模型ID。');
    var config = {
      base_url: baseUrl,
      model: model,
      temperature: parseNumberInput(el.candidateTemperature, 0),
      max_tokens: parseIntegerInput(el.candidateMaxTokens, 4096),
      scope: valueOf(el.testScope) || 'all'
    };
    if (!SCOPE_LABELS[config.scope]) throw new Error('请选择有效评测范围。');
    applyThinking(config, el.candidateThinking);
    return config;
  }

  function collectGraderConfig(required, includeKey) {
    var baseUrl = valueOf(el.graderBaseUrl).trim();
    var model = valueOf(el.graderModel).trim();
    if (required && !baseUrl) throw new Error('请填写评分模型Base URL。');
    if (required && !model) throw new Error('请填写评分模型ID。');
    var config = {
      base_url: baseUrl,
      model: model,
      temperature: parseNumberInput(el.graderTemperature, 0),
      max_tokens: parseIntegerInput(el.graderMaxTokens, 768)
    };
    applyThinking(config, el.graderThinking);
    if (includeKey) config.api_key = valueOf(el.graderApiKey);
    return config;
  }

  function applyThinking(config, node) {
    var raw = valueOf(node);
    if (raw === 'true') config.enable_thinking = true;
    else if (raw === 'false') config.enable_thinking = false;
  }

  function thinkingToValue(value) {
    if (value === true) return 'true';
    if (value === false) return 'false';
    return '';
  }

  function refreshStatus() {
    return api('api/status').then(function (status) {
      applyStatus(status || {});
      return state.status;
    });
  }

  function refreshRuns(selectedId) {
    var seq = ++state.listSeq;
    return api('api/runs').then(function (runs) {
      if (seq !== state.listSeq) return runs;
      state.runs = Array.isArray(runs) ? runs : [];
      renderRunSelector(selectedId || (state.run && state.run.id));
      renderComparison();
      return runs;
    });
  }

  function mergeRunInList(run) {
    if (!run || !run.id || !Array.isArray(state.runs)) return;
    var replaced = false;
    state.runs = state.runs.map(function (item) {
      if (item.id !== run.id) return item;
      replaced = true;
      return Object.assign({}, item, run);
    });
    if (!replaced) state.runs.unshift(run);
  }

  function renderRunSelector(selectedId) {
    if (!el.runSelect) return;
    clearNode(el.runSelect);
    if (!state.runs.length) {
      var empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '暂无评测';
      el.runSelect.appendChild(empty);
      return;
    }
    state.runs.forEach(function (run) {
      var option = document.createElement('option');
      option.value = run.id;
      option.textContent = formatRunLabel(run);
      el.runSelect.appendChild(option);
    });
    var wanted = selectedId || (state.run && state.run.id);
    if (wanted) el.runSelect.value = wanted;
  }

  function formatRunLabel(run) {
    var answered = numberOrZero(run.answered);
    var total = numberOrZero(run.total || datasetTotal());
    var label = run.name || run.id;
    if (isBaselineRun(run)) label = '【参考基线】' + label;
    else if (isProtectedRun(run)) label = '【内置只读】' + label;
    if (RUNNING_TEST_STATUSES[run.test_status]) label += ' · 运行中';
    if (run.grading_status === 'running') label += ' · 评分中';
    return label + '（' + answered + '/' + total + '）';
  }

  function findBaselineRun() {
    return state.status && state.status.baseline_run_id
      ? state.runs.find(function (run) { return run.id === state.status.baseline_run_id; })
      : null;
  }

  function loadRun(id, silent) {
    if (!id) return Promise.resolve();
    var seq = ++state.loadingSeq;
    var previousId = state.run && state.run.id;
    if (!silent) {
      beginBusy();
      setMessage(el.globalStatus, '正在加载评测…');
    }
    return api('api/runs/' + encodeURIComponent(id))
      .then(function (run) {
        if (seq !== state.loadingSeq || state.unloading) return run;
        if (previousId !== run.id) state.casePanels = {};
        state.run = run;
        mergeRunInList(run);
        renderRunSelector(run.id);
        renderRun();
        renderComparison();
        if (!silent) setMessage(el.globalStatus, selectedRunNotice(run));
        return run;
      })
      .catch(function (err) {
        if (seq === state.loadingSeq && !silent) setMessage(el.globalStatus, err.message, 'error');
        if (seq === state.loadingSeq && silent) throw err;
      })
      .finally(function () {
        if (!silent) endBusy();
      });
  }

  function startTest() {
    if (state.status && state.status.read_only) {
      setMessage(el.testStatus || el.globalStatus, '当前为只读浏览模式，不能启动评测。', 'warn');
      return;
    }
    if (state.launching) return;
    if (state.status && state.status.active_run_id) {
      setMessage(el.testStatus || el.globalStatus, '已有测试或评分正在运行，请等待结束后再启动新测试。', 'warn');
      return;
    }
    var payload;
    try {
      payload = collectTestPayload();
    } catch (err) {
      setMessage(el.testStatus || el.globalStatus, err.message, 'error');
      return;
    }
    state.launching = true;
    beginBusy();
    setMessage(el.testStatus, '正在创建新评测并启动测试…', 'warn');
    setMessage(el.globalStatus, '正在启动测试…');
    api('api/tests', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (run) {
        clearSecretInputs();
        state.casePanels = {};
        state.run = run;
        renderRun();
        return Promise.all([refreshStatus(), refreshRuns(run.id)]).then(function () { return run; });
      })
      .then(function (run) {
        renderRunSelector(run.id);
        setMessage(el.globalStatus, '已启动测试，API Key 未保存并已清空。', 'ok');
        setMessage(el.testStatus, '测试已启动；答题阶段可请求停止。', 'warn');
        schedulePollingIfNeeded(true);
      })
      .catch(function (err) {
        setMessage(el.testStatus || el.globalStatus, err.message, 'error');
        setMessage(el.globalStatus, err.message, 'error');
      })
      .finally(function () {
        state.launching = false;
        clearSecretInputs();
        endBusy();
      });
  }

  function collectTestPayload() {
    var candidate = collectCandidateConfig(true);
    var grader = collectGraderConfig(candidate.scope !== 'one', true);
    return Object.assign({}, candidate, {
      name: valueOf(el.newName).trim() || candidate.model,
      notes: valueOf(el.newNotes).trim(),
      api_key: valueOf(el.candidateApiKey),
      grader: grader
    });
  }

  function saveSettings() {
    if (state.status && state.status.read_only) {
      setMessage(el.testStatus || el.globalStatus, '当前为只读浏览模式，不能保存设置。', 'warn');
      return;
    }
    var payload;
    try {
      payload = collectPublicSettings(false);
      var publicText = JSON.stringify(payload);
      [valueOf(el.candidateApiKey), valueOf(el.graderApiKey)].forEach(function (key) {
        if (key && publicText.indexOf(key) !== -1) {
          throw new Error('公开设置中不能包含API Key，请只在密钥输入框填写。');
        }
      });
    } catch (err) {
      setMessage(el.testStatus || el.globalStatus, err.message, 'error');
      return;
    }
    beginBusy();
    setMessage(el.testStatus, '正在保存公开设置…', 'warn');
    api('api/settings', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (settings) {
        fillSettings(settings || payload);
        setMessage(el.testStatus, '已保存公开设置；API Key 未保存。', 'ok');
      })
      .catch(function (err) { setMessage(el.testStatus || el.globalStatus, err.message, 'error'); })
      .finally(function () {
        clearSecretInputs();
        endBusy();
      });
  }

  function stopSelectedTest() {
    if (state.status && state.status.read_only) {
      setMessage(el.testStatus || el.globalStatus, '当前为只读浏览模式，不能停止评测。', 'warn');
      return;
    }
    if (!state.run || !isSelectedRunActive()) {
      setMessage(el.testStatus || el.globalStatus, '当前评测没有正在运行的测试。', 'warn');
      return;
    }
    if (isGradingActive(state.run)) {
      setMessage(el.testStatus || el.globalStatus, '当前处于评分阶段，不能停止；已保存回答和评分进度会继续保留。', 'warn');
      return;
    }
    beginBusy();
    setMessage(el.testStatus, '已请求停止；会等待当前 HTTP 请求结束后保存状态。', 'warn');
    api('api/runs/' + encodeURIComponent(state.run.id) + '/stop', { method: 'POST', body: JSON.stringify({}) })
      .then(function (run) {
        state.run = run;
        renderRun();
        return Promise.all([refreshStatus(), refreshRuns(run.id)]).then(function () { return run; });
      })
      .then(function () {
        setMessage(el.globalStatus, '已提交停止请求。', 'warn');
        schedulePollingIfNeeded(true);
      })
      .catch(function (err) { setMessage(el.testStatus || el.globalStatus, err.message, 'error'); })
      .finally(function () { endBusy(); });
  }

  function gradeExistingRun() {
    if (state.status && state.status.read_only) {
      setMessage(el.globalStatus, '当前为只读浏览模式，不能启动评分。', 'warn');
      return;
    }
    if (state.grading) return;
    var reason = gradeDisabledReason(state.run);
    if (reason) {
      setMessage(el.globalStatus, reason, 'warn');
      return;
    }
    var payload;
    try {
      payload = { grader: collectGraderConfig(true, true) };
    } catch (err) {
      setMessage(el.globalStatus, err.message, 'error');
      return;
    }
    state.grading = true;
    beginBusy();
    setMessage(el.globalStatus, '正在启动评分当前回答…', 'warn');
    api('api/runs/' + encodeURIComponent(state.run.id) + '/grade', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (run) {
        if (run && run.id) state.run = run;
        return Promise.all([refreshStatus(), refreshRuns(state.run && state.run.id)]);
      })
      .then(function () {
        if (state.run) return loadRun(state.run.id, true);
        return null;
      })
      .then(function () {
        setMessage(el.globalStatus, '已启动评分；完成前会自动刷新进度。', 'ok');
        schedulePollingIfNeeded(true);
      })
      .catch(function (err) { setMessage(el.globalStatus, err.message, 'error'); })
      .finally(function () {
        clearSecretInputs();
        state.grading = false;
        endBusy();
      });
  }

  function renameRun() {
    if (state.status && state.status.read_only) {
      setMessage(el.globalStatus, '当前为只读浏览模式，不能修改名称或备注。', 'warn');
      return;
    }
    if (!state.run) return;
    if (isProtectedRun(state.run)) {
      setMessage(el.globalStatus, '内置结果为只读，不能删除、重命名或重新评分。', 'warn');
      return;
    }
    if (isRunActive(state.run)) {
      setMessage(el.globalStatus, '运行或评分中的评测不能重命名或修改备注。', 'error');
      return;
    }
    var currentName = state.run.name || state.run.id;
    var name = window.prompt('新的评测名称', currentName);
    if (name === null) return;
    name = String(name).trim();
    if (!name) {
      setMessage(el.globalStatus, '评测名称不能为空。', 'error');
      return;
    }
    var currentNotes = state.run.notes || '';
    var notes = window.prompt('新的备注（可留空）', currentNotes);
    if (notes === null) return;
    beginBusy();
    api('api/runs/' + encodeURIComponent(state.run.id), {
      method: 'PATCH',
      body: JSON.stringify({ name: name, notes: String(notes) })
    })
      .then(function (run) {
        state.run = run;
        renderRun();
        return refreshRuns(run.id);
      })
      .then(function () { setMessage(el.globalStatus, '已更新评测名称和备注。', 'ok'); })
      .catch(function (err) { setMessage(el.globalStatus, err.message, 'error'); })
      .finally(function () { endBusy(); });
  }

  function deleteRun() {
    if (state.status && state.status.read_only) {
      setMessage(el.globalStatus, '当前为只读浏览模式，不能删除评测。', 'warn');
      return;
    }
    if (!state.run) return;
    if (isProtectedRun(state.run)) {
      setMessage(el.globalStatus, '内置结果为只读，不能删除、重命名或重新评分。', 'warn');
      return;
    }
    if (isRunActive(state.run)) {
      setMessage(el.globalStatus, '运行或评分中的评测不能删除。', 'error');
      return;
    }
    var label = state.run.name || state.run.id;
    var ok = window.confirm('确定永久删除评测“' + label + '”吗？请先下载 JSON 备份；删除后不能在本页面恢复。');
    if (!ok) return;
    var deletedId = state.run.id;
    beginBusy();
    api('api/runs/' + encodeURIComponent(deletedId), { method: 'DELETE' })
      .then(function () { return refreshRuns(); })
      .then(function () {
        var next = findBaselineRun() || state.runs[0];
        if (next) return loadRun(next.id, true);
        state.run = null;
        state.casePanels = {};
        clearSelectedRunUrl();
        renderRunSelector();
        renderRun();
        return null;
      })
      .then(function () { setMessage(el.globalStatus, '已删除评测。', 'ok'); })
      .catch(function (err) { setMessage(el.globalStatus, err.message, 'error'); })
      .finally(function () { endBusy(); });
  }

  function renderRun() {
    var run = state.run;
    updateControls();
    if (!run) {
      if (el.downloadRun) {
        el.downloadRun.classList.add('disabled');
        el.downloadRun.removeAttribute('href');
        el.downloadRun.removeAttribute('download');
      }
      setText(el.runMeta, '未选择评测。');
      renderBaselineInfo(null);
      renderStats(null);
      renderTaskRows(null);
      renderDomainRows(null);
      renderProgress(null);
      clearSelectedRunUrl();
      return;
    }
    if (el.downloadRun) {
      el.downloadRun.classList.remove('disabled');
      el.downloadRun.href = runDownloadHref(run.id);
      el.downloadRun.download = (run.id || 'hangauge-run') + '.json';
    }
    history.replaceState(null, '', '?run=' + encodeURIComponent(run.id));
    renderBaselineInfo(run);
    setText(el.runMeta, buildRunMeta(run));
    renderStats(run);
    renderTaskRows(run);
    renderDomainRows(run);
    renderProgress(run);
    refreshOpenPanelsIfNeeded(run);
  }

  function buildRunMeta(run) {
    var parts = [];
    var counts = assessmentCounts(run);
    parts.push('当前评测：' + (run.name || run.id));
    if (isBaselineRun(run)) parts.push('参考基线，只读');
    else if (isProtectedRun(run)) parts.push('内置结果，只读');
    parts.push('已回答：' + formatCount(counts && counts.answered, counts && counts.cases));
    parts.push('已评分：' + formatCount(counts && counts.graded, counts && counts.answered));
    if (run.test) {
      parts.push('测试状态：' + formatTestStatus(run.test.status));
      if (isGradingActive(run)) parts.push('阶段：评分中');
      var targetTotal = testTargetCount(run);
      if (targetTotal) parts.push('本次目标：' + targetTotal + '/' + datasetTotal());
      if (run.test.current_item) parts.push('当前题目：' + run.test.current_item);
      if (run.test.error) parts.push('测试错误：' + run.test.error);
    }
    if (run.grading) {
      parts.push('评分状态：' + formatGradingStatus(run.grading.status));
      if (isFiniteNumber(run.grading.completed) || isFiniteNumber(run.grading.total)) {
        parts.push('评分进度：' + numberOrZero(run.grading.completed) + '/' + numberOrZero(run.grading.total));
      }
      var gradingError = run.grading.error || run.grading.current_error;
      if (gradingError) parts.push('评分错误：' + gradingError);
    }
    if (run.provider) parts.push('候选模型：' + formatProvider(run.provider));
    if (run.grading_provider) parts.push('评分模型：' + formatProvider(run.grading_provider));
    if (run.notes) parts.push('备注：' + run.notes);
    if (run.updated_at) parts.push('更新时间：' + run.updated_at);
    return parts.join(' ｜ ');
  }

  function formatProvider(provider) {
    if (!provider) return '未记录';
    var parts = [];
    if (provider.model) parts.push(provider.model);
    if (provider.base_url && !(state.status && state.status.read_only)) parts.push(provider.base_url);
    if (provider.scope) parts.push('范围 ' + provider.scope);
    if (provider.temperature !== undefined) parts.push('temperature ' + provider.temperature);
    if (provider.max_tokens !== undefined) parts.push('max_tokens ' + provider.max_tokens);
    if (provider.enable_thinking === false) parts.push('禁止思考');
    if (provider.enable_thinking === true) parts.push('启用思考');
    return parts.join('，') || '—';
  }

  function renderStats(run) {
    var counts = assessmentCounts(run);
    setText(el.statCases, run ? formatInteger(counts && counts.cases) : '—');
    setText(el.statAnswered, run ? formatInteger(counts && counts.answered) : '—');
    setText(el.statGraded, run ? formatInteger(counts && counts.graded) : '—');
    setText(el.statGradeStatus, run ? compactGradeStatus(run, counts) : '—');
    var overall = run && run.assessment && run.assessment.overall_score;
    var displayOverall = isBaselineRun(run) && !isFiniteNumber(overall) ? 100 : overall;
    setText(el.overallScoreLabel, isBaselineRun(run) ? '综合参考分' : '综合得分');
    setText(el.overallScore, isFiniteNumber(displayOverall) ? formatNumber(displayOverall) : '—');
    var note = '未选择评测。';
    if (run) {
      if (isBaselineRun(run)) {
        note = '参考基线固定为100，只表示对照答案，不是竞争模型成绩。';
      } else if (isFiniteNumber(overall)) {
        note = (isProtectedRun(run) ? '内置只读结果：' : '') + '全部 ' + countValue(counts && counts.cases) + ' 题已评分。';
      } else {
        note = '已评分 ' + formatCount(counts && counts.graded, counts && counts.cases) + ' 题，完成全部题目评分后显示综合得分。';
      }
    }
    setText(el.overallScoreNote, note);
    renderAbilities(run);
  }

  function renderAbilities(run) {
    setText(el.abilityRunName, run ? run.name || run.id : '未选择评测');
    clearNode(el.abilityScores);
    clearNode(el.abilityHighlights);
    if (!run || !state.dataset) return;
    var byTask = run.assessment && run.assessment.by_task || {};
    var tasks = state.dataset.tasks || [];
    var completed = [];
    tasks.forEach(function (task) {
      var result = byTask[task.id] || {};
      var hasScore = isFiniteNumber(result.score);
      var graded = countValue(result.graded);
      var cases = groupCases(task.id, result, 'task');
      var card = document.createElement('button');
      card.type = 'button';
      card.className = 'ability-card';
      card.dataset.taskId = task.id;
      card.setAttribute('aria-controls', taskPanelId(run, task.id));
      var name = document.createElement('span');
      name.className = 'ability-name';
      name.textContent = task.name || task.id;
      card.appendChild(name);
      var score = document.createElement('span');
      score.className = 'ability-score';
      score.textContent = hasScore ? formatNumber(result.score) : '—';
      var scale = document.createElement('span');
      scale.className = 'ability-scale';
      scale.textContent = ' / 100';
      score.appendChild(scale);
      card.appendChild(score);
      var track = document.createElement('span');
      track.className = 'ability-track';
      track.setAttribute('aria-hidden', 'true');
      var fill = document.createElement('span');
      fill.className = 'ability-fill';
      fill.style.width = (hasScore ? Number(result.score) : 0) + '%';
      track.appendChild(fill);
      card.appendChild(track);
      var detail = document.createElement('span');
      detail.className = 'ability-detail';
      var method = result.method || task.grading_method;
      if (isBaselineRun(run)) {
        detail.textContent = '参考基线 · 100仅作对照';
      } else if (!hasScore) {
        detail.textContent = '尚无评分 · 已回答 ' + countValue(result.answered) + '/' + cases + ' 题';
      } else if (method === 'direct') {
        detail.textContent = '判断一致 ' + Math.round(Number(result.score) * graded / 100) + '/' + graded + ' 题';
      } else {
        detail.textContent = '内容评分 · ' + graded + '/' + cases + ' 题';
      }
      if (hasScore && graded < cases) detail.textContent += ' · 部分样本';
      card.appendChild(detail);
      var action = document.createElement('span');
      action.className = 'ability-detail';
      action.textContent = '查看逐题得分与理由';
      card.appendChild(action);
      card.addEventListener('click', function () {
        var panel = casePanel(run, task.id);
        if (!panel || !panel.open) toggleTaskCases(task.id);
        var target = document.getElementById(taskPanelId(run, task.id));
        if (target) target.scrollIntoView({ block: 'start', inline: 'nearest' });
      });
      el.abilityScores.appendChild(card);
      if (hasScore && graded === cases) completed.push({ name: task.name || task.id, score: Number(result.score) });
    });
    var messages = [];
    if (isBaselineRun(run)) {
      messages.push('这是参考基线，100分表示对照答案，不是被测模型成绩。请选择其他评测查看模型得分。');
    } else if (completed.length === tasks.length && completed.length > 1) {
      completed.sort(function (a, b) { return b.score - a.score; });
      var describe = function (item) { return item.name + ' ' + formatScore(item.score); };
      messages.push('本轮得分较高：' + completed.slice(0, 3).map(describe).join('；') + '。');
      messages.push('本轮得分较低：' + completed.slice(-3).reverse().map(describe).join('；') + '。');
    } else {
      messages.push('已完成 ' + completed.length + '/' + tasks.length + ' 项能力评分。部分样本仅显示已评分题目的均分，不能视为完整能力结果。');
    }
    messages.forEach(function (text) {
      var line = document.createElement('p');
      line.textContent = text;
      el.abilityHighlights.appendChild(line);
    });
  }

  function renderComparison() {
    var runs = comparisonRuns();
    renderComparisonCards(runs);
    renderComparisonTable(el.comparisonTaskHead, el.comparisonTaskRows, state.dataset && state.dataset.tasks, runs, 'by_task');
    renderComparisonTable(el.comparisonDomainHead, el.comparisonDomainRows, state.dataset && state.dataset.domains, runs, 'by_domain');
  }

  function comparisonRuns() {
    if (!Array.isArray(state.runs)) return [];
    var ids = state.status && Array.isArray(state.status.bundled_run_ids) ? state.status.bundled_run_ids : [];
    var picked = [];
    ids.forEach(function (id) {
      var run = state.runs.find(function (item) { return item.id === id; });
      if (run) picked.push(run);
    });
    if (!picked.length) {
      picked = state.runs.filter(function (run) { return isProtectedRun(run) || isBaselineRun(run); });
    }
    return picked.slice().sort(function (a, b) {
      if (isBaselineRun(a) !== isBaselineRun(b)) return isBaselineRun(a) ? 1 : -1;
      return comparisonName(a).localeCompare(comparisonName(b), 'zh-CN');
    });
  }

  function renderComparisonCards(runs) {
    clearNode(el.comparisonSummary);
    if (!el.comparisonSummary) return;
    if (!runs.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '暂无内置评测可对比。';
      el.comparisonSummary.appendChild(empty);
      return;
    }
    runs.forEach(function (run) {
      var card = document.createElement('article');
      card.className = 'comparison-card';
      var title = document.createElement('h3');
      title.textContent = comparisonName(run);
      card.appendChild(title);
      var value = document.createElement('div');
      value.className = 'comparison-score';
      value.textContent = comparisonScoreLabel(run);
      card.appendChild(value);
      var note = document.createElement('p');
      note.className = 'hint';
      note.textContent = isBaselineRun(run)
        ? '参考基线，只作对照'
        : '本轮模型实测得分';
      card.appendChild(note);
      var meta = document.createElement('p');
      meta.className = 'hint';
      meta.textContent = '候选：' + formatProvider(run.provider) + '；评分：' + graderLabel(run);
      card.appendChild(meta);
      var actions = document.createElement('div');
      actions.className = 'comparison-actions';
      var detail = document.createElement('button');
      detail.type = 'button';
      detail.className = 'secondary small';
      detail.textContent = '查看详情';
      detail.addEventListener('click', function () { loadRun(run.id); });
      actions.appendChild(detail);
      var link = document.createElement('a');
      link.className = 'button-link secondary small';
      link.href = '?run=' + encodeURIComponent(run.id);
      link.textContent = '打开详情链接';
      actions.appendChild(link);
      card.appendChild(actions);
      el.comparisonSummary.appendChild(card);
    });
    var warning = comparisonGraderWarning(runs);
    if (warning) {
      var line = document.createElement('p');
      line.className = 'hint warn';
      line.style.gridColumn = '1 / -1';
      line.textContent = warning;
      el.comparisonSummary.appendChild(line);
    }
  }

  function renderComparisonTable(head, body, groups, runs, assessmentKey) {
    if (!head || !body) return;
    clearNode(head);
    clearNode(body);
    var tr = document.createElement('tr');
    appendHeaderCell(tr, assessmentKey === 'by_task' ? '能力' : '领域');
    runs.forEach(function (run) { appendHeaderCell(tr, comparisonName(run)); });
    head.appendChild(tr);
    if (!Array.isArray(groups) || !groups.length || !runs.length) {
      appendEmptyRow(body, Math.max(1, runs.length + 1));
      return;
    }
    groups.forEach(function (group) {
      var row = document.createElement('tr');
      appendCell(row, group.name || group.id);
      runs.forEach(function (run) {
        var result = run.assessment && run.assessment[assessmentKey] && run.assessment[assessmentKey][group.id];
        var score = result && result.score;
        var text = isBaselineRun(run) ? baselineComparisonScore(score) : formatScore(score);
        appendCell(row, text);
        row.lastElementChild.className = 'task-score';
      });
      body.appendChild(row);
    });
  }

  function comparisonScoreLabel(run) {
    var score = run && run.assessment && run.assessment.overall_score;
    if (isBaselineRun(run)) return '100 参考';
    return isFiniteNumber(score) ? formatNumber(score) + ' 分' : '—';
  }

  function baselineComparisonScore(score) {
    return isFiniteNumber(score) ? formatNumber(score) + ' 参考' : '100 参考';
  }

  function comparisonName(run) {
    if (!run) return '—';
    if (isBaselineRun(run)) return '参考基线：' + (run.name || run.id);
    return run.name || run.id;
  }

  function graderLabel(run) {
    if (isBaselineRun(run)) return '参考基线';
    if (run && run.grading_provider) return formatProvider(run.grading_provider);
    var models = {};
    var evaluations = run && run.evaluations;
    if (evaluations && typeof evaluations === 'object') {
      Object.keys(evaluations).forEach(function (key) {
        var item = evaluations[key];
        if (item && item.grader_model) models[item.grader_model] = true;
      });
    }
    var names = Object.keys(models);
    if (names.length === 1) return names[0];
    if (names.length > 1) return names.join('，');
    return '评分配置未记录';
  }

  function comparisonGraderWarning(runs) {
    var labels = {};
    var missing = false;
    runs.forEach(function (run) {
      if (isBaselineRun(run)) return;
      var label = graderLabel(run);
      if (label === '评分配置未记录') missing = true;
      else labels[label] = true;
    });
    var known = Object.keys(labels);
    if (known.length > 1) return '注意：内置结果的评分配置不同，横向对比应同时查看逐题理由。';
    if (missing) return '注意：部分历史内置结果未记录完整评分配置，横向对比应同时查看逐题理由。';
    return '';
  }

  function renderTaskRows(run) {
    var tbody = el.taskRows;
    if (!tbody) return;
    clearNode(tbody);
    var tasks = state.dataset && Array.isArray(state.dataset.tasks) ? state.dataset.tasks : [];
    if (!tasks.length) {
      appendEmptyRow(tbody, 8);
      return;
    }
    var byTask = run && run.assessment && run.assessment.by_task;
    tasks.forEach(function (task) {
      var counts = byTask && byTask[task.id];
      var cases = groupCases(task.id, counts, 'task');
      var answered = countValue(counts && counts.answered);
      var graded = countValue(counts && counts.graded);
      var panel = casePanel(run, task.id);
      var tr = document.createElement('tr');
      appendExpandCell(tr, run, task, panel);
      appendCell(tr, task.name || task.id);
      appendCell(tr, formatScore(counts && counts.score));
      tr.lastElementChild.className = 'task-score';
      appendCell(tr, formatInteger(cases));
      appendCell(tr, formatInteger(answered));
      appendCell(tr, formatInteger(graded));
      appendCell(tr, formatMethod(counts && counts.method));
      appendStatusCell(tr, assessmentStatus(run, counts, cases, answered, graded));
      tbody.appendChild(tr);
      if (panel && panel.open) appendCasePanelRow(tbody, run, task, panel, 8);
    });
  }

  function renderDomainRows(run) {
    var tbody = el.domainRows;
    if (!tbody) return;
    clearNode(tbody);
    clearNode(el.domainScores);
    var domains = state.dataset && Array.isArray(state.dataset.domains) ? state.dataset.domains : [];
    if (!domains.length) {
      appendEmptyRow(tbody, 7);
      return;
    }
    var byDomain = run && run.assessment && run.assessment.by_domain;
    domains.forEach(function (domain) {
      var counts = byDomain && byDomain[domain.id];
      var cases = groupCases(domain.id, counts, 'domain');
      var answered = countValue(counts && counts.answered);
      var graded = countValue(counts && counts.graded);
      var value = counts && counts.score;
      var card = document.createElement('div');
      card.className = 'domain-card';
      card.dataset.domainId = domain.id;
      var title = document.createElement('h3');
      title.textContent = domain.name || domain.id;
      card.appendChild(title);
      var score = document.createElement('div');
      score.className = 'ability-score';
      score.textContent = isFiniteNumber(value) ? formatNumber(value) : '—';
      var scale = document.createElement('span');
      scale.className = 'ability-scale';
      scale.textContent = ' / 100';
      score.appendChild(scale);
      card.appendChild(score);
      var detail = document.createElement('p');
      detail.className = 'hint';
      detail.textContent = isBaselineRun(run)
        ? '参考基线 · 100仅作对照'
        : '已评分 ' + graded + '/' + cases + ' 题' + (isFiniteNumber(value) ? '' : ' · 待完成');
      card.appendChild(detail);
      el.domainScores.appendChild(card);
      var tr = document.createElement('tr');
      appendCell(tr, domain.name || domain.id);
      appendCell(tr, formatScore(value));
      tr.lastElementChild.className = 'task-score';
      appendCell(tr, formatInteger(cases));
      appendCell(tr, formatInteger(answered));
      appendCell(tr, formatInteger(Math.max(numberOrZero(cases) - numberOrZero(answered), 0)));
      appendCell(tr, formatInteger(graded));
      appendStatusCell(tr, assessmentStatus(run, counts, cases, answered, graded));
      tbody.appendChild(tr);
    });
  }

  function appendExpandCell(tr, run, task, panel) {
    var td = document.createElement('td');
    var button = document.createElement('button');
    var panelId = taskPanelId(run, task.id);
    button.type = 'button';
    button.className = 'secondary small';
    button.textContent = panel && panel.open ? '收起' : '展开';
    button.disabled = !run;
    button.setAttribute('aria-expanded', panel && panel.open ? 'true' : 'false');
    button.setAttribute('aria-controls', panelId);
    button.addEventListener('click', function () { toggleTaskCases(task.id); });
    td.appendChild(button);
    tr.appendChild(td);
  }

  function appendCasePanelRow(tbody, run, task, panel, colSpan) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = colSpan;
    var container = document.createElement('div');
    container.id = taskPanelId(run, task.id);
    container.className = 'case-panel';
    if (panel.loading) {
      var loading = document.createElement('p');
      loading.className = 'hint';
      loading.textContent = '正在加载逐题对照…';
      container.appendChild(loading);
    } else if (panel.error) {
      var error = document.createElement('p');
      error.className = 'hint error';
      error.textContent = panel.error;
      container.appendChild(error);
    } else if (panel.data && Array.isArray(panel.data.cases)) {
      appendCases(container, panel.data.cases);
    } else {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '展开后加载逐题对照。';
      container.appendChild(empty);
    }
    td.appendChild(container);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function appendCases(parent, cases) {
    if (!cases.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '暂无逐题数据。';
      parent.appendChild(empty);
      return;
    }
    var list = document.createElement('div');
    list.className = 'case-list';
    cases.forEach(function (item) {
      var card = document.createElement('article');
      card.className = 'case-card';
      var title = document.createElement('h3');
      title.textContent = item.item_id || '未命名题目';
      card.appendChild(title);
      var grid = document.createElement('div');
      grid.className = 'case-grid';
      appendCaseBlock(grid, '题目/指令', primaryPrompt(item.input), true);
      appendCaseBlock(grid, '材料', formatMaterials(item.input), true);
      appendCaseBlock(grid, '参考基线回答', item.baseline_raw || '—', false);
      appendCaseBlock(grid, '当前模型回答', item.model_raw || '未回答', false);
      appendCaseBlock(grid, '评分', formatEvaluation(item.evaluation, item.grading_method), false);
      appendCaseBlock(grid, '评分说明', item.evaluation && item.evaluation.reason ? item.evaluation.reason : '未评分', false);
      card.appendChild(grid);
      list.appendChild(card);
    });
    parent.appendChild(list);
  }

  function appendCaseBlock(parent, label, value, full) {
    var block = document.createElement('div');
    block.className = 'case-block' + (full ? ' full' : '');
    var labelNode = document.createElement('div');
    labelNode.className = 'label';
    labelNode.textContent = label;
    var pre = document.createElement('pre');
    pre.className = 'text-box';
    pre.textContent = value == null || value === '' ? '—' : String(value);
    block.appendChild(labelNode);
    block.appendChild(pre);
    parent.appendChild(block);
  }

  function toggleTaskCases(taskId) {
    if (!state.run || !taskId) return;
    var key = caseKey(state.run.id, taskId);
    var panel = state.casePanels[key] || { open: false, loading: false, error: '', data: null, seq: 0, signature: '' };
    panel.open = !panel.open;
    state.casePanels[key] = panel;
    renderRun();
    if (panel.open && !panel.data && !panel.loading) fetchTaskCases(taskId, false);
  }

  function fetchTaskCases(taskId, refresh) {
    if (!state.run || !taskId) return;
    var runId = state.run.id;
    var requestedSignature = casesSignature(state.run);
    var key = caseKey(runId, taskId);
    var panel = state.casePanels[key] || { open: true, loading: false, error: '', data: null, seq: 0, signature: '' };
    if (panel.loading) return;
    panel.open = true;
    panel.loading = true;
    panel.error = '';
    panel.seq = ++state.caseSeq;
    state.casePanels[key] = panel;
    if (!refresh) renderTaskRows(state.run);
    api('api/runs/' + encodeURIComponent(runId) + '/cases?task_id=' + encodeURIComponent(taskId))
      .then(function (data) {
        var current = state.casePanels[key];
        if (!current || current.seq !== panel.seq || !state.run || state.run.id !== runId) return;
        current.loading = false;
        current.error = '';
        current.data = data || { cases: [] };
        current.signature = requestedSignature;
        renderTaskRows(state.run);
        refreshOpenPanelsIfNeeded(state.run);
      })
      .catch(function (err) {
        var current = state.casePanels[key];
        if (!current || current.seq !== panel.seq || !state.run || state.run.id !== runId) return;
        current.loading = false;
        current.error = err.message;
        renderTaskRows(state.run);
      });
  }

  function refreshOpenPanelsIfNeeded(run) {
    if (!run) return;
    var signature = casesSignature(run);
    Object.keys(state.casePanels).forEach(function (key) {
      var panel = state.casePanels[key];
      if (!panel || !panel.open || panel.loading) return;
      var parts = key.split('::');
      if (parts[0] !== run.id) return;
      if (panel.signature !== signature) fetchTaskCases(parts[1], true);
    });
  }

  function renderBaselineInfo(run) {
    if (!el.baselineInfo) return;
    if (!run) {
      el.baselineInfo.textContent = '';
      el.baselineInfo.style.display = 'none';
      return;
    }
    var baselineId = state.status && state.status.baseline_run_id;
    var baselineName = state.status && state.status.baseline_name;
    var parts = [];
    if (baselineId) {
      parts.push('参考基线：' + (baselineName || baselineId));
      parts.push('ID：' + baselineId);
      parts.push('100分只表示对照答案，不是竞争模型成绩');
    }
    if (isBaselineRun(run)) parts.push('当前选择的是参考基线，只读保留');
    else if (isProtectedRun(run)) parts.push('当前选择的是内置只读结果，显示真实assessment得分');
    el.baselineInfo.textContent = parts.join(' ｜ ');
    el.baselineInfo.style.display = parts.length ? '' : 'none';
  }

  function renderProgress(run) {
    if (!el.testProgress && !el.testStatus) return;
    if (!run || (!run.test && !run.grading)) {
      if (el.testProgress) {
        el.testProgress.max = datasetTotal() || 1;
        el.testProgress.value = 0;
      }
      if (el.testStatus && !valueOf(el.testStatus)) renderTestHint();
      return;
    }
    var counts = assessmentCounts(run);
    var target = testTargetCount(run) || numberOrZero(counts && counts.cases) || datasetTotal() || 1;
    var captured = numberOrZero(counts && counts.answered);
    var graded = numberOrZero(counts && counts.graded);
    var gradingTotal = run.grading && isFiniteNumber(run.grading.total) ? Number(run.grading.total) : captured;
    var gradingDone = run.grading && isFiniteNumber(run.grading.completed) ? Number(run.grading.completed) : graded;
    if (el.testProgress) {
      el.testProgress.max = Math.max(target, gradingTotal, 1);
      el.testProgress.value = isGradingActive(run) ? Math.min(gradingDone, gradingTotal || target) : Math.min(captured, target);
    }
    var message = '';
    var kind = 'ok';
    if (run.test) {
      message = formatTestStatus(run.test.status) + '：已回答 ' + captured + '/' + target;
      var attempted = testAttemptedCount(run);
      if (attempted) message += '，已尝试 ' + attempted;
      if (target < datasetTotal()) message += '（部分范围；全量 ' + datasetTotal() + ' 题）';
      if (run.test.current_item && RUNNING_TEST_STATUSES[run.test.status]) message += '，当前 ' + run.test.current_item;
      if (isGradingActive(run)) message += '；正在评分，不能停止';
      else if (run.test.status === 'stopping') message += '；停止会在当前 HTTP 请求结束后生效';
      if (run.test.error) message += '；测试错误：' + run.test.error;
      kind = RUNNING_TEST_STATUSES[run.test.status] ? 'warn' : (run.test.status === 'completed' || run.test.status === 'stopped' ? 'ok' : 'error');
    }
    if (run.grading) {
      var gradingMessage = '评分' + formatGradingStatus(run.grading.status) + '：已评分 ' + gradingDone + '/' + gradingTotal;
      var gradingError = run.grading.error || run.grading.current_error;
      if (gradingError) gradingMessage += '；评分错误：' + gradingError;
      message = message ? (message + ' ｜ ' + gradingMessage) : gradingMessage;
      if (run.grading.status === 'running') kind = 'warn';
      else if (run.grading.status === 'failed' || run.grading.status === 'interrupted') kind = 'error';
    }
    setMessage(el.testStatus, message, kind);
  }

  function renderStorageInfo() {
    if (!el.storageInfo) return;
    if (state.status.read_only) {
      el.storageInfo.textContent = '只读浏览模式：可以查看、下载和对比已发布记录，不能创建、评分、修改或删除。';
      return;
    }
    var text = state.status.storage_path
      ? '结果保存到服务器磁盘：' + state.status.storage_path + '。刷新或重启服务仍保留；不是浏览器缓存。删除前请下载JSON备份。'
      : '结果保存到本地服务配置的结果目录。删除前请下载JSON备份。';
    el.storageInfo.textContent = text;
  }

  function renderTestHint() {
    if (!el.testStatus) return;
    if (state.status.read_only) {
      setMessage(el.testStatus, '只读浏览模式已隐藏运行与设置控件。');
      return;
    }
    if (state.run && (state.run.test || state.run.grading)) return;
    var scope = valueOf(el.testScope) || 'all';
    var hint = SCOPE_LABELS[scope] || SCOPE_LABELS.all;
    setMessage(el.testStatus, '测试范围：' + hint + '。每次运行都会创建新的已保存评测；API Key 只用于本次请求，不保存。', scope === 'all' ? 'warn' : '');
  }

  function updateControls() {
    var busy = isBusy();
    var hasRun = !!state.run;
    var readOnly = !!(state.status && state.status.read_only);
    var selectedActive = isSelectedRunActive();
    var gradingActive = isGradingActive(state.run);
    var protectedRun = isProtectedRun(state.run);
    var anyActive = !!(state.status && state.status.active_run_id);
    if (el.runSelect) el.runSelect.disabled = busy;
    if (el.saveSettings) el.saveSettings.disabled = readOnly || busy;
    if (el.runTest) el.runTest.disabled = readOnly || busy || state.launching || anyActive;
    if (el.stopTest) {
      el.stopTest.disabled = readOnly || busy || !selectedActive || gradingActive;
      el.stopTest.title = gradingActive ? '评分阶段不能停止；已保存回答和评分进度会保留。' : '';
    }
    if (el.renameRun) {
      el.renameRun.disabled = readOnly || busy || !hasRun || selectedActive || protectedRun;
      el.renameRun.style.display = (readOnly || protectedRun) ? 'none' : '';
    }
    if (el.gradeRun) {
      var gradeReason = gradeDisabledReason(state.run);
      el.gradeRun.disabled = readOnly || busy || state.grading || !!gradeReason;
      el.gradeRun.title = readOnly ? '只读模式不能启动评分。' : (gradeReason || '');
      el.gradeRun.style.display = (readOnly || protectedRun) ? 'none' : '';
    }
    if (el.deleteRun) {
      el.deleteRun.disabled = readOnly || busy || !hasRun || selectedActive || protectedRun;
      el.deleteRun.style.display = (readOnly || protectedRun) ? 'none' : '';
    }
    if (el.downloadRun) {
      if (!hasRun) el.downloadRun.classList.add('disabled');
      else el.downloadRun.classList.remove('disabled');
    }
  }

  function schedulePollingIfNeeded(force) {
    if (state.unloading || (state.status && state.status.read_only)) return;
    clearPollTimer();
    var active = !!(state.status && state.status.active_run_id);
    if (!active && state.run && isRunActive(state.run)) active = true;
    if (!active && !force) return;
    state.pollTimer = window.setTimeout(pollOnce, POLL_MS);
  }

  function pollOnce() {
    if (state.unloading) return;
    if (isBusy()) {
      schedulePollingIfNeeded();
      return;
    }
    clearPollTimer();
    refreshStatus()
      .then(function (status) {
        if (isBusy()) return status.active_run_id;
        var activeId = status.active_run_id;
        var selectedId = state.run && state.run.id;
        var jobs = [refreshRuns()];
        if (selectedId && (activeId === selectedId || isRunActive(state.run))) {
          jobs.push(loadRun(selectedId, true));
        }
        return Promise.all(jobs).then(function () { return activeId; });
      })
      .then(function (activeId) {
        if (!activeId && state.run && (isTerminalTest(state.run) || isTerminalGrading(state.run))) {
          renderRun();
        }
        schedulePollingIfNeeded();
      })
      .catch(function (err) {
        setMessage(el.testStatus || el.globalStatus, err.message, 'error');
        schedulePollingIfNeeded();
      });
  }

  function clearPollTimer() {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function gradeDisabledReason(run) {
    if (!run) return '请先选择评测。';
    if (isBaselineRun(run)) return '参考基线只读，不需要重新评分。';
    if (isProtectedRun(run)) return '内置结果只读，不能重新评分。';
    if (isRunActive(run)) return isGradingActive(run) ? '当前评分正在运行。' : '当前测试正在运行，结束后才能评分。';
    var counts = assessmentCounts(run);
    var answered = countValue(counts && counts.answered);
    var graded = countValue(counts && counts.graded);
    if (!answered) return '当前结果没有已保存回答。';
    if (graded >= answered && gradingStatus(run) === 'completed') return '当前回答已经完成评分。';
    if (graded >= answered && !run.grading) return '当前回答已经完成评分。';
    return '';
  }

  function assessmentStatus(run, counts, cases, answered, graded) {
    if (!run || !counts) return { text: '无评分数据', cls: 'warn' };
    if (isBaselineRun(run)) return { text: '参考基线', cls: 'ok' };
    if (!answered) return { text: '未回答', cls: 'warn' };
    if (graded < answered) return { text: isGradingActive(run) ? '评分中' : '未完成评分', cls: 'warn' };
    if (answered < cases) return { text: '部分完成', cls: 'warn' };
    return { text: isProtectedRun(run) ? '内置只读' : '已评分', cls: 'ok' };
  }

  function compactGradeStatus(run, counts) {
    if (isBaselineRun(run)) return '参考';
    if (isProtectedRun(run)) return '只读';
    var status = gradingStatus(run);
    if (status && status !== 'not_started') return formatGradingStatus(status);
    var answered = countValue(counts && counts.answered);
    var graded = countValue(counts && counts.graded);
    if (!answered) return '无回答';
    if (!graded) return '未评分';
    if (graded < answered) return '部分评分';
    return '已评分';
  }

  function gradingStatus(run) {
    return run && run.grading && run.grading.status ? run.grading.status : '';
  }

  function isSelectedRunActive() {
    return state.run && isRunActive(state.run);
  }

  function isRunActive(run) {
    if (!run) return false;
    if (state.status && state.status.active_run_id && state.status.active_run_id === run.id) return true;
    if (run.test && RUNNING_TEST_STATUSES[run.test.status]) return true;
    return isGradingActive(run);
  }

  function isGradingActive(run) {
    if (!run) return false;
    if (run.grading && run.grading.status === 'running') return true;
    return !!(run.test && run.test.phase === 'grading' && RUNNING_TEST_STATUSES[run.test.status]);
  }

  function isTerminalTest(run) {
    return !!(run && run.test && TERMINAL_TEST_STATUSES[run.test.status]);
  }

  function isTerminalGrading(run) {
    var status = gradingStatus(run);
    return status === 'completed' || status === 'failed' || status === 'interrupted';
  }

  function isBaselineRun(run) {
    if (!run) return false;
    if (run.is_baseline) return true;
    return !!(state.status && state.status.baseline_run_id && run.id === state.status.baseline_run_id);
  }

  function isProtectedRun(run) {
    if (!run) return false;
    if (run.protected || run.is_baseline) return true;
    if (state.status && Array.isArray(state.status.bundled_run_ids) && state.status.bundled_run_ids.indexOf(run.id) !== -1) return true;
    return isBaselineRun(run);
  }

  function testTargetCount(run) {
    var ids = run && run.test && run.test.target_ids;
    return Array.isArray(ids) ? ids.length : 0;
  }

  function testAttemptedCount(run) {
    var ids = run && run.test && run.test.attempted_ids;
    if (Array.isArray(ids)) return ids.length;
    return run && run.test ? countValue(assessmentCounts(run) && assessmentCounts(run).answered) : 0;
  }

  function assessmentCounts(run) {
    return run && run.assessment && run.assessment.counts ? run.assessment.counts : null;
  }

  function countValue(value) {
    return isFiniteNumber(value) ? Number(value) : 0;
  }

  function groupCases(id, counts, kind) {
    if (counts && isFiniteNumber(counts.cases)) return Number(counts.cases);
    return estimateGroupCases(id, kind);
  }

  function estimateGroupCases(id, kind) {
    if (!state.dataset || !Array.isArray(state.dataset.cases)) return 0;
    var total = 0;
    state.dataset.cases.forEach(function (item) {
      var input = item && item.input;
      if (!input) return;
      var value = kind === 'task' ? input.task_id : input.domain_id;
      if (value === id) total += 1;
    });
    return total;
  }

  function primaryPrompt(input) {
    if (!input || typeof input !== 'object') return '—';
    var config = input.task_config;
    var question = input.question || input.prompt || input.query || (config && (config.question || config.query));
    var requirements = config ? '任务要求：\n' + JSON.stringify(config, null, 2) : '';
    return [question, input.instruction, requirements].filter(Boolean).join('\n\n') || JSON.stringify(input, null, 2);
  }

  function formatMaterials(input) {
    if (!input || typeof input !== 'object') return '—';
    var parts = [];
    var docs = input.documents || input.materials || input.passages || input.texts;
    if (Array.isArray(docs)) {
      docs.forEach(function (doc, index) {
        if (doc && typeof doc === 'object') {
          var label = doc.doc_id || doc.id || ('材料' + (index + 1));
          var text = doc.text || doc.content || JSON.stringify(doc, null, 2);
          parts.push(label + '：' + text);
        } else {
          parts.push('材料' + (index + 1) + '：' + String(doc));
        }
      });
    }
    if (!parts.length && input.material) parts.push(String(input.material));
    if (!parts.length && input.text) parts.push(String(input.text));
    if (!parts.length && input.context) parts.push(String(input.context));
    return parts.length ? parts.join('\n\n') : '—';
  }

  function formatEvaluation(evaluation, fallbackMethod) {
    if (!evaluation) return '未评分';
    var parts = [];
    parts.push('分数：' + formatScore(evaluation.score));
    parts.push('方式：' + formatMethod(evaluation.method || fallbackMethod));
    if (evaluation.grading_version) parts.push('版本：' + evaluation.grading_version);
    return parts.join('\n');
  }

  function formatTestStatus(status) {
    return ({
      running: '运行中',
      stopping: '停止中',
      completed: '已完成',
      stopped: '已停止',
      failed: '失败',
      interrupted: '已中断'
    })[status] || (status || '未运行');
  }

  function formatGradingStatus(status) {
    return ({
      confirmed: '已确认',
      not_started: '未开始',
      running: '进行中',
      completed: '已完成',
      failed: '失败',
      interrupted: '已中断'
    })[status] || (status || '未开始');
  }

  function formatMethod(method) {
    return ({ direct: '直接判定', gateway: '标准模型评分', reference: '参考基线' })[method] || (method || '—');
  }

  function formatScore(value) {
    return isFiniteNumber(value) ? formatNumber(value) + ' 分' : '—';
  }

  function formatCount(value, total) {
    if (!isFiniteNumber(value)) return '—';
    return isFiniteNumber(total) ? (formatInteger(value) + '/' + formatInteger(total)) : formatInteger(value);
  }

  function appendEmptyRow(tbody, colSpan) {
    var tr = document.createElement('tr');
    var td = document.createElement('td');
    td.colSpan = colSpan;
    td.textContent = '暂无数据';
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function appendHeaderCell(tr, value) {
    var th = document.createElement('th');
    th.textContent = value == null || value === '' ? '—' : String(value);
    tr.appendChild(th);
  }

  function appendCell(tr, value) {
    var td = document.createElement('td');
    td.textContent = value == null || value === '' ? '—' : String(value);
    tr.appendChild(td);
  }

  function appendStatusCell(tr, status) {
    var td = document.createElement('td');
    var span = document.createElement('span');
    span.className = 'pill ' + status.cls;
    span.textContent = status.text;
    td.appendChild(span);
    tr.appendChild(td);
  }

  function casePanel(run, taskId) {
    if (!run) return null;
    return state.casePanels[caseKey(run.id, taskId)] || null;
  }

  function caseKey(runId, taskId) {
    return String(runId) + '::' + String(taskId);
  }

  function taskPanelId(run, taskId) {
    return 'cases-' + safeId(run && run.id) + '-' + safeId(taskId);
  }

  function safeId(value) {
    return String(value || '').replace(/[^A-Za-z0-9_-]/g, '-');
  }

  function casesSignature(run) {
    var counts = assessmentCounts(run) || {};
    var grading = run && run.grading || {};
    return [
      run && run.id,
      counts.answered,
      counts.graded,
      grading.status,
      grading.completed,
      grading.total,
      run && run.updated_at
    ].join('|');
  }

  function clearSelectedRunUrl() {
    if (location.search) history.replaceState(null, '', location.pathname);
  }

  function setInputValue(node, value) { if (node) node.value = value == null ? '' : String(value); }
  function valueOf(node) { return node && node.value != null ? String(node.value) : ''; }
  function setText(node, value) { if (node) node.textContent = value == null || value === '' ? '—' : String(value); }
  function clearNode(node) { if (node) while (node.firstChild) node.removeChild(node.firstChild); }
  function datasetTotal() { return numberOrZero(state.dataset && state.dataset.total); }
  function numberOrZero(value) { return isFiniteNumber(value) ? Number(value) : 0; }
  function isFiniteNumber(value) { return value !== null && value !== '' && Number.isFinite(Number(value)); }
  function formatInteger(value) { return isFiniteNumber(value) ? String(Math.round(Number(value))) : '—'; }
  function formatNumber(value) { return isFiniteNumber(value) ? Number(value).toFixed(2).replace(/0+$/, '').replace(/\.$/, '') : '—'; }

  function parseNumberInput(node, fallback) {
    var raw = valueOf(node).trim();
    if (!raw) return fallback;
    var value = Number(raw);
    if (!Number.isFinite(value)) throw new Error('Temperature必须是有限数值。');
    return value;
  }

  function parseIntegerInput(node, fallback) {
    var raw = valueOf(node).trim();
    if (!raw) return fallback;
    var value = Number(raw);
    if (!Number.isInteger(value)) throw new Error('最大输出Token数必须是整数。');
    return value;
  }
})();
