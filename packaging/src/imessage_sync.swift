// ── networkcrm-imessage-sync ──────────────────────────────────────────────────
// Copies the databases macOS guards into a normal, unprotected directory so the
// Python importers can read them without the CRM app itself ever holding Full
// Disk Access. Four sources (NC-272; this said three after Calendar was added):
//
//   ~/Library/Messages/chat.db                (+ -wal, -shm)  -> …/NetworkCRM/imessage
//   …/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite   -> …/NetworkCRM/whatsapp
//   …/Application Support/AddressBook/AddressBook-v22.abcddb  -> …/NetworkCRM/addressbook
//     …plus Sources/<account>/AddressBook-v22.abcddb, one per account
//   …/Group Containers/group.com.apple.calendar (every calendar) -> …/NetworkCRM/calendar
//
// The third is why `Sweep` exists further down: an address book is NOT at one
// fixed path. The flat file named above is only the "On My Mac" store; every
// iCloud, Google or Exchange account gets its own database under
// Sources/<UUID>/, and on a Mac that syncs contacts that is where nearly all live.
// Copying only the flat file, as an earlier version of this program did, can
// import a single contact out of thousands and still report success.
//
// The name still says "imessage" because the Full Disk Access grant is pinned to
// this binary's identifier and code hash: renaming it would orphan every grant
// already given on every machine that installed the app, to buy nothing but a more
// accurate filename. The name is historical; the job is "the protected message
// databases". Renaming is tracked as debt, not done quietly.
//
// WHY THIS EXISTS AS A SEPARATE BINARY
// chat.db is readable only by a process holding Full Disk Access (FDA). There
// is no narrower TCC permission for it, and a user file-picker does not help:
// macOS's mandatory access control overrides the user's own selection for that
// path. So *something* has to hold FDA. This file exists so that the something
// is as small as humanly possible instead of the whole Python app.
//
// WhatsApp's container is a different TCC category (app data, since Sequoia)
// rather than FDA, but it lands here for the same reason and one better: reading
// it live from Python raised a consent dialog attributed to "python3" every time
// the background loop ran. FDA on this binary covers both, so the app itself
// needs no permission for either and the dialog disappears entirely.
//
// WHY IT MUST BE COMPILED, NOT A SHELL SCRIPT
// TCC keys the grant to the process that performs the read. For a shell script
// that process is the interpreter — /bin/bash — so "grant FDA to my sync
// script" actually means "grant FDA to bash", which every unsandboxed process
// on the machine can then re-use to read anything. A compiled Mach-O binary
// gets its own grant that nothing else inherits. That is the entire point of
// this file being Swift rather than five lines of `cp`.
//
// WHY IT IS THIS SHORT
// The auditability *is* the security argument. You are being asked to give one
// binary read access to your whole disk; you should be able to read every line
// of what it does in a couple of minutes. Accordingly: the only import is
// Foundation, there is no networking of any kind (no URLSession, no sockets, no
// subprocesses), the only arguments accepted are optional destination paths, and
// the only files written are the database copies, the sweep copies, the three
// destination directories and their subdirectories, and one small JSON report
// (see "THE REPORT" at the bottom) — all of them inside
// ~/Library/Application Support/NetworkCRM, which is the CRM's own folder.
// Nothing outside it is written, ever. A sweep also DELETES copies whose source
// has gone, again only inside its own destination directory, because an account
// you removed from Contacts must stop being in the app too.
//
// WAL: both databases run in SQLite WAL mode, so recent messages can live
// entirely in the -wal file and not yet be in the main database. Copying only the
// main file therefore silently loses the newest history. All parts are copied
// together (see copyOne).
//
// Note that ~/Library/Messages cannot be *listed* without FDA even though
// individual files inside it can be stat()ed, so those parts are looked up by
// exact name rather than by globbing the directory. That is still true and still
// why iMessage and WhatsApp name their files.
//
// WHY CONTACTS IS A SWEEP AND NOT ONE FLAT FILE.
// Naming one flat file instead of enumerating AddressBook/Sources/*/ would be the
// narrower choice: no directory listing decides what gets copied, and a Mac that
// keeps its contacts only per-account would simply be reported absent, with the
// app falling back to a .vcf export. It does not work, for two reasons.
//
// Its auditability is kept anyway, by construction: see the end of this note.
//
// First, the failure it causes is silent. On a Mac that syncs
// contacts the flat store EXISTS and can hold a single person; every other contact
// is an account one, living in Sources/<UUID>/AddressBook-v22.abcddb. So nothing is
// ever "reported absent": a flat-file helper reports `current`, the Data page
// shows a healthy Apple Contacts row, and the import brings in one contact out of
// thousands — with no error, no warning and no count anywhere to notice it by.
// Second, the fallback, exporting a .vcf by hand, breaks the app's one product
// rule: nothing is manual unless macOS genuinely forbids the alternative, and
// macOS does not forbid this.
//
// So a Source may now declare a SWEEP, and the two that need one have it. The
// auditability the narrower choice protects is kept by construction rather than
// by refusing to enumerate: a sweep can only ever copy files whose name matches
// ONE literal prefix or suffix, only beneath ONE named subdirectory of the
// source, only to a bounded depth, and never more than a bounded number of files
// or bytes. Read those five fields and you know the complete set of paths this
// binary can touch — which was the property that mattered. Nor is enumerating an
// escalation: this process already holds Full Disk Access, so a directory listing
// tells it nothing about files it could not already open.
//
// What is still refused, deliberately: THE SOURCE LIST IS COMPILED IN. It is not
// read from a config file and it never will be. A manifest would turn the one
// binary on this machine holding Full Disk Access into a confused deputy —
// anything able to write that file could have this helper copy any file on the
// disk into a directory the unprivileged app can read, which is precisely the
// escalation the separate-binary design exists to prevent. That is why adding a
// source still costs a new binary, and a new binary still costs the user one
// re-grant. It is a real cost, paid on purpose.
//
// Exit codes: 0 = at least one source copied, or everything already current.
//             2 = something was blocked by permissions (see the exit-code
//                 contract at the bottom — this wins over a partial success).
//             1 = a real I/O error.
// The same outcome is written as JSON for the app to read; the exit code is for
// whoever launched us, which in normal operation is launchd, six hours ago.

import Foundation

let fm = FileManager.default
let toolName = "networkcrm-imessage-sync"

let supportDir = fm.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/NetworkCRM", isDirectory: true)

/// How a file's name has to look for a sweep to copy it. One literal, no globs,
/// no regex — so the set of copyable paths stays readable off the source.
enum Match {
    case prefix(String)
    case suffix(String)
    // WhatsApp caches each profile picture as BOTH `<jid>-<id>.jpg` and a smaller
    // `.thumb`, and whatsapp_photos.pick_best() takes the .jpg when there is one
    // and the .thumb when there is not. Copying only .jpg would silently drop
    // every face that exists only as a thumbnail — and it is not possible to
    // measure how many that is from here without reading the container, which is
    // the read this whole program exists to keep out of the app. So both.
    case anySuffix([String])

    func accepts(_ name: String) -> Bool {
        switch self {
        case .prefix(let p): return name.hasPrefix(p)
        case .suffix(let s): return name.hasSuffix(s)
        case .anySuffix(let list): return list.contains { name.hasSuffix($0) }
        }
    }

