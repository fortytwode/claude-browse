#!/usr/bin/env python3
"""Fake `codex`: `app-server` (JSON lines) and `app-server proxy` (WebSocket
over stdio, only when FAKE_DAEMON=1). Behaviour from FAKE_SCENARIO."""
import base64, hashlib, json, os, struct, sys, threading, time

SCENARIO = os.environ.get("FAKE_SCENARIO", "ok")
ARGS = sys.argv[1:]
PROXY = ARGS[:2] == ["app-server", "proxy"]
if ARGS[:1] != ["app-server"]:
    sys.exit(2)
if PROXY and os.environ.get("FAKE_DAEMON") != "1":
    sys.stderr.write("no daemon\n"); sys.exit(1)
TRACE = os.environ.get("FAKE_TRACE")
DRAINING = False
COUNTER = os.environ.get("FAKE_COUNTER")
if PROXY and COUNTER:
    try: n = int(open(COUNTER).read() or 0)
    except (OSError, ValueError): n = 0
    n += 1
    open(COUNTER, "w").write(str(n))
    DRAINING = n <= int(os.environ.get("FAKE_DRAIN_CONNECTIONS", "0"))
inp, out = sys.stdin.buffer, sys.stdout.buffer
lock = threading.Lock()

def trace(text):
    if TRACE:
        with open(TRACE, "a") as fh: fh.write(text + "\n")

trace(f"start mode={'daemon' if PROXY else 'private'} scenario={SCENARIO} pid={os.getpid()}")

def exact(n):
    data = b""
    while len(data) < n:
        chunk = inp.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def put(obj):
    raw = json.dumps(obj).encode()
    with lock:
        if PROXY:
            n = len(raw); head = b"\x81"
            if n < 126: head += bytes([n])
            elif n < 65536: head += bytes([126]) + struct.pack(">H", n)
            else: head += bytes([127]) + struct.pack(">Q", n)
            out.write(head + raw)
        else:
            out.write(raw + b"\n")
        out.flush()

