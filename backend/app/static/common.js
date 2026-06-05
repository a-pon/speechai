function formatDuration(sec) {
  if (sec == null) return "—";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatMs(ms) {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function statusLabel(status) {
  const map = {
    uploaded: "загружена",
    processing: "обработка",
    ready: "готово",
    failed: "ошибка",
  };
  return map[status] || status;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    const error = new Error("Требуется вход");
    error.code = 401;
    throw error;
  }
  return res;
}

async function getCurrentUser() {
  const res = await fetch("/api/auth/me");
  if (res.status === 401) return null;
  if (!res.ok) throw new Error("Не удалось получить сессию");
  return res.json();
}

async function login(username, password) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Не удалось войти");
  return data;
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
}

async function deleteConsultation(id) {
  if (!confirm("Удалить запись? Действие необратимо.")) return false;
  const res = await apiFetch(`/api/consultations/${id}`, { method: "DELETE" });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Не удалось удалить запись");
  }
  return true;
}

function renderAuthBar(user) {
  const authBar = document.getElementById("auth-bar");
  if (!authBar || !user) return;
  const roleLabel = user.role === "admin" ? "Админ" : "Врач";
  const nameLabel = user.doctor_name ? ` · ${escapeHtml(user.doctor_name)}` : "";
  authBar.hidden = false;
  authBar.innerHTML = `
    <div class="auth-user">
      <strong>${escapeHtml(user.username)}</strong>
      <span>${roleLabel}${nameLabel}</span>
    </div>
    <button type="button" id="logout-button">Выйти</button>
  `;
  authBar.querySelector("#logout-button").addEventListener("click", async () => {
    await logout();
    window.location.href = "/";
  });
}
