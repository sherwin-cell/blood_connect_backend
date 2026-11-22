"""Centralized notification processing for Blood-Connect.

Firestore listeners detect lifecycle events, write one record per recipient
to the `notifications` collection (the outbox), and a delivery loop sends an
FCM push for each new record.

See README.md for the data contract and deployment notes.
"""
