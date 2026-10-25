import pytest

from kovan.domain.exit_reasons import classify_exit
from kovan.domain.models import RestartPolicy, RunState
from kovan.domain.restart import RestartTracker, effective_policy, should_restart


def c(rc, stopping=False, tb=False, expect=False, win=False, shutdown=False):
    return classify_exit(rc, stopping=stopping, had_traceback=tb, expect_exit=expect, is_windows=win,
                         shutdown=shutdown)


@pytest.mark.parametrize("args, reason, state", [
    (dict(rc=-15, stopping=True), "user_stop", RunState.STOPPED),
    (dict(rc=0, stopping=True, shutdown=True), "panel_shutdown", RunState.STOPPED),
    (dict(rc=-15), "external_stop", RunState.STOPPED_EXTERNALLY),
    (dict(rc=-2), "external_stop", RunState.STOPPED_EXTERNALLY),
    (dict(rc=143), "external_stop", RunState.STOPPED_EXTERNALLY),
    (dict(rc=-9), "killed", RunState.CRASHED),
    (dict(rc=-11), "crashed", RunState.CRASHED),
    (dict(rc=0), "external_stop", RunState.STOPPED_EXTERNALLY),
    (dict(rc=0, expect=True), "exited_ok", RunState.STOPPED),
    (dict(rc=1, tb=True), "crashed", RunState.CRASHED),
    (dict(rc=3), "crashed", RunState.CRASHED),
    (dict(rc=None), "killed", RunState.CRASHED),
    # Windows
    (dict(rc=1, win=True), "killed", RunState.CRASHED),             # TerminateProcess / Görev Yöneticisi
    (dict(rc=1, tb=True, win=True), "crashed", RunState.CRASHED),   # Python hatası
    (dict(rc=0xC000013A, win=True), "external_stop", RunState.STOPPED_EXTERNALLY),
    (dict(rc=-1073741510, win=True), "external_stop", RunState.STOPPED_EXTERNALLY),  # işaretli gösterim
    (dict(rc=0xC0000005, win=True), "crashed", RunState.CRASHED),
])
def test_classify_exit(args, reason, state):
    info = c(**args)
    assert (info.reason, info.state) == (reason, state)


def test_signal_names():
    assert c(-9).signal == "SIGKILL" and c(-15).signal == "SIGTERM"


def test_effective_policy_precedence():
    assert effective_policy(RestartPolicy.ALWAYS, RestartPolicy.NEVER, "never") == RestartPolicy.ALWAYS
    assert effective_policy(None, RestartPolicy.ON_FAILURE, "never") == RestartPolicy.ON_FAILURE
    assert effective_policy(None, None, "always") == RestartPolicy.ALWAYS
    assert effective_policy(None, None, None) == RestartPolicy.NEVER
    assert effective_policy(None, None, "bozuk") == RestartPolicy.NEVER


@pytest.mark.parametrize("policy, reason, expected", [
    (RestartPolicy.ON_FAILURE, "crashed", True), (RestartPolicy.ON_FAILURE, "killed", True),
    (RestartPolicy.ON_FAILURE, "external_stop", False), (RestartPolicy.ALWAYS, "external_stop", True),
    (RestartPolicy.ALWAYS, "user_stop", False), (RestartPolicy.ALWAYS, "panel_shutdown", False),
    (RestartPolicy.NEVER, "crashed", False),
])
def test_should_restart(policy, reason, expected):
    assert should_restart(policy, reason) is expected


def test_restart_backoff_and_crash_loop():
    t = RestartTracker()
    delays = [t.next_delay(100 + i, 4, 300) for i in range(4)]
    assert delays == [1, 2, 4, 8]
    assert t.next_delay(105, 4, 300) is None            # pencere içinde 5. deneme → çökme döngüsü
    assert t.next_delay(100 + 400, 4, 300) == 1         # pencere geçince sayaç sıfırlanır


def test_backoff_capped():
    t = RestartTracker()
    assert max(t.next_delay(i, 100, 10_000) for i in range(10)) == 60
