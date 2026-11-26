// 공통 API 클라이언트
// - 인증 토큰(access_token)은 HttpOnly 쿠키라 JS에서 읽을 수 없음 → 브라우저가 자동 전송
// - 상태 변경 요청에는 CSRF 토큰 헤더 + 서버 발급 1회용 Nonce 헤더를 첨부
function readCookie(name) {
  const hit = document.cookie.split('; ').find((c) => c.startsWith(name + '='));
  return hit ? decodeURIComponent(hit.split('=').slice(1).join('=')) : '';
}

async function fetchNonce() {
  const res = await fetch('/api/nonce', { credentials: 'same-origin', cache: 'no-store' });
  if (!res.ok) throw new Error('Nonce 발급 실패');
  return (await res.json()).nonce;
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-Request-Nonce'] = await fetchNonce();
    const csrf = readCookie('csrf_token');
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  const res = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* 비 JSON 응답 */ }
  if (res.status === 401 && path !== '/api/login') {
    location.href = '/login';
  }
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
}
