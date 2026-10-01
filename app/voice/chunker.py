"""Incremental sentence splitter: feeds speakable text to TTS while the LLM is still streaming.

Release rules, in order of preference:
1. A sentence followed by whitespace and more text (the classic boundary).
2. A sentence that ends the buffer ("...calendar." with nothing after it yet). Held only when it could be
   an abbreviation ("e.g.") or a number ("2." of "2.30"), which the idle flush resolves shortly after.
3. `idle_flush()`: called by the pipeline when tokens pause, releases a speakable clause so the listener
   never waits on a long sentence. Cuts at a word boundary; Chirp's streaming input handles clause chunks.
"""

from __future__ import annotations

import re

# Sentence end: . ! ? (optionally followed by quotes/brackets) then whitespace and the next sentence.
_BOUNDARY = re.compile(r'(?<=[.!?])["\')\]]*\s+(?=\S)')
# Sentence end at the very end of the buffer (no whitespace after it yet).
_TERMINAL_END = re.compile(r'[.!?]["\')\]]*\s*$')
_DIGIT_DOT_END = re.compile(r"\d\.\s*$")  # "at 2." could be "2.30"
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|vs|Mr|Mrs|Dr|a\.m|p\.m)\.$", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[,;]\s+|\s[-\u2013\u2014]\s")  # natural pauses inside a sentence
_NUMBER_START = re.compile(r"^[\d:]")  # rest would begin mid-number ("1:30", "30")
_NUMBER_END = re.compile(r"[\d:]$|\b(?:at|or|to|and|from|until|till)$", re.IGNORECASE)  # head would dangle


class SentenceChunker:
    def __init__(self, min_chars: int = 12, min_clause_chars: int = 20):
        self.buffer = ""
        self.min_chars = min_chars
        self.min_clause_chars = min_clause_chars

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self.buffer)
            if not m:
                break
            head = self.buffer[: m.start()].strip()
            if _ABBREV.search(head) or len(head) < self.min_chars:
                # Not a real boundary (abbreviation) or too short to be worth a TTS round-trip: keep accumulating.
                nxt = _BOUNDARY.search(self.buffer, m.end())
                if not nxt:
                    break
                m = nxt
                head = self.buffer[: m.start()].strip()
            out.append(head)
            self.buffer = self.buffer[m.end() :]
        # A finished sentence with nothing after it yet: release now rather than waiting for the next token,
        # unless it might be an abbreviation or a number continuing after the dot.
        tail = self.buffer.strip()
        if (
            len(tail) >= self.min_chars
            and _TERMINAL_END.search(tail)
            and not _ABBREV.search(tail)
            and not _DIGIT_DOT_END.search(tail)
        ):
            out.append(tail)
            self.buffer = ""
        return out

    def idle_flush(self) -> list[str]:
        """Tokens have paused: release a clause if there is a natural pause to cut at.

        Cuts only after a comma/semicolon/dash, or at a word boundary once the text is long, and never right
        before a number or time ("I have 1:30 PM" is spoken in one piece)."""
        tail = self.buffer.strip()
        if len(tail) < self.min_clause_chars:
            return []
        if _TERMINAL_END.search(tail):
            self.buffer = ""
            return [tail]
        m = None
        for cand in _CLAUSE_BREAK.finditer(tail):
            if cand.end() >= self.min_clause_chars:
                m = cand  # keep the last clause break that leaves a speakable head
        cut = m.end() if m else (tail.rfind(" ") if len(tail) >= self.min_clause_chars * 2 else -1)
        if cut < self.min_clause_chars:
            return []
        head, rest = tail[:cut].rstrip(), tail[cut:].lstrip()
        if not rest or _NUMBER_START.match(rest) or _NUMBER_END.search(head):
            return []  # would split "at 1" | ":30 PM" or leave a dangling number: wait for more tokens
        self.buffer = rest
        return [head]

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []
