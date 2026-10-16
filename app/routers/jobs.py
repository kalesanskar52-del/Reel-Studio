import json
import importlib.util
import shutil
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from app.config import settings
from app.db import connect, now
from app.schemas import RecordingJobRequest, ExplainerJobRequest, RenderRequest, TranscriptEdit
from app.services.jobs import create_job, get_job, list_jobs
from app.services.jobs import set_job_status
from app.services.cancellation import cancel_job
from app.services.demo import process_demo_job
from app.services.processing import run_ai_job
from app.utils.files import path_is_within

router = APIRouter(prefix="/api/v1", tags=["studio jobs"])


def require_gemini() -> None:
    if settings.demo_mode:
        return
    if not settings.gemini_api_key:
        raise HTTPException(503, "Gemini is not configured. Add GEMINI_API_KEY to backend/.env and restart the server.")


def require_recording_providers() -> None:
    if settings.demo_mode:
        return
    if importlib.util.find_spec("faster_whisper") is None:
        raise HTTPException(503, "Local transcription is not installed. Run pip install -r requirements.txt in backend and restart.")
    if not settings.gemini_api_key:
        raise HTTPException(503, "Gemini is not configured. Add GEMINI_API_KEY to backend/.env and restart the server.")


def ensure_brand_exists(brand_id: str | None) -> None:
    if not brand_id:
        return
    with connect() as db:
        exists = db.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Brand kit not found")


def run_demo_job(job_id: str, kind: str, payload: dict):
    set_job_status(job_id, "processing", 25)
    try:
        result = process_demo_job(job_id, kind, payload)
        set_job_status(job_id, "completed", 100, result=result)
    except Exception as exc:
        set_job_status(job_id, "failed", 0, error=f"Demo processing failed: {exc}")


@router.post("/jobs/recording", status_code=202)
def create_recording_job(body: RecordingJobRequest, background_tasks: BackgroundTasks):
    require_recording_providers()
    with connect() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (body.asset_id,)).fetchone()
        if not asset:
            raise HTTPException(404, "Recording asset not found; upload the source video first")
    ensure_brand_exists(body.brand_id)
    payload = body.model_dump() | {"source_path": asset["stored_path"], "source_filename": asset["filename"], "source_content_type": asset["content_type"], "source_size_bytes": asset["size_bytes"]}
    job = create_job("recording_highlights", payload)
    if settings.demo_mode:
        background_tasks.add_task(run_demo_job, job["id"], job["kind"], job["input_json"])
    else:
        background_tasks.add_task(run_ai_job, job["id"], job["kind"], job["input_json"])
    return job


@router.post("/jobs/explainer", status_code=202)
def create_explainer_job(body: ExplainerJobRequest, background_tasks: BackgroundTasks):
    require_gemini()
    ensure_brand_exists(body.brand_id)
    job = create_job("topic_explainer", body.model_dump())
    if settings.demo_mode:
        background_tasks.add_task(run_demo_job, job["id"], job["kind"], job["input_json"])
    else:
        background_tasks.add_task(run_ai_job, job["id"], job["kind"], job["input_json"])
    return job


@router.get("/jobs")
def get_jobs(limit: int = Query(default=50, ge=1, le=200)):
    return list_jobs(limit)


@router.get("/jobs/{job_id}")
def get_one_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.patch("/jobs/{job_id}/transcript")
def edit_job_transcript(job_id: str, body: TranscriptEdit):
    with connect() as db:
        row = db.execute("SELECT kind,status,result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found")
        if row["kind"] != "recording_highlights":
            raise HTTPException(409, "Only class recording transcripts can be edited")
        if row["status"] in {"queued", "processing"}:
            raise HTTPException(409, "Wait for recording processing to finish before saving transcript edits")
        result = json.loads(row["result_json"] or "{}")
        if "transcript_text" not in result:
            raise HTTPException(409, "This recording does not have a transcript to edit")
        result["transcript_text"] = body.transcript_text
        changed = db.execute(
            "UPDATE jobs SET result_json=?,updated_at=? WHERE id=? AND status NOT IN ('queued','processing')",
            (json.dumps(result, ensure_ascii=False), now(), job_id),
        ).rowcount
        if not changed:
            raise HTTPException(409, "Recording processing has not finished; try saving again shortly")
    return get_job(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel_one_job(job_id: str):
    job = cancel_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in {"cancelled", "queued", "processing"}:
        raise HTTPException(409, f"A {job['status']} job cannot be cancelled")
    return job


@router.post("/clips/{clip_id}/render", status_code=202)
def request_clip_render(clip_id: str, body: RenderRequest, background_tasks: BackgroundTasks):
    if not settings.demo_mode and settings.enable_video_rendering and not (shutil.which("ffmpeg") or importlib.util.find_spec("imageio_ffmpeg")):
        raise HTTPException(503, "FFmpeg is unavailable. Install requirements.txt or add FFmpeg to PATH.")
    with connect() as db:
        clip = db.execute("SELECT id,job_id,metadata_json FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not clip:
        raise HTTPException(404, "Clip not found")
    ensure_brand_exists(body.brand_id)
    metadata = json.loads(clip["metadata_json"])
    job = create_job("clip_render", {"clip_id": clip_id, "source_path": metadata.get("source_path"), **body.model_dump()})
    if settings.demo_mode:
        background_tasks.add_task(run_demo_job, job["id"], job["kind"], job["input_json"])
    else:
        background_tasks.add_task(run_ai_job, job["id"], job["kind"], job["input_json"])
    return job


@router.get("/jobs/{job_id}/download")
def download_job_output(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    result = job.get("result_json") or {}
    output = result.get("output_path")
    if not output:
        raise HTTPException(404, "This job has no rendered video output")
    output_path = Path(output)
    if not path_is_within(output_path, settings.output_dir) or not output_path.is_file():
        raise HTTPException(404, "Rendered video is unavailable")
    return FileResponse(output_path, media_type="video/mp4", filename=output_path.name)
