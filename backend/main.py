import asyncio
import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from pipelines import architecture_pipeline, ingestion_pipeline, logic_pipeline, security_pipeline
from pipelines.pipeline_utils import safe_extract_zip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

app = FastAPI(title="NeuroSense Unified Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=4)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/run-agent")
async def run_agent(
    request: Request,
    file: UploadFile = File(...),
    agent: str = Form(...),
    model: str = Form("llama3.1:8b"),
    skip_llm: bool = Form(False),
):
    """
    Unified endpoint:
    1. Accept project ZIP (max 50 MB)
    2. Run ingestion pipeline
    3. Run selected agent pipeline
    4. Return structured JSON output
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit")

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    agent = agent.lower()
    if agent not in ("architecture", "logic", "security", "developer"):
        raise HTTPException(status_code=400, detail="Invalid agent type")

    logger.info("run-agent request: agent=%s model=%s skip_llm=%s", agent, model, skip_llm)
    start_time = time.time()

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit")

    def _run_pipelines() -> dict:
        with tempfile.TemporaryDirectory() as td:
            upload_path = os.path.join(td, "upload.zip")
            with open(upload_path, "wb") as f:
                f.write(raw)

            ingestion_out_dir = os.path.join(td, "ingestion_output")
            os.makedirs(ingestion_out_dir, exist_ok=True)
            ingestion_pipeline.ingest_zip(upload_path, ingestion_out_dir)

            ingestion_zip_path = shutil.make_archive(
                os.path.join(td, "ingestion_output"),
                "zip",
                ingestion_out_dir,
            )

            if agent == "architecture":
                return architecture_pipeline.run_architecture_pipeline(
                    ingestion_input=ingestion_zip_path,
                    model=model,
                    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                    skip_llm=skip_llm,
                )
            if agent in ("developer", "logic"):
                return logic_pipeline.run_logic_pipeline(
                    ingestion_input=ingestion_zip_path,
                    model=model,
                    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                    skip_llm=skip_llm,
                )
            if agent == "security":
                source_dir = os.path.join(td, "source")
                os.makedirs(source_dir, exist_ok=True)
                safe_extract_zip(upload_path, source_dir)
                return security_pipeline.run_security_pipeline(
                    ingestion_input=ingestion_zip_path,
                    source_dir=source_dir,
                    model=model,
                    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                    skip_llm=skip_llm,
                )
            raise ValueError(f"Unhandled agent: {agent}")

    loop = asyncio.get_event_loop()

    try:
        report_payload = await loop.run_in_executor(_executor, _run_pipelines)
    except Exception as exc:
        logger.exception("Pipeline failed for agent=%s", agent)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    elapsed = round(time.time() - start_time, 2)
    logger.info("run-agent completed: agent=%s elapsed=%.2fs", agent, elapsed)

    return {
        "status": "completed",
        "agent": agent,
        "analysis_time_seconds": elapsed,
        "model_used": model,
        "report": report_payload,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
