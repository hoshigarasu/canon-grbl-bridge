(function () {
  'use strict';

  // ── Edit ボタン → ポップアップエディタ ───────────────────────
  function openEditor() {
    fetch('/active-file')
      .then(r => r.json())
      .then(d => {
        const f = d.path;
        if (!f) { alert('No file loaded'); return; }
        window.open('/editor?file=' + encodeURIComponent(f),
          'gcode-editor', 'width=960,height=720,resizable=yes');
      })
      .catch(e => alert('Error: ' + e));
  }

  document.addEventListener('click', function (e) {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.textContent.trim() !== 'Edit') return;
    e.stopImmediatePropagation();
    openEditor();
  }, true);

  // ── ⚙ grblHAL設定ボタン（右下固定） ─────────────────────────
  function addSettingsBtn() {
    if (document.getElementById('_grbl_btn')) return;
    const btn = document.createElement('button');
    btn.id = '_grbl_btn';
    btn.textContent = '⚙';
    btn.title = 'grblHAL Settings';
    btn.style.cssText = [
      'position:fixed','bottom:16px','right:16px','z-index:9999',
      'width:36px','height:36px','border-radius:50%',
      'background:#313244','color:#cdd6f4','border:1px solid #45475a',
      'font-size:16px','cursor:pointer','box-shadow:0 2px 6px rgba(0,0,0,.4)'
    ].join(';');
    btn.addEventListener('click', () =>
      window.open('/grbl-settings','grbl-settings','width=900,height=700,resizable=yes'));
    document.body.appendChild(btn);
  }

  // ── 実行時間計測（Startキャプチャ → 実行中のみポーリング） ───
  const INTERP_IDLE = 1;

  let _runStart = null;
  let _checkIv  = null;
  let _lastCompleted = null;

  function timerEl() {
    let el = document.getElementById('_run_timer');
    if (!el) {
      el = document.createElement('div');
      el.id = '_run_timer';
      el.style.cssText = [
        'position:fixed','top:10px','left:50%','transform:translateX(-50%)',
        'z-index:9999','background:rgba(24,24,37,.9)',
        'color:#a6adc8','padding:3px 14px','border-radius:16px',
        'font-family:monospace','font-size:12px',
        'border:1px solid #313244','pointer-events:none','display:none'
      ].join(';');
      document.body.appendChild(el);
    }
    return el;
  }

  function fmt(s) {
    const m = Math.floor(s / 60), ss = Math.floor(s % 60);
    return `${m}m ${String(ss).padStart(2,'0')}s`;
  }

  function stopCheck() {
    if (_checkIv) { clearInterval(_checkIv); _checkIv = null; }
  }

  function finishRun() {
    stopCheck();
    if (!_runStart) return;
    const elapsed = (Date.now() - _runStart) / 1000;
    _lastCompleted = elapsed;
    _runStart = null;
    const el = timerEl();
    el.textContent = `✓ ${fmt(elapsed)}`;
    el.style.color = '#a6e3a1';
    el.style.display = 'block';
  }

  async function checkState() {
    // 実行中のみ呼ばれる
    const elapsed = _runStart ? (Date.now() - _runStart) / 1000 : 0;
    timerEl().textContent = `⏱ ${fmt(elapsed)}`;

    try {
      const r = await fetch('/status-summary');
      const d = await r.json();
      if (d.interp_state === INTERP_IDLE) {
        finishRun();
      }
    } catch(e) { /* silent */ }
  }

  // Start/Stepボタンをキャプチャ（伝播は止めない）
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('button');
    if (!btn) return;
    const txt = btn.textContent.trim();
    if (txt !== 'Start' && txt !== 'Step') return;

    // 既に実行中ならリセット
    stopCheck();
    _runStart = Date.now();
    const el = timerEl();
    el.style.color = '#cdd6f4';
    el.style.display = 'block';
    el.textContent = '⏱ 0m 00s';

    // 実行中のみ500msポーリング
    _checkIv = setInterval(checkState, 500);
  }, true);

  // ── 初期化 ────────────────────────────────────────────────────
  function init() {
    addSettingsBtn();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
