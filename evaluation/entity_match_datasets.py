"""Seeded synthetic people for entity matching (INT-004, N-7), with the true links known.

`people(n, seed)` returns a CRM table (left), a billing table (right) and the set of true
(customer_id, account_ref) links. The right table is written the way a second system writes it: "Last, First"
names with typos and dropped honorifics, sub-addressed or re-cased e-mails, formatted phones, abbreviated
streets, ZIP+4 and other date formats; some values are missing or changed (a person moved, a new e-mail).
It also holds hard negatives: relatives at the same address with the same surname, and namesakes (Sr./Jr.)
sharing the name, the address and the home phone."""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

FIRST = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "William", "Elizabeth", "David",
         "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Nancy",
         "Matthew", "Lisa", "Anthony", "Betty", "Mark", "Margaret", "Donald", "Sandra", "Steven", "Ashley", "Paul",
         "Kimberly", "Andrew", "Emily", "Joshua", "Donna", "Kenneth", "Michelle", "Kevin", "Carol", "Brian", "Amanda",
         "George", "Melissa", "Timothy", "Deborah", "José", "Zoë", "Chloé", "Renée", "Søren", "Anaïs"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
        "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young",
        "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker",
        "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "O'Brien", "Müller", "Ødegaard", "D'Angelo"]
STREETS = ["Main", "Oak", "Pine", "Maple", "Cedar", "Elm", "Washington", "Lake", "Hill", "Park", "Sunset", "River",
           "Church", "Highland", "Forest", "Meadow", "Spring", "Walnut", "Chestnut", "Willow"]
TYPES = [("Street", "St"), ("Avenue", "Ave"), ("Road", "Rd"), ("Drive", "Dr"), ("Lane", "Ln"), ("Boulevard", "Blvd"),
         ("Court", "Ct")]
DOMAINS = ["example.com", "mail.test", "gmail.com", "corp.example", "inbox.test"]
LEFT_COLUMNS = ["customer_id", "full_name", "email", "phone", "street_address", "postcode", "birth_date", "segment"]
RIGHT_COLUMNS = ["account_ref", "holder_name", "contact_email", "tel", "address_line", "zip", "dob", "plan"]


def _typo(rng: random.Random, s: str) -> str:
    if len(s) < 4:
        return s
    i = rng.randrange(1, len(s) - 1)
    op = rng.choice(["swap", "drop", "double", "replace"])
    if op == "swap":
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "drop":
        return s[:i] + s[i + 1:]
    if op == "double":
        return s[:i] + s[i] + s[i:]
    return s[:i] + rng.choice("aeiourstln") + s[i + 1:]


def _person(rng: random.Random, i: int, *, last: str | None = None, address: tuple[str, str] | None = None
            ) -> dict[str, Any]:
    first, surname = rng.choice(FIRST), last or rng.choice(LAST)
    number, street, (long_type, _short) = rng.randint(1, 9999), rng.choice(STREETS), rng.choice(TYPES)
    street_address, postcode = address or (f"{number} {street} {long_type}", f"{rng.randint(10000, 99999)}")
    local = f"{first}.{surname}".lower().replace("'", "").replace(" ", "")
    return {"first": first, "last": surname, "email": f"{local}{i % 97}@{rng.choice(DOMAINS)}",
            "phone": f"{rng.randint(200, 989)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}",
            "street_address": street_address, "postcode": postcode,
            "birth_date": date(1950, 1, 1) + timedelta(days=rng.randint(0, 20000))}


def _left_row(p: dict[str, Any], cid: str, rng: random.Random) -> list[Any]:
    title = rng.choice(["", "", "", "Dr. ", "Mr. ", "Ms. "])
    return [cid, f"{title}{p['first']} {p['last']}", p["email"],
            f"({p['phone'][:3]}) {p['phone'][3:6]}-{p['phone'][6:]}", p["street_address"], p["postcode"],
            p["birth_date"].isoformat(), rng.choice(["retail", "smb", "enterprise"])]


