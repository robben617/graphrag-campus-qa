// 统一的 API 访问层：后端地址可通过 URL 参数 ?api=http://host:port 覆盖。
(function () {
  const params = new URLSearchParams(window.location.search);
  const BASE = (params.get('api') || 'http://127.0.0.1:8000').replace(/\/$/, '');

  function getToken() {
    return localStorage.getItem('user_token') || '';
  }

  async function request(method, path, body) {
    const opts = { method, headers: {} };
    const token = getToken();
    if (token) opts.headers['X-User-Token'] = token;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(BASE + path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* 无 JSON */ }
    if (!resp.ok) {
      const msg = (data && (data.message || data.detail)) || `请求失败 (${resp.status})`;
      const err = new Error(msg);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  window.API = {
    BASE,
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
    // 检索对比
    searchCompare: (q) => request('GET', `/api/v1/search/compare?q=${encodeURIComponent(q)}`),
    // 时效 / 健康 / 统计
    versioning: () => request('GET', '/api/v1/versioning'),
    health: () => request('GET', '/api/v1/health'),
    stats: () => request('GET', '/api/v1/stats'),
    relationPaths: (nodeId) => request('GET', `/api/v1/graph/paths/${encodeURIComponent(nodeId)}`),
    getToken,
  };
})();
