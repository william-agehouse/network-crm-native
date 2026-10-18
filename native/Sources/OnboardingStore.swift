import Foundation
import Observation

// First-run setup's state and every engine call it makes (static/app/onboarding.js). The
// views in OnboardingFlow.swift and OnboardingSteps.swift only draw this.
//
//   GET  /api/import/sources              the Contacts and iMessage rows' help and pickers
//   GET  /api/import/detect               copies found on this Mac (never on a development port)
//   POST /api/import/pick-file {kind}     the engine's own file picker
//   POST /api/import/open {source}        Finder at an allowlisted folder
//   POST /api/import/run {source, path}   runs one import (waits up to the engine's ceiling)
//   GET  /api/describe-queue              three people worth a line
//   POST /api/contact/{id}/note {body}    "How do you know …?"
//   POST /api/contact/{id}/skip {}        Skip
//   POST /api/settings {key: ANTHROPIC_API_KEY, value}
//   POST /api/onboarding {done: true}     Finish, and Skip setup
//
// Whether to show the wizard at all is OnboardingGate's job (OnboardingFlow.swift).

// MARK: - Engine answers (prefixed so names never clash)

/// One row of `GET /api/import/sources` (`importers.py:1628-1656`), the parts the wizard reads.
struct OnbSource: Codable, Sendable, Hashable {
    @Def var key: String
    @Def var label: String
    /// "file", "folder" or "none".
    @Def var kind: String
    /// The picker the engine opens: "vcf", "chatdb" … (`api_import.py:93-114`).
    @Def var pickKind: String?
    /// Markdown-lite help.
    @Def var exportHelp: String
    /// Finder buttons, in order (iMessage has two).
    @Def var openHelpers: [OnbOpenHelper]
    @Def var openHelper: String
    enum CodingKeys: String, CodingKey {
        case key, label, kind
        case pickKind = "pick_kind", exportHelp = "export_help", openHelpers = "open_helpers", openHelper = "open_helper"
    }
}

struct OnbOpenHelper: Codable, Sendable, Hashable {
    @Def var key: String
    @Def var label: String
}

struct OnbSourcesResponse: Codable, Sendable, Hashable {
    @Def var sources: [OnbSource]
    /// How long one import may run on the engine (NC-176); the wait must outlast it.
    @Def var importTimeoutSec: Int?
    enum CodingKeys: String, CodingKey {
        case sources
        case importTimeoutSec = "import_timeout_sec"
    }
}

/// One copy `GET /api/import/detect` found (`importers.py:944-947`, `api_import.py:477-546`).
struct OnbFinding: Codable, Sendable, Hashable, Identifiable {
    @Def var source: String
    @Def var path: String
    @Def var label: String
    /// A sentence-like stamp of the file's age, shown under the label.
    @Def var mtime: String
    @Def var count: Int
    var id: String { source + path }
}

struct OnbDetectResponse: Codable, Sendable, Hashable {
    @Def var found: [OnbFinding]
}

/// `POST /api/import/run` (`api_import.py:841-943`).
struct OnbImportRun: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var output: String
    @Def var newlyAdded: Int?
    enum CodingKeys: String, CodingKey {
        case ok, output
        case newlyAdded = "newly_added"
    }
}

/// `POST /api/import/pick-file` (`api_import.py:944-990`).
struct OnbPickResult: Codable, Sendable, Hashable {
    @Def var path: String
    @Def var cancelled: Bool
    @Def var error: String
}

/// A person in `GET /api/describe-queue` (`api_views.py:73-84`): the person page's fields
/// plus their last three messages, oldest first.
struct OnbQueuePerson: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var name: String
    @Def var photo: String
    @Def var company: String
    @Def var position: String
    @Def var homeMetro: String
    @Def var homeCity: String
    @Def var roleType: String
    @Def var topics: [String]
    @Def var strength: Int
    @Def var strengthLabel: String
    @Def var lastMessages: [OnbLastMessage]
    enum CodingKeys: String, CodingKey {
        case id, name, photo, company, position, topics, strength
        case homeMetro = "home_metro", homeCity = "home_city", roleType = "role_type"
        case strengthLabel = "strength_label", lastMessages = "last_messages"
    }
}

