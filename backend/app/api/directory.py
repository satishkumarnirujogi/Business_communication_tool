"""Doctors and services: add, edit, hide (admins). Reading them is in calendar.py (staff)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit
from ..auth import Principal
from ..data import SourceError, get_store
from ..scheduling import regenerate
from .deps import require_admin

router = APIRouter(prefix="/api/admin", tags=["directory"])


class DoctorIn(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    specialty: str = Field(..., min_length=2, max_length=100)
    languages: str = Field("en", max_length=100)
    bio: str = Field("", max_length=2000)
    photo_url: str = Field("", max_length=500)
    active: bool = True


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    duration_minutes: int = Field(..., ge=5, le=480)
    price: float | None = Field(None, ge=0)
    description: str = Field("", max_length=2000)
    color: str = Field("", pattern="^(#[0-9a-fA-F]{6})?$")
    active: bool = True
    doctor_ids: list[int] = []


def _write(fn):
    try:
        return fn()
    except KeyError:
        raise HTTPException(404, "Not found.") from None
    except SourceError as e:
        raise HTTPException(409, str(e)) from None


def _doctor_ids() -> set[int]:
    return {d["id"] for d in get_store().list_doctors(active_only=False)}


@router.post("/doctors", status_code=201)
def add_doctor(body: DoctorIn, p: Principal = Depends(require_admin)):
    row = _write(lambda: get_store().save_doctor(body.model_dump()))
    audit.record("doctor.add", "allowed", actor=p.label, params=body.model_dump())
    return row


@router.put("/doctors/{doctor_id}")
def edit_doctor(doctor_id: int, body: DoctorIn, p: Principal = Depends(require_admin)):
    row = _write(lambda: get_store().save_doctor({"id": doctor_id, **body.model_dump()}))
    audit.record("doctor.update", "allowed", actor=p.label, params={"id": doctor_id, **body.model_dump()})
    slots = regenerate()  # hiding a doctor removes their free slots; showing them again restores them
    return {**row, "slots": {"added": slots["added"], "removed": slots["removed"]}}


@router.delete("/doctors/{doctor_id}")
def hide_doctor(doctor_id: int, p: Principal = Depends(require_admin)):
    """Doctors are hidden, not deleted, so past appointments keep their doctor."""
    row = _write(lambda: get_store().save_doctor({"id": doctor_id, "active": False}))
    audit.record("doctor.hide", "allowed", actor=p.label, params={"id": doctor_id})
    regenerate()
    return row


def _check_doctors(ids: list[int]) -> None:
    unknown = set(ids) - _doctor_ids()
    if unknown:
        raise HTTPException(400, f"Unknown doctor id(s): {', '.join(map(str, sorted(unknown)))}.")


@router.post("/services", status_code=201)
def add_service(body: ServiceIn, p: Principal = Depends(require_admin)):
    _check_doctors(body.doctor_ids)
    values = body.model_dump(exclude={"doctor_ids"})
    row = _write(lambda: get_store().save_service(values, body.doctor_ids))
    audit.record("service.add", "allowed", actor=p.label, params=body.model_dump())
    return row


@router.put("/services/{service_id}")
def edit_service(service_id: int, body: ServiceIn, p: Principal = Depends(require_admin)):
    _check_doctors(body.doctor_ids)
    values = {"id": service_id, **body.model_dump(exclude={"doctor_ids"})}
    row = _write(lambda: get_store().save_service(values, body.doctor_ids))
    audit.record("service.update", "allowed", actor=p.label, params={"id": service_id, **body.model_dump()})
    return row


@router.delete("/services/{service_id}")
def hide_service(service_id: int, p: Principal = Depends(require_admin)):
    current = next((s for s in get_store().list_services(active_only=False) if s["id"] == service_id), None)
    if not current:
        raise HTTPException(404, "Not found.")
    row = _write(lambda: get_store().save_service({"id": service_id, "active": False}, current["doctor_ids"]))
    audit.record("service.hide", "allowed", actor=p.label, params={"id": service_id})
    return row
