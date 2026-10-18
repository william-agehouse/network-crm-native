import Foundation
import Observation
import os
import SwiftUI

// The shared state every screen reads, injected with `.environment(app)`:
//
//     @AppEnvironment private var app
//
// (not `@Environment(AppModel.self)`, which stops the app when SwiftUI draws a view
// somewhere it did not hand the model to; see AppEnvironment below.)
//
// Screens talk to the engine only through `app.client` (an EngineClient), and move between
// each other only through this model (`openPerson`, `section`), so `--snapshot` can set a
// scene up purely by setting properties here.

@MainActor
@Observable
final class AppModel {
    enum Section: String, CaseIterable, Identifiable, Sendable {
        case people, ask, lists, data, settings
        var id: String { rawValue }
        var title: String {
            switch self {
            case .people: "People"
            case .ask: "Ask"
            case .lists: "Lists"
            case .data: "Data"
            case .settings: "Settings"
            }
        }
        var symbol: String {
            switch self {
            case .people: "person.2"
            case .ask: "bubble.left.and.text.bubble.right"
            case .lists: "list.bullet.rectangle"
            case .data: "tray.full"
            case .settings: "gearshape"
            }
        }
        /// Data and Settings sit apart at the foot of the sidebar: tools, not places.
        var isTool: Bool { self == .data || self == .settings }
    }

    let engine: EngineController
    /// The one way to reach the engine. Valid from launch; calls fail with
    /// `EngineError.unreachable` until `engine.isReady`.
    var client: EngineClient { engine.client }

    var section: Section = .people

    // People
    /// The People search field. Setting it from elsewhere (a snapshot, Ask) searches.
    var peopleSearch = ""
    /// The person whose page People shows; nil = the table alone.
    var selectedPersonID: Int?
    /// Bumped by ⌘F; the People screen focuses its search field when this changes.
    var searchFocusRequest = 0
    /// Set by ⌘F until People has put the caret in its search field, so a ⌘F pressed on
    /// another screen still lands once People appears, and only that once.
    var searchFocusPending = false
    /// Bumped after a person's page saves anything; the People table refetches when it
    /// changes, so a renamed, hidden or deleted person is not left stale in the rows.
    private(set) var peopleRevision = 0
    /// Per person: bumped when their page must be read again from scratch (another person
    /// was merged into them). PersonPage keys its load on it.
    private(set) var personReloads: [Int: Int] = [:]

    // Ask
    /// The open chat; nil = the newest one (never rewrite nil into an id: contract §8.8).
    var selectedChatID: Int?
    /// Text to put in the Ask composer without sending (intent chips, "Ask about…").
    var askDraft = ""

    // Lists
    var selectedListID: Int?

    // Sheets the window presents (RootView). Each sheet's view belongs to one screen's
    // files; anything may open one by setting these.
    /// ⌘K: the command bar (CommandBar*.swift).
    var showCommandBar = false
    /// First-run setup (Onboarding*.swift). `OnboardingGate` decides on launch; the Help
    /// menu opens it again.
    var showOnboarding = false
    /// Merge two people (Merge*.swift): from a person's page, the duplicates list or ⌘K.
    var mergeRequest: MergeRequest?
    /// Full Disk Access, Gmail, or pasting a list of people (SetupFlow*.swift).
    var setupFlow: SetupFlow?
    /// File > Add Person…: the People screen shows its own add sheet while this is true.
    var showAddPerson = false

    // Requests from ⌘K and the menus that a screen carries out and then clears.
    /// A view of People to switch to. Nil fields keep what is set.
    var peopleRequest: PeopleRequest?
    /// Which part of the Data page to open on: "duplicates", "export", or an import row's
    /// key from /api/data/ledger (e.g. "linkedin_facts").
    var dataFocus: String?
    /// ⌘K "Add a note about X": People opens that person with the caret in the note box,
    /// then sets it back to nil.
    var noteComposerRequest: Int?
    /// Ask starts a new chat / Lists starts a new list, then sets it back to false.
    var requestNewChat = false
    var requestNewList = false
    /// Development only: `--snapshot` sets it so a screen can open a panel on appear, for a
    /// picture of a state that otherwise needs a click. A screen reads it once and clears it.
    var snapshotHint: String?

    // Sidebar counts
    /// From /api/status: everyone not hidden or deleted.
    private(set) var peopleCount: Int?
    private(set) var chatCount: Int?
    private(set) var listCount: Int?

    /// True once the visible screen has drawn its first real content (or its error).
    /// Screens call `markSettled()` after their first load; `--snapshot` waits for it.
    private(set) var isSettled = false

    @ObservationIgnored private var pollTask: Task<Void, Never>?
    /// Whether first-run setup has been asked about this session (once the engine answered).
    @ObservationIgnored private var onboardingChecked = false

    init(engine: EngineController) {
        self.engine = engine
        Self.latest = self
    }

    /// The model made last. In the app there is only one; in --snapshot, the scene being
    /// drawn. What `AppEnvironment` falls back on.
    static weak var latest: AppModel?

    // MARK: Moving between screens

    /// Show a person's page in People, from anywhere (an Ask card, a list member).
    func openPerson(_ id: Int) {
        selectedPersonID = id
        section = .people
    }

    func openList(_ id: Int) {
        selectedListID = id
        section = .lists
    }

