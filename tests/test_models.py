"""Model profiles keyed by the SSDP DevModel code, and where the code is kept."""
import logging

from bridge.bambu import models
from bridge.bambu.models import P1_PROFILE, profile_for
from bridge.config import PrinterConfig
from bridge.discover import DiscoveredPrinter
from bridge.discovery_reporter import DiscoveryReporter
from bridge.printer import BambuPrinter
from bridge.reconciler import ConfigReconciler
from bridge.store import PrinterStore


def test_c12_is_the_p1s_profile():
    profile = profile_for("C12", serial="01P00C000000001")
    assert profile.name == "P1S"
    assert profile.family == "p1"


def test_c11_is_the_p1p_profile():
    assert profile_for("C11", serial="01S00C000000001").name == "P1P"


def test_code_lookup_ignores_case_and_whitespace():
    assert profile_for(" c12 ", serial="S").name == "P1S"


def test_profile_names_what_the_start_and_upload_paths_need():
    profile = profile_for("C12", serial="S")
    assert profile.start_url_scheme == "file:///sdcard/"
    assert profile.ftps_session_reuse is True
    assert profile.ftps_tls_max is None
    assert profile.idle_stg_cur == 255
    assert profile.vibration_cali is True


def test_an_unknown_code_uses_the_p1_profile_and_warns_once_per_printer(caplog):
    models._warned_serials.clear()
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.models"):
        first = profile_for("Z99", serial="S1")
        again = profile_for("Z99", serial="S1")
        other = profile_for("Z99", serial="S2")
    assert first == again == other == P1_PROFILE
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "S1" in warnings[0].getMessage() and "Z99" in warnings[0].getMessage()


def test_a_missing_code_uses_the_p1_profile_and_warns_once(caplog):
    models._warned_serials.clear()
    with caplog.at_level(logging.WARNING, logger="bridge.bambu.models"):
        assert profile_for("", serial="S3") == P1_PROFILE
        assert profile_for(None, serial="S3") == P1_PROFILE
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_store_persists_the_model_and_hands_it_to_the_fleet(tmp_path):
    path = str(tmp_path / "printers.json")
    store = PrinterStore(path)
    store.upsert("S1", "code", "10.0.0.5")
    store.set_model("S1", "C12")
    store.set_model("NOPE", "C12")

    reloaded = PrinterStore(path)
    assert [c.model for c in reloaded.configs()] == ["C12"]
    assert not reloaded.has("NOPE")


def test_set_model_ignores_blank_and_unchanged(tmp_path, monkeypatch):
    store = PrinterStore(str(tmp_path / "printers.json"))
    store.upsert("S1", "code", "10.0.0.5")
    store.set_model("S1", "C12")
    saves = []
    monkeypatch.setattr(store, "_save", lambda: saves.append(1))
    store.set_model("S1", "C12")
    store.set_model("S1", "")
    assert saves == []
    assert store.configs()[0].model == "C12"


def test_one_discovery_pass_records_every_seen_model():
    seen = []

    class _Dpf:
        def report_discovered(self, printers):
            return {}

    found = [
        DiscoveredPrinter(ip="10.0.0.5", serial="S1", name="P1S-1", model="C12"),
        DiscoveredPrinter(ip="10.0.0.6", serial="S2", name="P1P-1", model="C11"),
    ]
    reporter = DiscoveryReporter(
        _Dpf(),
        discover_fn=lambda _timeout, probe_ips=None: found,
        on_found=lambda printers: seen.extend((p.serial, p.model) for p in printers),
        monotonic=lambda: 0.0,
    )
    reporter.tick()
    assert seen == [("S1", "C12"), ("S2", "C11")]


def test_a_failing_model_callback_does_not_stop_the_discovery_report():
    reported = []

    class _Dpf:
        def report_discovered(self, printers):
            reported.append(printers)
            return {}

    def boom(_printers):
        raise RuntimeError("store is read-only")

    reporter = DiscoveryReporter(
        _Dpf(),
        discover_fn=lambda _timeout, probe_ips=None: [
            DiscoveredPrinter(ip="10.0.0.5", serial="S1", name="P", model="C12"),
        ],
        on_found=boom,
        monotonic=lambda: 0.0,
    )
    reporter.tick()
    assert len(reported) == 1


