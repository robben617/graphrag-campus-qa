// 身份组件：创建身份 / 粘贴已有 Token / 切换 / 退出。
//
// 此前前端只从 localStorage 读 user_token，却没有任何地方写入它，
// 于是永远以「匿名用户」运行——多用户隔离、掌握标记、会话归属在界面上完全不可见。
//
// 变更后派发 identity:changed 事件，各视图据此重新拉取自己那份数据。
(function () {
  const ROLE_LABEL = { student: '学生', teacher: '老师', admin: '管理员' };
  let panelOpen = false;

  function box() { return document.getElementById('identity-box'); }

  function dispatchChanged() {
    window.dispatchEvent(new CustomEvent('identity:changed'));
  }

  async function refresh() {
    const el = box();
    if (!el) return;
    const token = API.getToken();
    if (!token) {
      el.innerHTML = `
        <div class="identity-card">
          <div class="muted" style="font-size:12px">当前：匿名访客</div>
          <button class="btn btn-mini" id="id-open">创建 / 切换身份</button>
        </div>${panelOpen ? panelHtml('') : ''}`;
      if (panelOpen) loadLlmPanel();
      return;
    }
    try {
      const me = await API.me();
      el.innerHTML = `
        <div class="identity-card">
          <div class="identity-who"><b>${escapeHtml(me.username)}</b>
            <span class="badge">${escapeHtml(ROLE_LABEL[me.role] || me.role)}</span></div>
          <button class="btn btn-mini btn-ghost" id="id-open">切换身份</button>
        </div>${panelOpen ? panelHtml(me.username) : ''}`;
    } catch (e) {
      // Token 失效
      API.clearToken();
      el.innerHTML = `
        <div class="identity-card">
          <div class="muted" style="font-size:12px">身份已失效，请重新登录</div>
          <button class="btn btn-mini" id="id-open">创建 / 切换身份</button>
        </div>${panelOpen ? panelHtml('') : ''}`;
    }
    if (panelOpen) loadLlmPanel();
  }

  function panelHtml(defaultName) {
    return `
      <div class="identity-panel">
        <div class="muted" style="font-size:12px;margin-bottom:6px">新建身份</div>
        <input id="id-name" placeholder="用户名，如 xiaoming" value="${escapeHtml(defaultName || '')}" />
        <select id="id-role">
          <option value="student">学生（问答 / 自测 / 掌握）</option>
          <option value="teacher">老师（+ 维护端）</option>
          <option value="admin">管理员（+ 用户列表）</option>
        </select>
        <button class="btn btn-mini" id="id-create" style="width:100%">创建并切换</button>

        <div class="muted" style="font-size:12px;margin:10px 0 6px">已有 Token 直接登录</div>
        <textarea id="id-token" placeholder="粘贴你的 API Token" style="min-height:52px"></textarea>
        <button class="btn btn-mini btn-ghost" id="id-use" style="width:100%">使用此 Token</button>

        <div class="muted" style="font-size:12px;margin:12px 0 6px">大模型 Key（可选，用自己的额度）</div>
        <div id="llm-status" class="muted" style="font-size:11px;margin-bottom:6px;line-height:1.5"></div>
        <input id="llm-base" placeholder="Base URL，如 https://api.deepseek.com/v1" />
        <input id="llm-model" placeholder="模型，如 deepseek-chat" />
        <input id="llm-key" type="password" placeholder="API Key（留空则沿用已保存的）" autocomplete="off" />
        <div style="display:flex;gap:6px">
          <button class="btn btn-mini" id="llm-save" style="flex:1">保存</button>
          <button class="btn btn-mini btn-ghost" id="llm-test" style="flex:1">测试</button>
          <button class="btn btn-mini btn-ghost" id="llm-clear" style="flex:1">清除</button>
        </div>
        <div id="llm-msg" class="muted" style="font-size:12px;margin-top:6px"></div>

        ${API.getToken() ? '<button class="btn btn-mini btn-ghost" id="id-logout" style="width:100%;margin-top:10px">退出（回到匿名）</button>' : ''}
        <div id="id-msg" class="muted" style="font-size:12px;margin-top:8px"></div>
      </div>`;
  }

  // ---------- 大模型 Key 配置 ----------
  // 说明：配置按用户隔离，匿名访客没有 user_id 可挂，因此保存类操作必须先有身份。
  // 前端不缓存 key，任何时候都不把明文 key 写进 DOM 持久状态或 localStorage。
  function llmLabel(c) {
    if (c.source === 'user') {
      return `当前：个人 Key（${c.key_hint || '已保存'}，${c.model}）`;
    }
    if (c.source === 'server') {
      return '当前：未配置个人 Key，使用服务器默认模型（正式上线后可能关闭）';
    }
    return '当前：未配置 Key，回答会退化为「原文摘编」';
  }

  async function loadLlmPanel() {
    const status = document.getElementById('llm-status');
    if (!status) return;
    if (!API.getToken()) {
      status.textContent = '匿名访客无法保存 Key，请先创建身份';
      return;
    }
    try {
      const c = await API.llmConfig();
      const base = document.getElementById('llm-base');
      const model = document.getElementById('llm-model');
      if (base && !base.value) base.value = c.base_url || '';
      if (model && !model.value) model.value = c.model || '';
      status.textContent = llmLabel(c);
    } catch (e) {
      status.textContent = '读取配置失败：' + e.message;
    }
  }

  function llmPayload() {
    const get = (id) => (document.getElementById(id) || {}).value || '';
    return {
      api_key: get('llm-key').trim(),
      base_url: get('llm-base').trim(),
      model: get('llm-model').trim(),
      enabled: true,
    };
  }

  async function saveLlm() {
    const msg = document.getElementById('llm-msg');
    if (!API.getToken()) { if (msg) msg.textContent = '请先创建身份'; return; }
    if (msg) msg.textContent = '保存中…';
    try {
      const c = await API.saveLLMConfig(llmPayload());
      const keyEl = document.getElementById('llm-key');
      if (keyEl) keyEl.value = '';
      if (msg) msg.textContent = '已保存。建议点「测试」验证一下能否连通。';
      const status = document.getElementById('llm-status');
      if (status) status.textContent = llmLabel(c);
    } catch (e) {
      if (msg) msg.textContent = '保存失败：' + e.message;
    }
  }

  async function testLlm() {
    const msg = document.getElementById('llm-msg');
    if (!API.getToken()) { if (msg) msg.textContent = '请先创建身份'; return; }
    if (msg) msg.textContent = '测试中…（会真实调用一次模型）';
    try {
      const r = await API.testLLMConfig(llmPayload());
      if (msg) {
        msg.textContent = r.ok
          ? `连接成功：${r.model}，往返 ${r.latency_ms} ms`
          : `连接失败：${r.message}`;
      }
    } catch (e) {
      if (msg) msg.textContent = '测试失败：' + e.message;
    }
  }

  async function clearLlm() {
    const msg = document.getElementById('llm-msg');
    if (!API.getToken()) { if (msg) msg.textContent = '请先创建身份'; return; }
    if (!window.confirm('清除已保存的大模型 Key？清除后将回退到服务器默认配置或原文摘编。')) return;
    try {
      await API.deleteLLMConfig();
      ['llm-key', 'llm-base', 'llm-model'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      if (msg) msg.textContent = '已清除';
      const status = document.getElementById('llm-status');
      if (status) status.textContent = llmLabel(await API.llmConfig());
    } catch (e) {
      if (msg) msg.textContent = '清除失败：' + e.message;
    }
  }

  async function createIdentity() {
    const name = (document.getElementById('id-name').value || '').trim();
    const role = document.getElementById('id-role').value;
    const msg = document.getElementById('id-msg');
    if (!name) { if (msg) msg.textContent = '请填写用户名'; return; }
    if (msg) msg.textContent = '创建中…';
    try {
      const r = await API.createUser(name, role);
      API.setToken(r.api_token);
      panelOpen = false;
      await refresh();
      dispatchChanged();
      window.alert(`身份已创建并切换。\n\n用户名：${r.username}\n角色：${ROLE_LABEL[r.role] || r.role}\nToken：${r.api_token}\n\n请自行保存该 Token —— 换了浏览器或清了缓存后，只能凭它登录。`);
    } catch (e) {
      if (!msg) return;
      if (e.status === 403) {
        msg.textContent = '系统已初始化，创建账号需要管理员权限。请改用「已有 Token 直接登录」，或让管理员代为创建。';
      } else if (e.status === 400) {
        msg.textContent = '用户名已存在，请改用「已有 Token 直接登录」。';
      } else {
        msg.textContent = '创建失败：' + e.message;
      }
    }
  }

  async function useToken() {
    const t = (document.getElementById('id-token').value || '').trim();
    const msg = document.getElementById('id-msg');
    if (!t) { if (msg) msg.textContent = '请粘贴 Token'; return; }
    API.setToken(t);
    try {
      const me = await API.me();
      if (me.user_id === 'anonymous' && me.username === '匿名用户') {
        API.clearToken();
        if (msg) msg.textContent = '该 Token 无法识别，请检查是否完整。';
        return;
      }
      panelOpen = false;
      await refresh();
      dispatchChanged();
    } catch (e) {
      API.clearToken();
      if (msg) msg.textContent = '校验失败：' + e.message;
    }
  }

  function logout() {
    API.clearToken();
    panelOpen = false;
    refresh();
    dispatchChanged();
  }

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (t.id === 'id-open') { panelOpen = !panelOpen; refresh(); return; }
    if (t.id === 'id-create') { createIdentity(); return; }
    if (t.id === 'id-use') { useToken(); return; }
    if (t.id === 'id-logout') { logout(); return; }
    if (t.id === 'llm-save') { saveLlm(); return; }
    if (t.id === 'llm-test') { testLlm(); return; }
    if (t.id === 'llm-clear') { clearLlm(); return; }
    // 点击面板外部收起
    if (panelOpen && !t.closest('#identity-box')) { panelOpen = false; refresh(); }
  });

  refresh();
  window.IdentityWidget = { refresh };
})();
