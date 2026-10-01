/* Production workstation controls. No prototype timers, queues or sample audio. */
window.VS_WORKSTATION = (() => {
  'use strict';
  let S, installed = false, draftKey = '', restoring = false, saveTimer;
  let cursor = -1, history = [], audioBound = null, pageObserver;
  const q = (selector, root) => (root || document).querySelector(selector);
  const all = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  const W = {selection: [0, 0], zoom: 1, loop: false, playerKind: '', playerPage: '', collapsed: false};
  const clock = ms => `${String(Math.floor(Math.max(0, ms) / 60000)).padStart(2, '0')}:${String(Math.floor(Math.max(0, ms) / 1000) % 60).padStart(2, '0')}`;
  const media = () => window.VS_MEDIA;
  const page = () => (q('[data-page-view]:not(.hidden)') || {}).dataset?.pageView || 'cover';
  const setText = (selector, value) => { const node = q(selector); if (node) node.textContent = value; };
  const supported = '#coverPitch,#rvcIndex,#rvcProtect,#rvcSmoothing,.mixer-body .mix-row input';
  const notWired = () => S.toast('该功能尚未接入新界面');

  function storageKey() {
    return 'vs.workstation.draft.v1:' + ((S.data && S.data.project && S.data.project.path) || '') + ':' + ((S.song && S.song.id) || '');
  }
  function snapshot() {
    const controls = {};
    all(supported).forEach((node, index) => { controls[node.id || 'mix-' + index] = node.value; });
    return {controls, voiceId: (window.VS_PAGES && window.VS_PAGES.cover.voiceId) || '', selection: [...W.selection], zoom: W.zoom, loop: W.loop};
  }
  function buttons() {
    const undo = q('#undoEdit'), redo = q('#redoEdit');
    if (undo) undo.disabled = cursor <= 0;
    if (redo) redo.disabled = cursor < 0 || cursor >= history.length - 1;
  }
  function persist(state) {
    if (!S.song) { setText('#autoSaveState', '尚未选择工程'); return; }
    try {
      localStorage.setItem(draftKey, JSON.stringify(state));
      setText('#autoSaveState', '参数已自动保存');
    } catch (_) { setText('#autoSaveState', '参数保存失败'); }
  }
  function commit() {
    if (!S || restoring) return;
    clearTimeout(saveTimer); saveTimer = null;
    const state = snapshot(), old = history[cursor];
    if (!old || JSON.stringify(old) !== JSON.stringify(state)) {
      history.splice(cursor + 1); history.push(state);
      if (history.length > 60) history.shift();
      cursor = history.length - 1;
    }
    persist(state); buttons();
  }
  function scheduleSave() {
    if (restoring || !S.song) return;
    setText('#autoSaveState', '正在保存参数…');
    clearTimeout(saveTimer); saveTimer = setTimeout(commit, 300);
  }
  function restore(state) {
    if (!state || !state.controls) return;
    restoring = true;
    all(supported).forEach((node, index) => {
      const value = state.controls[node.id || 'mix-' + index];
      if (value === undefined) return;
      node.value = value; node.dispatchEvent(new Event('input', {bubbles: true}));
    });
    const cover = window.VS_PAGES && window.VS_PAGES.cover;
    const voice = (S.voices || []).find(item => item.id === state.voiceId && item.cover_ready);
    if (cover) {
      cover.voiceId = voice ? voice.id : '';
      setText('.voice-card .voice-name', voice ? voice.name : '请选择目标声音');
      setText('.voice-card .voice-meta', voice ? voice.subtitle : '仅显示已授权且验证通过的模型');
    }
    const duration = media()?.state.duration || (S.song && S.song.duration_ms) || 0;
    W.selection = (state.selection || [0, 0]).map(value => Math.max(0, Math.min(duration, Number(value) || 0)));
    W.zoom = Math.max(1, Math.min(4, Number(state.zoom) || 1)); W.loop = !!state.loop;
    media()?.setLoop(W.loop);
    paintTimeline(); restoring = false;
  }
  function historyMove(delta) {
    if (delta < 0) commit();
    else { clearTimeout(saveTimer); saveTimer = null; }
    const next = cursor + delta;
    if (next < 0 || next >= history.length) return;
    cursor = next; restore(history[cursor]); persist(history[cursor]); buttons();
    if (window.VS_PAGES) window.VS_PAGES.cover.paint();
  }

  function installHistory() {
    const holder = q('.hero-actions');
    if (holder && !q('#undoEdit')) holder.insertAdjacentHTML('afterbegin',
      '<div class="history-tools"><button class="mini" id="undoEdit" aria-label="撤销参数 Ctrl+Z">↶</button>' +
      '<button class="mini" id="redoEdit" aria-label="重做参数 Ctrl+Y">↷</button><span id="autoSaveState" title="按工程与歌曲保存到本机前端草稿；包括移调、RVC、混音、试听选区。音频生成与删除不可撤销。">参数已自动保存</span></div>');
    if (q('#undoEdit')) q('#undoEdit').onclick = () => historyMove(-1);
    if (q('#redoEdit')) q('#redoEdit').onclick = () => historyMove(1);
    document.addEventListener('input', event => { if (event.target.matches(supported)) scheduleSave(); });
    document.addEventListener('change', event => { if (event.target.matches(supported)) scheduleSave(); });
    document.addEventListener('click', event => {
      if (event.target.closest('[data-preset],.mixer .subhead button')) scheduleSave();
    });
    document.addEventListener('keydown', event => {
      if (!(event.ctrlKey || event.metaKey) || page() !== 'cover' ||
          (event.target.closest && event.target.closest('textarea,input:not([type=range]),[contenteditable=true]'))) return;
      const key = event.key.toLowerCase();
      if (key === 'z' || key === 'y') { event.preventDefault(); historyMove(key === 'y' || event.shiftKey ? 1 : -1); }
    });
    const cover = window.VS_PAGES && window.VS_PAGES.cover;
    if (cover) {
      const pickVoice = cover.pickVoice;
      cover.pickVoice = async function (...args) {
        const result = await pickVoice.apply(this, args); if (result) scheduleSave(); return result;
      };
    }
  }

  function duration() { return media()?.state.duration || (S.song && S.song.duration_ms) || 0; }
  function paintRuler() {
    const tracks = q('#tracks'); if (!tracks) return;
    const row = q('#tracks .track'), wave = row && q('.wave-wrap', row);
    const rowWidth = row ? row.getBoundingClientRect().width : tracks.clientWidth;
    const waveWidth = wave ? wave.getBoundingClientRect().width : tracks.clientWidth;
    const hidden = Math.max(0, rowWidth - tracks.clientWidth);
    const full = duration();
    const view = waveWidth ? Math.min(full, full * (waveWidth - hidden) / waveWidth) : full;
    const offset = waveWidth ? tracks.scrollLeft / waveWidth * full : 0;
    all('#ruler .tick').forEach((node, index, nodes) => {
      node.style.left = index / Math.max(1, nodes.length - 1) * 100 + '%';
      node.textContent = clock(Math.min(full, offset + index / Math.max(1, nodes.length - 1) * Math.max(0, view)));
    });
  }
  function paintSelection() {
    const total = duration(), [a, b] = W.selection;
    all('.edit-selection').forEach(node => {
      node.style.left = (total ? a / total * 100 : 0) + '%';
      node.style.width = (total ? (b - a) / total * 100 : 0) + '%';
      node.style.display = b > a ? 'block' : 'none';
    });
    setText('#selectionLabel', b > a ? `选区 ${clock(a)}–${clock(b)} · ${((b - a) / 1000).toFixed(1)} 秒` : '拖动波形框选试听区域');
    const loop = q('#loopRegion');
    if (loop) { loop.classList.toggle('active', W.loop); loop.disabled = b <= a; loop.setAttribute('aria-pressed', String(W.loop)); }
    if (q('#clearSelection')) q('#clearSelection').disabled = b <= a;
  }
  function paintTimeline() {
    const tracks = q('#tracks'); if (!tracks) return;
    all('#tracks .track').forEach(node => { node.style.minWidth = Math.max(0, tracks.clientWidth - 20) * W.zoom + 'px'; });
    setText('#zoomReset', W.zoom + '×');
    paintSelection(); paintRuler();
    if (media()) media().draw();
    paintRuler(); // The waveform adapter refreshes ruler labels when it draws.
  }
  function previewSelection() {
    if (!S.song) { S.toast('请先选择一首歌'); return; }
    let [start, end] = W.selection;
    if (end <= start) {
      start = media()?.state.position || 0; end = Math.min(duration(), start + 20000);
      W.selection = [start, end]; paintSelection(); scheduleSave();
    }
    if (media()) media().playRange(start, end, W.loop);
  }
  function installTimeline() {
    const toolbar = q('.preview-toolbar');
    if (toolbar && !q('#zoomIn')) toolbar.insertAdjacentHTML('afterbegin',
      '<button class="mini" id="zoomOut" aria-label="缩小时间线">−</button><button class="mini" id="zoomReset">1×</button>' +
      '<button class="mini" id="zoomIn" aria-label="放大时间线">＋</button><button class="mini" id="loopRegion">循环</button>');
    if (toolbar && !q('#localGenerate')) toolbar.insertAdjacentHTML('beforeend', '<button class="mini" id="localGenerate" title="该功能尚未接入新界面">局部生成</button>');
    const ruler = q('.timeline-ruler');
    if (ruler && !q('#selectionLabel')) ruler.insertAdjacentHTML('afterend', '<div class="timeline-edit-state"><span id="selectionLabel"></span><button id="clearSelection">清除选区</button></div>');
    setText('.timeline .card-sub', '单击定位 · 拖动框选 · 支持循环试听');
    ['zoomIn', 'zoomOut', 'zoomReset'].forEach(id => {
      const node = q('#' + id); if (!node) return;
      node.onclick = () => { W.zoom = id === 'zoomReset' ? 1 : Math.max(1, Math.min(4, W.zoom + (id === 'zoomIn' ? .5 : -.5))); paintTimeline(); scheduleSave(); };
    });
    if (q('#clearSelection')) q('#clearSelection').onclick = () => { W.selection = [0, 0]; W.loop = false; media()?.setLoop(false); paintSelection(); scheduleSave(); };
    if (q('#loopRegion')) q('#loopRegion').onclick = () => { W.loop = !W.loop; media()?.setLoop(W.loop); paintSelection(); scheduleSave(); };
    if (q('#previewRangeBtn')) { q('#previewRangeBtn').textContent = '试听选区'; q('#previewRangeBtn').onclick = previewSelection; }
    if (q('#localGenerate')) q('#localGenerate').onclick = notWired;
    all('#tracks .wave-wrap').forEach(wave => {
      if (!q('.edit-selection', wave)) { const region = document.createElement('span'); region.className = 'edit-selection'; wave.appendChild(region); }
      let start = null, origin = 0, dragged = false;
      const position = event => { const rect = wave.getBoundingClientRect(); return Math.max(0, Math.min(duration(), (event.clientX - rect.left) / Math.max(1, rect.width) * duration())); };
      wave.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.stopImmediatePropagation(); event.preventDefault();
        start = position(event); origin = event.clientX; dragged = false;
        wave.setPointerCapture(event.pointerId);
        media()?.selectRole(wave.closest('.track').dataset.role);
      }, true);
      wave.addEventListener('pointermove', event => {
        if (start === null || Math.abs(event.clientX - origin) <= 4) return;
        dragged = true; const end = position(event); W.selection = [Math.min(start, end), Math.max(start, end)]; paintSelection();
      });
      const finish = event => {
        if (start === null) return;
        if (event.type === 'pointerup') { if (dragged) scheduleSave(); else media()?.seek(start); }
        start = null;
      };
      wave.addEventListener('pointerup', finish); wave.addEventListener('pointercancel', finish);
    });
    if (q('#tracks')) q('#tracks').addEventListener('scroll', paintRuler);
    window.addEventListener('resize', paintTimeline);
  }

  function hidePlayer() {
    q('.app')?.classList.remove('has-audio', 'player-collapsed'); W.playerKind = ''; W.playerPage = '';
  }
  function onPlayback(kind, detail = {}) {
    if (!S) return;
    if (kind === 'cover' && S.playbackKind !== 'file' && page() !== 'cover') return;
    if (!detail.playing && !W.playerKind) return;
    if (detail.playing) {
      const changed = W.playerKind !== kind;
      W.playerKind = kind; W.playerPage = page();
      q('.app')?.classList.add('has-audio');
      if (changed) { W.collapsed = false; q('.app')?.classList.remove('player-collapsed'); }
      setText('#playerHandle', '▶ ' + (kind === 'tts' ? '语音试听' : kind === 'cover' ? '翻唱试听' : '音频试听'));
      if (kind === 'tts' || kind === 'file') {
        setText('#nowSub', kind === 'tts' ? '文字生成 · 本地音频' : '本地音频试听');
        setText('#playerMode', kind === 'tts' ? '文字生成' : '文件试听');
      } else {
        if (!window.VS_PAGES?.cover.compareOn) setText('#nowSub', '翻唱工程 · 本地音频');
        if (!S.audio || S.audio.paused) setText('#nowTitle', S.song ? S.song.title : '翻唱试听');
      }
    }
    if (detail.title) setText('#nowTitle', detail.title);
  }
  function bindAudio() {
    if (!S.audio || S.audio === audioBound) return;
    audioBound = S.audio;
    audioBound.addEventListener('play', () => requestAnimationFrame(() => {
      const kind = page() === 'tts' ? 'tts' : page() === 'cover' ? 'cover' : 'file';
      onPlayback(kind, {playing: !S.audio.paused, title: q('#nowTitle')?.textContent});
    }));
    audioBound.addEventListener('pause', () => { S.playing = false; setText('#playBtn', '▶'); setText('#ttsPlay', '▶'); });
    audioBound.addEventListener('ended', () => { S.playing = false; setText('#playBtn', '▶'); setText('#ttsPlay', '▶'); });
  }
  function onPage(name) {
    all('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.page === (name === 'train' ? 'voices' : name)));
    if (W.playerKind && W.playerPage !== name) {
      if (S.audio) S.audio.pause(); media()?.pause(); hidePlayer();
    }
    if (name === 'cover') requestAnimationFrame(paintTimeline);
  }
  function installPlayer() {
    const player = q('.transport');
    if (player && !q('#collapsePlayer')) player.insertAdjacentHTML('beforeend', '<button class="mini" id="collapsePlayer" aria-label="折叠播放器">⌄</button>');
    if (!q('#playerHandle') && q('.app')) q('.app').insertAdjacentHTML('beforeend', '<button class="player-handle" id="playerHandle">▶ 展开播放器</button>');
    if (q('#collapsePlayer')) q('#collapsePlayer').onclick = () => { W.collapsed = true; q('.app')?.classList.add('player-collapsed'); };
    if (q('#playerHandle')) q('#playerHandle').onclick = () => { W.collapsed = false; q('.app')?.classList.remove('player-collapsed'); };
    bindAudio(); hidePlayer();
    pageObserver = new MutationObserver(() => onPage(page()));
    all('[data-page-view]').forEach(node => pageObserver.observe(node, {attributes: true, attributeFilter: ['class']}));
  }

  function paintTraining() {
    const training = window.VS_PAGES && window.VS_PAGES.training;
    const stage = (training && training.state && training.state.workflow && training.state.workflow.stage) || '';
    const index = {importing: 0, preprocessing: 2, review_required: 3, freezing: 3, feature_preparing: 4, training: 4, verifying: 5, saved: 5}[stage] || 0;
    all('[data-page-view="train"] .step-item').forEach((node, position) => node.classList.toggle('active', position === index));
  }
  function installNavigation() {
    q('#quickImportBtn')?.remove(); q('.nav-item[data-page="train"]')?.remove(); q('.production-bar')?.remove();
    const trainingButton = q('[data-page-view="voices"] .page-actions .btn');
    if (trainingButton) { trainingButton.textContent = '训练新声音'; trainingButton.onclick = () => { S.showPage('train'); window.VS_PAGES.training.refresh(); }; }
    const actions = q('[data-page-view="train"] .page-actions');
    if (actions && !q('#backToVoices')) actions.insertAdjacentHTML('afterbegin', '<button class="btn ghost" id="backToVoices">返回声音库</button>');
    if (q('#backToVoices')) q('#backToVoices').onclick = () => S.showPage('voices');
    const stepper = q('[data-page-view="train"] .stepper');
    if (stepper) stepper.innerHTML = [['导入','添加声音素材'],['质检','排除坏音频'],['切片','清理与分句'],['转写 / 校对','ASR · 可选校对 · 冻结'],['训练','GPT-SoVITS / RVC'],['验收','试听后启用版本']].map((step, i) => `<div class="step-item${i ? '' : ' active'}"><div class="step-num">${i + 1}</div><div class="step-copy"><b>${step[0]}</b><span>${step[1]}</span></div></div>`).join('');
    if (q('#reviewTraining')) q('#reviewTraining').textContent = '转写 / 校对（可选）';
    const training = window.VS_PAGES && window.VS_PAGES.training;
    if (training) { const original = training.paint; training.paint = function (...args) { const result = original.apply(this, args); paintTraining(); return result; }; }
    setText('.settings-tab[data-setting="engine"]', '模型与计算');
    onPage(page());
  }
  function paintTasks() {
    setText('#taskDrawer h3', 'GPU / 后台任务中心');
    setText('#taskDrawer .card-sub', '训练、分离、翻唱进度；可恢复任务会显示“继续”');
    const active = (S.tasks || []).filter(task => task.cancellable).length;
    const resumable = (S.tasks || []).filter(task => task.resumable).length;
    const engine = (S.data && S.data.engine && S.data.engine.label) || '本地任务';
    setText('#gpuLabel', active ? `${active} 个后台任务` : resumable ? `${resumable} 个任务可恢复` : engine);
  }
  function installTasks() {
    const gpu = q('#gpuBtn');
    if (gpu) { gpu.setAttribute('aria-label', 'GPU / 后台任务中心'); gpu.onclick = () => { q('#taskDrawer')?.classList.toggle('show'); S.refresh(); paintTasks(); }; }
    // Existing drawer buttons call real task.cancel/task.resume/file.reveal.
    // Unsafe process suspension is deliberately not offered as a fake pause.
    paintTasks();
  }
  function onState() {
    if (!S || !installed) return;
    bindAudio(); paintTasks(); paintTraining();
    const key = storageKey();
    if (key !== draftKey) {
      if (saveTimer && draftKey) commit();
      clearTimeout(saveTimer); saveTimer = null; draftKey = key;
      W.selection = [0, 0]; W.zoom = 1; W.loop = false;
      let saved;
      try { saved = JSON.parse(localStorage.getItem(key)); } catch (_) { saved = null; }
      if (saved) restore(saved);
      history = [snapshot()]; cursor = 0; buttons();
      setText('#autoSaveState', S.song ? '参数已自动保存' : '尚未选择工程');
      if (!S.song) hidePlayer();
    }
    paintTimeline();
  }
  function onEvent(name) {
    if (!S) return;
    if (name === 'cover.media') { requestAnimationFrame(paintTimeline); return; }
    if (name.indexOf('training.') === 0) requestAnimationFrame(paintTraining);
    if (name.indexOf('job.') === 0 || name.indexOf('cover.pipeline.') === 0) paintTasks();
  }
  function install(state) {
    S = state; if (installed) { onState(); return; } installed = true;
    const style = document.createElement('style'); style.id = 'workstation-production-style';
    style.textContent = '.task-drawer{width:min(540px,calc(100vw - 36px));max-height:calc(100vh - 108px)}.job b,.job small{font-size:11px}.job small{line-height:1.55}.job-actions{flex-wrap:wrap}#previewMode{max-width:110px}.history-tools span{font-size:10px}.timeline .card-head{flex-wrap:wrap}.preview-toolbar{gap:5px}.preview-toolbar .mini{padding:5px 7px}.timeline-edit-state{min-height:24px;flex-wrap:wrap}.edit-selection{z-index:1}.track .wave-wrap{touch-action:none}.train-side .step-item{padding:9px 8px}.train-side .step-copy span{font-size:10px}.transport{min-height:0}.player-handle{width:100%}';
    document.head.appendChild(style);
    installNavigation(); installHistory(); installTimeline(); installPlayer(); installTasks(); onState();
  }
  return {install, onState, onEvent, onPage, onPlayback, paint: onState, paintTimeline, state: W};
})();
