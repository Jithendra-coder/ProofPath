from types import SimpleNamespace

from assessment_evaluator.app.evaluation import evaluate_test_cases


def test_evaluation_groups_case_results_by_competency():
    cases = [
        SimpleNamespace(competency_code="python", arguments={"value": "a"}, expected="A"),
        SimpleNamespace(competency_code="python", arguments={"value": "b"}, expected="B"),
        SimpleNamespace(competency_code="sql", arguments={"value": "c"}, expected="C"),
    ]

    result = evaluate_test_cases(
        cases,
        code="solution",
        entrypoint="solve",
        run_case=lambda _code, _entrypoint, args: SimpleNamespace(
            status="completed", value=args["value"].upper()
        ),
    )

    assert result.status == "passed"
    assert result.competency_results == [
        {"competency_code": "python", "status": "demonstrated", "passed_cases": 2, "total_cases": 2},
        {"competency_code": "sql", "status": "demonstrated", "passed_cases": 1, "total_cases": 1},
    ]


def test_infrastructure_failure_does_not_publish_partial_scores():
    result = evaluate_test_cases(
        [SimpleNamespace(competency_code="python", arguments={}, expected=1)],
        code="solution",
        entrypoint="solve",
        run_case=lambda *_: SimpleNamespace(status="infrastructure_failed", value=None),
    )

    assert result.status == "infrastructure_failed"
    assert result.competency_results == []
