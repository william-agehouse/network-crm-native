import AppKit
import Foundation
import Observation

// The Full Disk Access sheet's state and every engine call it makes. The web page's version
// is static/app/fda.js against crm/api_fda.py:
//
//   GET  /api/fda/status      the helper's own report of its last run (read-only: four
//                             file-exists checks and one small JSON file in the app's own
//                             folder, api_fda.py:193-283)
//   POST /api/fda/install     {} runs the bundled installer          (api_fda.py:303-355)
//   POST /api/fda/uninstall   {} the switch's other position, NC-187 (api_fda.py:358-394)
//   POST /api/fda/recheck     {} runs the helper through launchd now (api_fda.py:397-446)
//
// The two "Open" buttons do not go through the engine's /api/import/open: a native app
// opens the same allowlisted targets itself (crm/importers.py:1580-1585), so the engine
// never has to start a program on the person's screen.

// MARK: - Answers (crm/api_fda.py:250-283)

struct FullDiskAccessSource: Codable, Sendable, Hashable, Identifiable {
    /// "iMessage", "WhatsApp", "Contacts", "Calendar" (importers.HELPER_SOURCE_LABELS).
    @Def var label: String
    /// copied | current | blocked | absent | unknown.
    @Def var outcome: String
    @Def var hint: String?
    @Def var bytes: Int?
    /// Files the helper found for this source, written whether it copied or not (NC-093).
    @Def var matched: Int?
    @Def var files: Int?
    var id: String { label }
}

struct FullDiskAccessStatus: Codable, Sendable, Hashable, HasDefault {
    /// unavailable | not_installed | no_report | blocked | outdated | absent | quiet | ok
    @Def var state: String
    @Def var available: Bool
    @Def var installer: String
    @Def var installed: Bool
    @Def var binary: String
    @Def var binaryDir: String
    @Def var binaryExists: Bool
    @Def var log: String
    @Def var sources: [FullDiskAccessSource]
    @Def var blocked: [String]
    @Def var ok: [String]
    @Def var missing: [String]
    /// NC-223: the installed helper is an older program than the one in this app,
    /// even if it copies every source; `newerSays` is what the newer one changes.
    @Def var newerInApp: Bool
    @Def var newerSays: String
    @Def var copies: [String]
    @Def var ranAt: Double?
    @Def var ranAgoSeconds: Double?
    @Def var staleHours: Int
    @Def var targets: [String: String]
    /// Seconds the engine lets each button's work run (NC-206).
    @Def var waits: [String: Double]

    static var defaultValue: FullDiskAccessStatus { FullDiskAccessStatus() }

    enum CodingKeys: String, CodingKey {
        case state, available, installer, installed, binary, log, sources, blocked, ok, missing, copies, targets, waits
        case binaryDir = "binary_dir", binaryExists = "binary_exists", ranAt = "ran_at"
        case ranAgoSeconds = "ran_ago_seconds", staleHours = "stale_hours"
        case newerInApp = "newer_in_app", newerSays = "newer_says"
    }
}

/// `POST /api/fda/install` and `/uninstall`: exit 0 means the agent is installed, NOT that
/// the permission is granted — read `status.state` for that (api_fda.py:346-355).
struct FullDiskAccessRun: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var exit: Int
    @Def var output: String
    @Def var status: FullDiskAccessStatus
}

struct FullDiskAccessRecheck: Codable, Sendable, Hashable {
    /// False = the helper did not write a new report within `timeout` seconds.
    @Def var ran: Bool
    @Def var waited: Double
    @Def var timeout: Int
    @Def var status: FullDiskAccessStatus
}

// MARK: - Wording rules

enum FullDiskAccessWords {
    /// NC-324: whichever route is used, what the app keeps is not encrypted (fda.js:347-350).
    static let storedPlainly = "Whichever way you choose, what this app imports, and the copies the helper makes, are kept unencrypted in the app's own folder on this Mac. Any program you run can read them there, just as it can read your other files."

    /// Per-source line: what the helper said, and the dot's tone.
    // from static/app/fda.js:39-45
    static func outcome(_ outcome: String) -> (text: String, tone: SetupTone) {
        switch outcome {
        case "copied": ("copied just now", .ok)
        case "current": ("already up to date", .ok)
        case "blocked": ("blocked by macOS", .bad)
        case "absent": ("not found on this Mac", .faint)
        default: ("unknown", .faint)
        }
    }

