"""Optional ANSI styling for human reports; stored evidence stays plain text."""

import os
import sys


def add_color_argument(parser, default="auto"):
    # Python 3.14 colors argparse help independently, before parsing --color.
    # Keep parser messages plain on all supported Python versions.
    if hasattr(parser, "color"):
        parser.color = False
    parser.add_argument("--color", choices=("auto", "always", "never"), default=default,
                        help="terminal colors (default: auto; honors NO_COLOR and TERM=dumb)")


class Terminal:
    COLORS = {"PASS": "32", "FAIL": "31", "ERROR": "31",
              "WARN": "33", "WARNING": "33", "MISSING": "33", "STALE": "33",
              "UNKNOWN": "33", "NOTE": "36", "CACHED": "36", "SKIP": "2",
              "HEADER": "1", "ADDED": "32", "REMOVED": "31", "CHANGED": "33"}

    def __init__(self, mode="auto", stream=None):
        stream = sys.stdout if stream is None else stream
        self.enabled = mode == "always" or (mode == "auto" and stream.isatty()
                                            and not os.environ.get("NO_COLOR")
                                            and os.environ.get("TERM") != "dumb")

    def paint(self, text, kind):
        text = str(text)
        return f"\x1b[{self.COLORS[kind]}m{text}\x1b[0m" if self.enabled else text

    def cell(self, value, width=0):
        # Apply padding outside escape sequences so visible columns stay aligned.
        return self.paint(value, value) + " " * max(0, width - len(value))

    def report(self, text):
        return "\n".join(self._line(line) for line in text.split("\n"))

    def _line(self, line):
        label, separator, message = line.partition(" ")
        if label in {"ERROR", "WARNING", "WARN", "NOTE"}:
            return self.paint(label, label) + separator + message
        if line.startswith(("--- ", "+++ ", "@@ ")):
            return self.paint(line, "NOTE")
        if line.startswith("+"):
            return self.paint(line, "ADDED")
        if line.startswith("-"):
            return self.paint(line, "REMOVED")
        if line.startswith("~ "):
            return self.paint(line, "CHANGED")
        if line.startswith("Result: "):
            return self.paint(line, "PASS" if line.startswith("Result: 0 error(s)") else "FAIL")
        return line
