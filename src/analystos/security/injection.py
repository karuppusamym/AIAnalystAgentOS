"""Deterministic screening of untrusted text before it can reach a model (P7-10).

Ported from ``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/injection_defense.py``
(classifier v2, AR-10) with the generic signals of
``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/prompt_risk.py`` and the
two-detector verdict of ``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/ingest_screening.py``
(ADR-0018 §4 PORT; the donor has no licence file, so the P7-13 licence check is open). The pattern tables are the donor's, unchanged; the
bank-specific prompt-risk signals (maker-checker, regulatory holds, sanctions, SAR filing) are not
ported.

Where it runs: ``skills/catalog.has_injection`` / ``screen_text`` (crawled descriptions, MCP tool
descriptions and results, knowledge ingest), ``knowledge/documents.screen`` (uploaded documents) and
``context/compiler.compile_context`` (every knowledge item, immediately before a prompt).

Detection categories: instruction override (including text addressed to the model, a new identity
or a change to what it answers), system-prompt extraction, credential extraction, policy/security
bypass, privilege escalation, data exfiltration, shell commands, encoded payloads (base64, hex, URL
encoding), Unicode evasion (homoglyphs, zero-width and tag characters, soft hyphens), letter-spacing
and leetspeak, and eleven languages. Measured on the donor corpus (37/37 caught), its AR-10 unseen
set (38/40 caught) and 46 benign catalog texts (0 flagged): ``tests/unit/test_security_injection.py``.

It is a pattern matcher and evadable by paraphrase (the two pinned residual misses prove it). The
load-bearing control stays CLAUDE.md rule 3: a model's output is a proposal that cannot execute,
authorize, approve or publish without the deterministic path.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

INJECTION_DEFENSE_VERSION = "injection-defense-v2"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Outcome of screening a piece of retrieved metadata."""

    flagged: bool
    threat_type: str
    confidence: float
    evidence: list[str]
    content_origin: str
    classifier_version: str = INJECTION_DEFENSE_VERSION


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _InjectionPattern:
    threat_type: str
    pattern: re.Pattern[str]
    confidence: float


def _pat(threat_type: str, pattern: str, confidence: float) -> _InjectionPattern:
    return _InjectionPattern(
        threat_type, re.compile(pattern, re.IGNORECASE | re.DOTALL), confidence
    )


# Verbs that set the model's instructions aside, and the nouns that name them.
_OVERRIDE_VERBS = (
    r"ignore|ignoring|disregard|disregarding|forget|forgetting|override|overlook|"
    r"set aside|pay no attention to|stop following|do not (?:follow|obey)|don'?t (?:follow|obey)"
)
_INSTRUCTION_NOUNS = (
    r"instructions?|rules?|messages?|controls?|prompts?|directions?|directives?|"
    r"guidance|guidelines|constraints?"
)

