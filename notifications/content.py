"""User-facing notification text."""

from __future__ import annotations

from datetime import datetime


def _blood(data: dict) -> str:
    return str(data.get("bloodType") or data.get("requestBloodType") or "blood").strip()


def _hospital_suffix(data: dict) -> str:
    hospital = str(data.get("hospitalName") or "").strip()
    return f" at {hospital}" if hospital else ""


def _needed_by_suffix(data: dict) -> str:
    value = data.get("neededByDate")
    if isinstance(value, datetime):
        return f", needed by {value.strftime('%b %d, %Y')}"
    return ""


def _excerpt(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def donor_application(status: str) -> tuple[str, str]:
    if status == "approved":
        return (
            "Donor application approved",
            "PRC approved your donor application. Watch for your screening appointment.",
        )
    return (
        "Donor application not approved",
        "PRC reviewed your donor application. Open Blood-Connect to see the reason.",
    )


def donor_appointment(donor: dict) -> tuple[str, str]:
    status = str(donor.get("appointmentStatus") or "").strip().lower()
    appointment = donor.get("appointment") or {}
    when = " ".join(
        part for part in (str(appointment.get("date") or ""), str(appointment.get("time") or "")) if part
    )
    where = str(appointment.get("location") or "").strip()
    details = ", ".join(part for part in (when, where) if part)

    if status == "scheduled":
        return ("Screening appointment scheduled", f"PRC scheduled your appointment: {details}." if details else "PRC scheduled your appointment.")
    if status in ("rescheduled", "updated"):
        return ("Appointment updated", f"Your appointment was changed: {details}." if details else "Your appointment details changed.")
    if status in ("cancelled", "canceled"):
        return ("Appointment cancelled", "PRC cancelled your screening appointment.")
    if status == "completed":
        return ("Appointment completed", "Thank you for attending your PRC appointment.")
    if status in ("missed", "no_show", "no-show"):
        return ("Appointment missed", "You missed your PRC appointment. Contact PRC to reschedule.")
    return ("Appointment update", f"Your appointment status is now: {status}.")


def id_verification(status: str) -> tuple[str, str]:
    if status == "approved":
        return ("ID verified", "PRC verified the ID you submitted.")
    return ("ID not verified", "PRC could not verify your ID. Open Blood-Connect for details.")


def request_submitted(request: dict) -> tuple[str, str]:
    return (
        "Blood request received",
        f"Your {_blood(request)} request was submitted. PRC will review it shortly.",
    )


def request_approved(request: dict) -> tuple[str, str]:
    return (
        "Blood request approved",
        f"PRC approved your {_blood(request)} request. Matching donors are being notified.",
    )


def request_rejected(request: dict) -> tuple[str, str]:
    return (
        "Blood request not approved",
        f"PRC could not approve your {_blood(request)} request. Open Blood-Connect to see why.",
    )


def request_fulfilled(request: dict) -> tuple[str, str]:
    return (
        "Blood request fulfilled",
        f"PRC marked your {_blood(request)} request as fulfilled.",
    )


def request_expired(request: dict) -> tuple[str, str]:
    return (
        "Blood request expired",
        f"Your blood request for {_blood(request)} has expired. "
        "Submit a new request if blood is still needed.",
    )


def request_open(request: dict) -> tuple[str, str]:
    urgent = str(request.get("urgency") or "").strip().lower() == "urgent"
    title = "Urgent: blood donor needed" if urgent else "Blood donor needed near you"
    return (
        title,
        f"A patient needs {_blood(request)}{_hospital_suffix(request)}{_needed_by_suffix(request)}. Tap to help.",
    )


def request_closed_for_donor(request: dict, status: str) -> tuple[str, str]:
    if status == "fulfilled":
        return (
            "Request fulfilled",
            f"The {_blood(request)} request you offered to help with has been fulfilled. Thank you!",
        )
    if status == "expired":
        return (
            "Request expired",
            f"The {_blood(request)} request you offered to help with has expired.",
        )
    return (
        "Request cancelled",
        f"The {_blood(request)} request you offered to help with was cancelled by the requester.",
    )


def offer_received(offer: dict) -> tuple[str, str]:
    blood_type = str(offer.get("donorBloodType") or "").strip()
    donor = f"A {blood_type} donor" if blood_type else "A donor"
    return (
        "A donor offered to help",
        f"{donor} offered to donate. PRC will review the match.",
    )


def offer_confirmed_for_requester(offer: dict) -> tuple[str, str]:
    name = str(offer.get("donorName") or "A donor").strip()
    return ("Donor confirmed", f"PRC confirmed {name} as a donor for your request.")


def offer_approved(offer: dict) -> tuple[str, str]:
    return (
        "Your offer was confirmed",
        f"PRC confirmed your offer to donate{_hospital_suffix(offer)}. PRC will contact you with next steps.",
    )


def offer_rejected(offer: dict) -> tuple[str, str]:
    return (
        "Offer not selected",
        "Thank you for offering. PRC did not need your donation for this request.",
    )


def offer_donated(offer: dict) -> tuple[str, str]:
    return ("Donation recorded", "PRC recorded your donation. Thank you for saving a life!")


def chat_message(sender_name: str, message: dict) -> tuple[str, str]:
    text = message.get("message") or message.get("text") or "Sent you a message."
    return (sender_name or "New message", _excerpt(text))


def announcement(data: dict) -> tuple[str, str]:
    title = str(data.get("title") or "PRC announcement").strip()
    body = data.get("message") or data.get("content") or data.get("body") or ""
    return (title, _excerpt(body, 160) or "Open Blood-Connect to read the announcement.")
