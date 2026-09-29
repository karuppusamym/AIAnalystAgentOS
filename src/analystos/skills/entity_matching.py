"""General entity matching (INT-004, N-7): deterministic record linkage between two tables.

Two tables that describe the same real-world entities (customers in a CRM and in billing, say) rarely share
a clean key. This module links their records without a model:

1. **Normalise** each compared value by its declared field type (``id``, ``email``, ``name``, ``address``,
   ``phone``, ``postcode``, ``date``, ``text``): accents, case, punctuation, honorifics, street-type
   abbreviations, e-mail sub-addresses, phone formatting, date formats.
2. **Features.** Every normalised value becomes an exact form, a token set, a padded trigram set and blocking
   keys. The similarities below are set operations on those features, so a keyed hash of every feature
   element compares exactly like the plain feature. A PII-tagged field is hashed with a per-workspace key
   the moment it is read (``Hasher``); its raw value is never kept, stored, logged or sent to a model.
3. **Blocking.** Only record pairs that share a blocking key (an id, an e-mail, the last seven phone digits,
   the Soundex code of a name token, a house number with its street) are compared; an oversized block is
   skipped and counted, and a comparison budget bounds the work.
4. **Scoring.** The weighted mean of the per-field similarities over the fields present on both records.
5. **Thresholds with a review band.** ``score >= match_threshold`` proposes a match, ``review_threshold <=
   score < match_threshold`` proposes a pair for review, below is dropped. A pair compared on too little of
   the spec's weight (``min_match_coverage``) is reviewed however high it scores. With ``one_to_one`` a
   record is linked at most once, strongest score first.

Every result is a *proposal*: `services/entity_matching.py` stores it for a person to accept or reject, and
only accepted pairs become the reviewed crosswalk (join key) analysis may use. ``evaluate`` measures
precision and recall against known truth (the seeded synthetic fixture in ``evaluation/``).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FieldType = Literal["id", "email", "name", "address", "phone", "postcode", "date", "text"]
FIELD_TYPES: tuple[str, ...] = ("id", "email", "name", "address", "phone", "postcode", "date", "text")
DEFAULT_WEIGHTS: dict[str, float] = {"id": 4.0, "email": 3.0, "phone": 2.5, "name": 2.0, "date": 2.0, "address": 1.5,
                                     "postcode": 1.0, "text": 1.0}
MAX_FIELDS = 12
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "jr", "sr", "ii", "iii", "iv", "phd", "md"}
ADDRESS_ABBREVIATIONS = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "court": "ct", "place": "pl", "square": "sq", "terrace": "ter", "highway": "hwy", "parkway": "pkwy",
    "circle": "cir", "apartment": "apt", "suite": "ste", "unit": "unit", "floor": "fl", "building": "bldg",
    "north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw", "number": "no",
}
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y%m%d", "%d-%m-%Y", "%b %d %Y",
                "%d %b %Y", "%B %d %Y", "%d %B %Y")


# ------------------------------------------------------------------------------------ spec
class MatchSide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str = Field(pattern=r"^[^.\s]+\.[^.\s]+$")  # "schema.table"
    key: str  # the record key: carried into the crosswalk, so never a PII column
    source_id: str | None = None


class MatchField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str
    right: str
    type: FieldType
    weight: float | None = Field(default=None, gt=0, le=10)

    @property
    def label(self) -> str:
        return self.left if self.left == self.right else f"{self.left}~{self.right}"

    @property
    def effective_weight(self) -> float:
        return self.weight if self.weight is not None else DEFAULT_WEIGHTS[self.type]


class MatchSpec(BaseModel):
    """What to link and how strictly. The thresholds are on the weighted-mean score in [0, 1]."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    left: MatchSide
    right: MatchSide
    fields: list[MatchField] = Field(min_length=1, max_length=MAX_FIELDS)
    match_threshold: float = Field(default=0.85, gt=0, le=1)
    review_threshold: float = Field(default=0.6, gt=0, le=1)
    # The match band also needs this share of the spec's total weight compared on both records: a pair that
    # agrees only on household fields (name, address, phone) is a Sr./Jr. as often as a match, so a person decides.
    min_match_coverage: float = Field(default=0.6, ge=0, le=1)
    one_to_one: bool = True
    max_rows: int = Field(default=50_000, ge=1, le=200_000)  # per side; a larger table is refused, never sampled
    max_block_size: int = Field(default=500, ge=2, le=10_000)  # a blocking key shared by more records is skipped
    max_comparisons: int = Field(default=2_000_000, ge=1, le=20_000_000)

    @model_validator(mode="after")
    def _check(self) -> MatchSpec:
        if self.review_threshold > self.match_threshold:
            raise ValueError("review_threshold must not exceed match_threshold")
        names = [self.left.key, self.right.key, *[f.left for f in self.fields], *[f.right for f in self.fields]]
        bad = [n for n in names if not _IDENT.match(n)]
        if bad:
            raise ValueError(f"column names must be plain identifiers: {', '.join(bad)}")
        labels = [f.label for f in self.fields]
        if len(set(labels)) != len(labels):
            raise ValueError("a field pair is listed twice")
        return self

    def left_columns(self) -> list[str]:
        return list(dict.fromkeys([self.left.key, *[f.left for f in self.fields]]))

    def right_columns(self) -> list[str]:
        return list(dict.fromkeys([self.right.key, *[f.right for f in self.fields]]))


