"""One rule set for the name a peer supporter is listed under, shared by the bot,
the CLI and the tests.

PURE ON PURPOSE: this module imports only re, unicodedata and typing. config.py
imports it, so importing config from here would be a cycle -- and a rule set that
needs the bot's state to decide whether "Sam" is a name is a rule set nobody can
test. The one roster-aware rule ("somebody else already goes by that") lives in
config.name_taken_by_other, which calls name_key from here.

Why so strict: a listed name is shown to a student who may be in distress, next to
a number they type to choose. It must not be able to smuggle a link, a phone number,
a username, markup, an invisible character, or something that looks like the bot's
own buttons into that list, and two supporters must never look identical in it.
"""

import re
import unicodedata
from typing import Dict, Iterable, Optional, Tuple

NAME_MAX_LENGTH = 32
# The Telegram first name is captured verbatim-ish, but never stored unbounded: a
# first name is attacker-controlled text from somebody's own profile.
CAPTURED_NAME_MAX_LENGTH = 64
RESET_KEYWORD = "reset"

# The ONLY values name_problem can return. Each is also a MESSAGES key, so the
# supporter always gets a specific, friendly reason. 'name_taken' is NOT here: it is
# roster-aware and lives in config/handlers.
REJECTION_KEYS = (
    "name_multiline",
    "name_invisible_chars",
    "name_empty",
    "name_too_long",
    "name_has_at",
    "name_looks_like_link",
    "name_has_phone",
    "name_bad_chars",
    "name_numeric",
    "name_needs_letter",
    "name_reserved",
)

# Checked BEFORE clean_name collapses whitespace, or a newline would silently become
# a space and "Sam\nCall me on ..." would be half-accepted.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x85  ")
# Control, format (zero-width, bidi overrides, ZWJ), surrogate, private-use and
# unassigned. Any of these can make two names look identical or reorder the line.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
# Hyphen, straight and curly apostrophe, full stop. Nothing that can form markup,
# a link, a mention or our own "(2)" collision suffix.
_ALLOWED_PUNCTUATION = frozenset("-'’.")
# "t.me", "sam.com", "Dr.Who": a letter/digit, a dot, then two or more letters.
# "J. Tan" passes because the space breaks it.
_DOMAIN_RE = re.compile(r"[^\W_]\.[^\W\d_]{2,}")
# Five or more digits, optionally separated by spaces/dots/hyphens/apostrophes.
# "Sup 3000" (four) is a name; "9123 4567" is a phone number.
_PHONE_RE = re.compile(r"\d(?:[\s.\-'’]*\d){4,}")

# Applied BEFORE casefold: uppercase Cyrillic/Greek letters that render like Latin.
_UPPER_CONFUSABLES = str.maketrans({
    # Cyrillic
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})

# Applied AFTER casefold and mark stripping: lowercase look-alikes, plus the two
# digits that pass for letters.
_LOWER_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "һ": "h", "ԛ": "q", "ԝ": "w",
    # Latin
    "ɡ": "g", "ı": "i",
    # Greek
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "ω": "w",
    # Digits
    "0": "o", "1": "l",
})


def clean_name(raw) -> str:
    """NFC, then strip and collapse every run of whitespace to one space.

    Validates NOTHING -- it is the display form of whatever it is given. Callers
    that need a verdict call name_problem.
    """
    text = unicodedata.normalize("NFC", "" if raw is None else str(raw))
    return " ".join(text.split())


def name_key(name) -> str:
    """The comparison key for "is this the same name?". NEVER displayed.

    Deliberately lossy: case, accents, spacing, punctuation, fullwidth and
    mathematical letter forms, common Cyrillic/Greek look-alikes, 0/o, 1/l, rn/m and
    vv/w all collapse, so two supporters cannot be told apart only by something a
    student cannot see.
    """
    text = unicodedata.normalize("NFKC", "" if name is None else str(name))
    text = text.translate(_UPPER_CONFUSABLES)
    text = text.casefold()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = text.translate(_LOWER_CONFUSABLES)
    text = "".join(ch for ch in text if ch.isalnum())
    return text.replace("rn", "m").replace("vv", "w")


