// 右上角身份组件：只负责「我是谁」，不再承载模型配置。
//
// 为什么把模型配置挪走：
//   1. 身份面板被放在右上角常驻，任何一次切换身份都会先看到「粘贴 API Key」，
//      对第一次访问的陌生人等同劝退；
//   2. 模型是「用哪个引擎」，身份是「以谁的身份」，两者生命周期完全不同——
//      模型要跟着会话走，身份要跟着人走。
//   模型选择现在位于聊天输入框旁（chat.js），密钥只存在浏览器本地。
//
// 管理员在这里保留一个隐藏入口，用于调整「所有人没选模型时用哪个」，
// 非管理员不渲染该按钮。
(function () {
  const ROLE_LABEL = { student: '学生', teacher: '老师', admin: '管理员' };
  let identityOpen = false;
  let defaultOpen = false;
  let currentUser = null;

  function idBox() { return document.getElementById('identity-box'); }

  function dispatchChanged() {
    // 部署策略里含「当前身份有没有可用模型」，换人后必须重取，否则沿用上一个身份的结论
    API.resetDeployInfo();
    window.dispatchEvent(new CustomEvent('identity:changed'));
  }

  function refresh() {
    const el = idBox();
    if (!el) return Promise.resolve();
    if (!API.getToken()) {
      currentUser = null;
      renderAnonymous(el);
      return Promise.resolve();
    }
    return API.me()
      .then((me) => {
        // 无 token 或 token 无效时后端返回匿名身份（不报错），据此判定需要重新登录
        if (!me || me.user_id === 'anonymous') {
          API.clearToken();
          currentUser = null;
          renderAnonymous(el, true);
          return;
        }
        currentUser = me;
        renderUser(el);
      })
      .catch(() => {
        currentUser = null;
        renderAnonymous(el, true);
      });
  }

  function renderAnonymous(el, invalid) {
    el.innerHTML = `
      <div class="identity-card">
        <div class="muted" style="font-size:12px">当前：匿名访客${invalid ? '（登录已失效）' : ''}</div>
        <button class="btn btn-mini" id="id-open">创建 / 切换身份</button>
      </div>${identityOpen ? identityPanel() : ''}${defaultOpen ? defaultPanel() : ''}`;
    if (defaultOpen) loadDefault();
  }

  function renderUser(el) {
    el.innerHTML = `
      <div class="identity-card">
        <div class="identity-who"><b>${escapeHtml(currentUser.username)}</b>
          <span class="badge">${escapeHtml(ROLE_LABEL[currentUser.role] || currentUser.role)}</span></div>
        <button class="btn btn-mini btn-ghost" id="id-open">切换身份</button>
      </div>${identityOpen ? identityPanel() : ''}${defaultOpen ? defaultPanel() : ''}`;
    if (defaultOpen) loadDefault();
  }

  // 自助只能创建学生；老师 / 管理员由管理员在维护端导入名单。
  function identityPanel() {
    return `
      <div class="identity-panel">
        ${currentUser ? '' : `
          <div class="muted" style="font-size:12px">创建学生身份</div>
          <input id="id-name" placeholder="用户名，如 xiaoming" />
          <button class="btn btn-mini" id="id-create" style="width:100%">创建并登录</button>
          <div class="model-note">老师与管理员账号由管理员统一导入名单，不开放自助注册。</div>
        `}

        <div class="muted" style="font-size:12px;margin:10px 0 6px">用 Token 登录</div>
        <textarea id="id-token" placeholder="粘贴管理员发给你的 Token" style="min-height:46px"></textarea>
        <button class="btn btn-mini btn-ghost" id="id-use" style="width:100%">使用此 Token</button>

        ${currentUser && currentUser.role === 'admin'
          ? '<button class="btn btn-mini btn-ghost" id="dft-open" style="width:100%;margin-top:8px">⚙ 默认模型（管理员）</button>'
          : ''}
        ${currentUser ? '<button class="btn btn-mini btn-ghost" id="id-logout" style="width:100%;margin-top:8px">退出（回到匿名）</button>' : ''}
        <div id="id-msg" class="muted" style="font-size:12px;margin-top:8px"></div>
      </div>`;
  }

  // ---------- 管理员：系统默认模型 ----------
  function defaultPanel() {
    return `
      <div class="identity-panel">
        <div class="muted" style="font-size:12px">系统默认模型</div>
        <div class="model-note">所有没有自选模型的用户都会用它。改完下一次提问即生效，不用重启服务。</div>
        <div id="dft-status" class="muted" style="font-size:11px;margin:4px 0"></div>
        <input id="dft-base" placeholder="Base URL，如 https://api.deepseek.com/v1" />
        <input id="dft-model" placeholder="模型名，如 deepseek-chat" />
        <input id="dft-key" type="password" placeholder="API Key（留空沿用已保存的）" autocomplete="off" />
        <div style="display:flex;gap:6px">
          <button class="btn btn-mini" id="dft-save" style="flex:1">保存</button>
          <button class="btn btn-mini btn-ghost" id="dft-clear" style="flex:1">清除</button>
        </div>
        <div id="dft-msg" class="muted" style="font-size:12px"></div>
        <button class="btn btn-mini btn-ghost" id="dft-back" style="width:100%;margin-top:6px">返回身份</button>
      </div>`;
  }

  async function loadDefault() {
    const st = document.getElementById('dft-status');
    if (!st) return;
    try {
      const d = await API.defaultLLM();
      const base = document.getElementById('dft-base');
      const model = document.getElementById('dft-model');
      if (base && !base.value) base.value = d.base_url || '';
      if (model && !model.value) model.value = d.model || '';
      if (d.source === 'db') {
        st.textContent = `当前：${d.model || '未命名'}（数据库配置${d.key_hint ? '，Key ' + d.key_hint : ''}，由 ${d.updated_by || '管理员'} 设置）`;
      } else if (d.source === 'env') {
        st.textContent = `当前：${d.model}（来自 .env，尚未在页面配置）`;
      } else {
        st.textContent = '当前：未配置可用模型，回答将退回原文摘编';
      }
    } catch (e) {
      st.textContent = '读取失败：' + e.message;
    }
  }

  async function saveDefault() {
    const msg = document.getElementById('dft-msg');
    const get = (id) => (document.getElementById(id) || {}).value || '';
    if (msg) msg.textContent = '保存中…';
    try {
      const d = await API.saveDefaultLLM({
        api_key: get('dft-key').trim(),
        base_url: get('dft-base').trim(),
        model: get('dft-model').trim(),
      });
      const keyEl = document.getElementById('dft-key');
      if (keyEl) keyEl.value = '';
      if (msg) msg.textContent = `已保存：${d.model}。下一次提问即生效。`;
      const st = document.getElementById('dft-status');
      if (st) st.textContent = `当前：${d.model}（数据库配置${d.key_hint ? '，Key ' + d.key_hint : ''}）`;
      API.resetDeployInfo();
    } catch (e) {
      if (msg) msg.textContent = '保存失败：' + e.message;
    }
  }

  async function clearDefault() {
    const msg = document.getElementById('dft-msg');
    if (!window.confirm('清除数据库里的默认模型？清除后会落回 .env 的配置。')) return;
    try {
      const d = await API.deleteDefaultLLM();
      if (msg) msg.textContent = d.deleted ? '已清除，当前落回 .env 配置' : '本来就没有页面配置';
      ['dft-key', 'dft-base', 'dft-model'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      const st = document.getElementById('dft-status');
      if (st) st.textContent = d.model ? `当前：${d.model}（来自 .env）` : '当前：未配置可用模型';
      API.resetDeployInfo();
    } catch (e) {
      if (msg) msg.textContent = '清除失败：' + e.message;
    }
  }

  // ---------- 身份操作 ----------
  async function createIdentity() {
    const name = ((document.getElementById('id-name') || {}).value || '').trim();
    const msg = document.getElementById('id-msg');
    if (!name) { if (msg) msg.textContent = '请填写用户名'; return; }
    if (msg) msg.textContent = '创建中…';
    try {
      const r = await API.createUser(name, 'student');
      API.setToken(r.api_token);
      identityOpen = false;
      await refresh();
      dispatchChanged();
      window.alert(`身份已创建。\n\n用户名：${r.username}\nToken：${r.api_token}\n\n换浏览器或清缓存后，只能凭这个 Token 登录，请自行保存。`);
    } catch (e) {
      if (!msg) return;
      if (e.status === 400) {
        msg.textContent = '用户名已存在，请改用 Token 登录，或换一个名字。';
      } else if (e.status === 403) {
        msg.textContent = '当前系统不允许自助创建，请联系管理员导入名单。';
      } else {
        msg.textContent = '创建失败：' + e.message;
      }
    }
  }

  async function useToken() {
    const t = ((document.getElementById('id-token') || {}).value || '').trim();
    const msg = document.getElementById('id-msg');
    if (!t) { if (msg) msg.textContent = '请粘贴 Token'; return; }
    API.setToken(t);
    try {
      const me = await API.me();
      if (!me || me.user_id === 'anonymous') {
        API.clearToken();
        if (msg) msg.textContent = '该 Token 无法识别，请检查是否完整。';
        return;
      }
      identityOpen = false;
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
    defaultOpen = false;
    currentUser = null;
    refresh();
    dispatchChanged();
  }

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (t.id === 'id-open') {
      identityOpen = !identityOpen;
      defaultOpen = false;
      refresh();
      return;
    }
    if (t.id === 'dft-open') {
      defaultOpen = true;
      identityOpen = false;
      refresh();
      return;
    }
    if (t.id === 'dft-back') {
      defaultOpen = false;
      identityOpen = true;
      refresh();
      return;
    }
    if (t.id === 'id-create') { createIdentity(); return; }
    if (t.id === 'id-use') { useToken(); return; }
    if (t.id === 'id-logout') { logout(); return; }
    if (t.id === 'dft-save') { saveDefault(); return; }
    if (t.id === 'dft-clear') { clearDefault(); return; }
    // 点击组件之外 → 收起面板
    if ((identityOpen || defaultOpen) && !(t.closest && t.closest('#identity-box'))) {
      identityOpen = false;
      defaultOpen = false;
      refresh();
    }
  });

  refresh();
  window.IdentityWidget = { refresh };
})();