# ------------------------------------------------------------------------------------ normalisation
TRANSLITERATE = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ß": "ss", "ł": "l",
                               "Ł": "l", "đ": "d", "Đ": "d", "þ": "th", "Þ": "th", "ð": "d", "Ð": "d"})


def fold(value: Any) -> str:
    """Accents removed (letters without a decomposition transliterated), lower case, every run of
    non-alphanumerics one space."""
    s = unicodedata.normalize("NFKD", str(value).translate(TRANSLITERATE))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_name(value: Any) -> str:
    """Honorifics and suffixes dropped, tokens sorted: "Dr. Smith, John" == "john smith"."""
    tokens = [t for t in fold(value).split() if t not in HONORIFICS]
    return " ".join(sorted(tokens))


def norm_email(value: Any) -> str:
    s = str(value).strip().lower()
    if "@" not in s:
        return fold(s).replace(" ", "")
    local, _, domain = s.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def norm_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:]


def norm_id(value: Any) -> str:
    s = fold(value).replace(" ", "")
    return s.lstrip("0") or s


def norm_address(value: Any) -> str:
    return " ".join(ADDRESS_ABBREVIATIONS.get(t, t) for t in fold(value).split())


def norm_postcode(value: Any) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    if re.fullmatch(r"\d{9}", s):  # US ZIP+4
        return s[:5]
    return s


def date_readings(value: Any) -> list[str]:
    """Every ISO date the value can mean, most likely first: 03/04/2020 is both 3 April and 4 March."""
    if isinstance(value, datetime):
        return [value.date().isoformat()]
    if isinstance(value, date):
        return [value.isoformat()]
    s = " ".join(str(value).replace(",", " ").split())
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        s = s[:10]
    out: list[str] = []
    for fmt in DATE_FORMATS:
        try:
            iso = datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
        if iso not in out:
            out.append(iso)
    return out or ([fold(s)] if fold(s) else [])


def norm_date(value: Any) -> str:
    readings = date_readings(value)
    return readings[0] if readings else ""


NORMALISERS = {"id": norm_id, "email": norm_email, "name": norm_name, "address": norm_address, "phone": norm_phone,
               "postcode": norm_postcode, "date": norm_date, "text": fold}


def soundex(token: str) -> str:
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"), "l": "4",
             **dict.fromkeys("mn", "5"), "r": "6"}
    token = re.sub(r"[^a-z]", "", token.lower())
    if not token:
        return ""
    out, last = token[0].upper(), codes.get(token[0], "")
    for ch in token[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def trigrams(s: str) -> set[str]:
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)} if s else set()


