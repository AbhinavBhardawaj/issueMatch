# IssueMatch

The GitHub integration is assembled with `app.main.create_app(webhook_secret, candidate_service)`.

Production wiring supplies:

* a persistent `CandidateRepository`
* an installation-scoped GitHub client factory
* Developer B's implementation of `AnalysisService`

The cross-team contract is deliberately small:

* input: `app.models.CandidateSubmission` and `app.models.RepositoryAnalysisContext`
* output: `app.models.ApproachAnalysis`

The included `InMemoryCandidateRepository` is only for local development and tests.

## Analysis Architecture

Developer B's analysis implementation follows:

```text
CandidateSubmission
        +
RepositoryAnalysisContext
        ↓
DefaultAnalysisService
        ↓
StrandsAnalysisProvider
        ↓
Strands Agents
        ↓
Ollama
        ↓
Local Llama model
        ↓
Raw AI JSON
        ↓
Strict JSON parsing
        ↓
Repository evidence verification
        ↓
Pydantic validation
        ↓
PASS / REVISION_REQUIRED / REJECT
```

The analysis implementation is available through the existing `AnalysisService` contract and can be created using:

```python
from app.analysis.factory import create_analysis_service

analysis_service = create_analysis_service()
```

It can then be injected into `CandidateService`:

```python
candidate_service = CandidateService(
    repository,
    github_client_factory,
    analysis_service,
)
```

## Analysis Responsibilities

The analyzer evaluates a contributor's proposed approach against the real repository context supplied by the existing context collection layer.

It considers:

* Understanding of the original GitHub issue
* Relevant repository files and paths
* Compatibility with the provided code
* Technical completeness
* Test strategy
* Repository-grounded evidence
* Missing or unclear implementation details

The analyzer is instructed not to invent:

* files
* functions
* classes
* APIs
* repository behavior
* implementation details

The analyzer only reasons over the bounded `RepositoryAnalysisContext`.

It does not execute repository code, shell commands, or installation commands.

## Analysis Decisions

The analyzer returns exactly one decision.

### `PASS`

The proposed approach is supported by the available repository evidence.

A `PASS` result must contain repository-grounded evidence.

### `REVISION_REQUIRED`

The approach may be valid, but important repository-specific information, implementation details, or reasoning are missing or unclear.

A `REVISION_REQUIRED` result must contain revision feedback.

### `REJECT`

The approach conflicts with the available repository evidence or clearly does not fit the actual repository.

A `REJECT` result must contain concrete issues or a recommendation.

AI output is never trusted directly. It is parsed, checked against repository evidence, and validated using the existing `ApproachAnalysis` Pydantic model.

A `PASS` without valid repository-grounded evidence is converted to `REVISION_REQUIRED`.

Unexpected fields or invalid decision-specific output are rejected.

## AI Configuration

The Strands/Ollama provider is configured through environment variables.

### `OLLAMA_HOST`

Ollama server URL.

Default:

```text
http://localhost:11434
```

Example:

```bash
export OLLAMA_HOST=http://localhost:11434
```

### `OLLAMA_MODEL`

Ollama model name.

Default:

```text
llama3.1
```

Example:

```bash
export OLLAMA_MODEL=llama3.2:1b
```

## Local Ollama Setup

Start Ollama:

```bash
ollama serve
```

Pull the model:

```bash
ollama pull llama3.2:1b
```

Verify it:

```bash
ollama run llama3.2:1b
```

Example:

```text
>>> Say exactly: IssueMatch model is working.
IssueMatch model is working.
```

## Python Dependencies

The project uses:

* FastAPI
* httpx
* PyJWT
* Pydantic
* pytest
* pytest-asyncio
* boto3
* Strands Agents with Ollama support

Install the project with:

```bash
python -m pip install -e .
```

The AI integration uses:

```text
strands-agents[ollama]
```

No LangChain, vector database, or additional AI framework is required for the analysis provider.

## GitHub App Configuration

The existing GitHub App authentication layer expects:

```text
GITHUB_APP_ID
GITHUB_PRIVATE_KEY
GITHUB_WEBHOOK_SECRET
```

