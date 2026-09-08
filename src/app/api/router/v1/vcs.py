from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_db_handler
from vcs.db.sqlite import DBHandler
from vcs.services import audit

router = APIRouter(prefix="/v1")


@router.get("/sources")
def list_sources(db_handler: DBHandler = Depends(get_db_handler)) -> list[dict]:
    return audit.get_sources(db_handler)


@router.get("/history")
def history(path: str) -> dict:
    try:
        versions = audit.get_version_list(path)
    except audit.OutOfScopeError:
        raise HTTPException(status_code=403, detail="path out of scope")
    return {"path": path, "versions": versions}


@router.get("/diff")
def diff(path: str, v1: str, v2: str) -> dict:
    try:
        text = audit.check_diff(path, v1, v2)
    except audit.OutOfScopeError:
        raise HTTPException(status_code=403, detail="path out of scope")
    return {"path": path, "v1": v1, "v2": v2, "diff": text}