struct OnbLastMessage: Codable, Sendable, Hashable {
    @Def var channel: String
    @Def var direction: Int
    @Def var ts: String
    @Def var body: String
}

struct OnbDescribeQueue: Codable, Sendable, Hashable {
    @Def var queue: [OnbQueuePerson]
}

/// `POST /api/settings`: only the warning is read; the masked rows are never kept.
struct OnbSettingSaved: Codable, Sendable, Hashable {
    @Def var warning: String?
}

/// `GET /api/settings/ledger`, only `install.setup_again.done` — the read-only answer to
/// "has this Mac finished or skipped setup" (`api_abilities.py:261-311`).
struct OnbSettingsLedger: Codable, Sendable, Hashable {
    @Def var install: OnbInstall
}

struct OnbInstall: Codable, Sendable, Hashable, HasDefault {
    @Def var setupAgain: OnbSetupAgain
    static var defaultValue: OnbInstall { OnbInstall() }
    enum CodingKeys: String, CodingKey { case setupAgain = "setup_again" }
}

struct OnbSetupAgain: Codable, Sendable, Hashable, HasDefault {
    @Def var done: Bool
    static var defaultValue: OnbSetupAgain { OnbSetupAgain() }
}

// MARK: - The store

/// One import source's step (Contacts or iMessage): its help, what was found, the path
/// box, and the result of the last run.
@MainActor
@Observable
final class OnbImportStep {
    let key: String
    private(set) var spec: OnbSource?
    /// nil while detecting; [] when nothing was found (or detection is skipped).
    private(set) var found: [OnbFinding]?
    var path = ""
    private(set) var running = false
    private(set) var picking = false
    /// The last run's one line, and whether it worked (green) or not (red).
    private(set) var runLine: String?
    private(set) var runOK = false
    /// Per finding: busy, and its own result line.
    private(set) var findingBusy: Set<String> = []
    private(set) var findingResult: [String: (ok: Bool, line: String)] = [:]

    init(key: String) { self.key = key }

    func setSpec(_ s: OnbSource?) { spec = s }
    func setFound(_ f: [OnbFinding]) { found = f }

    func setRunning(_ on: Bool) { running = on }
    func setPicking(_ on: Bool) { picking = on }
    func setRun(ok: Bool, line: String) { runOK = ok; runLine = line }
    func setFinding(_ id: String, busy: Bool) {
        if busy { findingBusy.insert(id) } else { findingBusy.remove(id) }
    }
    func setFindingResult(_ id: String, ok: Bool, line: String) { findingResult[id] = (ok, line) }

    /// "Run the import" waits for a path unless the source needs none (onboarding.js:251).
    var canRun: Bool {
        guard let spec, !running else { return false }
        return spec.kind == "none" || !path.trimmingCharacters(in: .whitespaces).isEmpty
    }
}

@MainActor
@Observable
final class OnboardingStore {
    /// The seven steps of onboarding.js STEPS: who you are (NC-318) and LinkedIn (NC-319)
    /// come before the two import steps.
    enum Step: Int, CaseIterable, Sendable {
        case hello, you, linkedin, contacts, imessage, remember, ai
    }

    var step: Step = .hello
    /// Lifted out of the Contacts step so Back-then-Continue remembers it (onboarding.js:582-585).
    private(set) var contactsImported = false

    let contacts = OnbImportStep(key: "contacts")
    let imessage = OnbImportStep(key: "imessage")
    /// Seconds one import may take on the engine; the wait adds a margin (api.js:60-88).
    private(set) var importCeiling = 300

    // Remember three people
    /// nil while loading.
    private(set) var queue: [OnbQueuePerson]?
    private(set) var queueIndex = 0
    private(set) var rememberedCount = 0
    var noteText = ""
    private(set) var reviewBusy = false

    // Who you are (NC-318)
    var selfName = ""
    var selfEmail = ""
    var youNote = ""
    private(set) var youBusy = false
    @ObservationIgnored private var youLoaded = false

    // LinkedIn (NC-319)
    private(set) var linkedInAsked = false
    private(set) var linkedInBusy = false
    static let linkedInDataPage = URL(string: "https://www.linkedin.com/mypreferences/d/download-my-data")!

    // AI key
    var apiKey = ""
    private(set) var finishing = false