    // from static/app/fda.js:97-105
    static func ago(_ seconds: Double?) -> String {
        guard let seconds else { return "" }
        let m = Int((seconds / 60).rounded(.toNearestOrAwayFromZero))
        if m < 1 { return "just now" }
        if m < 60 { return "\(m) minute\(m == 1 ? "" : "s") ago" }
        let h = Int((Double(m) / 60).rounded(.toNearestOrAwayFromZero))
        if h < 48 { return "\(h) hour\(h == 1 ? "" : "s") ago" }
        return "\(Int((Double(h) / 24).rounded(.toNearestOrAwayFromZero))) days ago"
    }

    // from static/app/fda.js:129-134
    static func humanBytes(_ n: Int) -> String {
        guard n > 0 else { return "0 KB" }
        let kb = Double(n) / 1024
        if kb < 1024 { return "\(max(1, Int(kb.rounded(.toNearestOrAwayFromZero)))) KB" }
        return String(format: "%.1f MB", kb / 1024)
    }

    /// "3 files, 12 KB": how much the helper found, so a green row cannot hide an almost
    /// empty import (NC-093). A count of one is shown on purpose.
    // from static/app/fda.js:120-127 (the count gets thousands separators here, as the
    // "files each" line above it has on the web page too, so the two lines agree)
    static func found(_ s: FullDiskAccessSource) -> String {
        var bits: [String] = []
        if let m = s.matched { bits.append("\(Display.number(m)) file\(m == 1 ? "" : "s")") }
        if let b = s.bytes, b > 0 { bits.append(humanBytes(b)) }
        return bits.joined(separator: ", ")
    }

    // from static/app/fda.js:145
    static func sourceState(_ s: FullDiskAccessSource) -> String {
        var text = outcome(s.outcome).text
        let f = found(s)
        if !f.isEmpty { text += " · \(f)" }
        if let hint = s.hint, !hint.isEmpty { text += " — \(hint)" }
        return text
    }

    /// The one line for a working helper.
    // from static/app/fda.js:392-401
    static func workingHead(_ status: FullDiskAccessStatus) -> String {
        let working = status.sources.filter { $0.outcome == "copied" || $0.outcome == "current" }.map(\.label)
        switch status.state {
        case "ok":
            let who = working.isEmpty ? "Every source" : SetupWords.nameList(working)
            return "\(who) \(working.count == 1 ? "refreshes itself" : "refresh themselves"). Last run \(ago(status.ranAgoSeconds))."
        case "quiet":
            return "The sync helper has not reported for over \(status.staleHours) hours."
        default:
            return "The sync helper found none of the apps it copies from on this Mac."
        }
    }

    /// "iMessage 1,204 · WhatsApp 3 files each": how much each source actually read.
    // from static/app/fda.js:408-418
    static func readLine(_ status: FullDiskAccessStatus) -> String {
        let read = status.sources
            .filter { $0.matched != nil && ($0.outcome == "copied" || $0.outcome == "current") }
            .map { "\($0.label) \(Display.number($0.matched ?? 0))" }
            .joined(separator: " · ")
        guard !read.isEmpty else { return "" }
        return "\(read) \(read.contains("·") ? "files each" : "files")"
    }

    /// Which sources are on which of the two routes right now. A source is on the helper
    /// only when it is installed and its last run copied the source or found it current;
    /// everything else is read from the copy you make yourself (importers.py auto_copy_label).
    struct LiveRoutes: Equatable, Sendable {
        var helper: [String]
        var ownCopy: [String]
        /// Every source the helper copies, in the helper's own order.
        var all: [String]
    }

    static let defaultSources = ["iMessage", "WhatsApp", "Contacts", "Calendar"]

    static func liveRoutes(_ status: FullDiskAccessStatus) -> LiveRoutes {
        let all = status.copies.isEmpty ? defaultSources : status.copies
        let good = status.installed
            ? Set(status.sources.filter { $0.outcome == "copied" || $0.outcome == "current" }.map(\.label)) : []
        return LiveRoutes(helper: all.filter { good.contains($0) }, ownCopy: all.filter { !good.contains($0) }, all: all)
    }