def name_problem(raw, reserved_keys=frozenset()) -> Optional[str]:
    """None when `raw` is acceptable as a listed name, else the FIRST problem key.

    The order is part of the contract (tests pin it): the most specific, most
    actionable reason wins, so "Call 91234567" is told about the phone number rather
    than something vaguer. `reserved_keys` are name_key()s of words the bot itself
    uses; config passes RESERVED_NAME_KEYS. Roster collisions are NOT checked here.
    """
    text = "" if raw is None else str(raw)

    # 1-2 run on the RAW text: clean_name would hide a newline or a tab as a space.
    if any(ch in _LINE_BREAKS for ch in text):
        return "name_multiline"
    if any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text):
        return "name_invisible_chars"

    name = clean_name(text)
    if not name:
        return "name_empty"
    if len(name) > NAME_MAX_LENGTH:
        return "name_too_long"

    # NFKC first, so a fullwidth "＠" or "．" cannot slip past the checks below.
    folded = unicodedata.normalize("NFKC", name).casefold()
    if "@" in folded:
        return "name_has_at"
    if "://" in folded or "www." in folded or _DOMAIN_RE.search(folded):
        return "name_looks_like_link"
    if _PHONE_RE.search(folded):
        return "name_has_phone"

    # Whitelist, not blacklist: letters, numbers, spaces, emoji-ish symbols and four
    # punctuation marks. Parentheses are out, which is what makes the collision
    # suffix " (2)" unforgeable. Combining marks are fine (accents, emoji variation
    # selectors) but a stack of more than two is "Zalgo" text, not a name.
    # Walked in NFD: clean_name's NFC would otherwise fold the first mark of a stack
    # into its base letter (S + U+0301 -> U+015A) and let a three-mark stack through
    # as two. NFD only splits base+mark, so every other verdict is unchanged.
    marks_in_a_row = 0
    for ch in unicodedata.normalize("NFD", name):
        category = unicodedata.category(ch)
        if category.startswith("M"):
            marks_in_a_row += 1
            if marks_in_a_row > 2:
                return "name_bad_chars"
            continue
        marks_in_a_row = 0
        if ch == " " or category[0] in ("L", "N") or category in ("So", "Sk"):
            continue
        if ch in _ALLOWED_PUNCTUATION:
            continue
        return "name_bad_chars"

    # People pick from the list by TYPING a number; a name that is a number would
    # be indistinguishable from a choice.
    core = [c for c in name if c not in " -'’."]
    if core and all(unicodedata.category(c) == "Nd" for c in core):
        return "name_numeric"
    if not any(unicodedata.category(c).startswith("L") for c in name):
        return "name_needs_letter"
    if name_key(name) in reserved_keys:
        return "name_reserved"
    return None


def disambiguate(entries: Iterable[Tuple[int, str]]) -> Dict[int, str]:
    """member_id -> the label shown to requesters.

    Names are grouped by name_key. A name nobody else shares is shown as-is. EVERY
    member of a collision group is numbered, " (1)", " (2)", ... in ascending
    member_id order, so the order is stable across renders and nobody is "the real
    Alex". The suffix cannot be forged, because parentheses fail name_problem. No id
    or username is ever shown -- the number is a position in the group, nothing more.
    """
    groups: Dict[str, list] = {}
    for member_id, name in entries:
        groups.setdefault(name_key(name), []).append((int(member_id), name))
    labels: Dict[int, str] = {}
    for members in groups.values():
        if len(members) == 1:
            member_id, name = members[0]
            labels[member_id] = name
            continue
        for n, (member_id, name) in enumerate(sorted(members, key=lambda m: m[0]), start=1):
            labels[member_id] = "%s (%d)" % (name, n)
    return labels