# ------------------------------------------------------------------------------------ features
class Hasher:
    """Keyed hash of feature elements for PII fields: equal inputs give equal digests, so every set
    comparison is unchanged, while a stored digest cannot be reversed without the workspace key."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def __call__(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:24]


def workspace_hasher(secret: str, workspace_id: str) -> Hasher:
    """Domain-separated from every other use of the platform secret, and different in every workspace."""
    return Hasher(hmac.new(secret.encode(), f"entity-match:{workspace_id}".encode(), hashlib.sha256).digest())


@dataclass(frozen=True)
class Feature:
    exact: str
    tokens: frozenset[str] = frozenset()
    grams: frozenset[str] = frozenset()
    blocks: tuple[str, ...] = ()


def feature(value: Any, ftype: str, hasher: Hasher | None = None) -> Feature | None:
    """The comparable form of one value (None when empty). With `hasher`, every element is a digest."""
    if value is None:
        return None
    norm = NORMALISERS[ftype](value)
    if not norm:
        return None
    tokens: set[str] = set()
    grams: set[str] = set()
    blocks: list[str] = []
    if ftype == "email":
        local = norm.split("@", 1)[0]
        tokens, grams, blocks = {"local:" + local}, trigrams(local), [norm]
    elif ftype == "phone":
        tokens = {"last7:" + norm[-7:]} if len(norm) >= 7 else set()
        blocks = [norm[-7:]] if len(norm) >= 7 else [norm]
    elif ftype == "date":
        readings = date_readings(value)
        tokens, blocks = set(readings), readings
    elif ftype in ("id", "postcode"):
        blocks = [norm]
    elif ftype == "name":
        parts = norm.split()
        tokens, grams = set(parts), trigrams(norm)
        blocks = sorted({soundex(t) for t in parts if len(t) >= 2} - {""})
    elif ftype == "address":
        parts = norm.split()
        tokens, grams = set(parts), trigrams(norm)
        number = next((t for t in parts if t.isdigit()), None)
        street = next((t for t in parts if t.isalpha() and len(t) > 2), None)
        if number and street:
            blocks = [f"{number}|{street}"]
    else:  # text
        tokens, grams = set(norm.split()), trigrams(norm)
    if hasher is not None:
        return Feature(hasher(norm), frozenset(map(hasher, tokens)), frozenset(map(hasher, grams)),
                       tuple(hasher(b) for b in blocks))
    return Feature(norm, frozenset(tokens), frozenset(grams), tuple(blocks))


def dice(a: frozenset[str], b: frozenset[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(a: Feature, b: Feature, ftype: str) -> float:
    """Per-type similarity in [0, 1] on features only (identical for hashed and plain features)."""
    if a.exact == b.exact:
        return 1.0
    if ftype == "date":  # an ambiguous day/month that can read the same
        return 0.9 if a.tokens & b.tokens else 0.0
    if ftype in ("id", "postcode"):
        return 0.0
    if ftype == "email":
        return 0.7 if a.tokens & b.tokens else 0.0
    if ftype == "phone":
        return 0.8 if a.tokens & b.tokens else 0.0
    if ftype == "name":
        return max(dice(a.grams, b.grams), jaccard(a.tokens, b.tokens))
    return 0.5 * dice(a.grams, b.grams) + 0.5 * jaccard(a.tokens, b.tokens)


@dataclass
class Record:
    key: str
    features: list[Feature | None]


def build_records(rows: list[dict[str, Any]], key: str, columns: list[str], types: list[str],
                  hashed: list[bool], hasher: Hasher | None) -> tuple[list[Record], int]:
    """Records keyed by `key` (rows without a key are dropped and counted). A PII column's values are hashed
    here and the raw value goes out of scope with the row."""
    out: list[Record] = []
    dropped = 0
    for row in rows:
        k = row.get(key)
        if k is None or str(k).strip() == "":
            dropped += 1
            continue
        feats = [feature(row.get(c), t, hasher if h else None) for c, t, h in zip(columns, types, hashed, strict=True)]
        out.append(Record(str(k), feats))
    return out, dropped


# ------------------------------------------------------------------------------------ matching
@dataclass
class MatchPair:
    left_key: str
    right_key: str
    score: float
    band: Literal["match", "review"]
    fields: dict[str, float | None]


@dataclass
class MatchResult:
    pairs: list[MatchPair]
    stats: dict[str, Any] = field(default_factory=dict)


class BlockingTooLoose(ValueError):
    """The candidate pairs exceed the comparison budget: add a sharper field (id, e-mail, phone)."""


def candidate_pairs(left: list[Record], right: list[Record], *, max_block_size: int,
                    max_comparisons: int) -> tuple[set[tuple[int, int]], dict[str, int]]:
    """Pairs sharing at least one blocking key of the same field. A key held by more than `max_block_size`
    records on either side is skipped (it separates nothing); more than `max_comparisons` pairs is refused."""
    lindex: dict[tuple[int, str], list[int]] = defaultdict(list)
    rindex: dict[tuple[int, str], list[int]] = defaultdict(list)
    for index, records in ((lindex, left), (rindex, right)):
        for i, rec in enumerate(records):
            for f, feat in enumerate(rec.features):
                if feat is not None:
                    for b in set(feat.blocks):
                        index[(f, b)].append(i)
    pairs: set[tuple[int, int]] = set()
    skipped = 0
    for bkey, lids in lindex.items():
        rids = rindex.get(bkey)
        if not rids:
            continue
        if len(lids) > max_block_size or len(rids) > max_block_size:
            skipped += 1
            continue
        for li in lids:
            for ri in rids:
                pairs.add((li, ri))
        if len(pairs) > max_comparisons:
            raise BlockingTooLoose(f"more than {max_comparisons} candidate pairs: blocking is too loose; add an id, "
                                   "e-mail or phone field, or raise max_comparisons")
    return pairs, {"blocks": len(lindex), "blocks_skipped": skipped, "candidate_pairs": len(pairs)}


def score_pair(a: Record, b: Record, fields: list[MatchField]) -> tuple[float, float, dict[str, float | None]]:
    """The weighted mean similarity over the fields present on both records, and their share of the weight."""
    total = weight = 0.0
    per: dict[str, float | None] = {}
    for f, (fa, fb) in zip(fields, zip(a.features, b.features, strict=True), strict=True):
        if fa is None or fb is None:
            per[f.label] = None
            continue
        sim = similarity(fa, fb, f.type)
        per[f.label] = round(sim, 4)
        total += f.effective_weight * sim
        weight += f.effective_weight
    full = sum(f.effective_weight for f in fields)
    return (total / weight if weight else 0.0), (weight / full if full else 0.0), per


def match(left: list[Record], right: list[Record], spec: MatchSpec) -> MatchResult:
    """Block, score and band every candidate pair; with `one_to_one`, keep each record's strongest link."""
    cands, stats = candidate_pairs(left, right, max_block_size=spec.max_block_size, max_comparisons=spec.max_comparisons)
    scored: list[tuple[float, int, int, float, dict[str, float | None]]] = []
    for li, ri in cands:
        s, coverage, per = score_pair(left[li], right[ri], spec.fields)
        if s >= spec.review_threshold:
            scored.append((s, li, ri, coverage, per))
    scored.sort(key=lambda x: (-x[0], left[x[1]].key, right[x[2]].key))
    used_l: set[int] = set()
    used_r: set[int] = set()
    pairs: list[MatchPair] = []
    superseded = thin = 0
    for s, li, ri, coverage, per in scored:
        if spec.one_to_one and (li in used_l or ri in used_r):
            superseded += 1
            continue
        used_l.add(li)
        used_r.add(ri)
        band: Literal["match", "review"] = "match" if s >= spec.match_threshold else "review"
        if band == "match" and coverage < spec.min_match_coverage:
            band, thin = "review", thin + 1
        pairs.append(MatchPair(left[li].key, right[ri].key, round(s, 4), band, per))
    stats.update({"left_records": len(left), "right_records": len(right), "above_review": len(scored),
                  "superseded": superseded, "thin_evidence": thin, "match": sum(p.band == "match" for p in pairs),
                  "review": sum(p.band == "review" for p in pairs)})
    return MatchResult(pairs, stats)


