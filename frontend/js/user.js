// 侧栏底部两块组件，职责严格分开：
//
//   身份（#identity-box）：我是谁 —— 创建 / 粘贴 Token 切换 / 退出。
//   设置（#settings-box）：用哪把模型 —— 只在自部署形态（SCHOOL_DEPLOY_MODE=selfhost）出现，
//                          公开演示站点整块不渲染。
//
// 为什么拆开：把「填 API Key」混在「切换身份」里，等于让第一次访问的陌生人
// 在还没弄清这是什么东西之前，先交出一把密钥。身份是入门动作，配置是进阶动作。
//
// 变更后派发 identity:changed 事件，各视图据此重新拉取自己那份数据。
(function () {
  const ROLE_LABEL = { student: '学生', teacher: '老师', admin: '管理员' };
  let identityOpen = false;
  let settingsOpen = false;

  function idBox() { return document.getElementById('identity-box'); }
  function setBox() { return document.getElementById('settings-box'); }

  function dispatchChanged() {
    // 部署策略里含「当前身份有没有可用 Key」，换人后必须重取，否则沿用上一个身份的结论
    API.resetDeployInfo();
    window.dispatchEvent(new CustomEvent('identity:changed'));
  }

  async function refresh() {
    await Promise.all([renderIdentity(), renderSettings()]);
  }

  // ---------- 身份 ----------
  async function renderIdentity() {
    const el = idBox();
    if (!el) return;
    const token = API.getToken();
    if (!token) {
      el.innerHTML = `
        <div class="identity-card">
          <div class="muted" style="font-size:12px">当前：匿名访客</div>
          <button class="btn btn-mini" id="id-open">创建 / 切换身份</button>
        </div>${identityOpen ? identityPanel('') : ''}`;
      return;
    }
    try {
      const me = await API.me();
      el.innerHTML = `
        <div class="identity-card">
          <div class="identity-who"><b>${escapeHtml(me.username)}</b>
            <span class="badge">${escapeHtml(ROLE_LABEL[me.role] || me.role)}</span></div>
          <button class="btn btn-mini btn-ghost" id="id-open">切换身份</button>
        </div>${identityOpen ? identityPanel(me.username) : ''}`;
    } catch (e) {
      // Token 失效
      API.clearToken();
      el.innerHTML = `
        <div class="identity-card">
          <div class="muted" style="font-size:12px">身份已失效，请重新登录</div>
          <button class="btn btn-mini" id="id-open">创建 / 切换身份</button>
        </div>${identityOpen ? identityPanel('') : ''}`;
    }
  }

  function identityPanel(defaultName) {
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

        ${API.getToken() ? '<button class="btn btn-mini btn-ghost" id="id-logout" style="width:100%;margin-top:10px">退出（回到匿名）</button>' : ''}
        <div id="id-msg" class="muted" style="font-size:12px;margin-top:8px"></div>
      </div>`;
  }

  // ---------- 模型设置（仅自部署形态） ----------
  async function renderSettings() {
    const el = setBox();
    if (!el) return;
    let info = null;
    try {
      info = await API.deployInfo();
    } catch (e) {
      info = null;
    }
    // 拿不到策略、或策略说未开放 → 不渲染入口。宁可少一个按钮，也不要吓跑访客。
    if (!info || !info.byok_allowed) {
      el.innerHTML = '';
      settingsOpen = false;
      return;
    }
    el.innerHTML = `
      <div class="identity-card">
        <button class="btn btn-mini btn-ghost" id="set-open">⚙ 模型设置</button>
      </div>${settingsOpen ? settingsPanel() : ''}`;
    if (settingsOpen) loadLlmPanel();
  }

  function settingsPanel() {
    return `
      <div class="identity-panel">
        <div class="muted" style="font-size:11px;margin-bottom:6px;line-height:1.6">
          自部署形态：可用自己的模型与额度。密钥加密后仅存本机数据库，只发往你填写的地址，可随时清除。
        </div>
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
      </div>`;
  }

  // 前端不缓存 key，任何时候都不把明文 key 写进 DOM 持久状态或 localStorage。
  function llmLabel(c) {
    if (c.source === 'user') {
      return `当前：个人 Key（${c.key_hint || '已保存'}，${c.model}）`;
    }
    if (c.source === 'server') {
      return '当前：未配置个人 Key，使用服务器默认模型';
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
      API.resetDeployInfo();
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
      API.resetDeployInfo();
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

  // ---------- 身份操作 ----------
  async function createIdentity() {
    const name = (document.getElementById('id-name').value || '').trim();
    const role = document.getElementById('id-role').value;
    const msg = document.getElementById('id-msg');
    if (!name) { if (msg) msg.textContent = '请填写用户名'; return; }
    if (msg) msg.textContent = '创建中…';
    try {
      const r = await API.createUser(name, role);
      API.setToken(r.api_token);
      identityOpen = false;
      API.resetDeployInfo(); // 换身份后「当前有没有可用 Key」会变，先作废旧结论再重取
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
      identityOpen = false;
      API.resetDeployInfo();
      await refresh();
      dispatchChanged();
    } catch (e) {
      API.clearToken();
      if (msg) msg.textContent = '校验失败：' + e.message;
    }
  }

  function logout() {
    API.clearToken();
    identityOpen = false;
    settingsOpen = false;
    API.resetDeployInfo();
    refresh();
    dispatchChanged();
  }

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (t.id === 'id-open') {
      identityOpen = !identityOpen;
      settingsOpen = false;
      refresh();
      return;
    }
    if (t.id === 'set-open') {
      settingsOpen = !settingsOpen;
      identityOpen = false;
      refresh();
      return;
    }
    if (t.id === 'id-create') { createIdentity(); return; }
    if (t.id === 'id-use') { useToken(); return; }
    if (t.id === 'id-logout') { logout(); return; }
    if (t.id === 'llm-save') { saveLlm(); return; }
    if (t.id === 'llm-test') { testLlm(); return; }
    if (t.id === 'llm-clear') { clearLlm(); return; }
    // 点击两个组件之外的位置 → 收起面板
    const inIdentity = t.closest && t.closest('#identity-box');
    const inSettings = t.closest && t.closest('#settings-box');
    if ((identityOpen || settingsOpen) && !inIdentity && !inSettings) {
      identityOpen = false;
      settingsOpen = false;
      refresh();
    }
  });

  refresh();
  window.IdentityWidget = { refresh };
})();
