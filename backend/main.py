"""RevVeritas FastAPI backend (Concept 1 + observability).

Exposes the audit pipeline, per-finding reasoning traces, and the human-approval
gate, and serves the single-screen dashboard. Every agent step is also written to
a JSONL trace file by the Tracer.
"""
from __future__ import annotations

import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import config
from agents import runtime
from agents.orchestrator import OrchestratorAgent
from models import Finding, FindingStatus, LeakReport
from observability import Tracer
from tools import case_memory, data_loader

app = FastAPI(title="RevVeritas", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = os.path.join(config.ROOT, "frontend")


def _page(name: str, media_type: str = "text/html"):
    path = os.path.join(FRONTEND_DIR, name)
    if os.path.exists(path):
        return FileResponse(path, media_type=media_type)
    return JSONResponse({"message": f"RevVeritas API up. {name} not found."}, status_code=404)

# In-memory store of the most recent audit (findings keyed by signature).
STATE: dict = {"report": None, "run_id": None, "generated_at": None, "by_sig": {}}


@app.on_event("startup")
def _startup() -> None:
    case_memory.reset_memory()        # fresh slate per server start (demo-friendly)
    data_loader.clear_cache()


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _slim(f: Finding) -> dict:
    return {
        "signature": f.signature, "leak_type": f.leak_type.value,
        "customer_id": f.customer_id, "contract_id": f.contract_id,
        "product": f.product, "dollar_impact": f.dollar_impact,
        "confidence": round(f.confidence, 2), "status": f.status.value,
        "explanation": f.explanation, "priority": round(f.dollar_impact * f.confidence, 2),
    }


def _evidence_rows(f: Finding) -> dict:
    """The conflicting contract / invoice / usage rows behind a finding."""
    contracts = data_loader.load_contracts(f.customer_id)
    invoices = data_loader.load_invoices(f.customer_id)
    usage = data_loader.load_usage(f.customer_id)
    return {
        "contracts": contracts[contracts["product"] == f.product].to_dict("records"),
        "invoices": invoices[invoices["contract_id"] == f.contract_id]
            .sort_values("invoice_date").to_dict("records"),
        "usage": usage[usage["product"] == f.product].to_dict("records"),
    }


def _summary(report: LeakReport) -> dict:
    breakdown: dict = defaultdict(lambda: {"confirmed_count": 0, "confirmed_dollars": 0.0,
                                           "weighted_dollars": 0.0})
    for f in report.confirmed:
        b = breakdown[f.leak_type.value]
        b["confirmed_count"] += 1
        b["confirmed_dollars"] = round(b["confirmed_dollars"] + f.dollar_impact, 2)
        b["weighted_dollars"] = round(b["weighted_dollars"] + f.dollar_impact * f.confidence, 2)
    counts = defaultdict(int)
    for f in report.findings:
        counts[f.status.value] += 1
    return {
        "customers_audited": report.customers_audited,
        "headline_total": report.headline_total,
        "needs_review_total": round(sum(f.dollar_impact for f in report.needs_review), 2),
        "breakdown": [{"leak_type": k, **v} for k, v in sorted(
            breakdown.items(), key=lambda kv: kv[1]["confirmed_dollars"], reverse=True)],
        "counts": dict(counts),
        "judgment_engine": "gemini" if runtime.available() else "heuristic",
        "run_id": STATE["run_id"], "generated_at": STATE["generated_at"],
    }


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "judgment_engine": "gemini" if runtime.available() else "heuristic",
            "model": config.GEMINI_MODEL if runtime.available() else None,
            "dataset": "custom" if data_loader.is_custom_dataset() else "demo"}


@app.post("/api/audit")
def run_audit(customer_id: str | None = None) -> dict:
    tracer = Tracer()
    report = OrchestratorAgent(tracer=tracer).run(customer_id=customer_id)
    STATE.update(report=report, run_id=tracer.run_id,
                 generated_at=datetime.now(timezone.utc).isoformat(),
                 by_sig={f.signature: f for f in report.findings})
    return {**_summary(report),
            "findings": sorted((_slim(f) for f in report.findings),
                               key=lambda d: d["priority"], reverse=True)}


@app.get("/api/report")
def get_report() -> dict:
    if STATE["report"] is None:
        return run_audit()
    return {**_summary(STATE["report"]),
            "findings": sorted((_slim(f) for f in STATE["report"].findings),
                               key=lambda d: d["priority"], reverse=True)}


