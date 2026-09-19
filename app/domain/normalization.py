def normalize_code(text: str) -> str:
    """Normalize code text by collapsing all whitespace sequences into single spaces."""
    return " ".join(text.split())


def normalize_path(path: str) -> str:
    """Normalize file paths by stripping and normalizing slashes without case-folding."""
    return path.strip().replace("\\", "/")
