/* VoiceStudio HTML shell — page behaviours.
 *
 * Split out of studio-bridge.js so each workflow (cover, tts, training …) can
 * be added without touching the core adapter.  `VS_PAGES.install(state)` is
 * called by the adapter once the bridge is connected and real data is painted.
 */
window.VS_PAGES = (function () {
  'use strict';
  let S = null;
  let installed = false;
  const RIGHTS_TEXT = '我确认自己拥有或已经获得处理、使用该音频所需的权利，并理解公开传播或商业发行可能需要额外取得歌曲、录音等相关授权。';

  const q = (selector, root) => (root || document).querySelector(selector);
  const qq = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector));
  const esc = (value) => S ? S.esc(value) : String(value == null ? '' : value);
  const toast = (message) => { if (S) { S.toast(message); } };

  // ------------------------------------------------------------------ modal
  function openModal(options) {
    return new Promise((resolve) => {
      const mask = document.createElement('div');
      mask.className = 'modal-mask show';
      const actions = (options.actions || []).map((action, index) =>
        `<button class="btn ${action.kind || 'ghost'}" data-index="${index}">${esc(action.label)}</button>`).join('');
      mask.innerHTML = `<div class="modal"><h3>${esc(options.title || '')}</h3>`
        + `<p>${options.body || ''}</p>${options.content || ''}`
        + `<div class="modal-actions">${actions}</div></div>`;
      const close = (value) => { mask.remove(); resolve(value); };
      qq('button[data-index]', mask).forEach((button) => {
        button.onclick = () => {
          const action = (options.actions || [])[Number(button.dataset.index)] || {};
          if (typeof action.onClick === 'function') {
            const value = action.onClick(mask);
            if (value === undefined) { return; }
            close(value);
            return;
          }
          close(action.value);
        };
      });
      mask.onclick = (event) => { if (event.target === mask) { close(null); } };
      document.body.appendChild(mask);
      qq('[data-choice]', mask).forEach((node) => {
        node.onclick = () => {
          qq('[data-choice]', mask).forEach((other) => other.classList.remove('active'));
          node.classList.add('active');
        };
      });
      if (typeof options.onReady === 'function') { options.onReady(mask); }
      const focus = q('input, select, textarea', mask);
      if (focus) { focus.focus(); }
    });
  }

  function chosen(mask, attribute) {
    const active = q('[data-choice].active', mask);
    return active ? active.getAttribute(attribute) : '';
  }

  function confirmModal(title, body, confirmLabel) {
    return openModal({
      title, body,
      actions: [
        { label: '取消', value: false },
        { label: confirmLabel || '确认', kind: 'primary', value: true },
      ],
    });
  }

  // -------------------------------------------------------------- task popup
  const TASKS = new Map();

  function taskPopup() {
    let pop = q('#taskPop');
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'task-pop';
      pop.id = 'taskPop';
      pop.innerHTML = '<div class="task-title"><span id="taskTitle">正在处理</span>'
        + '<span><button class="mini" id="taskCancel">取消</button> '
        + '<button class="mini" id="taskClose">×</button></span></div>'
        + '<div class="task-stage" id="taskStage">准备中…</div>'
        + '<div class="progressbar"><i id="taskBar"></i></div>'
        + '<div class="task-foot"><span id="taskPct">0%</span><span>本地 GPU 任务</span></div>';
      document.body.appendChild(pop);
    }
    return pop;
  }

  function showTask(requestId, title) {
    const pop = taskPopup();
    TASKS.set(String(requestId), { title: title || '正在处理' });
    S.setText('#taskTitle', title || '正在处理');
    S.setText('#taskStage', '准备中…');
    S.setText('#taskPct', '0%');
    const bar = q('#taskBar');
    if (bar) { bar.style.width = '0%'; }
    pop.classList.add('show');
    const cancel = q('#taskCancel');
    if (cancel) { cancel.onclick = () => { S.invoke('cover.cancel', { request_id: requestId }); cancel.disabled = true; }; }
    const close = q('#taskClose');
    if (close) { close.onclick = () => { pop.classList.remove('show'); TASKS.delete(String(requestId)); }; }
  }

  function updateTask(requestId, percent, stage, message) {
    if (!TASKS.has(String(requestId))) { return; }
    const bar = q('#taskBar');
    if (bar) { bar.style.width = Math.max(0, Math.min(100, percent)) + '%'; }
    S.setText('#taskPct', Math.round(percent) + '%');
    if (stage) { S.setText('#taskStage', stage + (message ? ' · ' + message : '…')); }
    else if (message) { S.setText('#taskStage', message); }
  }

  function finishTask(requestId, message) {
    TASKS.delete(String(requestId));
    const pop = q('#taskPop');
    if (!pop) { return; }
    S.setText('#taskStage', message || '完成');
    S.setText('#taskPct', '100%');
    const bar = q('#taskBar');
    if (bar) { bar.style.width = '100%'; }
    setTimeout(() => pop.classList.remove('show'), 1200);
  }

  // ------------------------------------------------------------------- cover
  const cover = {
    state: null,
    songId: '',
    voiceId: '',

    install() {
      const render = q('#coverRender');
      if (render) { render.onclick = () => this.smartNext(); }
      const reseparate = q('#reSeparate');
      if (reseparate) { reseparate.onclick = () => this.pickSeparation(); }
      const applyPitch = q('#applyPitch');
      if (applyPitch) { applyPitch.onclick = () => this.transpose(); }
      const voiceSelect = q('.voice-select');
      if (voiceSelect) { voiceSelect.onclick = () => this.pickVoice(); }
      const lyricImport = q('.lyrics .mini');
      if (lyricImport) { lyricImport.onclick = () => this.transcribe(); }
      const pitch = q('#coverPitch');
      if (pitch) { pitch.oninput = (event) => { S.setText('#coverPitchVal', (Number(event.target.value) > 0 ? '+' : '') + event.target.value); }; }
      this.watchSong();
      if (!this.songId && S.songs && S.songs.length) {
        this.songId = S.songs[0].id;
      }
      if (this.songId) { this.refresh(); }
    },

    watchSong() {
      const list = q('#songList');
      if (!list) { return; }
      list.addEventListener('click', (event) => {
        const row = event.target.closest('.song-row');
        if (!row) { return; }
        setTimeout(() => this.syncFromLibrary(), 40);
      });
    },

    syncFromLibrary() {
      const active = q('#songList .song-row.active');
      if (!active) { return; }
      const title = q('.song-name', active);
      const song = (S.songs || []).find((item) => title && item.title === title.textContent);
      if (song && song.id !== this.songId) {
        this.songId = song.id;
        this.refresh();
      }
    },

    refresh() {
      if (!this.songId) { return; }
      S.invoke('cover.state', { cover_id: this.songId }, (reply) => {
        if (!reply.ok) { return; }
        this.state = reply.data;
        this.paint();
      });
    },

    paint() {
      const state = this.state;
      if (!state) { return; }
      const rights = q('.rights-state') || q('#heroStatus');
      const render = q('#coverRender');
      const hints = [];
      if (!state.rights_confirmed) { hints.push('需要先确认歌曲处理权利'); }
      if (!state.has_vocal) { hints.push('下一步：分离人声与伴奏'); }
      else if (!this.voiceId) { hints.push('下一步：选择目标声音'); }
      else if (!state.has_ai_vocal) { hints.push('下一步：生成 AI 人声'); }
      else if (!state.has_final_mix) { hints.push('下一步：生成最终混音'); }
      else { hints.push('已完成，可导出成品'); }
      if (render) { render.textContent = '✦ ' + hints[0]; }
      if (rights && rights.classList.contains('rights-state')) {
        rights.textContent = state.rights_confirmed ? '歌曲权利：已确认' : '歌曲权利：未确认';
      }
    },

    async ensureRights() {
      if (this.state && this.state.rights_confirmed) { return true; }
      const ok = await openModal({
        title: '歌曲权利确认',
        body: esc(RIGHTS_TEXT) + '<br><br>这只是你的权利声明，VoiceStudio 不会替你取得版权。',
        actions: [{ label: '取消', value: false }, { label: '我确认', kind: 'primary', value: true }],
      });
      if (!ok) { return false; }
      const reply = await new Promise((resolve) => S.invoke('cover.attest', { cover_id: this.songId, confirmed: true }, resolve));
      if (!reply.ok) { return false; }
      this.state = reply.data;
      this.paint();
      return true;
    },

    async pickSeparation() {
      if (!this.songId) { toast('请先导入或选择一首歌'); return; }
      if (!(await this.ensureRights())) { return; }
      const engines = (this.state && this.state.engines) || {};
      const options = [];
      if (engines.uvr5 && engines.uvr5.ready) { options.push({ id: 'uvr5', label: '快速 · UVR5', detail: '速度优先，适合普通试听' }); }
      if (engines.roformer && engines.roformer.ready) { options.push({ id: 'roformer', label: '高质量 · RoFormer', detail: '更干净的人声边缘，推荐翻唱' }); }
      if (!options.length) { toast('分离引擎未安装：请到设置安装本地引擎'); return; }
      const choice = await openModal({
        title: '选择分离方式',
        body: '所有结果都会保存到当前歌曲工程，并复用缓存。',
        content: '<div class="mode-list">' + options.map((item, index) =>
          `<div class="mode${index === 0 ? ' active' : ''}" data-choice data-mode="${esc(item.id)}"><span class="radio"></span>`
          + `<div><b>${esc(item.label)}</b><span>${esc(item.detail)}</span></div></div>`).join('') + '</div>',
        actions: [
          { label: '取消', value: null },
          { label: '开始分离', kind: 'primary', onClick: (mask) => chosen(mask, 'data-mode') || options[0].id },
        ],
      });
      if (!choice) { return; }
      S.invoke('cover.separate', { cover_id: this.songId, mode: choice });
    },

    async transpose() {
      if (!this.voiceId) { toast('请先选择目标声音'); return; }
      S.invoke('cover.transpose', { cover_id: this.songId, profile_id: this.voiceId });
    },

    async pickVoice() {
      const voices = (S.voices || []).filter((voice) => voice.cover_ready);
      if (!voices.length) { toast('没有已就绪的歌唱模型：请先训练声音'); return; }
      const choice = await openModal({
        title: '选择目标声音',
        body: '只有已授权且歌唱模型通过验证的声音可用于 AI 翻唱。',
        content: '<div class="voice-list">' + voices.map((voice, index) =>
          `<div class="voice-row${index === 0 ? ' active' : ''}" data-choice data-voice="${esc(voice.id)}">`
          + '<div class="voice-badge">◉</div><div>'
          + `<b style="font-size:9.7px">${esc(voice.name)}</b>`
          + `<div style="font-size:7.8px;color:var(--muted);margin-top:3px">${esc(voice.subtitle)}</div></div>`
          + '<span class="status ready">可用</span></div>').join('') + '</div>',
        actions: [
          { label: '取消', value: null },
          { label: '使用该声音', kind: 'primary', onClick: (mask) => chosen(mask, 'data-voice') || voices[0].id },
        ],
      });
      if (!choice) { return; }
      const voice = voices.find((item) => item.id === choice) || voices[0];
      this.voiceId = voice.id;
      const name = q('.voice-card .voice-name');
      if (name) { name.textContent = voice.name; }
      const meta = q('.voice-card .voice-meta');
      if (meta) { meta.textContent = voice.subtitle; }
      this.paint();
      toast('已选择声音：' + voice.name);
    },

    async smartNext() {
      if (!this.songId) { toast('请先导入或选择一首歌'); return; }
      if (!this.state) { await new Promise((resolve) => { this.refresh(); setTimeout(resolve, 400); }); }
      const state = this.state || {};
      if (!state.rights_confirmed) { await this.ensureRights(); return; }
      if (!state.has_vocal) { await this.pickSeparation(); return; }
      if (!this.voiceId) { await this.pickVoice(); return; }
      if (!state.has_ai_vocal) { this.convert(); return; }
      if (!state.has_final_mix) { this.render(); return; }
      this.exportFinal();
    },

    convert() {
      if (!this.voiceId) { toast('请先选择目标声音'); return; }
      const pitch = q('#coverPitch');
      const dereverb = q('#dereverbToggle');
      S.invoke('cover.convert', {
        cover_id: this.songId,
        profile_id: this.voiceId,
        pitch_shift: pitch ? Number(pitch.value) : 0,
        settings: {},
        cleanup: dereverb && dereverb.classList.contains('on') ? { mode: 'denoise', dereverb: 'medium' } : null,
      });
    },

    render() {
      const rows = qq('.mixer-body .mix-row');
      const value = (index, fallback) => {
        const input = rows[index] ? q('input', rows[index]) : null;
        return input ? Number(input.value) : fallback;
      };
      S.invoke('cover.render', {
        cover_id: this.songId,
        profile_id: this.voiceId,
        mix: { ai: value(0, 80), instrumental: value(1, 80), original: value(2, 0) },
      });
    },

    async exportFinal() {
      if (!this.state || !this.state.has_final_mix) { toast('请先生成最终混音'); return; }
      const format = await openModal({
        title: '导出最终混音',
        body: '同时会生成 .voicestudio.json 记录 AI 生成标识、声音模型与输出哈希。',
        content: '<div class="format-grid">'
          + '<div class="format-opt active" data-choice data-format="wav">WAV<br><small>无损</small></div>'
          + '<div class="format-opt" data-choice data-format="mp3">MP3<br><small>320 kbps</small></div>'
          + '<div class="format-opt" data-choice data-format="both">两者<br><small>WAV + MP3</small></div></div>',
        actions: [
          { label: '取消', value: null },
          { label: '开始导出', kind: 'primary', onClick: (mask) => chosen(mask, 'data-format') || 'wav' },
        ],
      });
      if (!format) { return; }
      S.invoke('cover.export', { cover_id: this.songId, format: format, existing_policy: 'reject' });
    },

    async transcribe() {
      if (!this.songId) { toast('请先导入或选择一首歌'); return; }
      if (!(await this.ensureRights())) { return; }
      S.invoke('cover.lyrics', { cover_id: this.songId, language: 'zh' });
    },

    onEvent(name, data) {
      if (name === 'job.started' && data.kind && ['separate', 'convert', 'render', 'export', 'cleanup', 'transpose', 'lyrics'].includes(data.kind)) {
        const labels = { separate: '分离人声与伴奏', convert: '生成 AI 人声', render: '生成最终混音', export: '导出成品', cleanup: '人声清理', transpose: '音高分析', lyrics: '歌词识别' };
        showTask(data.request_id, labels[data.kind] || '正在处理');
      } else if (name === 'job.progress') {
        updateTask(data.request_id, data.percent, data.message, '');
      } else if (name === 'job.result') {
        const messages = {
          separate: '分离完成', convert: 'AI 人声已生成', render: '最终混音已生成',
          export: '导出完成', cleanup: '人声清理完成', transpose: '音高分析完成', lyrics: '歌词识别完成',
        };
        finishTask(data.request_id, messages[data.kind] || '完成');
        if (data.kind === 'transpose' && data.data && data.data.suggested_transpose !== undefined) {
          const value = Number(data.data.suggested_transpose);
          const pitch = q('#coverPitch');
          if (pitch) { pitch.value = value; S.setText('#coverPitchVal', (value > 0 ? '+' : '') + value); }
          toast(`RMVPE 建议 ${value > 0 ? '+' : ''}${value} 半音（已填入）`);
        }
        if (data.kind === 'export' && data.data && data.data.outputs) {
          toast('导出完成：' + data.data.outputs.map((item) => String(item).split(/[\\/]/).pop()).join('、'));
        }
        S.invoke('app.refresh');
        setTimeout(() => this.refresh(), 300);
      } else if (name === 'job.error') {
        finishTask(data.request_id, '失败');
        toast((data.message || '任务失败'));
      } else if (name === 'job.cancelling') {
        toast('正在取消任务…');
      }
    },
  };

  // --------------------------------------------------------------------- tts
  const tts = {
    voiceId: '',
    pending: '',

    install() {
      const button = q('#ttsGenerate');
      if (button) { button.onclick = () => this.generate(); }
      const clear = q('#clearScript');
      if (clear) { clear.onclick = () => { const area = q('#scriptText'); if (area) { area.value = ''; this.updateChars(); } }; }
      const area = q('#scriptText');
      if (area) { area.oninput = () => this.updateChars(); this.updateChars(); }
      const play = q('#ttsPlay');
      if (play) { play.onclick = () => this.togglePlay(); }
      const search = q('#ttsVoiceSearch');
      if (search) { search.oninput = () => { /* 列表由适配层重渲染 */ }; }
      this.refreshHistory();
    },

    updateChars() {
      const area = q('#scriptText');
      const counter = q('#charCount');
      if (!area || !counter) { return; }
      const count = area.value.replace(/\s/g, '').length;
      counter.textContent = `${count} 字 · 预计 ${Math.max(1, Math.round(count / 3.2))} 秒`;
    },

    refreshHistory() {
      S.invoke('tts.history', {}, (reply) => {
        if (!reply.ok) { return; }
        const data = reply.data || {};
        const holder = q('.history-mini');
        if (holder) {
          const label = q('.section-label', holder);
          holder.innerHTML = '';
          if (label) { holder.appendChild(label); }
          const rows = data.history || [];
          if (!rows.length) {
            const empty = document.createElement('div');
            empty.className = 'history-item';
            empty.innerHTML = '<b>暂无生成记录</b><div style="color:var(--muted);margin-top:4px">生成完成后会保留文字、声音与试听记录</div>';
            holder.appendChild(empty);
          }
          rows.slice(0, 6).forEach((row) => {
            const item = document.createElement('div');
            item.className = 'history-item';
            const text = esc(row.text);
            item.innerHTML = `<b>${esc(row.created_text)} · ${esc(row.voice)}</b>`
              + `<div style="color:var(--muted);margin-top:4px">${text}</div>`;
            const output = (row.outputs || [])[0];
            if (output && row.exists) {
              item.onclick = () => S.invoke('file.open', { path: output });
            }
            holder.appendChild(item);
          });
        }
        const output = q('[data-page-view="tts"] .script-foot');
        if (output && data.output_dir) { output.title = '输出目录：' + data.output_dir; }
      });
    },

    currentVoice() {
      const active = q('#ttsVoiceList .voice-row.active');
      return (active && active.dataset.voiceId) || this.voiceId || '';
    },

    generate() {
      const voice = this.currentVoice();
      const area = q('#scriptText');
      const text = area ? area.value.trim() : '';
      if (!voice) { toast('请先选择一个可用声音'); return; }
      if (!text) { toast('请输入要生成的文字'); return; }
      const speedRow = qq('[data-page-view="tts"] .setting-row');
      const speed = speedRow[0] ? Number(q('input', speedRow[0]).value) / 100 : 1.0;
      const pauseValue = speedRow[2] ? Number(q('input', speedRow[2]).value) : 55;
      const pause = Math.max(0.1, Math.min(1.0, pauseValue / 100 + 0.05));
      S.invoke('tts.generate', {
        profile_id: voice, text, speed: speed, pause: pause, language: 'zh',
      });
    },

    cancel() {
      if (!this.pending) { toast('没有正在执行的生成任务'); return; }
      S.invoke('tts.cancel', { request_id: this.pending });
    },

    togglePlay() {
      const record = (S.generations || []).find((item) => item.exists);
      if (!record) { toast('还没有可试听的生成结果'); return; }
      S.playFile(record.path, record.text);
    },

    onEvent(name, data) {
      if (data.kind !== 'tts') { return; }
      if (name === 'job.started') {
        this.pending = data.request_id;
        showTask(data.request_id, '生成语音');
      } else if (name === 'job.progress') {
        updateTask(data.request_id, data.percent, data.stage === 'load' ? '加载声音模型' : '语音合成', data.message);
      } else if (name === 'job.result') {
        this.pending = '';
        finishTask(data.request_id, '生成完成');
        const wav = data.wav || (data.outputs || [])[0];
        if (wav) { S.invoke('file.open', { path: wav }); }
        S.invoke('app.refresh');
        setTimeout(() => this.refreshHistory(), 400);
      } else if (name === 'job.error') {
        this.pending = '';
        finishTask(data.request_id, '失败');
        toast(data.message || '生成失败');
        this.refreshHistory();
      } else if (name === 'job.cancelled') {
        this.pending = '';
        finishTask(data.request_id, '已取消');
        toast('已取消生成');
        this.refreshHistory();
      } else if (name === 'job.cancelling') {
        toast('正在取消生成…');
      }
    },
  };

  // ------------------------------------------------------------------ voices
  const voices = {
    selectedId: '',

    install() {
      const actions = qq('[data-page-view="voices"] .detail-actions .btn');
      const [audition, edit, reveal, remove] = actions;
      if (audition) { audition.onclick = () => this.audition(); }
      if (edit) { edit.onclick = () => this.rename(); }
      if (reveal) { reveal.onclick = () => this.reveal(); }
      if (remove) { remove.onclick = () => this.archive(); }
      const versions = q('[data-page-view="voices"] .model-versions');
      if (versions) {
        versions.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-version]');
          if (button) { this.activate(button.dataset.version, button.dataset.kind); }
        });
      }
      const importButton = qq('[data-page-view="voices"] .page-actions .btn')[1];
      if (importButton) { importButton.onclick = () => this.importModel(); }
    },

    detail(profileId, done) {
      const id = profileId || this.selectedId;
      if (!id) { toast('请先选择一个声音'); return; }
      S.invoke('voices.detail', { profile_id: id }, (reply) => {
        if (reply.ok) { this.selectedId = id; this.paint(reply.data); }
        if (done) { done(reply); }
      });
    },

    paint(detail) {
      const versions = q('[data-page-view="voices"] .model-versions');
      if (!versions) { return; }
      if (!detail.versions.length) {
        versions.innerHTML = '<div class="model-version"><div><b>尚无已保存模型</b>'
          + '<span>完成训练后这里会列出真实版本</span></div><span class="status">未训练</span></div>';
        return;
      }
      versions.innerHTML = detail.versions.map((item) => {
        const action = item.active
          ? '<span class="status ready">当前</span>'
          : `<button class="mini" data-version="${esc(item.id)}" data-kind="${esc(item.kind)}"${item.usable ? '' : ' disabled'}>设为默认</button>`;
        return `<div class="model-version"><div><b>${esc(item.name)}${item.active ? ' · 当前' : ''}</b>`
          + `<span>${esc(item.meta)}</span></div>${action}</div>`;
      }).join('');
    },

    audition() {
      this.detail(this.selectedId, (reply) => {
        if (!reply.ok) { return; }
        const path = reply.data.preview;
        if (!path) { toast('这个声音还没有可试听的音频'); return; }
        S.playFile(path, reply.data.name);
      });
    },

    async rename() {
      const name = q('#detailName');
      const current = name ? name.textContent : '';
      const value = await openModal({
        title: '重命名声音',
        body: '只修改显示名称，素材与模型不受影响。',
        content: `<div class="field"><label>新名称</label><input id="renameInput" value="${esc(current)}"></div>`,
        actions: [
          { label: '取消', value: null },
          { label: '保存', kind: 'primary', onClick: (mask) => (q('#renameInput', mask) || {}).value || '' },
        ],
      });
      if (!value) { return; }
      S.invoke('voices.rename', { profile_id: this.selectedId, name: value });
    },

    reveal() {
      const path = (S.data && S.data.project && S.data.project.path) || '';
      if (!path) { toast('找不到工程目录'); return; }
      S.invoke('file.reveal', { path: path });
    },

    async archive() {
      const name = q('#detailName');
      const ok = await openModal({
        title: '移除声音配置',
        body: `将移除「${esc(name ? name.textContent : '')}」的声音配置。原始素材、快照与模型文件都会保留。`,
        actions: [{ label: '取消', value: false }, { label: '移除', kind: 'danger', value: true }],
      });
      if (!ok) { return; }
      S.invoke('voices.archive', { profile_id: this.selectedId });
    },

    activate(versionId, kind) {
      S.invoke('voices.activate', { profile_id: this.selectedId, version_id: versionId, kind: kind });
    },

    async importModel() {
      const value = await openModal({
        title: '导入模型',
        body: '新界面不会直接启用未登记的文件：请先完成授权、版本登记与验证。请填写本地模型文件的完整路径。',
        content: '<div class="field"><label>模型文件路径（多个用 ; 分隔）</label><input id="modelInput" placeholder="C:\\models\\voice.pth"></div>',
        actions: [
          { label: '取消', value: null },
          { label: '登记', kind: 'primary', onClick: (mask) => (q('#modelInput', mask) || {}).value || '' },
        ],
      });
      if (!value) { return; }
      const paths = String(value).split(';').map((item) => item.trim()).filter(Boolean);
      if (!paths.length) { return; }
      S.invoke('voices.import_model', { paths: paths });
    },

    onEvent(name) {
      if (name === 'voices.changed') {
        S.invoke('app.refresh');
        setTimeout(() => this.detail(this.selectedId), 400);
      }
    },
  };

  // ----------------------------------------------------------- unsupported UI
  const UNSUPPORTED = [
    '.recommend-row',        // 效果预设：后端没有预设概念
    '#advancedBtn',          // RVC 高级参数：当前 Worker 不接受
    '#advancedPanel',
    '.take-strip',           // 多 Take 版本：Worker 只产出单次结果
    '#abSeg',                // A/B 对比：无对应能力
    '#abCompare',
    '#previewRangeBtn',      // 20 秒局部试听：无局部渲染
    '#previewRender',
  ];

  const UNSUPPORTED_TEXT = [
    '.setting-row:nth-of-type(2)',   // 音色强度
    '.setting-row:nth-of-type(3)',   // 细节保留
  ];

  function hideUnsupported() {
    UNSUPPORTED.forEach((selector) => qq(selector).forEach((node) => node.classList.add('hidden')));
    UNSUPPORTED_TEXT.forEach((selector) => qq(selector).forEach((node) => node.classList.add('hidden')));
    // 自动处理开关里后端不支持的项（保留“去混响”）
    qq('.settings-body .toggle-row').forEach((row) => {
      const label = q('b', row);
      const text = label ? label.textContent : '';
      if (text.includes('缺失时自动分离') || text.includes('自动音高校正')) { row.classList.add('hidden'); }
    });
    // 文字生成：情绪与音高没有引擎参数，输出开关也没有对应实现
    qq('[data-page-view="tts"] .emotion-grid').forEach((node) => node.classList.add('hidden'));
    qq('[data-page-view="tts"] .param-card .divider').forEach((node) => node.classList.add('hidden'));
    qq('[data-page-view="tts"] .setting-row').forEach((row) => {
      const label = q('label', row);
      if (label && label.textContent.includes('音高')) { row.classList.add('hidden'); }
    });
    qq('[data-page-view="tts"] .param-card:nth-of-type(2)').forEach((node) => node.classList.add('hidden'));
    // 导出：后端只支持 WAV / MP3
    qq('[data-page-view="exports"] .format-opt').forEach((node) => {
      const text = node.textContent || '';
      if (text.includes('FLAC') || text.includes('M4A')) { node.classList.add('hidden'); }
    });
  }

  // ------------------------------------------------------------------- public
  function install(state) {
    S = state;
    if (installed) {
      cover.refresh();
      tts.refreshHistory();
      return;
    }
    installed = true;
    hideUnsupported();
    cover.install();
    tts.install();
    voices.install();
  }

  function onEvent(name, data) {
    const event = String(name);
    const payload = data || {};
    if (event.indexOf('job.') === 0) {
      cover.onEvent(event, payload);
      tts.onEvent(event, payload);
      return;
    }
    if (event === 'voices.changed') { voices.onEvent(event, payload); }
  }

  return { install, onEvent, cover, tts, voices, openModal };
})();
