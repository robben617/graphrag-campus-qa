// 智能问答视图：左侧会话列表（新建/切换/删除），右侧消息区，支持多轮追问。
// 会话持久化在服务端，刷新页面后可从列表里恢复历史（此前 sessionId 只存内存变量，刷新即丢失）。
(function () {
  const view = document.getElementById('view-chat');
  let sessionId = null;
  let sending = false;
  // 回答方式：extractive（原文摘编，零成本）/ llm（大模型增强）。
  // 默认值由服务端按部署形态给出，用户手动切过后用 localStorage 记住。
  let answerMode = 'extractive';
  let modeKnown = false;
  let lastQuestion = '';

  // 模型选择：自定义模型只存在浏览器本地（localStorage），按身份隔离。
  // 服务端只在本次请求里转发密钥，不落库——这是「用户觉得安全」的关键：
  // 用户自己能看见、能删掉，不必相信任何承诺。
  let deployInfo = null;
  let currentUid = 'anonymous';
  let models = [];
  let activeModelId = 'default';
  let modelPopOpen = false;
  let modelFormOpen = false;

  function render() {
    view.innerHTML = `
      <h2>💬 智能问答</h2>
      <div class="chat-layout">
        <aside class="session-panel">
          <button class="btn" id="session-new" style="width:100%">＋ 新会话</button>
          <div class="session-list" id="session-list"><div class="muted" style="padding:10px 4px">加载中…</div></div>
        </aside>
        <div class="chat-box">
          <div class="messages" id="chat-messages"></div>
          <div class="mode-row" id="mode-row" style="display:none">
            <span class="muted" style="font-size:12px">回答方式</span>
            <div class="mode-seg">
              <button class="mode-btn" data-mode="extractive">原文摘编</button>
              <button class="mode-btn" data-mode="llm">大模型增强</button>
            </div>
            <span class="muted mode-tip" id="mode-tip"></span>
          </div>
          <div class="chat-input-row">
            <textarea id="chat-input" placeholder="输入你的问题，如：奖学金申请需要什么条件？（Ctrl+Enter 发送）"></textarea>
            <button class="btn" id="chat-send">发送</button>
          </div>
          <div class="chat-toolbar">
            <div class="model-wrap">
              <button class="model-pick" id="model-pick">模型</button>
              ${modelPopOpen ? modelPop() : ''}
            </div>
            <span class="muted" id="chat-status"></span>
          </div>
        </div>
      </div>`;
    loadModeInfo();
    loadModelInfo();
  }

  // ---------- 回答方式 ----------
  // 只有确实有可用大模型时才显示切换器：给一个点下去也做不到的选项，比不给更糟。
  async function loadModeInfo() {
    const row = document.getElementById('mode-row');
    if (!row) return;
    let info = null;
    try { info = await API.deployInfo(); } catch (e) { info = null; }
    if (!info || !info.effective) {
      answerMode = 'extractive';
      row.style.display = 'none';
      return;
    }
    if (!modeKnown) {
      const saved = localStorage.getItem('answer_mode');
      answerMode = (saved === 'llm' || saved === 'extractive')
        ? saved
        : (info.default_mode || 'extractive');
      modeKnown = true;
    }
    row.style.display = 'flex';
    paintMode();
  }

  function paintMode() {
    document.querySelectorAll('#mode-row .mode-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.mode === answerMode);
    });
    const tip = document.getElementById('mode-tip');
    if (tip) {
      tip.textContent = answerMode === 'llm'
        ? '大模型归纳成通顺回答，会消耗额度、响应更慢'
        : '直接返回条款原文，零成本、不会改写措辞';
    }
  }

  function setMode(mode) {
    answerMode = mode;
    modeKnown = true;
    try { localStorage.setItem('answer_mode', mode); } catch (e) { /* 隐私模式忽略 */ }
    paintMode();
  }

  // 摘编答案上的「改用大模型」：填回原问题并切模式，由用户自己按发送。
  // 不自动重发，是为了不把同一条问题重复写进会话历史。
  function enhanceLast() {
    if (!lastQuestion) return;
    setMode('llm');
    const input = document.getElementById('chat-input');
    if (input) {
      input.value = lastQuestion;
      input.focus();
    }
    const status = document.getElementById('chat-status');
    if (status) status.textContent = '已切到大模型增强，按发送（或 Ctrl+Enter）再问一次';
  }

  // ---------- 模型选择 ----------
  // 存在 localStorage，按身份隔离：学生不该看到老师配的模型与额度。
  function readModels(uid) {
    try {
      const raw = localStorage.getItem('llm_models_' + uid);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter((m) => m && m.id && m.model) : [];
    } catch (e) {
      return [];
    }
  }

  function writeModels(uid, list) {
    try {
      localStorage.setItem('llm_models_' + uid, JSON.stringify(list));
    } catch (e) { /* 隐私模式下写不进去，本次会话内仍可用 */ }
  }

  async function loadModelInfo() {
    let info = null;
    let me = null;
    try { info = await API.deployInfo(); } catch (e) { info = null; }
    try { me = await API.me(); } catch (e) { me = null; }
    deployInfo = info;
    currentUid = (me && me.user_id) ? me.user_id : 'anonymous';
    models = readModels(currentUid);
    let saved = null;
    try { saved = localStorage.getItem('llm_active_' + currentUid); } catch (e) { saved = null; }
    activeModelId = saved && (saved === 'default' || models.some((m) => m.id === saved)) ? saved : 'default';
    paintModel();
  }

  function activeModel() {
    if (activeModelId === 'default') return null;
    return models.find((m) => m.id === activeModelId) || null;
  }

  function paintModel() {
    const btn = document.getElementById('model-pick');
    if (!btn) return;
    const cur = activeModel();
    const label = cur ? (cur.name || cur.model) : ((deployInfo && deployInfo.model) || '默认模型');
    const usable = !!cur || !!(deployInfo && deployInfo.effective);
    btn.innerHTML = `<span class="dot ${usable ? '' : 'off'}"></span>${escapeHtml(label)}<span class="caret">▾</span>`;
    btn.title = cur ? `自定义模型：${cur.model}` : '使用服务端默认模型';
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (e) { return url || ''; }
  }

  function modelPop() {
    const byok = !!(deployInfo && deployInfo.byok_allowed);
    const isAnon = currentUid === 'anonymous';
    const canCustom = byok && !isAnon;
    const defaultLabel = (deployInfo && deployInfo.model) || '服务端默认';
    return `<div class="model-pop">
      <div class="model-item ${activeModelId === 'default' ? 'active' : ''}" data-model="default">
        <span>服务端默认模型 <span class="meta">${escapeHtml(defaultLabel)}</span></span>
      </div>
      ${models.map((m) => `<div class="model-item ${activeModelId === m.id ? 'active' : ''}" data-model="${escapeHtml(m.id)}">
          <span>${escapeHtml(m.name || m.model)} <span class="meta">${escapeHtml(hostOf(m.base_url))} · ${escapeHtml(m.model)}</span></span>
          <button class="del" data-del-model="${escapeHtml(m.id)}" title="删除">✕</button>
        </div>`).join('')}
      ${canCustom && !modelFormOpen ? '<button class="btn btn-mini btn-ghost" id="model-add" style="width:100%">＋ 添加自定义模型</button>' : ''}
      ${modelFormOpen ? modelForm() : ''}
      ${!byok ? '<div class="model-note">公开演示站点统一使用服务端模型；要用自己的模型请自行部署（SCHOOL_DEPLOY_MODE=selfhost）。</div>' : ''}
      ${byok && isAnon ? '<div class="model-note">匿名访客没有保存配置的位置，请先在右上角创建身份。</div>' : ''}
      <div class="model-note">密钥只保存在本浏览器，不写入服务器数据库；换浏览器需重新填写。</div>
    </div>`;
  }

  function modelForm() {
    return `<div class="model-form">
      <input id="mf-name" placeholder="备注名（可选），如 我的 DeepSeek" />
      <input id="mf-base" placeholder="Base URL，如 https://api.deepseek.com/v1" />
      <input id="mf-model" placeholder="模型名，如 deepseek-chat" />
      <input id="mf-key" type="password" placeholder="API Key（只存本浏览器）" autocomplete="off" />
      <div style="display:flex;gap:6px">
        <button class="btn btn-mini btn-ghost" id="mf-test" style="flex:1">测试</button>
        <button class="btn btn-mini" id="mf-save" style="flex:1">保存</button>
      </div>
      <div id="mf-msg" class="muted" style="font-size:12px"></div>
    </div>`;
  }

  function rerenderModels() {
    const wrap = document.querySelector('#view-chat .model-wrap');
    if (!wrap) return;
    wrap.innerHTML = `<button class="model-pick" id="model-pick">模型</button>${modelPopOpen ? modelPop() : ''}`;
    paintModel();
  }

  function toggleModelPop() {
    modelPopOpen = !modelPopOpen;
    if (!modelPopOpen) modelFormOpen = false;
    rerenderModels();
  }

  function isModeRowVisible() {
    const row = document.getElementById('mode-row');
    return !!row && row.style.display !== 'none';
  }

  function pickModel(id) {
    if (id !== 'default' && !models.some((m) => m.id === id)) return;
    activeModelId = id;
    try { localStorage.setItem('llm_active_' + currentUid, id); } catch (e) { /* 忽略 */ }
    // 选了模型却仍走「原文摘编」没有意义：自动切到生成，用户可再手动切回
    if (id !== 'default' && isModeRowVisible()) setMode('llm');
    modelPopOpen = false;
    modelFormOpen = false;
    rerenderModels();
    const status = document.getElementById('chat-status');
    if (status && id !== 'default') {
      const m = models.find((x) => x.id === id);
      if (m) status.textContent = `当前模型：${m.name || m.model}`;
    }
  }

  function modelDraft() {
    const get = (id) => ((document.getElementById(id) || {}).value || '').trim();
    return { name: get('mf-name'), base_url: get('mf-base'), model: get('mf-model'), api_key: get('mf-key') };
  }

  function validateDraft(d) {
    if (!d.base_url) return '请填写 Base URL';
    if (!/^https?:\/\//i.test(d.base_url)) return 'Base URL 必须以 http:// 或 https:// 开头';
    if (!d.model) return '请填写模型名';
    // 请求头只能放 ASCII：模型名出现中文时浏览器会直接拒绝发送请求
    if (/[^\x21-\x7e]/.test(d.model)) return '模型名只能包含英文、数字与常见符号';
    if (d.api_key.length < 8) return 'API Key 太短，请检查是否复制完整';
    return '';
  }

  async function testDraft() {
    const msg = document.getElementById('mf-msg');
    const d = modelDraft();
    const bad = validateDraft(d);
    if (bad) { if (msg) msg.textContent = bad; return; }
    if (msg) msg.textContent = '测试中…（会真实调用一次模型）';
    try {
      const r = await API.testLLMConfig({ api_key: d.api_key, base_url: d.base_url, model: d.model });
      if (msg) {
        msg.textContent = r.ok
          ? `连接成功：${r.model}，往返 ${r.latency_ms} ms`
          : `连接失败：${r.message}`;
      }
    } catch (e) {
      if (msg) msg.textContent = '测试失败：' + e.message;
    }
  }

  function saveDraft() {
    const msg = document.getElementById('mf-msg');
    const d = modelDraft();
    const bad = validateDraft(d);
    if (bad) { if (msg) msg.textContent = bad; return; }
    const item = {
      id: 'm_' + Date.now().toString(36),
      name: d.name || d.model,
      base_url: d.base_url.replace(/\/$/, ''),
      model: d.model,
      api_key: d.api_key,
    };
    models = models.concat([item]);
    writeModels(currentUid, models);
    activeModelId = item.id;
    try { localStorage.setItem('llm_active_' + currentUid, item.id); } catch (e) { /* 忽略 */ }
    modelFormOpen = false;
    modelPopOpen = false;
    if (isModeRowVisible()) setMode('llm');
    rerenderModels();
    const status = document.getElementById('chat-status');
    if (status) status.textContent = `已切换到「${item.name}」，密钥只保存在本浏览器`;
  }

  function removeModel(id) {
    if (!window.confirm('删除这个自定义模型？（只影响本浏览器）')) return;
    models = models.filter((m) => m.id !== id);
    writeModels(currentUid, models);
    if (activeModelId === id) {
      activeModelId = 'default';
      try { localStorage.setItem('llm_active_' + currentUid, 'default'); } catch (e) { /* 忽略 */ }
    }
    rerenderModels();
  }

  function welcome() {
    const box = document.getElementById('chat-messages');
    box.innerHTML = `<div class="msg assistant">
      <div class="meta">助手</div>
      你好！我是校园知识问答助手，可解答学生手册与校园通知相关的问题。答案会标注引用来源；知识库中没有的内容我会明确告诉你，绝不编造。可以连续追问，例如先问"违纪处分有哪些种类"，再问"那最严重的那种会开除吗"。
    </div>`;
  }

  function addMessage(role, content, extra) {
    const box = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    let meta = role === 'user' ? '你' : '助手';
    if (extra && extra.mode) meta += ` · ${extra.mode}${extra.degraded ? '（降级）' : ''}`;
    let html = `<div class="meta">${escapeHtml(meta)}</div><div>${escapeHtml(content)}</div>`;
    if (extra && extra.conflict_notice) {
      html += `<div class="cite" style="border-color:var(--warn);background:#fffbeb">⚠ ${escapeHtml(extra.conflict_notice)}</div>`;
    }
    if (extra && extra.degradation_notice) {
      html += `<div class="cite" style="border-color:var(--warn);background:#fffbeb">⚠ ${escapeHtml(extra.degradation_notice)}</div>`;
    }
    if (extra && extra.mode_notice) {
      html += `<div class="cite" style="border-color:#bfdbfe;background:#eff6ff">ℹ ${escapeHtml(extra.mode_notice)}</div>`;
    }
    if (extra && extra.citations && extra.citations.length) {
      html += `<div class="cites"><div class="muted" style="font-size:12px;margin-bottom:4px">引用来源</div>` +
        extra.citations.map((c, i) => {
          const loc = c.article_no ? `第${c.article_no}条` : '';
          const ch = c.chapter ? `${c.chapter} ` : '';
          const docId = c.doc_id || '';
          // 引用标题可点击 → 跳转到知识图谱探索该文档/实体（若图谱里存在该节点）
          const titleHtml = docId
            ? `<a href="javascript:void(0)" class="cite-link" data-graph="${escapeHtml(docId)}">《${escapeHtml(c.doc_title)}》</a>`
            : `《${escapeHtml(c.doc_title)}》`;
          return `<div class="cite"><div class="src">[${i + 1}] ${titleHtml} ${escapeHtml(ch + loc)}</div>
                  <div class="snip">${escapeHtml(c.snippet || '')}</div></div>`;
        }).join('') + `</div>`;
    }
    if (extra && extra.relations && extra.relations.length) {
      html += `<div class="path-box"><div class="muted" style="font-size:12px">关系路径</div>` +
        extra.relations.map((p) => `<div class="path-line">${escapeHtml(p)}</div>`).join('') + `</div>`;
    }
    if (extra && extra.entities && extra.entities.length) {
      html += `<div class="muted" style="margin-top:6px;font-size:12px">相关实体：${escapeHtml(extra.entities.map(e => e.name).join('、'))}</div>`;
    }
    if (extra && extra.can_enhance) {
      html += `<div class="enhance-hint">以上为条款原文摘编。
        <a href="javascript:void(0)" data-enhance="1">改用大模型归纳一次</a></div>`;
    }
    div.innerHTML = html;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  // ---------- 会话列表 ----------
  async function loadSessions(keepCurrent) {
    const box = document.getElementById('session-list');
    if (!box) return;
    try {
      const list = await API.listSessions();
      if (!list.length) {
        box.innerHTML = '<div class="muted" style="padding:10px 4px">还没有会话，问一个问题就开始。</div>';
        return;
      }
      box.innerHTML = list.map((s) => `
        <div class="session-item ${s.session_id === sessionId ? 'active' : ''}" data-id="${escapeHtml(s.session_id)}">
          <div class="session-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
          <div class="session-meta">${escapeHtml((s.updated_at || '').slice(5, 16))} · ${s.message_count} 条</div>
          <button class="session-del" data-del="${escapeHtml(s.session_id)}" title="删除会话">✕</button>
        </div>`).join('');
      if (!keepCurrent && !sessionId && list.length) {
        // 刷新页面后自动恢复最近一次会话，避免历史"消失"
        await openSession(list[0].session_id);
      }
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
    }
  }

  async function openSession(id) {
    const box = document.getElementById('chat-messages');
    if (!box) return;
    box.innerHTML = '<div class="muted">加载历史…</div>';
    try {
      const msgs = await API.sessionMessages(id);
      sessionId = id;
      box.innerHTML = '';
      if (!msgs.length) {
        welcome();
      } else {
        msgs.forEach((m) => addMessage(m.role, m.content, { citations: m.citations || [] }));
      }
      highlightActive();
    } catch (e) {
      box.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
      sessionId = null;
    }
  }

  function highlightActive() {
    document.querySelectorAll('#session-list .session-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.id === sessionId);
    });
  }

  async function newSession() {
    try {
      const s = await API.createSession();
      sessionId = s.session_id;
      welcome();
      await loadSessions(true);
      highlightActive();
      const input = document.getElementById('chat-input');
      if (input) input.focus();
    } catch (e) {
      addMessage('assistant', '⚠ ' + e.message, null);
    }
  }

  async function removeSession(id) {
    if (!window.confirm('删除该会话及其全部消息？此操作不可撤销。')) return;
    try {
      await API.deleteSession(id);
      if (id === sessionId) { sessionId = null; welcome(); }
      await loadSessions(true);
      highlightActive();
    } catch (e) {
      window.alert('删除失败：' + e.message);
    }
  }

  // ---------- 发送 ----------
  async function send(text) {
    if (sending) return;
    const input = document.getElementById('chat-input');
    const q = (text !== undefined ? text : input.value).trim();
    if (!q) return;
    if (input) input.value = '';
    lastQuestion = q;
    addMessage('user', q);
    const status = document.getElementById('chat-status');
    const btn = document.getElementById('chat-send');
    sending = true;
    status.textContent = answerMode === 'llm' ? '检索并生成中…' : '检索中…';
    if (btn) btn.disabled = true;
    try {
      if (!sessionId) {
        const s = await API.createSession();
        sessionId = s.session_id;
      }
      const resp = await API.sessionAsk(sessionId, q, answerMode, activeModel());
      if (resp.rewritten_question) {
        addMessage('assistant', `（理解：${resp.rewritten_question}）`, null);
      }
      addMessage('assistant', resp.answer, resp);
      await loadSessions(true);
      highlightActive();
    } catch (e) {
      addMessage('assistant', '⚠ ' + e.message + (e.hint ? '\n' + e.hint : ''), null);
    } finally {
      sending = false;
      status.textContent = ''; if (btn) btn.disabled = false;
    }
  }

  // ---------- 事件 ----------
  view.addEventListener('click', (e) => {
    // 模型区域内的点击不要冒泡到 document：
    // 浮层是「先重建 DOM 再显示」的，重建后 e.target 已脱离文档，
    // document 上的「点外部关闭」会把它误判成外部点击，刚打开就关掉。
    if (e.target.closest && e.target.closest('#view-chat .model-wrap')) {
      e.stopPropagation();
    }
    const modelDel = e.target.closest('[data-del-model]');
    if (modelDel) { e.stopPropagation(); removeModel(modelDel.dataset.delModel); return; }
    const modelPick = e.target.closest('.model-pop [data-model]');
    if (modelPick) { pickModel(modelPick.dataset.model); return; }
    if (e.target.id === 'model-pick') { toggleModelPop(); return; }
    if (e.target.id === 'model-add') { modelFormOpen = true; rerenderModels(); return; }
    if (e.target.id === 'mf-test') { testDraft(); return; }
    if (e.target.id === 'mf-save') { saveDraft(); return; }
    const del = e.target.closest('[data-del]');
    if (del) { e.stopPropagation(); removeSession(del.dataset.del); return; }
    const item = e.target.closest('.session-item');
    if (item) { openSession(item.dataset.id); return; }
    const link = e.target.closest('[data-graph]');
    if (link) { window.App.switchView('graph'); window.dispatchEvent(new CustomEvent('graph:focus', { detail: link.dataset.graph })); return; }
    const enhance = e.target.closest('[data-enhance]');
    if (enhance) { enhanceLast(); return; }
    const modeBtn = e.target.closest('#mode-row .mode-btn');
    if (modeBtn) { setMode(modeBtn.dataset.mode); return; }
    if (e.target.id === 'chat-send') send();
    else if (e.target.id === 'session-new') newSession();
  });
  view.addEventListener('keydown', (e) => {
    if (e.target.id === 'chat-input' && e.ctrlKey && e.key === 'Enter') send();
  });

  // 切换用户后，会话归属变了，必须重新拉列表；可用模型也可能变了，重算切换器
  window.addEventListener('identity:changed', () => {
    sessionId = null;
    modelPopOpen = false;
    modelFormOpen = false;
    if (document.getElementById('session-list')) {
      welcome(); loadSessions(); loadModeInfo(); loadModelInfo();
    }
  });

  // 点模型浮层之外的任何地方 → 收起（浮层在输入区上方，遮挡消息时很容易挡住视线）
  document.addEventListener('click', (e) => {
    if (!modelPopOpen) return;
    if (e.target.closest && e.target.closest('#view-chat .model-wrap')) return;
    modelPopOpen = false;
    modelFormOpen = false;
    rerenderModels();
  });

  render();
  welcome();
  loadSessions();

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  window.addEventListener('view:changed', (e) => {
    if (e.detail === 'chat' && !document.getElementById('session-list')) {
      render(); welcome(); loadSessions();
    }
  });
})();