    /// One line for a few seconds (the web page's toast).
    private(set) var toast: String?
    @ObservationIgnored private var toastTask: Task<Void, Never>?

    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var sourcesLoaded = false
    private var client: EngineClient? { app?.client }

    static let total = Step.allCases.count
    /// The page outlasts the engine's import ceiling by this much (api.js:71).
    static let importMarginSeconds = 15
    static let importCeilingMax = 3600

    func attach(_ app: AppModel) { self.app = app }

    // MARK: Moving

    func next() {
        step = Step(rawValue: min(step.rawValue + 1, Self.total - 1)) ?? .ai
    }

    func back() {
        step = Step(rawValue: max(step.rawValue - 1, 0)) ?? .hello
    }

    /// Continue on the Contacts step: allowed with nothing imported, but says what that
    /// costs (onboarding.js:206-211).
    func continueFromContacts() {
        if !contactsImported {
            show("Skipping this means iMessage still adds people, but as bare names — no phone, email, company or title.")
        }
        next()
    }

    // MARK: Who you are (onboarding.js StepYou, NC-318)

    /// Prefilled with whatever is already set, so setting up again shows what is there.
    func loadYou() async {
        guard !youLoaded, let client else { return }
        youLoaded = true
        guard let s = try? await client.get("/api/settings", as: SettingsResponse.self) else { return }
        if selfName.isEmpty { selfName = s.value(for: "SELF_NAME") ?? "" }
        if selfEmail.isEmpty { selfEmail = s.value(for: "SELF_EMAIL") ?? "" }
    }

    /// Saves what was typed and moves on. An empty box never clears what is already set.
    func saveYou() async {
        guard let client, !youBusy else { return }
        let name = selfName.trimmingCharacters(in: .whitespacesAndNewlines)
        let email = selfEmail.trimmingCharacters(in: .whitespacesAndNewlines)
        let problem = LedgerFormat.textProblem("SELF_EMAIL", email)
        guard problem.isEmpty else { youNote = problem; return }
        youBusy = true
        defer { youBusy = false }
        youNote = ""
        var warning = ""
        do {
            for (key, value) in [("SELF_NAME", name), ("SELF_EMAIL", email)] where !value.isEmpty {
                let saved = try await client.post(
                    "/api/settings", body: ["key": .string(key), "value": .string(value)] as [String: JSONValue],
                    as: OnbSettingSaved.self)
                if warning.isEmpty, let w = saved.warning { warning = w }
            }
        } catch {
            youNote = Self.sentence(error)
            return
        }
        // NC-240's rule: a save that could not lock the settings file says so.
        if !warning.isEmpty { show(warning) }
        next()
    }

    // MARK: LinkedIn (onboarding.js StepLinkedIn, NC-319)

    func loadLinkedIn() async {
        guard let client else { return }
        if let w = try? await client.get("/api/data/linkedin-wait", as: LinkedInWait.self) {
            linkedInAsked = w.waiting
        }
    }

    /// "I have asked for it": the People page and the Data page then say they are waiting.
    func markLinkedInAsked() async {
        guard let client, !linkedInBusy else { return }
        linkedInBusy = true
        defer { linkedInBusy = false }
        do {
            let w = try await client.post("/api/data/linkedin-asked", body: ["asked": true] as [String: JSONValue],
                                          as: LinkedInWait.self)
            linkedInAsked = w.waiting
            if !w.waiting { show("LinkedIn people are already here, so there is nothing to wait for.") }
        } catch {
            show(Self.sentence(error))
        }
    }

    // MARK: Import steps

    func importStep(_ key: String) -> OnbImportStep { key == "contacts" ? contacts : imessage }

    /// The step's help and picker from /api/import/sources, then what is on this Mac.
    func loadImportStep(_ s: OnbImportStep) async {
        if !sourcesLoaded { await loadSources() }
        await detect(s)
    }

    private func loadSources() async {
        guard let client else { return }
        do {
            let d = try await client.get("/api/import/sources", as: OnbSourcesResponse.self)
            // typeof-number check of api.js:78-84: anything unusable keeps the fallback.
            if let sec = d.importTimeoutSec, sec > 0 { importCeiling = min(Self.importCeilingMax, sec) }
            contacts.setSpec(d.sources.first { $0.key == "contacts" })
            imessage.setSpec(d.sources.first { $0.key == "imessage" })
            sourcesLoaded = true
        } catch {
            // The step still shows its words and Continue (onboarding.js:177, :291).
            contacts.setSpec(nil)
            imessage.setSpec(nil)
        }
    }