def messages():
    if not PROXY:
        for line in inp:
            if line.strip(): yield json.loads(line)
        return
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        c = inp.read(1)
        if not c: return
        head += c
    key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    out.write(b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\nsec-websocket-accept: " + accept + b"\r\n\r\n"); out.flush()
    if SCENARIO == "deaf":
        time.sleep(60); return
    try:
        while True:
            b1, b2 = exact(2)
            n = b2 & 0x7F
            if n == 126: n = struct.unpack(">H", exact(2))[0]
            elif n == 127: n = struct.unpack(">Q", exact(8))[0]
            mask = exact(4) if b2 & 0x80 else b""
            data = exact(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if b1 & 0x0F == 8: return
            if b1 & 0x0F == 1: yield json.loads(data)
    except EOFError:
        return

TID = os.environ.get("FAKE_THREAD", "aaaaaaaa-1111-2222-3333-444444444444")
OTHER = "bbbbbbbb-1111-2222-3333-444444444444"
T1 = "turn-0001"; T2 = "turn-0002"
NOW = int(time.time())

def thread(status="idle"):
    return {"id": TID, "path": f"/tmp/rollout-{TID}.jsonl", "preview": "fake thread", "status": {"type": status}, "turns": []}

def note(method, params, tid=TID):
    put({"jsonrpc": "2.0", "method": method, "params": dict(params, threadId=tid)})

def user(i, text): return {"type": "userMessage", "id": i, "content": [{"type": "text", "text": text}]}
def agent(i, text): return {"type": "agentMessage", "id": i, "text": text}
def cmd(i, command, output, code=0, status="completed"):
    return {"type": "commandExecution", "id": i, "command": command, "aggregatedOutput": output, "exitCode": code, "status": status}

OLD_TURN = {"id": T1, "status": "completed", "startedAt": NOW - 900, "completedAt": NOW - 880, "durationMs": 20000,
            "items": [user("u1", "first question"), agent("a1", "first answer")]}
MISSED_TURN = {"id": T2, "status": "completed", "startedAt": NOW - 300, "completedAt": NOW - 284, "durationMs": 16020,
               "items": [user("u2", "Run the long build and tell me the result, this message is long enough to need wrapping on a phone"),
                         agent("a2", "I’ll run the command now."),
                         cmd("c2", "/bin/zsh -lc 'sleep 10; echo finished-marker'", "finished-marker\n"),
                         agent("a3", "## Result\n\n- the build **passed**\n- `finished-marker` was printed")]}
INFLIGHT_TURN = {"id": T2, "status": "inProgress", "startedAt": NOW - 5, "completedAt": None, "durationMs": None,
                 "items": [user("u2", "Run the long build"), agent("a2", "I’ll run the command now.")]}

def finish_inflight():
    time.sleep(float(os.environ.get("FAKE_DELAY", "1.5")))
    note("turn/started", {"turn": {"id": "foreign-turn"}}, tid=OTHER)
    note("item/agentMessage/delta", {"itemId": "zz", "delta": "LEAK FROM ANOTHER THREAD"}, tid=OTHER)
    note("item/commandExecution/outputDelta", {"itemId": "c2", "delta": "finished-marker\n"})
    note("item/completed", {"item": cmd("c2", "/bin/zsh -lc 'sleep 10; echo finished-marker'", "finished-marker\n")})
    note("item/started", {"item": agent("a3", "")})
    for piece in ["The build ", "**passed** and ", "printed the marker, ", "which is a sentence long enough to wrap on a narrow phone screen."]:
        note("item/agentMessage/delta", {"itemId": "a3", "delta": piece}); time.sleep(0.05)
    note("item/completed", {"item": agent("a3", "The build **passed** and printed the marker, which is a sentence long enough to wrap on a narrow phone screen.")})
    note("turn/completed", {"turn": {"id": T2, "status": "completed"}})

def run_turn(text):
    tid = "turn-live"
    note("turn/started", {"turn": {"id": tid}})
    note("item/started", {"item": user("ul", text)}); note("item/completed", {"item": user("ul", text)})
    note("item/started", {"item": {"type": "reasoning", "id": "r1"}}); time.sleep(0.2)
    note("item/completed", {"item": {"type": "reasoning", "id": "r1"}})
    note("item/started", {"item": cmd("cl", "ls | head -2", "", None, "inProgress")})
    time.sleep(float(os.environ.get("FAKE_TURN_SECONDS", "0.3")))
    note("item/completed", {"item": cmd("cl", "ls | head -2", "alpha.txt\nbeta.txt\n")})
    note("item/started", {"item": agent("al", "")})
    for piece in ["You asked: ", text[:40], ". Done."]:
        note("item/agentMessage/delta", {"itemId": "al", "delta": piece})
    note("item/completed", {"item": agent("al", "You asked: " + text[:40] + ". Done.")})
    note("turn/completed", {"turn": {"id": tid, "status": "completed"}})

resumes = 0
for m in messages():
    method, rid, params = m.get("method"), m.get("id"), m.get("params") or {}
    trace(f"recv {method}")
    if rid is None: continue
    def ok(result): put({"jsonrpc": "2.0", "id": rid, "result": result})
    def err(message): put({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": message}})
    if DRAINING and method in ("thread/start", "thread/resume", "thread/fork", "turn/start"):
        err("Server is draining; retry after reconnecting")
    elif method == "initialize":
        if SCENARIO == "badinit" and PROXY: err("daemon too old")
        else: ok({"userAgent": "fake"})
    elif method == "thread/list":
        rows = [{"id": TID, "preview": "Okay. The playback failure, yes, we should fix. Are we converging on a plan for it", "updatedAt": NOW - 20},
                {"id": OTHER, "preview": "Continue the imported CodeX session context from /var/folders/q5/f_3w79553xz26z", "updatedAt": NOW - 12 * 3600}]
        ok({"data": rows[: params.get("limit") or 10]})
    elif method == "thread/start":
        ok({"thread": thread(), "model": "fake-model"})
    elif method == "thread/fork":
        t = thread(); t["id"] = "cccccccc-1111-2222-3333-444444444444"; ok({"thread": t, "model": "fake-model"})
    elif method == "thread/resume":
        resumes += 1
        if SCENARIO == "writer" or (SCENARIO == "norollout"):
            err(f"thread {params.get('threadId')} already has an active writer" if SCENARIO == "writer" else f"no rollout found for thread id {params.get('threadId')}")
        else:
            TID = params.get("threadId") or TID
            ok({"thread": thread("active" if SCENARIO == "inflight" else "idle"), "model": "fake-model"})
            if SCENARIO == "inflight":
                threading.Thread(target=finish_inflight, daemon=True).start()
    elif method == "thread/turns/list":
        if SCENARIO == "inflight": ok({"data": [INFLIGHT_TURN, OLD_TURN]})
        elif SCENARIO == "missed": ok({"data": [MISSED_TURN, OLD_TURN]})
        else: ok({"data": [OLD_TURN]})
    elif method == "thread/unsubscribe":
        ok({"status": "unsubscribed"})
    elif method == "turn/start":
        ok({"turn": {"id": "turn-live", "status": "inProgress", "items": []}})
        text = " ".join(p.get("text", "") for p in params.get("input") or [])
        threading.Thread(target=run_turn, args=(text,), daemon=True).start()
    elif method == "turn/interrupt":
        trace("interrupt received"); ok({})
    else:
        ok({})
trace(f"exit pid={os.getpid()}")