    var described: String {
        switch self {
        case .prefix(let p): return "\(p)*"
        case .suffix(let s): return "*\(s)"
        case .anySuffix(let list): return list.map { "*\($0)" }.joined(separator: " ")
        }
    }
}

/// The bounded directory walk a source may declare when its files are not at a
/// fixed set of paths. Every field is a limit; together they are the complete
/// description of what a sweep can touch, which is the property the auditability
/// argument in the header rests on.
///
/// `maxFiles` and `maxBytes` are refusals, not truncations: hitting either is
/// logged loudly and reported, because a sweep that quietly stopped halfway is
/// the same silent-partial-data failure this whole change exists to fix.
struct Sweep {
    /// Relative to the source dir. "" sweeps the source dir itself.
    let subpath: String
    let match: Match
    /// Directory levels below `subpath` that may be entered. 1 = only the files
    /// directly inside it.
    let maxDepth: Int
    let maxFiles: Int
    let maxBytes: Int64

    var described: String {
        let where_ = subpath.isEmpty ? "" : "\(subpath)/"
        return "\(where_)…/\(match.described) (≤\(maxDepth) deep, ≤\(maxFiles) files)"
    }
}

/// One protected source and where its copy belongs.
///
/// `parts` is ordered: the main file first, then the sidecars that only exist
/// between checkpoints. It may be EMPTY, for a source that is a tree rather than
/// a database — see `main` and step 1 of sync().
struct Source {
    let label: String
    let dir: URL
    let parts: [String]
    /// Optional bounded walk for files that are not at fixed paths.
    let sweep: Sweep?
    let dest: URL
    /// What a missing source most likely means, for the log line.
    let absentHint: String

    /// The file whose presence means "this source exists" and whose readability
    /// is the permission probe. nil for a tree source, where the source dir
    /// itself plays that role — being listable is exactly what it needs.
    var main: String? { return parts.first }
}

let sources: [Source] = [
    Source(
        label: "iMessage",
        dir: fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Messages", isDirectory: true),
        parts: ["chat.db", "chat.db-wal", "chat.db-shm"],
        sweep: nil,
        dest: supportDir.appendingPathComponent("imessage", isDirectory: true),
        absentHint: "you have never used Messages on this Mac"
    ),
    Source(
        label: "WhatsApp",
        dir: fm.homeDirectoryForCurrentUser
            .appendingPathComponent(
                "Library/Group Containers/group.net.whatsapp.WhatsApp.shared",
                isDirectory: true),
        parts: ["ChatStorage.sqlite", "ChatStorage.sqlite-wal", "ChatStorage.sqlite-shm"],
        // The profile-photo cache (NC-047). Not an extra permission — it is the
        // same container this source already reads, so it costs nothing new. It
        // is here so the APP never has to read that container: Media/Profile was
        // the last thing only reachable in place, and while it was, the Data page
        // probed the container on every load and the boot pass probed it once a
        // day. Each probe raises macOS's "would like to access data from other apps"
        // dialog for the Python interpreter. A grant to a bare interpreter does
        // not durably stick, so that dialog would never stop coming back.
        //
        // maxDepth 1: the cache is flat. The caps are refusals, not truncations —
        // half an avatar cache looks exactly like a smaller one.
        sweep: Sweep(subpath: "Media/Profile", match: .anySuffix([".jpg", ".thumb"]),
                     maxDepth: 1, maxFiles: 20_000, maxBytes: 2 * 1024 * 1024 * 1024),
        dest: supportDir.appendingPathComponent("whatsapp", isDirectory: true),
        absentHint: "WhatsApp Desktop is not installed"
    ),
    // Contacts. Its own TCC category, not WhatsApp's app-data one; Full Disk
    // Access covers it (NC-192), and the app cannot read it and must not ask
    // to. Without this, Apple Contacts is the one source that only ever moves
    // when a human exports a .vcf and clicks a button.
    //
    // The -wal matters more here than anywhere else. An address book's -wal is
    // often several times the size of its main file, and holds changes written
    // minutes earlier. Copying the main file alone would take a snapshot missing
    // most of the address book's recent state.
    //
    // THE SWEEP IS THE WHOLE POINT OF THIS SOURCE. The flat file above is only
    // the "On My Mac" store. Every account — iCloud, Google, Exchange — gets its
    // own store under Sources/<UUID>/, and on a Mac that syncs contacts that is
    // where all of them are. On such a Mac the flat store can hold a single
    // person and the real address book thousands. maxDepth 1 because the layout is exactly
    // Sources/<UUID>/AddressBook-v22.abcddb and nothing deeper is wanted; the
    // prefix match is what also brings the -wal and -shm along, which matters for
    // the same reason as above.
    Source(
        label: "Contacts",
        dir: fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AddressBook",
                                    isDirectory: true),
        parts: ["AddressBook-v22.abcddb", "AddressBook-v22.abcddb-wal",
                "AddressBook-v22.abcddb-shm"],
        sweep: Sweep(subpath: "Sources", match: .prefix("AddressBook-v22.abcddb"),
                     maxDepth: 1, maxFiles: 300, maxBytes: 4 * 1024 * 1024 * 1024),
        dest: supportDir.appendingPathComponent("addressbook", isDirectory: true),
        absentHint: "you keep no contacts on this Mac at all"
    ),
    // Calendar (NC-042). A check that finds no `~/Library/Calendars` and
    // concludes "there is no calendar on this Mac" is right about the path and
    // wrong about the machine. That is the classic PRE-VENTURA location; modern
    // macOS keeps Calendar's store in this group container. A probe that can
    // tell the two apart shows it — a sandboxed listdir reports a TCC-protected
    // path as PermissionError and a genuinely missing one as FileNotFoundError,
    // so on a modern macOS it reads as below: Messages and the group container
    // exist but are protected, and the old Calendars folder is simply not there.
    //
    //     ~/Library/Messages                             DENIED  (exists)
    //     ~/Library/Calendars                            ABSENT
    //     ~/Library/Group Containers/…apple.calendar     DENIED  (exists)
    //
    // A probe that only asks "does it exist" gets no, and stops. The
    // lesson is not "measure more", it is that an absence is only evidence about
    // the path you named.
    //
    // parts is EMPTY, deliberately, which makes this a tree source whose
    // permission probe is the directory itself. Naming Calendar.sqlitedb as the
    // main file would be re-running the original mistake one level down: if
    // Apple's filename differs on this OS, a flat `parts` reports the whole
    // source ABSENT while the data sits right there. The sweep finds whatever is
    // actually named, and the log says what it found.
    //
    // Both layouts are matched because the store has moved once already and the
    // point of this source is not to care: `.sqlitedb` (+ -wal/-shm, which matter
    // here for exactly the reason they matter for Contacts — a checkpointed
    // database without its WAL is a snapshot missing its most recent state) and
    // `.ics`, in case any account still keeps flat event files.
    //
    // maxDepth 3 covers <container>/Calendar.sqlitedb and the deeper
    // per-account shapes without walking the whole container. The caps are
    // refusals, not truncations — half a calendar is not a smaller calendar.
    Source(
        label: "Calendar",
        dir: fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.calendar",
                                    isDirectory: true),
        parts: [],
        sweep: Sweep(subpath: "", match: .anySuffix([".sqlitedb", ".sqlitedb-wal",
                                                     ".sqlitedb-shm", ".ics"]),
                     maxDepth: 3, maxFiles: 20_000, maxBytes: 4 * 1024 * 1024 * 1024),
        dest: supportDir.appendingPathComponent("calendar", isDirectory: true),
        absentHint: "you have never used Calendar on this Mac"
    ),
]

