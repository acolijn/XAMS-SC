"""Turning the Alarms page's form back into a recipient list. See §4.4, §11.

Parsing the posted rows and describing what changed, so the audit record says
who was added, removed or silenced rather than merely that the file was
written. Neither half is about HTTP.

A blank row is somebody mid-edit far more often than it is a mistake, so this
DROPS empty rows rather than refusing the save — one of them must not hold
the rest of the list hostage.
"""

from __future__ import annotations

def people_from_form(form: dict) -> tuple[list[dict], list[dict]]:
    """Rebuild the recipient list from the posted rows.

    Rows arrive as `name-0`, `email-0`, `phone-0`, `enabled-0`, `remove-0`.
    The index ties the fields of one person together and is otherwise
    meaningless - the saved order is the order of the rows on the page.

    An entirely blank row is dropped rather than refused: the page offers a
    spare row at the bottom for adding somebody, and submitting without using
    it must not be an error.
    """
    indices = sorted({key.split("-", 1)[1] for key in form
                      if key.startswith("name-") and "-" in key},
                     key=lambda i: (len(i), i))
    people, removed = [], []
    for i in indices:
        person = {
            "name": (form.get(f"name-{i}") or "").strip(),
            "email": (form.get(f"email-{i}") or "").strip(),
            # Kept as an empty string when blank, which MEANS "do not SMS
            # this person" - they are notified by email alone. It is not a
            # number somebody forgot to fill in.
            "phone": (form.get(f"phone-{i}") or "").strip(),
            "enabled": bool(form.get(f"enabled-{i}")),
        }
        if not any((person["name"], person["email"], person["phone"])):
            continue
        # Removal is a checkbox applied on save, never a button that deletes
        # on click: this page sits open beside a self-refreshing UI, and a
        # one-click irreversible delete next to that is the wrong affordance.
        if form.get(f"remove-{i}"):
            removed.append(person)
            continue
        people.append(person)
    return people, removed


def recipient_changes(before: list[dict], after: list[dict],
                       removed: list[dict]) -> list[dict]:
    """What changed, as audit lines (§4.4).

    Keyed by name, because that is what a person is called in a conversation
    about who was on the list. It stays recoverable who would have been
    notified when a given alarm fired - which matters most for somebody who
    was REMOVED, since the file no longer mentions them at all.
    """
    def summarise(person):
        bits = [person.get("email") or "no email",
                person.get("phone") or "no phone",
                "enabled" if person.get("enabled") else "disabled"]
        return ", ".join(bits)

    old_by_name = {(p.get("name") or "").casefold(): p for p in before}
    new_by_name = {(p.get("name") or "").casefold(): p for p in after}
    lines = []

    for person in removed:
        lines.append({"target": person.get("name") or "(unnamed)",
                      "old": summarise(person), "new": "removed"})
    for key, person in new_by_name.items():
        old = old_by_name.get(key)
        if old is None:
            lines.append({"target": person.get("name"),
                          "old": None, "new": summarise(person)})
        elif summarise(old) != summarise(person):
            lines.append({"target": person.get("name"),
                          "old": summarise(old), "new": summarise(person)})
    for key, old in old_by_name.items():
        if key not in new_by_name and not any(
                (r.get("name") or "").casefold() == key for r in removed):
            # Vanished without the remove box being ticked - a blanked-out
            # row. Recorded all the same; a person who stops being notified
            # must leave a trace however they left.
            lines.append({"target": old.get("name") or "(unnamed)",
                          "old": summarise(old), "new": "removed"})
    return lines