    /// Copies found on this Mac for this step's source. Skipped on a development port: the
    /// engine probes folders macOS protects, which can put a permission prompt on screen.
    func detect(_ s: OnbImportStep) async {
        guard let app, let client else { return }
        if app.engine.isDevelopmentPort {
            s.setFound([])
            return
        }
        let found = (try? await client.get("/api/import/detect", as: OnbDetectResponse.self))?.found ?? []
        s.setFound(found.filter { $0.source == s.key })
    }

    /// "Choose a file": the engine's own picker, so the engine is the program that may
    /// read the file. A cancel is silent; a picker that could not open says so
    /// (onboarding.js:181-189).
    func pick(_ s: OnbImportStep) async {
        guard let client, let kind = s.spec?.pickKind, !kind.isEmpty, !s.picking else { return }
        s.setPicking(true)
        defer { s.setPicking(false) }
        do {
            // The engine closes its picker after two minutes.
            let r = try await client.post(
                "/api/import/pick-file", body: ["kind": .string(kind)] as [String: JSONValue], as: OnbPickResult.self,
                timeout: 135)
            if !r.path.isEmpty { s.path = r.path } else if !r.error.isEmpty { show(r.error) }
        } catch {
            show(Self.sentence(error))
        }
    }

    /// A Finder button from the source's own list (`ui.js:27-32`).
    func open(_ helper: OnbOpenHelper) async {
        guard let client else { return }
        do {
            _ = try await client.post(
                "/api/import/open", body: ["source": .string(helper.key)] as [String: JSONValue], as: WriteResult.self)
        } catch {
            show(Self.sentence(error))
        }
    }

    /// "Run the import" with the path in the box.
    func runImport(_ s: OnbImportStep) async {
        guard s.canRun else { return }
        s.setRunning(true)
        let r = await run(source: s.key, path: s.path)
        s.setRun(ok: r.ok, line: r.line)
        if r.ok && s.key == "contacts" { contactsImported = true }
        s.setRunning(false)
    }

    /// A finding's own Import button (onboarding.js:124-156).
    func runFinding(_ f: OnbFinding, in s: OnbImportStep) async {
        guard !s.findingBusy.contains(f.id) else { return }
        s.setFinding(f.id, busy: true)
        let r = await run(source: f.source, path: f.path)
        s.setFindingResult(f.id, ok: r.ok, line: r.line)
        if r.ok && f.source == "contacts" { contactsImported = true }
        s.setFinding(f.id, busy: false)
    }

