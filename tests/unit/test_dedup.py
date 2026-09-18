import pytest
import time
from datetime import datetime, timezone, timedelta
from app.dedup.detector import (
    InMemoryDedupStore,
    check_existing_github_issues,
    normalized_finding_signature,
    STALE_RESERVATION_SECONDS
)
from app.domain.models import ExistingIssue

# DD1
def test_first_reservation(make_finding):
    store = InMemoryDedupStore()
    f = make_finding()
    res = store.check_and_reserve(f)
    assert not res.is_duplicate
    assert res.reason == "New reservation"

# DD2
def test_same_finding_twice(make_finding):
    store = InMemoryDedupStore()
    f = make_finding()
    res1 = store.check_and_reserve(f)
    assert not res1.is_duplicate
    
    res2 = store.check_and_reserve(f)
    assert not res2.is_duplicate
    assert res2.reason == "Own reservation"

# DD3
def test_different_finding_same_sig(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1")
    f2 = make_finding(finding_id="F-2") # identical otherwise
    
    res1 = store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert not res1.is_duplicate
    assert res2.is_duplicate
    assert res2.reason == "Active reservation exists"
    assert res2.finding_id == "F-1" # Returns the ID of the owner

# DD4
def test_title_change_same_sig(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1", title="Title A")
    f2 = make_finding(finding_id="F-2", title="Title B") # only title changed
    
    store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert res2.is_duplicate

# DD5
def test_different_defect_not_duplicate(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1", description="Defect A")
    f2 = make_finding(finding_id="F-2", description="Defect B")
    
    res1 = store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert not res1.is_duplicate
    assert not res2.is_duplicate

# DD6
def test_stale_reservation_overwritten(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1")
    f2 = make_finding(finding_id="F-2")
    
    res1 = store.check_and_reserve(f1)
    assert not res1.is_duplicate
    
    # Manually backdate the reservation to be stale
    sig = res1.signature
    store._reservations[sig]["timestamp"] = datetime.now(timezone.utc) - timedelta(seconds=STALE_RESERVATION_SECONDS + 10)
    
    res2 = store.check_and_reserve(f2)
    assert not res2.is_duplicate
    assert res2.reason == "Overwrote stale reservation"
    assert res2.finding_id == "F-2"

# DD7
def test_github_marker_found(make_finding):
    f = make_finding(finding_id="F-123")
    issues = [
        ExistingIssue(number=1, title="Bug", state="open", body_summary="Some issue\n<!-- opencontrib:finding:F-123:v:V-456 -->")
    ]
    assert check_existing_github_issues(f, issues) is True

# DD8
def test_github_marker_not_found(make_finding):
    f = make_finding(finding_id="F-123")
    issues = [
        ExistingIssue(number=1, title="Bug", state="open", body_summary="Some issue\n<!-- opencontrib:finding:F-999:v:V-456 -->")
    ]
    assert check_existing_github_issues(f, issues) is False

# DD9
def test_signature_deterministic():
    sig1 = normalized_finding_signature("repo1", "file.py", "func", "desc")
    sig2 = normalized_finding_signature("repo1", "file.py", "func", "desc")
    assert sig1 == sig2
    
    # Check casing and whitespace are normalized
    sig3 = normalized_finding_signature("repo1", "  FILE.py ", " FUNC ", "  DESC  ")
    assert sig1 == sig3

# DD10
def test_description_length_normalization():
    # Only first 120 chars should matter
    desc1 = "A" * 120 + "B"
    desc2 = "A" * 120 + "C"
    
    sig1 = normalized_finding_signature("repo1", "file.py", "func", desc1)
    sig2 = normalized_finding_signature("repo1", "file.py", "func", desc2)
    
    assert sig1 == sig2