# Instruction override patterns
INSTRUCTION_OVERRIDE_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:" + _OVERRIDE_VERBS + r")\b.{0,60}"
        r"\b(?:previous|prior|preceding|earlier|original|system|developer|all|above|your|its|"
        r"any|these|those)\b.{0,40}\b(?:" + _INSTRUCTION_NOUNS + r")\b",
        0.95,
    ),
    # "Disregard the guidance above", "set aside the rules you were given":
    # the qualifier follows the noun.
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:" + _OVERRIDE_VERBS + r")\b.{0,20}\b(?:" + _INSTRUCTION_NOUNS + r")\b.{0,20}"
        r"(?:\babove\b|\bso far\b|\bbefore this\b|\byou (?:were|have been) (?:given|told)\b|"
        r"\bgiven to you\b)",
        0.95,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:ignore|disregard|forget)\s+(?:everything|anything)\s+"
        r"(?:above|before|previously|so far|you (?:were|have been) told)\b"
        r"|\b(?:ignore|disregard|forget)\s+what you (?:were|have been) told\b",
        0.90,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:rather|instead) (?:than|of) (?:what )?"
        r"(?:you (?:were|have been) told|your (?:instructions|rules)|the system prompt)\b",
        0.85,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:new instructions?|updated instructions?|real instructions?)\b"
        r".{0,30}\b(?:follow|obey|execute|comply)\b",
        0.90,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\bdo not follow\b.{0,30}\b(?:previous|original|system)\b",
        0.90,
    ),
    # A new identity for the model. Narrowed in v2: "starting now ... new"
    # matched "Starting now, the column is populated by the new core system".
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:you are now|from now on|starting now|henceforth)\b.{0,50}"
        r"\b(?:different|new|my|unrestricted)\b.{0,20}"
        r"\b(?:ai|assistant|model|bot|persona|role|character|identity|instructions?|rules?|"
        r"master|owner|purpose)\b",
        0.85,
    ),
    # Changing what the model answers.
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:from now on|starting now|henceforth|going forward)\b.{0,30}"
        r"\b(?:answer|respond|reply)\b"
        r"|\b(?:answer|respond|reply)\s+(?:to\s+)?(?:every|all|any|each)\s+"
        r"(?:questions?|quer(?:y|ies)|requests?|prompts?|messages?)\b",
        0.85,
    ),
    # Text addressed to the model rather than describing data.
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:note|message|instructions?|attention|reminder|rule|task)\s+(?:to|for)\s+"
        r"(?:the\s+)?(?:ai|assistant|model|llm|chatbot|copilot|language model)\b"
        r"|\byour\s+(?:new|real|actual|true|only|first)\s+"
        r"(?:task|job|goal|instructions?|role|purpose|mission|priority)\b"
        r"|\b(?:highest|top|first)[- ]priority\s+(?:instructions?|commands?|rules?)\b"
        r"|\btreat\s+(?:this|the following|these)\b.{0,40}\bas\s+(?:your|an?|the)\s+"
        r"(?:[a-z-]+\s+)?(?:instructions?|commands?|system (?:prompt|message)|rules?)\b",
        0.85,
    ),
    # Chat-template role markers. A bare "System:" label is ordinary catalog
    # text ("System: SAP ECC"), so the colon form needs a directive after it.
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\[(?:system|assistant|developer)\]"
        r"|<\|?(?:im_start|im_end|system|endoftext)\|?>"
        r"|###\s*(?:system|instruction)"
        r"|(?:^|[.;!?]\s+)(?:system|assistant|developer)\s*:\s*"
        r"(?:you|the user|ignore|from now|always|never)\b",
        0.85,
    ),
]

# System-prompt extraction
PROMPT_EXTRACTION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "PROMPT_EXTRACTION",
        r"\b(?:reveal|print|dump|return|expose|repeat|show|describe|output|tell me|"
        r"what (?:is|are))\b.{0,40}"
        r"\b(?:system ?prompt|developer message|hidden (?:instructions?|rules|prompt)|"
        r"initial instructions?|your (?:instructions|prompt|rules))\b",
        0.90,
    ),
]

# Credential extraction patterns. v2: a credential word followed by one of
# its attributes is a column being described ("show password reset date"),
# not a secret being asked for.
_NOT_A_CREDENTIAL_ATTRIBUTE = (
    r"(?!\s*(?:reset|hash(?:es|ed)?|expir\w*|chang\w*|updat\w*|polic\w*|length|dates?|salt|"
    r"strength|flags?|attempts?|rotat\w*|age|history|column|field|creat\w*|count|issued|type)\b)"
)
CREDENTIAL_EXTRACTION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "CREDENTIAL_EXTRACTION",
        r"\b(?:reveal|print|dump|return|expose|output|show|give)\b.{0,50}\b"
        r"(?:api[_ ]?keys?|passwords?|credentials?|access[_ ]?tokens?|secrets?|"
        r"private[_ ]?keys?|connection[_ ]?strings?)\b" + _NOT_A_CREDENTIAL_ATTRIBUTE,
        0.95,
    ),
    _pat(
        "CREDENTIAL_EXTRACTION",
        r"\b(?:extract|leak|exfiltrate)\b.{0,40}\b(?:credentials?|tokens?|keys?|secrets?)\b",
        0.95,
    ),
]

