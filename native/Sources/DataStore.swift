import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers

// The Data page's state and every engine call it makes. Views read it and call its
// methods; nothing here draws. Every word and number on the page comes from one answer,
// GET /api/data/ledger (crm/api_ledger.py); after anything you do on the page it asks
// again (NC-125: there is no refresh button to press). Routes, exactly as data.js sends them:
//
//   GET  /api/data/ledger                 the quick answer, drawn first
//   GET  /api/data/ledger?scan=1          the same, after looking on this Mac for files to
//                                         import. It globs folders macOS protects and can
//                                         raise a permission prompt, so it runs only in the
//                                         app opened from Finder (never on NC_NATIVE_PORT,
//                                         so never in --snapshot or --selftest) and from the
//                                         page's own Check Again button.
//   POST /api/import/run {source, path, dry_run, ...act.form} data.js:86-87
//   POST /api/import/open {source: target}                  ui.js:29-32
//   POST /api/settings {key, value}                         ledger.js:242-244
//   GET  /api/provenance?source=                            data.js:124
//   GET  /api/duplicates                                    data.js:426
//   POST /api/merge {keep_id, dup_id}, once per record folded  data.js:435-446
//   GET  /api/export?format=md|csv|json                     data.js:509-511 (DataExport.swift)
//
// The web page's file picker asks the ENGINE to open a dialog (POST /api/import/pick-file,
// osascript). /api/import/run takes any path the client sends (api_import.py:841, through
// human_path), so this app picks with its own NSOpenPanel instead and never calls pick-file.

/// One import control's state, kept here so closing and reopening a row keeps a running
/// import and its result.
struct DataImportRun: Equatable {
    var running = false
    var result: DataImportResult?
    /// The request itself failed (the engine was not reached, or it refused).
    var failure: String?
    var note = ""

    var log: String? {
        if let failure { return failure }
        guard let result else { return nil }
        return result.output.isEmpty ? "(no output)" : result.output
    }
    var ok: Bool { failure == nil && (result?.ok ?? false) }
    /// The free offline facts pass runs after an import, so its chips join the import's own.
    var receipts: [DataReceipt] { (result?.receipts ?? []) + (result?.enriched ?? []) }
    var added: Int { result?.newlyAdded ?? 0 }
}

/// An import started from Needs you: its need goes away once it worked, so the page keeps
/// the result at the top of the section instead (NC-216).
struct DataRan: Equatable {
    let label: String
    let run: DataImportRun
}

enum DataProvState: Equatable {
    case loading
    case loaded(DataProvenancePanel?)
    case failed
}

@MainActor
@Observable
final class DataStore: LedgerHost {
    private(set) var answer: LedgerAnswer?
    private(set) var error: String?
    /// True while the ?scan=1 answer is on its way.
    private(set) var scanning = false
    /// "", "timeout" or "error": I could not look is a different answer from nothing found.
    private(set) var scanFailed = ""
    var openKey = ""
    /// The id to bring into view ("row-<key>", "dups", "export"); `scrollTick` re-asks.
    private(set) var scrollTarget: String?
    private(set) var scrollTick = 0
    private(set) var flashText: String?
    var ran: DataRan?
    private(set) var runs: [String: DataImportRun] = [:]
    var paths: [String: String] = [:]
    private(set) var provenance: [String: DataProvState] = [:]
    var shownRecords: Set<String> = []
    var dupsOpen = false
    private(set) var groups: [[DataDupMember]]?
    private(set) var merging = false
    private(set) var exporting: String?

    @ObservationIgnored weak var app: AppModel?
    @ObservationIgnored private var asked = 0
    @ObservationIgnored private var flashTask: Task<Void, Never>?
    @ObservationIgnored private var pendingFocus: String?

    private var client: EngineClient? { app?.client }

    /// The scan only runs in the app opened from Finder: never against a development port.
    var scanAllowed: Bool { !(app?.engine.isDevelopmentPort ?? true) }

    // MARK: Loading

    /// One store per window, kept while you visit other screens, so an import that is still
    /// running, its result, the open row and the duplicates you were reviewing are all
    /// still there when you come back. (The web page forgets them when you leave it; an
    /// import of a large source can take minutes.)
    private final class Kept {
        weak var owner: AppModel?
        let store: DataStore
        init(owner: AppModel, store: DataStore) {
            self.owner = owner
            self.store = store
        }
    }
    private static var stores: [ObjectIdentifier: Kept] = [:]

    static func store(for app: AppModel) -> DataStore {
        let key = ObjectIdentifier(app)
        // The owner is checked, not only the address: a window model that has gone away can
        // leave its address to a new one (each --snapshot scene makes its own), which must
        // not inherit the old page's open rows.
        if let kept = stores[key], kept.owner === app { return kept.store }
        stores = stores.filter { $0.value.owner != nil }
        let s = DataStore()
        stores[key] = Kept(owner: app, store: s)
        return s
    }