def link_records(left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]], spec: MatchSpec, *,
                 pii_left: set[str] | frozenset[str] = frozenset(), pii_right: set[str] | frozenset[str] = frozenset(),
                 hasher: Hasher | None = None) -> MatchResult:
    """Normalise, hash the PII columns, block, score and band (the whole deterministic path)."""
    if (pii_left or pii_right) and hasher is None:
        raise ValueError("PII columns need a hasher")
    types = [f.type for f in spec.fields]
    lcols, rcols = [f.left for f in spec.fields], [f.right for f in spec.fields]
    left, ldrop = build_records(left_rows, spec.left.key, lcols, types, [c in pii_left for c in lcols], hasher)
    right, rdrop = build_records(right_rows, spec.right.key, rcols, types, [c in pii_right for c in rcols], hasher)
    for side, recs in (("left", left), ("right", right)):
        keys = [r.key for r in recs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"the {side} key is not unique; a crosswalk needs one record per key")
    result = match(left, right, spec)
    result.stats.update({"left_rows_without_key": ldrop, "right_rows_without_key": rdrop,
                         "left_version": records_digest(left), "right_version": records_digest(right)})
    return result


def records_digest(records: list[Record]) -> str:
    """A version of one side's compared content (keys + normalised features, PII only as digests): the
    promotion approval binds to it, so a changed input needs a new match run."""
    h = hashlib.sha256()
    for r in sorted(records, key=lambda r: r.key):
        h.update(r.key.encode())
        for f in r.features:
            h.update(b"\x1f" + (f.exact.encode() if f else b"\x00"))
        h.update(b"\x1e")
    return h.hexdigest()


# ------------------------------------------------------------------------------------ measurement
def evaluate(pairs: list[MatchPair], truth: set[tuple[str, str]]) -> dict[str, Any]:
    """Precision and recall of the auto band alone and of auto + review (a reviewer accepting the review band)."""
    def pr(got: set[tuple[str, str]]) -> dict[str, Any]:
        tp = len(got & truth)
        precision = tp / len(got) if got else 1.0
        recall = tp / len(truth) if truth else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"proposed": len(got), "true_positive": tp, "precision": round(precision, 4), "recall": round(recall, 4),
                "f1": round(f1, 4)}

    auto = {(p.left_key, p.right_key) for p in pairs if p.band == "match"}
    both = {(p.left_key, p.right_key) for p in pairs}
    review = both - auto
    return {"truth": len(truth), "match_band": pr(auto), "match_and_review": pr(both),
            "review_band": {"proposed": len(review), "true": len(review & truth)}}
