import pytest

from service2.app.assessment_runner import PodmanAssessmentRunner


RUNNER = PodmanAssessmentRunner()


@pytest.mark.skipif(not RUNNER.available(), reason="Pinned rootless Podman image is unavailable")
def test_candidate_code_runs_as_unprivileged_with_network_and_root_writes_blocked():
    result = RUNNER.run_case(
        '''def probe():
    import os, socket
    try:
        open("/etc/proofpath-write-check", "w").close()
        root_write = True
    except OSError:
        root_write = False
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.5).close()
        network = True
    except OSError:
        network = False
    with open("/tmp/proofpath-check", "w") as handle:
        handle.write("ok")
    return {"uid": os.getuid(), "root_write": root_write, "network": network}
''',
        "probe",
        {},
    )
    assert result.status == "completed"
    assert result.value == {"uid": 65534, "root_write": False, "network": False}


@pytest.mark.skipif(not RUNNER.available(), reason="Pinned rootless Podman image is unavailable")
def test_candidate_infinite_loop_is_stopped_by_container_timeout():
    result = RUNNER.run_case("def spin():\n    while True: pass", "spin", {})
    assert result.status == "timed_out"