class _Fleet:
    def __init__(self):
        self.added = []

    def by_id(self, _bambu_id):
        return None

    def add_printer(self, cfg):
        self.added.append(cfg)

    def remove_printer(self, _bambu_id):
        return None


def test_reconciler_carries_a_model_from_3dpf_into_the_store_and_fleet(tmp_path):
    class _Dpf:
        def get_printers_config(self):
            return {"printers": [{
                "bambu_id": "S1", "local_ip": "10.0.0.5", "access_code": "code",
                "model_name": "C12", "printer_id": "p1", "config_version": 1,
            }]}

        def ack_printers_config(self, acks, removed=None):
            return {}

    store = PrinterStore(str(tmp_path / "printers.json"))
    fleet = _Fleet()
    ConfigReconciler(_Dpf(), fleet, store, monotonic=lambda: 0.0).tick()
    assert store.configs()[0].model == "C12"
    assert fleet.added[0].model == "C12"


def test_printer_profile_follows_the_model_it_learns():
    models._warned_serials.clear()
    printer = BambuPrinter(PrinterConfig(bambu_id="S1", ip="10.0.0.5", access_code="x"))
    assert printer.profile == P1_PROFILE
    printer.set_model("C12")
    assert printer.profile.name == "P1S"


# --- Every Bambu model code (plan U6) ----------------------------------------

_ALL_CODES = {
    "BL-P001": "X1C", "BL-P002": "X1", "C13": "X1E", "N6": "X2D",
    "C11": "P1P", "C12": "P1S", "N7": "P2S",
    "N2S": "A1", "N1": "A1 mini", "N9": "A2L",
    "O1D": "H2D", "O1E": "H2D Pro", "O2D": "H2D Pro",
    "O1C": "H2C", "O1C2": "H2C", "O1S": "H2S",
}


def test_every_current_bambu_code_has_its_own_profile():
    for code, name in _ALL_CODES.items():
        assert models.is_known_model(code), code
        assert profile_for(code, serial="S").name == name


def test_start_and_upload_settings_match_the_shop_p1s_for_every_model():
    # The table describes machines. It does not change how a start or an
    # upload is sent until a capture proves another setting (plan U7).
    p1s = profile_for("C12", serial="S")
    for code in _ALL_CODES:
        profile = profile_for(code, serial="S")
        assert profile.start_url_scheme == p1s.start_url_scheme
        assert profile.ftps_session_reuse == p1s.ftps_session_reuse
        assert profile.ftps_tls_max == p1s.ftps_tls_max
        assert profile.vibration_cali == p1s.vibration_cali


def test_only_the_captured_p1s_is_verified_so_far():
    verified = {code for code in _ALL_CODES if profile_for(code, serial="S").verified}
    assert verified == {"C12"}
    assert P1_PROFILE.verified is False


def test_dual_nozzle_models_report_two_nozzles():
    for code in ("O1D", "O1E", "O2D", "O1C", "O1C2", "N6"):
        profile = profile_for(code, serial="S")
        assert profile.nozzle_count == 2
        assert "dual_nozzle" in profile.capabilities
    for code in ("C12", "BL-P001", "N7", "N2S", "O1S"):
        assert profile_for(code, serial="S").nozzle_count == 1


def test_idle_stage_and_idle_bug_by_family():
    assert profile_for("BL-P001", serial="S").idle_stg_cur == -1
    assert profile_for("C12", serial="S").idle_stg_cur == 255
    assert profile_for("C12", serial="S").stg_cur_idle_bug is True
    assert profile_for("N2S", serial="S").stg_cur_idle_bug is True
    assert profile_for("BL-P001", serial="S").stg_cur_idle_bug is False


def test_chamber_heater_only_on_h2_and_x2():
    heaters = {code for code in _ALL_CODES if profile_for(code, serial="S").chamber_heater}
    assert heaters == {"O1D", "O1E", "O2D", "O1C", "O1C2", "O1S", "N6"}
    assert "chamber_temperature" in profile_for("BL-P001", serial="S").capabilities
    assert "chamber_temperature" not in profile_for("C12", serial="S").capabilities


def test_no_model_publishes_drying_yet():
    for code in _ALL_CODES:
        assert profile_for(code, serial="S").publishes_drying is False


def test_unknown_code_is_not_a_known_model():
    assert models.is_known_model("Z99") is False
    assert models.is_known_model(None) is False