    static func helperTag(_ status: FullDiskAccessStatus, _ live: LiveRoutes) -> (String, SetupTone) {
        if live.helper.isEmpty { return status.installed ? ("Installed, not copying yet", .warn) : ("Off", .plain) }
        if live.ownCopy.isEmpty { return ("In use", .ok) }
        return ("In use for \(SetupWords.nameList(live.helper))", .ok)
    }

    static func ownCopyTag(_ live: LiveRoutes) -> (String, SetupTone) {
        if live.ownCopy.isEmpty { return ("Ready if you turn the helper off", .plain) }
        if live.helper.isEmpty { return ("In use", .ok) }
        return ("In use for \(SetupWords.nameList(live.ownCopy))", .ok)
    }

    /// The Data page's own two labels (importers.AUTO_COPY_HELPER / AUTO_COPY_MANUAL).
    static func liveLine(_ live: LiveRoutes) -> String {
        if live.helper.isEmpty {
            return "Live now: your own copies. They read as “Your copy in the import folder” on the Data page."
        }
        if live.ownCopy.isEmpty {
            return "Live now: the helper. Its sources read as “Updated automatically” on the Data page."
        }
        return "Live now: the helper for \(SetupWords.nameList(live.helper)) (“Updated automatically” on the Data page), and your own copies for \(SetupWords.nameList(live.ownCopy)) (“Your copy in the import folder”)."
    }

    /// The SF Symbol for a source label (the web page's SRC_ICON, fda.js:52-55).
    static func symbol(_ label: String) -> String {
        switch label {
        case "iMessage": "message"
        case "WhatsApp": "phone.bubble"
        case "Contacts": "person.crop.circle"
        case "Calendar": "calendar"
        default: "bubble.left"
        }
    }
}

// MARK: - The store

@MainActor
@Observable
final class FullDiskAccessStore {
    enum Busy: String, Sendable { case none, install, uninstall, check }

    /// nil until the first answer.
    private(set) var status: FullDiskAccessStatus?
    private(set) var loadError: String?
    private(set) var busy: Busy = .none
    /// The installer's own output, kept for "Show details".
    private(set) var output = ""
    var showOutput = false
    /// A check has run in this sheet, so "checked … ago" may be shown (fda.js:514).
    private(set) var checked = false
    private(set) var notice: SetupNotice?
    var confirmTurnOff = false

    /// Set by `start(app:)`; the self-test sets it directly.
    @ObservationIgnored var client: EngineClient?
    @ObservationIgnored private var isPreview = false

    init() {}

    /// For `--snapshot` only: a sheet drawn from a made-up status, never the engine.
    init(preview status: FullDiskAccessStatus, output: String = "", checked: Bool = false, notice: SetupNotice? = nil) {
        self.status = status
        self.notice = notice
        self.output = output
        self.showOutput = !output.isEmpty
        self.checked = checked
        self.isPreview = true
    }

    func start(app: AppModel) async {
        if client == nil { client = app.client }
        if !isPreview { await load() }
        app.markSettled()
    }

    func load() async {
        guard let client else { return }
        do {
            status = try await client.get("/api/fda/status", as: FullDiskAccessStatus.self)
            loadError = nil
        } catch is CancellationError {
        } catch {
            // The web page renders nothing here (fda.js:213); a sheet opened on purpose
            // says why it is empty instead.
            if status == nil { loadError = SetupWords.sentence(error) }
        }
    }

    // MARK: The three buttons that write

    // from static/app/fda.js:236-256
    func install() async {
        guard let client, busy == .none else { return }
        busy = .install
        output = ""
        defer { busy = .none }
        do {
            let r = try await client.post(
                "/api/fda/install", body: [String: JSONValue](), as: FullDiskAccessRun.self, timeout: wait("install"))
            output = r.output
            status = r.status
            // The installer opens System Settings itself on the blocked path, so "now grant
            // it" is the whole message, not "installed ✓".
            note(r.ok ? "Helper installed. Now grant it Full Disk Access."
                : "The installer reported a problem — see the details below.", r.ok ? .ok : .bad)
            if !r.ok { showOutput = true }
        } catch is CancellationError {
        } catch {
            note(SetupWords.sentence(error), .bad)
            await load()  // whatever happened, show what is true now
        }
    }

