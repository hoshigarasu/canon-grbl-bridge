(function () {
  'use strict';

  function openEditor() {
    fetch('/active-file')
      .then(r => r.json())
      .then(d => {
        const f = d.path;
        if (!f) { alert('No file loaded'); return; }
        window.open(
          '/editor?file=' + encodeURIComponent(f),
          'gcode-editor',
          'width=960,height=720,resizable=yes'
        );
      })
      .catch(e => alert('Error: ' + e));
  }

  // Editボタンクリックをキャプチャフェーズで横取り → ポップアップに差し替え
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.textContent.trim() !== 'Edit') return;
    e.stopPropagation();
    openEditor();
  }, true);
})();
