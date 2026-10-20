"""Holds the live retriever and hot-swaps it when a new index bundle is published."""

from __future__ import annotations

import asyncio
import logging
import threading

from advisor.config import Settings
from advisor.retrieval.bundle import Bundle, load_bundle
from advisor.retrieval.models import Encoder, Reranker
from advisor.retrieval.retriever import Retriever

log = logging.getLogger("advisor.knowledge")


class KnowledgeBase:
    def __init__(self, settings: Settings):
        self.s = settings
        self.retriever: Retriever | None = None
        self.bundle: Bundle | None = None
        self.encoder: Encoder | None = None
        self.reranker: Reranker | None = None
        self.reranker_error = ""
        self.base: Bundle | None = None  # the published build, before portal records are merged in
        self.portal_chunks: list[dict] = []
        self.portal_vectors = None
        self.portal_model = ""
        self._compute = asyncio.Semaphore(2)  # bound concurrent CPU-heavy embedding/rerank work
        self._lock = threading.Lock()

    def load(self) -> None:
        """Blocking load (run in a thread at start-up and after a re-index)."""
        import os

        import torch

        # Two compute slots share the CPU; letting each use every core just oversubscribes it.
        torch.set_num_threads(max(2, (os.cpu_count() or 4) // 2))
        with self._lock:
            bundle = load_bundle(self.s.index_dir)
            if bundle is None:
                log.warning("no index bundle found in %s", self.s.index_dir)
                return
            model = bundle.manifest.get("embedding_model", self.s.embedding_model)
            if self.encoder is None or self.encoder.name != model:
                self.encoder = Encoder(model, self.s.embedding_device, self.s.models_local_only)
            if self.reranker is None and self.s.reranker_model:
                try:
                    self.reranker = Reranker(
                        self.s.reranker_model, self.s.embedding_device, self.s.models_local_only
                    )
                except Exception as exc:  # reranking is an optional quality boost
                    self.reranker_error = str(exc)[:200]
                    log.warning("reranker unavailable: %s", exc)
            self.base = bundle
            if not self.portal_chunks:
                from advisor import portal_sync

                self.portal_chunks, self.portal_vectors, self.portal_model = portal_sync.load(portal_sync.store_dir(self.s))
            self._publish()
            log.info("knowledge base ready: build %s (%d chunks, %d from the portal)",
                     bundle.build_id, len(self.bundle.chunks), len(self.bundle.chunks) - len(bundle.chunks))

    def _publish(self) -> None:
        """Merge the portal records into the published build and swap the live retriever."""
        import numpy as np

        base = self.base
        extra = self.portal_chunks if self.portal_vectors is not None and self.portal_vectors.shape[0] else []
        model = base.manifest.get("embedding_model", self.s.embedding_model)
        if extra and self.portal_model != model:
            log.warning("portal records were embedded with %s, the index uses %s; sync again", self.portal_model, model)
            extra = []
        bundle = base if not extra else Bundle(
            base.path, base.chunks + extra, np.vstack([base.vectors, self.portal_vectors]),
            {**base.manifest, "chunks": len(base.chunks) + len(extra), "portal_chunks": len(extra)},
        )
        self.retriever = Retriever(bundle, self.encoder, self.reranker)
        self.bundle = bundle

    def set_portal_records(self, chunks: list[dict], vectors, model: str) -> None:
        with self._lock:
            self.portal_chunks, self.portal_vectors, self.portal_model = chunks, vectors, model
            if self.base is not None:
                self._publish()

    @property
    def ready(self) -> bool:
        return self.retriever is not None

    def info(self) -> dict:
        if not self.bundle:
            return {"ready": False}
        m = self.bundle.manifest
        return {
            "ready": True, "build_id": m["build_id"], "created_at": m.get("created_at"),
            "chunks": m["chunks"], "portal_chunks": m.get("portal_chunks", 0), "embedding_model": m.get("embedding_model"),
            "reranker": self.reranker.name if self.reranker else None,
            "reranker_error": self.reranker_error,
        }
