/* VoiceStudio HTML shell adapter.
 *
 * The v4 prototype is rendered verbatim; this file only connects it to the
 * local Python core through QWebChannel.  When the page is opened in a normal
 * browser (no channel) the adapter returns immediately and the prototype keeps
 * its own demo behaviour, so the same file still serves as the visual
 * reference.
 *
 * Rules:
 *  - Never change the prototype's markup, class names or inline styles.
 *  - Only replace the *content* of lists/cards with real local data.
 *  - Anything not wired to a real operation says so explicitly.
 */
(() => {
  'use strict';
  // `__VS_STATIC__` is injected by the visual-verification script so the shell
  // can be captured exactly as the prototype renders, with no local data.
  if (window.__VS_STATIC__) {
    return;
  }
  if (!window.qt || !window.qt.webChannelTransport || typeof QWebChannel === 'undefined') {
    return;
  }

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector));
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
  const NOT_WIRED = '该功能尚未接入新界面';
  const S = {
    bridge: null,
    data: null,
    songs: [],
    voices: [],
    tasks: [],
    exports: [],
    generations: [],
    song: null,
    selectionVersion: 0,
    voice: null,
    songQuery: '',
    songFilter: 'all',
    voiceQuery: '',
    ttsQuery: '',
    ttsVoiceId: '',
    voiceFilter: 'all',
    exportFilter: 'all',
    settingsTab: 'general',
    audio: null,
    playing: false,
    toastTimer: null,
  };
  window.__vsBridge = S;
  S.fileUrl = fileUrl;
  S.playFile = playFile;
  S.$ = $;
  S.$$ = $$;
  S.esc = esc;
  S.toast = toast;
  S.invoke = invoke;
  S.setText = setText;
  S.state = S;
  S.selectSong = selectSong;
  let refreshTimer = null;
  S.refresh = () => {
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => { refreshTimer = null; invoke('app.refresh'); }, 120);
  };

  // ------------------------------------------------------------------ utils
  function toast(message) {
    const node = $('#toast');
    if (!node) { return; }
    node.textContent = String(message == null ? '' : message);
    node.classList.add('show');
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => node.classList.remove('show'), 2400);
  }

  function parse(reply) {
    try { return JSON.parse(reply); } catch (error) { return { ok: false, message: '本地返回无法解析' }; }
  }

  function invoke(action, payload, done) {
    if (!S.bridge) { toast('本地桥接未连接'); return; }
    const selectionVersion = S.selectionVersion;
    S.bridge.invoke(action, JSON.stringify(payload || {}), (reply) => {
      const result = parse(reply);
      if (result.message) { toast(result.message); }
      if (result.ok && result.data && result.data.songs) {
        if (result.cover_id && selectionVersion === S.selectionVersion) { S.song = { id: result.cover_id }; }
        applyState(result.data);
      }
      if (done) { done(result); }
    });
  }

  function setText(selector, value) {
    const node = $(selector);
    if (node) { node.textContent = String(value == null ? '' : value); }
  }

  // ----------------------------------------------------------------- songs
  function renderSongs() {
    const list = $('#songList');
    if (!list) { return; }
    const query = S.songQuery.toLowerCase();
    const rows = S.songs.filter((song) => {
      const byQuery = !query || String(song.title).toLowerCase().includes(query);
      if (!byQuery) { return false; }
      if (S.songFilter === 'todo') { return song.status === 'todo'; }
      if (S.songFilter === 'ready') { return song.status !== 'todo'; }
      return true;
    });
    list.innerHTML = '';
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'library-foot';
      empty.textContent = S.songs.length ? '没有匹配的歌曲工程。' : '还没有歌曲工程；点击上方“导入歌曲到工程”开始。';
      list.appendChild(empty);
    }
    rows.forEach((song, index) => {
      const row = document.createElement('div');
      const active = S.song && S.song.id === song.id;
      row.className = 'song-row' + (active ? ' active' : '');
      row.dataset.coverId = song.id;
      row.innerHTML = `<div class="song-cover alt${index % 4}">♪</div>`
        + `<div><div class="song-name">${esc(song.title)}</div>`
        + `<div class="song-meta">${esc(song.duration_text)} · ${esc(song.format)}</div></div>`
        + `<div class="song-actions"><span class="song-status ${song.status === 'todo' ? '' : song.status === 'done' ? 'done' : 'ready'}">${esc(song.status_text)}</span><button class="song-delete" title="移入工程回收目录">删除</button></div>`;
      row.onclick = () => selectSong(song);
      row.querySelector('.song-delete').onclick = async event => {
        event.stopPropagation();
        const confirmed = await window.VS_PAGES.openModal({title:'删除歌曲', body:`将“${esc(song.title)}”及其工程音轨移入回收目录。外部原始文件不会删除。`,actions:[{label:'取消',value:false},{label:'删除',kind:'primary',value:true}]});
        if (confirmed) invoke('song.delete',{cover_id:song.id});
      };
      list.appendChild(row);
    });
    setText('#songCount', `${S.songs.length} 首`);
  }

  function selectSong(song) {
    S.selectionVersion += 1;
    S.song = song;
    renderSongs();
    renderHero();
    renderStems();
    renderLyrics();
    loadAudio();
    if (window.VS_PAGES && S.pagesReady) { window.VS_PAGES.cover.syncFromLibrary(); }
  }

  function renderHero() {
    const song = S.song;
    if (!song) {
      setText('#heroTitle', '未选择歌曲');
      setText('#heroDur', '—');
      setText('#heroFmt', '—');
      setText('#heroStatus', 'NO PROJECT');
      const status = $('#heroStatus');
      if (status) { status.className = 'status'; }
      return;
    }
    setText('#heroTitle', song.title);
    setText('#heroDur', song.duration_text);
    setText('#heroFmt', song.format);
    setText('#nowTitle', `${song.title} · 本地工程`);
    setText('#totalTime', song.duration_text);
    const status = $('#heroStatus');
    if (status) {
      status.textContent = song.status === 'todo' ? 'NEEDS STEMS' : song.status === 'done' ? 'COVER READY' : 'STEMS READY';
      status.className = 'status ' + (song.status === 'todo' ? 'orange' : 'ready');
    }
  }

  function renderStems() {
    const holder = $('.stem-list');
    const summary = $('.sep-summary');
    const song = S.song;
    if (holder) {
      holder.innerHTML = '';
      const stems = (song && song.stems) || [];
      if (!stems.length) {
        const empty = document.createElement('div');
        empty.className = 'stem-row';
        empty.innerHTML = '<div><div class="stem-name">尚无分离结果</div>'
          + '<div class="stem-meta">在 AI 翻唱页分离后，这里会列出真实的 stem 文件</div></div>'
          + '<div class="stem-wave"></div><div></div>';
        holder.appendChild(empty);
      }
      stems.forEach((stem) => {
        const row = document.createElement('div');
        row.className = 'stem-row';
        row.innerHTML = `<div><div class="stem-name">${esc(stem.label)}</div>`
          + `<div class="stem-meta">${esc(stem.name)} · ${esc(stem.size_text)}</div></div>`
          + `<div class="stem-wave"><canvas class="stemCanvas"></canvas></div>`
          + `<div><button class="mini stem-play">▶ 试听</button></div>`;
        const button = $('.stem-play', row);
        if (button) {
          button.onclick = () => (stem.exists ? playFile(stem.path, stem.label) : toast('文件不存在：' + stem.path));
        }
        holder.appendChild(row);
      });
    }
    if (summary) {
      const cover = $('.song-cover', summary);
      if (cover) { cover.textContent = '♪'; }
      const title = $('b', summary);
      if (title) { title.textContent = song ? (song.source_name || song.title) : '未选择歌曲'; }
      const meta = $('.sep-files div div', summary);
      if (meta) {
        meta.textContent = song ? `${song.duration_text} · ${song.stems.length} 个 stem · ${song.status_text}` : '—';
      }
      const status = $('.status', summary);
      if (status) {
        status.textContent = song ? song.status_text : '未选择';
        status.className = 'status ' + (song && song.status !== 'todo' ? 'ready' : 'orange');
      }
    }
  }

  function renderLyrics() {
    const holder = $('#lyrics');
    if (!holder) { return; }
    const song = S.song;
    const lines = (song && song.lyrics) || [];
    if (!lines.length) {
      holder.innerHTML = '<div class="lyric"><time>--:--</time><span>该歌曲工程还没有歌词（LRC）。</span></div>';
      return;
    }
    holder.innerHTML = '';
    lines.forEach((line) => {
      const item = document.createElement('div');
      item.className = 'lyric';
      item.dataset.time = String(line.seconds);
      item.innerHTML = `<time>${esc(line.time)}</time><span>${esc(line.text)}</span>`;
      item.onclick = () => seekSeconds(Number(line.seconds) || 0);
      holder.appendChild(item);
    });
  }

  // ------------------------------------------------------------------- tts
  function renderTtsVoices() {
    const list = $('#ttsVoiceList');
    if (!list) { return; }
    const query = S.ttsQuery.toLowerCase();
    if (!S.voices.some(v => v.id === S.ttsVoiceId)) { S.ttsVoiceId = (S.voices[0] || {}).id || ''; }
    const rows = S.voices.filter((voice) => !query || voice.name.toLowerCase().includes(query));
    list.innerHTML = '';
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'library-foot';
      empty.textContent = S.voices.length ? '没有匹配的声音。' : '还没有声音配置；请先到“训练声音”创建。';
      list.appendChild(empty);
    }
    rows.forEach((voice, index) => {
      const row = document.createElement('div');
      row.className = 'voice-row' + (voice.id === S.ttsVoiceId ? ' active' : '');
      row.dataset.voiceId = voice.id;
      row.innerHTML = `<div class="voice-badge">◉</div>`
        + `<div><b style="font-size:9.7px">${esc(voice.name)}</b>`
        + `<div style="font-size:7.8px;color:var(--muted);margin-top:3px">${esc(voice.subtitle)}</div></div>`
        + `<span class="status ${voice.tts_ready ? 'ready' : 'orange'}">${voice.tts_ready ? '可生成' : '未就绪'}</span>`;
      row.onclick = () => {
        S.ttsVoiceId = voice.id;
        $$('.voice-row').forEach((node) => node.classList.remove('active'));
        row.classList.add('active');
        setText('#nowSub', voice.name);
        if (window.VS_PAGES && window.VS_PAGES.tts) { window.VS_PAGES.tts.voiceId = voice.id; }
        toast(`已选择声音：${voice.name}${voice.tts_ready ? '' : '（尚不可用于文字生成）'}`);
      };
      list.appendChild(row);
    });
    const head = $('[data-page-view="tts"] .card-head .status');
    if (head) { head.textContent = `${S.voices.length} 个`; }
  }

  // ---------------------------------------------------------------- voices
  function renderVoiceGrid() {
    const grid = $('#voiceGrid');
    if (!grid) { return; }
    const query = S.voiceQuery.toLowerCase();
    const rows = S.voices.filter((voice) => {
      if (query && !voice.name.toLowerCase().includes(query)) { return false; }
      if (S.voiceFilter === 'tts') { return voice.tts_ready; }
      if (S.voiceFilter === 'cover') { return voice.cover_ready; }
      return true;
    });
    grid.innerHTML = '';
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'library-foot';
      empty.textContent = '没有匹配的声音。';
      grid.appendChild(empty);
    }
    rows.forEach((voice, index) => {
      const card = document.createElement('div');
      card.className = 'voice-card-grid' + (index === 0 ? ' active' : '');
      card.dataset.voiceId = voice.id;
      const tags = [];
      tags.push(voice.tts_ready ? '<span class="cap ok">✓ 文字生成</span>' : '<span class="cap">文字生成未就绪</span>');
      tags.push(voice.cover_ready ? '<span class="cap ok">✓ AI 翻唱</span>' : '<span class="cap">AI 翻唱未就绪</span>');
      card.innerHTML = `<div class="voice-card-top"><div class="avatar">◉</div><div>`
        + `<div class="voice-card-title">${esc(voice.name)}</div>`
        + `<div class="voice-card-sub">${esc(voice.subtitle)}</div></div></div>`
        + `<div class="voice-tags">${tags.join('')}</div>`;
      card.onclick = () => {
        $$('.voice-card-grid').forEach((node) => node.classList.remove('active'));
        card.classList.add('active');
        renderVoiceDetail(voice);
      };
      grid.appendChild(card);
    });
    if (rows.length) { renderVoiceDetail(rows[0]); }
  }

  function renderVoiceDetail(voice) {
    S.voice = voice;
    S.voiceId = voice.id;
    if (window.VS_PAGES && window.VS_PAGES.voices) { window.VS_PAGES.voices.selectedId = voice.id; }
    setText('#detailName', voice.name);
    setText('#detailMeta', `创建于 ${voice.created_text} · ${voice.asset_count} 段素材 · ${voice.duration_text}`);
    const stats = $('[data-page-view="voices"] .stat-grid');
    if (stats) {
      stats.innerHTML = `<div class="stat"><b>${voice.asset_count}</b><span>授权素材</span></div>`
        + `<div class="stat"><b>${esc(voice.duration_text)}</b><span>素材时长</span></div>`
        + `<div class="stat"><b>${voice.tts_ready ? 'TTS' : '—'}</b><span>文字生成</span></div>`
        + `<div class="stat"><b>${voice.cover_ready ? 'RVC' : '—'}</b><span>AI 翻唱</span></div>`;
    }
    const versions = $('[data-page-view="voices"] .model-versions');
    if (versions) {
      if (!voice.models.length) {
        versions.innerHTML = '<div class="model-version"><div><b>尚无已保存模型</b>'
          + '<span>完成训练后这里会列出真实版本</span></div><span class="status">未训练</span></div>';
      } else {
        versions.innerHTML = voice.models.map((model) => {
          const ready = ['available', 'verified', 'active', 'ready'].includes(model.status);
          return `<div class="model-version"><div><b>${esc(model.name)}${model.active ? ' · 当前' : ''}</b>`
            + `<span>${esc(model.meta)}</span></div>`
            + `<span class="status ${ready ? 'ready' : 'orange'}">${ready ? '可用' : esc(model.status)}</span></div>`;
        }).join('');
      }
    }
  }

  // --------------------------------------------------------------- training
  function renderSamples() {
    const list = $('#sampleList');
    if (!list) { return; }
    const rows = [];
    S.voices.forEach((voice) => (voice.assets || []).forEach((asset) => rows.push(asset)));
    list.innerHTML = '';
    if (!rows.length) {
      list.innerHTML = '<div class="sample-row"><span></span><span>还没有授权素材</span>'
        + '<div class="quality"><i style="width:0%"></i></div><span class="status">空</span></div>';
      return;
    }
    rows.slice(0, 40).forEach((asset) => {
      const row = document.createElement('div');
      row.className = 'sample-row';
      row.innerHTML = `<button class="sample-play">▶</button><span>${esc(asset.name)}</span>`
        + `<div class="quality"><i style="width:${asset.quality}%"></i></div>`
        + `<span class="status ${asset.state_class}">${esc(asset.state_text)}</span>`;
      const button = $('.sample-play', row);
      if (button) {
        button.onclick = () => (asset.exists ? playFile(asset.path, asset.name) : toast('文件不存在：' + asset.path));
      }
      list.appendChild(row);
    });
    const score = $('[data-page-view="train"] .score');
    const total = rows.length;
    const confirmed = rows.filter((asset) => asset.state_text === '已确认').length;
    if (score) {
      score.innerHTML = `${total ? Math.round((confirmed / total) * 100) : 0} <small>/ 100</small>`;
    }
  }

  // ------------------------------------------------------------------ tasks
  function renderTasks() {
    const drawer = $('#taskDrawer');
    if (!drawer) { return; }
    const head = drawer.firstElementChild;
    drawer.innerHTML = '';
    if (head) { drawer.appendChild(head); }
    const rows = S.tasks.slice(0, 8);
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'job';
      empty.innerHTML = '<div class="job-top"><div><b>暂无任务</b><br><small>本地任务历史为空</small></div>'
        + '<span class="status">空闲</span></div><div class="jobbar"><i style="width:0%"></i></div>';
      drawer.appendChild(empty);
      return;
    }
    rows.forEach((task) => {
      const job = document.createElement('div');
      job.className = 'job';
      const actions = task.cancellable
        ? `<div class="job-actions"><button class="mini" data-task-cancel="${esc(task.id)}">取消</button></div>`
        : task.outputs.length
          ? `<div class="job-actions"><button class="mini" data-task-reveal="${esc(task.outputs[0])}">打开输出</button></div>`
          : '';
      job.innerHTML = `<div class="job-top"><div><b>${esc(task.title)}</b><br><small>${esc(task.kind_text)}</small></div>`
        + `<span class="status ${esc(task.status_class)}">${esc(task.status_text)}</span></div>`
        + `<div class="jobbar"><i style="width:${Math.round(task.progress)}%"></i></div>`
        + actions;
      drawer.appendChild(job);
    });
    $$('[data-task-cancel]', drawer).forEach((button) => {
      button.onclick = () => invoke('task.cancel', {id: button.dataset.taskCancel});
    });
    $$('[data-task-reveal]', drawer).forEach((button) => {
      button.onclick = () => invoke('file.reveal', { path: button.dataset.taskReveal });
    });
  }

  // ---------------------------------------------------------------- exports
  function renderExports() {
    const table = $('.export-table');
    if (!table) { return; }
    const rows = S.exports.filter((item) => S.exportFilter === 'all' || item.kind === S.exportFilter);
    table.innerHTML = '<div class="table-head"><span>文件</span><span>来源</span><span>格式</span>'
      + '<span>时间</span><span>操作</span></div>';
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'export-row';
      empty.innerHTML = '<div class="file-main"><div class="file-icon">—</div>'
        + '<div><b>还没有导出文件</b><div style="color:var(--muted);margin-top:3px">完成翻唱导出或文字生成后会显示在这里</div></div></div>'
        + '<span>—</span><span>—</span><span>—</span><span></span>';
      table.appendChild(empty);
    }
    rows.slice(0, 60).forEach((item) => {
      const row = document.createElement('div');
      row.className = 'export-row';
      row.innerHTML = `<div class="file-main"><div class="file-icon">${esc(item.format.slice(0, 3))}</div>`
        + `<div><b>${esc(item.name)}</b><div style="color:var(--muted);margin-top:3px">${esc(item.size_text)}</div></div></div>`
        + `<span>${esc(item.kind_text)}</span><span>${esc(item.format)}</span><span>${esc(item.time_text)}</span>`
        + `<button class="mini">打开</button>`;
      const button = $('button', row);
      if (button) { button.onclick = () => invoke('file.reveal', { path: item.path }); }
      table.appendChild(row);
    });
    const head = $('.export-list .card-sub');
    if (head) { head.textContent = `共 ${S.exports.length} 个文件`; }
  }

  // ----------------------------------------------------------------- recent
  function renderRecent() {
    const grid = $('.project-grid');
    if (grid) {
      const projects = (S.data && S.data.projects) || [];
      grid.innerHTML = '';
      if (!projects.length) {
        grid.innerHTML = '<div class="project-card"><div class="project-thumb"></div>'
          + '<div class="project-title">还没有工程</div><div class="project-meta">点击右上角新建</div></div>';
      }
      projects.slice(0, 12).forEach((project) => {
        const card = document.createElement('div');
        card.className = 'project-card';
        card.innerHTML = `<div class="project-thumb"></div><div class="project-title">${esc(project.name)}</div>`
          + `<div class="project-meta">${esc(project.updated_text)}${project.active ? ' · 当前工程' : ''}</div>`;
        card.onclick = () => invoke('project.activate', { path: project.path });
        grid.appendChild(card);
      });
    }
    const activity = $('.activity');
    if (activity) {
      const label = $('.section-label', activity);
      activity.innerHTML = '';
      if (label) { activity.appendChild(label); }
      S.tasks.slice(0, 6).forEach((task) => {
        const item = document.createElement('div');
        item.className = 'activity-item';
        item.innerHTML = `<b>${esc(task.title)}</b><span>${esc(task.kind_text)} · ${esc(task.updated_text)} · ${esc(task.status_text)}</span>`;
        activity.appendChild(item);
      });
      if (!S.tasks.length) {
        const item = document.createElement('div');
        item.className = 'activity-item';
        item.innerHTML = '<b>暂无活动</b><span>完成一次分离、训练或生成后这里会有记录</span>';
        activity.appendChild(item);
      }
    }
  }

  // --------------------------------------------------------------- settings
  const SETTINGS_TITLES = {
    general: '常规', engine: '计算与模型', storage: '存储', appearance: '外观', privacy: '隐私与数据',
  };

  function switchRow(title, copy, on, key) {
    return `<div class="toggle-row"><div class="toggle-copy"><b>${esc(title)}</b><span>${esc(copy)}</span></div>`
      + `<span class="switch ${on ? 'on' : ''}" data-setting-key="${esc(key)}"></span></div>`;
  }

  function pathRow(label, value, key) {
    const change = key ? `<button class="mini" data-pick="${esc(key)}">更改</button>` : '<button class="mini">—</button>';
    return `<div class="path-row"><span>${esc(label)}</span><div class="path">${esc(value)}</div>${change}</div>`;
  }

  function renderSettings(tab) {
    const holder = $('#settingsContent');
    if (!holder) { return; }
    S.settingsTab = tab || S.settingsTab;
    setText('#settingsTitle', SETTINGS_TITLES[S.settingsTab] || '常规');
    const settings = (S.data && S.data.settings) || {};
    const engine = (S.data && S.data.engine) || {};
    const storage = (S.data && S.data.storage) || {};
    let html = '';
    if (S.settingsTab === 'general') {
      html = '<div class="settings-section"><h3>应用行为</h3>'
        + switchRow('启动时恢复上次工程', '直接回到离开前的工作状态', settings.restore_workspace, 'ui.restore_workspace')
        + switchRow('任务完成后本地提示', '长任务完成时提醒', settings.task_notifications, 'ui.task_notifications')
        + switchRow('生成完成后自动播放', '使用本地播放器试听结果', settings.autoplay, 'generation.autoplay')
        + `<div class="field" style="max-width:280px;margin-top:10px"><label>默认语言</label>`
        + `<select data-setting-select="ui.language"><option value="zh-CN"${settings.language === 'zh-CN' ? ' selected' : ''}>简体中文</option></select></div>`
        + `<div class="field" style="max-width:280px;margin-top:10px"><label>默认输出目录</label>`
        + `<div class="path">${esc(settings.output_dir || '')}</div></div></div>`;
    } else if (S.settingsTab === 'engine') {
      const manifest = engine.manifest_checked
        ? (engine.manifest_valid ? '校验通过' : esc((engine.manifest_errors || []).join('；') || '校验失败'))
        : (engine.manifest_present ? '未完整校验（点击右侧按钮）' : '尚未安装');
      html = '<div class="settings-section"><h3>GPU 与计算</h3>'
        + `<div class="path-row"><span>本地引擎</span><div class="path">${esc(engine.python || engine.message || '未检测')}</div>`
        + '<button class="mini" data-action="engine.verify">重新检测</button></div>'
        + `<div class="path-row"><span>安装清单</span><div class="path">${manifest}</div><button class="mini">—</button></div>`
        + `<div class="path-row"><span>FFmpeg</span><div class="path">${esc(engine.ffmpeg || '未找到私有 FFmpeg')}</div><button class="mini">—</button></div>`
        + `<div class="path-row"><span>安装 / 修复</span><div class="path">下载固定版本的 Python、PyTorch、FFmpeg 与模型</div>`
        + '<button class="mini" data-action="engine.install">开始安装</button></div>'
        + switchRow('默认开启智能优化', '人声分离与降噪', settings.smart_optimization, 'smart_optimization')
        + '</div><div class="settings-section"><h3>模型目录</h3>'
        + pathRow('模型根目录', settings.paths ? settings.paths.models : '', 'models')
        + pathRow('运行时目录', storage.runtime_root || '', '')
        + '</div>';
    } else if (S.settingsTab === 'storage') {
      html = '<div class="settings-section"><h3>工程与缓存</h3>'
        + pathRow('工程目录', settings.paths ? settings.paths.projects : '', 'projects')
        + pathRow('缓存目录', settings.paths ? settings.paths.cache : '', 'cache')
        + `<div class="path-row"><span>磁盘</span><div class="path">已用 ${esc(storage.disk_used_gb)} GB / 共 ${esc(storage.disk_total_gb)} GB · 可用 ${esc(storage.disk_free_gb)} GB</div><button class="mini">—</button></div>`
        + `<div class="storage-bar"><i style="width:${Math.max(0, Math.min(100, storage.disk_percent || 0))}%"></i></div></div>`;
    } else if (S.settingsTab === 'appearance') {
      html = '<div class="settings-section"><h3>主题</h3><div class="theme-options">'
        + `<div class="theme-card${settings.theme === 'light' ? ' active' : ''}"><div class="theme-preview"></div><b>暖橙白 · 当前</b></div>`
        + `<div class="theme-card dark${settings.theme === 'dark' ? ' active' : ''}"><div class="theme-preview"></div><b>深色工作室</b></div></div></div>`
        + '<div class="settings-section"><h3>界面密度</h3><div class="field" style="max-width:300px"><label>布局密度</label>'
        + `<select data-setting-select="ui.density">`
        + ['standard', 'compact', 'comfortable'].map((value) => {
          const label = { standard: '标准', compact: '紧凑', comfortable: '宽松' }[value];
          return `<option value="${value}"${settings.density === value ? ' selected' : ''}>${label}</option>`;
        }).join('')
        + '</select></div></div>';
    } else {
      html = '<div class="settings-section"><h3>本地优先</h3>'
        + '<div class="toggle-row"><div class="toggle-copy"><b>禁止上传音频到网络</b><span>所有模型处理只在本机执行</span></div>'
        + '<span class="switch on" data-setting-key="__locked"></span></div>'
        + switchRow('保存任务历史', '仅保存在本机数据库', true, '__history')
        + '</div>';
    }
    holder.innerHTML = html;
    $$('.switch', holder).forEach((node) => {
      node.onclick = () => {
        const key = node.dataset.settingKey;
        if (!key || key.startsWith('__')) { toast('该选项由本地策略固定'); return; }
        const next = !node.classList.contains('on');
        node.classList.toggle('on', next);
        invoke('settings.set', { key: key, value: next });
      };
    });
    $$('[data-pick]', holder).forEach((node) => {
      node.onclick = () => invoke('settings.pick_directory', { key: node.dataset.pick });
    });
    $$('[data-action="engine.verify"]', holder).forEach((node) => {
      node.onclick = () => invoke('engine.verify', {}, () => renderSettings('engine'));
    });
    $$('[data-action="engine.install"]', holder).forEach((node) => {
      node.onclick = () => invoke('engine.install', { tools: true });
    });
    $$('[data-setting-select]', holder).forEach((node) => {
      node.onchange = () => invoke('settings.set', { key: node.dataset.settingSelect, value: node.value });
    });
    $$('.theme-card', holder).forEach((node) => {
      node.onclick = () => {
        const dark = node.classList.contains('dark');
        invoke('settings.set', { key: 'ui.theme', value: dark ? 'dark' : 'light' }, () => renderSettings('appearance'));
      };
    });
  }

  // ----------------------------------------------------------------- engine
  function renderEngine() {
    const engine = (S.data && S.data.engine) || {};
    setText('#gpuLabel', engine.label || '本地引擎未检测');
  }

  function renderGenerations() {
    const holder = $('.history-mini');
    if (!holder) { return; }
    const label = $('.section-label', holder);
    holder.innerHTML = '';
    if (label) { holder.appendChild(label); }
    const rows = S.generations.slice(0, 6);
    if (!rows.length) {
      const item = document.createElement('div');
      item.className = 'history-item';
      item.innerHTML = '<b>暂无生成记录</b><div style="color:var(--muted);margin-top:4px">生成语音后会保存在这里</div>';
      holder.appendChild(item);
      return;
    }
    rows.forEach((record) => {
      const item = document.createElement('div');
      item.className = 'history-item';
      item.innerHTML = `<b>${esc(record.created_text)}</b>`
        + `<div style="color:var(--muted);margin-top:4px">${esc(record.text)}</div>`;
      item.onclick = () => {
        if (record.exists) { playFile(record.path, record.text); } else { toast('该记录的输出文件已不存在'); }
      };
      holder.appendChild(item);
    });
  }

  // ------------------------------------------------------------------ audio
  function audioSource() {
    const song = S.song;
    if (!song) { return null; }
    const order = ['final_mix', 'ai_vocal', 'vocal'];
    for (const role of order) {
      const stem = (song.stems || []).find((item) => item.role === role && item.exists);
      if (stem) { return stem.path; }
    }
    return song.source_path && song.source_exists ? song.source_path : null;
  }

  function fileUrl(path) {
    // Keep the drive-letter colon intact; percent-encode every other segment
    // so Chinese file names and spaces load correctly from file:// pages.
    const normalized = String(path).replace(/\\/g, '/');
    const prefix = normalized.startsWith('/') ? 'file://' : 'file:///';
    const encoded = normalized.split('/').map((segment, index) => (
      index === 0 && /^[A-Za-z]:$/.test(segment) ? segment : encodeURIComponent(segment)
    )).join('/');
    return prefix + encoded;
  }

  function ensureAudio() {
    if (S.audio) { return S.audio; }
    const audio = new Audio();
    audio.preload = 'metadata';
    audio.addEventListener('timeupdate', paintPlayhead);
    audio.addEventListener('loadedmetadata', paintPlayhead);
    audio.addEventListener('ended', () => { S.playing = false; setText('#playBtn', '▶'); });
    S.audio = audio;
    return audio;
  }

  function loadAudio() {
    if (S.pagesReady) { return; }
    const source = audioSource();
    const audio = ensureAudio();
    if (!source) {
      audio.removeAttribute('src');
      setText('#curTime', '00:00');
      setText('#totalTime', S.song ? S.song.duration_text : '00:00');
      paintPercent(0);
      return;
    }
    if (audio.dataset.source !== source) {
      audio.dataset.source = source;
      audio.src = fileUrl(source);
    }
  }

  function paintPercent(percent) {
    const value = Math.max(0, Math.min(100, percent));
    $$('.playhead').forEach((node) => { node.style.left = value + '%'; });
    $$('.processed').forEach((node) => { node.style.width = value + '%'; });
    const bar = $('#progress i');
    if (bar) { bar.style.width = value + '%'; }
  }

  function paintPlayhead() {
    const audio = S.audio;
    if (!audio || !audio.duration) { return; }
    const percent = (audio.currentTime / audio.duration) * 100;
    paintPercent(percent);
    setText('#curTime', clock(audio.currentTime));
    setText('#totalTime', clock(audio.duration));
    const lines = $$('.lyric');
    let active = lines[0];
    lines.forEach((line) => { if (Number(line.dataset.time) <= audio.currentTime) { active = line; } });
    lines.forEach((line) => line.classList.toggle('active', line === active));
  }

  function clock(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
  }

  function seekSeconds(seconds) {
    if (S.pagesReady && S.playbackKind !== 'file') { window.VS_MEDIA.seek(seconds * 1000); return; }
    const audio = ensureAudio();
    if (!audio.src || !audio.duration) { return; }
    audio.currentTime = Math.max(0, Math.min(audio.duration, seconds));
    paintPlayhead();
  }

  function playFile(path, label) {
    if (!path) { toast('素材路径不可用'); return; }
    if (S.pagesReady) { window.VS_MEDIA.pause(); }
    S.playbackKind = 'file';
    const audio = ensureAudio();
    audio.dataset.source = path;
    audio.src = fileUrl(path);
    audio.play().then(() => {
      S.playing = true;
      setText('#playBtn', 'Ⅱ');
      setText('#nowTitle', label || path.split(/[\\/]/).pop());
      toast('正在试听：' + (label || path.split(/[\\/]/).pop()));
    }).catch(() => toast('无法播放该文件'));
  }

  function togglePlay() {
    const audio = ensureAudio();
    if (!audio.src) { toast('当前歌曲没有可播放的本地音频'); return; }
    if (S.playing) {
      audio.pause();
      S.playing = false;
      setText('#playBtn', '▶');
    } else {
      audio.play().then(() => { S.playing = true; setText('#playBtn', 'Ⅱ'); }).catch(() => toast('无法播放该本地文件'));
    }
  }

  // ------------------------------------------------------------------ apply
  function applyState(data) {
    const changed = S.data && S.data.project.path !== data.project.path;
    if (changed) { S.song = null; S.ttsVoiceId = ''; S.selectionVersion += 1; }
    S.data = data;
    S.songs = data.songs || [];
    S.voices = data.voices || [];
    S.tasks = data.tasks || [];
    S.exports = data.exports || [];
    S.generations = data.generations || [];
    const active = S.songs.find((song) => song.id === (S.song && S.song.id)) || S.songs[0] || null;
    S.song = active;
    renderSongs();
    renderHero();
    renderStems();
    renderLyrics();
    renderTtsVoices();
    renderVoiceGrid();
    if (!S.pagesReady) { renderSamples(); }
    renderTasks();
    renderExports();
    renderRecent();
    renderGenerations();
    renderSettings(S.settingsTab);
    renderEngine();
    loadAudio();
    if (S.pagesReady) { window.VS_PAGES.onState(changed); }
  }

  // ------------------------------------------------------------------- wire
  const UNWIRED = [
    '#previewRender',
  ];

  function wire() {
    const showPage = (name) => {
      $$('[data-page-view]').forEach(node => node.classList.toggle('hidden', node.dataset.pageView !== name));
      $$('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.page === name));
      if (S.pagesReady && window.VS_PAGES.media) { window.VS_PAGES.media.draw(); }
    };
    S.showPage = showPage;
    $$('[data-page]').forEach(node => node.addEventListener('click', () => showPage(node.dataset.page)));
    $('#settingsBtn').onclick = () => showPage('settings');
    $('#recentBtn').onclick = () => showPage('recent');
    $$('.switch').forEach(node => { node.onclick = () => node.classList.toggle('on'); });
    $$('input[type="range"]').forEach(node => {
      node.addEventListener('input', () => {
        const label = node.parentElement.querySelector('.value,.mix-val');
        if (label) { label.textContent = node.value; }
      });
    });
    $('#scriptText').addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); $('#ttsGenerate').click(); }
    });
    document.addEventListener('click', (event) => {
      const button = event.target.closest && event.target.closest(UNWIRED.join(','));
      if (button) {
        event.preventDefault();
        event.stopPropagation();
        toast(NOT_WIRED);
      }
    }, true);

    const search = $('#songSearch');
    if (search) { search.oninput = () => { S.songQuery = search.value; renderSongs(); }; }
    $$('.filter').forEach((node) => {
      node.onclick = () => {
        $$('.filter').forEach((other) => other.classList.remove('active'));
        node.classList.add('active');
        S.songFilter = node.dataset.filter || 'all';
        renderSongs();
      };
    });

    const ttsSearch = $('#ttsVoiceSearch');
    if (ttsSearch) { ttsSearch.oninput = () => { S.ttsQuery = ttsSearch.value; renderTtsVoices(); }; }

    const voiceSearch = $('#voiceSearch');
    if (voiceSearch) { voiceSearch.oninput = () => { S.voiceQuery = voiceSearch.value; renderVoiceGrid(); }; }
    $$('[data-vfilter]').forEach((node) => {
      node.onclick = () => {
        $$('[data-vfilter]').forEach((other) => other.classList.remove('active'));
        node.classList.add('active');
        S.voiceFilter = node.dataset.vfilter || 'all';
        renderVoiceGrid();
      };
    });

    $('#importModal').remove();
    $('#coverImport').onclick = () => invoke('song.import', {});
    $('#quickImportBtn').onclick = () => {
      const page = $('[data-page-view]:not(.hidden)');
      if (S.pagesReady && page && page.dataset.pageView === 'train') { window.VS_PAGES.training.importAssets(); }
      else if (S.pagesReady && page && page.dataset.pageView === 'separator') { window.VS_PAGES.separator.choose(); }
      else { invoke('song.import', {}); }
    };

    const play = $('#playBtn');
    if (play) { play.onclick = togglePlay; }
    const back = $('#back');
    if (back) { back.onclick = () => { const audio = ensureAudio(); if (audio.src) { seekSeconds(audio.currentTime - 10); } }; }
    const forward = $('#forward');
    if (forward) { forward.onclick = () => { const audio = ensureAudio(); if (audio.src) { seekSeconds(audio.currentTime + 10); } }; }
    const progress = $('#progress');
    if (progress) {
      progress.onclick = (event) => {
        const audio = ensureAudio();
        if (!audio.duration) { return; }
        const rect = progress.getBoundingClientRect();
        seekSeconds(((event.clientX - rect.left) / rect.width) * audio.duration);
      };
    }
    $$('.wave-wrap').forEach((wrap) => {
      wrap.onpointerdown = (event) => {
        const audio = ensureAudio();
        if (!audio.duration) { return; }
        const rect = wrap.getBoundingClientRect();
        seekSeconds(((event.clientX - rect.left) / rect.width) * audio.duration);
      };
    });

    const ttsPlay = $('#ttsPlay');
    if (ttsPlay) {
      ttsPlay.onclick = () => {
        const record = S.generations.find((item) => item.exists);
        if (!record) { toast('还没有可试听的生成结果'); return; }
        playFile(record.path, record.text);
      };
    }

    const gpu = $('#gpuBtn');
    if (gpu) { gpu.onclick = () => { const drawer = $('#taskDrawer'); if (drawer) { drawer.classList.toggle('show'); } invoke('app.refresh'); }; }
    const closeDrawer = $('#closeTaskDrawer');
    if (closeDrawer) { closeDrawer.onclick = () => { const drawer = $('#taskDrawer'); if (drawer) { drawer.classList.remove('show'); } }; }

    $$('.settings-tab').forEach((node) => {
      node.onclick = () => {
        $$('.settings-tab').forEach((other) => other.classList.remove('active'));
        node.classList.add('active');
        renderSettings(node.dataset.setting || 'general');
      };
    });
    const saveSettings = $('[data-page-view="settings"] .page-actions .btn.primary');
    if (saveSettings) { saveSettings.onclick = () => invoke('settings.save', {}); }

    $$('.export-list .card-head .mini').forEach((node) => {
      node.onclick = () => {
        $$('.export-list .card-head .mini').forEach((other) => other.classList.remove('active'));
        node.classList.add('active');
        const label = node.textContent.trim();
        S.exportFilter = label === '全部' ? 'all' : label === '翻唱' ? 'cover' : label === '语音' ? 'tts' : 'separate';
        renderExports();
      };
    });

    const recentCreate = $('[data-page-view="recent"] .page-actions .btn.primary');
    if (recentCreate) { recentCreate.onclick = () => invoke('project.create', {}); }

    $$('[data-page="recent"]').forEach((node) => { node.onclick = () => invoke('app.refresh'); });
    $$('.nav-item').forEach((node) => {
      node.addEventListener('click', () => { setTimeout(() => invoke('app.refresh'), 60); });
    });
  }

  // -------------------------------------------------------------------- boot
  new QWebChannel(window.qt.webChannelTransport, (channel) => {
    try {
      S.bridge = channel.objects.bridge;
      if (!S.bridge) { S.error = 'bridge object missing'; toast('本地桥接不可用'); return; }
      if (S.bridge.event && S.bridge.event.connect) {
        S.bridge.event.connect((name, payload) => {
          let data = {};
          try { data = JSON.parse(payload); } catch (error) { data = {}; }
          if (name === 'project.changed' && data.data) { applyState(data.data); return; }
          if (name === 'songs.changed') { invoke('app.refresh'); return; }
          if (name === 'worker.state') { setText('#gpuLabel', '本地工作进程：' + (data.state === 'running' ? '就绪' : data.state)); return; }
          if (name === 'worker.ready') { setText('#gpuLabel', data.ready ? '本地引擎就绪' : '本地工作进程未就绪'); return; }
          if (name === 'engine.verified') {
            if (S.data && data.engine) {
              S.data.engine = data.engine;
              renderEngine();
              renderSettings('engine');
            }
            toast(data.engine && data.engine.manifest_valid ? '引擎完整性校验通过' : '引擎完整性校验未通过');
            return;
          }
          if (name === 'worker.event') {
            if (data.event === 'result' || data.event === 'error') S.refresh();
            return;
          }
          if (window.VS_PAGES && typeof window.VS_PAGES.onEvent === 'function') {
            window.VS_PAGES.onEvent(name, data);
          }
        });
      }
      wire();
      S.bridge.bootstrap((reply) => {
        try {
          const result = parse(reply);
          if (!result.ok) { toast(result.message || '无法读取本地状态'); return; }
          applyState(result.data);
          if (window.VS_PAGES && typeof window.VS_PAGES.install === 'function') {
            window.VS_PAGES.install(S);
            S.pagesReady = true;
            window.VS_PAGES.onState(false);
          }
        } catch (error) {
          S.error = String((error && error.stack) || error);
          toast('界面数据渲染失败：' + ((error && error.message) || error));
        }
      });
    } catch (error) {
      S.error = 'boot: ' + String((error && error.stack) || error);
      toast('本地桥接初始化失败');
    }
  });
})();
