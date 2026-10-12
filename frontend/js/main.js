// 视图路由：左侧菜单切换 + URL hash 同步。
//
// hash 同步带来的实际好处：
// - 刷新页面停留在当前视图，不会被打回问答页；
// - 可以直接把「知识图谱」「维护端」等页面链接发给别人；
// - 浏览器前进/后退可用。
(function () {
  const views = ['chat', 'graph', 'quiz', 'mastery', 'compare', 'admin', 'about'];

  function switchView(name, syncHash) {
    if (views.indexOf(name) === -1) return;
    views.forEach((v) => {
      const el = document.getElementById('view-' + v);
      if (el) el.classList.toggle('active', v === name);
    });
    document.querySelectorAll('.nav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === name);
    });
    if (syncHash !== false && location.hash.slice(1) !== name) {
      // 用 replaceState：切换视图不该在历史里堆一串条目，避免"返回"要点很多次
      history.replaceState(null, '', '#' + name);
    }
    window.dispatchEvent(new CustomEvent('view:changed', { detail: name }));
  }

  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });

  window.addEventListener('hashchange', () => {
    const h = location.hash.slice(1);
    if (views.indexOf(h) !== -1) switchView(h, false);
  });

  window.App = { switchView };
  window.escapeHtml = escapeHtml;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // 初始视图恢复：放在 DOMContentLoaded 里，确保各视图脚本都已注册 view:changed 监听
  document.addEventListener('DOMContentLoaded', () => {
    const h = location.hash.slice(1);
    if (views.indexOf(h) !== -1 && h !== 'chat') switchView(h, false);
  });
})();
