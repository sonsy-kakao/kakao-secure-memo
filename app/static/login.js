document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const msg = document.getElementById('msg');
  msg.textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      body: { username: form.username.value, password: form.password.value },
    });
    location.href = '/';
  } catch (err) {
    msg.textContent = err.message;
  }
});