// ── output helpers ───────────────────────────────────────────────────────────
// Every line is timestamped because the normal way to read this program's
// output is a log file that launchd appends to on every run — every 120 seconds
// — and it adds no timestamps of its own. Most of those runs are one line
// saying the source has not moved, so the timestamps are how you tell them
// apart.
//
// Line-buffered on purpose. launchd points stdout at a FILE, and stdout to a file
// is block-buffered, so the notes below would sit in a buffer until exit while
// stderr went straight out — putting the summary line ABOVE the details that
// explain it in the very log the timestamps exist to make readable. warn() also
// flushes stdout first, so ordering holds even mid-run.
setvbuf(stdout, nil, _IOLBF, 0)

func stamp() -> String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f.string(from: Date())
}

func note(_ msg: String) {
    print("\(stamp()) \(toolName): \(msg)")
}

// NC-223. launchd runs this every two minutes, 720 times a day, and every run used
// to add a line per source saying nothing had changed, to a log nothing trims. The
// lines that say "nothing happened" now print only for a person watching: run by
// hand in a terminal, with --verbose, or in --self-test. Copies, refusals and
// errors always print, and sync-status.json carries every source's outcome anyway.
let verbose = isatty(STDOUT_FILENO) != 0
    || CommandLine.arguments.contains("--verbose")
    || CommandLine.arguments.contains("--self-test")

func routine(_ msg: String) {
    if verbose { note(msg) }
}

func warn(_ msg: String) {
    fflush(stdout)      // keep the two streams in real order in the launchd log
    FileHandle.standardError.write(Data("\(stamp()) \(toolName): \(msg)\n".utf8))
}

func die(_ msg: String, code: Int32) -> Never {
    warn(msg)
    exit(code)
}

/// Absolute path of this executable — printed in the permission error so the
/// user knows exactly which binary to add to the Full Disk Access list.
func selfPath() -> String {
    let argv0 = CommandLine.arguments.first ?? toolName
    if let resolved = realpath(argv0, nil) {
        defer { free(resolved) }
        return String(cString: resolved)
    }
    return argv0
}

/// True when an error is macOS refusing access rather than a real I/O problem.
/// A TCC denial surfaces as EPERM ("Operation not permitted"), which Foundation
/// wraps as NSFileReadNoPermissionError.
func isPermissionDenied(_ error: Error) -> Bool {
    let ns = error as NSError
    if ns.domain == NSCocoaErrorDomain,
       ns.code == NSFileReadNoPermissionError || ns.code == NSFileWriteNoPermissionError {
        return true
    }
    if ns.domain == NSPOSIXErrorDomain,
       ns.code == Int(EPERM) || ns.code == Int(EACCES) {
        return true
    }
    if let under = ns.userInfo[NSUnderlyingErrorKey] as? NSError,
       under.domain == NSPOSIXErrorDomain,
       under.code == Int(EPERM) || under.code == Int(EACCES) {
        return true
    }
    return false
}

func fdaMessage(_ detail: String) -> String {
    return "\(detail) — this needs macOS Full Disk Access. Open System Settings > "
        + "Privacy & Security > Full Disk Access and grant it to this exact binary: \(selfPath())"
}

// ── arguments ────────────────────────────────────────────────────────────────
// The positional argument still means "the iMessage destination", unchanged, so
// any existing script or muscle memory keeps working. WhatsApp gets a flag
// rather than a second positional, because two bare paths in a row is exactly
// the kind of interface you get the wrong way round at 2am.
let args = Array(CommandLine.arguments.dropFirst())

if args.contains("-h") || args.contains("--help") {
    // Built by looping rather than indexing sources[0] and sources[1] by hand.
    // Adding the Contacts source is exactly the change that would have left a
    // hand-written help text quietly describing two of three.
    let described = sources.map { s in
        let names = s.parts.isEmpty ? "(no fixed files)" : s.parts.joined(separator: ", ")
        let swept = s.sweep.map { "\n    and    \($0.described)" } ?? ""
        return """
          \(names)
            from \(s.dir.path)\(swept)
            to   \(s.dest.path)
        """
    }.joined(separator: "\n\n")
    print("""
    usage: \(toolName) [IMESSAGE_DEST_DIR] [--whatsapp-dest DIR] [--contacts-dest DIR] [--verbose]

    Copies the protected databases to unprotected directories:

    \(described)

    A source that is absent is skipped, not an error — few Macs have all three.
    Files already up to date are left alone. A run where nothing changed prints
    nothing unless it is in a terminal or given --verbose.

    Needs Full Disk Access. Does nothing else: no network, no other files.
    """)
    exit(0)
}

// One flag per source that is not the positional. Kept as a table rather than an
// if-chain so adding a source cannot silently leave a flag out; the `overrides`
// precondition below is the backstop that actually catches it if one is.
var destFlags: [String: URL?] = [
    "--whatsapp-dest": nil, "--contacts-dest": nil, "--calendar-dest": nil,
]
var imessageDest: URL? = nil
// See the "self-test" block near the bottom. Parsed here so the loop below does
// not reject it as unknown; acted on there, because it calls sync() and sync()
// needs the cursor global, which is initialised further down the file.
var selfTestArg: String? = nil
var i = 0
while i < args.count {
    let a = args[i]
    if a == "--self-test" {
        guard i + 1 < args.count else { die("--self-test needs a directory", code: 1) }
        selfTestArg = args[i + 1]
        i += 2
    } else if a == "--verbose" {
        i += 1      // read where `verbose` is defined; accepted here so it is not unknown
    } else if destFlags.keys.contains(a) {
        guard i + 1 < args.count else { die("\(a) needs a directory", code: 1) }
        destFlags[a] = URL(fileURLWithPath: (args[i + 1] as NSString).expandingTildeInPath)
            .standardizedFileURL
        i += 2
    } else if a.hasPrefix("-") {
        die("unknown option '\(a)'. Run with --help.", code: 1)
    } else {
        guard imessageDest == nil else {
            die("expected at most one destination directory, got an extra '\(a)'. "
                + "Use \(destFlags.keys.sorted().joined(separator: ", ")) for the others. "
                + "Run with --help.", code: 1)
        }
        imessageDest = URL(fileURLWithPath: (a as NSString).expandingTildeInPath)
            .standardizedFileURL
        i += 1
    }
}

// Positional by source order, and it MUST stay the same length as `sources` —
// the run loop indexes into it. A short array here would trap on the last source.
let overrides: [URL?] = [
    imessageDest,
    destFlags["--whatsapp-dest"] ?? nil,
    destFlags["--contacts-dest"] ?? nil,
    destFlags["--calendar-dest"] ?? nil,
]
precondition(overrides.count == sources.count,
             "one destination override per source, in source order")

