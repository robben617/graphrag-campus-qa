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

        ${API.getToken() ? '<button class="btn btn-mini btn-ghost" id="id-logout" style="width:100%;margin-top:8px">退出（回到匿名）</button>' : ''}
        <div id="id-msg" class="muted" style="font-size:12px;margin-top:8px"></div>
      </div>`;
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
    // 点击面板外部收起
    if (panelOpen && !t.closest('#identity-box')) { panelOpen = false; refresh(); }
  });

  refresh();
  window.IdentityWidget = { refresh };
})();
