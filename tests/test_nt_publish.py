"""
What reaches NetworkTables, checked against a stub ntcore.

The point of interest is missing readings. Skipping a publish leaves the topic
holding its last value, so a sensor that stopped answering looks like a healthy
frozen one in AdvantageScope. Doubles get NaN instead, which draws as a gap.

    python3 tests/test_nt_publish.py
"""
import math
import sys
import types
from pathlib import Path

sys.path.insert(0, "src")


# --- stub ntcore ----------------------------------------------------------
class Pub:
    def __init__(self, log, key):
        self.log, self.key = log, key

    def set(self, value):
        self.log.append((self.key, value))

    def close(self):
        pass


class Topic:
    def __init__(self, log, key):
        self.log, self.key = log, key

    def publish(self):
        return Pub(self.log, self.key)


class Table:
    def __init__(self, log):
        self.log = log

    def getBooleanTopic(self, key):
        return Topic(self.log, key)

    def getDoubleTopic(self, key):
        return Topic(self.log, key)


class Inst:
    def __init__(self, log):
        self.log = log

    def startServer(self):
        pass

    def getTable(self, name):
        return Table(self.log)


PUBLISHED = []
fake = types.ModuleType("ntcore")
fake.NetworkTableInstance = types.SimpleNamespace(getDefault=lambda: Inst(PUBLISHED))
sys.modules["ntcore"] = fake

from health_monitor import (DEFAULTS, NTPublisher, deep_merge,   # noqa: E402
                            nt_topic)

cfg = deep_merge(DEFAULTS, {"nt": {"mode": "server"}})
nt = NTPublisher(cfg)
assert nt.enabled, "stub ntcore should have been accepted"


def sent(key):
    return [v for k, v in PUBLISHED if k == key]


# A reading that has never had a value creates no topic - we cannot know
# whether it is a double or a boolean.
nt.publish({"volts_in": None})
assert sent("power/volts_in") == [], PUBLISHED
print("PASS  a never-seen reading publishes nothing")

# Once it has a value, it publishes normally.
nt.publish({"volts_in": 5.05})
assert sent("power/volts_in") == [5.05], PUBLISHED
print("PASS  a real value publishes")

# When it goes missing, the topic gets NaN rather than being left stale.
nt.publish({"volts_in": None})
last = sent("power/volts_in")[-1]
assert isinstance(last, float) and math.isnan(last), PUBLISHED
print("PASS  a reading that goes missing publishes NaN, not a stale value")

# Booleans have no NaN. A missing one keeps its last state, which is the least
# bad option - worth knowing when reading a trace.
nt.publish({"throttled_now": True})
before = len(sent("throttle/throttled_now"))
nt.publish({"throttled_now": None})
assert len(sent("throttle/throttled_now")) == before, PUBLISHED
print("PASS  a missing boolean is left alone (no NaN exists for it)")

# Booleans must not be mistaken for numbers - bool is a subclass of int.
assert nt.numeric["throttle/throttled_now"] is False
assert nt.numeric["power/volts_in"] is True
print("PASS  booleans publish as booleans, not doubles")

# Strings never reach NetworkTables.
nt.publish({"some_text": "hello"})
assert sent("some_text") == [], PUBLISHED
print("PASS  strings are dropped")


# Grouped topic names are what make AdvantageScope show collapsible headings
# instead of thirty loose entries.
assert nt_topic("volts_in") == "power/volts_in"
assert nt_topic("throttled_raw") == "throttle/throttled_raw"
assert nt_topic("svc_photonvision_active") == "services/photonvision"
assert nt_topic("ok") == "ok", "ok stays at the top"
assert nt_topic("heartbeat") == "heartbeat", "heartbeat stays at the top"
assert nt_topic("something_new") == "something_new", "unknown fields are not lost"
print("PASS  topics are grouped, ok and heartbeat stay at the top")

# A watched service must appear even before it is installed. It publishes as a
# number because a boolean cannot express "not installed", and a topic that
# never receives a value never shows up in the tree at all.
nt.publish({"svc_photonvision_active": None})
v = sent("services/photonvision")
assert v and math.isnan(v[-1]), v
nt.publish({"svc_photonvision_active": True})
assert sent("services/photonvision")[-1] == 1.0
nt.publish({"svc_photonvision_active": False})
assert sent("services/photonvision")[-1] == 0.0
print("PASS  a service appears even when not installed (NaN / 1 / 0)")
print("\nALL PASS")
