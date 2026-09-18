import hashlib
import json
import re
from datetime import datetime, timezone
import threading
from pydantic import BaseModel, ConfigDict
from app.domain.models import Finding, ExistingIssue

class DedupResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    signature: str
    finding_id: str
    is_duplicate: bool
    reason: str

STALE_RESERVATION_SECONDS: int = 300

def _normalize_description(description: str) -> str:
    desc = description[:120].lower()
    desc = re.sub(r'[^a-z0-9\s]', '', desc)
    desc = re.sub(r'\s+', ' ', desc).strip()
    return desc

def normalized_finding_signature(repository_id: str, file: str, function: str | None, description: str) -> str:
    normalized = _normalize_description(description)
    payload = json.dumps({
        "repository_id": repository_id, 
        "file": file.strip().lower(), 
        "function": (function or "").strip().lower(), 
        "defect": normalized
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

class InMemoryDedupStore:
    def __init__(self):
        self._reservations = {}
        self._lock = threading.Lock()
        
    def check_and_reserve(self, finding: Finding) -> DedupResult:
        signature = normalized_finding_signature(
            finding.repository_id, finding.file, finding.function, finding.description
        )
        now = datetime.now(timezone.utc)
        
        with self._lock:
            if signature in self._reservations:
                reservation = self._reservations[signature]
                if reservation["finding_id"] == finding.finding_id:
                    return DedupResult(
                        signature=signature, 
                        finding_id=finding.finding_id, 
                        is_duplicate=False, 
                        reason="Own reservation"
                    )
                
                age_seconds = (now - reservation["timestamp"]).total_seconds()
                if age_seconds > STALE_RESERVATION_SECONDS:
                    self._reservations[signature] = {"finding_id": finding.finding_id, "timestamp": now}
                    return DedupResult(
                        signature=signature, 
                        finding_id=finding.finding_id, 
                        is_duplicate=False, 
                        reason="Overwrote stale reservation"
                    )
                    
                return DedupResult(
                    signature=signature, 
                    finding_id=reservation["finding_id"], 
                    is_duplicate=True, 
                    reason="Active reservation exists"
                )
                
            self._reservations[signature] = {"finding_id": finding.finding_id, "timestamp": now}
            return DedupResult(
                signature=signature, 
                finding_id=finding.finding_id, 
                is_duplicate=False, 
                reason="New reservation"
            )

def check_existing_github_issues(finding: Finding, existing_issues: list[ExistingIssue]) -> bool:
    marker = f"opencontrib:finding:{finding.finding_id}"
    for issue in existing_issues:
        if marker in issue.body_summary:
            return True
    return False
