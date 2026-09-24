from app.models import Consultation


def reset_for_manual_retry(row: Consultation) -> int:
    """Start a new generation while keeping durable completed stages."""
    row.status = "uploaded"
    row.error_message = None
    row.error_category = None
    row.processing_attempts = 0
    row.stage_first_failure_at = None
    row.worker_interruptions = 0
    row.step_running = False
    row.processing_generation += 1
    row.lease_until = None
    row.queued_at = None
    return row.processing_generation
