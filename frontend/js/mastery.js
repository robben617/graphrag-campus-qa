// 掌握标记视图：知识点（实体）标记「已掌握 / 未掌握」，未掌握项会进入问答提示。
// 对应设计文档 §6 标准版#11、验收 AT-07；后端接口此前已实现但前端一直无入口。
(function () {
  const view = document.getElementById('view-mastery');
  const KIND_LABEL = { entity: '领域对象', process: '办事流程', department: '归口部门', document: '文件' };
  let allCandidates = [];
  let filterStatus = '';   // '' | 'mastered' | 'unmastered'
  let keyword = '';

  function render() {
    view.innerHTML = `
      <h2>🎯 掌握标记</h2>
      <div class="card" style="margin-bottom:14px">
        <div class="muted" style="line-height:1.7">
          把知识点标成「已掌握 / 未掌握」。<b>未掌握</b>的项会在你提问相关问题时被提示，方便针对性复习；
          标记按身份隔离，切换身份后各看各的。
        </div>
        <div class="mastery-stat" id="mastery-stat"></div>
      </div>
      <div class="graph-toolbar">
        <input id="mastery-search" placeholder="搜索知识点（如：奖学金 / 缓考 / 教务处）" style="flex:1" />
        <select id="mastery-filter">
          <option value="">全部</option>
          <option value="unmastered">仅未掌握</option>
          <option value="mastered">仅已掌握</option>
        </select>
        <button class="btn" id="mastery-search-btn">搜索</button>
        <button class="btn btn-ghost" id="mastery-refresh">刷新</button>
      </div>
      <div id="mastery-list"></div>`;
  }

  async function loadCandidates() {
    const box = document.getElementById('mastery-list');
    if (!box) return;
    box.innerHTML = '<div class="muted">加载中…</div>';
    try {
      allCandidates = await API.masteryCandidates(keyword);
      paint();
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}${e.hint ? '<br/>' + escapeHtml(e.hint) : ''}</div>`;
    }
  }

  function paint() {
    const box = document.getElementById('mastery-list');
    const items = allCandidates.filter((c) => !filterStatus || c.status === filterStatus);
    const mastered = allCandidates.filter((c) => c.status === 'mastered').length;
    const unmastered = allCandidates.filter((c) => c.status === 'unmastered').length;
    const stat = document.getElementById('mastery-stat');
    if (stat) {
      stat.innerHTML = `
        <span class="badge ok">已掌握 ${mastered}</span>
        <span class="badge warn">未掌握 ${unmastered}</span>
        <span class="badge">未标记 ${allCandidates.length - mastered - unmastered}</span>`;
    }
    if (!items.length) {
      box.innerHTML = '<div class="card">没有符合条件的知识点。</div>';
      return;
    }
    box.innerHTML = items.map((c) => {
      const cur = c.status || '';
      const cls = cur === 'mastered' ? 'mastery-row mastered' : (cur === 'unmastered' ? 'mastery-row unmastered' : 'mastery-row');
      const state = cur === 'mastered' ? '<span class="badge ok">已掌握</span>'
        : (cur === 'unmastered' ? '<span class="badge warn">未掌握</span>' : '<span class="badge">未标记</span>');
      return `<div class="${cls}" data-id="${escapeHtml(c.item_id)}">
        <div class="mastery-main">
          <div class="mastery-name">${escapeHtml(c.name)} ${state}
            <span class="muted" style="font-weight:400;font-size:12px">${escapeHtml(KIND_LABEL[c.kind] || c.kind || '')}${c.category ? ' · ' + escapeHtml(c.category) : ''}</span>
          </div>
          <div class="muted" style="font-size:12px">${escapeHtml(c.description || '')}</div>
        </div>
        <div class="mastery-actions">
          <button class="btn btn-mini" data-act="mastered">已掌握</button>
          <button class="btn btn-mini btn-ghost" data-act="unmastered">未掌握</button>
          <button class="btn btn-mini btn-ghost" data-act="clear" ${cur ? '' : 'disabled'}>清除</button>
        </div>
      </div>`;
    }).join('');
  }

  async function setStatus(id, status) {
    const item = allCandidates.find((c) => c.item_id === id);
    if (!item) return;
    try {
      if (status === 'clear') {
        await API.masteryRemove(id);
        item.status = null;
      } else {
        await API.masterySet(id, item.item_type || 'entity', item.name, status);
        item.status = status;
      }
      paint();
    } catch (e) {
      window.alert('标记失败：' + e.message + (e.hint ? '\n' + e.hint : ''));
    }
  }

  view.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (btn) {
      const row = btn.closest('.mastery-row');
      setStatus(row.dataset.id, btn.dataset.act);
      return;
    }
    if (e.target.id === 'mastery-search-btn') { keyword = document.getElementById('mastery-search').value.trim(); loadCandidates(); }
    else if (e.target.id === 'mastery-refresh') loadCandidates();
  });
  view.addEventListener('keydown', (e) => {
    if (e.target.id === 'mastery-search' && e.key === 'Enter') { keyword = e.target.value.trim(); loadCandidates(); }
  });
  view.addEventListener('change', (e) => {
    if (e.target.id === 'mastery-filter') { filterStatus = e.target.value; paint(); }
  });

  window.addEventListener('view:changed', (e) => { if (e.detail === 'mastery') { render(); loadCandidates(); } });
  window.addEventListener('identity:changed', () => {
    if (document.getElementById('mastery-list')) loadCandidates();
  });

  if (view && !view.innerHTML.trim()) render();
})();
