const loginSection = document.getElementById("login-section");
const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const uploadSection = document.getElementById("upload-section");
const uploadForm = document.getElementById("upload-form");
const uploadStatus = document.getElementById("upload-status");
const recordsSection = document.getElementById("records-section");
const onecSection = document.getElementById("onec-section");
const onecForm = document.getElementById("onec-form");
const listBody = document.querySelector("#list-table tbody");
const doctorNameInput = uploadForm.querySelector('[name="doctor_name"]');
const doctorNameLabel = document.getElementById("doctor-name-label");

let pollTimer = null;
let currentUser = null;

function applyRoleUi(user) {
  loginSection.hidden = true;
  uploadSection.hidden = false;
  recordsSection.hidden = false;
  onecSection.hidden = user.role !== "admin";
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

function splitOnecList(value) {
  return value
    .replace(/\n/g, ";")
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean);
}

function isoToDisplayDate(value) {
  if (!value) return "";
  const trimmed = String(value).trim();
  if (!trimmed) return "";
  const parts = trimmed.split("-");
  if (parts.length === 3) {
    return `${parts[2]}/${parts[1]}/${parts[0]}`;
  }
  return trimmed;
}

function displayToIsoDate(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  const parts = trimmed.split("/");
  if (parts.length === 3) {
    return `${parts[2]}-${parts[1]}-${parts[0]}`;
  }
  return trimmed;
}

function readOnecForm() {
  const fd = new FormData(onecForm);
  const doctor = {
    code: String(fd.get("doctor_code") || "").trim() || null,
    full_name: String(fd.get("doctor_full_name") || "").trim() || null,
    position: String(fd.get("doctor_position") || "").trim() || null,
    category: String(fd.get("doctor_category") || "").trim() || null,
  };
  const patientPhones = splitOnecList(String(fd.get("patient_phones") || ""));
  const patientEmails = splitOnecList(String(fd.get("patient_emails") || ""));
  const patient = {
    code: String(fd.get("patient_code") || "").trim() || null,
    full_name: String(fd.get("patient_full_name") || "").trim() || null,
    birth_date: String(fd.get("patient_birth_date") || "").trim() || null,
    age: String(fd.get("patient_age") || "").trim() || null,
    gender: String(fd.get("patient_gender") || "").trim() || null,
    phones: patientPhones,
    emails: patientEmails,
  };
  return {
    consultation_date: String(fd.get("consultation_date") || "").trim() || null,
    doctor,
    patient,
  };
}

function syncOnecToUpload() {
  const payload = readOnecForm();
  const hiddenMap = {
    source_payload_json: JSON.stringify(payload),
    doctor_code: payload.doctor.code || "",
    doctor_position: payload.doctor.position || "",
    doctor_category: payload.doctor.category || "",
    patient_code: payload.patient.code || "",
    patient_birth_date: payload.patient.birth_date || "",
    patient_age: payload.patient.age || "",
    patient_gender: payload.patient.gender || "",
    patient_phones_json: JSON.stringify(payload.patient.phones),
    patient_emails_json: JSON.stringify(payload.patient.emails),
  };

  Object.entries(hiddenMap).forEach(([name, value]) => {
    const input = uploadForm.querySelector(`[name="${name}"]`);
    if (input) input.value = value;
  });

  if (payload.doctor.full_name) {
    doctorNameInput.value = payload.doctor.full_name;
  }
  const patientInput = uploadForm.querySelector('[name="patient_name"]');
  if (payload.patient.full_name) {
    patientInput.value = payload.patient.full_name;
  }
  const consultationDateInput = uploadForm.querySelector('[name="consultation_date"]');
  if (payload.consultation_date) {
    consultationDateInput.value = displayToIsoDate(payload.consultation_date);
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

onecForm.addEventListener("input", syncOnecToUpload);

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
  syncOnecToUpload();
  await fetchList();
}

function fillOnecFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const map = {
    consultation_date: "consultation_date",
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

  Object.entries(map).forEach(([paramName, fieldName]) => {
    const value = params.get(paramName);
    if (!value) return;
    const field = onecForm.querySelector(`[name="${fieldName}"]`);
    if (!field) return;
    if (fieldName === "consultation_date" || fieldName === "patient_birth_date") {
      field.value = isoToDisplayDate(value);
      return;
    }
    field.value = value;
  });
}

fillOnecFromQuery();
initPage().catch((err) => {
  loginStatus.textContent = "Ошибка: " + err.message;
});