// ── copying ──────────────────────────────────────────────────────────────────
/// size:mtime for each part that exists, used to skip work that is already done.
///
/// Not an optimisation for its own sake: launchd runs this every six hours, and a
/// heavy WhatsApp or Messages history is easily several GB. Re-copying that four
/// times a day for no reason costs real disk writes, and the copy lives beside
/// the original so it doubles the space. Every present part must match, because
/// the main file's mtime does not move while only the WAL is growing.
func signature(_ dir: URL, _ parts: [String]) -> [String: String] {
    var out: [String: String] = [:]
    for name in parts {
        let p = dir.appendingPathComponent(name).path
        guard let a = try? fm.attributesOfItem(atPath: p),
              let size = (a[.size] as? NSNumber)?.int64Value,
              let mod = a[.modificationDate] as? Date else { continue }
        out[name] = "\(size):\(Int(mod.timeIntervalSince1970))"
    }
    return out
}

/// A sweep that could not be walked. Carried out rather than logged in place so
/// sync() decides what it means — a listing refused by TCC is `.blocked`, and a
/// cap being hit must not be mistaken for one.
enum SweepError: Error {
    case denied(String)
    case tooMany(String)
}

/// Every file a sweep matches, as relative-path -> "size:mtime", relative to the
/// SOURCE DIR (not to the subpath) so the keys can share one dictionary with the
/// fixed `parts` above and copy straight into the destination at the same shape.
///
/// stat-only: nothing here opens a file. That is what makes it affordable to run
/// on the 2-minute cadence over an address book with many account stores.
///
/// Throws rather than returning a partial answer. A sweep that silently stopped
/// at the cap would look exactly like an address book with fewer people in it,
/// which is the failure mode this whole change exists to remove.
func sweepSignature(_ dir: URL, _ sweep: Sweep) throws -> [String: String] {
    let root = sweep.subpath.isEmpty ? dir : dir.appendingPathComponent(sweep.subpath,
                                                                       isDirectory: true)
    guard fm.fileExists(atPath: root.path) else { return [:] }

    var out: [String: String] = [:]
    var bytes: Int64 = 0
    // Breadth-first with an explicit depth, so `maxDepth` is enforced by the walk
    // itself and cannot be forgotten at a recursion site.
    var frontier: [(URL, String, Int)] = [(root, sweep.subpath, 0)]
    while let (dirURL, relDir, depth) = frontier.popLast() {
        let children: [URL]
        do {
            children = try fm.contentsOfDirectory(at: dirURL,
                                                  includingPropertiesForKeys: nil,
                                                  options: [.skipsHiddenFiles])
        } catch {
            if isPermissionDenied(error) { throw SweepError.denied(dirURL.path) }
            // A directory that vanished mid-walk (a sync deleting an account) is
            // not an error; there is simply nothing under it any more.
            continue
        }
        for child in children {
            let name = child.lastPathComponent
            let rel = relDir.isEmpty ? name : "\(relDir)/\(name)"
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: child.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                if depth < sweep.maxDepth { frontier.append((child, rel, depth + 1)) }
                continue
            }
            guard sweep.match.accepts(name) else { continue }
            guard let a = try? fm.attributesOfItem(atPath: child.path),
                  let size = (a[.size] as? NSNumber)?.int64Value,
                  let mod = a[.modificationDate] as? Date else { continue }
            out[rel] = "\(size):\(Int(mod.timeIntervalSince1970))"
            bytes += size
            if out.count > sweep.maxFiles {
                throw SweepError.tooMany(
                    "more than \(sweep.maxFiles) files match \(sweep.match.described) "
                    + "under \(root.path)")
            }
            if bytes > sweep.maxBytes {
                throw SweepError.tooMany(
                    "more than \(sweep.maxBytes) bytes match \(sweep.match.described) "
                    + "under \(root.path)")
            }
        }
    }
    return out
}

/// Delete anything under the sweep's destination subtree that the source no
/// longer has. Without this a cancelled meeting or a removed account stays in the
/// app forever, which reads as the app being wrong rather than stale.
///
/// Scoped twice over: only under `dest/subpath`, and only names the sweep itself
/// would have copied. A file the user put there by hand is left alone.
func pruneSweep(_ dest: URL, _ sweep: Sweep, keep: Set<String>) -> Int {
    let root = sweep.subpath.isEmpty ? dest : dest.appendingPathComponent(sweep.subpath,
                                                                         isDirectory: true)
    guard fm.fileExists(atPath: root.path) else { return 0 }
    guard let walker = fm.enumerator(at: root, includingPropertiesForKeys: nil,
                                     options: [.skipsHiddenFiles]) else { return 0 }
    var removed = 0
    for case let url as URL in walker {
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue
        else { continue }
        guard sweep.match.accepts(url.lastPathComponent) else { continue }
        // Rebuild the key the same way sweepSignature did: relative to dest.
        let rel = String(url.path.dropFirst(dest.path.count + 1))
        if !keep.contains(rel), (try? fm.removeItem(at: url)) != nil { removed += 1 }
    }
    return removed
}

/// Copies one file to a temp name inside destDir, then rename(2)s it into
/// place. rename(2) within a single directory is atomic, so a reader (the
/// Python importer) never observes a half-written file — it sees either the
/// previous copy or the new one.
///
/// Returns the byte count copied, or nil if the source does not exist. In the
/// nil case any previous copy is deleted: leaving a stale -wal next to a fresh
/// database would be worse than having no sidecar at all.
///
/// `name` may contain slashes — a sweep passes a path relative to the source dir,
/// like "Sources/<UUID>/AddressBook-v22.abcddb". The temp file is therefore made
/// in the DESTINATION'S OWN PARENT directory rather than at destDir, both so the
/// name stays a legal filename and so rename(2) stays within one directory, which
/// is the only reason it is atomic.
func copyOne(_ name: String, from srcDir: URL, to destDir: URL) throws -> Int64? {
    let src = srcDir.appendingPathComponent(name)
    let dst = destDir.appendingPathComponent(name)

    if !fm.fileExists(atPath: src.path) {
        if fm.fileExists(atPath: dst.path) {
            try fm.removeItem(at: dst)
        }
        return nil
    }

    let parent = dst.deletingLastPathComponent()
    if parent.path != destDir.path {
        try fm.createDirectory(at: parent, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
    }
    let tmp = parent.appendingPathComponent(".\(dst.lastPathComponent).tmp-\(getpid())")
    if fm.fileExists(atPath: tmp.path) {
        try fm.removeItem(at: tmp)      // leftover from a killed earlier run
    }

    do {
        // clonefile(2) first. Source and destination are both under the user's
        // home directory, so on APFS this is copy-on-write: the new file shares
        // the original's blocks and NOTHING is written. In outline, for any
        // database of real size:
        //
        //     FileManager.copyItem   the whole file read and written again
        //     clonefile(2)           a few KB of metadata, whatever the size
        //
        // That is the difference between "this can run every six hours" and "this
        // can run every two minutes": a real copy of both databases rewrites them
        // in full on every run, which at a 2-minute interval adds up to a great deal
        // of SSD wear a day; the clone writes almost nothing. The clone is
        // byte-identical to a real copy and opens as the same SQLite database.
        //
        // Falls back rather than failing, because clonefile is refused for real
        // reasons that are none of our business: EXDEV if the destination is ever
        // moved to another volume, ENOTSUP on HFS+ or a network mount. errno is
        // not inspected — any failure means "do it the slow, always-correct way".
        if clonefile(src.path, tmp.path, 0) != 0 {
            try fm.copyItem(at: src, to: tmp)
        }
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tmp.path)
        let size = (try fm.attributesOfItem(atPath: tmp.path)[.size] as? NSNumber)?.int64Value ?? 0
        guard rename(tmp.path, dst.path) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno), userInfo: [
                NSLocalizedDescriptionKey: "rename to \(dst.path) failed: \(String(cString: strerror(errno)))"
            ])
        }
        return size
    } catch {
        try? fm.removeItem(at: tmp)     // never leave a partial file behind
        throw error
    }
}