    /// One import, boiled down to what a wizard row can show: did it work, and the single
    /// line worth reading (onboarding.js:107-122). The wait outlasts the engine's own
    /// ceiling, so a first import that is merely slow is never shown as a failure (NC-176).
    private func run(source: String, path: String) async -> (ok: Bool, line: String) {
        guard let client else { return (false, "The engine is not connected.") }
        do {
            // Its own long-lived request, as the Data page's imports: the shared client stops
            // every call after a few minutes whatever its own timeout (DataTransport.swift).
            let d = try await client.longPost(
                "/api/import/run", body: ["source": .string(source), "path": .string(path)] as [String: JSONValue],
                as: OnbImportRun.self, timeout: TimeInterval(importCeiling + Self.importMarginSeconds))
            let lines = d.output.trimmingCharacters(in: .whitespacesAndNewlines)
                .components(separatedBy: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            if d.ok {
                app?.peopleChanged()
                await app?.refreshCounts()
            }
            return (d.ok, lines.last ?? (d.ok ? "Imported" : "Import failed"))
        } catch {
            return (false, Self.sentence(error))
        }
    }

    // MARK: Remember three people

    func loadQueue() async {
        guard queue == nil, let client else { return }
        // A failure is an empty queue, which says what fills it (onboarding.js:459).
        let q = (try? await client.get("/api/describe-queue", as: OnbDescribeQueue.self))?.queue ?? []
        queue = q
        queueIndex = 0
        noteText = ""
    }

    /// For the self-test: a queue of made-up people instead of the engine's three.
    func useQueue(_ people: [OnbQueuePerson]) {
        queue = people
        queueIndex = 0
        noteText = ""
    }

    var currentPerson: OnbQueuePerson? {
        guard let queue, queue.indices.contains(queueIndex) else { return nil }
        return queue[queueIndex]
    }

    /// Save the line as a note and move on; a failure keeps the text (onboarding.js:464-470).
    func saveNote() async {
        let body = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, let person = currentPerson, !body.isEmpty, !reviewBusy else { return }
        reviewBusy = true
        defer { reviewBusy = false }
        do {
            _ = try await client.post(
                "/api/contact/\(person.id)/note", body: ["body": .string(body)] as [String: JSONValue],
                as: NoteCreated.self)
            rememberedCount += 1
            app?.peopleChanged()
            advance()
        } catch {
            show(Self.sentence(error))
        }
    }

    /// Skip marks them reviewed so they leave the queue, and moves on even if that failed
    /// (onboarding.js:472-476).
    func skip() async {
        guard let client, let person = currentPerson, !reviewBusy else { return }
        reviewBusy = true
        defer { reviewBusy = false }
        do {
            _ = try await client.post("/api/contact/\(person.id)/skip", as: WriteResult.self)
        } catch {
            show(Self.sentence(error))
        }
        advance()
    }

    private func advance() {
        queueIndex += 1
        noteText = ""
    }

    // MARK: Finishing

    /// "Save the key and finish" / "Finish": the key first when one was typed; a refused
    /// key keeps the wizard open with the key still in the box (onboarding.js:528-536).
    func saveKeyAndFinish() async {
        guard let client, !finishing else { return }
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        finishing = true
        if !key.isEmpty {
            do {
                let saved = try await client.post(
                    "/api/settings", body: ["key": "ANTHROPIC_API_KEY", "value": .string(key)] as [String: JSONValue],
                    as: OnbSettingSaved.self)
                if let w = saved.warning, !w.isEmpty { show(w) }
            } catch {
                show(Self.sentence(error))
                finishing = false
                return
            }
        }
        await finish()
    }

    /// Marks setup done, then closes, also when the engine refused: the web page says why
    /// and lets the person in either way (onboarding.js:50-54).
    func finish() async {
        finishing = true
        defer { finishing = false }
        if let client {
            do {
                _ = try await client.post("/api/onboarding", body: ["done": true] as [String: JSONValue],
                                          as: OnboardingState.self)
            } catch {
                show(Self.sentence(error))
                try? await Task.sleep(for: .seconds(2))
            }
        }
        guard let app else { return }
        app.showOnboarding = false
        // People loaded under the sheet before setup's imports and before the LinkedIn
        // step asked for the file: reload it, so it shows who setup brought in and the
        // line saying the app is waiting for LinkedIn's file (NC-319).
        app.peopleChanged()
        await app.refreshCounts()
    }

    // MARK: Words

    func show(_ text: String) {
        toast = text
        toastTask?.cancel()
        toastTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            self?.toast = nil
        }
    }

    /// NC-363: plain words, never a route or a status (EngineError keeps those for the log).
    static func sentence(_ error: Error) -> String {
        EngineError.sentence(for: error)
    }

    /// "1,204 contacts" beside a finding (onboarding.js:137-142).
    static func countLabel(_ f: OnbFinding) -> String? {
        guard f.count > 0 else { return nil }
        switch f.source {
        case "whatsapp": return "\(Display.number(f.count)) conversations"
        case "imessage", "contacts": return "\(Display.number(f.count)) contacts"
        default: return nil
        }
    }

    /// "Last exchange · WhatsApp · 3d ago" over the quote (onboarding.js:426-430).
    static func lastExchangeMeta(_ m: OnbLastMessage) -> String {
        "Last exchange · \(Display.channelLabel(m.channel)) · \(Display.relDate(m.ts))"
    }

    /// The quote, cut at 220 characters with an ellipsis.
    static func quote(_ body: String) -> String {
        body.count > 220 ? String(body.prefix(220)) + "…" : body
    }

    /// The first word of the name, for "How do you know Ada?" (onboarding.js:414).
    static func firstName(_ name: String) -> String {
        name.split(separator: " ").first.map(String.init) ?? name
    }
}
