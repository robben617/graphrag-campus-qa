// 智能问答视图：首次提问自动创建会话，天然支持多轮追问。
(function () {
  const view = document.getElementById('view-chat');
  let sessionId = null;

  function render() {
    view.innerHTML = `
      <h2>💬 智能问答</h2>
      <div class="chat-box">
        <div class="messages" id="chat-messages"></div>
        <div class="chat-input-row">
          <textarea id="chat-input" placeholder="输入你的问题，如：奖学金申请需要什么条件？（Ctrl+Enter 发送）"></textarea>
          <button class="btn" id="chat-send">发送</button>
        </div>
        <div class="chat-input-row" style="align-items:center;justify-content:space-between">
          <span class="muted" id="chat-status"></span>
          <button class="btn btn-ghost" id="chat-clear" style="padding:6px 14px;font-size:13px">清空对话</button>
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
    if (extra && extra.citations && extra.citations.length) {
      html += `<div class="cites"><div class="muted" style="font-size:12px;margin-bottom:4px">引用来源</div>` +
        extra.citations.map((c, i) => {
          const loc = c.article_no ? `第${c.article_no}条` : '';
          const ch = c.chapter ? `${c.chapter} ` : '';
          return `<div class="cite"><div class="src">[${i + 1}] 《${escapeHtml(c.doc_title)}》 ${escapeHtml(ch + loc)}</div>
                  <div class="snip">${escapeHtml(c.snippet || '')}</div></div>`;
        }).join('') + `</div>`;
    }
    if (extra && extra.entities && extra.entities.length) {
      html += `<div class="muted" style="margin-top:6px;font-size:12px">相关实体：${escapeHtml(extra.entities.map(e => e.name).join('、'))}</div>`;
    }
    div.innerHTML = html;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  async function send(text) {
    const input = document.getElementById('chat-input');
    const q = (text !== undefined ? text : input.value).trim();
    if (!q) return;
    if (input) input.value = '';
    addMessage('user', q);
    const status = document.getElementById('chat-status');
    const btn = document.getElementById('chat-send');
    status.textContent = '检索中…'; btn.disabled = true;
    try {
      // 首次提问自动创建会话，之后自动进入多轮追问模式
      if (!sessionId) {
        const s = await API.createSession();
        sessionId = s.session_id;
      }
      const resp = await API.sessionAsk(sessionId, q);
      if (resp.rewritten_question) {
        addMessage('assistant', `（理解：${resp.rewritten_question}）`, null);
      }
      addMessage('assistant', resp.answer, resp);
    } catch (e) {
      addMessage('assistant', '⚠ ' + e.message, null);
    } finally {
      status.textContent = ''; btn.disabled = false;
    }
  }

  function clearChat() {
    sessionId = null;   // 下次提问会新建会话
    welcome();
  }

  render();
  welcome();

  // 事件委托
  view.addEventListener('click', (e) => {
    if (e.target.id === 'chat-send') send();
    else if (e.target.id === 'chat-clear') clearChat();
  });
  view.addEventListener('keydown', (e) => {
    if (e.target.id === 'chat-input' && e.ctrlKey && e.key === 'Enter') send();
  });
})();
