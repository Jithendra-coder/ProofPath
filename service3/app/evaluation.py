import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


class AssessmentCase(Protocol):
    competency_code: str
    arguments: dict[str, Any]
    expected: Any


class CaseExecution(Protocol):
    status: str
    value: Any


@dataclass(frozen=True)
class Evaluation:
    status: str
    competency_results: list[dict[str, str | int]]


def evaluate_test_cases(
    cases: Iterable[AssessmentCase],
    *,
    code: str,
    entrypoint: str,
    run_case: Callable[[str, str, dict[str, Any]], CaseExecution],
) -> Evaluation:
    counts: dict[str, dict[str, int]] = {}
    run_status = "passed"
    for case in cases:
        bucket = counts.setdefault(case.competency_code, {"passed_cases": 0, "total_cases": 0})
        bucket["total_cases"] += 1
        try:
            observed = run_case(code, entrypoint, case.arguments)
        except Exception:
            run_status = "infrastructure_failed"
            break
        if observed.status == "infrastructure_failed":
            run_status = "infrastructure_failed"
            break
        if observed.status == "timed_out":
            run_status = "timed_out"
        elif observed.status == "completed":
            try:
                matches = json.dumps(observed.value, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
                    case.expected, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError):
                matches = False
            if matches:
                bucket["passed_cases"] += 1
        if run_status == "passed" and bucket["passed_cases"] < bucket["total_cases"]:
            run_status = "failed"

    results = [
        {
            "competency_code": competency_code,
            "status": "demonstrated" if counts[competency_code]["passed_cases"] == values["total_cases"] else "insufficient",
            **values,
        }
        for competency_code, values in sorted(counts.items())
    ]
    if run_status == "infrastructure_failed":
        results = []
    return Evaluation(run_status, results)