The GitHub App authenticator creates installation-scoped access tokens, which are used by the GitHub REST client.

The webhook verifies the GitHub `X-Hub-Signature-256` signature before processing events.

## Developer A Integration

Developer A owns the persistent candidate storage and production application wiring.

The current `InMemoryCandidateRepository` is intentionally limited to local development and tests.

Developer A's production integration should connect:

```text
Persistent CandidateRepository
        +
Installation-scoped GitHub client factory
        +
create_analysis_service()
        ↓
CandidateService
        ↓
GitHub webhook
```

Developer A does not need to interact with the internal Strands/Ollama implementation.

The shared boundary remains:

```text
CandidateSubmission
RepositoryAnalysisContext
        ↓
AnalysisService
        ↓
ApproachAnalysis
```

This keeps the analysis implementation independent of the storage implementation.

## Testing

Run the normal test suite with:

```bash
pytest -q
```

The normal suite does not require Ollama to be running.

### Real Ollama Integration Test

The real Ollama integration test is intentionally skipped unless explicitly enabled.

Run:

```bash
RUN_OLLAMA_TESTS=1 OLLAMA_MODEL=llama3.2:1b \
pytest -q tests/analysis/test_ollama_integration.py -s
```

This verifies the real path:

```text
DefaultAnalysisService
        ↓
StrandsAnalysisProvider
        ↓
Strands Agent
        ↓
Ollama
        ↓
Llama
        ↓
AnalysisService validation
```

The test should only be considered a real AI integration test when Ollama is running and the requested model is available.

## Failure Handling

If repository context collection fails, the candidate is marked as failed.

If the analysis service fails, the candidate is marked as failed.

Empty AI responses are rejected.

Invalid AI output cannot become a successful `PASS` result.

This prevents AI/provider failures from being treated as valid analyses.

## Compile Check

Run:

```bash
python3 -m compileall -q app tests
```

No output indicates successful compilation.

## Current Verification

Developer B's analysis implementation has been verified with:

```text
39 passed, 1 skipped
```

The real Ollama integration test has also been verified separately with:

```text
1 passed
```

The real integration used:

```text
OLLAMA_MODEL=llama3.2:1b
```

The Python source tree also passes:

```bash
python3 -m compileall -q app tests
```

## Responsibilities

### Developer A

* GitHub App integration
* Persistent candidate storage
* DynamoDB integration
* Production dependency composition
* Installation-scoped GitHub client wiring
* Deployment/runtime configuration

### Developer B

* `AnalysisService`
* Analysis prompt
* AI provider abstraction
* Strands Agents integration
* Ollama integration
* Repository-grounded evidence verification
* Strict analysis validation
* `PASS` / `REVISION_REQUIRED` / `REJECT` decision handling
* Analysis tests
* Real Ollama integration test

The two components communicate through the existing shared models and `AnalysisService` contract.

## Issue Scout & Verifier Testing Tiers

The system clearly distinguishes three tiers of end-to-end verification:

### 1. AUTOMATED E2E
- **Scope:** Synthetic signed webhook delivery + real FastAPI wiring + real SQLite durable queue + real worker + controlled deterministic LLM & GitHub dependencies.
- **Location:** `tests/integration/test_pipeline_e2e_scenarios.py`
- **Execution:** Runs in standard CI/local runs (`pytest -q tests/integration/test_pipeline_e2e_scenarios.py`).

### 2. LIVE PROVIDER E2E
- **Scope:** Real GitHub APIs (authentication, trees, contents, issue writes) + real Scout & Verifier LLM providers.
- **Location:** `tests/integration/test_live_scout_verifier_e2e.py`
- **Safety:** Strictly guarded by `RUN_LIVE_SCOUT_E2E=1`, sandbox repository identification check, and explicit permission flags. Skips honestly when credentials are not configured.

### 3. FINAL LIVE ACCEPTANCE
- **Scope:** Actual `git push` by developer -> actual GitHub App webhook delivery -> SQLite durable queue -> real Scout Agent -> real Verifier Pipeline -> deterministic gate -> real GitHub issue created or rejected.
- **Runner:** `scripts/run_live_webhook_receiver.py` (receives live webhooks over an exposed tunnel).