# Policy bypass patterns
POLICY_BYPASS_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "POLICY_BYPASS",
        r"\b(?:bypass|disable|evade|circumvent|turn off|skip|ignore)\b.{0,50}\b"
        r"(?:authorization|policy|policies|security|guardrails?|controls?|"
        r"audit|masking|sandbox|safety)\b",
        0.95,
    ),
    # v2: validation and filtering are ordinary data-quality words ("skip
    # validation for null rows"); only the stronger verbs make them a bypass.
    _pat(
        "POLICY_BYPASS",
        r"\b(?:bypass|disable|evade|circumvent|turn off)\b.{0,50}\b"
        r"(?:validation|filtering|filters?|restrictions?)\b",
        0.90,
    ),
    # v2: "DAN" alone matched a steward named Dan, and "developer mode" alone
    # matched a mobile build flag.
    _pat(
        "POLICY_BYPASS",
        r"\b(?:jailbreak|jailbroken|do anything now|unrestricted mode|god mode|dan mode)\b"
        r"|\byou are (?:now )?dan\b"
        r"|\b(?:enter|enable|activate|switch to|turn on|you are in)\s+developer mode\b",
        0.95,
    ),
    _pat(
        "POLICY_BYPASS",
        r"\b(?:act as|you are|pretend to be|roleplay as|behave as|become)\b.{0,40}"
        r"\b(?:unrestricted|unfiltered|uncensored|jailbroken|"
        r"without (?:any )?(?:rules|restrictions|limits|filters)|"
        r"with no (?:rules|restrictions|limits|filters))\b",
        0.90,
    ),
]

# Privilege escalation patterns
PRIVILEGE_ESCALATION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "PRIVILEGE_ESCALATION",
        r"\b(?:act as|become|you are now|impersonate|switch to|pretend to be|roleplay as)\b"
        r".{0,40}\b"
        r"(?:root|administrator|admin|platform[_ ]?admin|superuser|security[_ ]?officer|sudo)\b",
        0.90,
    ),
    # v2: "grant ... access|role" matched "Grant access to the reporting
    # role". A grant is an escalation when it is to the model itself or of
    # administrator-level rights.
    _pat(
        "PRIVILEGE_ESCALATION",
        r"\b(?:escalate|elevate)\b.{0,30}\b(?:privileges?|permissions?|access|rights|role)\b"
        r"|\bgrant\b.{0,20}\b(?:me|us|yourself|this session|the (?:ai|assistant|model)|"
        r"elevated|admin(?:istrator)?|root|superuser|full|unrestricted)\b.{0,25}"
        r"\b(?:privileges?|permissions?|access|rights|roles?|control)\b",
        0.85,
    ),
]

# Data exfiltration patterns. v2: moving data is what catalog text describes
# ("Export all rows to the warehouse nightly", "Email the report to finance@").
# It is exfiltration when the data goes somewhere outside, or when it is the
# whole database.
DATA_EXFILTRATION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:send|post|transmit|upload|forward|email|exfiltrate|leak)\b.{0,50}"
        r"\b(?:data|records?|rows?|results?|tables?|credentials?|passwords?|keys?|tokens?|"
        r"everything)\b.{0,30}\b(?:to|at)\b.{0,30}"
        r"(?:https?://|ftp://|[a-z0-9._%+-]+@[a-z0-9.-]+)",
        0.90,
    ),
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:dump|export|extract|copy)\b.{0,30}\b(?:all|every|entire|complete|whole)\b.{0,40}"
        r"\b(?:data|records?|rows?|tables?|database)\b.{0,40}"
        r"\b(?:external|outside|remote|third[- ]party|personal|attacker|pastebin)\b"
        r"|\b(?:dump|export|extract)\b.{0,20}\b(?:the )?(?:entire|whole|complete)\s+"
        r"(?:database|schema|warehouse)\b",
        0.85,
    ),
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:curl|wget|fetch|requests?\.(?:get|post))\b.{0,60}\bhttps?://",
        0.90,
    ),
]

# System command injection patterns. v2: "system" alone matched "Run by the
# billing system nightly".
SYSTEM_COMMAND_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "SYSTEM_COMMAND",
        r"\b(?:execute|run|eval|exec)\b.{0,30}\b(?:shell|bash|powershell|cmd\.exe|os\.system|"
        r"subprocess|(?:system|shell|terminal|os) commands?)\b",
        0.90,
    ),
    _pat(
        "SYSTEM_COMMAND",
        r"(?:;\s*(?:rm|cat|ls|chmod|chown|wget|curl)\s)",
        0.90,
    ),
]

