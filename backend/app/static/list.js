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
const consultationDateInput = uploadForm.querySelector('[name="consultation_date"]');
const consultationTypeInput = uploadForm.querySelector('[name="consultation_type"]');
const clinicDivisionInput = uploadForm.querySelector('[name="clinic_division"]');

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
let recordAutoStopHandle = null;

const RECORD_MAX_DURATION_MS = 90 * 60 * 1000;
const RECORD_COORDINATION_KEY = "speechai-recording-event";
const recordTabId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const recordChannel = "BroadcastChannel" in window ? new BroadcastChannel("speechai-recording") : null;

function canViewAllRecords(user) {
  return user?.role === "admin" || user?.can_view_all_records === true;
}

function formatIsoDate(value) {
  if (!value) return "";
  const trimmed = String(value).trim();
  if (!trimmed) return "";
  return trimmed;
}

function consultationTypeLabel(value) {
  if (value === "primary_child") return "Первичная детская";
  if (value === "repeat_adult") return "Повторная";
  return "Первичная";
}

function formatDmyDate(value) {
  if (!value) return "";
  const trimmed = String(value).trim();
  if (!trimmed) return "";
  const isoMatch = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (isoMatch) return trimmed;
  const dmyMatch = trimmed.match(/^(\d{2})[./-](\d{2})[./-](\d{4})$/);
  if (dmyMatch) return `${dmyMatch[3]}-${dmyMatch[2]}-${dmyMatch[1]}`;
  return trimmed;
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

function extractLoginToken(link) {
  if (!link) return "";
  try {
    return new URL(link, window.location.origin).searchParams.get("token") || "";
  } catch {
    return "";
  }
}

async function readApiResponseData(res) {
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json().catch(() => ({}));
  }
  const text = await res.text().catch(() => "");
  const detail = text.trim();
  return detail ? { detail: detail.slice(0, 500) } : {};
}

function buildUploadFormData({ requireFile = true } = {}) {
  applyOnecToUploadForm();
  const fd = new FormData();
  const fileInput = uploadForm.querySelector('[name="file"]');
  const file = fileInput?.files?.[0];
  if (requireFile && !file) {
    throw new Error("Выберите аудиофайл");
  }
  if (file) {
    fd.append("file", file, file.name || "consultation.mp3");
  }
  const sourcePayloadInput = uploadForm.querySelector('[name="source_payload_json"]');
  if (sourcePayloadInput?.value) {
    try {
      const payload = JSON.parse(sourcePayloadInput.value);
      payload.consultation_type = consultationTypeInput.value;
      payload.clinic_division = clinicDivisionInput.value;
      sourcePayloadInput.value = JSON.stringify(payload);
    } catch {
      sourcePayloadInput.value = "";
    }
  }

  const fields = [
    "doctor_name",
    "patient_name",
    "consultation_date",
    "consultation_type",
    "clinic_division",
    "source_system",
    "source_payload_json",
    "doctor_code",
    "doctor_position",
    "doctor_category",
    "patient_code",
    "patient_birth_date",
    "patient_age",
    "patient_gender",
    "patient_phones_json",
    "patient_emails_json",
  ];

  fields.forEach((name) => {
    const input = uploadForm.querySelector(`[name="${name}"]`);
    if (!input) return;
    const value = String(input.value ?? "").trim();
    if (value) fd.append(name, value);
  });

  const requiredFields = [
    ["doctor_name", "ФИО врача"],
    ["patient_name", "ФИО пациента"],
    ["consultation_date", "дата консультации"],
    ["consultation_type", "вид консультации"],
    ["clinic_division", "подразделение клиники"],
  ];
  const missing = requiredFields
    .filter(([name]) => !String(fd.get(name) || "").trim())
    .map(([, label]) => label);
  if (missing.length) {
    throw new Error(`Заполните обязательные поля в блоке 1С: ${missing.join(", ")}`);
  }

  return fd;
}

function stopRecordTimer() {
  if (recordTimerHandle) {
    clearInterval(recordTimerHandle);
    recordTimerHandle = null;
  }
}

function stopRecordAutoStop() {
  if (recordAutoStopHandle) {
    clearTimeout(recordAutoStopHandle);
    recordAutoStopHandle = null;
  }
}

function formatLimitDuration() {
  return `${Math.floor(RECORD_MAX_DURATION_MS / 60000)} минут`;
}

