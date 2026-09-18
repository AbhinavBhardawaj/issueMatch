from app.models.context import RepositoryAnalysisContext


def verify_evidence(evidence: list[str],
    context: RepositoryAnalysisContext) -> list[str]:
    """
    Keep only evidence that can be grounded in the repository context.
    """

    available_paths = {
        file.path
        for file in context.code_context.file_contents
    }

    available_paths.update(
        file.path
        for file in context.code_context.test_files
    )

    verified: list[str] = []

    for item in evidence:
        if not item:
            continue

        for path in available_paths:
            if path in item:
                verified.append(item)
                break

    return verified