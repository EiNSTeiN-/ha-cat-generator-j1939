import importlib.util
from pathlib import Path

import pytest
from decoda import SPN
from decoda.exceptions import UnknownReferenceError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cat_generator_dm1", ROOT / "dm1.py")
dm1 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dm1)


class _Spns:
    def __init__(self, spns=None):
        self._spns = spns or {}

    def get_by_id(self, spn_id):
        try:
            return self._spns[spn_id]
        except KeyError:
            raise UnknownReferenceError(f"SPN not found for id: {spn_id}")


class _Spec:
    def __init__(self, spns=None):
        self.SPNs = _Spns(spns)


def _dtc_bytes(spn_id: int, fmi: int, occurrence_count: int) -> bytes:
    return bytes(
        [
            spn_id & 0xFF,
            (spn_id >> 8) & 0xFF,
            ((spn_id >> 11) & 0xE0) | (fmi & 0x1F),
            occurrence_count,
        ]
    )


def test_parse_uses_local_failure_to_start_spn_for_cat_dm1_payload():
    message = dm1.parse(_Spec(), b"\x55\xff" + _dtc_bytes(1664, 7, 3))

    assert len(message.dtcs) == 1
    assert message.dtcs[0].spn.id == 1664
    assert message.dtcs[0].spn.name == "Failure to Start"
    assert message.dtcs[0].spn.description == "The engine failed to start"
    assert message.dtcs[0].fmi == 7
    assert message.dtcs[0].oc == 3


def test_parse_skips_unknown_dtc_spns_without_dropping_known_cat_dtcs():
    known = SPN(
        id=1208,
        name="Engine Oil Filter Intake Pressure",
        description="",
        value_decoder=None,
    )
    message = dm1.parse(
        _Spec({1208: known}),
        b"\x55\xff" + _dtc_bytes(9999, 2, 1) + _dtc_bytes(1208, 16, 4),
    )

    assert [dtc.spn.id for dtc in message.dtcs] == [1208]
    assert message.dtcs[0].fmi == 16
    assert message.dtcs[0].oc == 4


@pytest.mark.parametrize("payload", [b"", b"\x55"])
def test_parse_rejects_payloads_without_lamp_bytes(payload):
    with pytest.raises(ValueError, match="DM1 message too short"):
        dm1.parse(_Spec(), payload)
