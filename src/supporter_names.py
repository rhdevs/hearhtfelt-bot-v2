"""One rule set for the name a peer supporter is listed under, shared by the bot,
the CLI and the tests.

PURE ON PURPOSE: this module imports only re, unicodedata and typing. config.py
imports it, so importing config from here would be a cycle -- and a rule set that
needs the bot's state to decide whether "Sam" is a name is a rule set nobody can
test. The one roster-aware rule ("somebody else already goes by that") lives in
config.name_taken_by_other, which calls names_look_alike from here.

Why so strict: a listed name is shown to a student who may be in distress, next to
a number they type to choose. It must not be able to smuggle a link, a phone number,
a username, markup, an invisible or blank-rendering character (a Hangul filler looks
like a letter to Unicode), or something that looks like the bot's own buttons into
that list, and two supporters must never look identical in it.
"Identical" is judged with two keys, because one cannot do both jobs: name_key is
case-insensitive (so "IVY" is "ivy") and name_skeleton is case-preserving (so the
capital I in "BiII" is the lowercase l in "Bill"). names_look_alike is either.
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

# Characters that render as NOTHING but are not in an invisible category, so the
# category check above misses them. The Hangul fillers (U+115F, U+1160, U+3164,
# U+FFA0) are category Lo -- LETTERS -- and Telegram users set their first name to
# U+3164 precisely because it looks blank; U+3164 and U+FFA0 even NFKC to U+1160,
# which isalnum(), so the look-alike keys kept them and "Sam" + filler stood next
# to "Sam" unnumbered. U+2800 (Braille blank) is a symbol. The rest are
# Default_Ignorable marks: the combining grapheme joiner, the Khmer inherent vowels,
# the Mongolian free variation selectors and every variation selector EXCEPT
# FE0E/FE0F, which choose text-vs-emoji presentation and are how an ordinary "❤️"
# is typed. unicodedata does not expose Default_Ignorable, so the non-C members are
# listed by hand; the C-category ones are already covered above.
_BLANK_CODEPOINTS = frozenset(
    [chr(c) for c in (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C,
                      0x180D, 0x180F, 0x2800, 0x3164, 0xFFA0)]
    + [chr(c) for c in range(0xFE00, 0xFE0E)]
    + [chr(c) for c in range(0xE0100, 0xE01F0)]
)
# Hyphen, straight and curly apostrophe, full stop. Nothing that can form markup,
# a link, a mention or our own "(2)" collision suffix.
_ALLOWED_PUNCTUATION = frozenset("-'’.")
# "t.me", "sam.com", "Dr.Who": a letter/digit, a dot, then two or more letters.
# "J. Tan" passes because the space breaks it.
_DOMAIN_RE = re.compile(r"[^\W_]\.[^\W\d_]{2,}")
# Five or more digits, optionally separated by spaces/dots/hyphens/apostrophes.
# "Sup 3000" (four) is a name; "9123 4567" is a phone number.
_PHONE_RE = re.compile(r"\d(?:[\s.\-'’]*\d){4,}")

# Applied BEFORE casefold: uppercase Cyrillic/Greek/Armenian letters that render
# like Latin. Kept as a plain dict too, because _SKELETON_UPPER is built from it.
_UPPER_CONFUSABLE_MAP = {
    # Cyrillic
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "Ӏ": "I",
    # Armenian
    "Օ": "O", "Ս": "U",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}
_UPPER_CONFUSABLES = str.maketrans(_UPPER_CONFUSABLE_MAP)

# In a sans-serif UI font capital I IS lowercase l. casefold() hides that by turning
# I into i, which is why name_key alone let 'BiII' pass for 'Bill'. So the skeleton
# uses the same uppercase map, except that every capital-I look-alike (Latin, Greek,
# Cyrillic and the Cyrillic palochka) becomes 'l' instead of 'I'.
# The digit 1 passes for BOTH i and l. name_key already reads it as l ("A11" is
# "All"); the skeleton reads it as i, so "B1ll" is caught as "Bill" too. It must be
# mapped here, before _LOWER_CONFUSABLES would turn it into l.
_SKELETON_UPPER = str.maketrans({
    **_UPPER_CONFUSABLE_MAP,
    "I": "l", "Ι": "l", "І": "l", "Ӏ": "l",
    "1": "i",
})

# Applied AFTER casefold and mark stripping: lowercase look-alikes, plus the two
# digits that pass for letters.
_LOWER_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "һ": "h", "ԛ": "q", "ԝ": "w",
    # Armenian
    "օ": "o", "ո": "n", "ս": "u", "հ": "h", "զ": "q",
    # Latin
    "ɡ": "g", "ı": "i", "ɑ": "a", "ɩ": "i", "ǀ": "l",
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


def is_invisible_char(ch) -> bool:
    """True for any character a student could not see: an invisible category
    (control, format, surrogate, private-use, unassigned) or a blank-rendering
    letter, symbol or mark from _BLANK_CODEPOINTS."""
    return ch in _BLANK_CODEPOINTS or unicodedata.category(ch) in _INVISIBLE_CATEGORIES


def strip_invisible(text) -> str:
    """`text` with every is_invisible_char removed. Validates nothing."""
    return "".join(ch for ch in ("" if text is None else str(text))
                   if not is_invisible_char(ch))


def name_key(name) -> str:
    """The comparison key for "is this the same name?". NEVER displayed.

    Deliberately lossy: case, accents, spacing, punctuation, fullwidth and
    mathematical letter forms, common Cyrillic/Greek look-alikes, 0/o, 1/l, rn/m,
    vv/w, and invisible and blank-rendering characters all collapse, so two
    supporters cannot be told apart only by something a student cannot see.
    """
    text = unicodedata.normalize("NFKC", "" if name is None else str(name))
    text = strip_invisible(text)  # AFTER NFKC: U+3164 and U+FFA0 only become U+1160 there.
    text = text.translate(_UPPER_CONFUSABLES)
    text = text.casefold()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = text.translate(_LOWER_CONFUSABLES)
    text = "".join(ch for ch in text if ch.isalnum())
    return text.replace("rn", "m").replace("vv", "w")


def name_skeleton(name) -> str:
    """The CASE-PRESERVING visual key: what the name looks like on screen. NEVER
    displayed.

    Complements name_key, which is case-insensitive and so cannot see that I and l
    are the same glyph. Marks are stripped BEFORE the map, so an accented capital
    ("Ì") becomes I and then l. There is deliberately no casefold: folding case is
    name_key's job, and doing both in one key would make "Ali" read as "all".
    Invisible and blank-rendering characters collapse away here too.
    """
    text = unicodedata.normalize("NFKC", "" if name is None else str(name))
    text = strip_invisible(text)  # AFTER NFKC: U+3164 and U+FFA0 only become U+1160 there.
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = text.translate(_SKELETON_UPPER).translate(_LOWER_CONFUSABLES)
    text = "".join(ch for ch in text if ch.isalnum())
    return text.replace("rn", "m").replace("vv", "w")


def names_look_alike(a, b) -> bool:
    """True iff a student could not reliably tell a from b: same name_key (case,
    accents, spacing, common homoglyphs) OR same name_skeleton (I/l/1 and other
    case-sensitive look-alikes).

    Two keys, because one cannot do both: folding case makes I == i, and a visual
    map makes I == l; merging both into one key would make "Ali" read as the
    reserved word "all", and "Ian" as "Lan".
    """
    return name_key(a) == name_key(b) or name_skeleton(a) == name_skeleton(b)


def reserved_forms(name) -> frozenset:
    """The forms of `name` compared against the bot's reserved words: its name_key
    and its casefolded skeleton. The skeleton form is what catches "CanceI" (capital
    I) for "Cancel"; config builds RESERVED_NAME_KEYS from these same forms."""
    return frozenset(k for k in (name_key(name), name_skeleton(name).casefold()) if k)


def name_problem(raw, reserved_keys=frozenset()) -> Optional[str]:
    """None when `raw` is acceptable as a listed name, else the FIRST problem key.

    The order is part of the contract (tests pin it): the most specific, most
    actionable reason wins, so "Call 91234567" is told about the phone number rather
    than something vaguer. `reserved_keys` are the reserved_forms() of words the bot
    itself uses; config passes RESERVED_NAME_KEYS, built from reserved_forms. Roster
    collisions are NOT checked here.
    """
    text = "" if raw is None else str(raw)

    # 1-2 run on the RAW text: clean_name would hide a newline or a tab as a space.
    if any(ch in _LINE_BREAKS for ch in text):
        return "name_multiline"
    if any(is_invisible_char(ch) for ch in text):
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
    if reserved_forms(name) & reserved_keys:
        return "name_reserved"
    return None


def disambiguate(entries: Iterable[Tuple[int, str]]) -> Dict[int, str]:
    """member_id -> the label shown to requesters.

    Names are grouped into connected components of names_look_alike. A name that
    looks like nobody else's is shown as-is. EVERY member of a look-alike group is
    numbered, " (1)", " (2)", ... in ascending member_id order, so the order is
    stable across renders and nobody is "the real Alex". Grouping is transitive: if
    A looks like B and B like C, all three are numbered together, even when A and C
    alone would not collide -- otherwise two of the three would still look the same.
    The suffix cannot be forged, because parentheses fail name_problem. No id or
    username is ever shown -- the number is a position in the group, nothing more.
    """
    items = [(int(member_id), name) for member_id, name in entries]
    # Union-find over every pair. O(n^2) name comparisons is fine: a roster is tens
    # of people, and a pairwise check is the only way to honour "either key".
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if names_look_alike(items[i][1], items[j][1]):
                parent[find(i)] = find(j)

    groups: Dict[int, list] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    labels: Dict[int, str] = {}
    for members in groups.values():
        if len(members) == 1:
            member_id, name = members[0]
            labels[member_id] = name
            continue
        for n, (member_id, name) in enumerate(sorted(members, key=lambda m: m[0]), start=1):
            labels[member_id] = "%s (%d)" % (name, n)
    return labels
