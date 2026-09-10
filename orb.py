#!/usr/bin/env python3
"""BRAIN orb: serves the sphere page and a tiny ElevenLabs TTS proxy.

Runs on 127.0.0.1:8090 behind Caddy (handle_path /orb*), as the openclaw user.
- GET  /            -> index.html (the sphere + voice page)
- POST /tts         -> {"text": ...} -> audio/mpeg from ElevenLabs
- POST /stt         -> raw audio body -> {"text": ...} via ElevenLabs Scribe
Auth for /tts: Authorization: Bearer <gateway token> (same secret as the dashboard).
Keys are read at request time from ~/.openclaw/.env and ~/.openclaw/openclaw.json;
nothing secret is stored in this repo.
"""
import http.server, json, os, re, urllib.request

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".openclaw", "openclaw.json")
ENV = os.path.join(HOME, ".openclaw", ".env")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # "Rachel"

KEYS_FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>BRAIN keys</title><body style="font-family:system-ui;max-width:520px;margin:40px auto;padding:0 16px;background:#000;color:#eee">
<h2>BRAIN &middot; keys</h2>
<p style="color:#aaa">Saved only on this server. Leave a field blank to keep what's already there.</p>
<form method=post>
<label>Gateway secret (to confirm it's you)<br><input name=secret type=password style="width:100%;padding:10px;margin:6px 0 14px;background:#111;border:1px solid #333;color:#eee;border-radius:8px" required autocomplete=off></label>
<label>ElevenLabs API key<br><input name=eleven style="width:100%;padding:10px;margin:6px 0 14px;background:#111;border:1px solid #333;color:#eee;border-radius:8px" autocomplete=off></label>
<label>ElevenLabs voice ID (optional)<br><input name=voice style="width:100%;padding:10px;margin:6px 0 14px;background:#111;border:1px solid #333;color:#eee;border-radius:8px" autocomplete=off></label>
<label>Anthropic API key (optional, only to replace it)<br><input name=anthropic style="width:100%;padding:10px;margin:6px 0 14px;background:#111;border:1px solid #333;color:#eee;border-radius:8px" autocomplete=off placeholder="sk-ant-..."></label>
<button style="padding:10px 18px;background:#a78bfa;border:none;border-radius:8px;font-weight:700">Save</button></form>%MSG%</body>"""


def read_env():
    out = {}
    try:
        for line in open(ENV):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def read_cfg():
    try:
        return json.load(open(CFG))
    except Exception:
        return {}


def gateway_token():
    return (((read_cfg().get("gateway") or {}).get("auth") or {}).get("token") or "").strip()


def eleven_settings():
    env = read_env()
    tts = ((read_cfg().get("tts") or {}).get("providers") or {}).get("elevenlabs") or {}
    key = tts.get("apiKey") or ""
    if key.startswith("${"):  # ${ELEVENLABS_API_KEY} style reference
        key = env.get(key.strip("${}"), "")
    key = key or env.get("ELEVENLABS_API_KEY", "")
    voice = tts.get("speakerVoiceId") or env.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE
    model = tts.get("model") or "eleven_flash_v2_5"
    return key, voice, model


class H(http.server.BaseHTTPRequestHandler):
    server_version = "brain-orb/1"

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("", "/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.startswith("/keys"):
            return self._send(200, KEYS_FORM.replace("%MSG%", "").encode(), "text/html; charset=utf-8")
        return self._send(404, b"not found")

    def do_keys(self):
        """Owner-entered keys page: merges values into ~/.openclaw/.env (never overwrites
        other keys). Protected by the gateway secret typed into the form."""
        import urllib.parse
        n = int(self.headers.get("Content-Length", "0") or 0)
        d = urllib.parse.parse_qs(self.rfile.read(n).decode())
        get = lambda k: (d.get(k, [""])[0] or "").strip()
        if not gateway_token() or get("secret") != gateway_token():
            return self._send(200, KEYS_FORM.replace("%MSG%", "<p style=color:#f87171>Wrong gateway secret.</p>").encode(), "text/html; charset=utf-8")
        updates = {}
        if get("eleven"):
            updates["ELEVENLABS_API_KEY"] = get("eleven")
        if get("voice"):
            updates["ELEVENLABS_VOICE_ID"] = get("voice")
        if get("anthropic").startswith("sk-ant-"):
            updates["ANTHROPIC_API_KEY"] = get("anthropic")
        if not updates:
            return self._send(200, KEYS_FORM.replace("%MSG%", "<p style=color:#f87171>Nothing to save.</p>").encode(), "text/html; charset=utf-8")
        env = read_env()
        env.update(updates)
        os.makedirs(os.path.dirname(ENV), exist_ok=True)
        with open(ENV, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in env.items()))
        os.chmod(ENV, 0o600)
        saved = ", ".join(updates.keys())
        return self._send(200, KEYS_FORM.replace("%MSG%", f"<p style=color:#4ade80>Saved: {saved}. Voice is active immediately; a new Anthropic key takes effect after the gateway restarts.</p>").encode(), "text/html; charset=utf-8")

    def do_stt(self):
        """Speech-to-text via ElevenLabs Scribe. Body: raw audio (webm/ogg/mp4/wav)."""
        auth = self.headers.get("Authorization", "")
        tok = gateway_token()
        if not tok or auth != "Bearer " + tok:
            return self._send(401, b"unauthorized")
        n = int(self.headers.get("Content-Length", "0") or 0)
        audio = self.rfile.read(n) if n else b""
        if len(audio) < 800:
            return self._send(200, json.dumps({"text": ""}).encode(), "application/json")
        key, _, _ = eleven_settings()
        if not key:
            return self._send(503, b"ElevenLabs key not configured on the server")
        ctype = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip() or "audio/webm"
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/wav": "wav", "audio/mpeg": "mp3"}.get(ctype, "webm")
        boundary = "----brainorb" + os.urandom(8).hex()
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v1\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"tag_audio_events\"\r\n\r\nfalse\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.{ext}\"\r\nContent-Type: {ctype}\r\n\r\n".encode(),
            audio, b"\r\n", f"--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/speech-to-text",
            data=body,
            headers={"xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
                text = (data.get("text") or "").strip()
                return self._send(200, json.dumps({"text": text}).encode(), "application/json")
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode(errors="replace")
            return self._send(502, f"Scribe {e.code}: {detail}".encode())
        except Exception as e:
            return self._send(502, f"Scribe unreachable: {e}".encode())

    def do_POST(self):
        if self.path.startswith("/stt"):
            return self.do_stt()
        if self.path.startswith("/keys"):
            return self.do_keys()
        if self.path != "/tts":
            return self._send(404, b"not found")
        auth = self.headers.get("Authorization", "")
        tok = gateway_token()
        if not tok or auth != "Bearer " + tok:
            return self._send(401, b"unauthorized")
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            text = (json.loads(self.rfile.read(n) or b"{}").get("text") or "").strip()
        except Exception:
            text = ""
        if not text:
            return self._send(400, b"missing text")
        key, voice, model = eleven_settings()
        if not key:
            return self._send(503, b"ElevenLabs key not configured on the server")
        # keep spoken replies reasonable in length (and cost)
        text = re.sub(r"\s+", " ", text)[:1800]
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
            data=json.dumps({
                "text": text,
                "model_id": model,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
            }).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return self._send(200, r.read(), "audio/mpeg")
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode(errors="replace")
            return self._send(502, f"ElevenLabs {e.code}: {detail}".encode())
        except Exception as e:
            return self._send(502, f"ElevenLabs unreachable: {e}".encode())

    def log_message(self, fmt, *args):  # quieter journal
        if "/tts" in fmt % args or "/stt" in fmt % args or "GET / " in fmt % args:
            super().log_message(fmt, *args)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", 8090), H).serve_forever()
