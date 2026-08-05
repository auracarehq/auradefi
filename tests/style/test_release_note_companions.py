"""A behaviour change pinned in DECISIONS.md must reach CHANGELOG.md too.

MOTIVATING FINDING (0.1.1 wave 2, `CHANGELOG.md:6`, spec-fidelity, major):
`docs/internal/RELEASE_0.1.1.md` §5 wave A says, verbatim, "Update `docs/internal/DECISIONS.md`
in the same change, and note in `CHANGELOG.md` that any 0.1.0 data is not
portable to 0.1.1." DECISIONS.md got its three `(0.1.1)` bullets — the embed
id derivation, the sync loop, the `SyncStatePort` break. CHANGELOG.md got
nothing: the file still ended at `## [0.1.0] — in progress`. The work order's
acceptance list named only DECISIONS.md, so the second half of a two-artefact
clause had no owner, and nothing in the suite noticed.

WHY THE CLASS IS DANGEROUS. DECISIONS.md is read by us; CHANGELOG.md is the
only one of the pair a *host* reads. When a release re-derives a persisted id
(0.1.1 re-derives every embed connection id, and with it every library-ingested
`transaction_id`), a host that never learns of it upgrades in place and its old
rows go silently unaddressable — the exact silent-data-loss shape the release
was cut to remove. The pinned decision existing internally makes that worse,
not better: it proves we knew.

THE RULE, mechanically. Every version marked `(X.Y.Z)` on a DECISIONS.md
pinned-algorithm bullet must own a `## [X.Y.Z]` section in CHANGELOG.md; and
when such a bullet declares data **not portable**, that CHANGELOG section must
say so in words a host can find by searching for "portab". Nothing here reads
source or runs code — it is a text-level companion-artefact check, so it stays
fast and cannot go stale against a refactor.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "internal" / "DECISIONS.md"
CHANGELOG = REPO / "CHANGELOG.md"

# `(0.1.1)` on a bullet line. Three-part only: a two-part `(0.1)` is a
# SemVer series, not a release, and never gets its own CHANGELOG heading.
_VERSION_MARKER = re.compile(r"\((\d+\.\d+\.\d+)\)")
_NOT_PORTABLE = re.compile(r"not\s+(?:\*\*)?portable", re.IGNORECASE)
_PORTABILITY_WORD = re.compile(r"portab", re.IGNORECASE)


def _bullets(decisions_text: str) -> list[str]:
    """DECISIONS.md list items, continuation lines folded into their bullet.

    Folding matters: a wrapped bullet can carry its version marker on one
    line and "not portable" on the next, and a per-LINE scan would then see
    a version that breaks nothing.
    """
    bullets: list[str] = []
    for line in decisions_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            bullets.append(stripped)
        elif bullets and stripped:
            bullets[-1] += " " + stripped
    return bullets


def _pinned_versions(decisions_text: str) -> dict[str, list[str]]:
    """Version → the DECISIONS.md bullets that pin it."""
    pinned: dict[str, list[str]] = {}
    for bullet in _bullets(decisions_text):
        for version in set(_VERSION_MARKER.findall(bullet)):
            pinned.setdefault(version, []).append(bullet)
    return pinned


def _changelog_sections(changelog_text: str) -> dict[str, str]:
    """Version → the body under its `## [X.Y.Z]` heading (may be empty)."""
    headings = list(
        re.finditer(r"^##\s*\[([^\]]+)\]", changelog_text, re.MULTILINE)
    )
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(changelog_text)
        )
        sections[heading.group(1).strip()] = changelog_text[heading.end() : end]
    return sections


def missing_changelog_sections(
    decisions_text: str, changelog_text: str
) -> list[str]:
    """Versions pinned in DECISIONS.md with no CHANGELOG.md section."""
    sections = _changelog_sections(changelog_text)
    return sorted(
        version
        for version in _pinned_versions(decisions_text)
        if version not in sections
    )


def unannounced_portability_breaks(
    decisions_text: str, changelog_text: str
) -> list[str]:
    """Versions whose DECISIONS bullets break portability in silence."""
    sections = _changelog_sections(changelog_text)
    offenders = []
    for version, bullets in _pinned_versions(decisions_text).items():
        if not any(_NOT_PORTABLE.search(bullet) for bullet in bullets):
            continue
        body = sections.get(version)
        if body is None or not _PORTABILITY_WORD.search(body):
            offenders.append(version)
    return sorted(offenders)


def test_every_version_pinned_in_decisions_has_a_changelog_section():
    decisions_text = DECISIONS.read_text(encoding="utf-8")
    changelog_text = CHANGELOG.read_text(encoding="utf-8")

    missing = missing_changelog_sections(decisions_text, changelog_text)

    assert not missing, (
        "docs/internal/DECISIONS.md pins behaviour for version(s) "
        + ", ".join(missing)
        + " with no `## [version]` section in CHANGELOG.md. RELEASE_0.1.1 §5 "
        "and §3.7 require both artefacts to move in the same change: "
        "DECISIONS.md is for us, CHANGELOG.md is the only one a host reads."
    )


def test_a_portability_break_is_announced_to_hosts():
    decisions_text = DECISIONS.read_text(encoding="utf-8")
    changelog_text = CHANGELOG.read_text(encoding="utf-8")

    offenders = unannounced_portability_breaks(decisions_text, changelog_text)

    assert not offenders, (
        "docs/internal/DECISIONS.md declares data NOT PORTABLE for version(s) "
        + ", ".join(offenders)
        + " but that CHANGELOG.md section never mentions portability. A host "
        "upgrading in place has no way to learn its persisted ids re-derive."
    )


# --- the gate, proved against the defect it was written for ---------------
# Reconstructed as scratch strings; source is never edited to test a gate.

_DEFECT_DECISIONS = (
    "- **Embed id derivation** (0.1.1): 0.1.0 embed connection ids are\n"
    "  **not portable** to 0.1.1 (they re-derive on the next connect).\n"
)
_DEFECT_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] — in progress\n"
_FIXED_CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\n## [0.1.1]\n\n"
    "Embed connection ids and the library-ingested transaction ids hashed\n"
    "over them re-derive: 0.1.0 data is not portable to 0.1.1.\n\n"
    "## [0.1.0] — in progress\n"
)


def test_gate_fails_on_the_motivating_defect():
    assert missing_changelog_sections(_DEFECT_DECISIONS, _DEFECT_CHANGELOG) == [
        "0.1.1"
    ]
    assert unannounced_portability_breaks(
        _DEFECT_DECISIONS, _DEFECT_CHANGELOG
    ) == ["0.1.1"]


def test_gate_passes_once_the_companion_artefact_is_written():
    assert missing_changelog_sections(_DEFECT_DECISIONS, _FIXED_CHANGELOG) == []
    assert (
        unannounced_portability_breaks(_DEFECT_DECISIONS, _FIXED_CHANGELOG) == []
    )


def test_gate_does_not_fire_on_a_version_that_broke_nothing():
    quiet = "- **Embed sync loop** (0.1.2): one connection's failure is filed.\n"
    changelog = "# Changelog\n\n## [0.1.2]\n\nSync loop isolation.\n"
    assert missing_changelog_sections(quiet, changelog) == []
    assert unannounced_portability_breaks(quiet, changelog) == []
