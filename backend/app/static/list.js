const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const mainTabs = document.getElementById("main-tabs");
const uploadSection = document.getElementById("upload-section");
const recordingPanel = document.getElementById("recording-panel");
const recordStartButton = document.getElementById("record-start-button");
const recordPauseButton = document.getElementById("record-pause-button");
const recordStopButton = document.getElementById("record-stop-button");
const recordStatus = document.getElementById("record-status");
const recordTimer = document.getElementById("record-timer");
const uploadForm = document.getElementById("upload-form");
const uploadStatus = document.getElementById("upload-status");
const recordsSection = document.getElementById("records-section");
const usersSection = document.getElementById("users-section");
const onecSection = document.getElementById("onec-section");
const onecForm = document.getElementById("onec-form");
const usersTableBody = document.querySelector("#users-table tbody");
const userCreateForm = document.getElementById("user-create-form");
const userCreateStatus = document.getElementById("user-create-status");
const listBody = document.querySelector("#list-table tbody");
const doctorNameInput = uploadForm.querySelector('[name="doctor_name"]');
const doctorNameLabel = document.getElementById("doctor-name-label");
const consultationDateInput = uploadForm.querySelector('[name="consultation_date"]');

let pollTimer = null;
let currentUser = null;
let activeView = "upload";
const authRetryDelayMs = 250;
let recordStream = null;
let recordRecorder = null;
let recordChunks = [];
let recordStartedAt = 0;
let recordElapsedMs = 0;
let recordTimerHandle = null;

function formatIsoDate(value) {
  if (!value) return "";
  const trimmed = String(value).trim();
  if (!trimmed) return "";
  const parts = trimmed.split("-");
  return parts.length === 3 ? `${parts[2]}/${parts[1]}/${parts[0]}` : trimmed;
}

function formatDmyDate(value) {
  if (!value) return "";
  const trimmed = String(value).trim();
  if (!trimmed) return "";
  const parts = trimmed.split("/");
  return parts.length === 3 ? `${parts[2]}-${parts[1]}-${parts[0]}` : trimmed;
}