def _right_row(p: dict[str, Any], ref: str, rng: random.Random) -> list[Any]:
    first, last = p["first"], p["last"]
    if rng.random() < 0.25:
        first = _typo(rng, first)
    if rng.random() < 0.15:
        last = _typo(rng, last)
    name = f"{last.upper()}, {first}" if rng.random() < 0.5 else f"{first} {last}"
    email = p["email"]
    r = rng.random()
    if r < 0.2:
        email = email.upper()
    elif r < 0.35:
        local, domain = email.split("@")
        email = f"{local}+billing@{domain}"
    elif r < 0.45:
        email = None
    elif r < 0.55:
        email = f"{p['first'].lower()}{rng.randint(1, 999)}@{rng.choice(DOMAINS)}"  # a new address
    phone = None if rng.random() < 0.2 else ("+1 " + f"{p['phone'][:3]}.{p['phone'][3:6]}.{p['phone'][6:]}")
    street = p["street_address"]
    for long_type, short in TYPES:
        if rng.random() < 0.6:
            street = street.replace(long_type, short + ".")
    if rng.random() < 0.3:
        street = street.upper()
    zip_code = p["postcode"] + (f"-{rng.randint(1000, 9999)}" if rng.random() < 0.3 else "")
    dob = p["birth_date"]
    dob_text = rng.choice([dob.strftime("%m/%d/%Y"), dob.isoformat(), dob.strftime("%d %b %Y")])
    if rng.random() < 0.1:
        dob_text = None
    return [ref, name, email, phone, street, zip_code, dob_text, rng.choice(["basic", "plus", "pro"])]


def people(n: int = 600, seed: int = 11, *, overlap: float = 0.7, relatives: float = 0.15, namesakes: float = 0.1
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
    """`n` CRM customers; `overlap` of them also have a billing account; the billing table adds as many
    unrelated accounts: `relatives` of them live with a CRM customer under the same surname, `namesakes` also
    share the first name and the home phone (a father and son)."""
    rng = random.Random(seed)
    base = [_person(rng, i) for i in range(n)]
    left = [_left_row(p, f"C{i:05d}", rng) for i, p in enumerate(base)]
    truth: set[tuple[str, str]] = set()
    right: list[list[Any]] = []
    shared = rng.sample(range(n), int(n * overlap))
    for j, i in enumerate(shared):
        ref = f"ACC-{100000 + j}"
        right.append(_right_row(base[i], ref, rng))
        truth.add((f"C{i:05d}", ref))
    extra = n - len(shared)
    for k in range(extra):
        ref = f"ACC-{500000 + k}"
        r = rng.random()
        if r < relatives:  # a relative: same surname and address, different person
            host = base[rng.randrange(n)]
            p = _person(rng, n + k, last=host["last"], address=(host["street_address"], host["postcode"]))
        elif r < relatives + namesakes:  # Sr./Jr.: same name, address and home phone; another birth date and e-mail
            host = base[rng.randrange(n)]
            p = {**_person(rng, n + k, last=host["last"], address=(host["street_address"], host["postcode"])),
                 "first": host["first"], "phone": host["phone"]}
        else:
            p = _person(rng, n + k)
        right.append(_right_row(p, ref, rng))
    rng.shuffle(right)
    return ([dict(zip(LEFT_COLUMNS, r, strict=True)) for r in left],
            [dict(zip(RIGHT_COLUMNS, r, strict=True)) for r in right], truth)


def spec(**over: Any) -> dict[str, Any]:
    """The MatchSpec for these tables (asset names are placeholders a test replaces)."""
    body = {"name": "crm_billing", "left": {"asset": "crm.customers", "key": "customer_id"},
            "right": {"asset": "billing.accounts", "key": "account_ref"},
            "fields": [{"left": "full_name", "right": "holder_name", "type": "name"},
                       {"left": "email", "right": "contact_email", "type": "email"},
                       {"left": "phone", "right": "tel", "type": "phone"},
                       {"left": "street_address", "right": "address_line", "type": "address"},
                       {"left": "postcode", "right": "zip", "type": "postcode"},
                       {"left": "birth_date", "right": "dob", "type": "date"}]}
    body.update(over)
    return body
