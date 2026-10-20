"""Answer from what admins enter in the portal (policies, fees, courses, knowledge, CMS).

The portal exports its verified records; we embed just those (a few hundred passages, seconds on
CPU) and the knowledge base merges them into the live index. No full re-index is needed, and the
records are kept on disk so they survive a restart.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np

from advisor.ingest.model import Block, Document
from advisor.ingest.pipeline import build_chunks

log = logging.getLogger("advisor.portal_sync")

KIND = "portal_record"
SECTION_LABELS = {"policy": "rule", "fees": "fee schedule", "course": "course record",
                  "knowledge": "knowledge entry", "cms": "notice"}


def export_url(settings) -> str:
    if settings.portal_export_url:
        return settings.portal_export_url
    if settings.auth_verify_url.endswith("/auth/me"):
        return settings.auth_verify_url[: -len("/auth/me")] + "/admin/chatbot-export"
    return ""


def store_dir(settings) -> Path:
    return settings.data_dir / "portal"


def to_documents(records: list[dict]) -> list[Document]:
    docs = []
    for r in records:
        slug = re.sub(r"[^a-z0-9]+", "-", str(r["id"]).lower()).strip("-")
        title = str(r.get("title") or "").strip() or "Department record"
        docs.append(Document(
            url=f"doc://portal/{r.get('section', 'record')}/{slug}", title=title, kind=KIND,
            blocks=[Block(heading=title, text=str(r.get("text") or "").strip(), atomic=True)],
            source_type="portal", priority=10,  # entered by department admins: as authoritative as uploads
            meta={"section": r.get("section", ""), "variant": slug,
                  "note": f"Department {SECTION_LABELS.get(r.get('section'), 'record')} maintained by the administrator."},
        ))
    return [d for d in docs if d.blocks[0].text]


def build(records: list[dict], encoder) -> tuple[list[dict], np.ndarray]:
    chunks = [asdict(c) for c in build_chunks(to_documents(records))]
    if not chunks:
        return [], np.zeros((0, 0), dtype=np.float32)
    return chunks, encoder.encode_passages([c["index_text"] for c in chunks])


def save(directory: Path, chunks: list[dict], vectors: np.ndarray, model: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "chunks.json.tmp"
    tmp.write_text(json.dumps({"model": model, "chunks": chunks}, ensure_ascii=False))
    np.save(directory / "vectors.tmp.npy", vectors.astype(np.float32))
    (directory / "vectors.tmp.npy").replace(directory / "vectors.npy")
    tmp.replace(directory / "chunks.json")


def load(directory: Path) -> tuple[list[dict], np.ndarray | None, str]:
    try:
        data = json.loads((directory / "chunks.json").read_text())
        vectors = np.load(directory / "vectors.npy").astype(np.float32)
    except (OSError, ValueError):
        return [], None, ""
    if len(data["chunks"]) != len(vectors):
        log.warning("portal records and vectors disagree; ignoring them until the next sync")
        return [], None, ""
    return data["chunks"], vectors, data.get("model", "")


async def fetch(settings, authorization: str) -> list[dict]:
    url = export_url(settings)
    if not url:
        raise RuntimeError("No portal export URL is configured (set ADVISOR_PORTAL_EXPORT_URL).")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers={"Authorization": authorization})
    if resp.status_code != 200:
        raise RuntimeError(f"The portal export failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()["documents"]
