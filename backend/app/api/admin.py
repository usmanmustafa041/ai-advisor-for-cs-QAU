import csv
import io
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import admin_user, hash_password
from app.core.database import get_db
from app.nlp.transformer import get_intent_classifier, model_status
from app.rag.corpus import split_passages, text_problem

router = APIRouter(prefix="/admin", tags=["administration"])


class CourseCreate(BaseModel):
    code: str = Field(min_length=2, max_length=20)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    theory_credit_hours: float = Field(default=3, ge=0, le=9)
    lab_credit_hours: float = Field(default=0, ge=0, le=9)
    source_id: str | None = None


class FeeCreate(BaseModel):
    program_code: str | None = None
    official_fee_category: str = Field(min_length=1)
    shift: str = Field(min_length=1)
    fee_type: str = Field(min_length=1)
    amount: float = Field(ge=0)
    currency: str = Field(default="PKR", min_length=3, max_length=3)
    effective_from: date
    effective_to: date | None = None
    source_id: str


class PolicyCreate(BaseModel):
    rule_code: str = Field(min_length=1, max_length=50)
    category: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    effective_from: date
    source_id: str
    priority: int = Field(default=100, ge=0, le=1000)


class TimetableCreate(BaseModel):
    offering_id: str
    session_type: str = Field(pattern="^(class|lab|tutorial)$")
    day_of_week: int = Field(ge=1, le=7)
    starts_at: str = Field(pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    ends_at: str = Field(pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    room: str = Field(min_length=1)
    lab_group: str | None = None


class PrerequisiteCreate(BaseModel):
    course_code: str = Field(min_length=2, max_length=20)
    prerequisite_course_code: str = Field(min_length=2, max_length=20)
    curriculum: str = "Fall 2025 onward"
    relation_type: str = Field(default="prerequisite", pattern="^(prerequisite|corequisite)$")
    minimum_grade: str | None = None
    waiver_condition: str | None = None
    source_id: str
    verified: bool = False


class KnowledgeCreate(BaseModel):
    source_id: str
    title: str = Field(min_length=3, max_length=300)
    category: str = Field(min_length=2, max_length=50)
    content: str = Field(min_length=20)


class SettingUpdate(BaseModel):
    value: object


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="student", pattern="^(student|admin)$")


def _audit(db: Session, user_id: str, action: str, entity_type: str, entity_id: str, before=None, after=None) -> None:
    db.execute(text("""INSERT INTO audit_log
        (actor_user_id, action, entity_type, entity_id, before_data, after_data)
        VALUES (:user_id, :action, :entity_type, :entity_id, CAST(:before AS jsonb), CAST(:after AS jsonb))"""),
        {"user_id": user_id, "action": action, "entity_type": entity_type, "entity_id": entity_id,
         # default=str: rows carry Decimal credit hours and date values
         "before": json.dumps(before, default=str) if before is not None else None,
         "after": json.dumps(after, default=str) if after is not None else None})


def _commit(db: Session, message: str = "Record conflicts with existing data") -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=message) from None


@router.get("/sources")
def sources(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT id::text, source_code, title, verification_status
        FROM source_records ORDER BY verification_status, source_code""")).mappings()
    return [dict(r) for r in rows]


@router.get("/offerings")
def offerings(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT o.id::text, c.code AS course_code, at.term, at.academic_year, o.section
        FROM course_offerings o JOIN courses c ON c.id=o.course_id JOIN academic_terms at ON at.id=o.term_id
        ORDER BY at.academic_year DESC, at.term, c.code, o.section""")).mappings()
    return [dict(r) for r in rows]