function todayIsoLocal() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function splitList(value) {
  return String(value || "")
    .replace(/\n/g, ";")
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatErrorMessage(err, fallback) {
  if (!err) return fallback;
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message || fallback;
  if (typeof err === "object") {
    if (typeof err.detail === "string") return err.detail;
    if (Array.isArray(err.detail)) return err.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    if (err.detail != null) return JSON.stringify(err.detail);
    return JSON.stringify(err);
  }
  return fallback;
}

function stopRecordTimer() {
  if (recordTimerHandle) {
    clearInterval(recordTimerHandle);
    recordTimerHandle = null;
  }
}

function updateRecordTimer() {
  const elapsed = recordElapsedMs + (recordStartedAt ? Date.now() - recordStartedAt : 0);
  const totalSec = Math.max(0, Math.floor(elapsed / 1000));
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  recordTimer.textContent = `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function setRecordUi(state) {
  if (!recordingPanel) return;
  recordingPanel.hidden = currentUser?.role !== "doctor";
  if (state === "idle") {
    recordStatus.textContent = "Микрофон не используется.";
    recordTimer.hidden = true;
    recordTimer.textContent = "00:00";
    recordStartButton.hidden = false;
    recordPauseButton.hidden = true;
    recordStopButton.hidden = true;
  } else if (state === "recording") {
    recordStatus.textContent = "Запись идёт...";
    recordTimer.hidden = false;
    recordStartButton.hidden = true;
    recordPauseButton.hidden = false;
    recordPauseButton.textContent = "Пауза";
    recordStopButton.hidden = false;
  } else if (state === "paused") {
    recordStatus.textContent = "Запись на паузе.";
    recordTimer.hidden = false;
    recordStartButton.hidden = true;
    recordPauseButton.hidden = false;
    recordPauseButton.textContent = "Продолжить";
    recordStopButton.hidden = false;
  } else if (state === "busy") {
    recordStatus.textContent = "Отправляем запись...";
    recordTimer.hidden = false;
    recordStartButton.hidden = true;
    recordPauseButton.hidden = true;
    recordStopButton.hidden = true;
  }
}

function syncWorkspaceVisibility() {
  const isDoctor = currentUser?.role === "doctor";
  const isAdmin = currentUser?.role === "admin";

  if (!currentUser) {
    uploadSection.hidden = true;
    onecSection.hidden = true;
    recordingPanel.hidden = true;
    recordsSection.hidden = true;
    usersSection.hidden = true;
    return;
  }

  if (activeView === "upload") {
    uploadSection.hidden = false;
    onecSection.hidden = false;
    recordingPanel.hidden = false;
    recordsSection.hidden = true;
    usersSection.hidden = true;
    return;
  }

  if (activeView === "records") {
    uploadSection.hidden = true;
    onecSection.hidden = true;
    recordingPanel.hidden = true;
    recordsSection.hidden = false;
    usersSection.hidden = true;
    return;
  }

  if (activeView === "users") {
    uploadSection.hidden = true;
    onecSection.hidden = true;
    recordingPanel.hidden = true;
    recordsSection.hidden = true;
    usersSection.hidden = !isAdmin;
  }
}

function cleanupRecording() {
  stopRecordTimer();
  recordStartedAt = 0;
  recordElapsedMs = 0;
  recordChunks = [];
  recordRecorder = null;
  if (recordStream) {
    recordStream.getTracks().forEach((track) => track.stop());
    recordStream = null;
  }
  setRecordUi("idle");
}

function getPreferredMimeType() {
  const candidates = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm", "audio/ogg"];
  return candidates.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) || "";
}

async function sendRecordedAudio(blob, ext) {
  setRecordUi("busy");
  const fd = new FormData(uploadForm);
  fd.set("file", new File([blob], `consultation.${ext}`, { type: blob.type || "application/octet-stream" }));
  try {
    const res = await apiFetch("/api/consultations/upload", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const message = formatErrorMessage(data, "");
      throw new Error(message || `HTTP ${res.status} ${res.statusText || ""}`.trim());
    }
    cleanupRecording();
    window.location.href = `/record/${data.id}`;
  } catch (err) {
    uploadStatus.textContent = "Ошибка: " + formatErrorMessage(err, "Не удалось отправить запись");
    setRecordUi("idle");
    if (recordStream) {
      recordStream.getTracks().forEach((track) => track.stop());
      recordStream = null;
    }
    recordRecorder = null;
  }
}

async function startRecording() {
  uploadStatus.textContent = "";
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      throw new Error("Браузер не поддерживает запись звука");
    }
    recordStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = getPreferredMimeType();
    recordChunks = [];
    recordRecorder = mimeType
      ? new MediaRecorder(recordStream, { mimeType })
      : new MediaRecorder(recordStream);
    recordRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        recordChunks.push(event.data);
      }
    };
    recordRecorder.onstop = () => {
      if (!recordChunks.length) return;
      const blob = new Blob(recordChunks, { type: recordRecorder?.mimeType || "audio/webm" });
      const ext = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mp4") ? "m4a" : "webm";
      void sendRecordedAudio(blob, ext);
    };
    recordRecorder.start(1000);
    recordElapsedMs = 0;
    recordStartedAt = Date.now();
    updateRecordTimer();
    stopRecordTimer();
    recordTimerHandle = setInterval(updateRecordTimer, 1000);
    setRecordUi("recording");
  } catch (err) {
    cleanupRecording();
    uploadStatus.textContent = "Ошибка: нет доступа к микрофону или он занят. " + formatErrorMessage(err, "");
  }
}

function togglePauseRecording() {
  if (!recordRecorder) return;
  if (recordRecorder.state === "recording") {
    recordElapsedMs += Date.now() - recordStartedAt;
    recordStartedAt = 0;
    recordRecorder.pause();
    stopRecordTimer();
    setRecordUi("paused");
  } else if (recordRecorder.state === "paused") {
    recordRecorder.resume();
    recordStartedAt = Date.now();
    recordTimerHandle = setInterval(updateRecordTimer, 1000);
    setRecordUi("recording");
  }
}

function stopRecording() {
  if (!recordRecorder || recordRecorder.state === "inactive") return;
  if (recordRecorder.state === "recording") {
    recordElapsedMs += Date.now() - recordStartedAt;
    recordStartedAt = 0;
  }
  stopRecordTimer();
  setRecordUi("busy");
  try {
    recordRecorder.stop();
  } catch (err) {
    uploadStatus.textContent = "Ошибка: " + formatErrorMessage(err, "Не удалось остановить запись");
    cleanupRecording();
  }
}

function setView(view) {
  activeView = view;
  const buttons = mainTabs?.querySelectorAll("button[data-view]") || [];
  buttons.forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
  syncWorkspaceVisibility();
}

function setupTabs() {
  mainTabs?.querySelectorAll("button[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
}

function syncHiddenUploadFieldsFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const queryMap = {
    doctor_code: "doctor_code",
    doctor_full_name: "doctor_full_name",
    doctor_position: "doctor_position",
    doctor_category: "doctor_category",
    patient_code: "patient_code",
    patient_full_name: "patient_full_name",
    patient_birth_date: "patient_birth_date",
    patient_age: "patient_age",
    patient_gender: "patient_gender",
    patient_phones: "patient_phones_json",
    patient_emails: "patient_emails_json",
  };

  Object.entries(queryMap).forEach(([paramName, fieldName]) => {
    const value = params.get(paramName);
    const input = uploadForm.querySelector(`[name="${fieldName}"]`);
    if (!input || value == null) return;
    if (fieldName === "patient_phones_json" || fieldName === "patient_emails_json") {
      input.value = JSON.stringify(splitList(value));
    } else {
      input.value = value;
    }
  });

  const sourcePayloadInput = uploadForm.querySelector('[name="source_payload_json"]');
  if (sourcePayloadInput) {
    const payload = {
      consultation_date: params.get("consultation_date") || null,
      doctor: {
        code: params.get("doctor_code") || null,
        full_name: params.get("doctor_full_name") || null,
        position: params.get("doctor_position") || null,
        category: params.get("doctor_category") || null,
      },
      patient: {
        code: params.get("patient_code") || null,
        full_name: params.get("patient_full_name") || null,
        birth_date: params.get("patient_birth_date") || null,
        age: params.get("patient_age") || null,
        gender: params.get("patient_gender") || null,
        phones: splitList(params.get("patient_phones")),
        emails: splitList(params.get("patient_emails")),
      },
    };
    sourcePayloadInput.value = JSON.stringify(payload);
  }
  const consultationFromQuery = params.get("consultation_date");
  if (consultationFromQuery) {
    consultationDateInput.value = formatDmyDate(consultationFromQuery);
  }
}

function applyQueryToUploadForm() {
  const params = new URLSearchParams(window.location.search);
  const doctorFullName = params.get("doctor_full_name") || currentUser?.doctor_name || currentUser?.username || "";
  const patientFullName = params.get("patient_full_name") || "";
  doctorNameInput.value = currentUser?.role === "doctor" ? (currentUser.doctor_name || currentUser.username) : doctorFullName;
  if (patientFullName) {
    uploadForm.querySelector('[name="patient_name"]').value = patientFullName;
  }
  consultationDateInput.value = todayIsoLocal();
  if (currentUser?.role === "admin" && params.get("consultation_date")) {
    consultationDateInput.value = formatDmyDate(params.get("consultation_date"));
  }
}

function applyOnecToUploadForm() {
  if (!onecForm) return;
  const fd = new FormData(onecForm);
  const mappings = {
    doctor_code: "doctor_code",
    doctor_full_name: "doctor_name",
    doctor_position: "doctor_position",
    doctor_category: "doctor_category",
    patient_code: "patient_code",
    patient_full_name: "patient_name",
    patient_birth_date: "patient_birth_date",
    patient_age: "patient_age",
    patient_gender: "patient_gender",
  };
  Object.entries(mappings).forEach(([sourceName, targetName]) => {
    const target = uploadForm.querySelector(`[name="${targetName}"]`);
    if (!target) return;
    const value = String(fd.get(sourceName) || "").trim();
    if (value) target.value = value;
  });
  const hiddenMap = {
    source_payload_json: JSON.stringify({
      consultation_date: String(fd.get("consultation_date") || "").trim() || null,
      doctor: {
        code: String(fd.get("doctor_code") || "").trim() || null,
        full_name: String(fd.get("doctor_full_name") || "").trim() || null,
        position: String(fd.get("doctor_position") || "").trim() || null,
        category: String(fd.get("doctor_category") || "").trim() || null,
      },
      patient: {
        code: String(fd.get("patient_code") || "").trim() || null,
        full_name: String(fd.get("patient_full_name") || "").trim() || null,
        birth_date: String(fd.get("patient_birth_date") || "").trim() || null,
        age: String(fd.get("patient_age") || "").trim() || null,
        gender: String(fd.get("patient_gender") || "").trim() || null,
        phones: splitList(fd.get("patient_phones")),
        emails: splitList(fd.get("patient_emails")),
      },
    }),
    doctor_code: String(fd.get("doctor_code") || "").trim() || "",
    doctor_position: String(fd.get("doctor_position") || "").trim() || "",
    doctor_category: String(fd.get("doctor_category") || "").trim() || "",
    patient_code: String(fd.get("patient_code") || "").trim() || "",
    patient_birth_date: String(fd.get("patient_birth_date") || "").trim() || "",
    patient_age: String(fd.get("patient_age") || "").trim() || "",
    patient_gender: String(fd.get("patient_gender") || "").trim() || "",
    patient_phones_json: JSON.stringify(splitList(fd.get("patient_phones"))),
    patient_emails_json: JSON.stringify(splitList(fd.get("patient_emails"))),
  };
  Object.entries(hiddenMap).forEach(([name, value]) => {
    const input = uploadForm.querySelector(`[name="${name}"]`);
    if (input) input.value = value;
  });
  consultationDateInput.value = String(fd.get("consultation_date") || "").trim() || todayIsoLocal();
}

async function fetchConsultations() {
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
    tr.querySelector(".btn-delete")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        if (await deleteConsultation(item.id)) fetchConsultations();
      } catch (err) {
        alert("Ошибка: " + err.message);
      }
    });
    tr.addEventListener("click", () => {
      window.location.href = `/record/${item.id}`;
    });
    listBody.appendChild(tr);
  });

  const pending = items.some((i) => i.status === "processing" || i.status === "uploaded");
  if (pending && !pollTimer) {
    pollTimer = setInterval(fetchConsultations, 3000);
  }
  if (!pending && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function loadUsers() {
  if (!currentUser || currentUser.role !== "admin") return;
  const res = await apiFetch("/api/users");
  const users = await res.json();
  usersTableBody.innerHTML = "";
  users.forEach((user) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" class="u-username" value="${escapeHtml(user.username)}" readonly></td>
      <td>
        <select class="u-role">
          <option value="doctor" ${user.role === "doctor" ? "selected" : ""}>doctor</option>
          <option value="admin" ${user.role === "admin" ? "selected" : ""}>admin</option>
        </select>
      </td>
      <td><input type="text" class="u-doctor-name" value="${escapeHtml(user.doctor_name || "")}"></td>
      <td><input type="text" class="u-password" maxlength="8" minlength="8" placeholder="новый пароль"></td>
      <td>
        <div class="link-cell">
          <input type="text" class="u-link" value="${escapeHtml(user.login_link || "")}" readonly>
          <button type="button" class="btn-link">Копировать</button>
        </div>
      </td>
      <td class="row-actions">
        <button type="button" class="btn-save">Сохранить</button>
        ${user.username === "admin" ? "" : '<button type="button" class="btn-delete">Удалить</button>'}
      </td>
    `;
    tr.querySelector(".btn-link").addEventListener("click", async () => {
      const link = tr.querySelector(".u-link").value;
      await navigator.clipboard.writeText(link);
      userCreateStatus.textContent = "Ссылка скопирована";
      setTimeout(() => (userCreateStatus.textContent = ""), 1200);
    });
    tr.querySelector(".btn-save").addEventListener("click", async () => {
      try {
        const payload = {
          role: tr.querySelector(".u-role").value,
          doctor_name: tr.querySelector(".u-doctor-name").value,
          password: tr.querySelector(".u-password").value || null,
        };
        const resSave = await apiFetch(`/api/users/${encodeURIComponent(user.username)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resSave.ok) {
          const data = await resSave.json().catch(() => ({}));
          throw new Error(data.detail || "Не удалось сохранить пользователя");
        }
        userCreateStatus.textContent = "Пользователь сохранён";
        await loadUsers();
      } catch (err) {
        userCreateStatus.textContent = "Ошибка: " + err.message;
      }
    });
    tr.querySelector(".btn-delete")?.addEventListener("click", async () => {
      if (!confirm(`Удалить пользователя ${user.username}?`)) return;
      try {
        const resDelete = await apiFetch(`/api/users/${encodeURIComponent(user.username)}`, {
          method: "DELETE",
        });
        if (!resDelete.ok) {
          const data = await resDelete.json().catch(() => ({}));
          throw new Error(data.detail || "Не удалось удалить пользователя");
        }
        await loadUsers();
      } catch (err) {
        userCreateStatus.textContent = "Ошибка: " + err.message;
      }
    });
    usersTableBody.appendChild(tr);
  });
}

userCreateForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(userCreateForm);
  const payload = {
    username: String(fd.get("username") || "").trim(),
    role: String(fd.get("role") || "").trim(),
    doctor_name: String(fd.get("doctor_name") || "").trim(),
    password: String(fd.get("password") || "").trim(),
  };
  try {
    const res = await apiFetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Не удалось создать пользователя");
    userCreateStatus.textContent = "Пользователь добавлен";
    userCreateForm.reset();
    await loadUsers();
  } catch (err) {
    userCreateStatus.textContent = "Ошибка: " + err.message;
  }
});

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginStatus.textContent = "Входим…";
  const formData = new FormData(loginForm);
  try {
    currentUser = await login(formData.get("username"), formData.get("password"));
    loginStatus.textContent = "";
    loginForm.reset();
    renderAuthBar(currentUser);
    await initWorkspace();
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
    uploadStatus.textContent = "Ошибка: " + err.message;
  }
});

recordStartButton?.addEventListener("click", () => {
  void startRecording();
});

recordPauseButton?.addEventListener("click", () => {
  togglePauseRecording();
});

recordStopButton?.addEventListener("click", () => {
  stopRecording();
});

async function initWorkspace() {
  if (!currentUser) return;
  loginSection.hidden = true;
  mainTabs.hidden = false;
  mainTabs.querySelectorAll(".admin-only").forEach((node) => {
    node.hidden = currentUser.role !== "admin";
  });
  mainTabs.querySelectorAll(".doctor-only").forEach((node) => {
    node.hidden = currentUser.role !== "doctor";
  });
  applyQueryToUploadForm();
  syncHiddenUploadFieldsFromQuery();
  if (currentUser.role === "doctor") {
    onecForm.addEventListener("input", applyOnecToUploadForm);
    applyOnecToUploadForm();
  }
  if (currentUser.role === "doctor") {
    doctorNameInput.readOnly = true;
    doctorNameInput.setAttribute("aria-readonly", "true");
    doctorNameLabel.querySelector("input").title = "Поле заполняется автоматически";
  } else {
    doctorNameInput.readOnly = false;
    doctorNameInput.removeAttribute("aria-readonly");
  }
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  const initialView = requestedView === "records" ? "records" : "upload";
  setView(initialView);
  if (currentUser.role === "doctor") {
    setRecordUi("idle");
  }
  await fetchConsultations();
  if (currentUser.role === "admin") {
    await loadUsers();
  }
}

async function getCurrentUserWithRetry() {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const user = await getCurrentUser();
    if (user) return user;
    if (attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, authRetryDelayMs));
    }
  }
  return null;
}

async function initPage() {
  setupTabs();
  currentUser = await getCurrentUserWithRetry();
  if (!currentUser) {
    loginSection.hidden = false;
    mainTabs.hidden = true;
    uploadSection.hidden = true;
    recordsSection.hidden = true;
    usersSection.hidden = true;
    return;
  }
  renderAuthBar(currentUser);
  await initWorkspace();
}

initPage().catch((err) => {
  loginStatus.textContent = "Ошибка: " + err.message;
});
