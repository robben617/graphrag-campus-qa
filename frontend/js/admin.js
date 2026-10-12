// 老师维护端视图：增量入库（通知）+ 质量评测 + 文档时效清单。
// 对应设计文档 §6 挑战版#16（质量评测）、#17（增量入库 / 老师维护入口）。
// 需要 teacher 或 admin 身份；学生身份下会给出明确引导而不是静默失败。
(function () {
  const view = document.getElementById('view-admin');
  const CATEGORIES = ['学籍管理', '违纪处分', '奖助学金', '宿舍管理', '教务教学'];

  function render() {
    view.innerHTML = `
      <h2>🛠️ 维护端</h2>
      <div id="admin-role-hint"></div>

      <div class="card" style="margin-bottom:16px">
        <h3 style="margin-top:0">① 增量入库（新通知）</h3>
        <div class="muted" style="margin-bottom:12px;line-height:1.7">
          粘贴通知全文并填写元数据。入库后会按分类自动检测可替代的旧文件，
          建立 <b>SUPERSEDES</b> 关系并把旧文件置为「已被替代」——旧版默认不再参与检索。
        </div>
        <div class="form-grid">
          <label>标题 *<input id="ai-title" placeholder="如：关于开展2026年优秀学生奖学金评定工作的通知" /></label>
          <label>分类 *<input id="ai-category" list="ai-cat-list" placeholder="选择或输入分类" />
            <datalist id="ai-cat-list">${CATEGORIES.map((c) => `<option value="${c}"></option>`).join('')}</datalist>
          </label>
          <label>发文部门<input id="ai-issuer" placeholder="如：学生资助管理中心" /></label>
          <label>版本<input id="ai-version" placeholder="如：2026" /></label>
          <label>发布日期<input id="ai-publish" type="date" /></label>
          <label>生效日期<input id="ai-effective" type="date" /></label>
          <label>适用对象<input id="ai-applies" placeholder="如：全日制在校本科生" /></label>
          <label>原始文件名<input id="ai-filename" placeholder="如：2026年奖学金评定通知.txt" /></label>
        </div>
        <label style="display:block;margin-top:10px">通知全文 *
          <textarea id="ai-text" style="width:100%;min-height:140px;margin-top:6px" placeholder="粘贴通知正文，段落之间用空行分隔"></textarea>
        </label>
        <label class="checkbox-row"><input type="checkbox" id="ai-auto" checked /> 自动检测并建立 SUPERSEDES 关系</label>
        <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
          <button class="btn" id="ai-submit">入库</button>
          <span class="muted" id="ai-status"></span>
        </div>
        <div id="ai-result" style="margin-top:12px"></div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <h3 style="margin-top:0">② 质量评测</h3>
        <div class="muted" style="margin-bottom:12px">对 55 题评测集跑一遍检索，报告 Top-K 召回、事实一致性与引用覆盖率。</div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <label class="muted">Top K <input id="ev-topk" type="number" value="5" min="1" max="20" style="width:80px" /></label>
          <button class="btn" id="ev-run">运行评测</button>
          <button class="btn btn-ghost" id="ev-latest">读取上次结果</button>
          <span class="muted" id="ev-status"></span>
        </div>
        <div id="ev-result" style="margin-top:12px"></div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <h3 style="margin-top:0">③ 文档时效清单</h3>
        <div class="muted" style="margin-bottom:10px">现行有效 / 已被替代 / 已废止 三类状态与替代指向。</div>
        <div id="ver-list"></div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">④ 用户与权限</h3>
        <div class="muted" style="margin-bottom:10px">仅管理员可见。会话、掌握标记、答题记录均按用户隔离。</div>
        <button class="btn btn-ghost" id="usr-load">加载用户列表</button>
        <div id="usr-list" style="margin-top:12px"></div>
      </div>`;
  }

  async function loadUsers() {
    const box = document.getElementById('usr-list');
    box.innerHTML = '<div class="muted">加载中…</div>';
    try {
      const users = await API.listUsers();
      box.innerHTML = users.length
        ? users.map((u) => `<div class="kv">
            <span class="k">${escapeHtml(u.username)}</span>
            <span><span class="badge">${escapeHtml(u.role)}</span>
              <span class="muted" style="font-size:12px">${escapeHtml((u.created_at || '').slice(0, 16))}</span></span>
          </div>`).join('')
        : '<div class="muted">暂无用户</div>';
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}${e.hint ? '<br/>' + escapeHtml(e.hint) : ''}</div>`;
    }
  }

  function statusBadge(s) {
    const cls = s === '现行有效' ? 'ok' : (s === '已被替代' ? 'warn' : 'refuse');
    return `<span class="badge ${cls}">${escapeHtml(s)}</span>`;
  }

  async function loadRoleHint() {
    const box = document.getElementById('admin-role-hint');
    if (!box) return;
    try {
      const me = await API.me();
      const can = me.role === 'teacher' || me.role === 'admin';
      box.innerHTML = can
        ? `<div class="card" style="margin-bottom:16px;border-color:#bbf7d0;background:#f0fdf4">
            当前身份：<b>${escapeHtml(me.username)}</b>（${escapeHtml(me.role)}）— 具备维护权限。
           </div>`
        : `<div class="card" style="margin-bottom:16px;border-color:#fde68a;background:#fffbeb">
            当前身份：<b>${escapeHtml(me.username)}</b>（${escapeHtml(me.role)}）— <b>权限不足</b>。
            请在左下角切换到「老师」或「管理员」身份后再使用本页功能。
           </div>`;
      return can;
    } catch (e) { /* 忽略 */ }
    return false;
  }

  // ---------- 增量入库 ----------
  async function submitIngest() {
    const status = document.getElementById('ai-status');
    const box = document.getElementById('ai-result');
    const payload = {
      title: document.getElementById('ai-title').value.trim(),
      category: document.getElementById('ai-category').value.trim(),
      issuer: document.getElementById('ai-issuer').value.trim(),
      version: document.getElementById('ai-version').value.trim(),
      publish_date: document.getElementById('ai-publish').value.trim(),
      effective_date: document.getElementById('ai-effective').value.trim(),
      applies_to: document.getElementById('ai-applies').value.trim(),
      source_filename: document.getElementById('ai-filename').value.trim(),
      text: document.getElementById('ai-text').value,
      auto_supersede: document.getElementById('ai-auto').checked,
    };
    if (!payload.title || !payload.category || !payload.text.trim()) {
      box.innerHTML = '<div class="error-text">标题、分类、通知全文为必填项。</div>';
      return;
    }
    status.textContent = '入库中…（含向量索引构建）';
    box.innerHTML = '';
    try {
      const r = await API.ingestNotice(payload);
      const sup = (r.superseded || []).length
        ? `<div class="cite" style="border-color:var(--warn);background:#fffbeb">已将旧文件置为「已被替代」：${escapeHtml(r.superseded.join('、'))}</div>`
        : '<div class="muted" style="margin-top:6px">未检测到需要替代的同类旧文件。</div>';
      box.innerHTML = `<div class="card" style="background:#f0fdf4;border-color:#bbf7d0">
        ✅ 入库成功：<b>${escapeHtml(r.title)}</b><br/>
        <span class="muted">doc_id：${escapeHtml(r.doc_id)} · 条款块：${r.clauses} 段${(r.candidates || []).length ? ' · 候选旧版：' + escapeHtml(r.candidates.join('、')) : ''}</span>
      </div>${sup}`;
      // 入库后时效清单会变，顺手刷新
      loadVersioning();
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}${e.hint ? '<br/>' + escapeHtml(e.hint) : ''}</div>`;
    } finally {
      status.textContent = '';
    }
  }

  // ---------- 评测 ----------
  function paintEval(r) {
    const box = document.getElementById('ev-result');
    if (!r || r.exists === false) {
      box.innerHTML = '<div class="muted">暂无评测记录，点「运行评测」生成。</div>';
      return;
    }
    const pct = (v) => (typeof v === 'number' ? (v * 100).toFixed(1) + '%' : v);
    box.innerHTML = `<div class="graph-stats-grid">
        <div class="stat-card"><div class="num">${pct(r.recall_at_k)}</div><div class="lbl">Recall@${r.top_k || 5}</div></div>
        <div class="stat-card"><div class="num">${pct(r.factuality)}</div><div class="lbl">事实一致性</div></div>
        <div class="stat-card"><div class="num">${pct(r.citation_coverage)}</div><div class="lbl">引用覆盖率</div></div>
        <div class="stat-card"><div class="num">${r.total ?? '—'}</div><div class="lbl">评测题数</div></div>
      </div>
      <div class="muted" style="font-size:12px">时间：${escapeHtml(r.created_at || '—')}</div>
      ${r.details && r.details.length ? `<details style="margin-top:10px"><summary class="muted">逐题明细（${r.details.length} 条）</summary>
        <div style="margin-top:8px">${r.details.map((d, i) => `<div class="kv"><span class="k">${i + 1}. ${escapeHtml(d.question)}</span><span class="muted">recall ${d.recall_at_k ?? d.recall ?? '—'}</span></div>`).join('')}</div>
      </details>` : ''}`;
  }

  async function runEval() {
    const status = document.getElementById('ev-status');
    const box = document.getElementById('ev-result');
    const topk = parseInt(document.getElementById('ev-topk').value || '5', 10);
    status.textContent = '评测运行中…（55 题检索，可能需 1~2 分钟）';
    box.innerHTML = '';
    try {
      const r = await API.evalRun(topk);
      paintEval(r);
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}${e.hint ? '<br/>' + escapeHtml(e.hint) : ''}</div>`;
    } finally {
      status.textContent = '';
    }
  }

  async function loadLatest() {
    const box = document.getElementById('ev-result');
    try { paintEval(await API.evalLatest()); }
    catch (e) { box.innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; }
  }

  // ---------- 时效清单 ----------
  async function loadVersioning() {
    const box = document.getElementById('ver-list');
    if (!box) return;
    try {
      const v = await API.versioning();
      const docs = v.documents || [];
      box.innerHTML = docs.length
        ? docs.map((d) => `<div class="kv" style="align-items:flex-start">
            <span class="k" style="flex:1">
              《${escapeHtml(d.title)}》
              ${d.version ? `<span class="muted" style="font-size:12px">v${escapeHtml(d.version)}</span>` : ''}
              ${d.effective_date ? `<div class="muted" style="font-size:12px">生效 ${escapeHtml(d.effective_date)}</div>` : ''}
            </span>
            <span style="text-align:right">
              ${statusBadge(d.status)}
              ${d.superseded_by ? `<div class="muted" style="font-size:12px">被 ${escapeHtml(d.superseded_by)} 替代</div>` : ''}
            </span>
          </div>`).join('')
        : '<div class="muted">无数据</div>';
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------- 分类覆盖度 ----------
  async function loadCoverage() {
    const box = document.getElementById('cov-list');
    if (!box) return;
    try {
      const s = await API.stats();
      const cov = s.coverage;
      if (!cov || !cov.detail) {
        box.innerHTML = '<div class="muted">暂无覆盖度数据</div>';
        return;
      }
      const rows = cov.detail.map((c) => `<div class="kv" style="align-items:flex-start">
          <span class="k" style="flex:1">
            ${escapeHtml(c.category)}
            <div class="muted" style="font-size:12px">${escapeHtml((c.description || '').slice(0, 44))}</div>
          </span>
          <span style="text-align:right">
            <span class="badge ${c.covered ? 'ok' : 'refuse'}">${c.covered ? '有原文' : '缺原文'}</span>
            <div class="muted" style="font-size:12px">文档 ${c.documents} · 条款 ${c.clauses} · 实体 ${c.entities}</div>
            ${c.note ? `<div class="muted" style="font-size:12px">${escapeHtml(c.note)}</div>` : ''}
          </span>
        </div>`).join('');
      const warn = cov.missing_count
        ? `；<b>${escapeHtml(cov.missing.join('、'))}</b> 仅有图谱实体、尚无原文条款，相关提问会拒答`
        : '，无空缺';
      box.innerHTML = `<div class="muted" style="margin-bottom:10px">
          ${cov.covered_count}/${cov.total} 个分类有原文支撑${warn}。
        </div>${rows}`;
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
    }
  }

  async function load() {
    render();
    await loadRoleHint();
    await loadVersioning();
    await loadCoverage();
    loadLatest();
  }

  view.addEventListener('click', (e) => {
    if (e.target.id === 'ai-submit') submitIngest();
    else if (e.target.id === 'ev-run') runEval();
    else if (e.target.id === 'ev-latest') loadLatest();
    else if (e.target.id === 'usr-load') loadUsers();
  });

  window.addEventListener('view:changed', (e) => { if (e.detail === 'admin') load(); });
  window.addEventListener('identity:changed', () => {
    if (document.getElementById('ver-list')) { loadRoleHint(); loadVersioning(); loadCoverage(); loadLatest(); }
  });

  if (view && !view.innerHTML.trim()) render();
})();