// ── the cursor: what we last copied FROM ─────────────────────────────────────
// One tiny file mapping each source label to the size:mtime of each part as it
// was AT THE SOURCE when we last copied it. Read once at startup, rewritten
// after every successful copy.
//
// Separate from sync-status.json on purpose: that file is a status document the
// app reads and a human may look at, this is our own bookkeeping. A dotfile so
// nobody mistakes it for something to interpret. Losing it is harmless — the
// next run simply copies once more and writes it again.
// `var` for exactly one reason: --self-test redirects it into its own temp
// directory so a test run neither reads nor overwrites the real cursor. Normal
// operation never reassigns it.
var cursorFile = supportDir.appendingPathComponent(".sync-cursor.json", isDirectory: false)

// NC-042: `cursor` is read and written from the per-source worker threads as
// well as from the main thread, so both accessors take a lock. An abandoned
// worker is blocked BEFORE its write — it never got its read — so this guards a
// race that should not be reachable rather than one that has been seen. That is
// the right side to be wrong on: the failure it would cause is a corrupt JSON
// write in a file whose whole job is telling the app what is true.
let cursorLock = NSLock()

func loadCursor() -> [String: [String: String]] {
    guard let data = try? Data(contentsOf: cursorFile),
          let obj = try? JSONSerialization.jsonObject(with: data),
          let dict = obj as? [String: [String: String]] else { return [:] }
    return dict
}

var cursor = loadCursor()
/// The cursor's bytes as they are on disk, so saveCursor can skip a rewrite that
/// would change nothing (NC-223: it rewrote the same bytes 720 times a day).
var cursorOnDisk: Data? = try? Data(contentsOf: cursorFile)

func saveCursor() {
    cursorLock.lock()
    let snapshot = cursor
    cursorLock.unlock()
    guard let data = try? JSONSerialization.data(withJSONObject: snapshot,
                                                 options: [.sortedKeys]) else { return }
    if data == cursorOnDisk { return }
    try? fm.createDirectory(at: supportDir, withIntermediateDirectories: true)
    if (try? data.write(to: cursorFile, options: .atomic)) != nil { cursorOnDisk = data }
}

enum Outcome {
    /// bytes, and how many files those bytes were. The file count is in the report
    /// because it is the only thing that distinguishes "Contacts synced" from
    /// "Contacts synced one store holding one person" — a failure that is
    /// otherwise invisible.
    case copied(Int64, Int)
    case current
    case absent
    case blocked
}