    func start(app: AppModel) async {
        self.app = app
        await load(quickFirst: true)
        // Coming back to the page: the groups on screen may have changed elsewhere.
        if dupsOpen && groups != nil { await loadDuplicates() }
        app.markSettled()
    }

    /// Ask once quickly and draw; then, in the real app, ask again with the scan, which can
    /// wait on a macOS permission prompt. After any action, ask with the scan straight away:
    /// the page already has something on screen.
    // from static/app/data.js:571-597
    func load(quickFirst: Bool) async {
        guard let client else { return }
        asked += 1
        let mine = asked
        var shown = !quickFirst && answer != nil
        if quickFirst || !scanAllowed {
            do {
                let quick = try await client.get("/api/data/ledger", as: LedgerAnswer.self)
                guard mine == asked else { return }
                answer = quick
                error = nil
                shown = true
                applyPendingFocus()
            } catch is CancellationError {
                return
            } catch {
                if mine == asked {
                    if answer == nil { self.error = LedgerSettings.sentence(error) } else { flash(LedgerSettings.sentence(error)) }
                }
                return
            }
        }
        if scanAllowed { await scan(mine: mine, shown: shown) }
    }

    /// The page's own "Check again" button, and the second half of every load in the real app.
    func checkAgain() async {
        asked += 1
        await scan(mine: asked, shown: answer != nil)
    }

    private func scan(mine: Int, shown: Bool) async {
        guard let client else { return }
        scanning = true
        defer { if mine == asked { scanning = false } }
        do {
            let full = try await client.get("/api/data/ledger", query: ["scan": "1"], as: LedgerAnswer.self)
            guard mine == asked else { return }
            answer = full
            error = nil
            scanFailed = full.scanFailed ? "error" : ""
            applyPendingFocus()
        } catch is CancellationError {
        } catch {
            guard mine == asked else { return }
            // The rows are already on screen: say the check failed, keep them.
            if !shown {
                self.error = LedgerSettings.sentence(error)
                return
            }
            if case EngineError.timedOut = error { scanFailed = "timeout" } else { scanFailed = "error" }
        }
    }

    func changed() async { await load(quickFirst: false) }

    // MARK: Moving around the page

    func openRow(_ key: String) {
        openKey = key
        bringIntoView("row-\(key)")
    }

    func toggle(_ key: String) { openKey = openKey == key ? "" : key }

    func bringIntoView(_ id: String) {
        scrollTarget = id
        scrollTick += 1
    }

    /// app.dataFocus: "duplicates", "merge", "export", or a row's key. Kept until the rows
    /// have arrived, then carried out once.
    // from static/app/data.js:386-417 and :428-434
    func focus(_ what: String) {
        pendingFocus = what
        applyPendingFocus()
    }

    private func applyPendingFocus() {
        guard let what = pendingFocus, let answer else { return }
        pendingFocus = nil
        switch what {
        case "duplicates":
            if (answer.install.duplicates ?? 0) > 0 {
                dupsOpen = true
                if groups == nil { Task { await loadDuplicates() } }
            }
            bringIntoView("dups")
        case "merge":
            bringIntoView("dups")
            app?.mergeRequest = MergeRequest()
        case "export":
            bringIntoView("export")
        default:
            if answer.rows.contains(where: { $0.key == what }) { openRow(what) }
        }
    }

    // MARK: Messages

