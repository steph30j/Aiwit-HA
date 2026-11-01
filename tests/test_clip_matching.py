"""Regression tests for event <-> clip matching (no Home Assistant needed).

Run from the repository root:  python3 tests/test_clip_matching.py
Incident: 2026-10-31, event 14:51 local; the 13:48 clip (visible in the cloud) was archived
instead of the 14:51 clip because filenames were also read as UTC.
"""
import importlib, sys, types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

pkg = types.ModuleType("aiwit")
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "aiwit")]
sys.modules["aiwit"] = pkg
client = importlib.import_module("aiwit.client")
archive_src = (Path(pkg.__path__[0]) / "clip_archive.py").read_text()

TZ = ZoneInfo("Europe/Paris")
client._ha_local_timezone = lambda: TZ


class Fake(client.AiwitClient):
    def __init__(self, names):
        self.names = names

    def list_clips(self, dev, day, type_filter=0, page=1):
        items = [{"fileName": n + ".ts2", "bucket": "b", "endpoint": "e", "pk": "x" * 16}
                 for n in self.names if n.startswith(day)]
        return {"resultCode": 0, "content": {"items": items}}


def local(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).astimezone(timezone.utc)


def pick(names, event, window):
    c = Fake(names).find_event_clip("U", event, max_delta_seconds=window)
    return c["fileName"][:14] if c else None


fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"{label}: {got}")

W = 120
ev_1451 = local(2026, 10, 31, 14, 51, 10)           # winter, UTC+1
check("filename read as local only", len(client.AiwitClient._clip_timestamp_candidates("20261031134811.ts2")), 1)
check("INCIDENT 14:51 event, only 13:48 clip visible", pick(["20261031134811"], ev_1451, W), None)
check("INCIDENT with the old 15 min window", pick(["20261031134811"], ev_1451, 900), None)
check("14:51 clip published -> selected", pick(["20261031134811", "20261031145105"], ev_1451, W), "20261031145105")
check("13:48 event -> its own clip", pick(["20261031134811"], local(2026, 10, 31, 13, 48, 20), W), "20261031134811")
ev_summer = local(2026, 7, 15, 14, 51, 10)           # summer, UTC+2: gap would be 2 h
check("summer: previous-hour clip not matched", pick(["20260715124811"], ev_summer, 900), None)
check("fall-back night 2nd 02:05 occurrence", pick(["20261025020500"], datetime(2026, 10, 25, 1, 5, 20, tzinfo=timezone.utc), W), "20261025020500")
check("fall-back night 1st 02:05 occurrence", pick(["20261025020500"], datetime(2026, 10, 25, 0, 5, 20, tzinfo=timezone.utc), W), "20261025020500")
check("archive default window is 120 s", "match_window: int = 120" in archive_src, True)
sys.exit(1 if fails else 0)
