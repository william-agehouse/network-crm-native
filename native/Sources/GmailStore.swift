import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers

// The Gmail sheet's state and every engine call it makes. The web page's version is
// static/app/gmail.js against crm/api_gmail.py and the GMAIL_ACCOUNTS setting:
//
//   GET  /api/gmail/status        what is set up, missing and waiting (read-only: file
//                                 existence checks and one database read, api_gmail.py:180-230)
//   GET  /api/settings            the current GMAIL_ACCOUNTS value, before changing it
//   POST /api/settings            {key:"GMAIL_ACCOUNTS", value} — add or remove an address,
//                                 through the one path that may write secrets/.env (gmail.js:97-129)
//   POST /api/gmail/credentials   {email, path} — the engine copies the picked file into its
//                                 secrets folder under the name it expects (api_gmail.py:311-351)
//   POST /api/gmail/connect       {email} — starts the Google sign-in; returns at once and the
//                                 sheet polls status every 2.5 s while it waits (api_gmail.py:435-474)

// MARK: - Answers (crm/api_gmail.py:180-206)

struct GmailAccount: Codable, Sendable, Hashable, Identifiable {
    @Def var email: String
    /// The file's name only, never its folder.
    @Def var credentialsFile: String
    @Def var hasCredentials: Bool
    @Def var hasToken: Bool
    /// Google has taken the permission back although the token file is still here (NC-183).
    @Def var signInExpired: Bool
    var id: String { email }
    enum CodingKeys: String, CodingKey {
        case email
        case credentialsFile = "credentials_file", hasCredentials = "has_credentials"
        case hasToken = "has_token", signInExpired = "sign_in_expired"
    }
}

struct GmailConnect: Codable, Sendable, Hashable, HasDefault {
    /// idle | running | succeeded | failed
    @Def var state: String
    @Def var running: Bool
    @Def var account: String
    @Def var message: String
    /// The sign-in program's last lines, shown under "what Google said".
    @Def var tail: [String]
    @Def var startedAt: String
    @Def var finishedAt: String
    static var defaultValue: GmailConnect { GmailConnect() }
    enum CodingKeys: String, CodingKey {
        case state, running, account, message, tail
        case startedAt = "started_at", finishedAt = "finished_at"
    }
}

struct GmailStatus: Codable, Sendable, Hashable, HasDefault {
    @Def var accounts: [GmailAccount]
    @Def var configured: Bool
    @Def var ready: [String]
    @Def var needsFile: [String]
    @Def var needsSignIn: [String]
    @Def var connect: GmailConnect
    @Def var connectTimeoutSec: Int
    static var defaultValue: GmailStatus { GmailStatus() }
    enum CodingKeys: String, CodingKey {
        case accounts, configured, ready, connect
        case needsFile = "needs_file", needsSignIn = "needs_sign_in", connectTimeoutSec = "connect_timeout_sec"
    }
}

struct GmailCredentialsSaved: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var savedAs: String
    @Def var hardened: Bool
    @Def var warning: String?
    @Def var status: GmailStatus?
    enum CodingKeys: String, CodingKey {
        case ok, hardened, warning, status
        case savedAs = "saved_as"
    }
}

struct GmailConnectStarted: Codable, Sendable, Hashable {
    @Def var started: Bool
    @Def var message: String
    @Def var status: GmailStatus?
}

/// `POST /api/settings` answers with every setting (secret ones masked); only these two
/// keys are read, so the rest is never even decoded.
struct GmailSettingSaved: Codable, Sendable, Hashable {
    @Def var secured: Bool?
    @Def var warning: String?
}

/// One row's line and tone (gmail.js:200-206). An expired permission outranks the token
/// file, and an open Google tab outranks everything (NC-183).
enum GmailRowState: Equatable, Sendable {
    case waiting, needsFile, expired, readyToSignIn, connected

    init(_ row: GmailAccount, connect: GmailConnect) {
        if connect.state == "running" && connect.account == row.email { self = .waiting }
        else if !row.hasCredentials { self = .needsFile }
        else if row.signInExpired { self = .expired }
        else if !row.hasToken { self = .readyToSignIn }
        else { self = .connected }
    }

    var says: String {
        switch self {
        case .waiting: "Waiting for you in the Google tab"
        case .needsFile: "Needs the file from Google"
        case .expired: "Google has taken the permission back"
        case .readyToSignIn: "Ready to sign in"
        case .connected: "Connected"
        }
    }

    var tone: SetupTone {
        switch self {
        case .waiting, .needsFile, .expired: .warn
        case .readyToSignIn: .plain
        case .connected: .ok
        }
    }
}

// MARK: - The store

