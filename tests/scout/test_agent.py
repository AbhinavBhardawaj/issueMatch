import pytest
import json
from app.scout.agent import ScoutAgent, MalformedScoutResponse
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext
from tests.scout.test_schemas import make_valid_draft_dict


class DummyLLMProvider:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.captured_prompt = ""

    async def complete(self, system: str, user: str) -> str:
        self.captured_prompt = user
        return self.response_text


def make_dummy_event():
    return GitHubPushEvent(
        delivery_id="d1",
        installation_id=1,
        repository_id=2,
        repository_owner="owner",
        repository_name="repo",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
    )


def make_dummy_context():
    return ScoutContext(
        installation_id=1,
        repository_id=2,
        owner="owner",
        name="repo",
        before_sha="a" * 40,
        commit_sha="b" * 40,
    )


@pytest.mark.asyncio
async def test_scout_agent_success():
    draft = make_valid_draft_dict()
    payload = json.dumps({"findings": [draft]})
    llm = DummyLLMProvider(payload)
    agent = ScoutAgent(llm)

    resp = await agent.discover(make_dummy_event(), make_dummy_context())
    assert len(resp.findings) == 1
    assert resp.findings[0].title == draft["title"]


@pytest.mark.asyncio
async def test_scout_agent_rejects_more_than_8_findings():
    drafts = [make_valid_draft_dict() for _ in range(9)]
    payload = json.dumps({"findings": drafts})
    llm = DummyLLMProvider(payload)
    agent = ScoutAgent(llm)

    with pytest.raises(MalformedScoutResponse, match="exceeding the maximum limit of 8"):
        await agent.discover(make_dummy_event(), make_dummy_context())


@pytest.mark.asyncio
async def test_scout_agent_rejects_malformed_json():
    llm = DummyLLMProvider("not valid json")
    agent = ScoutAgent(llm)

    with pytest.raises(MalformedScoutResponse):
        await agent.discover(make_dummy_event(), make_dummy_context())
