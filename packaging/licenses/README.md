# Licence texts for the Python the app carries

`appify.sh --embed` puts a trimmed copy of a python-build-standalone CPython
inside the app. That Python carries other projects' code (OpenSSL, SQLite and
the rest listed below), and most of their licences ask for their text to travel
with the program when it is handed out built. The archive the build unpacks has
only CPython's and pip's texts, so the others are kept here and the build
appends every `LICENSE.*.txt` in this folder to the app's
`Contents/Resources/python/THIRD-PARTY-LICENSES.txt`.

Where they came from: the `python/licenses/` folder of
`cpython-3.13.14+20260728-aarch64-apple-darwin-pgo+lto-full.tar.zst`, release
`20260728` of github.com/astral-sh/python-build-standalone. That is the same
build as the `install_only` archive the app bundles; both match the release's
published `SHA256SUMS`. Which text belongs to which part of Python was read from
the archive's own `PYTHON.json`.

| Text | Used by |
|---|---|
| bzip2 | `_bz2` |
| expat | `pyexpat`, `_elementtree` |
| libedit | `readline` (the permissive BSD editline, not GNU readline) |
| libffi | `_ctypes` |
| liblzma | `_lzma` |
| libuuid | `_uuid` |
| mpdecimal | `_decimal` |
| ncurses | `_curses`, `readline` |
| openssl-3 | `_ssl`, `_hashlib` (the build has OpenSSL 3.5) |
| sqlite | `_sqlite3` (public domain; kept for completeness) |
| zlib | `zlib` |

Left out on purpose: Tcl, Tix, libX11, libXau and libxcb, which only the window
toolkit (`tkinter`) uses, and the build removes it; OpenSSL 1.1, which this
build does not use; Berkeley DB, which no module in this build links.

When the bundled Python changes, take this folder again from the new build's
full archive.
