from types import SimpleNamespace

import numpy as np

from advisor import portal_sync


class FakeEncoder:
    name = "fake"

    def encode_passages(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


RECORDS = [
    {"id": "policy-1", "section": "policy", "title": "Attendance rule", "text": "Students need 75 percent attendance."},
    {"id": "cms-2", "section": "cms", "title": "Parking", "text": "Permits cost 77 rupees."},
    {"id": "cms-3", "section": "cms", "title": "Empty", "text": "  "},
]


def test_records_become_authoritative_portal_chunks():
    chunks, vectors = portal_sync.build(RECORDS, FakeEncoder())
    assert len(chunks) == 2 == len(vectors)  # the empty record is dropped
    assert {c["kind"] for c in chunks} == {portal_sync.KIND}
    assert chunks[0]["url"].startswith("doc://portal/")
    assert any("75 percent" in c["text"] for c in chunks)


def test_save_and_load_round_trip(tmp_path):
    chunks, vectors = portal_sync.build(RECORDS, FakeEncoder())
    portal_sync.save(tmp_path, chunks, vectors, "fake")
    loaded, loaded_vectors, model = portal_sync.load(tmp_path)
    assert loaded == chunks and model == "fake"
    assert loaded_vectors.shape == vectors.shape


def test_missing_store_loads_empty(tmp_path):
    assert portal_sync.load(tmp_path / "none") == ([], None, "")


def test_export_url_is_derived_from_the_sso_url():
    s = SimpleNamespace(portal_export_url="", auth_verify_url="http://backend:8000/api/v1/auth/me")
    assert portal_sync.export_url(s) == "http://backend:8000/api/v1/admin/chatbot-export"
    assert portal_sync.export_url(SimpleNamespace(portal_export_url="", auth_verify_url="")) == ""


def test_only_admin_listed_fee_items_bypass_the_official_fee_table():
    from advisor.rag.service import asks_admin_only_fee

    fees = SimpleNamespace(data={"pages": [{"groups": [{"rows": [{"head": "Tuition Fee"}, {"head": "Admission Fee"}]}]}]})
    record = {"url": "doc://portal/fees/bscs", "text": "| Shift | Fee | Amount |\n|---|---|---|\n"
              "| Morning | tuition fee | PKR 50,000 |\n| Morning | zebra lab fee | PKR 43,210 |"}
    bundle = SimpleNamespace(fees=fees, chunks=[record, {"url": "https://qau.edu.pk/fees", "text": "| a | lab | b |"}])
    assert asks_admin_only_fee("What is the zebra lab fee for BSCS?", bundle)
    assert not asks_admin_only_fee("What is the tuition fee for BSCS?", bundle)  # official table answers it
    assert not asks_admin_only_fee("What are the BSCS fees?", bundle)
    assert not asks_admin_only_fee("zebra lab fee", SimpleNamespace(fees=None, chunks=[record]))
