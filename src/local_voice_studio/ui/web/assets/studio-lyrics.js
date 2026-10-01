/* Real online lyric lookup and synchronized local playback. */
window.VS_LYRICS = (() => {
  'use strict';
  let S, installed = false, activeKey = '', lastRender = '', search = null, download = null;
  let session = 0, activeStamp = '', suppressFollowUntil = 0;
  const q = (selector, root) => (root || document).querySelector(selector);
  const all = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  const context = () => ({project: S?.data?.project?.path || '', cover_id: S?.song?.id || ''});
  const contextKey = value => value.project + '\n' + value.cover_id;
  const current = value => !!S && contextKey(value) === contextKey(context());
  const esc = value => S.esc(value);
  const text = (selector, value) => { const node = q(selector); if (node) node.textContent = value; };
  const format = seconds => { const value = Math.max(0, Math.round(Number(seconds) || 0)); return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`; };
  const syncLine = line => line.seconds !== null && line.seconds !== '' && Number.isFinite(Number(line.seconds));
  function sourceText(song) {
    const source = song.lyrics_source || {}, lines = song.lyrics || [];
    if (!lines.length) return '还没有歌词：可联网查找、导入 LRC 或使用本地识别。';
    const provider = typeof source === 'string' ? source : source.provider || source.label || source.name || (source.kind === 'online' ? '在线歌词' : '本地歌词');
    const isSynced = song.lyrics_synced !== false && lines.some(syncLine);
    const origin = song.lyrics_origin || (typeof source === 'object' && source.kind);
    return `${provider || '本地歌词'} · ${isSynced ? '同步 LRC · 点击跳转' : '纯文本 · 无时间轴，不能同步跳转'}`
      + (origin === 'auto' || origin === 'asr' || origin === 'automatic' ? ' · 自动识别结果，请核对歌词' : '');
  }
  function render() {
    if (!S) return;
    const song = S.song, holder = q('#lyrics'); if (!holder) return;
    const lines = song?.lyrics || [], target = context();
    const key = JSON.stringify([target, lines, song?.lyrics_synced, song?.lyrics_source, song?.lyrics_offset_ms]);
    text('#lyricsSourceLabel', song ? sourceText(song) : '选择一首歌曲后添加歌词。');
    const offset = q('#lyricsOffsetInput');
    const synced = song?.lyrics_synced !== false && lines.some(syncLine);
    if (offset) { if (document.activeElement !== offset) offset.value = String((Number(song?.lyrics_offset_ms) || 0) / 1000); offset.disabled = !synced; }
    if (q('#applyLyricsOffset')) q('#applyLyricsOffset').disabled = !synced;
    if (q('#findOnlineLyrics')) q('#findOnlineLyrics').disabled = !song;
    if (key === lastRender) return;
    lastRender = key; activeStamp = '';
    holder.replaceChildren();
    if (!lines.length) { holder.innerHTML = '<div class="lyrics-empty">还没有歌词。联网查找可获取同步 LRC；本地识别结果需自行核对。</div>'; return; }
    lines.forEach((line, index) => {
      const timed = synced && syncLine(line), node = document.createElement('div');
      node.className = 'lyric'; node.dataset.sync = String(timed); node.dataset.index = index;
      node.innerHTML = `<time>${timed ? esc(line.time || format(line.seconds)) : '文本'}</time><span>${esc(line.text)}</span>`;
      if (timed) {
        const seconds = Math.max(0, Number(line.seconds));
        node.dataset.time = String(seconds); node.setAttribute('role', 'button'); node.tabIndex = 0;
        const seek = () => {
          if (!current(target)) return;
          if (S.playbackKind === 'file' && S.audio) {
            if (Number.isFinite(S.audio.duration)) S.audio.currentTime = Math.min(S.audio.duration, seconds);
          } else if (window.VS_MEDIA) window.VS_MEDIA.seek(seconds * 1000);
          updatePosition(seconds * 1000);
        };
        node.onclick = seek;
        node.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); seek(); } };
      } else node.setAttribute('aria-disabled', 'true');
      holder.appendChild(node);
    });
    const position = S.playbackKind === 'file' && S.audio ? S.audio.currentTime * 1000 : window.VS_MEDIA?.state.position || 0;
    updatePosition(position);
  }
  function updatePosition(ms) {
    if (!S || !q('[data-page-view="cover"]:not(.hidden)')) return;
    const holder = q('#lyrics'); if (!holder) return;
    const rows = all('.lyric', holder), position = Number(ms) / 1000;
    let stamp = -Infinity;
    rows.forEach(node => { if (node.dataset.sync === 'true' && Number(node.dataset.time) <= position) stamp = Math.max(stamp, Number(node.dataset.time)); });
    const selected = rows.filter(node => node.dataset.sync === 'true' && Number(node.dataset.time) === stamp);
    rows.forEach(node => node.classList.toggle('active', selected.includes(node)));
    const anchor = selected[0], next = anchor ? anchor.dataset.index : '';
    if (next === activeStamp) return;
    activeStamp = next;
    if (!anchor || Date.now() < suppressFollowUntil) return;
    const box = holder.getBoundingClientRect(), row = anchor.getBoundingClientRect();
    if (row.top < box.top + 8) holder.scrollTop -= box.top + 8 - row.top;
    else if (row.bottom > box.bottom - 8) holder.scrollTop += row.bottom - box.bottom + 8;
  }
  function status(message, failure = false) {
    text('#onlineLyricsStatus', message);
    if (q('#onlineLyricsStatus')) q('#onlineLyricsStatus').classList.toggle('error', failure);
  }
  function busy() {
    const working = !!search || !!download;
    const button = q('#lyricsSearchButton');
    if (button) { button.disabled = working; button.textContent = search ? '搜索中…' : '搜索候选'; }
    all('[data-lyrics-record]').forEach(button => { button.disabled = working || button.dataset.unavailable === 'true'; });
    q('#onlineLyricsMask')?.setAttribute('aria-busy', String(working));
    ['#lyricsTrackName', '#lyricsArtistName', '#lyricsQuery'].forEach(id => { if (q(id)) q(id).readOnly = !!download; });
  }
  function close() {
    q('#onlineLyricsMask')?.remove(); search = null;
  }
  function open() {
    if (!S.song) { S.toast('请先选择一首歌'); return; }
    close();
    const mask = document.createElement('div'); mask.className = 'modal-mask show'; mask.id = 'onlineLyricsMask';
    mask.innerHTML = '<div class="modal online-lyrics-panel" role="dialog" aria-modal="true" aria-labelledby="onlineLyricsTitle">'
      + '<h3 id="onlineLyricsTitle">联网找歌词</h3><p>输入歌名和歌手，选出与当前歌曲时长最接近的版本。</p>'
      + '<div class="online-lyrics-fields"><div class="field"><label for="lyricsTrackName">歌名</label><input id="lyricsTrackName" maxlength="200"></div>'
      + '<div class="field"><label for="lyricsArtistName">歌手（可选）</label><input id="lyricsArtistName" maxlength="200" placeholder="用于区分原唱、翻唱或现场版"></div>'
      + '<div class="field"><label for="lyricsQuery">补充关键词（可选）</label><input id="lyricsQuery" maxlength="300" placeholder="专辑、版本或其他关键词"></div></div>'
      + '<div class="online-lyrics-actions"><button class="btn primary" id="lyricsSearchButton">搜索候选</button><span class="online-lyrics-status" id="onlineLyricsStatus" role="status">尚未搜索</span></div>'
      + '<div id="onlineLyricsResults"></div><p class="online-lyrics-disclosure">联网仅发送歌名、歌手、关键词和时长，不上传音频。歌词来源与时间轴类型会保存在当前工程。</p>'
      + '<div class="modal-actions"><button class="btn ghost" id="closeOnlineLyrics">关闭</button></div></div>';
    document.body.appendChild(mask); q('#lyricsTrackName').value = S.song.title;
    q('#lyricsSearchButton').onclick = beginSearch; q('#closeOnlineLyrics').onclick = close;
    mask.onclick = event => { if (event.target === mask) close(); };
    mask.addEventListener('keydown', event => { if (event.key === 'Escape') close(); else if (event.key === 'Enter' && event.target.tagName === 'INPUT') beginSearch(); });
    q('#lyricsTrackName').focus(); busy();
  }
  function beginSearch() {
    if (search || download || !q('#onlineLyricsMask')) return;
    const track_name = q('#lyricsTrackName').value.trim(), artist_name = q('#lyricsArtistName').value.trim(), query = q('#lyricsQuery').value.trim();
    if (!track_name && !query) { status('请输入歌名或关键词。', true); return; }
    const pending = {...context(), token: ++session, request_id: '', early: []};
    search = pending; q('#onlineLyricsResults').replaceChildren(); status('正在获取候选歌词…'); busy();
    S.invoke('cover.lyrics.search', {cover_id: pending.cover_id, query, track_name, artist_name}, reply => {
      if (search !== pending || !current(pending)) return;
      if (!reply.ok || !reply.request_id) { search = null; status(reply.message || '搜索未能启动，请重试。', true); busy(); return; }
      pending.request_id = String(reply.request_id);
      const early = pending.early.find(data => String(data.request_id) === pending.request_id);
      if (early) settleSearch(early);
    });
  }
  function candidates(results) {
    const holder = q('#onlineLyricsResults'); if (!holder) return;
    holder.replaceChildren();
    if (!results.length) { holder.innerHTML = '<div class="lyrics-empty">没有找到匹配歌词。可补充歌手、缩短关键词或导入本地 LRC。</div>'; return; }
    results.forEach(record => {
      const row = document.createElement('div'); row.className = 'online-lyrics-result';
      const diff = Number(record.duration_diff_seconds), validDiff = record.duration_diff_seconds != null && Number.isFinite(diff);
      const duration = Number(record.duration_seconds), hasDuration = Number.isFinite(duration) && duration > 0;
      const difference = validDiff ? Math.abs(diff) < 1 ? '时长吻合' : `时长${diff > 0 ? '长' : '短'} ${Math.abs(diff).toFixed(1)} 秒` : '时长差未知';
      const type = record.instrumental ? '伴奏 / 无歌词' : record.synced ? '同步 LRC' : '纯文本 / 无时间轴';
      row.innerHTML = `<div><b>${esc(record.title || '未命名')}</b><small>${esc(record.artist || '歌手未知')}${record.album ? ' · ' + esc(record.album) : ''} · ${hasDuration ? format(duration) : '时长未知'} · ${difference}</small>`
        + `<small><span class="status ${record.synced ? 'ready' : 'orange'}">${type}</span>${esc(record.source || '在线来源')}</small></div>`;
      const button = document.createElement('button'); button.className = 'mini'; button.dataset.lyricsRecord = String(record.record_id);
      button.textContent = record.synced ? '使用 LRC' : '使用文本'; button.disabled = !!record.instrumental;
      if (record.instrumental) { button.title = '伴奏版本没有歌词'; button.dataset.unavailable = 'true'; }
      button.onclick = () => beginDownload(record); row.appendChild(button); holder.appendChild(row);
    });
  }
  function settleSearch(data) {
    const pending = search;
    if (!pending || !current(pending) || contextKey(data) !== contextKey(pending)) return;
    if (!pending.request_id) { pending.early.push(data); if (pending.early.length > 8) pending.early.shift(); return; }
    if (String(data.request_id) !== pending.request_id) return;
    search = null;
    if (data.error) { status('搜索失败：' + data.error + '；可重新搜索。', true); q('#onlineLyricsResults')?.replaceChildren(); }
    else { const rows = Array.isArray(data.results) ? data.results : []; candidates(rows); status(rows.length ? `找到 ${rows.length} 个候选；请核对歌手与时长。` : '未找到匹配结果。'); }
    busy();
    all('[data-unavailable=true]').forEach(button => { button.disabled = true; });
  }
  async function beginDownload(record) {
    if (search || download || record.instrumental) return;
    const target = context();
    const hasLyrics = !!S.song?.has_lyrics || !!(S.song?.lyrics || []).length;
    let overwrite = false;
    if (hasLyrics) {
      overwrite = await window.VS_PAGES.openModal({title: '替换当前歌词', body: `将用 ${esc(record.artist || '歌手未知')} 的“${esc(record.title)}”替换当前歌曲的歌词。${record.synced ? '下载的是同步 LRC。' : '该候选是纯文本，不能同步跳转。'}`,
        actions: [{label: '取消', value: false}, {label: '替换歌词', kind: 'primary', value: true}]});
      if (!overwrite || !current(target) || !q('#onlineLyricsMask')) return;
    }
    if (!current(target)) return;
    const pending = {...target, token: ++session, request_id: '', early: []}; download = pending;
    status('正在下载并保存歌词…'); busy();
    S.invoke('cover.lyrics.download', {cover_id: pending.cover_id, record_id: record.record_id, overwrite}, reply => {
      if (download !== pending || !current(pending)) return;
      if (!reply.ok || !reply.request_id) { download = null; status(reply.message || '下载未能启动，请重试。', true); busy(); return; }
      pending.request_id = String(reply.request_id);
      const early = pending.early.find(data => String(data.request_id) === pending.request_id);
      if (early) settleDownload(early);
    });
  }
  function settleDownload(data) {
    const pending = download;
    if (!pending || !current(pending) || contextKey(data) !== contextKey(pending)) return;
    if (!pending.request_id) { pending.early.push(data); if (pending.early.length > 8) pending.early.shift(); return; }
    if (String(data.request_id) !== pending.request_id) return;
    download = null;
    if (data.error || data.ok === false) { status('下载失败：' + (data.error || '未能保存歌词') + '；可重试。', true); busy(); return; }
    status(`已保存 ${Number(data.line_count) || 0} 行${data.synced ? '同步歌词' : '纯文本歌词（不能同步跳转）'}。`);
    busy(); lastRender = ''; S.refresh();
    S.toast(data.synced ? '已保存同步歌词' : '已保存纯文本歌词；没有同步时间轴');
  }
  function applyOffset() {
    if (!S.song) return;
    const seconds = Number(q('#lyricsOffsetInput').value);
    if (!Number.isFinite(seconds) || Math.abs(seconds) > 60) { S.toast('歌词时间偏移须在 -60 到 60 秒之间'); return; }
    const target = context();
    S.invoke('cover.lyrics.offset', {cover_id: target.cover_id, offset_ms: Math.round(seconds * 1000)}, reply => {
      if (!reply.ok || !current(target)) return;
      if (reply.data?.lyrics) { S.song.lyrics = reply.data.lyrics; S.song.lyrics_offset_ms = Math.round(seconds * 1000); }
      lastRender = ''; S.refresh();
    });
  }
  function onState() {
    if (!installed || !S) return;
    const key = contextKey(context());
    if (key !== activeKey) {
      activeKey = key; ++session; search = null; download = null; close(); lastRender = ''; suppressFollowUntil = 0;
    }
    render();
  }
  function onEvent(name, data) {
    if (name === 'lyrics.online.search') settleSearch(data || {});
    else if (name === 'lyrics.online.download') settleDownload(data || {});
  }
  function install(state) {
    S = state; if (installed) { onState(); return; } installed = true;
    const head = q('.lyrics .subhead');
    if (head && !q('#findOnlineLyrics')) head.insertAdjacentHTML('beforeend', '<button class="mini" id="findOnlineLyrics">联网找歌词</button>');
    if (head && !q('#lyricsSourceLabel')) head.insertAdjacentHTML('afterend', '<div class="lyrics-source" id="lyricsSourceLabel"></div>');
    const holder = q('#lyrics');
    if (holder && !q('#lyricsOffsetInput')) holder.insertAdjacentHTML('afterend', '<div class="lyrics-timing"><label for="lyricsOffsetInput">时间偏移</label><input id="lyricsOffsetInput" type="number" value="0" min="-60" max="60" step="0.1" aria-label="歌词时间偏移（秒）"><span>秒</span><button class="mini" id="applyLyricsOffset">应用</button><span>正值延后</span></div>');
    q('#findOnlineLyrics').onclick = open; q('#applyLyricsOffset').onclick = applyOffset;
    if (holder) ['wheel', 'touchstart', 'pointerdown'].forEach(name => holder.addEventListener(name, () => { suppressFollowUntil = Date.now() + 5000; }, {passive: true}));
    const auto = all('.lyrics .mini').find(button => button.textContent === '自动识别');
    if (auto) { auto.textContent = '本地识别'; auto.title = '使用本地 ASR；歌唱识别可能不准确，请核对歌词'; }
    onState();
  }
  return {install, onState, onEvent, render, updatePosition, open, get ready() { return installed; }};
})();
