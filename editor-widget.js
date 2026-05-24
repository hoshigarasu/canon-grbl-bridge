(function () {
  'use strict';

  function openEditor() {
    fetch('/active-file')
      .then(r => r.json())
      .then(d => {
        const f = d.path;
        if (!f) { alert('No file loaded'); return; }
        window.open('/editor?file=' + encodeURIComponent(f), 'gcode-editor', 'width=960,height=720,resizable=yes');
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

  function addSettingsBtn() {
    if (document.getElementById('_grbl_btn')) return;
    const btn = document.createElement('button');
    btn.id = '_grbl_btn';
    btn.textContent = '⚙';
    btn.title = 'grblHAL Settings';
    btn.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:9999;width:36px;height:36px;border-radius:50%;background:#313244;color:#cdd6f4;border:1px solid #45475a;font-size:16px;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.4)';
    btn.addEventListener('click', () => window.open('/grbl-settings', 'grbl-settings', 'width=900,height=700,resizable=yes'));
    document.body.appendChild(btn);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', addSettingsBtn);
  } else {
    addSettingsBtn();
  }
})();
