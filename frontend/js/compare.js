// 检索对比视图：关键词 vs 向量 vs 混合(RRF)。
(function () {
  const view = document.getElementById('view-compare');

  function render() {
    view.innerHTML = `
      <h2>⚖️ 检索方式对比</h2>
      <div class="graph-toolbar">
        <input id="compare-input" placeholder="输入问题，如：缓考怎么申请" style="flex:1" />
        <button class="btn" id="compare-btn">对比</button>
      </div>
      <div class="compare-cols">
        <div class="compare-col"><h3>关键词检索</h3><div id="compare-keyword"></div></div>
        <div class="compare-col"><h3>向量检索</h3><div id="compare-vector"></div></div>
        <div class="compare-col"><h3>混合 (RRF)</h3><div id="compare-hybrid"></div></div>
      </div>`;
  }

  function renderHits(box, hits, withScore) {
    if (!hits || !hits.length) { box.innerHTML = '<div class="muted">无命中</div>'; return; }
    box.innerHTML = hits.map((h, i) => {
      const loc = h.article_no ? `第${h.article_no}条` : '';
      const ch = h.chapter ? `${h.chapter} ` : '';
      const score = withScore && h.score !== undefined ? `（${h.score}）` : '';
      return `<div class="compare-hit"><b>${i + 1}. 《${escapeHtml(h.doc_title)}》 ${escapeHtml(ch + loc)}</b>${score}<br/>
              <span class="muted">${escapeHtml((h.content || '').slice(0, 60))}…</span></div>`;
    }).join('');
  }

  async function run() {
    const q = document.getElementById('compare-input').value.trim();
    if (!q) return;
    try {
      const r = await API.searchCompare(q);
      renderHits(document.getElementById('compare-keyword'), r.keyword, true);
      renderHits(document.getElementById('compare-vector'), r.vector, true);
      renderHits(document.getElementById('compare-hybrid'), r.hybrid, true);
    } catch (e) {
      document.getElementById('compare-keyword').innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
    }
  }

  window.addEventListener('view:changed', (e) => { if (e.detail === 'compare') render(); });
  view.addEventListener('click', (e) => { if (e.target.id === 'compare-btn') run(); });
  view.addEventListener('keydown', (e) => { if (e.target.id === 'compare-input' && e.key === 'Enter') run(); });

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  if (view && !view.innerHTML.trim()) render();
})();
