"""Seed advisor knowledge base from local academic data and snapshots."""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "backend")

from advisor.config import get_settings
from advisor.ingest.pipeline import (
    IngestReport,
    build_chunks,
    collect_documents,
)
from advisor.ingest.snapshots import PageRecord, SnapshotStore
from advisor.retrieval.bundle import load_bundle, set_current, write_bundle
from advisor.retrieval.models import Encoder


def seed_knowledge():
    settings = get_settings()
    academic_dir = Path("/academic-data")
    if not academic_dir.exists():
        academic_dir = Path("academic-data")

    print(f"Using academic-data from: {academic_dir}")
    print(f"Target data_dir: {settings.data_dir}")
    print(f"Target index_dir: {settings.index_dir}")

    store = SnapshotStore(settings.snapshot_dir)

    # 1. Populate HTML snapshots
    snap_dir = academic_dir / "source-registry" / "snapshots"
    if snap_dir.exists():
        for f in snap_dir.glob("*.html"):
            b = f.read_bytes()
            name = f.stem.replace("_php", ".php").replace("_html", ".html")
            url = f"https://cs.qau.edu.pk/{name}"
            sha = store.put_bytes(b, ".html")
            store.upsert(
                PageRecord(
                    url=url,
                    final_url=url,
                    status=200,
                    content_type="text/html",
                    sha256=sha,
                    ext=".html",
                    bytes=len(b),
                    etag="",
                    last_modified="",
                    fetched_at="2026-08-23T00:00:00Z",
                    changed_at=None,
                    error=None,
                    discovered_from=None,
                )
            )
            print(f"Added snapshot: {url}")

    # 2. Populate scraped pages from cs_website_full.json
    full_json = academic_dir / "scraped" / "cs_website_full.json"
    if full_json.exists():
        with full_json.open(encoding="utf-8") as fh:
            pages = json.load(fh)
        for p in pages:
            url = p["url"]
            if store.get(url):
                continue
            title = p.get("title", "")
            content = p.get("content", "")
            html = f"<!DOCTYPE html><html><head><title>{title}</title></head><body><h1>{title}</h1><div>{content}</div></body></html>"
            b = html.encode("utf-8")
            sha = store.put_bytes(b, ".html")
            store.upsert(
                PageRecord(
                    url=url,
                    final_url=url,
                    status=200,
                    content_type="text/html",
                    sha256=sha,
                    ext=".html",
                    bytes=len(b),
                    etag="",
                    last_modified="",
                    fetched_at=p.get("timestamp") or "2026-08-23T00:00:00Z",
                    changed_at=None,
                    error=None,
                    discovered_from=None,
                )
            )

    # 3. Populate PDFs
    for pdf in academic_dir.glob("**/*.pdf"):
        b = pdf.read_bytes()
        url = f"https://cs.qau.edu.pk/doc/{pdf.name}"
        if not store.get(url):
            sha = store.put_bytes(b, ".pdf")
            store.upsert(
                PageRecord(
                    url=url,
                    final_url=url,
                    status=200,
                    content_type="application/pdf",
                    sha256=sha,
                    ext=".pdf",
                    bytes=len(b),
                    etag="",
                    last_modified="",
                    fetched_at="2026-08-23T00:00:00Z",
                    changed_at=None,
                    error=None,
                    discovered_from=None,
                )
            )
            print(f"Added PDF: {pdf.name}")

    print(f"Snapshot catalogue pages: {len(store.all_ok())}")

    # 4. Ingest and build chunks
    report = IngestReport()
    prereq_dir = settings.data_dir / "prerequisites"
    prereq_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = settings.data_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Keep repository-maintained departmental documents in the same ingestion
    # path as documents uploaded through the admin console.  Previously the
    # FYP procedure existed only in a legacy LLM prompt, so the live grounded
    # advisor could never retrieve or cite it.
    bundled_docs_dir = academic_dir / "department-documents"
    if bundled_docs_dir.exists():
        for source in bundled_docs_dir.iterdir():
            if source.is_file():
                target = docs_dir / source.name
                shutil.copy2(source, target)
                print(f"Added department document: {source.name}")

    print("Collecting documents from snapshot store...")
    docs = collect_documents(store, report, prereq_dir, docs_dir)
    print(f"Collected {len(docs)} documents.")

    print("Building chunks...")
    chunks = build_chunks(docs, report)
    print(f"Built {len(chunks)} chunks: {report.summary()}")

    # 5. Embed passages
    model_name = settings.embedding_model
    print(f"Embedding {len(chunks)} passages with {model_name}...")
    enc = Encoder(model_name, settings.embedding_device, settings.models_local_only)
    vectors = enc.encode_passages([c.index_text for c in chunks])
    print(f"Vectors shape: {vectors.shape}")

    # 6. Sidecar files
    files = {}
    if report.prereq_graph is not None:
        files["prereq_graph.json"] = json.dumps(
            report.prereq_graph.to_json(), ensure_ascii=False
        )
    if report.fee_pages:
        files["fees.json"] = json.dumps({"pages": report.fee_pages}, ensure_ascii=False)
    if report.timetable:
        files["timetable.json"] = json.dumps(report.timetable, ensure_ascii=False)

    # 7. Write index bundle
    print("Writing index bundle...")
    path = write_bundle(
        settings.index_dir,
        chunks,
        vectors,
        {
            "embedding_model": model_name,
            "ingest": report.summary(),
            "errors": report.errors[:20],
            "prerequisites": report.prereq_notes,
        },
        files,
    )
    print(f"Successfully published bundle: {path.name}")
    print(f"Setting CURRENT to {path.name}")
    set_current(settings.index_dir, path.name)

    # 8. Verify bundle loads
    bundle = load_bundle(settings.index_dir)
    assert bundle is not None, "Bundle failed to load!"
    print(
        f"Verification: Bundle loaded with {len(bundle.chunks)} chunks, {bundle.vectors.shape[0]} vectors!"
    )
    print("Knowledge base indexing complete!")


if __name__ == "__main__":
    seed_knowledge()