@router.get("/prerequisites")
def prerequisites(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT cp.id::text, c.code AS course_code,
        pc.code AS prerequisite_course_code, cs.name AS curriculum, cp.relation_type,
        cp.minimum_grade, cp.waiver_condition, cp.verified, sr.source_code
        FROM course_prerequisites cp JOIN courses c ON c.id=cp.course_id
        JOIN courses pc ON pc.id=cp.prerequisite_course_id
        JOIN curriculum_schemes cs ON cs.id=cp.curriculum_id
        JOIN source_records sr ON sr.id=cp.source_id
        ORDER BY c.code, pc.code""")).mappings()
    return [dict(r) for r in rows]


@router.post("/prerequisites", status_code=201)
def create_prerequisite(payload: PrerequisiteCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    if payload.course_code.upper() == payload.prerequisite_course_code.upper():
        raise HTTPException(status_code=422, detail="A course cannot be its own prerequisite")
    row = db.execute(text("""INSERT INTO course_prerequisites
        (curriculum_id, course_id, prerequisite_course_id, relation_type, minimum_grade,
         waiver_condition, source_id, verified)
        SELECT cs.id, c.id, pc.id, :relation_type, :minimum_grade, :waiver_condition,
               CAST(:source_id AS uuid), :verified
        FROM curriculum_schemes cs JOIN courses c ON upper(c.code)=upper(:course_code)
        JOIN courses pc ON upper(pc.code)=upper(:prerequisite_course_code)
        WHERE cs.name=:curriculum RETURNING id::text"""), payload.model_dump()).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Course, prerequisite course, or curriculum was not found")
    result = {"id": row, **payload.model_dump()}
    _audit(db, user["id"], "create", "prerequisite", row, after=result)
    _commit(db, "This prerequisite relationship already exists")
    return result


@router.patch("/prerequisites/{relationship_id}")
def update_prerequisite(relationship_id: str, payload: dict, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"relation_type", "minimum_grade", "waiver_condition", "verified", "source_id"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=422, detail="No editable fields supplied")
    if updates.get("relation_type") not in {None, "prerequisite", "corequisite"}:
        raise HTTPException(status_code=422, detail="Invalid relationship type")
    assignments = ", ".join(f"{key}=CAST(:{key} AS uuid)" if key == "source_id" else f"{key}=:{key}" for key in updates)
    updates["id"] = relationship_id
    row = db.execute(text(f"UPDATE course_prerequisites SET {assignments} WHERE id=:id RETURNING id::text"), updates).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Prerequisite relationship not found")
    _audit(db, user["id"], "update", "prerequisite", row, after=payload)
    _commit(db, "This prerequisite relationship already exists")
    return {"updated": True, "id": row}


@router.delete("/prerequisites/{relationship_id}")
def delete_prerequisite(relationship_id: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("DELETE FROM course_prerequisites WHERE id=:id RETURNING id::text"), {"id": relationship_id}).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Prerequisite relationship not found")
    _audit(db, user["id"], "delete", "prerequisite", row)
    db.commit()
    return {"deleted": True, "id": row}


@router.get("/knowledge")
def knowledge(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT kd.id::text, kd.title, kd.category, kd.processing_status,
        sr.source_code, sr.verification_status, COUNT(dc.id)::int AS chunks
        FROM knowledge_documents kd JOIN source_records sr ON sr.id=kd.source_id
        LEFT JOIN document_chunks dc ON dc.document_id=kd.id
        GROUP BY kd.id, sr.source_code, sr.verification_status ORDER BY kd.title""")).mappings()
    return [dict(row) for row in rows]


