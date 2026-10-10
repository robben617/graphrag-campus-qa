// 系统信息视图：健康检查 + 数据统计 + 时效全景。
(function () {
  const view = document.getElementById('view-about');

  function render() {
    view.innerHTML = `
      <h2>ℹ️ 系统信息</h2>
      <div class="card" style="margin-bottom:14px">
        <h3 style="margin-top:0">健康状态</h3>
        <div class="health-grid" id="health-grid"></div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <h3 style="margin-top:0">数据统计</h3>
        <div id="stats-box"></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">文档时效</h3>
        <div id="versioning-box"></div>
      </div>`;
  }

  async function load() {
    try {
      const h = await API.health();
      document.getElementById('health-grid').innerHTML = [
        ['状态', h.status === 'ok' ? '正常' : '空库'],
        ['版本', h.version],
        ['LLM', h.llm_configured ? `已配置（${h.llm_model}）` : '未配置（原文摘编模式）'],
        ['Embedding API', h.embedding_configured ? '已配置' : '未配置'],
        ['本地 Embedding', h.local_embedding === 'available' ? 'BGE 已就绪' : (h.local_embedding === 'off' ? '已关闭' : '不可用')],
        ['本地 Reranker', h.local_reranker === 'available' ? 'BGE 已就绪' : (h.local_reranker === 'off' ? '已关闭' : '不可用')],
        ['向量条目', h.vector_count],
      ].map(([k, v]) => `<div class="stat-card"><div class="lbl">${k}</div><div class="num" style="font-size:15px">${escapeHtml(String(v))}</div></div>`).join('');
    } catch (e) { /* ignore */ }

    try {
      const s = await API.stats();
      const st = s.stats;
      const rows = [
        ['源文件', st.documents], ['手册', st.handbook], ['通知', st.notices], ['条款', st.clauses],
        ['分类', st.categories], ['实体', st.entities], ['关系', st.relations], ['FAQ', st.faq], ['题库', st.quiz],
      ];
      document.getElementById('stats-box').innerHTML = rows.map(([k, v]) =>
        `<div class="kv"><span class="k">${k}</span><span>${v}</span></div>`).join('');
    } catch (e) { /* ignore */ }

    try {
      const v = await API.versioning();
      document.getElementById('versioning-box').innerHTML = (v.documents || []).map((d) => {
        const badge = d.status === '现行有效' ? 'ok' : (d.status === '已被替代' ? 'warn' : 'refuse');
        return `<div class="kv"><span class="k">《${escapeHtml(d.title)}》</span>
          <span><span class="badge ${badge}">${escapeHtml(d.status)}</span>${d.superseded_by ? '<span class="muted">已被替代</span>' : ''}</span></div>`;
      }).join('') || '<div class="muted">无数据</div>';
    } catch (e) { /* ignore */ }
  }

  window.addEventListener('view:changed', (e) => { if (e.detail === 'about') { render(); load(); } });

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  if (view && !view.innerHTML.trim()) render();
})();