@MainActor
@Observable
final class GmailStore {
    private(set) var status: GmailStatus?
    private(set) var loadError: String?
    /// The address field. Kept here so a failed save keeps what was typed.
    var draft = ""
    private(set) var adding = false
    /// The row with a request in flight (choose file, sign in, remove).
    private(set) var busyEmail: String?
    private(set) var notice: SetupNotice?
    var confirmRemove: GmailAccount?

    /// Set by `start(app:)`; the self-test sets it directly.
    @ObservationIgnored var client: EngineClient?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var isPreview = false

    /// How often to ask "have they clicked Allow yet" (gmail.js:36).
    static let pollInterval: Duration = .milliseconds(2500)

    init() {}

    /// For `--snapshot` only: a sheet drawn from a made-up status, never the engine.
    init(preview status: GmailStatus, draft: String = "") {
        self.status = status
        self.draft = draft
        self.isPreview = true
    }

    func start(app: AppModel) async {
        if client == nil { client = app.client }
        if !isPreview { await load() }
        app.markSettled()
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    func load() async {
        guard let client else { return }
        do {
            let s = try await client.get("/api/gmail/status", as: GmailStatus.self)
            apply(s)
            loadError = nil
        } catch is CancellationError {
        } catch {
            // gmail.js:302-303
            if status == nil { loadError = "Gmail's setup did not load. \(SetupWords.sentence(error))" }
        }
    }

    private func apply(_ s: GmailStatus) {
        let wasRunning = status?.connect.state == "running"
        status = s
        // Poll only while somebody is looking at a Google tab, and stop the moment it
        // settles (gmail.js:280-286). The outcome is then drawn from `connect` itself, so
        // the "A Google tab is opening" line from the start goes.
        if s.connect.state == "running" {
            startPolling()
        } else {
            stop()
            if wasRunning { notice = nil }
        }
    }

    private func startPolling() {
        guard pollTask == nil, !isPreview else { return }
        // One read per tick; `apply` schedules the next one while the sign-in still runs.
        pollTask = Task { [weak self] in
            try? await Task.sleep(for: Self.pollInterval)
            guard let self, !Task.isCancelled else { return }
            self.pollTask = nil
            await self.load()
        }
    }

    // MARK: The address (GMAIL_ACCOUNTS)

    // from static/app/gmail.js:92-95
    static func looksLikeEmail(_ s: String) -> Bool {
        let v = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.contains("@") && !v.contains(",") && !v.contains(" ") && v.count > 3
    }

    /// The comma-separated entries, each `email` or `email:credentials-file`.
    static func entries(_ value: String) -> [String] {
        value.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    static func address(of entry: String) -> String {
        (entry.split(separator: ":", maxSplits: 1).first.map(String.init) ?? entry)
            .trimmingCharacters(in: .whitespaces).lowercased()
    }

    /// Appends rather than replaces, so a second mailbox never loses the first, and the
    /// `address:file.json` form other people use survives untouched (gmail.js:108-129).
    func addAccount() async {
        let value = draft.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard let client, Self.looksLikeEmail(value), !adding else { return }
        adding = true
        defer { adding = false }
        do {
            var parts = Self.entries(try await currentAccounts(client))
            let already = parts.contains { Self.address(of: $0) == value }
            if !already { parts.append(value) }
            let saved = try await save(parts.joined(separator: ","), client)
            draft = ""
            notice = SetupNotice(text: saved.warning ?? (already ? "\(value) was already set up." : "\(value) added."),
                                 tone: saved.warning == nil ? .ok : .warn)
            await load()
        } catch is CancellationError {
        } catch {
            notice = SetupNotice(text: SetupWords.sentence(error), tone: .bad)
        }
    }

    /// The web page has no remove: this is AddAccount's mirror, through the same setting,
    /// keeping every other entry exactly as written. The sign-in file and the saved
    /// permission stay in the app's secrets folder; nothing here deletes a file.
    func removeAccount(_ email: String) async {
        guard let client, busyEmail == nil else { return }
        busyEmail = email
        defer { busyEmail = nil }
        do {
            let parts = Self.entries(try await currentAccounts(client))
            let kept = parts.filter { Self.address(of: $0) != email.lowercased() }
            guard kept.count != parts.count else {
                notice = SetupNotice(text: "\(email) was not set up, so there was nothing to remove.")
                await load()
                return
            }
            let saved = try await save(kept.joined(separator: ","), client)
            notice = SetupNotice(
                text: saved.warning ?? "\(email) removed. Gmail no longer collects from it. Mail already imported stays.",
                tone: saved.warning == nil ? .ok : .warn)
            await load()
        } catch is CancellationError {
        } catch {
            notice = SetupNotice(text: SetupWords.sentence(error), tone: .bad)
        }
    }

    private func currentAccounts(_ client: EngineClient) async throws -> String {
        try await client.get("/api/settings", as: SettingsResponse.self).value(for: "GMAIL_ACCOUNTS") ?? ""
    }

    private func save(_ value: String, _ client: EngineClient) async throws -> GmailSettingSaved {
        try await client.post(
            "/api/settings", body: ["key": "GMAIL_ACCOUNTS", "value": .string(value)] as [String: JSONValue],
            as: GmailSettingSaved.self)
    }

    // MARK: The file Google gives you

    /// A native open panel instead of the engine's /api/import/pick-file, which would put
    /// a picker on the screen from a background program.
    func pickFile(for email: String) async {
        guard let client, busyEmail == nil else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose the File Google Gave You"
        panel.message = "The JSON file from your Desktop app client. Its name starts with client_secret_."
        panel.prompt = "Choose"
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        let response: NSApplication.ModalResponse
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            response = await withCheckedContinuation { cont in
                panel.beginSheetModal(for: window) { cont.resume(returning: $0) }
            }
        } else {
            response = panel.runModal()
        }
        // A deliberate cancel says nothing (gmail.js:165-166).
        guard response == .OK, let url = panel.url else { return }
        busyEmail = email
        defer { busyEmail = nil }
        do {
            let r = try await sendCredentials(url, for: email, client)
            notice = SetupNotice(text: r.warning ?? "Saved as \(r.savedAs).", tone: r.warning == nil ? .ok : .warn)
            if let s = r.status { apply(s) }
            await load()
        } catch is CancellationError {
        } catch {
            notice = SetupNotice(text: SetupWords.sentence(error), tone: .bad)
        }
    }

    /// The engine reads the file at a path and copies it (api_gmail.py:311-351). The file
    /// the person picked usually sits in Downloads, which macOS lets THIS app read because
    /// the person picked it here, but not the engine, a separate program that would stall
    /// on a permission prompt. So the engine is handed a private copy in this app's own
    /// temporary folder (owner-only), which is deleted as soon as the engine has it.
    ///
    /// The engine is never given the picked path itself, not even for a file too big to be
    /// Google's: merely asking for the size of a file in Downloads makes macOS hold the
    /// engine until somebody answers a permission prompt. A file larger than the engine's
    /// limit is copied only up to one byte past it, so the engine still sees it is too big
    /// and says so in its own words.
    private func sendCredentials(_ picked: URL, for email: String, _ client: EngineClient) async throws -> GmailCredentialsSaved {
        let fm = FileManager.default
        let data: Data
        do {
            let handle = try FileHandle(forReadingFrom: picked)
            defer { try? handle.close() }
            data = try handle.read(upToCount: Self.engineFileLimit + 1) ?? Data()
        } catch {
            throw EngineError.engine(status: 0, message: "That file could not be read. Choose it again, or download it again from the Cloud Console.")
        }
        let dir = fm.temporaryDirectory.appending(path: "networkcrm-gmail-\(UUID().uuidString)", directoryHint: .isDirectory)
        try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? fm.removeItem(at: dir) }
        let copy = dir.appending(path: picked.lastPathComponent)
        guard fm.createFile(atPath: copy.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            throw EngineError.engine(status: 0, message: "The file could not be prepared for the engine. Try choosing it again.")
        }
        return try await client.post(
            "/api/gmail/credentials", body: ["email": .string(email), "path": .string(copy.path)] as [String: JSONValue],
            as: GmailCredentialsSaved.self)
    }

    /// The engine's own ceiling for a sign-in file (api_gmail.py `_MAX_BYTES`).
    static let engineFileLimit = 64 * 1024

    // MARK: Signing in

    // from static/app/gmail.js:179-189
    func signIn(_ email: String) async {
        guard let client, busyEmail == nil else { return }
        busyEmail = email
        defer { busyEmail = nil }
        do {
            let r = try await client.post(
                "/api/gmail/connect", body: ["email": .string(email)] as [String: JSONValue], as: GmailConnectStarted.self)
            notice = SetupNotice(text: r.message, tone: r.started ? .plain : .warn)
            if let s = r.status { apply(s) } else { await load() }
        } catch is CancellationError {
        } catch {
            notice = SetupNotice(text: SetupWords.sentence(error), tone: .bad)
        }
    }

    /// Google Cloud Console's Credentials page: the engine's allowlisted target
    /// `gmail_console` (crm/importers.py:1556-1557).
    static let consoleURL = URL(string: "https://console.cloud.google.com/apis/credentials")!

    func openConsole() {
        SetupWords.open(Self.consoleURL)
    }
}
