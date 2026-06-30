"""
Webhook Server
==============
A lightweight Flask application that receives commands from Microsoft Teams
(outgoing webhook) and WhatsApp (Meta Cloud API) and triggers test runs.

Endpoints
---------
POST /teams/webhook
    Teams outgoing webhook. Validates HMAC-SHA256 signature before acting.
    Responds immediately (< 5 s) with an ACK; runs tests in a background thread.

GET  /whatsapp/webhook
    Meta Cloud API verification challenge (hub.mode / hub.verify_token / hub.challenge).

POST /whatsapp/webhook
    Incoming WhatsApp message. Runs tests in background, replies when done.

GET  /health
    Health check — returns {"status": "ok"}.

Starting the server
-------------------
    python -m integrations.webhook_server          # default host 0.0.0.0:5000
    WEBHOOK_PORT=8080 python -m integrations.webhook_server

    # Or import and call:
    from integrations.webhook_server import create_app
    app = create_app()
    app.run(host="0.0.0.0", port=5000)

Environment variables
---------------------
    TEAMS_WEBHOOK_URL               — Teams incoming webhook (for sending back results)
    TEAMS_OUTGOING_WEBHOOK_SECRET   — HMAC secret from Teams Outgoing Webhook config
    WHATSAPP_API_TOKEN              — Meta Cloud API bearer token
    WHATSAPP_PHONE_NUMBER_ID        — Meta phone number ID
    WHATSAPP_VERIFY_TOKEN           — Verification token for Meta webhook registration
    WHATSAPP_NOTIFY_TO              — E.164 recipient number for WhatsApp notifications
    WEBHOOK_HOST                    — bind address (default: 0.0.0.0)
    WEBHOOK_PORT                    — port (default: 5000)
    WEBHOOK_SECRET_KEY              — Flask secret key (default: random on each start)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, request

from integrations.command_parser import (
    HELP_TEXT,
    CommandParseError,
    command_to_argv,
    parse_command,
)
from integrations.teams import TeamsClient
from integrations.whatsapp import WhatsAppClient
from utils.logger import log_info_emoji, log_warning, log_failure

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    """Create and configure the Flask webhook application."""
    app = Flask(__name__)
    app.secret_key = os.getenv("WEBHOOK_SECRET_KEY", os.urandom(24).hex())

    teams = TeamsClient()
    whatsapp = WhatsAppClient()

    # ---- internal state shared between threads ---------------------------
    _last_run: dict = {"status": "no_runs_yet", "timestamp": None, "summary": None}
    _lock = threading.Lock()

    # -----------------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------------

    @app.route("/health", methods=["GET"])
    def health() -> Response:
        return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})

    # -----------------------------------------------------------------------
    # Teams — receive commands
    # -----------------------------------------------------------------------

    @app.route("/teams/webhook", methods=["POST"])
    def teams_webhook() -> Response:
        body = request.get_data()
        auth_header = request.headers.get("Authorization", "")

        # 1. Validate HMAC signature
        if not teams.validate_incoming(body, auth_header):
            log_warning("Teams webhook: HMAC validation failed — request rejected")
            return jsonify({"type": "message", "text": "⛔ Unauthorized request."}), 401

        # 2. Parse JSON body
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return jsonify({"type": "message", "text": "⚠️ Could not parse request body."}), 400

        text = data.get("text", "").strip()

        # 3. Parse command
        try:
            cmd = parse_command(text)
        except CommandParseError as exc:
            return jsonify({"type": "message", "text": f"⚠️ {exc}\n\nSend `!help` for usage."}), 200

        if cmd is None:
            # Not a command — silently ignore (Teams bots receive all messages in channels)
            return jsonify({"type": "message", "text": ""}), 200

        if cmd["action"] == "help":
            return jsonify({"type": "message", "text": HELP_TEXT}), 200

        if cmd["action"] == "status":
            with _lock:
                status = _format_status(_last_run)
            return jsonify({"type": "message", "text": status}), 200

        if cmd["action"] == "run":
            argv = command_to_argv(cmd)
            ack = f"🚀 Test run started\n`python run_tests.py {' '.join(argv)}`"
            _run_tests_async(argv, _last_run, _lock, notify_teams=teams)
            return jsonify({"type": "message", "text": ack}), 200

        return jsonify({"type": "message", "text": "❓ Unknown command."}), 200

    # -----------------------------------------------------------------------
    # WhatsApp — webhook verification (GET)
    # -----------------------------------------------------------------------

    @app.route("/whatsapp/webhook", methods=["GET"])
    def whatsapp_verify() -> Response:
        mode      = request.args.get("hub.mode", "")
        token     = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")

        result = whatsapp.verify_webhook(mode, token, challenge)
        if result is not None:
            log_info_emoji("📱", "WhatsApp webhook verified")
            return Response(result, status=200, mimetype="text/plain")

        log_warning("WhatsApp webhook verification failed — wrong verify_token?")
        return Response("Forbidden", status=403)

    # -----------------------------------------------------------------------
    # WhatsApp — receive messages (POST)
    # -----------------------------------------------------------------------

    @app.route("/whatsapp/webhook", methods=["POST"])
    def whatsapp_message() -> Response:
        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            return jsonify({"status": "ok"}), 200  # always 200 to Meta

        messages = whatsapp.extract_incoming_messages(payload)

        for msg in messages:
            sender = msg["from"]
            text   = msg["text"]

            try:
                cmd = parse_command(text)
            except CommandParseError as exc:
                whatsapp.send_text(sender, f"⚠️ {exc}\n\nSend `!help` for usage.")
                continue

            if cmd is None:
                continue  # not a command, ignore

            if cmd["action"] == "help":
                whatsapp.send_text(sender, HELP_TEXT)
                continue

            if cmd["action"] == "status":
                with _lock:
                    whatsapp.send_text(sender, _format_status(_last_run))
                continue

            if cmd["action"] == "run":
                argv = command_to_argv(cmd)
                ack  = f"🚀 Test run started\n`python run_tests.py {' '.join(argv)}`"
                whatsapp.send_text(sender, ack)
                _run_tests_async(argv, _last_run, _lock, notify_whatsapp=whatsapp, reply_to=sender)

        # Meta always expects 200 OK immediately
        return jsonify({"status": "ok"}), 200

    return app


# ---------------------------------------------------------------------------
# Async test runner
# ---------------------------------------------------------------------------


def _run_tests_async(
    argv: list[str],
    state: dict,
    lock: threading.Lock,
    notify_teams: TeamsClient | None = None,
    notify_whatsapp: WhatsAppClient | None = None,
    reply_to: str | None = None,
) -> None:
    """Run run_tests.py in a background thread and send notifications when done."""

    def _worker() -> None:
        cmd = [sys.executable, "run_tests.py"] + argv
        start = time.monotonic()
        log_info_emoji("🚀", f"[webhook] Starting: {' '.join(cmd)}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30-minute hard limit
            )
            duration = time.monotonic() - start
            success = proc.returncode == 0
            summary = (
                f"{'✅ All tests passed' if success else '❌ Some tests failed'} "
                f"(exit {proc.returncode}, {duration:.1f}s)"
            )
        except subprocess.TimeoutExpired:
            summary = "⏰ Test run timed out after 30 minutes"
            success = False
        except Exception as exc:
            summary = f"💥 Test runner error: {exc}"
            success = False

        with lock:
            state["status"]    = "passed" if success else "failed"
            state["timestamp"] = datetime.utcnow().isoformat()
            state["summary"]   = summary

        log_info_emoji("✅" if success else "❌", f"[webhook] Run complete: {summary}")

        # Send result back via the platform that triggered the run
        if notify_teams and notify_teams.is_configured:
            notify_teams.send_text(summary)

        if notify_whatsapp and reply_to:
            notify_whatsapp.send_text(reply_to, summary)

    thread = threading.Thread(target=_worker, daemon=True, name="webhook-test-runner")
    thread.start()


# ---------------------------------------------------------------------------
# Status formatter
# ---------------------------------------------------------------------------


def _format_status(state: dict) -> str:
    if state.get("status") == "no_runs_yet":
        return "ℹ️ No test runs triggered yet this session."
    emoji = "✅" if state.get("status") == "passed" else "❌"
    ts    = state.get("timestamp", "unknown")
    summ  = state.get("summary", "—")
    return f"{emoji} Last run: {ts}\n{summ}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("WEBHOOK_PORT", "5000"))
    app  = create_app()
    log_info_emoji("🌐", f"Webhook server starting on {host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
