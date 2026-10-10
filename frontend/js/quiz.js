// 校规自测视图：按分类/难度抽题、判题解析、错题复习。
(function () {
  const view = document.getElementById('view-quiz');

  function render() {
    view.innerHTML = `
      <h2>📝 校规自测</h2>
      <div class="quiz-filter">
        <select id="quiz-category"><option value="">全部分类</option></select>
        <select id="quiz-difficulty">
          <option value="">全部难度</option><option>简单</option><option>中等</option><option>困难</option>
        </select>
        <select id="quiz-count">
          <option>5</option><option>10</option><option>20</option>
        </select>
        <button class="btn" id="quiz-start">开始答题</button>
        <button class="btn btn-ghost" id="quiz-wrong-btn">错题复习</button>
      </div>
      <div id="quiz-summary" class="muted" style="margin-bottom:12px"></div>
      <div id="quiz-area"></div>`;
  }

  async function loadCategories() {
    try {
      const cats = await API.quizCategories();
      const sel = document.getElementById('quiz-category');
      cats.forEach((c) => {
        const o = document.createElement('option');
        o.value = c.category; o.textContent = `${c.category}（${c.total}）`;
        sel.appendChild(o);
      });
      const sum = await API.quizSummary();
      document.getElementById('quiz-summary').textContent =
        `共 ${sum.total} 题 · 已作答 ${sum.attempted} 题 · 正确 ${sum.correct} · 错误 ${sum.wrong}`;
    } catch (e) { showErr(e); }
  }

  async function startQuiz(mode) {
    const area = document.getElementById('quiz-area');
    area.innerHTML = '<div class="muted">加载中…</div>';
    try {
      let questions;
      if (mode === 'wrong') {
        const wrong = await API.quizWrong();
        if (!wrong.length) { area.innerHTML = '<div class="card">暂无错题，先去答题吧！</div>'; return; }
        questions = wrong.map((w) => ({ qid: w.qid, question: w.question, options: w.options, category: w.category, difficulty: w.difficulty }));
      } else {
        const category = document.getElementById('quiz-category').value || null;
        const difficulty = document.getElementById('quiz-difficulty').value || null;
        const count = parseInt(document.getElementById('quiz-count').value, 10);
        questions = await API.quizQuestions(category, difficulty, count);
      }
      renderQuestions(questions);
    } catch (e) { showErr(e); }
  }

  function renderQuestions(questions) {
    const area = document.getElementById('quiz-area');
    if (!questions.length) { area.innerHTML = '<div class="card">没有符合条件的题目</div>'; return; }
    area.innerHTML = questions.map((q, qi) => `
      <div class="question-card" data-qid="${q.qid}">
        <div class="stem">${qi + 1}. ${escapeHtml(q.question)} <span class="muted" style="font-weight:400">（${escapeHtml(q.category || '')}·${escapeHtml(q.difficulty || '')}）</span></div>
        ${q.options.map((o, oi) => `<div class="option" data-opt="${oi}">${String.fromCharCode(65 + oi)}. ${escapeHtml(o)}</div>`).join('')}
        <div class="explain hidden"></div>
      </div>`).join('');

    area.querySelectorAll('.question-card').forEach((card) => {
      const qid = card.dataset.qid;
      card.querySelectorAll('.option').forEach((opt) => {
        opt.addEventListener('click', async () => {
          if (card.classList.contains('answered')) return;
          card.classList.add('answered');
          const chosen = parseInt(opt.dataset.opt, 10);
          try {
            const r = await API.quizAnswer(qid, chosen);
            markResult(card, r, chosen);
            refreshSummary();
          } catch (e) { showErr(e); }
        });
      });
    });
  }

  function markResult(card, r, chosen) {
    card.querySelectorAll('.option').forEach((opt) => {
      opt.classList.add('disabled');
      const idx = parseInt(opt.dataset.opt, 10);
      if (idx === r.correct_index) opt.classList.add('correct');
      if (idx === chosen && !r.is_correct) opt.classList.add('wrong');
    });
    const ex = card.querySelector('.explain');
    ex.classList.remove('hidden');
    ex.innerHTML = `<b>${r.is_correct ? '✅ 回答正确' : '❌ 回答错误，正确答案是 ' + String.fromCharCode(65 + r.correct_index)}</b><br/>解析：${escapeHtml(r.explanation)}${r.source ? '<br/><span class="muted">依据：' + escapeHtml(r.source) + '</span>' : ''}`;
  }

  async function refreshSummary() {
    try {
      const sum = await API.quizSummary();
      document.getElementById('quiz-summary').textContent =
        `共 ${sum.total} 题 · 已作答 ${sum.attempted} 题 · 正确 ${sum.correct} · 错误 ${sum.wrong}`;
    } catch (e) { /* ignore */ }
  }

  function showErr(e) {
    const area = document.getElementById('quiz-area');
    area.innerHTML = `<div class="error-text">${escapeHtml(e.message)}</div>`;
  }

  window.addEventListener('view:changed', (e) => {
    if (e.detail === 'quiz') { render(); loadCategories(); }
  });

  view.addEventListener('click', (e) => {
    if (e.target.id === 'quiz-start') startQuiz('normal');
    else if (e.target.id === 'quiz-wrong-btn') startQuiz('wrong');
  });

  // 首次加载即渲染界面骨架，不完全依赖切换事件，避免事件异常时白屏
  if (view && !view.innerHTML.trim()) render();
})();
