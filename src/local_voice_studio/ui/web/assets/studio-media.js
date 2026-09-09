/* Real waveform and preview controls. Audio routing lives in MediaService. */
window.VS_MEDIA = (() => {
  let S;
  const q = s => document.querySelector(s);
  const all = s => Array.from(document.querySelectorAll(s));
  const roles = ['original', 'vocal', 'instrumental', 'ai_vocal', 'final_mix'];
  const names = ['原曲', '原唱人声', '伴奏', 'AI 人声', '最终混音'];
  const M = { tracks: [], key: '', role: 'original', mode: 'solo_track', position: 0, duration: 0, playing: false };
  const clock = ms => `${String(Math.floor(ms / 60000)).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
  function mix() {
    const inputs = all('.mixer-body .mix-row input');
    return {ai: Number(inputs[0].value), instrumental: Number(inputs[1].value), original: Number(inputs[2].value)};
  }
  function configure(done) {
    if (!S.song) { if (done) done({ok: false}); return; }
    S.setText('#playerMode', {solo_track: '选中音轨', mix_preview: '快速混音', final_mix: '最终混音'}[M.mode]);
    const muted = [], solo = [];
    all('#tracks .track').forEach(row => {
      const buttons = row.querySelectorAll('.track-btn');
      if (buttons[0].classList.contains('active')) muted.push(row.dataset.role);
      if (buttons[1].classList.contains('active')) solo.push(row.dataset.role);
    });
    S.invoke('preview.configure', {cover_id: S.song.id, role: M.role, mode: M.mode, muted, solo, mix: mix()}, done);
  }
  function control(operation, value) { S.invoke('preview.control', {operation, value}); }
  function seek(ms) {
    if (S.playbackKind === 'file' && S.audio) { S.audio.currentTime = Math.max(0, ms / 1000); return; }
    configure(reply => { if (reply.ok) control('seek', Math.min(M.duration, Math.max(0, ms))); });
  }
  function load() {
    const song = S.song;
    const key = song ? JSON.stringify([S.data.project.path, song.id, song.source_path, song.stems.map(t => [t.role, t.sha256])]) : '';
    if (key === M.key) return;
    const changed = !song || M.coverId !== song.id;
    M.key = key; M.coverId = song ? song.id : ''; M.tracks = [];
    if (changed) {
      M.position = 0; M.playing = false; M.role = 'original'; M.mode = 'solo_track';
      all('.track-btn').forEach(b => b.classList.remove('active'));
      q('#previewMode').value = M.mode;
      control('pause');
    }
    M.duration = song ? song.duration_ms : 0;
    S.playbackKind = 'cover';
    if (S.audio) { S.audio.pause(); S.playing = false; }
    draw(); paintPosition();
    if (song) { configure(); S.invoke('cover.media', {cover_id: song.id}); }
  }
  function draw() {
    all('#tracks .track').forEach((row, index) => {
      const track = M.tracks.find(t => t.role === roles[index]);
      const canvas = row.querySelector('canvas');
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, rect.width * dpr); canvas.height = Math.max(1, rect.height * dpr);
      const context = canvas.getContext('2d'); context.scale(dpr, dpr);
      context.strokeStyle = row.dataset.color || '#e8843a';
      context.beginPath();
      const peaks = (track && track.peaks) || [];
      peaks.forEach((peak, i) => {
        const x = i / peaks.length * rect.width;
        context.moveTo(x, rect.height / 2 - peak[1] / 32768 * rect.height / 2);
        context.lineTo(x, rect.height / 2 - peak[0] / 32768 * rect.height / 2);
      });
      context.stroke();
      row.querySelector('.track-name').textContent = names[index];
      row.title = track ? (track.error || names[index]) : '尚无此音轨';
      row.querySelectorAll('.track-btn').forEach(b => { b.disabled = !track || !!track.error; });
      row.style.opacity = track && !track.error ? '1' : '.45';
    });
    q('#ruler').innerHTML = Array.from({length: 7}, (_, i) => `<span class="tick" style="left:${i / 6 * 100}%">${clock(M.duration * i / 6)}</span>`).join('');
    const original = M.tracks.find(t => t.role === 'original');
    q('.hero-meta span:last-child').textContent = original && original.metadata
      ? `${original.metadata.sample_rate / 1000} kHz / ${original.metadata.channels} 声道` : '等待音频分析';
  }
  function paintPosition() {
    const percent = M.duration ? Math.min(100, M.position / M.duration * 100) : 0;
    all('.playhead').forEach(n => { n.style.left = percent + '%'; });
    all('.processed').forEach(n => { n.style.width = percent + '%'; });
    q('#progress i').style.width = percent + '%';
    S.setText('#curTime', clock(M.position)); S.setText('#totalTime', clock(M.duration));
    S.setText('#playBtn', M.playing ? 'Ⅱ' : '▶');
    const lines = all('#lyrics .lyric');
    let active;
    lines.forEach(n => { if (Number(n.dataset.time) * 1000 <= M.position) active = n; });
    lines.forEach(n => n.classList.toggle('active', n === active));
  }
  function install(state) {
    S = state;
    const mode = document.createElement('select'); mode.id = 'previewMode'; mode.setAttribute('aria-label', '试听模式');
    mode.innerHTML = '<option value="solo_track">选中音轨</option><option value="mix_preview">快速混音</option><option value="final_mix">最终混音</option>';
    q('.preview-toolbar').replaceChildren(mode);
    mode.onchange = () => { M.mode = mode.value; S.playbackKind = 'cover'; configure(); };
    all('#tracks .track').forEach((row, index) => {
      row.dataset.role = roles[index];
      row.querySelectorAll('.track-btn').forEach((button, b) => {
        button.onclick = () => {
          button.classList.toggle('active');
          // Mute acts on the selected row; solo acts across the whole mix.
          M.role = roles[index]; M.mode = b === 1 ? 'mix_preview' : M.mode;
          mode.value = M.mode; S.playbackKind = 'cover'; configure();
        };
      });
      const wrap = row.querySelector('.wave-wrap');
      wrap.onpointerdown = event => {
        M.role = roles[index]; S.playbackKind = 'cover';
        const rect = wrap.getBoundingClientRect(); seek((event.clientX - rect.left) / rect.width * M.duration);
      };
    });
    q('#playBtn').onclick = () => {
      if (S.playbackKind === 'file' && S.audio) {
        if (S.audio.paused) S.audio.play().catch(() => S.toast('无法播放该文件')); else S.audio.pause();
        S.playing = !S.audio.paused; S.setText('#playBtn', S.playing ? 'Ⅱ' : '▶'); return;
      }
      if (!S.song) { S.toast('请先导入歌曲'); return; }
      if (S.audio) S.audio.pause();
      configure(reply => { if (reply.ok) control(M.playing ? 'pause' : 'play'); });
    };
    q('#back').onclick = () => seek((S.playbackKind === 'file' && S.audio ? S.audio.currentTime * 1000 : M.position) - 10000);
    q('#forward').onclick = () => seek((S.playbackKind === 'file' && S.audio ? S.audio.currentTime * 1000 : M.position) + 10000);
    q('#progress').onclick = event => {
      const duration = S.playbackKind === 'file' && S.audio ? S.audio.duration * 1000 : M.duration;
      const rect = q('#progress').getBoundingClientRect(); seek((event.clientX - rect.left) / rect.width * duration);
    };
    const volume = q('.transport-right input');
    if (volume) volume.oninput = () => { control('volume', Number(volume.value) / 100); if (S.audio) S.audio.volume = Number(volume.value) / 100; };
    all('.mixer-body .mix-row input').forEach(input => {
      input.oninput = () => { M.mode = 'mix_preview'; mode.value = M.mode; configure(); updateGains(); };
    });
    const reset = q('.mixer .subhead button');
    if (reset) { reset.disabled = false; reset.onclick = () => {
      all('.mixer-body .mix-row input').forEach((n, i) => { n.value = i === 2 ? 0 : 80; });
      updateGains(); configure();
    }; }
    window.addEventListener('resize', draw);
    updateGains();
  }
  function updateGains() {
    all('.mixer-body .mix-row').forEach(row => {
      const value = Number(row.querySelector('input').value);
      const db = value <= 80 ? -24 + (value - 1) * 24 / 79 : (value - 80) * 6 / 20;
      row.querySelector('.mix-val').textContent = value === 0 ? '−∞' : `${db.toFixed(1)} dB`;
    });
  }
  function onEvent(name, data) {
    if (name === 'cover.media' && S.song && data.cover_id === S.song.id && data.project === S.data.project.path) {
      M.tracks = data.tracks; const original = M.tracks.find(t => t.role === 'original');
      if (original && original.metadata) M.duration = original.metadata.duration_seconds * 1000;
      draw(); paintPosition();
      const failed = M.tracks.find(t => t.error); if (failed) S.toast('音轨分析失败：' + failed.error);
    }
    if (name === 'preview.state' && S.playbackKind !== 'file' && S.song && data.cover_id === S.song.id) {
      M.playing = data.playing; M.position = data.position; M.duration = data.duration || M.duration; paintPosition();
    }
    if (name === 'preview.error') S.toast(data.message);
  }
  return {install, load, draw, seek, onEvent, pause: () => control('pause'), state: M};
})();