@router.post("/knowledge", status_code=201)
def create_knowledge(payload: KnowledgeCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    problem = text_problem(payload.content)
    if problem:
        raise HTTPException(422, f"Unusable source text: {problem}")
    paragraphs = split_passages(payload.content)
    document_id = db.execute(text("""INSERT INTO knowledge_documents
        (source_id, title, category, mime_type, storage_path, processing_status, processed_at)
        VALUES (CAST(:source_id AS uuid), :title, :category, 'text/plain',
                'database://admin-entry', 'ready', NOW())
        ON CONFLICT (source_id) DO UPDATE SET title=EXCLUDED.title, category=EXCLUDED.category,
            processing_status='ready', processed_at=NOW(), processing_error=NULL
        RETURNING id::text"""), payload.model_dump()).scalar_one()
    db.execute(text("DELETE FROM document_chunks WHERE document_id=:id"), {"id": document_id})
    for index, content in enumerate(paragraphs):
        db.execute(text("""INSERT INTO document_chunks
            (document_id, chunk_index, content, token_count, embedding, metadata)
            VALUES (:document_id, :chunk_index, :content, :token_count,
                    NULL, CAST(:metadata AS jsonb))"""),
            {"document_id": document_id, "chunk_index": index, "content": content,
             "token_count": max(1, len(content.split())),
             "metadata": json.dumps({"entered_by_admin": True, "title": payload.title, "category": payload.category})})
    result = {"id": document_id, "title": payload.title, "category": payload.category, "chunks": len(paragraphs)}
    _audit(db, user["id"], "upsert", "knowledge_document", document_id, after=result)
    _commit(db, "The selected source is invalid")
    return result


@router.delete("/knowledge/{document_id}")
def delete_knowledge(document_id: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("DELETE FROM knowledge_documents WHERE id=:id RETURNING id::text"), {"id": document_id}).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    _audit(db, user["id"], "delete", "knowledge_document", row); db.commit()
    return {"deleted": True, "id": row}


def _smart_chunk(content: str, max_tokens: int = 150, overlap_sentences: int = 1) -> list[str]:
    """Split content into smart semantic chunks.

    Strategy:
    1. Split on double newlines (paragraphs) first.
    2. If a block is still too large, split by single newlines (good for bullet lists).
    3. If still too large, split by sentences.
    4. Never create a chunk smaller than 10 words.
    """
    import re

    def sentence_split(text: str) -> list[str]:
        # Split on sentence-ending punctuation followed by whitespace
        return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    def token_count(text: str) -> int:
        return max(1, len(text.split()))

    # First pass: double newlines
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [content.strip()]
        
    # Second pass: single newlines for overly large paragraphs
    sub_paragraphs = []
    for para in paragraphs:
        if token_count(para) > max_tokens:
            sub_paragraphs.extend([p.strip() for p in para.split("\n") if p.strip()])
        else:
            sub_paragraphs.append(para)

    chunks: list[str] = []
    for para in sub_paragraphs:
        if token_count(para) <= max_tokens:
            chunks.append(para)
        else:
            # Sentence-level splitting with rolling window
            sentences = sentence_split(para)
            buffer: list[str] = []
            buf_tokens = 0
            for sentence in sentences:
                sent_tokens = token_count(sentence)
                if buf_tokens + sent_tokens > max_tokens and buffer:
                    chunks.append(" ".join(buffer))
                    # Keep overlap sentences
                    buffer = buffer[-overlap_sentences:] if overlap_sentences else []
                    buf_tokens = sum(token_count(s) for s in buffer)
                buffer.append(sentence)
                buf_tokens += sent_tokens
            if buffer:
                chunks.append(" ".join(buffer))

    # Merge tiny trailing chunks into the previous one
    merged: list[str] = []
    for chunk in chunks:
        if merged and token_count(chunk) < 10:
            merged[-1] = merged[-1] + " " + chunk
        else:
            merged.append(chunk)

    return merged or [content.strip()]


@router.post("/knowledge/reindex")
def reindex_knowledge(user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    """Rebuild bounded passages for administrator-entered documents.

    Approved official sources become available to lexical retrieval immediately.
    Dense retrieval requires an explicit corpus/model fingerprinted rebuild.
    """
    docs = db.execute(
        text("SELECT id::text, title, category FROM knowledge_documents WHERE storage_path='database://admin-entry' ORDER BY title")
    ).mappings().all()

    total_chunks = 0
    processed = 0

    for doc in docs:
        doc_id = doc["id"]
        # Gather existing content from chunks (re-join to get full text)
        existing_chunks = db.execute(
            text("SELECT content FROM document_chunks WHERE document_id=:id ORDER BY chunk_index"),
            {"id": doc_id}
        ).scalars().all()

        if not existing_chunks:
            continue

        full_content = "\n\n".join(existing_chunks)
        new_chunks = split_passages(full_content)

        # Delete old chunks and re-insert
        db.execute(text("DELETE FROM document_chunks WHERE document_id=:id"), {"id": doc_id})
        for idx, content in enumerate(new_chunks):
            db.execute(
                text("""INSERT INTO document_chunks
                    (document_id, chunk_index, content, token_count, embedding, metadata)
                    VALUES (:document_id, :chunk_index, :content, :token_count,
                            NULL, CAST(:metadata AS jsonb))"""),
                {
                    "document_id": doc_id,
                    "chunk_index": idx,
                    "content": content,
                    "token_count": max(1, len(content.split())),
                    "metadata": json.dumps({
                        "entered_by_admin": True,
                        "reindexed": True,
                        "title": doc["title"],
                        "category": doc["category"],
                    }),
                }
            )
        # Mark as ready
        db.execute(
            text("UPDATE knowledge_documents SET processing_status='ready', processed_at=NOW() WHERE id=:id"),
            {"id": doc_id}
        )
        total_chunks += len(new_chunks)
        processed += 1

    _audit(db, user["id"], "reindex", "knowledge_documents", "all",
           after={"documents_processed": processed, "chunks_created": total_chunks})
    db.commit()
    return {"documents_processed": processed, "chunks_created": total_chunks}



@router.get("/model")
def model(_: dict = Depends(admin_user)) -> dict:
    return model_status()


@router.post("/model/reload")
def reload_model(user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    get_intent_classifier.cache_clear()
    status = model_status()
    _audit(db, user["id"], "reload", "nlp_model", status.get("model_name", "unknown"), after=status)
    db.commit()
    return status


@router.get("/settings")
def settings(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    return [dict(row) for row in db.execute(text("""SELECT key, value, description,
        updated_at::text AS updated_at FROM system_settings ORDER BY key""")).mappings()]


@router.patch("/settings/{key}")
def update_setting(key: str, payload: SettingUpdate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("""UPDATE system_settings SET value=CAST(:value AS jsonb),
        updated_by=:user_id, updated_at=NOW() WHERE key=:key
        RETURNING key, value, description, updated_at::text AS updated_at"""),
        {"key": key, "value": json.dumps(payload.value), "user_id": user["id"]}).mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Setting not found")
    result = dict(row); _audit(db, user["id"], "update", "setting", key, after=result); db.commit()
    return result


@router.get("/courses")
def courses(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    return [dict(r) for r in db.execute(text("SELECT code, title, description, theory_credit_hours, lab_credit_hours, active FROM courses ORDER BY code")).mappings()]


@router.post("/courses", status_code=201)
def create_course(payload: CourseCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    if payload.theory_credit_hours + payload.lab_credit_hours <= 0:
        raise HTTPException(status_code=422, detail="A course must have at least one credit hour")
    row = db.execute(text("""INSERT INTO courses
        (code, title, description, theory_credit_hours, lab_credit_hours, source_id)
        VALUES (upper(:code), :title, :description, :theory, :lab, CAST(:source_id AS uuid))
        RETURNING code, title, description,
                  theory_credit_hours::double precision AS theory_credit_hours,
                  lab_credit_hours::double precision AS lab_credit_hours, active"""),
        {"code": payload.code.strip(), "title": payload.title, "description": payload.description,
         "theory": payload.theory_credit_hours, "lab": payload.lab_credit_hours, "source_id": payload.source_id}).mappings().one()
    result = dict(row); _audit(db, user["id"], "create", "course", result["code"], after=result); _commit(db, "Course code already exists")
    return result


@router.patch("/courses/{course_code}")
def update_course(course_code: str, payload: dict, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"title", "description", "theory_credit_hours", "lab_credit_hours", "active"}; updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates: raise HTTPException(status_code=422, detail="No editable fields supplied")
    before = db.execute(text("SELECT code, title, description, theory_credit_hours, lab_credit_hours, active FROM courses WHERE upper(code)=upper(:code)"), {"code": course_code}).mappings().one_or_none()
    if not before: raise HTTPException(status_code=404, detail="Course not found")
    assignments = ", ".join(f"{k}=:{k}" for k in updates); updates["code"] = course_code
    row = db.execute(text(f"UPDATE courses SET {assignments}, updated_at=NOW() WHERE upper(code)=upper(:code) RETURNING code, title, description, theory_credit_hours, lab_credit_hours, active"), updates).mappings().one()
    result = dict(row); _audit(db, user["id"], "update", "course", result["code"], dict(before), result); _commit(db)
    return result


@router.delete("/courses/{course_code}")
def delete_course(course_code: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("UPDATE courses SET active=FALSE, updated_at=NOW() WHERE upper(code)=upper(:code) RETURNING code, title"), {"code": course_code}).mappings().one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Course not found")
    result = dict(row); _audit(db, user["id"], "disable", "course", result["code"], after={**result, "active": False}); db.commit()
    return {"disabled": True, **result}


@router.get("/fees")
def fees(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT f.id::text, p.code AS program_code, f.official_fee_category, f.shift, f.fee_type,
        f.amount, f.currency, f.effective_from::text, f.effective_to::text FROM fee_structures f
        LEFT JOIN programs p ON p.id=f.program_id ORDER BY f.effective_from DESC""")).mappings()
    return [dict(r) for r in rows]


@router.post("/fees", status_code=201)
def create_fee(payload: FeeCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    if payload.effective_to and payload.effective_to < payload.effective_from: raise HTTPException(status_code=422, detail="Invalid effective date range")
    row = db.execute(text("""INSERT INTO fee_structures
        (program_id, official_fee_category, shift, fee_type, amount, currency, effective_from, effective_to, source_id)
        VALUES ((SELECT id FROM programs WHERE upper(code)=upper(:program_code)), :category, :shift, :fee_type,
        :amount, upper(:currency), :effective_from, :effective_to, CAST(:source_id AS uuid)) RETURNING id::text"""),
        {"program_code": payload.program_code, "category": payload.official_fee_category, "shift": payload.shift,
         "fee_type": payload.fee_type, "amount": payload.amount, "currency": payload.currency,
         "effective_from": payload.effective_from, "effective_to": payload.effective_to, "source_id": payload.source_id}).scalar_one()
    _audit(db, user["id"], "create", "fee", row, after=payload.model_dump(mode="json")); _commit(db)
    return {"id": row, **payload.model_dump(mode="json")}


@router.patch("/fees/{fee_id}")
def update_fee(fee_id: str, payload: dict, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"official_fee_category", "shift", "fee_type", "amount", "currency", "effective_from", "effective_to"}; updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates: raise HTTPException(status_code=422, detail="No editable fields supplied")
    assignments = ", ".join(f"{k}=:{k}" for k in updates); updates["id"] = fee_id
    row = db.execute(text(f"UPDATE fee_structures SET {assignments} WHERE id=:id RETURNING id::text"), updates).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Fee record not found")
    _audit(db, user["id"], "update", "fee", row, after=payload); _commit(db); return {"updated": True, "id": row}


@router.delete("/fees/{fee_id}")
def delete_fee(fee_id: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("DELETE FROM fee_structures WHERE id=:id RETURNING id::text"), {"id": fee_id}).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Fee record not found")
    _audit(db, user["id"], "delete", "fee", row); db.commit(); return {"deleted": True, "id": row}


@router.get("/timetables")
def timetables(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(text("""SELECT t.id::text, t.offering_id::text, c.code AS course_code, at.term, at.academic_year,
        t.session_type, t.day_of_week, t.starts_at::text, t.ends_at::text, t.room, t.lab_group
        FROM timetable_entries t JOIN course_offerings o ON o.id=t.offering_id JOIN courses c ON c.id=o.course_id
        JOIN academic_terms at ON at.id=o.term_id ORDER BY at.academic_year DESC, at.term, t.day_of_week, t.starts_at""")).mappings()
    return [dict(r) for r in rows]


@router.post("/timetables", status_code=201)
def create_timetable(payload: TimetableCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("""INSERT INTO timetable_entries
        (offering_id, session_type, day_of_week, starts_at, ends_at, room, lab_group)
        VALUES (CAST(:offering_id AS uuid), :session_type, :day_of_week, CAST(:starts_at AS time), CAST(:ends_at AS time), :room, :lab_group) RETURNING id::text"""), payload.model_dump()).scalar_one()
    _audit(db, user["id"], "create", "timetable", row, after=payload.model_dump()); _commit(db, "Timetable entry conflicts with existing data")
    return {"id": row, **payload.model_dump()}


@router.patch("/timetables/{entry_id}")
def update_timetable(entry_id: str, payload: dict, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"session_type", "day_of_week", "starts_at", "ends_at", "room", "lab_group"}; updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates: raise HTTPException(status_code=422, detail="No editable fields supplied")
    assignments = ", ".join(f"{k}=:{k}" for k in updates); updates["id"] = entry_id
    row = db.execute(text(f"UPDATE timetable_entries SET {assignments} WHERE id=:id RETURNING id::text"), updates).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Timetable entry not found")
    _audit(db, user["id"], "update", "timetable", row, after=payload); _commit(db, "Timetable entry conflicts with existing data"); return {"updated": True, "id": row}


@router.delete("/timetables/{entry_id}")
def delete_timetable(entry_id: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("DELETE FROM timetable_entries WHERE id=:id RETURNING id::text"), {"id": entry_id}).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Timetable entry not found")
    _audit(db, user["id"], "delete", "timetable", row); db.commit(); return {"deleted": True, "id": row}


@router.get("/policies")
def policies(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    return [dict(r) for r in db.execute(text("SELECT id::text, rule_code, category, title, description, effective_from::text, effective_to::text, priority, active FROM academic_rules ORDER BY category, priority")).mappings()]


@router.post("/policies", status_code=201)
def create_policy(payload: PolicyCreate, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("""INSERT INTO academic_rules
        (rule_code, category, title, description, effective_from, priority, source_id)
        VALUES (:rule_code, :category, :title, :description, :effective_from, :priority, CAST(:source_id AS uuid)) RETURNING id::text"""), payload.model_dump()).scalar_one()
    _audit(db, user["id"], "create", "policy", row, after=payload.model_dump(mode="json")); _commit(db)
    return {"id": row, **payload.model_dump(mode="json")}


@router.patch("/policies/{policy_id}")
def update_policy(policy_id: str, payload: dict, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"category", "title", "description", "effective_from", "effective_to", "priority", "active"}; updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates: raise HTTPException(status_code=422, detail="No editable fields supplied")
    assignments = ", ".join(f"{k}=:{k}" for k in updates); updates["id"] = policy_id
    row = db.execute(text(f"UPDATE academic_rules SET {assignments} WHERE id=:id RETURNING id::text"), updates).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Policy not found")
    _audit(db, user["id"], "update", "policy", row, after=payload); _commit(db); return {"updated": True, "id": row}


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: str, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    row = db.execute(text("UPDATE academic_rules SET active=FALSE WHERE id=:id RETURNING id::text"), {"id": policy_id}).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Policy not found")
    _audit(db, user["id"], "disable", "policy", row); db.commit(); return {"disabled": True, "id": row}


@router.get("/users")
def users(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> list[dict]:
    return [dict(r) for r in db.execute(text("""SELECT id::text, full_name, email, role, active,
        last_login_at::text AS last_login_at, created_at::text AS created_at
        FROM app_users ORDER BY created_at DESC""")).mappings()]


@router.post("/users", status_code=201)
def create_user(payload: UserCreate, admin: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    try:
        row = db.execute(text("""INSERT INTO app_users (full_name, email, password_hash, role)
            VALUES (:full_name, :email, :password_hash, :role)
            RETURNING id::text, full_name, email, role, active,
                last_login_at::text AS last_login_at, created_at::text AS created_at"""), {
            "full_name": payload.full_name.strip(),
            "email": payload.email.lower().strip(),
            "password_hash": hash_password(payload.password),
            "role": payload.role,
        }).mappings().one()
        result = dict(row)
        _audit(db, admin["id"], "create", "user", result["id"],
               after={k: result[k] for k in ("full_name", "email", "role", "active")})
        _commit(db, "Email is already registered")
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from None


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: dict, admin: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    allowed = {"full_name", "email", "role", "active", "password"}; updates = {k: v for k, v in payload.items() if k in allowed}
    if not updates: raise HTTPException(status_code=422, detail="No editable fields supplied")
    if updates.get("role") not in {None, "student", "admin"}: raise HTTPException(status_code=422, detail="Invalid role")
    if user_id == admin["id"] and updates.get("active") is False: raise HTTPException(status_code=422, detail="Administrators cannot disable their own account")
    if "password" in updates: updates["password_hash"] = hash_password(str(updates.pop("password")))
    assignments = ", ".join(f"{k}=:{k}" for k in updates); updates["id"] = user_id
    row = db.execute(text(f"UPDATE app_users SET {assignments}, updated_at=NOW() WHERE id=:id RETURNING id::text, email, role, active"), updates).mappings().one_or_none()
    if not row: raise HTTPException(status_code=404, detail="User not found")
    result = dict(row); _audit(db, admin["id"], "update", "user", user_id, after=result); _commit(db, "Email is already registered"); return result


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    if user_id == admin["id"]:
        raise HTTPException(status_code=422, detail="Administrators cannot delete their own account")
    row = db.execute(text("""SELECT id::text, full_name, email, role, active
        FROM app_users WHERE id=:id"""), {"id": user_id}).mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        db.execute(text("DELETE FROM chat_sessions WHERE user_id=:id"), {"id": user_id})
        db.execute(text("DELETE FROM app_users WHERE id=:id"), {"id": user_id})
        _audit(db, admin["id"], "delete", "user", user_id, before=dict(row))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This user has linked academic records and cannot be deleted; deactivate the account instead",
        ) from None
    return {"deleted": True, "id": user_id}


def _query_log_rows(db: Session, search: str | None, email: str | None, start: date | None,
                    end: date | None, limit: int, offset: int) -> list[dict]:
    params = {"search": search, "pattern": f"%{search}%" if search else None, "email": email,
              "start": start, "end": end, "limit": limit, "offset": offset}
    rows = db.execute(text("""SELECT m.id::text, COALESCE(u.email, 'Guest') AS email,
        s.id::text AS session_id, m.content, m.intent, m.intent_confidence,
        m.response_time_ms, m.created_at::text AS created_at
        FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id
        LEFT JOIN app_users u ON u.id=s.user_id WHERE m.role='user'
        AND (CAST(:search AS text) IS NULL OR m.content ILIKE CAST(:pattern AS text) OR m.intent ILIKE CAST(:pattern AS text))
        AND (CAST(:email AS text) IS NULL OR u.email ILIKE CAST(:email AS text))
        AND (CAST(:start AS date) IS NULL OR m.created_at >= CAST(:start AS date))
        AND (CAST(:end AS date) IS NULL OR m.created_at < CAST(:end AS date) + INTERVAL '1 day')
        ORDER BY m.created_at DESC LIMIT :limit OFFSET :offset"""), params).mappings()
    return [dict(r) for r in rows]


@router.get("/query-logs")
def query_logs(_: dict = Depends(admin_user), db: Session = Depends(get_db),
               search: str | None = None, email: str | None = None,
               start: date | None = None, end: date | None = None,
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> list[dict]:
    return _query_log_rows(db, search, email, start, end, limit, offset)


@router.get("/query-logs-export")
def export_query_logs(_: dict = Depends(admin_user), db: Session = Depends(get_db),
                      search: str | None = None, email: str | None = None,
                      start: date | None = None, end: date | None = None) -> StreamingResponse:
    rows = _query_log_rows(db, search, email, start, end, 5000, 0)
    output = io.StringIO()
    columns = ["id", "email", "session_id", "content", "intent", "intent_confidence", "response_time_ms", "created_at"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader(); writer.writerows(rows)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=qau-query-logs.csv"})


@router.get("/logs")
def audit_logs(_: dict = Depends(admin_user), db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    return [dict(r) for r in db.execute(text("SELECT id, action, entity_type, entity_id, occurred_at::text AS occurred_at FROM audit_log ORDER BY occurred_at DESC LIMIT :limit"), {"limit": limit}).mappings()]


@router.get("/report")
def report(start: date | None = None, end: date | None = None, _: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    if start and end and end < start: raise HTTPException(status_code=422, detail="End date cannot precede start date")
    params = {"start": start, "end": end}
    counts = db.execute(text("""SELECT COUNT(*)::int AS total_messages, COUNT(DISTINCT session_id)::int AS total_sessions,
        COUNT(DISTINCT intent)::int AS intents_seen, MIN(created_at)::text AS first_message, MAX(created_at)::text AS last_message,
        ROUND(AVG(response_time_ms))::int AS average_response_ms FROM chat_messages WHERE role='user'
        AND (CAST(:start AS date) IS NULL OR created_at >= CAST(:start AS date)) AND (CAST(:end AS date) IS NULL OR created_at < CAST(:end AS date) + INTERVAL '1 day')"""), params).mappings().one()
    intents = db.execute(text("""SELECT COALESCE(intent, 'unknown') AS intent, COUNT(*)::int AS count FROM chat_messages
        WHERE role='user' AND (CAST(:start AS date) IS NULL OR created_at >= CAST(:start AS date)) AND (CAST(:end AS date) IS NULL OR created_at < CAST(:end AS date) + INTERVAL '1 day')
        GROUP BY intent ORDER BY count DESC"""), params).mappings()
    activity = db.execute(text("""SELECT COUNT(*)::int AS total_users,
        COUNT(*) FILTER (WHERE active AND last_login_at>=NOW()-INTERVAL '30 days')::int AS active_users_30d,
        MAX(last_login_at)::text AS latest_login FROM app_users WHERE role='student'""")).mappings().one()
    duration = db.execute(text("""SELECT ROUND(AVG(minutes),2) FROM (
        SELECT EXTRACT(EPOCH FROM (COALESCE(MAX(m.created_at), s.ended_at, s.started_at)-s.started_at))/60.0 AS minutes
        FROM chat_sessions s LEFT JOIN chat_messages m ON m.session_id=s.id
        WHERE s.user_id IS NOT NULL GROUP BY s.id
    ) durations""")).scalar_one()
    return {"generated_on": date.today().isoformat(), "range": {"start": start, "end": end}, **dict(counts),
            **dict(activity), "average_session_minutes": duration,
            "by_intent": [dict(r) for r in intents], "warning": "No query data exists for the selected range." if counts["total_messages"] == 0 else None}


# ---------------------------------------------------------------- timetable publishing
# The chatbot engine owns the timetable (PDF + admin edits). After every change it publishes the
# effective timetable here so the students' Timetable page shows the same classes as the chatbot.
_SECTION_NAMES = {"R": "Regular", "S": "Self Support", "RS": "Regular & Self Support", "Rep": "Repeaters"}
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}


class PublishedTimetable(BaseModel):
    term: str = Field(min_length=1, max_length=60)
    effective: str | None = None
    classes: list[dict] = Field(default_factory=list)
    sessions: list[dict] = Field(default_factory=list)


def _effective_date(label: str | None) -> date | None:
    """'Monday 5th October, 2026' -> 2026-10-05 (None when the label has no recognisable date)."""
    import re
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", label or "")
    month = _MONTHS.get(m.group(2).lower()) if m else None
    try:
        return date(int(m.group(3)), month, int(m.group(1))) if month else None
    except ValueError:
        return None


@router.put("/timetable/published")
def publish_timetable(payload: PublishedTimetable, user: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    courses = {c.get("key"): {**c, "group": g.get("group", ""), "program": g.get("program", "")}
               for g in payload.classes for c in g.get("courses", [])}
    effective = _effective_date(payload.effective) or date.today()
    db.execute(text("UPDATE academic_sessions SET is_current=false WHERE session_name<>:term"), {"term": payload.term})
    session_id = db.execute(text("""INSERT INTO academic_sessions(session_name,effective_from,is_current)
        VALUES (:term,:effective,true) ON CONFLICT(session_name) DO UPDATE SET
        effective_from=excluded.effective_from,is_current=true,updated_at=now() RETURNING id"""),
        {"term": payload.term, "effective": effective}).scalar_one()
    db.execute(text("DELETE FROM course_schedules WHERE academic_session_id=:id"), {"id": session_id})
    published = 0
    for slot in payload.sessions:
        if not slot.get("day") or not slot.get("time") or not (slot.get("code") or slot.get("text")):
            continue
        info = courses.get(slot.get("key"), {})
        try:
            hour = int(str(slot["time"]).split(":", 1)[0])
        except ValueError:
            hour = 8
        schedule_id = db.execute(text("""INSERT INTO course_schedules
            (academic_session_id,course_code,course_abbreviation,course_name,program,
             semester,section_type,day_of_week,time_slot,shift,room,is_active)
            VALUES (:sid,:code,:abbr,:name,:program,:semester,:section,:day,:time,:shift,:room,true) RETURNING id"""), {
            "sid": session_id, "code": slot.get("code") or slot.get("key") or "",
            "abbr": (slot.get("text") or "")[:20], "name": slot.get("title") or info.get("title") or slot.get("text") or "",
            "program": slot.get("program") or info.get("program") or "ALL", "semester": slot.get("group") or info.get("group") or "",
            "section": _SECTION_NAMES.get(slot.get("section") or "", slot.get("section") or "Regular"),
            "day": str(slot["day"]).title(), "time": slot["time"],
            "shift": "Evening" if hour >= 17 else ("Afternoon" if hour >= 13 else "Morning"),
            "room": slot.get("room") or "",
        }).scalar_one()
        teacher = (slot.get("teacher") or info.get("teacher") or "").strip()
        if teacher:
            prefix = teacher.split()[0] if teacher.split()[0].endswith(".") else None
            instructor_id = db.execute(text("""INSERT INTO instructors(name,title) VALUES (:name,:title)
                ON CONFLICT(name,department) DO UPDATE SET title=excluded.title RETURNING id"""),
                {"name": teacher, "title": prefix}).scalar_one()
            db.execute(text("INSERT INTO course_schedule_instructors(course_schedule_id,instructor_id) VALUES (:s,:i)"),
                       {"s": schedule_id, "i": instructor_id})
        published += 1
    _audit(db, user["id"], "publish", "timetable", payload.term, after={"sessions": published})
    db.commit()
    return {"term": payload.term, "published": published}


# ---------------------------------------------------------------- chatbot export
# The chatbot engine has its own index (crawled website + uploaded documents). This export lets it
# also answer from what admins enter here. Only records backed by a *verified* source are included,
# so demo/mock data never reaches students as fact.
_VERIFIED = "s.verification_status = 'verified'"


@router.get("/chatbot-export")
def chatbot_export(_: dict = Depends(admin_user), db: Session = Depends(get_db)) -> dict:
    docs: list[dict] = []
    for r in db.execute(text(f"""SELECT r.id::text, r.rule_code, r.category, r.title, r.description,
            r.effective_from::text AS effective_from, p.code AS program, s.title AS source
        FROM academic_rules r JOIN source_records s ON s.id=r.source_id LEFT JOIN programs p ON p.id=r.program_id
        WHERE r.active AND {_VERIFIED} AND (r.effective_to IS NULL OR r.effective_to >= CURRENT_DATE)
        ORDER BY r.rule_code""")).mappings():
        scope = f" ({r['program']})" if r["program"] else ""
        docs.append({"id": f"policy-{r['id']}", "section": "policy", "title": f"{r['title']}{scope}",
                     "text": f"Rule {r['rule_code']} · {r['category']}{scope}, effective {r['effective_from']}.\n\n"
                             f"{r['description']}\n\nSource: {r['source']}"})

    fees: dict[tuple, list[dict]] = {}
    for r in db.execute(text(f"""SELECT COALESCE(p.code, 'All programmes') AS program, f.official_fee_category AS category,
            f.shift, f.fee_type, f.amount::double precision AS amount, f.currency,
            f.effective_from::text AS effective_from, s.title AS source
        FROM fee_structures f JOIN source_records s ON s.id=f.source_id LEFT JOIN programs p ON p.id=f.program_id
        WHERE {_VERIFIED} AND (f.effective_to IS NULL OR f.effective_to >= CURRENT_DATE)
        ORDER BY 1, 2, 3, 4""")).mappings():
        fees.setdefault((r["program"], r["category"], r["effective_from"], r["source"]), []).append(dict(r))
    for (program, category, effective, source), rows in fees.items():
        lines = "\n".join(f"| {x['shift']} | {x['fee_type'].replace('_', ' ')} | {x['currency']} {x['amount']:,.0f} |" for x in rows)
        docs.append({"id": f"fees-{program}-{category}-{effective}", "section": "fees",
                     "title": f"{program} fees — {category}",
                     "text": f"{program} fee schedule, {category}, effective {effective}.\n\n"
                             f"| Shift | Fee | Amount |\n|---|---|---|\n{lines}\n\nSource: {source}"})

    prereqs: dict[str, list[str]] = {}
    for r in db.execute(text(f"""SELECT c.code, pc.code AS needs, cp.relation_type, cp.minimum_grade, cs.name AS curriculum
        FROM course_prerequisites cp JOIN courses c ON c.id=cp.course_id JOIN courses pc ON pc.id=cp.prerequisite_course_id
        JOIN curriculum_schemes cs ON cs.id=cp.curriculum_id JOIN source_records s ON s.id=cp.source_id
        WHERE {_VERIFIED} ORDER BY c.code, pc.code""")).mappings():
        grade = f" (minimum grade {r['minimum_grade']})" if r["minimum_grade"] else ""
        prereqs.setdefault(r["code"], []).append(f"{r['needs']} as a {r['relation_type']}{grade}, {r['curriculum']}")
    for r in db.execute(text(f"""SELECT c.code, c.title, c.description, c.theory_credit_hours::double precision AS theory,
            c.lab_credit_hours::double precision AS lab, s.title AS source
        FROM courses c LEFT JOIN source_records s ON s.id=c.source_id
        WHERE c.active AND (c.source_id IS NULL OR {_VERIFIED}) ORDER BY c.code""")).mappings():
        needs = prereqs.get(r["code"])
        body = (f"{r['code']} {r['title']}: {r['theory']:g} theory + {r['lab']:g} lab credit hours."
                + (f"\n\n{r['description']}" if r["description"] else "")
                + (f"\n\nPrerequisites of {r['code']}: " + "; ".join(needs) + "." if needs else f"\n\n{r['code']} has no recorded prerequisite.")
                + f"\n\nSource: {r['source'] or 'entered by the department administrator'}")
        docs.append({"id": f"course-{r['code']}", "section": "course", "title": f"{r['code']} {r['title']}", "text": body})

    for r in db.execute(text(f"""SELECT kd.id::text, kd.title, kd.category, s.title AS source,
            string_agg(dc.content, E'\n\n' ORDER BY dc.chunk_index) AS content
        FROM knowledge_documents kd JOIN source_records s ON s.id=kd.source_id
        JOIN document_chunks dc ON dc.document_id=kd.id
        WHERE kd.processing_status='ready' AND {_VERIFIED} GROUP BY kd.id, s.title ORDER BY kd.title""")).mappings():
        docs.append({"id": f"knowledge-{r['id']}", "section": "knowledge", "title": r["title"],
                     "text": f"{r['content']}\n\nSource: {r['source']}"})

    for r in db.execute(text("""SELECT id::text, category, title, content_markdown FROM knowledge_base
        WHERE is_active ORDER BY priority, title""")).mappings():
        docs.append({"id": f"cms-{r['id']}", "section": "cms", "title": r["title"], "text": r["content_markdown"]})
    return {"generated_at": date.today().isoformat(), "documents": docs}
