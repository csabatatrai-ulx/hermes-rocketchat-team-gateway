#!/usr/bin/env python3
"""Checks the try-it stack end to end: python3 try/check.py

Runs against the stack from try/compose.yaml with its scripted demo model, signs in as alice and bob over the
Rocket.Chat REST API and prints PASS or FAIL per check. Exit code 0 only if all pass. A button click sends a
preset message as the person who clicks; this script sends that same message (`@hermes /approve`) as alice or
bob, which is exactly what their click would send. Standard library only.

  TRY_PORT        Rocket.Chat port (default 3000)
  TRY_DEMO_PORT   the demo model's port (default 3001)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

RC = f"http://127.0.0.1:{os.environ.get('TRY_PORT', '3000')}/api/v1/"
MODEL = f"http://127.0.0.1:{os.environ.get('TRY_DEMO_PORT', '3001')}"
PASSWORD = "hermes-demo"
ROOM = "hermes-demo"
results = []


def call(path, body=None, auth=None):
    req = urllib.request.Request(RC + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("X-Auth-Token", auth["token"])
        req.add_header("X-User-Id", auth["id"])
    for _ in range(20):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code != 429:  # Rocket.Chat's API rate limiter: wait and ask again
                return {"_status": exc.code}
            time.sleep(3)
    return {"_status": 429}


def login(user):
    data = call("login", {"user": user, "password": PASSWORD})
    if data.get("status") != "success":
        sys.exit(f"Could not sign in as {user}. Is the stack up (docker compose -f try/compose.yaml up -d)?")
    return {"token": data["data"]["authToken"], "id": data["data"]["userId"], "name": user}


def post(who, rid, text):
    return call("chat.postMessage", {"roomId": rid, "text": text}, who)["message"]


def bot_replies(trigger, rid, who, kind="channels"):
    """The bot's messages after `trigger`, in order."""
    history = call(f"{kind}.history?roomId={rid}&count=30", auth=who).get("messages", [])
    return [m for m in reversed(history) if m["u"]["username"] == "hermes" and m["ts"] > trigger["ts"]]


def wait(trigger, rid, who, until, timeout=90, kind="channels"):
    end = time.time() + timeout
    while time.time() < end:
        found = bot_replies(trigger, rid, who, kind)
        if until(found):
            return found
        time.sleep(2)
    return bot_replies(trigger, rid, who, kind)


def text(messages):
    return "\n".join(m.get("msg", "") for m in messages)


def buttons(messages):
    return [a.get("text") for m in messages for att in (m.get("attachments") or []) for a in (att.get("actions") or [])]


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      got: {detail[:300]!r}"), flush=True)


def seen(word):
    with urllib.request.urlopen(f"{MODEL}/seen?text={word}", timeout=10) as resp:
        return json.loads(resp.read())["count"]


def main():
    try:
        with urllib.request.urlopen(f"{MODEL}/v1/models", timeout=5) as resp:
            assert b'"demo"' in resp.read()
    except Exception:  # noqa: BLE001
        sys.exit("The demo model does not answer on port {}. check.py needs the stack with its demo model "
                 "(no LLM_API_KEY, no Nous Portal sign-in).".format(os.environ.get("TRY_DEMO_PORT", "3001")))
    alice, bob = login("alice"), login("bob")
    rid = call(f"channels.info?roomName={ROOM}", auth=alice)["channel"]["_id"]
    tag = uuid.uuid4().hex[:6]
    print(f"Checking the try-it stack ({RC[:-8]}), run {tag}\n")

    # 1. A mention gets an answer that quotes the question and mentions the asker.
    m = post(alice, rid, f"@hermes hello from alice {tag}")
    r = wait(m, rid, alice, lambda f: m["_id"] in text(f), timeout=180)
    check("a mention gets an answer that quotes the question and mentions the asker",
          m["_id"] in text(r) and "@alice" in text(r), text(r))

    # 2. Two people at the same moment: each answer is for its own asker.
    ma = post(alice, rid, f"@hermes what is the plan, alice {tag}?")
    mb = post(bob, rid, f"@hermes what is the plan, bob {tag}?")
    ra = wait(ma, rid, alice, lambda f: ma["_id"] in text(f) and mb["_id"] in text(f))
    to_alice = [x for x in ra if ma["_id"] in x.get("msg", "")]
    to_bob = [x for x in ra if mb["_id"] in x.get("msg", "")]
    check("two people at once: each answer quotes its own question and mentions its own asker",
          bool(to_alice) and bool(to_bob) and "@alice" in text(to_alice) and "@bob" in text(to_bob), text(ra))

    # 3. A Hermes command that is not allowed is refused, and the model never sees it.
    before = seen("yolo")
    m = post(alice, rid, "@hermes /yolo on")
    r = wait(m, rid, alice, lambda f: "not enabled" in text(f), timeout=30)
    check("/yolo is refused over chat, without reaching the model",
          "not enabled" in text(r) and seen("yolo") == before, text(r))

    # 4. A dangerous command waits for a person, with buttons on the prompt.
    m = post(alice, rid, f"@hermes please delete the old build ({tag})")
    r = wait(m, rid, alice, lambda f: bool(buttons(f)))
    check("a dangerous command waits for approval, with buttons on the prompt",
          "Allow Once" in buttons(r) and "Done." not in text(r), f"{buttons(r)} {text(r)}")

    # 5. Another person's click (Bob's) runs nothing.
    b = post(bob, rid, "@hermes /approve")
    rb = wait(b, rid, bob, lambda f: b["_id"] in text(f), timeout=30)
    time.sleep(3)
    check("another person's approval runs nothing",
          "No pending" in text(rb) and "Done." not in text(bot_replies(m, rid, alice)), text(rb))

    # 6. The asker's click runs it.
    a = post(alice, rid, "@hermes /approve")
    ra = wait(a, rid, alice, lambda f: "Done." in text(f))
    check("the asker's approval runs the command", "printed: removed" in text(ra), text(ra))

    # 7. A one-to-one DM needs no mention.
    dm = call("im.create", {"username": "hermes"}, alice)["room"]["_id"]
    m = post(alice, dm, f"hello in a DM {tag}")
    r = wait(m, dm, alice, lambda f: bool(f), kind="im")
    check("a direct message needs no mention", "Demo model" in text(r), text(r))

    print(f"\n{sum(results)} / {len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