    func flash(_ text: String) {
        flashText = text
        flashTask?.cancel()
        flashTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.6))
            guard !Task.isCancelled else { return }
            self?.flashText = nil
        }
    }

    // MARK: Imports

    func run(_ key: String) -> DataImportRun { runs[key] ?? DataImportRun() }

    func path(_ key: String, act: LedgerAct) -> String { paths[key] ?? act.path }

    func setNote(_ key: String, _ note: String) {
        var r = run(key)
        r.note = note
        runs[key] = r
    }

    /// How long to wait for one import: the engine's own ceiling plus a margin, so the page
    /// outlasts the engine rather than racing it (NC-176). A missing or unusable ceiling
    /// leaves the engine's default of 300 s; the ceiling is clamped to 3,600 s as the
    /// engine clamps it.
    // from static/app/api.js:58-88
    var importTimeout: TimeInterval { Self.importWait(answer) }

    static func importWait(_ answer: LedgerAnswer?) -> TimeInterval {
        let sent = answer?.importTimeoutSec ?? 0
        let ceiling = sent > 0 ? min(3600, sent) : 300
        return TimeInterval(ceiling) + 15
    }

    /// Import a file, or run a source that needs none. One verb for every source: they all go
    /// to the one endpoint.
    // from static/app/data.js:75-103 (useImportRunner) and :244-252 (start)
    func runImport(_ act: LedgerAct, label: String, key: String, dryRun: Bool, fromNeeds: Bool) async {
        guard let client, !run(key).running else { return }
        let path = self.path(key, act: act)
        if act.needsPath && path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            setNote(key, act.folder ? "Choose a folder, or paste its path into the box."
                : "Choose a file, or paste its path into the box.")
            return
        }
        runs[key] = DataImportRun(running: true)
        var done = DataImportRun()
        do {
            // NC-372: the act's own extra fields ride along (Read shared history sends
            // extras: true), and the three every run sends cannot be overridden by them.
            var body: [String: JSONValue] = act.form
            body["source"] = .string(act.source)
            body["path"] = .string(path)
            body["dry_run"] = .bool(dryRun)
            // Its own long-lived request: the shared client stops every call after a few
            // minutes, and one import may run for up to an hour (DataTransport.swift).
            let result = try await client.longPost("/api/import/run", body: body, as: DataImportResult.self, timeout: importTimeout)
            done.result = result
        } catch is CancellationError {
            runs[key] = DataImportRun()
            return
        } catch {
            done.failure = LedgerSettings.sentence(error)
        }
        runs[key] = done
        if fromNeeds { ran = DataRan(label: label, run: done) }
        // A preview changed nothing, so it does not claim it did or refresh the page.
        if done.ok {
            if done.result?.dryRun == true {
                flash("\(label) preview — nothing changed")
            } else {
                flash("\(label) done")
                app?.peopleChanged()
                await app?.refreshCounts()
                await changed()
            }
        }
    }

    /// The file or folder to import, picked with the Mac's own panel. The prompts are the
    /// engine's own picker prompts (crm/api_import.py:93-117).
    func choose(_ act: LedgerAct, key: String) async {
        let panel = NSOpenPanel()
        panel.canChooseFiles = !act.folder
        panel.canChooseDirectories = act.folder
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        panel.prompt = act.folder ? "Choose Folder" : "Choose File"
        let (message, types) = Self.pickPrompt(act.pick)
        panel.message = message
        if !types.isEmpty { panel.allowedContentTypes = types }
        let current = path(key, act: act)
        if !current.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: (current as NSString).expandingTildeInPath).deletingLastPathComponent()
        }
        let response: NSApplication.ModalResponse
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            response = await withCheckedContinuation { cont in
                panel.beginSheetModal(for: window) { cont.resume(returning: $0) }
            }
        } else {
            response = panel.runModal()
        }
        guard response == .OK, let url = panel.url else { return }
        paths[key] = url.path
        setNote(key, "")
    }

    static func pickPrompt(_ kind: String) -> (String, [UTType]) {
        switch kind {
        case "vcf": ("Select your Contacts.vcf export", [.vCard])
        case "csv": ("Select LinkedIn Connections CSV", [.commaSeparatedText])
        case "mbox": ("Select email archive (.mbox)", [])
        case "chatdb": ("Select your chat.db copy", [])
        case "wadb": ("Select your ChatStorage.sqlite copy", [])
        case "profilefolder": ("Select the WhatsApp Media/Profile folder", [])
        case "ics": ("Select a calendar export (.ics)", [UTType(filenameExtension: "ics") ?? .calendarEvent, .calendarEvent])
        case "json": ("Select the sign-in file Google gave you", [.json])
        default: ("Choose what to import", [])
        }
    }

    // MARK: What a source added

    func loadProvenance(_ source: String) async {
        guard let client else { return }
        if case .loaded = provenance[source] { return }
        provenance[source] = .loading
        do {
            let d = try await client.get("/api/provenance", query: ["source": source], as: DataProvenanceAnswer.self)
            provenance[source] = .loaded(d.panels.first)
        } catch is CancellationError {
            provenance[source] = nil
        } catch {
            provenance[source] = .failed
        }
    }

    // MARK: Possible duplicates

    func loadDuplicates() async {
        guard let client else { return }
        do {
            groups = try await client.get("/api/duplicates", as: DataDuplicatesAnswer.self).groups
        } catch is CancellationError {
        } catch {
            groups = []
        }
    }

    /// Fold every other record in the group into the one kept, one merge per record.
    // from static/app/data.js:435-446
    func merge(_ group: [DataDupMember], keep: Int) async {
        guard let client, !merging else { return }
        merging = true
        defer { merging = false }
        do {
            for m in group where m.id != keep {
                _ = try await client.post(
                    "/api/merge", body: ["keep_id": .int(keep), "dup_id": .int(m.id)] as [String: JSONValue],
                    as: WriteResult.self)
                if app?.selectedPersonID == m.id { app?.selectedPersonID = keep }
            }
            flash("Merged")
        } catch is CancellationError {
            return
        } catch {
            flash(LedgerSettings.sentence(error))
        }
        app?.peopleChanged()
        await app?.refreshCounts()
        await loadDuplicates()
        await changed()
    }

    // MARK: Export

    func export(_ format: DataExportFormat) async {
        guard let app, exporting == nil else { return }
        exporting = format.rawValue
        defer { exporting = nil }
        if let saved = await DataExport.save(format, app: app) { flash(saved) }
    }
}
