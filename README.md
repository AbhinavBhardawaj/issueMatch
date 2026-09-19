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

The production factory now connects:

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

Start it with:

```bash
uvicorn app.main:create_production_app --factory --host 0.0.0.0 --port 8000
```

`create_production_app()` reads the GitHub App configuration, makes an
installation-scoped GitHub client per webhook delivery, creates Developer B's
analysis service through `create_analysis_service()`, and injects all of those
dependencies into `CandidateService`.

By default it requires a DynamoDB table via `DYNAMODB_TABLE`. The table needs
`pk` (string) as its partition key, `sk` (string) as its sort key, and an
`issue-key-index` global secondary index with `issue_key` as its partition key.

One example table setup is:

```bash
aws dynamodb create-table \
  --table-name issue-match-candidates \
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S AttributeName=issue_key,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
  --global-secondary-indexes '[{"IndexName":"issue-key-index","KeySchema":[{"AttributeName":"issue_key","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"},"ProvisionedThroughput":{"ReadCapacityUnits":5,"WriteCapacityUnits":5}}]' \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5
```

For a one-process local demo only, set `ISSUEMATCH_STORAGE=memory`; this mode
loses candidate state on restart and must not be used for production.

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

## End-to-end GitHub App demo

1. Create a GitHub App, install it only on a throwaway demo repository, subscribe
   it to the `Issue comments` and `Issues` events, and give it **Issues: Read & write** and
   **Contents: Read-only** repository permissions. Save the App ID, generated
   private key, and webhook secret outside this repository.
2. Install dependencies in a Python 3.11+ virtual environment, start Ollama and
   pull a model, then expose port 8000 through an HTTPS tunnel such as ngrok or
   Cloudflare Tunnel. Configure the GitHub App webhook URL as
   `https://YOUR-TUNNEL/webhooks/github`.
3. For a temporary local demonstration, export `ISSUEMATCH_STORAGE=memory` plus
   `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `OLLAMA_HOST`,
   and `OLLAMA_MODEL`; then start the production factory command above.
4. Create an issue in the installed repository and comment: `I'd like to work on
   this. I will modify src/example.py and update tests/test_example.py.` The bot
   fetches only the relevant GitHub context, calls Ollama, and posts its structured
   recommendation as a new issue comment.
5. Inspect the GitHub App's **Advanced → Recent deliveries** screen to diagnose
   non-2xx webhook responses or redeliver a test event.

Never commit the private key, webhook secret, AWS credentials, or tunnel URL.

## Current Verification

Developer B's analysis implementation was reported as verified with:

```text
40 passed, 1 skipped
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

## Candidate evaluation policy

The extractor separates `CLAIM_ONLY` from `APPROACH_SUBMITTED`. Messages such as
`Assign this issue to me` or `I would like to work on this issue` are recorded as
ignored deliveries and never invoke Strands/Ollama or receive a bot comment. A
comment with implementation substance—natural language, a function/class plan,
file references, logic, or pseudocode—is eligible for analysis.

Valid approaches receive an increasing issue-local priority. A `PASS` analysis is
stored as `ACCEPTED` and becomes the issue's `CANDIDATE_RECOMMENDED` candidate;
newer approaches are stored as waiting and are not analyzed while that candidate
is selected or assigned. `REVISION_REQUIRED` and `REJECT`/`DECLINED` do not block
later approaches. A revised comment from the same contributor is a new candidate
linked through `parent_candidate_id`.

The `issues.assigned` and `issues.unassigned` webhooks update assignment state.
Unassignment clears the recommendation and resumes the oldest waiting approach;
if no eligible approach remains, the issue returns to `WAITING_FOR_CANDIDATES`.
The bot only recommends contributors and never invokes GitHub's assignment API.