# Multi-language injection patterns
MULTILINGUAL_PATTERNS: list[_InjectionPattern] = [
    # Chinese: "ignore previous instructions", verb first and object first
    _pat("MULTILINGUAL_INJECTION", r"(?:忽略|无视|不理).{0,20}(?:指令|规则|指示|说明)", 0.85),
    _pat(
        "MULTILINGUAL_INJECTION",
        r"(?:指令|规则|指示|说明|提示).{0,20}(?:忽略|无视|不理|忘记)",
        0.85,
    ),
    # Japanese: object first (the usual order) and verb first
    _pat(
        "MULTILINGUAL_INJECTION",
        r"(?:指示|命令|ルール|規則|指令).{0,20}(?:無視|忘れ)|(?:無視|忘れ).{0,20}(?:指示|命令|ルール|規則)",
        0.85,
    ),
    # Korean: "ignore the instructions"
    _pat("MULTILINGUAL_INJECTION", r"(?:지시|명령|규칙|지침).{0,20}(?:무시|잊어)", 0.85),
    # Arabic: "ignore instructions"
    _pat("MULTILINGUAL_INJECTION", r"(?:تجاهل|أهمل).{0,20}(?:التعليمات|الأوامر)", 0.85),
    # Russian/Cyrillic: "ignore instructions"
    _pat(
        "MULTILINGUAL_INJECTION",
        r"(?:игнорируй|проигнорируй).{0,20}(?:инструкции|правила|указания)",
        0.85,
    ),
    # Spanish
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:ignora|olvida|descarta)\b.{0,30}"
        r"\b(?:instrucciones|reglas|indicaciones)\b.{0,20}"
        r"\b(?:anteriores|previas|del sistema)\b",
        0.85,
    ),
    # French
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:ignore|oublie)\b.{0,30}"
        r"\b(?:instructions|r[eè]gles|consignes)\b.{0,20}"
        r"\b(?:pr[eé]c[eé]dentes|du syst[eè]me)\b",
        0.85,
    ),
    # German
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:ignorier(?:e|en|t)?|vergiss|vergessen sie|missachte|missachten sie)\b.{0,40}"
        r"\b(?:anweisungen|anweisung|regeln|vorgaben|instruktionen|befehle)\b",
        0.85,
    ),
    # Italian
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:ignora|ignorate|dimentica|dimenticate)\b.{0,30}"
        r"\b(?:istruzioni|regole|indicazioni)\b",
        0.85,
    ),
    # Portuguese
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:ignore|ignora|esqueça|esqueca|desconsidere)\b.{0,30}"
        r"\b(?:instruções|instrucoes|regras|orientações|orientacoes)\b",
        0.85,
    ),
    # Dutch
    _pat(
        "MULTILINGUAL_INJECTION",
        r"\b(?:negeer|vergeet)\b.{0,30}\b(?:instructies|regels|aanwijzingen)\b",
        0.85,
    ),
]

ALL_PATTERNS: list[_InjectionPattern] = (
    INSTRUCTION_OVERRIDE_PATTERNS
    + PROMPT_EXTRACTION_PATTERNS
    + CREDENTIAL_EXTRACTION_PATTERNS
    + POLICY_BYPASS_PATTERNS
    + PRIVILEGE_ESCALATION_PATTERNS
    + DATA_EXFILTRATION_PATTERNS
    + SYSTEM_COMMAND_PATTERNS
    + MULTILINGUAL_PATTERNS
)

# The override phrase with every separator removed, for text whose letters were
# spaced apart one gap at a time ("i g n o r e a l l p r e v i o u s ..."),
# which leaves no word boundary for the patterns above to use.
_SQUASHED_SIGNATURES = re.compile(
    r"(?:ignore|disregard|forget)(?:all|any|every)?(?:the)?"
    r"(?:previous|prior|above|earlier|your|system)(?:instructions?|rules|prompts?|directions|guidance)"
    r"|(?:reveal|print|show|repeat)(?:me)?(?:your|the)?(?:systemprompt|hiddeninstructions)"
)


# ---------------------------------------------------------------------------
# Encoding / obfuscation detection
# ---------------------------------------------------------------------------

_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")
_HEX_RE = re.compile(r"(?:(?:0x|\\x)[0-9a-fA-F]{2}){4,}")
_URL_ENCODED_RE = re.compile(r"(?:%[0-9a-fA-F]{2}){4,}")

