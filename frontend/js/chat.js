// 智能问答视图：左侧会话列表（新建/切换/删除），右侧消息区，支持多轮追问。
// 会话持久化在服务端，刷新页面后可从列表里恢复历史（此前 sessionId 只存内存变量，刷新即丢失）。
(function () {
  const view = document.getElementById('view-chat');
  let sessionId = null;
  let sending = false;

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
          <div class="chat-input-row">
            <textarea id="chat-input" placeholder="输入你的问题，如：奖学金申请需要什么条件？（Ctrl+Enter 发送）"></textarea>
            <button class="btn" id="chat-send">发送</button>
          </div>
          <div class="chat-input-row" style="align-items:center;justify-content:space-between">
            <span class="muted" id="chat-status"></span>
          </div>
        </div>
      </div>`;
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
    addMessage('user', q);
    const status = document.getElementById('chat-status');
    const btn = document.getElementById('chat-send');
    sending = true;
    status.textContent = '检索中…'; if (btn) btn.disabled = true;
    try {
      if (!sessionId) {
        const s = await API.createSession();
        sessionId = s.session_id;
      }
      const resp = await API.sessionAsk(sessionId, q);
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
    const del = e.target.closest('[data-del]');
    if (del) { e.stopPropagation(); removeSession(del.dataset.del); return; }
    const item = e.target.closest('.session-item');
    if (item) { openSession(item.dataset.id); return; }
    const link = e.target.closest('[data-graph]');
    if (link) { window.App.switchView('graph'); window.dispatchEvent(new CustomEvent('graph:focus', { detail: link.dataset.graph })); return; }
    if (e.target.id === 'chat-send') send();
    else if (e.target.id === 'session-new') newSession();
  });
  view.addEventListener('keydown', (e) => {
    if (e.target.id === 'chat-input' && e.ctrlKey && e.key === 'Enter') send();
  });

  // 切换用户后，会话归属变了，必须重新拉列表
  window.addEventListener('identity:changed', () => {
    sessionId = null;
    if (document.getElementById('session-list')) { welcome(); loadSessions(); }
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
