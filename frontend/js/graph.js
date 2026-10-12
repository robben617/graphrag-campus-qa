// 知识图谱视图：统计 + 节点搜索 + 邻居探索 + SVG 力导向可视化 + 关系路径展开。
//
// 关系路径（relationPaths）此前在 api.js 里定义却从未被调用，对应设计文档
// §6 挑战版#14「来源条款 + 关系路径 + 原文片段全量可展开」缺失的一半。
(function () {
  const view = document.getElementById('view-graph');
  let currentSubgraph = null;
  let nodeNames = new Map();   // 节点 id → 显示名，供关系路径渲染使用
  let gen = 0;                 // 绘制代次：防止先发的慢请求覆盖后发的结果

  function render() {
    view.innerHTML = `
      <h2>🕸️ 知识图谱</h2>
      <div class="graph-stats-grid" id="graph-stats"></div>
      <div class="graph-toolbar">
        <input id="graph-search" placeholder="搜索节点（如：教务处 / 奖学金 / 缓考）" />
        <button class="btn" id="graph-search-btn">搜索</button>
        <button class="btn btn-ghost" id="graph-all-btn">全图</button>
        <label class="muted">深度
          <select id="graph-depth"><option value="1">1 跳</option><option value="2">2 跳</option></select>
        </label>
      </div>
      <div class="graph-main">
        <div id="graph-canvas">
          <div class="node-tooltip" id="node-tooltip"></div>
        </div>
        <aside class="path-panel">
          <h3>关系路径</h3>
          <div class="muted" style="font-size:12px;margin-bottom:8px">点击图中任一节点，查看它到归口部门 / 办事流程的可解释路径。</div>
          <div id="path-list"><div class="muted">未选中节点</div></div>
        </aside>
      </div>
      <div class="muted" id="graph-hint" style="margin-top:8px"></div>`;
  }

  function loadStats() {
    API.graphStats().then((s) => {
      const cards = [
        { num: s.nodes, lbl: '实体节点' },
        { num: s.relations, lbl: '关系' },
        { num: (s.relation_types || []).length, lbl: '关系类型' },
        { num: (s.node_kinds || []).length, lbl: '节点类型' },
      ];
      const el = document.getElementById('graph-stats');
      if (el) el.innerHTML = cards.map((c) =>
        `<div class="stat-card"><div class="num">${c.num}</div><div class="lbl">${c.lbl}</div></div>`).join('');
      const hint = document.getElementById('graph-hint');
      if (hint) hint.textContent = '关系类型：' + (s.relation_types || []).map(t => `${t.rel}(${t.c})`).join('、');
    }).catch((e) => showErr(e));
  }

  async function drawSubgraph(sub) {
    currentSubgraph = sub;
    const canvas = document.getElementById('graph-canvas');
    if (!canvas) return;
    canvas.querySelectorAll('svg').forEach((n) => n.remove());
    if (!sub || !sub.nodes || !sub.nodes.length) {
      canvas.insertAdjacentHTML('beforeend', '<div class="muted" style="padding:20px">无数据</div>');
      return;
    }
    (sub.nodes || []).forEach((n) => nodeNames.set(n.id, n.name));
    drawForce(canvas, sub.nodes, sub.edges);
  }

  async function searchAndDraw(q) {
    const myGen = ++gen;
    try {
      const nodes = await API.graphNodes(q);
      if (myGen !== gen) return;
      if (!nodes.length) {
        document.getElementById('graph-hint').textContent = '未找到匹配节点';
        return;
      }
      const first = nodes[0];
      const sub = await API.nodeNeighbors(first.id, depth());
      if (myGen !== gen) return;
      await drawSubgraph(sub);
      loadPaths(first.id);
    } catch (e) { if (myGen === gen) showErr(e); }
  }

  async function drawAll() {
    const myGen = ++gen;
    try {
      const sub = await API.graphSubgraph(null, depth());
      if (myGen !== gen) return;
      await drawSubgraph(sub);
      const list = document.getElementById('path-list');
      if (list) list.innerHTML = '<div class="muted">未选中节点</div>';
    } catch (e) { if (myGen === gen) showErr(e); }
  }

  // 从问答引用跳转过来：直接按节点 id 画邻居（doc_id 也支持，_resolve_node 会兜住）
  async function focusNode(nodeId) {
    const myGen = ++gen;
    try {
      const sub = await API.nodeNeighbors(nodeId, depth());
      if (myGen !== gen) return;
      await drawSubgraph(sub);
      loadPaths(nodeId);
      const hint = document.getElementById('graph-hint');
      if (hint) {
        const root = (sub.nodes || []).find((n) => n.id === nodeId);
        hint.textContent = root ? `已定位：${root.name}` : '已定位该节点';
      }
    } catch (e) {
      if (myGen === gen) showErr(e);
    }
  }

  // ---------- 关系路径 ----------
  function nameOf(id) {
    return nodeNames.get(id) || id;
  }

  async function loadPaths(nodeId) {
    const box = document.getElementById('path-list');
    if (!box) return;
    box.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const r = await API.relationPaths(nodeId, 2);
      const paths = r.paths || [];
      if (!paths.length) {
        box.innerHTML = `<div class="muted">该节点暂无到部门/流程的关系路径。</div>
          <div class="muted" style="font-size:12px;margin-top:6px">起点：${escapeHtml(nameOf(nodeId))}</div>`;
        return;
      }
      box.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:6px">起点：${escapeHtml(nameOf(nodeId))}</div>` +
        paths.slice(0, 12).map((p) => {
          const steps = p.filter((s) => s.rel);  // 去掉起点占位
          const line = steps.map((s) => `${escapeHtml(nameOf(s.from))} <span class="rel-chip">${escapeHtml(s.rel)}</span> ${escapeHtml(nameOf(s.to))}`).join('<br/>↳ ');
          return `<div class="path-item">${line}${steps.length && steps[steps.length - 1].note ? `<div class="muted" style="font-size:12px">${escapeHtml(steps[steps.length - 1].note)}</div>` : ''}</div>`;
        }).join('') +
        (paths.length > 12 ? `<div class="muted" style="font-size:12px">共 ${paths.length} 条，已显示前 12 条</div>` : '');
    } catch (e) {
      box.innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
    }
  }

  function depth() {
    const el = document.getElementById('graph-depth');
    return parseInt((el && el.value) || '1', 10);
  }

  function showErr(e) {
    const hint = document.getElementById('graph-hint');
    if (hint) hint.textContent = '错误：' + e.message;
  }

  // ---------- 事件 ----------
  window.addEventListener('view:changed', (e) => {
    if (e.detail === 'graph') {
      render();
      loadStats();
      drawAll();
    }
  });

  window.addEventListener('graph:focus', async (e) => {
    if (!document.getElementById('graph-canvas')) render();
    await focusNode(e.detail);
  });

  view.addEventListener('click', async (e) => {
    if (e.target.id === 'graph-search-btn') {
      const q = document.getElementById('graph-search').value.trim();
      if (q) await searchAndDraw(q);
    } else if (e.target.id === 'graph-all-btn') {
      await drawAll();
    }
  });
  view.addEventListener('keydown', async (e) => {
    if (e.target.id === 'graph-search' && e.key === 'Enter') {
      const q = e.target.value.trim();
      if (q) await searchAndDraw(q);
    }
  });
  view.addEventListener('change', async (e) => {
    if (e.target.id === 'graph-depth') {
      const q = document.getElementById('graph-search').value.trim();
      if (q) await searchAndDraw(q); else await drawAll();
    }
  });

  // ===== 轻量力导向布局（无外部依赖，SVG）=====
  function drawForce(container, nodes, edges) {
    const W = container.clientWidth || 800;
    const H = container.clientHeight || 520;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    container.appendChild(svg);

    const kindColor = { department: '#2563eb', entity: '#16a34a', process: '#d97706', document: '#7c3aed' };
    const simNodes = nodes.map((n, i) => ({
      id: n.id, name: n.name, kind: n.kind, category: n.category,
      x: W / 2 + Math.cos(i * 2.4) * 120, y: H / 2 + Math.sin(i * 2.4) * 120,
      vx: 0, vy: 0,
    }));
    const nodeMap = new Map(simNodes.map((n) => [n.id, n]));
    const simEdges = edges.filter((e) => nodeMap.has(e.src) && nodeMap.has(e.dst))
      .map((e) => ({ ...e, s: nodeMap.get(e.src), t: nodeMap.get(e.dst) }));

    // 力导向迭代
    for (let iter = 0; iter < 300; iter++) {
      for (const e of simEdges) {
        const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
        const d = Math.max(1, Math.hypot(dx, dy));
        const f = (d - 80) * 0.01;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
      }
      for (let i = 0; i < simNodes.length; i++) {
        for (let j = i + 1; j < simNodes.length; j++) {
          const a = simNodes[i], b = simNodes[j];
          const dx = b.x - a.x, dy = b.y - a.y;
          const d = Math.max(1, Math.hypot(dx, dy));
          const f = 400 / (d * d);
          const fx = (dx / d) * f, fy = (dy / d) * f;
          a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
        }
      }
      for (const n of simNodes) {
        n.vx += (W / 2 - n.x) * 0.001;
        n.vy += (H / 2 - n.y) * 0.001;
        n.vx *= 0.85; n.vy *= 0.85;
        n.x += n.vx; n.y += n.vy;
        n.x = Math.max(20, Math.min(W - 20, n.x));
        n.y = Math.max(20, Math.min(H - 20, n.y));
      }
    }

    const lineGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const nodeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    svg.appendChild(lineGroup); svg.appendChild(nodeGroup);

    for (const e of simEdges) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', e.s.x); line.setAttribute('y1', e.s.y);
      line.setAttribute('x2', e.t.x); line.setAttribute('y2', e.t.y);
      line.setAttribute('stroke', e.rel === 'SUPERSEDES' ? '#f59e0b' : '#cbd5e1');
      line.setAttribute('stroke-width', e.rel === 'SUPERSEDES' ? 2 : 1);
      lineGroup.appendChild(line);
      // 关系标签
      const lx = (e.s.x + e.t.x) / 2, ly = (e.s.y + e.t.y) / 2;
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', lx); label.setAttribute('y', ly);
      label.setAttribute('font-size', '8'); label.setAttribute('fill', '#94a3b8');
      label.textContent = e.rel;
      lineGroup.appendChild(label);
    }

    const tooltip = document.getElementById('node-tooltip');
    for (const n of simNodes) {
      const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', n.x); circle.setAttribute('cy', n.y);
      circle.setAttribute('r', n.kind === 'department' ? 13 : 10);
      circle.setAttribute('fill', kindColor[n.kind] || '#64748b');
      circle.setAttribute('stroke', '#fff'); circle.setAttribute('stroke-width', 1.5);
      g.appendChild(circle);
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', n.x); label.setAttribute('y', n.y + 22);
      label.setAttribute('font-size', '10'); label.setAttribute('fill', '#334155');
      label.setAttribute('text-anchor', 'middle');
      label.textContent = n.name;
      g.appendChild(label);
      g.style.cursor = 'pointer';
      g.addEventListener('mouseenter', (ev) => {
        if (!tooltip) return;
        tooltip.style.display = 'block';
        tooltip.style.left = (ev.offsetX + 12) + 'px';
        tooltip.style.top = (ev.offsetY + 12) + 'px';
        tooltip.innerHTML = `<b>${escapeHtml(n.name)}</b><br/>类型：${escapeHtml(n.kind)}<br/>分类：${escapeHtml(n.category || '—')}`;
      });
      g.addEventListener('mousemove', (ev) => {
        if (!tooltip) return;
        tooltip.style.left = (ev.offsetX + 12) + 'px';
        tooltip.style.top = (ev.offsetY + 12) + 'px';
      });
      g.addEventListener('mouseleave', () => { if (tooltip) tooltip.style.display = 'none'; });
      g.addEventListener('click', async () => {
        const myGen = ++gen;
        const sub = await API.nodeNeighbors(n.id, depth());
        if (myGen !== gen) return;
        await drawSubgraph(sub);
        loadPaths(n.id);
      });
      nodeGroup.appendChild(g);
    }
  }

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  if (view && !view.innerHTML.trim()) render();
})();
