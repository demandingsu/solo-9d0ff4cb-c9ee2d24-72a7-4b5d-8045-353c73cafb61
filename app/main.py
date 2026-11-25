"""两馆合库标本同件索引交接 API。

交接规则：馆员用已有的同件配对申请交接；系统在证据表中查找两条
能独立复核的证据链（除首尾登记号外，登记号不共用、证据不共用）。
两条链齐备才供数并附两条链的登记号与证据号；证据不足标为待补。
撤回证据时只重核用到它的交接对，撤回与重核在同一事务内一起生效；
查询接口直接读取已保存的结论。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .db import Database, DuplicateEvidence, DuplicateHandover

DB_PATH = os.environ.get("HANDOVER_DB", "/data/handover.db")

app = FastAPI(
    title="标本同件索引交接 API",
    description=(
        "两馆合库后移交标本同件索引的交接服务。证据存入 SQLite，"
        "每条证据含证据号和判为同件的两个登记号；交接对必须找到两条"
        "登记号（除首尾外）与证据均不共用的证据链才供数。"
    ),
    version="1.0.0",
)


def get_db() -> Database:
    return Database(DB_PATH)


# ----------------------------------------------------------------------
# 请求 / 响应模型
# ----------------------------------------------------------------------
class EvidenceIn(BaseModel):
    evidence_id: str = Field(..., min_length=1, examples=["E001"],
                             description="证据号，全局唯一")
    reg_a: str = Field(..., min_length=1, examples=["A001"],
                       description="判为同件的登记号之一")
    reg_b: str = Field(..., min_length=1, examples=["A002"],
                       description="判为同件的另一个登记号")


class EvidenceOut(BaseModel):
    evidence_id: str
    reg_a: str
    reg_b: str
    withdrawn: bool
    created_at: str
    withdrawn_at: str | None = None


class HandoverIn(BaseModel):
    reg_a: str = Field(..., min_length=1, examples=["A001"],
                       description="同件配对的登记号之一")
    reg_b: str = Field(..., min_length=1, examples=["A009"],
                       description="同件配对的另一个登记号")


class Chain(BaseModel):
    registrations: list[str] | None = Field(
        None, description="链上登记号，从申请对的一端到另一端")
    evidence: list[str] | None = Field(
        None, description="与登记号相邻关系一一对应的证据号")


class HandoverOut(BaseModel):
    id: int
    reg_a: str
    reg_b: str
    status: str = Field(
        description="supplied=供数中，pending=待补，paused=暂停供数")
    chain1: Chain
    chain2: Chain
    reason: str | None = None
    created_at: str
    updated_at: str


class WithdrawResult(BaseModel):
    withdrawn_evidence: str = Field(description="被撤回的证据号")
    affected_handovers: list[int] = Field(
        description="用到该证据、已在同一事务内完成重核的交接对 id")
    handovers: list[HandoverOut] = Field(
        description="受影响交接对重核后已保存的结论")


# ----------------------------------------------------------------------
# 证据接口
# ----------------------------------------------------------------------
@app.post(
    "/evidence",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
    tags=["证据"],
    summary="录入一条同件证据",
)
def add_evidence(payload: EvidenceIn) -> dict:
    db = get_db()
    try:
        return db.insert_evidence(payload.evidence_id, payload.reg_a,
                                  payload.reg_b)
    except DuplicateEvidence as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@app.get("/evidence", response_model=list[EvidenceOut], tags=["证据"],
         summary="列出有效证据（含已撤回请加 include_withdrawn=true）")
def list_evidence(include_withdrawn: bool = False) -> list[dict]:
    return get_db().list_evidence(include_withdrawn=include_withdrawn)


@app.get("/evidence/{evidence_id}", response_model=EvidenceOut,
         tags=["证据"], summary="查询单条证据")
def get_evidence(evidence_id: str) -> dict:
    row = get_db().get_evidence(evidence_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"证据不存在：{evidence_id}")
    return row


@app.post(
    "/evidence/{evidence_id}/withdraw",
    response_model=WithdrawResult,
    tags=["证据"],
    summary="撤回证据，并在同一事务内只重核用到它的交接对",
)
def withdraw_evidence(evidence_id: str) -> WithdrawResult:
    db = get_db()
    # 先记下受影响的交接对（撤回前它们的依据里含有该证据）
    affected = _handovers_using_evidence(db, evidence_id)
    row = db.withdraw_evidence(evidence_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"证据不存在或已撤回：{evidence_id}",
        )
    # 事务已提交，读取重核后保存的结论
    conclusions = [db.get_handover(hid) for hid in affected]
    return WithdrawResult(
        withdrawn_evidence=evidence_id,
        affected_handovers=affected,
        handovers=conclusions,  # type: ignore[arg-type]
    )


def _handovers_using_evidence(db: Database, evidence_id: str) -> list[int]:
    return db.handovers_using_evidence(evidence_id)


# ----------------------------------------------------------------------
# 交接接口
# ----------------------------------------------------------------------
@app.post(
    "/handovers",
    response_model=HandoverOut,
    status_code=status.HTTP_201_CREATED,
    tags=["交接"],
    summary="凭同件配对申请交接（当场判定供数或待补，结论落库）",
)
def create_handover(payload: HandoverIn) -> dict:
    if payload.reg_a == payload.reg_b:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="交接申请的两个登记号不能相同",
        )
    db = get_db()
    try:
        return db.create_handover(payload.reg_a, payload.reg_b)
    except DuplicateHandover as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{exc}，可直接用 GET /handovers/{{id}} 读取已保存结论",
        ) from exc


@app.get("/handovers", response_model=list[HandoverOut], tags=["交接"],
         summary="列出全部交接申请及其已保存结论（不临时重算）")
def list_handovers() -> list[dict]:
    return get_db().list_handovers()


@app.get("/handovers/{handover_id}", response_model=HandoverOut,
         tags=["交接"], summary="读取单个交接对已保存的结论")
def get_handover(handover_id: int) -> dict:
    row = get_db().get_handover(handover_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"交接申请不存在：id={handover_id}")
    return row


@app.post("/handovers/{handover_id}/reverify", response_model=HandoverOut,
          tags=["交接"],
          summary="手工重核（补入新证据后可让待补/暂停申请重新判定）")
def reverify_handover(handover_id: int) -> dict:
    row = get_db().reverify_handover(handover_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"交接申请不存在：id={handover_id}")
    return row


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
