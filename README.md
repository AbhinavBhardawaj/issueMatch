# IssueMatch

The GitHub integration is assembled with `app.main.create_app(webhook_secret, candidate_service)`.
Production wiring supplies a persistent `CandidateRepository`, an installation-scoped GitHub client factory, and Developer B's implementation of `AnalysisService`.

The cross-team contract is deliberately small:

- input: `app.models.CandidateSubmission` and `app.models.RepositoryAnalysisContext`
- output: `app.models.ApproachAnalysis`

The included `InMemoryCandidateRepository` is only for local development and tests.
