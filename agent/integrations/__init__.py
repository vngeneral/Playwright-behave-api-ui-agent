"""
Integrations Package
====================
Connectors for external messaging platforms:
  - Microsoft Teams (incoming webhook notifications + outgoing webhook commands)
  - WhatsApp (Meta Cloud API or Twilio — notifications + command reception)
  - Unified notifier (wraps Slack + Teams + WhatsApp)
  - Webhook server (Flask — receives commands from Teams and WhatsApp)

All credentials MUST come from environment variables. Nothing is hardcoded here.
"""
