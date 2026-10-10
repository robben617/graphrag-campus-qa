// 视图路由：左侧菜单切换。
(function () {
  const views = ['chat', 'graph', 'quiz', 'compare', 'about'];

  function switchView(name) {
    views.forEach((v) => {
      const el = document.getElementById('view-' + v);
      if (el) el.classList.toggle('active', v === name);
    });
    document.querySelectorAll('.nav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === name);
    });
    window.dispatchEvent(new CustomEvent('view:changed', { detail: name }));
  }

  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });

  window.App = { switchView };
  window.escapeHtml = escapeHtml;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
})();