@app.get("/api/findings/{signature}")
def finding_detail(signature: str) -> dict:
    f = STATE["by_sig"].get(signature)
    if f is None:
        raise HTTPException(404, "finding not found (run an audit first)")
    return {**_slim(f), "suggested_action": f.suggested_action,
            "recovery_draft": f.recovery_draft, "judged_by": f.judged_by,
            "evidence": _evidence_rows(f),
            "trace": [s.model_dump() for s in f.trace]}


@app.post("/api/findings/{signature}/approve")
def approve(signature: str) -> dict:
    f = STATE["by_sig"].get(signature)
    if f is None:
        raise HTTPException(404, "finding not found")
    case_memory.mark_resolved(signature)
    f.status = FindingStatus.RESOLVED
    return {"signature": signature, "status": "RESOLVED",
            "message": "Approved & marked resolved in case memory. Nothing was sent."}


@app.post("/api/findings/{signature}/reject")
def reject(signature: str) -> dict:
    f = STATE["by_sig"].get(signature)
    if f is None:
        raise HTTPException(404, "finding not found")
    case_memory.mark_resolved(signature)        # won't re-surface in future audits
    f.status = FindingStatus.DISMISSED
    return {"signature": signature, "status": "DISMISSED",
            "message": "Rejected. Suppressed from future audits via case memory."}


# --------------------------------------------------------------------------- #
# Dataset upload
# --------------------------------------------------------------------------- #
UPLOAD_DIR = os.path.join(config.DATA_DIR, "uploads")

REQUIRED_COLS = {
    "contracts": {"contract_id", "customer_id", "product", "contracted_unit_price",
                  "committed_quantity", "discount_pct", "discount_expiry_date",
                  "term_start", "term_end", "auto_renew", "minimum_commit_amount"},
    "invoices": {"invoice_id", "customer_id", "contract_id", "product",
                 "billed_unit_price", "billed_quantity", "invoice_date", "amount"},
    "usage": {"customer_id", "product", "month", "actual_usage_quantity"},
}


def _validate_csv(file_path: str, kind: str) -> tuple[bool, str]:
    """Check that a CSV has the required columns."""
    try:
        df = pd.read_csv(file_path, nrows=2)
    except Exception as e:
        return False, f"{kind}: failed to parse CSV — {e}"
    missing = REQUIRED_COLS[kind] - set(df.columns)
    if missing:
        return False, f"{kind}: missing columns — {', '.join(sorted(missing))}"
    return True, ""


@app.post("/api/upload-dataset")
async def upload_dataset(
    contracts: UploadFile = File(...),
    invoices: UploadFile = File(...),
    usage: UploadFile = File(...),
) -> dict:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    paths = {}
    for kind, f in [("contracts", contracts), ("invoices", invoices), ("usage", usage)]:
        dest = os.path.join(UPLOAD_DIR, f"{kind}.csv")
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        ok, msg = _validate_csv(dest, kind)
        if not ok:
            # Clean up on validation failure
            shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
            raise HTTPException(422, msg)
        paths[kind] = dest

    data_loader.override_paths(paths["contracts"], paths["invoices"], paths["usage"])
    case_memory.reset_memory()
    STATE.update(report=None, run_id=None, generated_at=None, by_sig={})

    # Return summary
    summary = {}
    for kind, path in paths.items():
        df = pd.read_csv(path)
        summary[kind] = {"rows": len(df), "columns": list(df.columns)}
    return {"status": "ok", "message": "Custom dataset loaded. Run an audit to see results.",
            "dataset": "custom", "summary": summary}


@app.post("/api/reset-dataset")
def reset_dataset() -> dict:
    data_loader.reset_overrides()
    case_memory.reset_memory()
    STATE.update(report=None, run_id=None, generated_at=None, by_sig={})
    return {"status": "ok", "message": "Reverted to demo dataset.", "dataset": "demo"}


@app.get("/")
def landing():
    return _page("landing.html")


@app.get("/signin")
def signin():
    return _page("signin.html")


@app.get("/signup")
def signup():
    return _page("signup.html")


@app.get("/app")
def dashboard():
    return _page("index.html")


@app.get("/theme.css")
def theme():
    return _page("theme.css", media_type="text/css")