function updateRecordTimer() {
  const elapsed = recordElapsedMs + (recordStartedAt ? Date.now() - recordStartedAt : 0);
  const totalSec = Math.max(0, Math.floor(elapsed / 1000));
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  recordTimer.textContent = `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function scheduleRecordAutoStop() {
  stopRecordAutoStop();
  const elapsed = recordElapsedMs + (recordStartedAt ? Date.now() - recordStartedAt : 0);
  const remaining = RECORD_MAX_DURATION_MS - elapsed;
  if (remaining <= 0) {
    uploadStatus.textContent = `Достигнут лимит записи ${formatLimitDuration()}. Отправляем запись в обработку.`;
    stopRecording();
    return;
  }
  recordAutoStopHandle = setTimeout(() => {
    uploadStatus.textContent = `Достигнут лимит записи ${formatLimitDuration()}. Отправляем запись в обработку.`;
    stopRecording();
  }, remaining);
}

function currentRecordingUserKey() {
  return currentUser?.username || "";
}

function publishRecordingEvent(type) {
  const username = currentRecordingUserKey();
  if (!username) return;
  const message = {
    type,
    username,
    tabId: recordTabId,
    sentAt: Date.now(),
  };
  recordChannel?.postMessage(message);
  try {
    localStorage.setItem(RECORD_COORDINATION_KEY, JSON.stringify(message));
  } catch {
    // localStorage can be unavailable in private browsing modes.
  }
}

function handleRecordingEvent(message) {
  if (!message || message.tabId === recordTabId || message.username !== currentRecordingUserKey()) return;
  if (message.type !== "recording-start-request") return;
  if (!recordRecorder || recordRecorder.state === "inactive") return;

  uploadStatus.textContent = "Открыта новая запись под этим логином. Текущая запись остановлена и отправляется в обработку.";
  stopRecording();
}

function setRecordUi(state) {
  if (!recordingPanel) return;
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
  } else if (state === "requesting") {
    recordStatus.textContent = "Запрашиваем доступ к микрофону...";
    recordTimer.hidden = true;
    recordTimer.textContent = "00:00";
    recordStartButton.hidden = true;
    recordPauseButton.hidden = true;
    recordStopButton.hidden = true;
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
  stopRecordAutoStop();
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
  try {
    const fd = buildUploadFormData({ requireFile: false });
    fd.set("file", new File([blob], `consultation.${ext}`, { type: blob.type || "application/octet-stream" }));
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
  publishRecordingEvent("recording-start-request");
  setRecordUi("requesting");
  try {
    await new Promise((resolve) => setTimeout(resolve, 300));
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
      if (!recordChunks.length) {
        uploadStatus.textContent = "Ошибка: запись не содержит аудиоданных.";
        cleanupRecording();
        return;
      }
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
    scheduleRecordAutoStop();
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
    stopRecordAutoStop();
    setRecordUi("paused");
  } else if (recordRecorder.state === "paused") {
    recordRecorder.resume();
    recordStartedAt = Date.now();
    recordTimerHandle = setInterval(updateRecordTimer, 1000);
    scheduleRecordAutoStop();
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
  stopRecordAutoStop();
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
    consultation_type: "consultation_type",
    clinic_division: "clinic_division",
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
      consultation_type: params.get("consultation_type") || null,
      clinic_division: params.get("clinic_division") || null,
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
  const consultationTypeFromQuery = params.get("consultation_type");
  if (consultationTypeFromQuery) {
    consultationTypeInput.value = consultationTypeFromQuery;
  }
  const clinicDivisionFromQuery = params.get("clinic_division");
  if (clinicDivisionFromQuery) {
    clinicDivisionInput.value = clinicDivisionFromQuery;
  }
}

function syncOnecFormFromQuery() {
  if (!onecForm) return;
  const params = new URLSearchParams(window.location.search);
  const queryMap = {
    consultation_date: "consultation_date",
    consultation_type: "consultation_type",
    clinic_division: "clinic_division",
    doctor_code: "doctor_code",
    doctor_full_name: "doctor_full_name",
    doctor_position: "doctor_position",
    doctor_category: "doctor_category",
    patient_code: "patient_code",
    patient_full_name: "patient_full_name",
    patient_birth_date: "patient_birth_date",
    patient_age: "patient_age",
    patient_gender: "patient_gender",
    patient_phones: "patient_phones",
    patient_emails: "patient_emails",
  };

  Object.entries(queryMap).forEach(([paramName, fieldName]) => {
    const input = onecForm.querySelector(`[name="${fieldName}"]`);
    if (!input) return;
    const value = params.get(paramName);
    if (value == null) return;
    if (fieldName === "consultation_date" || fieldName === "patient_birth_date") {
      input.value = formatDmyDate(value);
    } else if (fieldName === "patient_phones" || fieldName === "patient_emails") {
      input.value = splitList(value).join("\n");
    } else {
      input.value = value;
    }
  });
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
  consultationTypeInput.value = params.get("consultation_type") || "primary_adult";
  clinicDivisionInput.value = params.get("clinic_division") || "";
  if (currentUser?.role === "admin" && params.get("consultation_date")) {
    consultationDateInput.value = formatDmyDate(params.get("consultation_date"));
  }
}

function applyOnecToUploadForm() {
  if (!onecForm) return;
  const fd = new FormData(onecForm);
  const fieldValue = (name) => String(fd.get(name) || "").trim();
  const consultationDate = formatDmyDate(fieldValue("consultation_date")) || formatDmyDate(todayIsoLocal());
  const doctorFullName = fieldValue("doctor_full_name") || currentUser?.doctor_name || currentUser?.username || "";
  const hiddenMap = {
    source_payload_json: JSON.stringify({
      consultation_date: fieldValue("consultation_date") || null,
      consultation_type: fieldValue("consultation_type") || null,
      clinic_division: fieldValue("clinic_division") || null,
      doctor: {
        code: fieldValue("doctor_code") || null,
        full_name: fieldValue("doctor_full_name") || null,
        position: fieldValue("doctor_position") || null,
        category: fieldValue("doctor_category") || null,
      },
      patient: {
        code: fieldValue("patient_code") || null,
        full_name: fieldValue("patient_full_name") || null,
        birth_date: fieldValue("patient_birth_date") || null,
        age: fieldValue("patient_age") || null,
        gender: fieldValue("patient_gender") || null,
        phones: splitList(fd.get("patient_phones")),
        emails: splitList(fd.get("patient_emails")),
      },
    }),
    doctor_name: doctorFullName,
    patient_name: fieldValue("patient_full_name"),
    consultation_date: consultationDate,
    consultation_type: fieldValue("consultation_type") || "primary_adult",
    clinic_division: fieldValue("clinic_division"),
    doctor_code: fieldValue("doctor_code"),
    doctor_position: fieldValue("doctor_position"),
    doctor_category: fieldValue("doctor_category"),
    patient_code: fieldValue("patient_code"),
    patient_birth_date: fieldValue("patient_birth_date"),
    patient_age: fieldValue("patient_age"),
    patient_gender: fieldValue("patient_gender"),
    patient_phones_json: JSON.stringify(splitList(fd.get("patient_phones"))),
    patient_emails_json: JSON.stringify(splitList(fd.get("patient_emails"))),
  };
  Object.entries(hiddenMap).forEach(([name, value]) => {
    const input = uploadForm.querySelector(`[name="${name}"]`);
    if (input) input.value = value;
  });
}

async function fetchConsultations() {
  const res = await apiFetch("/api/consultations");
  const items = await res.json();
  listBody.innerHTML = "";
  items.forEach((item) => {
    const tr = document.createElement("tr");
    const canDelete = currentUser && (canViewAllRecords(currentUser) || currentUser.doctor_name === item.doctor_name);
    tr.innerHTML = `
      <td>${item.consultation_date}</td>
      <td>${consultationTypeLabel(item.consultation_type)}</td>
      <td>${escapeHtml(item.clinic_division || "—")}</td>
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
    const loginToken = extractLoginToken(user.login_link);
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
      <td><input type="text" class="u-token" value="${escapeHtml(loginToken)}" readonly></td>
      <td class="row-actions">
        <button type="button" class="btn-save">Сохранить</button>
        ${user.username === "admin" ? "" : '<button type="button" class="btn-delete">Удалить</button>'}
      </td>
    `;
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
  try {
    const fd = buildUploadFormData();
    const res = await apiFetch("/api/consultations/upload", { method: "POST", body: fd });
    const data = await readApiResponseData(res);
    if (!res.ok) throw new Error(formatErrorMessage(data, `HTTP ${res.status} ${res.statusText || ""}`.trim()));
    window.location.href = `/record/${data.id}`;
  } catch (err) {
    uploadStatus.textContent = "Ошибка: " + formatErrorMessage(err, "Ошибка загрузки");
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

recordChannel?.addEventListener("message", (event) => {
  handleRecordingEvent(event.data);
});

window.addEventListener("storage", (event) => {
  if (event.key !== RECORD_COORDINATION_KEY || !event.newValue) return;
  try {
    handleRecordingEvent(JSON.parse(event.newValue));
  } catch {
    // Ignore malformed coordination messages.
  }
});

window.addEventListener("pageshow", () => {
  if (!recordRecorder || recordRecorder.state === "inactive") {
    cleanupRecording();
  }
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
  syncOnecFormFromQuery();
  onecForm.addEventListener("input", applyOnecToUploadForm);
  applyOnecToUploadForm();
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  const initialView =
    requestedView === "records" && canViewAllRecords(currentUser)
      ? "records"
      : requestedView === "users" && currentUser.role === "admin"
        ? "users"
        : "upload";
  setView(initialView);
  if (initialView === "upload") {
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
