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
  const media = window.VS_MEDIA;
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
        button.onclick = async () => {
          const action = (options.actions || [])[Number(button.dataset.index)] || {};
          if (typeof action.onClick === 'function') {
            button.disabled = true;
            let value;
            try { value = await action.onClick(mask); }
            catch (error) { toast(String(error)); }
            button.disabled = false;
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
    if (!q('#taskCancel')) {
      const cancel = document.createElement('button'); cancel.id = 'taskCancel'; cancel.className = 'mini'; cancel.textContent = '取消';
      q('#taskClose').before(cancel);
    }
    return pop;
  }

  function showTask(requestId, title, cancelAction) {
    const pop = taskPopup();
    if (TASKS.has(String(requestId))) return;
    clearTimeout(pop.hideTimer);
    pop.dataset.requestId=String(requestId);
    TASKS.set(String(requestId), { title: title || '正在处理' });
    S.setText('#taskTitle', title || '正在处理');
    S.setText('#taskStage', '准备中…');
    S.setText('#taskPct', '0%');
    const bar = q('#taskBar');
    if (bar) { bar.style.width = '0%'; }
    pop.classList.add('show');
    const cancel = q('#taskCancel');
    if (cancel) { cancel.disabled = false; cancel.onclick = () => {
      if (cancelAction) cancelAction(); else S.invoke('cover.cancel', { request_id: requestId });
      cancel.disabled = true;
    }; }
    const close = q('#taskClose');
    if (close) { close.onclick = () => { pop.classList.remove('show'); }; }
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
    pop.hideTimer=setTimeout(() => { if(pop.dataset.requestId===String(requestId)) pop.classList.remove('show'); }, 1200);
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
      if (lyricImport) {
        lyricImport.onclick = () => S.invoke('cover.import_lrc', {cover_id: this.songId});
        const auto = document.createElement('button');
        auto.className = 'mini'; auto.textContent = '自动识别';
        auto.onclick = () => this.transcribe(); lyricImport.parentNode.appendChild(auto);
      }
      qq('.settings-body .toggle-row').forEach(row => {
        if ((q('b', row) || {}).textContent === '去混响') { q('.switch', row).id = 'dereverbToggle'; }
      });
      const pitch = q('#coverPitch');
      if (pitch) { pitch.oninput = (event) => { S.setText('#coverPitchVal', (Number(event.target.value) > 0 ? '+' : '') + event.target.value); }; }
      const presets = q('.recommend-row');
      presets.innerHTML = '<span>参数预设</span><button class="mini" data-preset="balanced">均衡</button><button class="mini" data-preset="voice">音色优先</button><button class="mini" data-preset="clear">辅音优先</button>';
      presets.onclick = event => {
        const values = {balanced:[.75,.33],voice:[.9,.25],clear:[.5,.1]}[event.target.dataset.preset];
        if (!values) return;
        q('#rvcIndex').value=values[0];q('#rvcProtect').value=values[1];S.setText('#rvcIndexVal',values[0]);S.setText('#rvcProtectVal',values[1]);
        toast('已更新检索强度与辅音保护参数，下次生成生效');
      };
      const advanced = q('#advancedPanel');
      advanced.innerHTML = '<div class="field"><label>音色检索强度 · Index Rate <output id="rvcIndexVal">0.75</output></label><input id="rvcIndex" type="range" min="0" max="1" step="0.05" value="0.75"></div><div class="field"><label>辅音保护 · Protect <output id="rvcProtectVal">0.33</output></label><input id="rvcProtect" type="range" min="0" max="0.5" step="0.01" value="0.33"></div><div class="field"><label>音高平滑</label><select id="rvcSmoothing"><option value="off">关闭</option><option value="light">轻度</option><option value="medium">中度</option></select></div><div class="card-sub">F0：RMVPE。平滑用于减少抖动，不会自动修正跑调。</div>';
      q('#advancedBtn').onclick = () => { advanced.classList.toggle('show'); };
      ['Index','Protect'].forEach(key => q('#rvc'+key).oninput = e => S.setText('#rvc'+key+'Val',e.target.value));
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
      const id = (S.song && S.song.id) || '';
      if (id !== this.songId) { this.state = null; this.songId = id; }
      if (id) { this.refresh(); }
      else { const button = q('#coverRender'); if (button) { button.textContent = '请先导入歌曲'; } }
      if (window.VS_PAGES.media) { window.VS_PAGES.media.load(); }
    },

    refresh() {
      if (!this.songId) { return; }
      const id = this.songId;
      S.invoke('cover.state', { cover_id: id }, (reply) => {
        if (id !== this.songId) { return; }
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
        settings: {index_rate: Number(q('#rvcIndex').value), protect: Number(q('#rvcProtect').value), f0_method:'rmvpe', autotune:q('#rvcSmoothing').value},
        cleanup: dereverb && dereverb.classList.contains('on') ? { mode: 'denoise', dereverb: 'light' } : null,
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
          + '<div class="format-opt" data-choice data-format="flac">FLAC<br><small>无损压缩</small></div>'
          + '<div class="format-opt" data-choice data-format="m4a">M4A<br><small>AAC 320 kbps</small></div>'
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
          S.setText('.smart-value', (value > 0 ? '+' : '') + value);
          S.setText('.smart-copy', '已按歌曲与目标声音的实际音高分析，建议已填入下方音调。');
          toast(`RMVPE 建议 ${value > 0 ? '+' : ''}${value} 半音（已填入）`);
        }
        if (data.kind === 'export' && data.data && data.data.outputs) {
          toast('导出完成：' + data.data.outputs.map((item) => String(item).split(/[\\/]/).pop()).join('、'));
        }
        S.invoke('app.refresh');
        setTimeout(() => this.refresh(), 300);
      } else if (name === 'job.cancelled') {
        finishTask(data.request_id, '已取消'); S.invoke('app.refresh');
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
        showTask(data.request_id, '生成语音', () => this.cancel());
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

  // ---------------------------------------------------------------- training
  const training = {
    profileId: '',
    state: null,

    install() {
      const drop = q('#trainDrop');
      if (drop) { drop.onclick = () => this.importAssets(); }
      const start = q('#trainStart');
      if (start) { start.onclick = () => this.primary(); }
      const restore = qq('[data-page-view="train"] .page-actions .btn')[0];
      if (restore) { restore.onclick = () => this.resume(); }
      this.setupTrainingUI();
      this.refresh();
    },

    setupTrainingUI() {
      const page = q('[data-page-view="train"]');
      const actions = q('.page-actions', page);
      const review = actions.querySelector('button');
      review.id = 'reviewTraining'; review.textContent = '校对片段（可选）';
      review.onclick = () => this.confirmDraft();
      actions.appendChild(q('#trainStart'));
      q('.train-start', page).remove();
      q('.train-center', page).insertAdjacentHTML('afterbegin', '<div id="trainStatus" class="card training-status" role="status">导入素材后点击一键训练，完成后可在声音库试听。</div>');
      const settings = q('.train-settings', page);
      settings.insertAdjacentHTML('beforeend', '<div class="field"><label><input type="checkbox" id="manualTrainingReview"> 训练前暂停，人工校对文字（可选）</label></div><div class="field"><label><input type="checkbox" id="smartTraining" checked> 自动清理与分离素材</label></div>');
      const quality = q('select', settings); quality.id = 'trainingQuality';
      quality.innerHTML = '<option value="standard">标准 · 8 / 15 轮</option><option value="quick">快速试训 · 4 / 5 轮</option><option value="extended">加长训练 · 12 / 20 轮</option>';
      quality.parentElement.classList.remove('hidden'); q('label', quality.parentElement).textContent = '训练时长（SoVITS / GPT）';
      q('.quality-card .section-label', page).textContent = '素材概况';
      q('.quality-card .status', page).textContent = '以筛选结果为准';
      qq('.quality-metric span', page).forEach((n,i) => n.textContent = ['素材数量','导入时长','声音授权','处理阶段'][i]);
      qq('.diagnostic-line > span', page).forEach((n,i) => n.textContent = ['导入音频时长','素材文件数','当前状态'][i]);
      q('.samples-card .mini', page).classList.add('hidden');
      const caps = qq('.train-settings .toggle-row', page);
      if (caps[1]) q('.toggle-copy span',caps[1]).textContent = '文字模型完成后继续训练 RVC';
    },

    refresh(profileId) {
      if (profileId) { this.profileId = profileId; }
      if (this.refreshPending) return;
      this.refreshPending = true;
      S.invoke('training.state', { profile_id: this.profileId || '' }, (reply) => {
        this.refreshPending = false;
        if (!reply.ok) { return; }
        this.state = reply.data;
        this.profileId = (reply.data.profile && reply.data.profile.id) || '';
        this.paint();
      });
    },

    paint() {
      const state = this.state;
      if (!state) { return; }
      const assets = state.assets || [];
      const list = q('#sampleList');
      const assetsKey = JSON.stringify(assets);
      if (list && this.assetsKey !== assetsKey) {
        this.assetsKey = assetsKey;
        list.innerHTML = '';
        if (!assets.length) {
          list.innerHTML = '<div class="sample-row"><span></span><span>还没有导入素材</span>'
            + '<div class="quality"><i style="width:0%"></i></div><span class="status">空</span></div>';
        }
        assets.forEach((asset) => {
          const row = document.createElement('div');
          row.className = 'sample-row';
          const flags = (asset.flags || []).length;
          const ratio = Math.max(0, Math.min(100, asset.seconds ? Math.round((asset.confirmed_seconds / asset.seconds) * 100) : 0));
          row.innerHTML = `<button class="sample-play">▶</button><span>${esc(asset.name)}</span>`
            + `<div class="quality"><i style="width:${ratio}%"></i></div>`
            + `<span class="status ${flags ? 'orange' : 'ready'}">${flags ? '需检查' : (asset.confirmed_seconds ? '已确认' : '待处理')}</span>`;
          const play = q('.sample-play', row);
          if (play) {
            play.onclick = () => (asset.exists ? S.playFile(asset.path, asset.name) : toast('素材文件已不存在'));
          }
          list.appendChild(row);
        });
      }
      // 质量诊断卡：显示真实统计
      const score = q('[data-page-view="train"] .score');
      if (score) {
        const total = state.total_seconds || 0;
        const target = state.singing_min_seconds || 180;
        score.textContent = state.total_text || '0:00';
      }
      const storage = q('[data-page-view="train"] .storage-bar i');
      if (storage) {
        storage.style.width = Math.min(100, Math.round(((state.total_seconds || 0) / (state.singing_min_seconds || 180)) * 100)) + '%';
      }
      const metrics = qq('[data-page-view="train"] .quality-metric b');
      if (metrics.length >= 4) {
        metrics[0].textContent = assets.length;
        metrics[1].textContent = state.total_text || '0:00';
        metrics[2].textContent = state.profile && state.profile.consent ? '已授权' : '未授权';
        metrics[3].textContent = ({preprocessing:'预处理',review_required:'等待处理',freezing:'锁定素材',feature_preparing:'提取特征',training:'训练中',verifying:'生成试听',saved:'已完成'})[(state.workflow || {}).stage] || '未开始';
      }
      const lines = qq('[data-page-view="train"] .diagnostic-line b');
      if (lines.length >= 3) {
        lines[0].textContent = (state.total_text || '0:00') + ' 已导入';
        lines[1].textContent = `${assets.length} 个文件`;
        lines[2].textContent = ({running:'运行中',waiting:'等待处理',completed:'完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'})[(state.workflow || {}).status] || '未开始';
      }
      const alert = q('[data-page-view="train"] .quality-alert');
      if (alert) {
        const flags = assets.filter(item => (item.flags || []).length).length;
        if (flags) { alert.textContent = `⚠ ${flags} 个素材存在质量标记，预处理阶段会进一步检查。`; alert.classList.remove('hidden'); }
        else { alert.classList.add('hidden'); }
      }
      // 步骤条
      const steps = qq('[data-page-view="train"] .step-item');
      const stage = (state.workflow && state.workflow.stage) || '';
      const index = { importing: 0, preprocessing: 1, review_required: 2, freezing: 3, feature_preparing: 3, training: 3, verifying: 4, saved: 4 }[stage];
      steps.forEach((node, position) => node.classList.toggle('active', index === undefined ? position === 0 : position === index));
      // 主按钮
      const start = q('#trainStart');
      if (start) {
        const workflow = state.workflow || {};
        if (workflow.running) { start.textContent = '取消当前任务'; }
        else if (workflow.stage === 'review_required' && state.draft && state.draft.segments) { start.textContent = '自动筛选并继续训练'; }
        else if (workflow.can_resume) { start.textContent = '继续上次任务'; }
        else if (assets.length) { start.textContent = '一键训练'; }
        else { start.textContent = '导入素材'; }
      }
      if (state.singing_request) start.textContent = '取消歌唱训练';
      const wf = state.workflow || {};
      q('#reviewTraining').disabled = !(state.draft && state.draft.segments && !wf.running);
      q('#trainStatus').textContent = wf.error || wf.waiting_reason && !wf.running && wf.waiting_reason || wf.message || '导入素材后点击一键训练，自动筛选合格片段并训练。';
      if (state.scanning) { start.disabled = true; start.textContent = '正在导入素材…'; } else { start.disabled = false; }
      const nameInput = q('[data-page-view="train"] .train-settings input');
      if (nameInput && state.profile && nameInput.dataset.profileId !== state.profile.id) { nameInput.value = state.profile.name; nameInput.dataset.profileId=state.profile.id; nameInput.readOnly=true; nameInput.title='可在我的声音页面修改名称'; }
    },

    paintDraft() {
      const draft = this.reviewDraft;
      if (!draft || !draft.segments) { return; }
      const holder = q('#trainDraftPanel');
      if (!holder) { return; }
      const rows = draft.segments;
      holder.innerHTML = rows.map((segment) => `<div class="sample-row" data-segment="${esc(segment.id)}">`
        + `<input type="checkbox" data-field="included" aria-label="纳入训练" ${segment.included ? 'checked' : ''} ${segment.hard_blocked ? 'disabled' : ''}>`
        + `<div><textarea data-field="text" aria-label="校对文字" style="width:100%">${esc(segment.text || '')}</textarea>`
        + `<div>${esc((segment.flags || []).join('、'))}</div>`
        + ((segment.flags || []).length && !segment.hard_blocked ? `<input data-field="override_reason" aria-label="人工复核原因" placeholder="确认质量可用时填写复核原因" value="${esc(segment.override_reason || '')}">` : '')
        + `</div><span>${Number(segment.seconds).toFixed(1)}s</span><span>${segment.hard_blocked ? '不可训练' : ''}</span></div>`).join('');
      holder.oninput = (event) => {
        const field = event.target.dataset.field;
        const row = event.target.closest('[data-segment]');
        if (!field || !row) { return; }
        const segment = draft.segments.find(item => item.id === row.dataset.segment);
        if (segment) { segment[field] = field === 'included' ? event.target.checked : event.target.value; }
      };
    },

    async importAssets(selection) {
      const consent = this.state && this.state.profile && this.state.profile.consent;
      if (!consent) {
        const nameInput = q('[data-page-view="train"] .train-settings input');
        const value = await openModal({
          title: '导入声音素材',
          body: '请先确认这是本人声音，或已经取得明确授权。原始文件不会被修改，会复制到当前工程。',
          content: `<div class="field"><label>声音名称</label><input id="voiceName" value="${esc((nameInput && nameInput.value) || '我的声音')}"></div>`,
          actions: [
            { label: '取消', value: null },
            { label: '确认并选择文件', kind: 'primary', onClick: (mask) => (q('#voiceName', mask) || {}).value || '我的声音' },
          ],
        });
        if (!value) { return; }
        S.invoke('training.import', { profile_id: '', name: value, consent: true, selection });
        return;
      }
      S.invoke('training.import', { profile_id: this.profileId, selection });
    },

    async primary() {
      const state = this.state || {};
      const workflow = state.workflow || {};
      if (state.singing_request) { S.invoke('task.cancel',{request_id:state.singing_request}); return; }
      if (workflow.running) { S.invoke('training.cancel', { workflow_id: workflow.id }); return; }
      if (workflow.stage === 'review_required' && state.draft) { S.invoke('training.resume', {workflow_id:workflow.id}); return; }
      if (workflow.can_resume) { S.invoke('training.resume', { workflow_id: workflow.id }); return; }
      if (!(state.assets || []).length) { this.importAssets(); return; }
      const consent = state.profile && state.profile.consent;
      if (!consent) { toast('请先确认授权后再开始训练'); return; }
      const smart = q('#smartTraining');
      const capabilities = qq('[data-page-view="train"] .train-settings .toggle-row .switch');
      S.invoke('training.start', {
        profile_id: this.profileId,
        smart: smart ? smart.checked : true,
        manual_review: q('#manualTrainingReview').checked, quality: q('#trainingQuality').value,
        train_tts: capabilities[0].classList.contains('on'), train_singing: capabilities[1].classList.contains('on'),
      });
    },

    async confirmDraft() {
      const draft = this.state && this.state.draft;
      if (!draft) { toast('还没有可确认的片段'); return; }
      const abnormal = draft.abnormal || [];
      this.reviewDraft = JSON.parse(JSON.stringify(draft));
      const save = async (confirm) => {
        const reply = await new Promise(resolve => S.invoke('training.edit_draft', {
          draft_id: draft.id, segments: this.reviewDraft.segments.map(({id, text, included, override_reason}) => ({id, text, included, override_reason})),
        }, resolve));
        return reply.ok ? confirm : undefined;
      };
      const ok = await openModal({
        title: '确认训练片段',
        body: `已识别 ${draft.segments.length} 个片段，其中 ${abnormal.length} 个需要检查。`
          + `当前已确认 ${draft.confirmed_seconds.toFixed(1)} 秒（最低 60 秒）。`,
        content: '<div id="trainDraftPanel" class="sample-list" style="max-height:280px;overflow:auto"></div>',
        onReady: mask => { q('.modal',mask).classList.add('review-modal'); this.paintDraft(); },
        actions: [
          { label: '取消', value: false },
          { label: '保存校对', onClick: () => save(false) },
          { label: '确认并训练', kind: 'primary', onClick: () => save(true) },
        ],
      });
      if (!ok) { return; }
      S.invoke('training.confirm', { draft_id: draft.id });
    },

    resume() {
      const workflow = this.state && this.state.workflow;
      if (!workflow || !workflow.can_resume) { toast('没有可恢复的训练任务'); return; }
      S.invoke('training.resume', { workflow_id: workflow.id });
    },

    onEvent(name, data) {
      if (name.indexOf('job.') === 0 && data.kind === 'singing') {
        if (name === 'job.started') { showTask(data.request_id,'训练歌唱模型',()=>S.invoke('task.cancel',{request_id:data.request_id})); this.refresh(); }
        else if (name === 'job.progress') updateTask(data.request_id,data.percent,data.message,'');
        else if (['job.result','job.error','job.cancelled'].includes(name)) { finishTask(data.request_id,name==='job.result'?'歌唱训练完成':(data.message||'已取消'));this.refresh();S.refresh(); }
      } else if (name === 'training.scanned') {
        toast(data.message || '素材已导入');
        this.refresh(data.profile_id);
      } else if (name === 'training.scanning') {
        toast('正在检查素材文件……');
      } else if (name === 'training.error') {
        toast(data.message || '素材导入失败');
      } else if (name === 'training.workflow') {
        const workflow = data.workflow || {};
        this.state = this.state || {};
        const previous = this.state.workflow || {};
        this.state.workflow = workflow;
        if (data.profile_id) { this.profileId = data.profile_id; }
        if (workflow.running) { showTask('training-' + workflow.id, '训练声音', () => S.invoke('training.cancel', {workflow_id: workflow.id})); updateTask('training-' + workflow.id, workflow.progress, workflow.message, ''); }
        else if (workflow.stage === 'saved') { finishTask('training-' + workflow.id, '训练完成'); toast('新声音已验证并启用'); }
        else if (workflow.status === 'failed') { finishTask('training-' + workflow.id, '失败'); toast(workflow.error || '训练失败'); }
        if (previous.stage !== workflow.stage || previous.status !== workflow.status) this.refresh(data.profile_id);
        else this.paint();
        if (!workflow.running && workflow.stage === 'review_required') {
          q('#taskPop').classList.remove('show');
          q('#trainStatus').textContent = workflow.waiting_reason || workflow.message;
        }
      } else if (name === 'training.draft') {
        this.state = this.state || {};
        this.state.draft = data.draft;
        toast(`已识别 ${(data.draft.segments || []).length} 个片段，请确认`);
        this.refresh(data.profile_id);
      }
    },
  };

  // --------------------------------------------------------------- separator
  const separator = {
    coverId: '',

    install() {
      const drop = q('#sepDrop');
      if (drop) { drop.onclick = () => this.choose(); }
      const start = q('#sepStart');
      if (start) { start.onclick = () => this.start(); }
      qq('[data-page-view="separator"] .mode').forEach((node, index) => {
        node.dataset.mode = index === 0 ? 'uvr5' : 'roformer';
        node.onclick = () => {
          qq('[data-page-view="separator"] .mode').forEach((other) => other.classList.remove('active'));
          node.classList.add('active');
        };
      });
    },

    mode() {
      const active = q('[data-page-view="separator"] .mode.active');
      return (active && active.dataset.mode) || 'uvr5';
    },

    choose(selection) {
      S.invoke('separator.import', {selection}, (reply) => {
        if (!reply.ok) { return; }
        this.coverId = reply.cover_id;
        S.invoke('app.refresh');
      });
    },

    async start() {
      if (!this.coverId) {
        const song = (S.songs || [])[0];
        if (song) { this.coverId = song.id; }
      }
      if (!this.coverId) { toast('请先选择要分离的歌曲'); return; }
      const ok = await openModal({
        title: '歌曲权利确认',
        body: esc(RIGHTS_TEXT) + '<br><br>这只是你的权利声明，VoiceStudio 不会替你取得版权。',
        actions: [{ label: '取消', value: false }, { label: '我确认并开始分离', kind: 'primary', value: true }],
      });
      if (!ok) { return; }
      S.invoke('cover.attest', { cover_id: this.coverId, confirmed: true }, (reply) => {
        if (!reply.ok) { return; }
        S.invoke('cover.separate', { cover_id: this.coverId, mode: this.mode() });
      });
    },
  };

  // ----------------------------------------------------------------- exports
  const exportsPage = {
    install() {
      const action = document.createElement('button'); action.className = 'btn primary'; action.textContent = '导出当前歌曲';
      action.onclick = () => {
        const option = q('[data-page-view="exports"] .format-opt.active');
        const format = (((option && option.textContent) || 'WAV').match(/WAV|MP3|FLAC|M4A/) || ['wav'])[0].toLowerCase();
        if (!cover.songId || !cover.state || !cover.state.has_final_mix) { toast('请先在翻唱页生成最终混音'); return; }
        S.invoke('cover.export', {cover_id:cover.songId,format,existing_policy:'reject'});
      };
      q('[data-page-view="exports"] .page-actions').appendChild(action);
      const buttons = qq('[data-page-view="exports"] .page-actions .btn');
      if (buttons[0]) { buttons[0].onclick = () => S.invoke('exports.open_folder', {}); }
      if (buttons[1]) { buttons[1].onclick = () => S.invoke('exports.clean_cache', {}); }
      const openList = q('.export-table');
      if (openList) {
        openList.addEventListener('click', (event) => {
          const row = event.target.closest('.export-row');
          const button = event.target.closest('button');
          if (!row || !button) { return; }
          const name = q('b', row);
          const item = (S.exports || []).find((entry) => name && entry.name === name.textContent);
          if (item) { S.invoke('file.reveal', { path: item.path }); }
        });
      }
    },
  };

  // ------------------------------------------------------------------ engine
  const enginePage = {
    install() {},

    start() {
      S.invoke('engine.install', { tools: true });
    },

    onEvent(name, data) {
      if (name === 'engine.install.started') {
        const mask = document.createElement('div');
        mask.className = 'modal-mask show';
        mask.id = 'engineInstallMask';
        mask.innerHTML = '<div class="modal"><h3>安装 / 修复本地引擎</h3>'
          + '<p>正在运行固定版本的安装脚本。可以关闭本窗口，安装会在后台继续。</p>'
          + '<div class="sample-list" id="engineLog" style="max-height:260px;overflow:auto;font-size:8.5px"></div>'
          + '<div class="modal-actions"><button class="btn ghost" id="engineClose">关闭</button></div></div>';
        document.body.appendChild(mask);
        const close = q('#engineClose', mask);
        if (close) { close.onclick = () => mask.remove(); }
      } else if (name === 'engine.install.log') {
        const log = q('#engineLog');
        if (log) {
          const line = document.createElement('div');
          line.textContent = data.line || '';
          log.appendChild(line);
          log.scrollTop = log.scrollHeight;
        }
      } else if (name === 'engine.install.done') {
        toast(data.message || '安装结束');
        const mask = q('#engineInstallMask');
        if (mask) { setTimeout(() => mask.remove(), 1500); }
        S.invoke('app.refresh');
      }
    },
  };

  // ----------------------------------------------------------- unsupported UI
  const UNSUPPORTED = [
    '.take-strip',           // 多 Take 版本：Worker 只产出单次结果
    '#previewRender',
  ];

  const UNSUPPORTED_TEXT = [
    '.setting-row:nth-of-type(2)',   // 音色强度
    '.setting-row:nth-of-type(3)',   // 细节保留
  ];

  function hideUnsupported() {
    UNSUPPORTED.forEach((selector) => qq(selector).forEach((node) => node.classList.add('hidden')));
    qq('.settings-body .setting-row').forEach(row => {
      if (/音色强度|细节保留/.test(row.textContent)) row.classList.add('hidden');
    });
    qq('#previewRange').forEach(node => node.remove());
    const selector = q('.voice-select');
    if (selector) selector.innerHTML = '<div class="avatar">◉</div><div class="voice-copy"><div class="voice-name">请选择目标声音</div><div class="voice-meta">仅显示已授权且验证通过的模型</div></div>';
    S.setText('.voice-card .caps', '选择声音后显示可用模型');
    S.setText('.smart-value', '—');
    S.setText('.smart-copy', '选择声音后可分析歌曲音高，结果以实际检测为准。');
    S.setText('#applyPitch', '分析移调建议');
    S.setText('.estimate', '处理时间以实际任务为准');
    qq('.cover-right b').forEach(node => { if (node.textContent.includes('缓存命中')) node.textContent = '等待任务'; });
    qq('.settings-body .toggle-row').forEach(row => {
      if (row.textContent.includes('自动识别歌词')) row.classList.add('hidden');
    });
    const suggestion = q('.pitch-advice') || (q('#applyPitch') && q('#applyPitch').parentElement.parentElement);
    if (suggestion) {
      const number = suggestion.querySelector('.pitch-number,.pitch-value'); if (number) number.textContent = '—';
    }
    S.setText('.timeline .card-sub', '点击音轨定位；使用 M 静音、S 独奏或快速混音试听');
    qq('.library-foot').filter(n=>n.textContent.includes('Take')).forEach(n=>n.textContent='歌曲、音轨、歌词与分析缓存保存在当前工程。');
    S.setText('.render-foot,.render-meta', '处理进度以实际任务为准');
    S.setText('#nowTitle', S.song ? S.song.title : '尚未选择音频');
    S.setText('#nowSub', '本地试听');
    S.setText('#playerMode', '选中音轨');
    S.setText('#gpuBtn b', '本地任务');
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
      node.onclick = () => { qq('[data-page-view="exports"] .format-opt').forEach(n => n.classList.remove('active')); node.classList.add('active'); };
    });
    // 训练：训练质量下拉没有后端参数
    qq('[data-page-view="train"] .train-settings .field').forEach((node) => {
      const label = q('label', node);
      if (label && label.textContent.includes('训练质量')) { node.classList.add('hidden'); }
    });
    // 训练：后端只支持 UVR5 / RoFormer 两种分离方式
    qq('[data-page-view="separator"] .mode').forEach((node) => {
      if ((node.textContent || '').includes('多轨')) { node.classList.add('hidden'); }
    });
  }

  // ------------------------------------------------------------------- public
  function install(state) {
    S = state;
    if (installed) {
      cover.refresh();
      tts.refreshHistory();
      training.refresh();
      return;
    }
    installed = true;
    const style = document.createElement('style');
    style.textContent = `
      .train-right{min-height:0;overflow:auto;padding-right:4px}.train-right>.card{flex-shrink:0}.train-right .train-settings{order:-1}.recommend-row{font-size:11px;flex-wrap:wrap}.review-modal{width:min(720px,calc(100vw - 40px))}#trainDraftPanel textarea{min-height:56px;border:1px solid #e5dcd4;border-radius:8px;padding:8px;font:inherit;resize:vertical}#trainDraftPanel .sample-row{grid-template-columns:24px minmax(0,1fr) 48px}#trainDraftPanel .sample-row>span:last-child{grid-column:2 / -1}.train-center .drop-card{flex:none;padding:12px}.train-center .dropzone{height:90px}.train-center .drop-icon{display:none}.train-center .samples-card{flex:1}.training-status{flex:none;padding:12px 16px;color:#9d501a;background:#fff7ef;font-size:12px;line-height:1.5}.train-settings .field{margin-top:12px}.train-settings input[type=checkbox]{width:auto;accent-color:#ff781d}.train-settings label{line-height:1.7}.page-actions #trainStart{min-width:150px}.sample-row{font-size:11px;min-height:42px}.sample-row>span{overflow-wrap:anywhere}.song-status{font-size:10px}.song-delete{background:transparent;color:#a37961;font-size:11px;cursor:pointer;padding:6px}.song-row{grid-template-columns:42px minmax(0,1fr) auto}.song-actions{display:flex;flex-direction:column;align-items:flex-end}.song-delete:hover{color:#c44728}.train-right .score{font-size:24px}.train-right .quality-metric b{font-size:13px;overflow-wrap:anywhere}.train-right .quality-alert{line-height:1.6}#advancedPanel input[type=range]{width:100%}#advancedPanel .field{margin-top:12px}#advancedPanel output{float:right}#previewMode{font:inherit;border:1px solid #e5dcd4;border-radius:8px;padding:5px;background:white}
    `;
    document.head.appendChild(style);
    hideUnsupported();
    cover.install();
    tts.install();
    voices.install();
    training.install();
    separator.install();
    exportsPage.install();
    enginePage.install();
    media.install(S);
    // No demonstration handler is allowed to claim a successful operation.
    qq('.demo').forEach(node => {
      node.removeAttribute('data-msg');
      if (!node.onclick && !node.dataset.page) { node.disabled = false; node.title = '该功能尚未接入新界面'; node.onclick=()=>toast('该功能尚未接入新界面'); }
    });
  }

  function onState(projectChanged) {
    if (projectChanged) {
      cover.songId = ''; cover.state = null; cover.voiceId = '';
      training.profileId = ''; training.state = null;
      separator.coverId = ''; voices.selectedId = ''; tts.voiceId = '';
    }
    cover.syncFromLibrary();
    if (projectChanged) { training.refresh(); tts.refreshHistory(); }
  }

  function onEvent(name, data) {
    const event = String(name);
    const payload = data || {};
    if (event === 'files.dropped') {
      const page = q('[data-page-view]:not(.hidden)');
      if (page && page.dataset.pageView === 'train') training.importAssets(payload.selection);
      else if (page && page.dataset.pageView === 'separator') separator.choose(payload.selection);
      else S.invoke('song.import', {selection: payload.selection});
      return;
    }
    media.onEvent(event, payload);
    if (event.indexOf('job.') === 0) {
      if (payload.kind === 'singing') { training.onEvent(event,payload); return; }
      cover.onEvent(event, payload);
      tts.onEvent(event, payload);
      return;
    }
    if (event === 'voices.changed') { voices.onEvent(event, payload); return; }
    if (event.indexOf('training.') === 0) { training.onEvent(event, payload); return; }
    if (event.indexOf('engine.') === 0) { enginePage.onEvent(event, payload); }
  }

  return { install, onState, onEvent, cover, tts, voices, training, separator, exportsPage, enginePage, openModal, media };
})();
