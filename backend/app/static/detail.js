const consultationId = window.location.pathname.split("/").filter(Boolean).pop();
const authRequired = document.getElementById("auth-required");
const detailHeader = document.getElementById("detail-header");
const tabTranscript = document.getElementById("tab-transcript");
const tabEvaluation = document.getElementById("tab-evaluation");

let pollTimer = null;
let currentUser = null;
const authRetryDelayMs = 250;

function canViewAllRecords(user) {
  return user?.role === "admin" || user?.can_view_all_records === true;
}

async function loadDetail() {
  const res = await apiFetch(`/api/consultations/${consultationId}`);
  if (!res.ok) {
    if (res.status === 403) {
      detailHeader.innerHTML = "<p>Недостаточно прав для просмотра этой записи.</p>";
      return;
    }
    detailHeader.innerHTML = "<p>Запись не найдена.</p>";
    return;
  }
  const data = await res.json();

  document.title = `${data.patient_name} — SpeechAI`;

  const canDelete = currentUser && (canViewAllRecords(currentUser) || currentUser.doctor_name === data.doctor_name);
  detailHeader.innerHTML = `
    <div class="meta">
      <div><strong>Пациент:</strong> ${escapeHtml(data.patient_name)}</div>
      <div><strong>Врач:</strong> ${escapeHtml(data.doctor_name)}</div>
      <div><strong>Дата:</strong> ${data.consultation_date}</div>
      <div><strong>Оценка:</strong> ${data.overall_score != null ? data.overall_score.toFixed(1) + " / 5" : "—"}</div>
      <div class="status-row">
        <span><strong>Статус:</strong> <span class="status-badge ${data.status}">${statusLabel(data.status)}</span></span>
        ${canDelete ? '<button type="button" class="btn-delete">Удалить</button>' : ""}
      </div>
      ${data.error_message ? `<div class="error-text"><strong>Ошибка:</strong> ${escapeHtml(data.error_message)}</div>` : ""}
    </div>
  `;

  const deleteBtn = detailHeader.querySelector(".btn-delete");
  if (deleteBtn) {
    deleteBtn.onclick = async () => {
      try {
        if (await deleteConsultation(consultationId)) window.location.href = "/";
      } catch (err) {
        alert("Ошибка: " + err.message);
      }
    };
  }

  if (data.segments && data.segments.length) {
    tabTranscript.innerHTML = data.segments
      .map((s) => {
        const role = s.speaker_role === "doctor" ? "Врач" : "Пациент";
        const cls = s.speaker_role === "doctor" ? "doctor" : "patient";
        return `<div class="msg ${cls}"><span class="time">${formatMs(s.start_ms)} · ${role}</span>${escapeHtml(s.text)}</div>`;
      })
      .join("");
  } else if (data.status === "processing" || data.status === "uploaded") {
    tabTranscript.textContent = "Идёт обработка…";
  } else {
    tabTranscript.textContent = data.transcript_text || "Нет данных";
  }

  tabEvaluation.innerHTML = data.evaluation_report
    ? `<div class="evaluation-text">${escapeHtml(data.evaluation_report)}</div>`
    : data.status === "processing" || data.status === "uploaded"
      ? "Ожидание оценки…"
      : "—";

  const pending = data.status === "processing" || data.status === "uploaded";
  if (pending && !pollTimer) {
    pollTimer = setInterval(loadDetail, 3000);
  }
  if (!pending && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    tabTranscript.hidden = tab !== "transcript";
    tabEvaluation.hidden = tab !== "evaluation";
  });
});

async function initPage() {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    currentUser = await getCurrentUser();
    if (currentUser) break;
    if (attempt === 0) {
      await new Promise((resolve) => setTimeout(resolve, authRetryDelayMs));
    }
  }
  if (!currentUser) {
    authRequired.hidden = false;
    setTimeout(() => {
      window.location.href = "/";
    }, 800);
    return;
  }

  renderAuthBar(currentUser);
  await loadDetail();
}

initPage().catch((err) => {
  if (err.code === 401) {
    window.location.href = "/";
    return;
  }
  detailHeader.innerHTML = `<p>Ошибка: ${escapeHtml(err.message)}</p>`;
});