    func openChat(_ id: Int?) {
        selectedChatID = id
        section = .ask
    }

    /// A person was edited, hidden, deleted or restored somewhere: refresh the table.
    func peopleChanged() { peopleRevision += 1 }

    /// Someone else's notes, numbers and messages moved onto this person (a merge): their
    /// open page reads them again (people.js:1452 reloads the kept person).
    func reloadPerson(_ id: Int) { personReloads[id, default: 0] += 1 }

    /// ⌘F: go to People and focus its search field.
    func focusSearch() {
        section = .people
        searchFocusPending = true
        searchFocusRequest += 1
    }

    /// Screens call this once their first load finished (data or error is on screen).
    func markSettled() { isSettled = true }

    /// For a screen that reloads from scratch (or a snapshot moving to another scene).
    func resetSettled() { isSettled = false }

    // MARK: Engine lifecycle

    /// Connect (starting an engine if needed), then keep the sidebar counts and status fresh.
    func start() async {
        await engine.connect()
        await afterConnect()
        startPolling()
    }

    func retry() async {
        await engine.reconnect()
        await afterConnect()
    }

    /// Counts, and the first-run question once per session: also after a Retry, when the
    /// first connect failed (a fresh Mac where Network CRM had never been opened).
    private func afterConnect() async {
        guard engine.isReady else { return }
        await refreshCounts()
        if !onboardingChecked {
            onboardingChecked = true
            await OnboardingGate.check(self)
        }
    }

    /// Counts for the sidebar. People comes from /api/status (no need to load thousands of rows);
    /// chats and lists are one small request each.
    func refreshCounts() async {
        if let s = await engine.refreshStatus() { peopleCount = s.peopleCount }
        guard engine.isReady else { return }
        async let chats = try? client.get("/api/chats", as: ChatsResponse.self)
        async let lists = try? client.get("/api/lists", as: ListsResponse.self)
        if let c = await chats { chatCount = c.total }
        if let l = await lists { listCount = l.lists.count }
    }

    /// "I've read this" on the database-moved-aside banner. A POST, because it changes what
    /// the engine remembers: as a GET (what it used to be) anything that merely loaded the
    /// address would have cleared the notice unread (NC-342). Returns nil when it worked,
    /// else a sentence saying why not, for the banner to show under its own text.
    func dismissRecoveryNotice() async -> String? {
        var problem: String?
        do {
            _ = try await client.post("/api/db-recovery-notice/dismiss", as: WriteResult.self)
        } catch is CancellationError {
        } catch {
            problem = "The notice could not be cleared. \(PersonStore.say(error))"
        }
        await refreshCounts()
        return problem
    }

    /// The web page polls status every 15 s while visible (`main.js:637-647`); while the
    /// engine is not answering, every 5 s, so the banner goes as soon as it is back.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                let wait = self?.engine.trouble == nil ? 15 : 5
                try? await Task.sleep(for: .seconds(wait))
                guard let self, !Task.isCancelled else { return }
                if self.engine.isReady { await self.refreshCounts() }
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
}

/// What a merge starts from. Both nil = pick both people in the sheet.
struct MergeRequest: Identifiable, Equatable, Sendable {
    /// The person the merge was started from (a person's page, a duplicates group).
    var firstID: Int?
    /// The other person, when already known (a duplicates group names both).
    var secondID: Int?
    var id: String { "\(firstID ?? 0)-\(secondID ?? 0)" }
}

/// The setup sheets the Data page and ⌘K open.
enum SetupFlow: String, Identifiable, Sendable {
    case fullDiskAccess, gmail, paste
    var id: String { rawValue }
}

/// A view of People asked for from elsewhere (⌘K). Values are the web page's own words
/// for them (static/app/people.js and main.js COMMANDS): sort "closeness", "quiet",
/// "recent", "name"; filter "all", "todo", "described", "excluded", "deleted"; location,
/// role and topic are facet values from /api/facets. Nil keeps the current setting.
struct PeopleRequest: Equatable, Sendable {
    var sort: String?
    var filter: String?
    var location: String?
    var role: String?
    var topic: String?
}

/// `@Environment(AppModel.self)` without the crash. Collapsing the sidebar on Lists stopped
/// the app with "No Observable object of type AppModel found" (NC-359): SwiftUI redrew a
/// view somewhere the window had not handed the model to. There is one model per window,
/// so a view that finds none takes that one, and the first time it happens the system log
/// gets a line naming the view (Console, process NetworkCRMNative, "environment").
@propertyWrapper
struct AppEnvironment: DynamicProperty {
    @Environment(AppModel.self) private var injected: AppModel?

    @MainActor var wrappedValue: AppModel {
        if let injected { return injected }
        AppEnvironmentLog.missing()
        return AppModel.latest ?? AppModel(engine: EngineController(port: 1, isDevelopmentPort: true))
    }
}

@MainActor
enum AppEnvironmentLog {
    private static var told = false
    private static let log = Logger(subsystem: "io.github.william-agehouse.networkcrm.native", category: "environment")

    static func missing() {
        guard !told else { return }
        told = true
        let where_ = Thread.callStackSymbols.prefix(12).joined(separator: " | ")
        log.fault("A view was drawn without the app model; used the window's own. Stack: \(where_, privacy: .public)")
    }
}
