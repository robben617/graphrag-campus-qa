// 统一的 API 访问层。
//
// 后端地址按三级顺序解析，解决「同一份前端要在三种环境下都能跑」的问题：
//   1. URL 参数 ?api=http://host:port  —— 显式指定，最高优先级（本地双端口开发用）
//   2. 同源（相对路径 /api/v1/...）    —— 部署在 nginx 后面时走反向代理，无需知道后端地址
//   3. http://127.0.0.1:8000           —— 兜底（file:// 直接打开，或同源探测失败）
//
// 为什么不能只写死 127.0.0.1:8000：docker compose 里前端由 nginx 托管在 8080，
// 后端在另一台机器/容器里，127.0.0.1 会指向访问者自己的电脑，必然连不上。
//
// 为什么不能只写同源：本地开发时前端常在 5500 端口（python frontend_server.py），
// 该端口并没有 /api 反代，一律同源会 404。所以用一次健康探测来决定走哪条。
(function () {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get('api');
  const LOOPBACK = 'http://127.0.0.1:8000';

  function normalize(b) {
    return (b || '').replace(/\/$/, '');
  }

  // 候选基址（按优先级）。同源记为 ''，请求时即相对路径。
  const candidates = [];
  if (explicit) {
    candidates.push(normalize(explicit));
  } else {
    if (location.protocol === 'http:' || location.protocol === 'https:') candidates.push('');
    if (candidates.indexOf(LOOPBACK) === -1) candidates.push(LOOPBACK);
  }

  let base = candidates[0];
  let ready = null;

  // 探测某个基址是否真的是本后端。
  // 关键：不能「只要 HTTP 有响应就算通」——python frontend_server.py / nginx 静态目录
  // 对 /api/v1/health 会返回 404，那样会把静态源误判成后端，导致所有接口全 404。
  // 健康检查的正常返回只有两种：200（ok/empty）与 503（存储不可用）。
  async function probe(candidate, timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(candidate + '/api/v1/health', { signal: ctrl.signal });
      return resp.status === 200 || resp.status === 503;
    } catch (e) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  // 只在「未显式指定、且有多个候选」时才探测，避免多余的网络往返。
  function ensureBase() {
    if (ready) return ready;
    if (explicit || candidates.length === 1) {
      ready = Promise.resolve(base);
      return ready;
    }
    ready = (async () => {
      for (const c of candidates) {
        // 首候选给足冷启动时间（本地模型首次加载可能较久），后续候选快速失败
        const ok = await probe(c, c === candidates[0] ? 2500 : 1200);
        if (ok) { base = c; return base; }
      }
      base = candidates[candidates.length - 1]; // 都探不通时用兜底，把真实错误暴露给用户
      return base;
    })();
    return ready;
  }

  function getToken() {
    return localStorage.getItem('user_token') || '';
  }

  function setToken(token) {
    if (token) localStorage.setItem('user_token', token);
    else localStorage.removeItem('user_token');
  }

  function clearToken() {
    localStorage.removeItem('user_token');
  }

  async function request(method, path, body, opts) {
    const b = await ensureBase();
    const o = { method, headers: {} };
    const token = getToken();
    if (token) o.headers['X-User-Token'] = token;
    if (body !== undefined) {
      o.headers['Content-Type'] = 'application/json';
      o.body = JSON.stringify(body);
    }
    const resp = await fetch(b + path, o);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* 无 JSON 体 */ }
    if (!resp.ok) {
      // 后端统一错误体：{ code, message, detail, request_id }
      const msg = (data && (data.message || data.detail || data.error_msg)) || `请求失败 (${resp.status})`;
      const err = new Error(msg);
      err.status = resp.status;
      err.data = data;
      err.requestId = data && data.request_id;
      // 权限不足现在是标准 403（后端 ForbiddenError），状态码可直接判定；
      // 仍保留文案兜底，以防将来有接口误用 400 而没同步改前端。
      if (resp.status === 403 || /权限不足/.test(msg)) {
        err.hint = '该操作需要更高权限，请在左下角切换为 老师 / 管理员 身份。';
      }
      if (resp.status === 401) err.hint = '身份已失效，请重新创建或粘贴 Token。';
      throw err;
    }
    return data;
  }

  window.API = {
    get BASE() { return base; },
    // 身份
    getToken, setToken, clearToken,
    me: () => request('GET', '/api/v1/me'),
    createUser: (username, role) => request('POST', '/api/v1/users', { username, role }),
    listUsers: () => request('GET', '/api/v1/users'),
    // 问答
    ask: (question) => request('POST', '/api/v1/ask', { question }),
    // 会话
    createSession: (title) => request('POST', '/api/v1/sessions', { title }),
    listSessions: () => request('GET', '/api/v1/sessions'),
    getSession: (id) => request('GET', `/api/v1/sessions/${id}`),
    deleteSession: (id) => request('DELETE', `/api/v1/sessions/${id}`),
    sessionMessages: (id) => request('GET', `/api/v1/sessions/${id}/messages`),
    sessionAsk: (id, question) => request('POST', `/api/v1/sessions/${id}/ask`, { question }),
    // 图谱
    graphStats: () => request('GET', '/api/v1/graph/stats'),
    graphCategories: () => request('GET', '/api/v1/graph/categories'),
    graphNodes: (q) => request('GET', `/api/v1/graph/nodes?q=${encodeURIComponent(q)}`),
    nodeNeighbors: (id, depth) => request('GET', `/api/v1/graph/nodes/${encodeURIComponent(id)}/neighbors?depth=${depth}`),
    graphSubgraph: (nodeIds, depth) => request('GET', `/api/v1/graph/subgraph?depth=${depth}` + (nodeIds ? `&node_ids=${encodeURIComponent(nodeIds.join(','))}` : '')),
    relationPaths: (nodeId, maxDepth) => request('GET', `/api/v1/graph/paths/${encodeURIComponent(nodeId)}?max_depth=${maxDepth || 2}`),
    // 自测
    quizCategories: () => request('GET', '/api/v1/quiz/categories'),
    quizQuestions: (category, difficulty, count) => {
      const p = new URLSearchParams();
      if (category) p.set('category', category);
      if (difficulty) p.set('difficulty', difficulty);
      p.set('count', count);
      return request('GET', `/api/v1/quiz/questions?${p.toString()}`);
    },
    quizAnswer: (qid, chosen) => request('POST', '/api/v1/quiz/answer', { qid, chosen }),
    quizWrong: () => request('GET', '/api/v1/quiz/wrong'),
    quizSummary: () => request('GET', '/api/v1/quiz/summary'),
    // 掌握标记
    masteryCandidates: (q) => request('GET', '/api/v1/mastery/candidates' + (q ? `?q=${encodeURIComponent(q)}` : '')),
    masteryList: (status) => request('GET', '/api/v1/mastery' + (status ? `?status=${encodeURIComponent(status)}` : '')),
    masterySet: (itemId, itemType, name, status) =>
      request('POST', '/api/v1/mastery', { item_id: itemId, item_type: itemType, name, status }),
    masteryRemove: (itemId) => request('DELETE', `/api/v1/mastery?item_id=${encodeURIComponent(itemId)}`),
    // 检索对比
    searchCompare: (q) => request('GET', `/api/v1/search/compare?q=${encodeURIComponent(q)}`),
    // 时效 / 健康 / 统计
    versioning: () => request('GET', '/api/v1/versioning'),
    health: () => request('GET', '/api/v1/health'),
    stats: () => request('GET', '/api/v1/stats'),
    // 维护端
    ingestNotice: (payload) => request('POST', '/api/v1/ingest/notice', payload),
    evalRun: (topK) => request('POST', `/api/v1/evaluation/run?top_k=${topK || 5}`),
    evalLatest: () => request('GET', '/api/v1/evaluation/latest'),
  };
})();
