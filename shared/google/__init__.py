"""Per-user Google integrations (OAuth, calendar, Gmail).

Architecture:
  authenticated LifeSight user
    → that user's google_connections row
    → Google access only for that account

Never accept a client-supplied user_id for credential selection.
Claude tools never receive raw Google credentials.
"""