/// Whole job for one source. Returns rather than exiting, because one source
/// being absent or blocked must never stop the other from syncing — most Macs
/// have Messages or WhatsApp, not both, and a user who grants FDA for iMessage
/// should not have the run fail because they have no WhatsApp.
func sync(_ s: Source, dest: URL) -> Outcome {
    // 1. Is there anything there at all? stat() usually still works without FDA,
    //    so a missing main file normally means the app was never used — but on
    //    some configurations TCC hides it outright, hence the doubled message
    //    later if this turns out to be the only source.
    //
    //    All three sources name a main file, so that is what gets probed. The
    //    `?? s.dir` is not decoration: `main` is `parts.first`, so a source declared
    //    with an empty `parts` (everything found by its sweep instead) falls back to
    //    probing the DIRECTORY, which is the capability such a sweep would need.
    let probeTarget = s.main.map { s.dir.appendingPathComponent($0) } ?? s.dir
    if !fm.fileExists(atPath: probeTarget.path) {
        routine("\(s.label): \(probeTarget.path) not found or hidden by macOS "
                + "(likely: \(s.absentHint)) — skipped")
        return .absent
    }

    // 2. Probe read access before touching anything, so a permission failure is
    //    reported unambiguously against the source rather than being confused
    //    with a write failure in the destination during the copy itself.
    do {
        if s.main == nil {
            _ = try fm.contentsOfDirectory(at: probeTarget,
                                           includingPropertiesForKeys: nil)
        } else {
            let probe = try FileHandle(forReadingFrom: probeTarget)
            try probe.close()
        }
    } catch {
        if isPermissionDenied(error) {
            warn(fdaMessage("\(s.label): cannot read \(probeTarget.path)"))
            return .blocked
        }
        warn("\(s.label): cannot open \(probeTarget.path): \(error.localizedDescription)")
        return .blocked
    }

    // 3. Already current? Compare the source against WHAT WE LAST COPIED FROM,
    //    recorded in the cursor file — not against the destination.
    //
    //    Comparing against the destination is the obvious thing and it is wrong.
    //    Historically it was wrong because the app rewrote the copy after taking
    //    it: importers.fold_wal() ran `PRAGMA wal_checkpoint(TRUNCATE)` on it,
    //    which GREW the main file (mtime restored, size not) and truncated the
    //    -wal. So the copy never matched the original again, every run saw
    //    "everything changed", and every run re-copied the whole database. Two
    //    consecutive runs whose main chat.db was byte-identical, with a little new
    //    -wal between them, would both copy all of it.
    //
    //    fold_wal is gone (imessage_import.snapshot_uri() reads
    //    the -wal instead of folding it, which is why this cadence is affordable),
    //    so nothing rewrites the copy now. Keep the cursor anyway: "has the SOURCE
    //    changed since I last looked" is the question, and only the source's own
    //    signature can answer it.
    //
    //    The question that matters is "has the SOURCE changed since I last looked",
    //    and only the source's own signature can answer it.
    //
    //    A sweep's files join the SAME signature dictionary, keyed by their path
    //    relative to the source dir. So "one file was added under one account"
    //    and "the flat store's -wal grew" are the same kind of change and get the
    //    same answer, and a removal shows up as a missing key rather than as
    //    nothing at all.
    var srcSig = signature(s.dir, s.parts)
    if let sweep = s.sweep {
        do {
            for (rel, sig) in try sweepSignature(s.dir, sweep) { srcSig[rel] = sig }
        } catch SweepError.denied(let path) {
            warn(fdaMessage("\(s.label): cannot list \(path)"))
            return .blocked
        } catch let SweepError.tooMany(detail) {
            // Loud, and NOT a partial copy. Half an address book looks exactly
            // like an address book with fewer people in it, which is the failure
            // this change exists to remove — so refuse and say the limit out loud.
            warn("\(s.label): refusing to copy — \(detail). Nothing was copied for "
                 + "this source; raise the limit in the source's Sweep and rebuild.")
            return .blocked
        } catch {
            warn("\(s.label): cannot walk \(s.dir.path): \(error.localizedDescription)")
            return .blocked
        }
    }
    cursorLock.lock()
    let lastSig = cursor[s.label] ?? [:]
    cursorLock.unlock()
    // The main file must still be there. A user who deletes the copy has to get a
    // new one; the sidecars are optional by nature (a source checkpoint can remove
    // the -shm, and copyOne already handles an absent part) so they are not
    // required here. A tree source has no main file, so the destination directory
    // existing is the equivalent check.
    let destMainExists = s.main.map { fm.fileExists(atPath: dest.appendingPathComponent($0).path) }
        ?? fm.fileExists(atPath: dest.path)
    if !srcSig.isEmpty, srcSig == lastSig, destMainExists {
        routine("\(s.label): source unchanged since the last copy — nothing to do")
        return .current
    }
    if srcSig.isEmpty {
        // Reachable only for a tree source: the directory exists and is listable
        // but holds nothing we want. Saying "absent" here is the honest answer and
        // it stops the run being reported as a permission problem.
        note("\(s.label): \(s.dir.path) is readable but holds no matching files "
             + "(likely: \(s.absentHint)) — skipped")
        return .absent
    }

    // 4. Destination. 0700: the copy contains the full text of every message you
    //    have ever sent or received, so it should not be world-readable even
    //    inside your own home dir.
    do {
        try fm.createDirectory(at: dest, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
    } catch {
        warn("\(s.label): cannot create destination \(dest.path): \(error.localizedDescription)")
        return .blocked
    }

    // 5. Copy. Main file first, then the sidecars. If the app checkpoints in
    //    between, the WAL we end up with can be newer than the database we
    //    copied; SQLite detects that via the WAL header salt and simply ignores
    //    the mismatched WAL, so the worst case is a snapshot missing the last few
    //    minutes, never a corrupt one. The next run picks them up.
    //
    //    ALL parts, together, every time — even the one whose signature says it
    //    did not change. Copying only the changed part looks like the obvious
    //    next optimisation and it would break the snapshot: a fresh -wal beside a
    //    main file from an earlier run is exactly the salt mismatch above, so
    //    SQLite would discard the -wal and the newest messages would go missing
    //    until the next checkpoint. With clonefile the whole set is nearly free
    //    anyway, so there is nothing to buy and a correctness bug to lose.
    var described: [String] = []
    var total: Int64 = 0
    var files = 0
    for name in s.parts {
        do {
            if let bytes = try copyOne(name, from: s.dir, to: dest) {
                described.append("\(name) \(bytes) B")
                total += bytes
                files += 1
            } else {
                described.append("\(name) absent")
            }
        } catch {
            if isPermissionDenied(error) {
                warn(fdaMessage("\(s.label): cannot copy \(s.dir.appendingPathComponent(name).path)"))
                return .blocked
            }
            // A real I/O error is not survivable in a way worth guessing about —
            // a half-synced pair of databases is worse than a loud failure.
            die("\(s.label): failed copying \(name): \(error.localizedDescription)", code: 1)
        }
    }
    // 6. The sweep's files, if this source has a sweep. Copied by relative path,
    //    so the destination mirrors the source's own Sources/<UUID>/ structure —
    //    which is what lets the Python side tell one account's store from
    //    another's without a naming convention of our own invention.
    //
    //    Only the files whose signature CHANGED are recopied here, unlike the
    //    fixed parts above. The salt-mismatch argument does not apply: these are
    //    independent files, not a database and its log, so a stale neighbour
    //    cannot corrupt a fresh one. With account stores that are megabytes
    //    each, that difference is the whole cost of the source.
    if let sweep = s.sweep {
        var swept = 0
        // Fixed parts share this dictionary; step 5 already did those.
        for (rel, sig) in srcSig where !s.parts.contains(rel) {
            if lastSig[rel] == sig,
               fm.fileExists(atPath: dest.appendingPathComponent(rel).path) { continue }
            do {
                if let bytes = try copyOne(rel, from: s.dir, to: dest) {
                    swept += 1
                    files += 1
                    total += bytes
                }
            } catch {
                if isPermissionDenied(error) {
                    warn(fdaMessage("\(s.label): cannot copy "
                                    + "\(s.dir.appendingPathComponent(rel).path)"))
                    return .blocked
                }
                die("\(s.label): failed copying \(rel): \(error.localizedDescription)",
                    code: 1)
            }
        }
        let keep = Set(srcSig.keys.filter { !s.parts.contains($0) })
        let gone = pruneSweep(dest, sweep, keep: keep)
        if swept > 0 || gone > 0 {
            described.append("\(swept) swept, \(gone) removed")
        } else if described.isEmpty {
            described.append("nothing changed")
        }
    }

    // Only now, with every part renamed into place, is it true that we have a
    // copy of THIS source state. Recording it earlier would let a failure halfway
    // through convince the next run that there was nothing to do.
    cursorLock.lock()
    cursor[s.label] = srcSig
    cursorLock.unlock()
    note("\(s.label): copied \(described.joined(separator: ", ")) "
         + "(total \(total) B) -> \(dest.path)")
    return .copied(total, files)
}

// ── self-test ────────────────────────────────────────────────────────────────
// `--self-test DIR` runs the real sync() — probe, signature, sweep, copy, prune —
// against a synthetic source tree, so the sweep machinery can be exercised on a
// build machine instead of being believed. It exists because the failure it
// guards against is invisible: a helper can report success while copying
// one file out of thousands, and no other test could catch it.
//
// SECURITY: DIR must resolve to somewhere under /tmp or /private/tmp, and the
// check is on the REALPATH so a symlink cannot aim it elsewhere. Those are
// world-writable directories, so this mode reveals and copies nothing that the
// caller could not already read and write without this binary — it grants zero
// use of the Full Disk Access grant. Anything else is refused, loudly. Do not
// relax this into a general --source-root: that would turn the one binary holding
// FDA into a file-exfiltration tool for anything able to run it.
if let arg = selfTestArg {
    let raw = URL(fileURLWithPath: (arg as NSString).expandingTildeInPath)
    guard let real = realpath(raw.path, nil) else {
        die("--self-test: \(raw.path) does not exist", code: 1)
    }
    let root = String(cString: real)
    free(real)
    guard root.hasPrefix("/tmp/") || root.hasPrefix("/private/tmp/") else {
        die("--self-test refuses \(root): it must be under /tmp. This mode is for "
            + "exercising the sweep on synthetic data, and confining it to a "
            + "world-writable directory is what makes it grant nothing.", code: 1)
    }
    // Every arm of Match, checked before anything touches the disk. The sweep
    // below can only exercise one of them, and an enum case with no exercise is
    // how a filename filter ends up matching nothing while the run reports
    // success. Cheap and total, so it runs on every self-test.
    let matchCases: [(Match, String, Bool)] = [
        (.prefix("main.db"),            "main.db-wal",   true),
        (.prefix("main.db"),            "other.db",      false),
        (.suffix(".ics"),               "a/b.ics",       true),
        (.suffix(".ics"),               "b.ics.bak",     false),
        (.anySuffix([".jpg", ".thumb"]), "4670-1.jpg",   true),
        (.anySuffix([".jpg", ".thumb"]), "4670-1.thumb", true),
        (.anySuffix([".jpg", ".thumb"]), "4670-1.png",   false),
        (.anySuffix([]),                 "anything.jpg", false),
    ]
    for (m, name, want) in matchCases where m.accepts(name) != want {
        die("self-test: \(m.described) on \(name) returned \(!want)", code: 1)
    }
    note("self-test: \(matchCases.count) Match cases pass")

    let src = URL(fileURLWithPath: root).appendingPathComponent("src", isDirectory: true)
    let dst = URL(fileURLWithPath: root).appendingPathComponent("dst", isDirectory: true)
    let fake = Source(
        label: "SelfTest", dir: src,
        parts: ["main.db", "main.db-wal"],
        sweep: Sweep(subpath: "Sources", match: .prefix("main.db"),
                     maxDepth: 1, maxFiles: 10, maxBytes: 1024 * 1024),
        dest: dst, absentHint: "the self-test tree was not created")
    // Own cursor, inside the test tree: a test run must not read or clobber the
    // real one, and it needs a persistent cursor of its own or the "unchanged
    // since last time" and prune paths could never be exercised across runs.
    cursorFile = URL(fileURLWithPath: root).appendingPathComponent("cursor.json")
    cursor = loadCursor()
    cursorOnDisk = try? Data(contentsOf: cursorFile)
    let outcome = sync(fake, dest: dst)
    saveCursor()
    note("self-test outcome: \(outcomeName(outcome))")
    // Print the destination tree so the caller can diff it against what it built.
    if let walker = fm.enumerator(at: dst, includingPropertiesForKeys: nil,
                                  options: [.skipsHiddenFiles]) {
        var seen: [String] = []
        for case let u as URL in walker {
            var isDir: ObjCBool = false
            if fm.fileExists(atPath: u.path, isDirectory: &isDir), !isDir.boolValue {
                seen.append(String(u.path.dropFirst(dst.path.count + 1)))
            }
        }
        for p in seen.sorted() { print("COPIED \(p)") }
    }
    exit(0)
}

// ── the log ──────────────────────────────────────────────────────────────────
// NC-223. launchd appends this program's output to imessage-sync.log (the
// installer's StandardOutPath) and nothing ever trimmed it. Once it passes 1 MB,
// keep its last 512 KB, starting at a whole line. The gap between the two sizes
// means a log that keeps growing (a refusal printed every run, say) is rewritten
// once per 512 KB, not on every run. launchd opens the file for appending, so
// what this run prints after the trim still lands at the new end.
let logFile = fm.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Logs/NetworkCRM/imessage-sync.log", isDirectory: false)
let logLimit = 1_048_576
let logKeep = 524_288

func trimLog(_ url: URL) {
    guard let size = (try? fm.attributesOfItem(atPath: url.path))?[.size] as? Int,
          size > logLimit,
          let h = try? FileHandle(forUpdating: url) else { return }
    defer { try? h.close() }
    guard (try? h.seek(toOffset: UInt64(size - logKeep))) != nil,
          var tail = try? h.readToEnd() else { return }
    if let nl = tail.firstIndex(of: 0x0A) { tail = tail.suffix(from: tail.index(after: nl)) }
    let head = Data("\(stamp()) \(toolName): log trimmed to its last \(logKeep / 1024) KB\n".utf8)
    guard (try? h.truncate(atOffset: 0)) != nil else { return }
    try? h.write(contentsOf: head + tail)
}

trimLog(logFile)

// ── the watchdog ─────────────────────────────────────────────────────────────
// sync() promises that one source being absent or blocked never stops the
// others, and for a source macOS *denies* that holds: the read fails, .blocked
// is returned, the loop moves on. It does not hold for a source macOS has never
// been ASKED about. That read does not fail — it blocks, in the kernel, until a
// human answers a consent dialog, and if nobody does it blocks forever.
//
// So a source macOS has not asked about stalls the whole run: it logs iMessage's
// line and then stops dead, alive and using no CPU, and every source after it
// is never reached. sync-status.json stays frozen at the last good run, so the
// app's Data page goes on showing a stale
// picture with nothing anywhere saying it is stale. One unanswered dialog takes
// out every other source, which is strictly worse than the feature being absent.
//
// A blocked syscall cannot be interrupted from the thread that made it, so the
// watchdog is a second thread that outlives it: on expiry it writes the report
// for whatever DID finish and exits the process. `outcomes` is appended to as
// the loop goes, so a partial report is a real one — the sources that completed
// report their true state and the stalled one is named as the reason.
//
// 90 seconds, against launchd's 120-second interval: long enough that no honest
// copy of a multi-gigabyte source is cut short, short enough that the next
// scheduled run is never racing a corpse of the previous one. Overridable for
// anyone whose sources are genuinely slower.
let watchdogSeconds: Double = ProcessInfo.processInfo
    .environment["NCRM_SYNC_WATCHDOG_SEC"].flatMap(Double.init) ?? 90

// ── run every source ─────────────────────────────────────────────────────────
var outcomes: [(Source, Outcome)] = []
/// Which source the main thread is inside, for the watchdog's message. Only ever
/// written before a sync() call and read by the watchdog thread.
var inFlight: String = "(starting up)"
let outcomesLock = NSLock()

let watchdog = Thread {
    Thread.sleep(forTimeInterval: watchdogSeconds)
    outcomesLock.lock()
    let stuck = inFlight
    let done = outcomes.map { $0.0.label }
    outcomesLock.unlock()
    note("watchdog: \(Int(watchdogSeconds))s elapsed and '\(stuck)' has not "
         + "returned. macOS blocks — rather than refuses — a read of a protected "
         + "folder it has never asked about, so this is most likely a consent "
         + "dialog nobody answered. "
         + (done.isEmpty ? "No source finished." : "Finished first: \(done.joined(separator: ", ")).")
         + " Reporting what completed and exiting; launchd retries shortly.")
    // Same exit code as a permission problem, because that is what this is: the
    // installer's contract treats 2 as "tell the user about Full Disk Access".
    reportAndExit(code: 2)
}
watchdog.start()

/// One source, on its own thread, abandoned if it does not come back in time.
///
/// The whole-run watchdog above stops the helper hanging forever. It does NOT
/// stop one bad source costing the others, because the sources run in order and
/// the watchdog kills the run: WhatsApp is second of four, so if it blocks,
/// Contacts and Calendar are never reached at all. Each run would log iMessage,
/// stall on WhatsApp until the watchdog fires, and exit — four sources, none
/// of them syncing, while the app's Data page shows the last good report as
/// though it were current.
///
/// Why this recurs rather than being a one-off, and it is worth writing down:
/// this binary is ad-hoc signed, so its cdhash changes on every compile, and TCC
/// keys app-data grants (WhatsApp's and Calendar's group containers) on that
/// hash. Every rebuild therefore ORPHANS those grants, and the next read blocks
/// on a fresh consent dialog instead of failing. Signing with a real identity,
/// where the builder has one, fixes it at the root.
///
/// A THREAD, not a child process. The obvious implementation is to re-exec
/// ourselves per source and kill the child on timeout. It would work, and
/// `tests/test_packaging.py` rejects it, deliberately. This binary
/// holds Full Disk Access and its audit argument is that you can read all of it
/// and see it has no network and spawns nothing; `Process(` would have spent that
/// argument to buy something a thread provides for free.
///
/// The abandoned thread stays stuck in its syscall for the life of the process,
/// which is seconds: launchd runs us every 120s and we exit at the end of the
/// run. It is holding a blocked read, so it has not written anything and will not
/// — but `cursor` is shared, so every access to it is behind a lock rather than
/// relying on that.
func syncAbandonable(_ s: Source, dest: URL, timeout: Double) -> Outcome {
    let done = DispatchSemaphore(value: 0)
    // Written by the worker, read by us only after `done` — or never.
    final class Slot { var value: Outcome? }
    let slot = Slot()
    let worker = Thread {
        let o = sync(s, dest: dest)
        slot.value = o
        done.signal()
    }
    worker.stackSize = 512 * 1024
    worker.start()
    if done.wait(timeout: .now() + timeout) == .timedOut {
        note("\(s.label): no answer after \(Int(timeout))s, so it is being "
             + "treated as blocked and the run moves on. macOS blocks rather than "
             + "refuses a read of a protected folder it has never asked about, so "
             + "this is most likely a consent dialog nobody answered.")
        return .blocked
    }
    return slot.value ?? .blocked
}

// Per-source ceiling. Deliberately well under the whole-run watchdog: the point
// is that the watchdog becomes the backstop and never the normal path. At least
// two sources must be able to time out before it fires, or it is still the
// watchdog deciding the run's fate and this function changed nothing.
let perSourceSeconds: Double = ProcessInfo.processInfo
    .environment["NCRM_SOURCE_TIMEOUT_SEC"].flatMap(Double.init) ?? 25

for (n, s) in sources.enumerated() {
    outcomesLock.lock(); inFlight = s.label; outcomesLock.unlock()
    let o = syncAbandonable(s, dest: overrides[n] ?? s.dest, timeout: perSourceSeconds)
    outcomesLock.lock(); outcomes.append((s, o)); outcomesLock.unlock()
}
outcomesLock.lock(); inFlight = "(finished)"; outcomesLock.unlock()

let copiedAny = outcomes.contains { if case .copied = $0.1 { return true } else { return false } }
let currentAny = outcomes.contains { if case .current = $0.1 { return true } else { return false } }
let blocked = outcomes.compactMap { (s, o) -> String? in
    if case .blocked = o { return s.label } else { return nil }
}

// ── THE REPORT ───────────────────────────────────────────────────────────────
// The CRM app has to be able to show, in its own window, that iMessage is still
// blocked and WhatsApp is not. It cannot learn that any other way:
//
//   · the exit code is seen only by whoever launched us, and in normal operation
//     that is launchd, up to six hours ago;
//   · stdout goes to ~/Library/Logs/NetworkCRM/imessage-sync.log, and having the
//     app screen-scrape our prose would turn every reworded log line into a
//     silent UI regression;
//   · the app cannot look at the protected folders itself — that is the entire
//     reason this binary exists.
//
// So the outcome is written as data, next to the copies, inside the app's own
// Application Support folder. The app already reads that folder for the database
// copies themselves, so this needs no permission it does not already have — and
// in particular it does NOT put the app anywhere near a TCC-guarded path.
//
// Written on every terminal path, including the blocked one, because "blocked"
// is the single most important thing the app needs to hear. The one gap: a hard
// I/O error inside sync() exits from there with code 1 and leaves the previous
// report in place, so the app also shows how old a report is and says so when it
// has gone quiet. That failure is loud in the log and rare; widening Outcome to
// carry it would mean reopening the exit-code contract below, which is not worth
// doing in the same change.
let reportFile = supportDir.appendingPathComponent("sync-status.json", isDirectory: false)

func outcomeName(_ o: Outcome) -> String {
    switch o {
    case .copied:  return "copied"
    case .current: return "current"
    case .absent:  return "absent"
    case .blocked: return "blocked"
    }
}

/// Write the report, then exit. `atomic` so the app can never read half a file.
/// Every failure here is swallowed: a report the app cannot have is a worse UI,
/// not a reason to fail a sync that already succeeded.
func reportAndExit(code: Int32) -> Never {
    var entries: [[String: Any]] = []
    for (s, o) in outcomes {
        var entry: [String: Any] = ["label": s.label, "outcome": outcomeName(o)]
        if case .copied(let bytes, let files) = o {
            entry["bytes"] = bytes
            entry["files"] = files
        }
        // How many files the source HAS, copied or not — the count the app needs
        // to notice a source that is technically fine and substantively empty.
        cursorLock.lock()
        entry["matched"] = (cursor[s.label] ?? [:]).count
        cursorLock.unlock()
        if case .absent = o { entry["hint"] = s.absentHint }
        entries.append(entry)
    }
    let report: [String: Any] = [
        "version": 1,
        "ran_at": Date().timeIntervalSince1970,
        "exit": Int(code),
        "binary": selfPath(),
        "sources": entries,
    ]
    if let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) {
        try? fm.createDirectory(at: supportDir, withIntermediateDirectories: true)
        try? data.write(to: reportFile, options: .atomic)
    }
    saveCursor()
    exit(code)
}

