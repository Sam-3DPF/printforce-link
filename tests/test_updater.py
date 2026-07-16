"""Self-updater version logic + throttled check (U9). The download/swap is validated by
the release smoke test, not here (it needs a real release + a frozen build)."""
from bridge.updater import SelfUpdater, is_newer, latest_release_tag


def test_is_newer_compares_semver():
    assert is_newer("v0.2.0", "0.1.0")
    assert is_newer("0.1.1", "0.1.0")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("v0.1.0", "0.2.0")


def test_is_newer_tolerates_v_prefix_and_junk():
    assert is_newer("v0.2.0", "v0.1.0")
    assert not is_newer("garbage", "0.1.0")   # malformed sorts low, never crashes


def test_latest_release_tag_returns_tag():
    assert latest_release_tag(fetch=lambda url: {"tag_name": "v0.3.0"}) == "v0.3.0"


def test_latest_release_tag_none_on_failure():
    def boom(url):
        raise OSError("offline")
    assert latest_release_tag(fetch=boom) is None
    assert latest_release_tag(fetch=lambda url: {}) is None   # no releases yet


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_updater_applies_when_newer():
    applied = []
    u = SelfUpdater("0.1.0", latest_tag_fn=lambda: "v0.2.0",
                    apply_fn=applied.append, monotonic=Clock())
    u.tick()
    assert applied == ["v0.2.0"]


def test_updater_noop_when_current_is_latest():
    applied = []
    u = SelfUpdater("0.2.0", latest_tag_fn=lambda: "v0.2.0",
                    apply_fn=applied.append, monotonic=Clock())
    u.tick()
    assert applied == []


def test_updater_is_throttled():
    calls = {"n": 0}

    def latest():
        calls["n"] += 1
        return "0.1.0"

    clock = Clock(1000.0)
    u = SelfUpdater("0.1.0", interval_seconds=3600, latest_tag_fn=latest,
                    apply_fn=lambda tag: None, monotonic=clock)
    u.tick()
    u.tick()                       # within interval -> no second check
    assert calls["n"] == 1
    clock.t = 1000.0 + 3601
    u.tick()
    assert calls["n"] == 2


def test_updater_swallows_check_failure():
    def boom():
        raise OSError("down")
    SelfUpdater("0.1.0", latest_tag_fn=boom, apply_fn=lambda tag: None, monotonic=Clock()).tick()
