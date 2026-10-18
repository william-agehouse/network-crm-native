"""NC-270. A command-line tool stops on an option it does not know.

Most import scripts read `sys.argv` by hand and ignored any word they did not
recognise, so `linkedin_import.py --help` went on to fetch and save a real
import. Each such script now names its options once and calls `check()` first
thing in `main()`: `--help` prints the usage and exits, and any other `--word`
it does not list exits with status 2 before anything is read or written.

Scripts built on argparse already behave this way and do not use this.
"""

import sys
from typing import Iterable, Sequence


def check(argv: Sequence[str], usage: str, flags: Iterable[str] = (),
          valued: Iterable[str] = ()) -> None:
    """`flags` stand alone (`--dry-run`); `valued` take the next word (`--db X`).
    Words that do not start with `--` are left to the script, as before."""
    flags, valued = set(flags), set(valued)
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(usage.strip())
        raise SystemExit(0)
    i = 0
    while i < len(args):
        word = args[i]
        if word in valued:
            if i + 1 >= len(args):
                sys.stderr.write(f"{word} needs a value.\n\n{usage.strip()}\n")
                raise SystemExit(2)
            i += 2
            continue
        if word.startswith("--") and word not in flags:
            sys.stderr.write(f"Unknown option {word}. Nothing was run.\n\n{usage.strip()}\n")
            raise SystemExit(2)
        i += 1
