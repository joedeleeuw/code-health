"""Markdown code spans whose context declares a current repository reference.

Field notes and dated reports are historical observations. Superseded documents,
retired sections and obsolete table rows describe former ownership; their
replacement pointers still name current owners. Repository-qualified spans and
nested repository inventories name their stated repository, not this checkout.
"""

import re
from collections.abc import Iterator
from pathlib import Path

SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+)")
FENCE = re.compile(r"^\s*(```|~~~)")
DATE = r"\d{4}-\d{2}-\d{2}"
ROOT = re.compile(r"(?:~/|/(?:home|Users)/[^/]+/)(?:code|projects)/([^/`\s]+)")
NONCURRENT_SECTION = re.compile(
    r"^(?:retired|removed|obsolete) (?:surfaces|paths|files|records)\b", re.I
)
STATUS = re.compile(r"\|\s*\*?\*?Status\*?\*?\s*\|\s*Superseded by\s+(.+?)\s*\|", re.I)
PROPOSAL = re.compile(
    r"(?im)^.*\bStatus\b.*(?:planning spec|spec for experiment branch)"
)


def declaration_span(before: str, after: str) -> bool:
    prior = re.search(r"\b(?:was|were) kept in\s*$", before, re.I)
    moved = re.match(
        r"\s+(?:is|are|was|were) (?:folded|moved|merged) into\b", after, re.I
    )
    deleted = re.match(
        r"\s+(?:(?:is|are|was|were|has been|have been)\s+)?"
        r"(?:deleted|removed|retired)\b",
        after,
        re.I,
    )
    created = re.search(r"\b(?:Create:|add)\s*$", before, re.I)
    definition = re.match(r"\s+(?:template|file|directory)\b", after, re.I)
    return bool((prior and moved) or deleted or (created and definition))


class MarkdownReferences:
    def __init__(self, text: str, path: str, repository: Path):
        self.text, self.path, self.repository = text, path, repository
        self.noncurrent_depth = None
        self.external_indent = None
        self.classified_table = False
        self.output_table = False
        self.proposed = bool(PROPOSAL.search(text))

    def historical_document(self) -> bool:
        text, path = self.text, self.path
        title = text.partition("\n")[0].casefold()
        if re.match(r"# field notes\b", title):
            return True
        if re.search(rf"(?:^|/)field-notes/{DATE}\.md$", path):
            return True
        archive_note = re.search(r"(?im)^Retirement note:([^\n]*(?:\n[^\n]+)*)", text)
        if archive_note and re.search(
            r"\brecord how .* originally developed\b", archive_note[1], re.S
        ):
            return True
        dated = re.search(rf"(?im)^(?:\*\*)?Date:(?:\*\*)?\s*{DATE}\b", text)
        dated = dated or re.search(DATE, title)
        return bool(dated and re.search(r"\b(?:report|inventory|audit)\b", title))

    def external_span(self, before: str, after: str, paragraph: str) -> bool:
        repository = self.repository
        qualified = re.search(
            r"\b(?:repo(?:sitory)?|project)\s+(?:`([\w][\w.-]*)`|([\w][\w.-]*))\s*$",
            before,
            re.I,
        )
        if qualified:
            name = qualified[1] or qualified[2]
            declared_names = {match[1] for match in ROOT.finditer(self.text)}
            if name != repository.name and (qualified[1] or name in declared_names):
                return True
        declared = ROOT.search(paragraph)
        if declared and declared[1] != repository.name:
            if re.search(r"\bthat project['’]s\s*$", before):
                return True
        if re.search(r"\b(?:corpora|repo famil(?:y|ies))\b", paragraph, re.I):
            return bool(re.search(r"\b(?:has|have|files in one)\s*$", before))
        if re.search(
            r"\b(?:in (?:one|two|three|\d+) families|repo families)\b", after, re.I
        ):
            return bool(re.search(r"\bfiles in one\s*$", before))
        return False

    def paragraph_spans(self, paragraph: str) -> Iterator[str]:
        paragraph = LINK.sub("", paragraph)
        if self.proposed and re.match(r"\d+\. Branch implements:", paragraph):
            return
        for match in SPAN.finditer(paragraph):
            before, after = paragraph[: match.start()], paragraph[match.end() :]
            if self.proposed and re.match(r"\s+should support:\s*$", after):
                continue
            if not declaration_span(before, after) and not self.external_span(
                before, after, paragraph
            ):
                yield match[1]

    def historical(self, line: str) -> bool:
        heading = HEADING.match(line)
        if heading:
            if (
                self.noncurrent_depth is not None
                and len(heading[1]) <= self.noncurrent_depth
            ):
                self.noncurrent_depth = None
            if NONCURRENT_SECTION.match(heading[2]):
                self.noncurrent_depth = len(heading[1])
            self.external_indent = None
        return self.noncurrent_depth is not None

    def external(self, line: str) -> bool:
        indent = len(line) - len(line.lstrip())
        if (
            self.external_indent is not None
            and line.strip()
            and indent <= self.external_indent
        ):
            self.external_indent = None
        declared = ROOT.search(line)
        if declared and re.match(r"\s*[-*]\s+`", line):
            self.external_indent = (
                indent if declared[1] != self.repository.name else None
            )
        return self.external_indent is not None and indent > self.external_indent

    def snapshot_row(self, line: str) -> bool:
        if not line.startswith("|"):
            self.classified_table = False
            self.output_table = False
            return False
        fields = [
            part.strip().casefold() for part in re.split(r"(?<!\\)\|", line.strip("|"))
        ]
        if fields == ["deleted", "replaced by"] and self.proposed:
            self.output_table = True
        if len(fields) == 3 and fields[1:] == ["classification", "evidence"]:
            self.classified_table = True
        return self.output_table or (
            self.classified_table
            and len(fields) == 3
            and fields[1] in {"obsolete", "merged"}
        )

    def paragraphs(self) -> Iterator[str]:
        text = self.text
        fenced = False
        paragraph = []
        for line in [*text.splitlines(), ""]:
            boundary = re.match(r"^(?:\s*[-*]\s|\s*\d+\.\s|\|)", line)
            if not line.strip() or HEADING.match(line) or FENCE.match(line) or boundary:
                if paragraph:
                    yield " ".join(paragraph)
                    paragraph = []
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            if self.historical(line) or self.snapshot_row(line) or self.external(line):
                continue
            if line.strip():
                paragraph.append(line)

    def __iter__(self) -> Iterator[str]:
        text = self.text
        if self.historical_document():
            return
        superseded = STATUS.search(text)
        if superseded:
            yield from SPAN.findall(superseded[1])
            return
        for paragraph in self.paragraphs():
            yield from self.paragraph_spans(paragraph)
