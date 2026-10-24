#!/usr/bin/env python3
"""A stand-in hermes (and systemctl) for the Delivery page test (tests/delivery/delivery.test.js): it records every call in
~/calls.jsonl and answers the way Hermes would, writing a log line for `message` so the page has something real to show.
A message containing "quarantine-me" is quarantined, as the fake Janus would do. It decides nothing that matters."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

H = Path(os.path.expanduser("~"))
args = sys.argv[1:]
text = sys.stdin.read() if args[:1] == ["message"] else ""
with open(H / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "stdin": text}) + "\n")
now = datetime.now().astimezone().isoformat(timespec="seconds")
if args[:1] == ["message"]:
    bad = "quarantine-me" in text
    name = "note-%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S")
    log = H / ".local/share/hermes/log" / ("deliveries-%s.jsonl" % now[:7])
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now, "id": "m-" + name, "route": args[1], "dir": "in", "from": "local", "to": None if bad else "~/mail/" + name,
                            "name": name, "bytes": len(text.encode()), "outcome": "quarantined" if bad else "delivered",
                            "reasons": ["the fake Janus does not like it"] if bad else []}) + "\n")
    print("%s: %s" % (name, "quarantined: the fake Janus does not like it" if bad else "delivered"))
    sys.exit(1 if bad else 0)
elif args[:1] == ["ack"]:
    try:
        os.remove(H / ".local/state/argus-ui/alerts/hermes.json")
    except FileNotFoundError:
        pass
    print("2 marked as seen")
elif args[:2] == ["--user", "start"]:
    print("")
else:
    sys.exit("fake hermes: unknown command %s" % args)