# Characters that render as nothing and can split a word a pattern would
# otherwise match. v2 adds the soft hyphen, the combining grapheme joiner, the
# Mongolian vowel separator and the Hangul fillers.
_ZERO_WIDTH_CHARS = frozenset(
    "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff"
    "\u00ad\u034f\u180e\u115f\u1160\u3164"
)

# Common homoglyph mappings (Cyrillic/Greek -> Latin)
_HOMOGLYPH_MAP: dict[str, str] = {
    "\u0410": "A", "\u0412": "B", "\u0421": "C", "\u0415": "E",
    "\u041d": "H", "\u041a": "K", "\u041c": "M", "\u041e": "O",
    "\u0420": "P", "\u0422": "T", "\u0425": "X", "\u0430": "a",
    "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0443": "y", "\u0445": "x", "\u0391": "A", "\u0392": "B",
    "\u0395": "E", "\u0397": "H", "\u0399": "I", "\u039a": "K",
    "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P",
    "\u03a4": "T", "\u03a7": "X", "\u03b1": "a", "\u03b5": "e",
    "\u03bf": "o", "\u03c1": "p",
    # v2: the Cyrillic letters that pass for Latin i, j, s, h, d, q, w and l,
    # and the Greek ones for i, v, k, x and u.
    "\u0456": "i", "\u0406": "I", "\u0458": "j", "\u0408": "J",
    "\u0455": "s", "\u0405": "S", "\u04bb": "h", "\u04ba": "H",
    "\u0501": "d", "\u051b": "q", "\u051d": "w", "\u04cf": "l",
    "\u03b9": "i", "\u03bd": "v", "\u03ba": "k", "\u03c7": "x",
    "\u03c5": "u",
}

# Unicode tag characters U+E0020-U+E007E mirror printable ASCII and render as
# nothing: a whole instruction can ride invisibly behind innocent text.
_TAG_OFFSET = 0xE0000
_PRINTABLE_TAGS = range(0xE0020, 0xE007F)
_ALL_TAGS = range(0xE0000, 0xE0080)
# Emoji subdivision flags are the one legitimate use, and spell at most six
# characters; hidden text longer than this is flagged on its own.
_HIDDEN_TAG_TEXT_FLAG_LENGTH = 8

# Letters an attacker spaced apart ("i g n o r e", "i.g.n.o.r.e").
_SPACED_SEPARATORS = re.compile(r"[ .\-*\u00b7]")
_SPACED_RUN = re.compile(r"(?<!\w)(?:\w[ .\-*\u00b7]){2,}\w(?!\w)")
_LEET = str.maketrans("013457@$", "oieastas")


def _is_variation_selector(ch: str) -> bool:
    code = ord(ch)
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def _strip_invisible(text: str) -> str:
    """Remove zero-width characters and variation selectors."""
    return "".join(
        ch for ch in text if ch not in _ZERO_WIDTH_CHARS and not _is_variation_selector(ch)
    )


def _decode_tag_characters(text: str) -> tuple[str, int]:
    """Replace tag characters with the ASCII they spell; count the hidden ones."""
    decoded: list[str] = []
    hidden = 0
    for ch in text:
        code = ord(ch)
        if code in _PRINTABLE_TAGS:
            decoded.append(chr(code - _TAG_OFFSET))
            hidden += 1
        elif code not in _ALL_TAGS:
            decoded.append(ch)
    return "".join(decoded), hidden