    // from static/app/fda.js:258-279
    func uninstall() async {
        guard let client, busy == .none else { return }
        busy = .uninstall
        output = ""
        defer { busy = .none }
        do {
            let r = try await client.post(
                "/api/fda/uninstall", body: [String: JSONValue](), as: FullDiskAccessRun.self, timeout: wait("uninstall"))
            output = r.output
            status = r.status
            // The grant outlives the helper: macOS keeps the entry until the person removes it.
            note(r.ok ? "Automatic copying is off. Its Full Disk Access entry stays in System Settings until you remove it there."
                : "Turning it off reported a problem — see the details below.", r.ok ? .ok : .bad)
            if !r.ok { showOutput = true }
        } catch is CancellationError {
        } catch {
            note(SetupWords.sentence(error), .bad)
            await load()
        }
    }

    // from static/app/fda.js:281-302
    func recheck() async {
        guard let client, busy == .none else { return }
        busy = .check
        defer { busy = .none }
        do {
            let r = try await client.post(
                "/api/fda/recheck", body: [String: JSONValue](), as: FullDiskAccessRecheck.self, timeout: wait("recheck"))
            status = r.status
            checked = true
            if !r.ran {
                note("The helper did not report back within \(r.timeout)s. It may still be copying.", .warn)
            } else if r.status.state == "ok" {
                note("Working — macOS is not blocking anything.", .ok)
            } else if !r.status.blocked.isEmpty {
                note("Still blocked: \(r.status.blocked.joined(separator: " and ")).", .warn)
            } else {
                notice = nil
            }
        } catch is CancellationError {
        } catch {
            note(SetupWords.sentence(error), .bad)
            await load()
        }
    }

    /// How long to wait for a button's work: the engine's own limit plus a margin, so the
    /// sheet never gives up while the script carries on and succeeds (NC-206).
    // from static/app/fda.js:189-199
    static let waitMargin: TimeInterval = 15
    static let waitFallback: [String: TimeInterval] = ["install": 300, "uninstall": 300, "recheck": 65]
    static let waitMax: TimeInterval = 3600
    /// EngineClient's session gives up on any request after importTimeout + 60 s, so a
    /// per-request wait above that would be cut short with a misleading number.
    static let sessionCeiling: TimeInterval = EngineClient.importTimeout + 55

    static func wait(_ action: String, waits: [String: Double]?) -> TimeInterval {
        let sec = waits?[action] ?? 0
        let base = sec.isFinite && sec > 0 ? sec : (waitFallback[action] ?? 65)
        return min(min(waitMax, base) + waitMargin, sessionCeiling)
    }

    private func wait(_ action: String) -> TimeInterval { Self.wait(action, waits: status?.waits) }

    // MARK: Opening things (no engine call)

    /// The Privacy → Full Disk Access pane. The URL is the engine's own allowlisted
    /// target `fda_settings` (crm/importers.py:1580-1582).
    static let settingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!

    func openSettings() {
        SetupWords.open(Self.settingsURL)
    }

    /// Finder with the helper already selected, or its folder when the file is missing:
    /// the same fallback as importers.resolve_open_target (crm/importers.py:1611-1615).
    func revealHelper() {
        guard let status, !status.binary.isEmpty else { return }
        let binary = URL(fileURLWithPath: status.binary)
        if FileManager.default.fileExists(atPath: binary.path) {
            NSWorkspace.shared.activateFileViewerSelecting([binary])
        } else if FileManager.default.fileExists(atPath: status.binaryDir) {
            NSWorkspace.shared.open(URL(fileURLWithPath: status.binaryDir, isDirectory: true))
        } else {
            note("The helper is not on this Mac yet. Set it up first.", .warn)
        }
    }

    // from static/app/fda.js:475-478
    func copyPath() {
        guard let path = status?.binary, !path.isEmpty else { return }
        NSPasteboard.general.clearContents()
        if NSPasteboard.general.setString(path, forType: .string) {
            note("Path copied.", .ok)
        } else {
            note("Could not copy the path.", .bad)
        }
    }

    private func note(_ text: String, _ tone: SetupTone = .plain) {
        notice = SetupNotice(text: text, tone: tone)
    }
}
