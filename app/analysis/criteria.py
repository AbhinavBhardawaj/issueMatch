"""Bounded lexical cross-check for explicit issue acceptance checklists.

This gate is intentionally conservative and only applies when maintainers wrote
at least two checkbox criteria. It supplements, rather than replaces, the model's
codebase analysis. It cannot establish semantic correctness on its own.
"""
import re
from dataclasses import dataclass


_CHECKBOX = re.compile(r"^\s*(?:[-*]\s*)?\[\s?\]\s+(.+)$", re.MULTILINE)
_CLAUSE = re.compile(r"(?:[.;\n]\s+|\bbut\b)", re.IGNORECASE)
_NEGATED_ACTION = re.compile(
    r"\b(?:will|would|do|does|should|can)\s+not\b|\b(?:won't|don't|never)\b|"
    r"\bleav(?:e|ing)\b.{0,45}\bunchanged\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(?:add|build|cast|change|check|convert|correct|cover|create|define|ensure|establish|extract|"
    r"fetch|fix|handle|implement|include|introduce|isolate|keep|load|migrate|modify|move|organize|"
    r"preserve|provide|refactor|register|remove|replace|return|satisfy|separate|set|split|structure|"
    r"support|update|use|validate|write)\b|"
    r"\b(?:i'll|i will|my plan is to|plan to|aim to|going to)\b",
    re.IGNORECASE,
)
_STOP = {
    "a", "an", "and", "are", "as", "be", "by", "can", "code", "correct", "correctly",
    "for", "from", "in", "is", "it", "not", "of", "on", "or", "proper", "properly", "return",
    "status", "success", "successfully", "the", "to", "via", "when", "with", "using", "use",
    "uses", "used", "also", "all", "clean", "cleanly", "well", "exist", "exists", "existing",
}
_ALIASES = {
    "environ": "environment", "env": "environment", "environment": "environment",
    "getenv": "environment", "login": "login", "logins": "login", "logged": "login",
    "secret": "secret", "secrets": "secret", "secret_key": "secret",
    "user": "user", "users": "user", "user_id": "user_id",
    "id": "id", "ids": "id", "integer": "integer", "int": "integer",
    "token": "token", "tokens": "token",
    "folder": "director", "directory": "director", "directories": "director",
}


def _stem(word: str) -> str:
    w = word.lower()
    for sfx, rep in (
        ("separating", "separat"), ("separation", "separat"), ("separated", "separat"),
        ("separates", "separat"), ("separate", "separat"),
        ("blueprints", "blueprint"), ("routes", "route"),
        ("cleanly", "clean"),
        ("directories", "director"), ("directory", "director"), ("folders", "director"), ("folder", "director"),
        ("established", "establish"), ("establishing", "establish"), ("establishment", "establish"),
        ("coverage", "cover"), ("covering", "cover"), ("covered", "cover"),
        ("endpoints", "endpoint"),
        ("users", "user"), ("logins", "login"), ("tokens", "token"),
        ("secrets", "secret"), ("credentials", "credenti"), ("credential", "credenti"),
        ("integers", "integer"),
        ("converts", "convert"), ("converting", "convert"), ("converted", "convert"),
        ("conversion", "convert"), ("converter", "convert"),
        ("tests", "test"), ("testing", "test"), ("tested", "test"),
        ("ing", ""), ("ed", ""), ("es", ""), ("s", ""),
    ):
        if w.endswith(sfx) and len(w) - len(sfx) + len(rep) >= 3:
            w = w[:-len(sfx)] + rep
            break
    return w


def _terms(value: str) -> set[str]:
    words = re.findall(r"[a-z]+(?:_[a-z]+)*|\b\d{3}\b", value.lower())
    result: set[str] = set()
    for word in words:
        if word in _STOP:
            continue
        canon = _ALIASES.get(word, word)
        st = _stem(canon)
        if st not in _STOP and len(st) >= 2:
            result.add(st)
        for part in word.split("_"):
            if len(part) >= 3 and part not in _STOP:
                part_st = _stem(_ALIASES.get(part, part))
                if part_st not in _STOP and len(part_st) >= 2:
                    result.add(part_st)
    return result


@dataclass(frozen=True)
class CriterionCoverage:
    criteria: tuple[str, ...]
    covered: tuple[str, ...]
    missing: tuple[str, ...]
    detail_gaps: tuple[str, ...] = ()


def _test_outcome_gaps(issue_body: str, approach: str) -> tuple[str, ...]:
    """Check explicitly requested success/failure cases, not generic 'tests'.

    Fragments are split at conjunctions so 'login success/failure and user
    lookup cases' does not falsely claim both user lookup outcomes.
    """
    issue = issue_body.lower()
    user_cases_required = bool(re.search(
        r"\b(?:user\s+(?:lookup|retrieval)|retriev\w*\s+user)\b.{0,50}"
        r"\bboth\s+success\s+and\s+failure\b", issue,
    ))
    if not user_cases_required:
        return ()
    fragments = re.split(r"[.;\n]|\band\b", approach.lower())
    user_fragments = [fragment for fragment in fragments if re.search(
        r"\buser\b|\bretriev\w*\b|/api/users", fragment,
    )]
    success = any(re.search(r"\bsuccess\w*\b|\bexist\w*\b", fragment)
                  for fragment in user_fragments)
    failure = any(re.search(r"\bfail\w*\b|\bnonexist\w*\b|\bnot found\b|\bmissing\b",
                            fragment) for fragment in user_fragments)
    if success and failure:
        return ()
    return ("Explain tests for both successful and unsuccessful user retrieval.",)


def check_acceptance_criteria(issue_body: str, approach: str) -> CriterionCoverage | None:
    """Compare an approach with explicit issue checkboxes using concrete terms.

    Two distinct shared terms avoid accepting a plan that merely mentions one
    issue keyword. Ambiguous prose is left to the model when no checklist exists.
    """
    criteria = tuple(match.group(1).strip()[:240] for match in _CHECKBOX.finditer(issue_body or ""))[:12]
    if len(criteria) < 2:
        return None
    # A list of issue keywords (or a sentence saying what will *not* be done)
    # is not a proposal covering those requirements. Match a positive action
    # clause per criterion instead of matching words anywhere in the comment.
    clauses = tuple(
        clause for clause in _CLAUSE.split(approach)
        if _ACTION.search(clause) and not _NEGATED_ACTION.search(clause)
    )
    covered: list[str] = []
    missing: list[str] = []
    for criterion in criteria:
        specific = _terms(criterion)
        threshold = min(2, len(specific))
        if threshold and any(len(specific & _terms(clause)) >= threshold for clause in clauses):
            covered.append(criterion)
        else:
            missing.append(criterion)
    return CriterionCoverage(criteria, tuple(covered), tuple(missing),
                             _test_outcome_gaps(issue_body, approach))