def _normalize_homoglyphs(text: str) -> str:
    """Replace known homoglyphs with their Latin equivalents."""
    return "".join(_HOMOGLYPH_MAP.get(ch, ch) for ch in text)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _base_form(text: str) -> tuple[str, int]:
    """NFKC, hidden tag text decoded, invisible characters removed, homoglyphs
    replaced -- whitespace left as it was, because letter-spacing is read from it."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized, hidden = _decode_tag_characters(normalized)
    normalized = _strip_invisible(normalized)
    return _normalize_homoglyphs(normalized), hidden


def _normalize_text(text: str) -> str:
    """Full normalization pipeline for detection."""
    return _collapse(_base_form(text)[0])


def _despace(text: str) -> str:
    """Join letters spaced apart: 'i g n o r e   a l l' -> 'ignore   all'."""
    return _SPACED_RUN.sub(lambda match: _SPACED_SEPARATORS.sub("", match.group()), text)


def _detect_encoded_payloads(text: str) -> list[str]:
    """Detect potentially malicious encoded content."""
    evidence: list[str] = []

    # Base64 detection
    for match in _BASE64_RE.finditer(text):
        candidate = match.group()
        try:
            decoded = base64.b64decode(candidate, validate=True).decode("utf-8", errors="ignore")
            # Check if the decoded content contains suspicious keywords
            decoded_lower = decoded.lower()
            suspicious_keywords = [
                "ignore", "instructions", "system prompt", "password",
                "credential", "bypass", "admin", "eval", "exec",
            ]
            if any(kw in decoded_lower for kw in suspicious_keywords):
                evidence.append(f"base64_encoded_injection:{candidate[:30]}...")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            # The candidate simply is not base64, which is the common case and not
            # interesting. Narrowed from a bare `except Exception`: in a detector, a wide
            # silent catch means a bug in the decoder reads exactly like "nothing found".
            continue

    # Hex encoding
    if _HEX_RE.search(text):
        evidence.append("hex_encoded_content_detected")

    # URL encoding
    if _URL_ENCODED_RE.search(text):
        evidence.append("url_encoded_content_detected")

    return evidence


def _detect_zero_width_steganography(original_text: str) -> list[str]:
    """Detect zero-width character steganography."""
    evidence: list[str] = []
    zwc_count = sum(1 for ch in original_text if ch in _ZERO_WIDTH_CHARS)
    if zwc_count > 3:
        evidence.append(f"zero_width_chars_detected:count={zwc_count}")
    return evidence


def _detect_homoglyph_evasion(original_text: str) -> list[str]:
    """Detect use of visually-similar Unicode characters to evade filters."""
    evidence: list[str] = []
    homoglyph_count = sum(1 for ch in original_text if ch in _HOMOGLYPH_MAP)
    if homoglyph_count > 2:
        evidence.append(f"homoglyph_evasion_detected:count={homoglyph_count}")
    return evidence


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def screen_metadata(
    text: str,
    content_origin: str = "unknown",
) -> ScreeningResult:
    """Screen a piece of retrieved metadata for indirect prompt injection.

    Parameters
    ----------
    text:
        The metadata text to screen (column description, table comment, etc.).
    content_origin:
        Attribution label for the source (e.g. "column:users.email.description").

    Returns
    -------
    ScreeningResult with flagged=True if injection is detected.
    """
    if not text or not text.strip():
        return ScreeningResult(
            flagged=False,
            threat_type="NONE",
            confidence=0.0,
            evidence=[],
            content_origin=content_origin,
        )

    # Pre-processing: detect evasion techniques on original text
    evasion_evidence: list[str] = []
    evasion_evidence.extend(_detect_zero_width_steganography(text))
    evasion_evidence.extend(_detect_homoglyph_evasion(text))
    evasion_evidence.extend(_detect_encoded_payloads(text))

    base, hidden_tag_count = _base_form(text)
    if hidden_tag_count:
        evasion_evidence.append(f"tag_characters_detected:count={hidden_tag_count}")

    # Patterns run on the normalized text and on two variants of it: letters
    # an attacker spaced apart joined back up, and then leetspeak read as
    # letters. A match found only in a variant is itself evidence of evasion.
    normalized = _collapse(base)
    despaced = _collapse(_despace(base))
    variants: dict[str, str] = {"normalized": normalized}
    if despaced != normalized:
        variants["despaced"] = despaced
    leet = despaced.translate(_LEET)
    if leet not in variants.values():
        variants["leetspeak"] = leet

    # Multilingual patterns run on text without homoglyph replacement
    # (replacing Cyrillic->Latin would break Cyrillic regex).
    normalized_no_homoglyph = _collapse(
        _strip_invisible(_decode_tag_characters(unicodedata.normalize("NFKC", text))[0])
    )

    max_confidence = 0.0
    threat_type = "NONE"
    pattern_evidence: list[str] = []

    for pat in ALL_PATTERNS:
        if pat.threat_type == "MULTILINGUAL_INJECTION":
            matched_in = ["normalized"] if pat.pattern.search(normalized_no_homoglyph) else []
        else:
            matched_in = [name for name, variant in variants.items() if pat.pattern.search(variant)]
        if not matched_in:
            continue
        if pat.confidence > max_confidence:
            max_confidence = pat.confidence
            threat_type = pat.threat_type
        pattern_evidence.append(f"pattern_match:{pat.threat_type}")
        if "normalized" not in matched_in and f"evasion:{matched_in[0]}" not in evasion_evidence:
            evasion_evidence.append(f"evasion:{matched_in[0]}")

    if max_confidence == 0.0 and _SQUASHED_SIGNATURES.search(re.sub(r"[^a-z]", "", leet.lower())):
        max_confidence = 0.85
        threat_type = "INSTRUCTION_OVERRIDE"
        pattern_evidence.append("pattern_match:INSTRUCTION_OVERRIDE")
        evasion_evidence.append("evasion:squashed")

    # Evasion techniques themselves increase confidence
    encoded_injection = any("base64_encoded_injection" in e for e in evasion_evidence)
    if evasion_evidence and max_confidence > 0:
        max_confidence = min(1.0, max_confidence + 0.05)
    elif encoded_injection:
        # Base64-encoded injection content is high-confidence on its own
        max_confidence = 0.90
        threat_type = "INSTRUCTION_OVERRIDE"
    elif hidden_tag_count >= _HIDDEN_TAG_TEXT_FLAG_LENGTH:
        # Text nobody can see has no business in a catalog, whatever it says.
        max_confidence = 0.80
        threat_type = "OBFUSCATION_DETECTED"
    elif evasion_evidence and max_confidence == 0 and len(evasion_evidence) >= 2:
        # Evasion detected without pattern match -- still suspicious
        # but only flag if multiple evasion techniques are used
        max_confidence = 0.70
        threat_type = "OBFUSCATION_DETECTED"

    all_evidence = pattern_evidence + evasion_evidence
    flagged = max_confidence >= 0.70

    return ScreeningResult(
        flagged=flagged,
        threat_type=threat_type if flagged else "NONE",
        confidence=round(max_confidence, 4),
        evidence=all_evidence,
        content_origin=content_origin,
    )


def screen_metadata_batch(
    items: list[tuple[str, str]],
) -> list[ScreeningResult]:
    """Screen multiple metadata items.

    Parameters
    ----------
    items:
        List of (text, content_origin) pairs.

    Returns
    -------
    List of ScreeningResult, one per input item.
    """
    return [screen_metadata(text, origin) for text, origin in items]


# ---------------------------------------------------------------------------
# Prompt-risk signals (generic subset of the donor's prompt_risk.py)
# ---------------------------------------------------------------------------

PROMPT_RISK_CLASSIFIER_VERSION = "deterministic-prompt-risk-v1-generic"


@dataclass(frozen=True, slots=True)
class PromptRiskAssessment:
    decision: Literal["ALLOW", "BLOCK"]
    score: float
    reason_codes: list[str]
    signal_count: int
    classifier_version: str = PROMPT_RISK_CLASSIFIER_VERSION


@dataclass(frozen=True, slots=True)
class _RiskSignal:
    reason_code: str
    pattern: re.Pattern[str]
    weight: float


def _signal(reason_code: str, pattern: str, weight: float) -> _RiskSignal:
    return _RiskSignal(reason_code, re.compile(pattern, re.IGNORECASE), weight)


RISK_SIGNALS = (
    _signal(
        "INSTRUCTION_OVERRIDE_ATTEMPT",
        r"\b(?:ignore|disregard|forget|override)\b.{0,45}\b(?:previous|prior|system|developer|all)\b.{0,30}"
        r"\b(?:instructions?|rules?|messages?|controls?)\b",
        1.0,
    ),
    _signal(
        "SYSTEM_PROMPT_EXTRACTION_ATTEMPT",
        r"\b(?:reveal|print|dump|return|expose|repeat|show me)\b.{0,40}\b"
        r"(?:system prompt|developer message|hidden instructions?|internal prompt)\b",
        1.0,
    ),
    _signal(
        "CREDENTIAL_EXTRACTION_ATTEMPT",
        r"\b(?:reveal|print|dump|return|expose|give me)\b.{0,45}\b"
        r"(?:actual |raw |full )?"
        r"(?:api keys?|passwords?|credentials?|access tokens?|secrets?)\b",
        1.0,
    ),
    _signal(
        "SECURITY_CONTROL_BYPASS_ATTEMPT",
        r"\b(?:bypass|disable|evade|circumvent|turn off|ignore)\b.{0,45}\b"
        r"(?:authorization|policy|policies|security|guardrails?|controls?|audit|"
        r"row[- ]level security)\b",
        1.0,
    ),
    _signal(
        "MASKING_BYPASS_ATTEMPT",
        r"\b(?:unmask|disable masking|without masking|bypass masking|"
        r"raw unredacted|remove redaction)\b",
        1.0,
    ),
    _signal(
        "PRIVILEGE_ESCALATION_ATTEMPT",
        r"\b(?:act as|become|you are now|impersonate|switch to)\b.{0,35}\b"
        r"(?:root|administrator|admin|platform admin|security officer)\b",
        0.9,
    ),
    _signal(
        "UNBOUNDED_DATA_EXTRACTION_ATTEMPT",
        r"\b(?:dump|export|return|extract)\b.{0,30}\b(?:all|every|entire)\b.{0,35}"
        r"\b(?:customer|account|transaction|cardholder|patient)\b.{0,20}\b(?:rows?|records?|values?|data)\b",
        0.9,
    ),
    _signal(
        "SELF_APPROVAL_ATTEMPT",
        r"\b(?:self[- ]approve|approve (?:my|your) own|auto[- ]approve|"
        r"rubber[- ]stamp)\b.{0,40}\b(?:review|request|change|version|deployment|"
        r"submission)\b",
        0.9,
    ),
    _signal(
        "AUDIT_TRAIL_SUPPRESSION_ATTEMPT",
        r"\b(?:don'?t|do not|avoid|suppress|hide|omit|skip)\b.{0,40}\b"
        r"(?:log(?:ging)? (?:this|it)|audit trail|audit log|leaving a record|"
        r"leaving an audit)\b",
        0.9,
    ),
)

_BLOCK_THRESHOLD = 0.8


def assess_prompt_risk(text: str) -> PromptRiskAssessment:
    """Score a user question (short, typed live) against the direct-attack signals. Value-free:
    the result names signals, never the matched text."""
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().lower()
    matched = [s for s in RISK_SIGNALS if s.pattern.search(normalized)]
    score = min(1.0, round(sum(s.weight for s in matched), 4))
    return PromptRiskAssessment(
        decision="BLOCK" if score >= _BLOCK_THRESHOLD else "ALLOW",
        score=score,
        reason_codes=sorted({s.reason_code for s in matched}) or ["NO_PROMPT_RISK_SIGNAL"],
        signal_count=len(matched),
    )


# ---------------------------------------------------------------------------
# One verdict (the donor's ingest_screening.screen_text)
# ---------------------------------------------------------------------------

CLEAN = "CLEAN"
QUARANTINED = "QUARANTINED"
SCREENING_VERSION = f"{PROMPT_RISK_CLASSIFIER_VERSION}+{INJECTION_DEFENSE_VERSION}"


@dataclass(frozen=True, slots=True)
class ScreeningVerdict:
    status: str
    reason_codes: list[str]
    version: str = SCREENING_VERSION

    @property
    def is_clean(self) -> bool:
        return self.status == CLEAN


def screen(text: str | None, content_origin: str = "unknown") -> ScreeningVerdict:
    """Both detectors; quarantined when either flags. Empty text is clean by definition."""
    if not text or not text.strip():
        return ScreeningVerdict(status=CLEAN, reason_codes=[])
    reasons: list[str] = []
    risk = assess_prompt_risk(text)
    if risk.decision == "BLOCK":
        reasons.extend(risk.reason_codes)
    metadata = screen_metadata(text, content_origin=content_origin)
    if metadata.flagged:
        reasons.append(f"INJECTION_DEFENSE:{metadata.threat_type}")
    if reasons:
        return ScreeningVerdict(status=QUARANTINED, reason_codes=sorted(set(reasons)))
    return ScreeningVerdict(status=CLEAN, reason_codes=[])


def is_injection(text: str | None) -> bool:
    """True when untrusted text reads like an instruction to a model (either detector flags it)."""
    return not screen(text).is_clean
