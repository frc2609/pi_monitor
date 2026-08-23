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

from health_monitor import DEFAULTS, NTPublisher, deep_merge   # noqa: E402

cfg = deep_merge(DEFAULTS, {"nt": {"mode": "server"}})
nt = NTPublisher(cfg)
assert nt.enabled, "stub ntcore should have been accepted"


def sent(key):
    return [v for k, v in PUBLISHED if k == key]


# A reading that has never had a value creates no topic - we cannot know
# whether it is a double or a boolean.
nt.publish({"volts_in": None})
assert sent("volts_in") == [], PUBLISHED
print("PASS  a never-seen reading publishes nothing")

# Once it has a value, it publishes normally.
nt.publish({"volts_in": 5.05})
assert sent("volts_in") == [5.05], PUBLISHED
print("PASS  a real value publishes")

# When it goes missing, the topic gets NaN rather than being left stale.
nt.publish({"volts_in": None})
last = sent("volts_in")[-1]
assert isinstance(last, float) and math.isnan(last), PUBLISHED
print("PASS  a reading that goes missing publishes NaN, not a stale value")

# Booleans have no NaN. A missing one keeps its last state, which is the least
# bad option - worth knowing when reading a trace.
nt.publish({"throttled_now": True})
before = len(sent("throttled_now"))
nt.publish({"throttled_now": None})
assert len(sent("throttled_now")) == before, PUBLISHED
print("PASS  a missing boolean is left alone (no NaN exists for it)")

# Booleans must not be mistaken for numbers - bool is a subclass of int.
assert nt.numeric["throttled_now"] is False
assert nt.numeric["volts_in"] is True
print("PASS  booleans publish as booleans, not doubles")

# Strings never reach NetworkTables.
nt.publish({"some_text": "hello"})
assert sent("some_text") == [], PUBLISHED
print("PASS  strings are dropped")

print("\nALL PASS")