// EXIT CODE CONTRACT — read install_imessage_sync.sh before changing this.
//
// The installer runs this once and branches on the exit code alone: 2 is the ONLY
// value that makes it print the Full Disk Access steps and open the System
// Settings pane. So the code has to answer the question the installer is actually
// asking — "is there something the user must go and authorise?" — not the question
// "did any work get done".
//
// Getting that backwards is a real failure: a blocked iMessage beside a
// successfully copied WhatsApp would exit 0, so the installer would announce
// "Full Disk Access is already granted" to a user who had never granted it and
// show no instructions at all. WhatsApp needs no FDA, so on any Mac with
// WhatsApp Desktop that outcome was the DEFAULT one, not an edge case.
//
// Hence: anything blocked wins, even alongside a success. A partial success still
// says so on stdout, line by line, so nothing is hidden either way.
if !blocked.isEmpty {
    let done = copiedAny || currentAny
    // "iMessage and Contacts and Calendar" read fine with two sources and stopped
    // reading like English the moment a third was added. Oxford-less list: "a",
    // "a and b", "a, b and c".
    let blockedList: String = {
        if blocked.count <= 2 { return blocked.joined(separator: " and ") }
        return blocked.dropLast().joined(separator: ", ") + " and " + blocked.last!
    }()
    warn("\(blockedList) still \(blocked.count == 1 ? "needs" : "need") "
        + "macOS Full Disk Access"
        + (done ? " (the other source above copied fine — this is not a total failure)" : "")
        + ". Nothing prompts for it; it must be granted by hand to this exact binary: "
        + "\(selfPath())")
    reportAndExit(code: 2)
}

if copiedAny || currentAny {
    reportAndExit(code: 0)
}

// Everything absent. Cannot be told apart from TCC hiding both, so name the
// likelier cause first but still point at the fix.
// Named by looping, not by indexing sources[0] and sources[1] — the hand-written
// version of this line described two sources for as long as there were three.
warn("found none of \(sources.map { $0.label }.joined(separator: ", "))"
    + ". If you use any of them on this Mac, this is macOS hiding them: grant Full "
    + "Disk Access to this exact binary: \(selfPath())")
reportAndExit(code: 2)
