// 知识图谱视图：统计 + 节点搜索 + 邻居探索 + SVG 力导向可视化。
(function () {
  const view = document.getElementById('view-graph');
  let currentSubgraph = null;

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
      <div id="graph-canvas">
        <div class="node-tooltip" id="node-tooltip"></div>
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
      document.getElementById('graph-stats').innerHTML = cards.map((c) =>
        `<div class="stat-card"><div class="num">${c.num}</div><div class="lbl">${c.lbl}</div></div>`).join('');
      const types = (s.relation_types || []).map(t => `${t.rel}(${t.c})`).join('、');
      document.getElementById('graph-hint').textContent = '关系类型：' + types;
    }).catch((e) => showErr(e));
  }

  async function drawSubgraph(sub) {
    currentSubgraph = sub;
    const canvas = document.getElementById('graph-canvas');
    canvas.querySelectorAll('svg').forEach((n) => n.remove());
    if (!sub || !sub.nodes || !sub.nodes.length) {
      canvas.innerHTML += '<div class="muted" style="padding:20px">无数据</div>';
      return;
    }
    drawForce(canvas, sub.nodes, sub.edges);
  }

  async function searchAndDraw(q) {
    try {
      const nodes = await API.graphNodes(q);
      if (!nodes.length) {
        document.getElementById('graph-hint').textContent = '未找到匹配节点';
        return;
      }
      const first = nodes[0];
      const sub = await API.nodeNeighbors(first.id, depth());
      await drawSubgraph(sub);
    } catch (e) { showErr(e); }
  }

  async function drawAll() {
    try {
      const sub = await API.graphSubgraph(null, depth());
      await drawSubgraph(sub);
    } catch (e) { showErr(e); }
  }

  function depth() { return parseInt(document.getElementById('graph-depth').value || '1', 10); }

  function showErr(e) {
    document.getElementById('graph-hint').textContent = '错误：' + e.message;
  }

  window.addEventListener('view:changed', (e) => {
    if (e.detail === 'graph') {
      render();
      loadStats();
      drawAll();
    }
  });

  // 事件委托
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
      line.setAttribute('stroke', '#cbd5e1'); line.setAttribute('stroke-width', 1);
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
        tooltip.style.display = 'block';
        tooltip.style.left = (ev.offsetX + 12) + 'px';
        tooltip.style.top = (ev.offsetY + 12) + 'px';
        tooltip.innerHTML = `<b>${escapeHtml(n.name)}</b><br/>类型：${escapeHtml(n.kind)}<br/>分类：${escapeHtml(n.category || '—')}`;
      });
      g.addEventListener('mousemove', (ev) => {
        tooltip.style.left = (ev.offsetX + 12) + 'px';
        tooltip.style.top = (ev.offsetY + 12) + 'px';
      });
      g.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
      g.addEventListener('click', async () => {
        const sub = await API.nodeNeighbors(n.id, depth());
        await drawSubgraph(sub);
      });
      nodeGroup.appendChild(g);
    }
  }

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  if (view && !view.innerHTML.trim()) render();
})();
