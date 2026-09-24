const consultationId = window.location.pathname.split("/").filter(Boolean).pop();
const authRequired = document.getElementById("auth-required");
const detailHeader = document.getElementById("detail-header");
const tabTranscript = document.getElementById("tab-transcript");
const tabEvaluation = document.getElementById("tab-evaluation");

let pollTimer = null;
let currentUser = null;
const authRetryDelayMs = 250;

function canViewAllRecords(user) {
  return user?.role === "admin" || user?.role === "supervisor";
}

function consultationTypeLabel(value) {
  if (value === "primary_child") return "Первичная детская";
  if (value === "repeat_adult") return "Повторная взрослая";
  return "Первичная взрослая";
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
  const isAdmin = currentUser?.role === "admin";
  detailHeader.innerHTML = `
    <div class="meta">
      <div><strong>Пациент:</strong> ${escapeHtml(data.patient_name)}</div>
      <div><strong>Врач:</strong> ${escapeHtml(data.doctor_name)}</div>
      <div><strong>Дата:</strong> ${data.consultation_date}</div>
      <div><strong>Вид консультации:</strong> ${consultationTypeLabel(data.consultation_type)}</div>
      <div><strong>Подразделение:</strong> ${escapeHtml(data.clinic_division || "—")}</div>
      <div><strong>Оценка:</strong> ${data.overall_score != null ? data.overall_score.toFixed(1) + " / 5" : "—"}</div>
      <div class="status-row">
        <span><strong>Статус:</strong> <span class="status-badge ${data.status}">${statusLabel(data.status)}</span></span>
        <span class="detail-actions">
          ${data.retry_available ? '<button type="button" id="retry-processing-button">Повторить обработку</button>' : ""}
          ${data.export_available ? '<button type="button" id="export-audio-button">Выгрузить аудио</button>' : ""}
          ${data.restore_available ? '<button type="button" id="restore-audio-button">Загрузить запись с сервера</button>' : ""}
          ${canDelete ? '<button type="button" class="btn-delete">Удалить</button>' : ""}
        </span>
      </div>
      ${data.status === "invalid_audio" ? '<div class="hint">Запись почти без звука. Проверьте микрофон или источник файла и загрузите запись со слышимой речью.</div>' : ""}
      ${isAdmin ? '<div id="remote-audio-status" class="status"></div>' : ""}
      ${isAdmin && data.remote_export_status === "exporting" ? '<div>Аудио выгружается…</div>' : ""}
      ${isAdmin && ["pending", "restoring"].includes(data.remote_restore_status) ? '<div>Аудио восстанавливается…</div>' : ""}
      ${isAdmin && data.remote_export_error ? `<div class="error-text"><strong>Выгрузка:</strong> ${escapeHtml(data.remote_export_error)}</div>` : ""}
      ${isAdmin && data.remote_restore_error ? `<div class="error-text"><strong>Восстановление:</strong> ${escapeHtml(data.remote_restore_error)}</div>` : ""}
      ${currentUser?.role === "admin" && data.processing_stage ? `<div><strong>Этап:</strong> ${escapeHtml(data.processing_stage)}</div>` : ""}
      ${currentUser?.role === "admin" && data.speechkit_operation_id ? `<div><strong>SpeechKit ID:</strong> ${escapeHtml(data.speechkit_operation_id)}</div>` : ""}
      ${currentUser?.role === "admin" && data.error_message ? `<div class="error-text"><strong>Ошибка:</strong> ${escapeHtml(data.error_message)}</div>` : ""}
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

  const exportAudioBtn = detailHeader.querySelector("#export-audio-button");
  if (exportAudioBtn) {
    const exportStatus = detailHeader.querySelector("#remote-audio-status");
    exportAudioBtn.onclick = async () => {
      exportAudioBtn.disabled = true;
      exportStatus.textContent = "Ставим выгрузку в очередь…";
      try {
        const resExport = await apiFetch(`/api/consultations/${consultationId}/export-audio`, { method: "POST" });
        const payload = await resExport.json().catch(() => ({}));
        if (!resExport.ok) {
          throw new Error(formatErrorMessage(payload, "Не удалось выгрузить аудио"));
        }
        exportStatus.textContent = payload.message;
      } catch (err) {
        exportStatus.textContent = "Ошибка выгрузки: " + formatErrorMessage(err, "Не удалось выгрузить аудио");
      } finally {
        exportAudioBtn.disabled = false;
      }
    };
  }

  const restoreAudioBtn = detailHeader.querySelector("#restore-audio-button");
  if (restoreAudioBtn) {
    const remoteStatus = detailHeader.querySelector("#remote-audio-status");
    restoreAudioBtn.onclick = async () => {
      restoreAudioBtn.disabled = true;
      remoteStatus.textContent = "Ставим восстановление в очередь…";
      try {
        const response = await apiFetch(`/api/consultations/${consultationId}/restore-audio`, { method: "POST" });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(formatErrorMessage(payload, "Не удалось восстановить аудио"));
        await loadDetail();
      } catch (err) {
        remoteStatus.textContent = "Ошибка: " + formatErrorMessage(err, "Не удалось восстановить аудио");
        restoreAudioBtn.disabled = false;
      }
    };
  }

  const retryBtn = detailHeader.querySelector("#retry-processing-button");
  if (retryBtn) {
    retryBtn.onclick = async () => {
      retryBtn.disabled = true;
      try {
        const resRetry = await apiFetch(`/api/consultations/${consultationId}/retry`, { method: "POST" });
        const payload = await resRetry.json().catch(() => ({}));
        if (!resRetry.ok) {
          throw new Error(formatErrorMessage(payload, "Не удалось повторить обработку"));
        }
        await loadDetail();
      } catch (err) {
        alert("Ошибка: " + formatErrorMessage(err, "Не удалось повторить обработку"));
      } finally {
        retryBtn.disabled = false;
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
