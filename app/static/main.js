// XSS 방어: 사용자 데이터는 절대 innerHTML로 넣지 않고 textContent로만 렌더링
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadProfile() {
  const { user } = await api('/api/me');
  document.getElementById('me-name').textContent = user.display_name;
  document.getElementById('me-github').textContent = user.github_id;
}

async function loadMemos() {
  const { memos } = await api('/api/memos');
  const list = document.getElementById('memo-list');
  list.replaceChildren();
  if (memos.length === 0) {
    list.appendChild(el('li', 'muted', '아직 작성된 메모가 없습니다.'));
    return;
  }
  for (const m of memos) {
    const li = el('li', 'memo');
    li.appendChild(el('h3', '', m.title));
    li.appendChild(el('p', 'content', m.content));
    li.appendChild(el('small', 'muted', `${m.author} · ${m.created_at}`));
    list.appendChild(li);
  }
}

document.getElementById('memo-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const msg = document.getElementById('msg');
  msg.textContent = '';
  try {
    await api('/api/memos', {
      method: 'POST',
      body: { title: form.title.value, content: form.content.value },
    });
    form.reset();
    await loadMemos();
  } catch (err) {
    msg.textContent = err.message;
  }
});

document.getElementById('logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } finally { location.href = '/login'; }
});

loadProfile().catch(console.error);
loadMemos().catch(console.error);
