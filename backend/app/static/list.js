const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const uploadSection = document.getElementById("upload-section");
const uploadForm = document.getElementById("upload-form");
const uploadStatus = document.getElementById("upload-status");
const recordsSection = document.getElementById("records-section");
const listBody = document.querySelector("#list-table tbody");
const doctorNameInput = uploadForm.querySelector('[name="doctor_name"]');
const doctorNameLabel = document.getElementById("doctor-name-label");

let pollTimer = null;
let currentUser = null;

function applyRoleUi(user) {
  loginSection.hidden = true;
  uploadSection.hidden = false;
  recordsSection.hidden = false;
  renderAuthBar(user);

  if (user.role === "doctor") {
    doctorNameInput.value = user.doctor_name || user.username;
    doctorNameInput.readOnly = true;
    doctorNameInput.setAttribute("aria-readonly", "true");
    doctorNameLabel.querySelector("input").title = "Поле заполняется автоматически";
  } else {
    doctorNameInput.readOnly = false;
    doctorNameInput.removeAttribute("aria-readonly");
  }
}

async function fetchList() {
  const res = await apiFetch("/api/consultations");
  const items = await res.json();
  listBody.innerHTML = "";
  items.forEach((item) => {
    const tr = document.createElement("tr");
    const canDelete = currentUser && (currentUser.role === "admin" || currentUser.doctor_name === item.doctor_name);
    tr.innerHTML = `
      <td>${item.consultation_date}</td>
      <td>${escapeHtml(item.patient_name)}</td>
      <td>${escapeHtml(item.doctor_name)}</td>
      <td>${formatDuration(item.duration_sec)}</td>
      <td>${item.overall_score != null ? item.overall_score.toFixed(1) : "—"}</td>
      <td class="row-actions">
        <span class="status-badge ${item.status}">${statusLabel(item.status)}</span>
        ${canDelete ? '<button type="button" class="btn-delete">Удалить</button>' : ""}
      </td>
    `;
    const deleteBtn = tr.querySelector(".btn-delete");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          if (await deleteConsultation(item.id)) fetchList();
        } catch (err) {
          alert("Ошибка: " + err.message);
        }
      });
    }
    tr.addEventListener("click", () => {
      window.location.href = `/record/${item.id}`;
    });
    listBody.appendChild(tr);
  });

  const pending = items.some((i) => i.status === "processing" || i.status === "uploaded");
  if (pending && !pollTimer) {
    pollTimer = setInterval(fetchList, 3000);
  }
  if (!pending && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginStatus.textContent = "Входим…";
  const formData = new FormData(loginForm);
  try {
    currentUser = await login(formData.get("username"), formData.get("password"));
    loginStatus.textContent = "";
    loginForm.reset();
    applyRoleUi(currentUser);
    await fetchList();
  } catch (err) {
    loginStatus.textContent = "Ошибка: " + err.message;
  }
});

uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  uploadStatus.textContent = "Загрузка…";
  const fd = new FormData(uploadForm);
  try {
    const res = await apiFetch("/api/consultations/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ошибка загрузки");
    window.location.href = `/record/${data.id}`;
  } catch (err) {
    if (err.code === 401) {
      window.location.reload();
      return;
    }
    uploadStatus.textContent = "Ошибка: " + err.message;
  }
});

const dateInput = uploadForm.querySelector('[name="consultation_date"]');
dateInput.valueAsDate = new Date();

async function initPage() {
  currentUser = await getCurrentUser();
  if (!currentUser) {
    loginSection.hidden = false;
    uploadSection.hidden = true;
    recordsSection.hidden = true;
    return;
  }

  applyRoleUi(currentUser);
  await fetchList();
}

initPage().catch((err) => {
  loginStatus.textContent = "Ошибка: " + err.message;
});
