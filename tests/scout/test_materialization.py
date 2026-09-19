import pytest
from app.github.events import GitHubPushEvent
from app.domain.states import FindingStatus
from app.scout.schemas import ScoutFindingDraft
from app.scout.worthiness import materialize_finding
from tests.scout.test_schemas import make_valid_draft_dict


def test_authoritative_materialization():
    event = GitHubPushEvent(
        delivery_id="d-123",
        installation_id=987,
        repository_id=555,
        repository_owner="secure-org",
        repository_name="secure-repo",
        default_branch="main",
        before_sha="0" * 40,
        after_sha="f" * 40,
        ref="refs/heads/main",
    )
    draft = ScoutFindingDraft(**make_valid_draft_dict())
    finding = materialize_finding(draft, event)

    # Assert backend controls identity fields
    assert finding.finding_id.startswith("F-")
    assert finding.installation_id == "987"
    assert finding.repository_id == "555"
    assert finding.commit_sha == "f" * 40
    assert finding.status == FindingStatus.DISCOVERED

    # Assert payload content passed accurately
    assert finding.title == draft.title
    assert finding.severity == draft.severity
    assert finding.file == draft.file
    assert len(finding.evidence) == 1
    assert finding.evidence[0].snippet == draft.evidence[0].snippet
