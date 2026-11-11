#!/usr/bin/env python3
"""Sets up the try-it stack: a bot user with a personal access token, alice and bob, the #hermes-demo room,
and Hermes's .env and config.yaml. Safe to run again: it reuses what already exists.

Standard library only; it runs in a stock Python container.
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

RC = "http://rocketchat:3000/api/v1/"
HOME = pathlib.Path("/hermes")
PASSWORD = "hermes-demo"
ROOM = "hermes-demo"


def call(path, body=None, auth=None):
    req = urllib.request.Request(RC + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("X-Auth-Token", auth["token"])
        req.add_header("X-User-Id", auth["id"])
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        data = json.loads(raw or b"{}")
    except ValueError:  # Rocket.Chat answers with a page while it is still starting
        data = {}
    return data if status < 400 else dict(data, _status=status)


def login(user):
    data = call("login", {"user": user, "password": PASSWORD})
    if data.get("status") != "success":
        raise SystemExit(f"Could not log in as {user}: {data}")
    return {"token": data["data"]["authToken"], "id": data["data"]["userId"]}


print("Waiting for Rocket.Chat...")
for _ in range(180):
    try:
        with urllib.request.urlopen("http://rocketchat:3000/api/info", timeout=10) as resp:
            if b'"version"' in resp.read():
                break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise SystemExit("Rocket.Chat did not start")
for _ in range(60):  # the admin account appears a moment after the API
    try:
        admin = login("admin")
        break
    except SystemExit:
        time.sleep(2)
else:
    admin = login("admin")

users = {}
for name, roles in (("hermes", ["user", "bot"]), ("alice", ["user"]), ("bob", ["user"])):
    found = call(f"users.info?username={name}", auth=admin)
    if found.get("success"):
        users[name] = found["user"]["_id"]
        continue
    made = call("users.create", {"username": name, "name": name.capitalize(), "email": f"{name}@example.com",
                                 "password": PASSWORD, "roles": roles, "verified": True,
                                 "requirePasswordChange": False, "sendWelcomeEmail": False}, auth=admin)
    users[name] = made["user"]["_id"]

bot = login("hermes")
call("users.removePersonalAccessToken", {"tokenName": "try"}, auth=bot)
token = call("users.generatePersonalAccessToken", {"tokenName": "try", "bypassTwoFactor": True}, auth=bot)["token"]

room = call(f"channels.info?roomName={ROOM}", auth=admin).get("channel")
if not room:
    room = call("channels.create", {"name": ROOM, "members": ["hermes", "alice", "bob"]}, auth=admin)["channel"]

env = {
    "ROCKETCHAT_TEAM_URL": "http://rocketchat:3000",
    "ROCKETCHAT_TEAM_BOT_USERNAME": "hermes",
    "ROCKETCHAT_TEAM_USER_ID": users["hermes"],
    "ROCKETCHAT_TEAM_TOKEN": token,
    "ROCKETCHAT_TEAM_ALLOWED_USERS": f"{users['alice']},{users['bob']}",
    "ROCKETCHAT_TEAM_HOME_CHANNEL": room["_id"],
}
(HOME / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
if os.environ.get("LLM_API_KEY"):
    model = f"""model:
  provider: custom
  default: {os.environ.get('LLM_MODEL') or 'gpt-4.1-mini'}
  base_url: {os.environ.get('LLM_BASE_URL') or 'https://api.openai.com/v1'}
  api_key: {os.environ['LLM_API_KEY']}
"""
else:
    # The scripted demo model, so the stack works without any account. `hermes portal` signs in to Nous Portal,
    # picks a real model and rewrites this section.
    model = """model:
  provider: custom
  default: demo
  base_url: http://demo-model:8080/v1
  api_key: demo
  context_length: 128000
"""
config = HOME / "config.yaml"
if not config.exists():  # keep a model chosen with `hermes portal` across restarts
    config.write_text(model + """plugins:
  enabled: [rocketchat-team-gateway]
# The bot may use a terminal, inside this container only, and asks a person before anything dangerous.
platform_toolsets:
  rocketchat_team: [terminal]
approvals:
  mode: manual
memory:
  memory_enabled: false
  user_profile_enabled: false
auxiliary:
  title_generation:
    enabled: false
""")
for f in (HOME / ".env", HOME / "config.yaml"):
    os.chown(f, 10000, 10000)
    f.chmod(0o600)
os.chown(HOME, 10000, 10000)
print("Ready. Open the Rocket.Chat address, sign in as alice or bob (password: hermes-demo), "
      f"and say '@hermes hi' in #{ROOM}.")
if not os.environ.get("LLM_API_KEY"):
    print("Model: the scripted demo model (not AI). `python3 try/check.py` checks the whole path. For a real model, "
          "sign in to Nous Portal with `docker compose -f try/compose.yaml exec -u hermes hermes hermes portal`, "
          "then restart the hermes service.")
