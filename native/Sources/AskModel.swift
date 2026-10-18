import AppKit
import Foundation
import Observation

// The Ask screen's state and every engine call it makes. Kept OUTSIDE the view, one per
// AppModel, so an answer that is still being worked out survives switching to People and
// back: the question keeps running, the elapsed time keeps counting, and the answer lands
// where it was asked. Wording follows static/app/ask.js line for line (contract §8.8).

/// One turn as the screen draws it: a stored ChatMessage, or a live answer with the extra
/// counts only a live answer carries.
struct AskTurn: Identifiable, Sendable {
    enum Role: Sendable { case user, assistant }

    /// Local, for SwiftUI's diffing only.
    let id: Int
    var role: Role
    var content: String
    var contacts: [AskCard] = []
    var truncated = false
    /// Cards removed because that person is hidden now (restored turns only).
    var withheld = 0
    var searched: Searched?
    var trail: [TrailEntry] = []
    var spend: Spend?
    var stopped = false
    var question: AskQuestion?
    // Live answers only: a restored turn does not carry these (contract §5).
    var cardsMatched: Int?
    var totalMatched: Int?
    var context: AskContext?
    /// The request itself failed: there is no answer to read.
    var failure: AskFailure?

    init(id: Int, role: Role, content: String) {
        self.id = id
        self.role = role
        self.content = content
    }

    init(id: Int, message m: ChatMessage) {
        self.id = id
        role = m.role == "user" ? .user : .assistant
        content = m.content
        contacts = m.contacts
        truncated = m.truncated
        withheld = m.contactsWithheld ?? 0
        searched = m.searched
        trail = m.trail ?? []
        spend = m.spend
        stopped = m.stopped ?? false
        question = m.question
    }

    init(id: Int, answer a: ChatAnswer) {
        self.id = id
        role = .assistant
        content = a.reply
        contacts = a.contacts
        truncated = a.truncated
        searched = a.searched
        trail = a.trail
        spend = a.spend
        stopped = a.stopped ?? false
        question = a.question
        cardsMatched = a.cardsMatched
        totalMatched = a.totalMatched
        context = a.context
    }
}

/// Why a question got no answer, kept on its turn so the notice can offer the right action.
struct AskFailure: Sendable {
    /// The plain sentence, from the app's words.
    var message: String
    /// The engine's own text, behind "Show detail" (R8: never a raw error on its own).
    var detail = ""
    /// The app stopped waiting; the answer may still be running in the engine.
    var timedOut: Bool
    var runID: String
    var question: String
    /// "Stop the answer" was pressed on this timed-out turn.
    var stoppedByYou = false
}

enum AskLoad: Equatable {
    case loading
    case loaded
    case failed(String)
}

@MainActor
@Observable
final class AskModel {
    // MARK: One per AppModel

    @ObservationIgnored private static var stores: [ObjectIdentifier: AskModel] = [:]

    static func shared(for app: AppModel) -> AskModel {
        let key = ObjectIdentifier(app)
        if let m = stores[key] { return m }
        let m = AskModel(app: app)
        stores[key] = m
        return m
    }

    @ObservationIgnored let app: AppModel
    private var client: EngineClient { app.client }

    private init(app: AppModel) {
        self.app = app
        self.shape = Self.loadPreference()
    }

    // MARK: Chats rail

    var chats: [ChatRow] = []
    var chatsState: AskLoad = .loading
    var adoption: ChatAdoption?
    var limits = ChatLimits.defaultValue
    /// The adoption receipt was dismissed (per adopted chat, like `ask.js` ADOPT_KEY).
    var adoptionSeen: String = UserDefaults.standard.string(forKey: AskModel.adoptKey) ?? ""

    // MARK: Transcript

    var turns: [AskTurn] = []
    var transcriptState: AskLoad = .loading
    /// The open chat's row (title, shape); nil when there is no chat at all yet.
    var conversation: ChatRow?
    /// The chat on screen, resolved: `app.selectedChatID`, or the newest one's id when that
    /// is nil. 0 = no chat exists yet (the first question creates one).
    var convID = 0
    /// A NAMED chat that is not there (404): the raw text, for "Show detail".
    var gone: String?
    /// Turns older than the newest 500 that the engine did not send.
    var trimmed = 0

    // MARK: Asking

    struct Pending: Equatable {
        let runID: String
        /// The chat the question was asked in (0 = none existed yet).
        let chatID: Int
        let question: String
        let startedAt: Date
        var stopping = false
    }

    var pending: Pending?
    var draft = ""
    /// The chips' values: the preference for a new chat, or the open chat's own shape.
    var shape: Shape
    /// What the engine said applied on the last answer (decides which chips are live).
    var lastShape: ShapeStatus?
    var selfName = ""
    /// Bumped to put the caret in the composer.
    var composerFocus = 0

    // MARK: Messages to the person

    var toast: String?
    var toastGeneration = 0

    /// `--snapshot`: people to tick on one answer when it appears (and whether to open
    /// the add-to-list chooser), so a picture can show a selection.
    struct Pretick {
        let turnID: Int
        let ids: [Int]
        var openChooser = false
    }
    var pretick: Pretick?

    /// Rename and delete targets, for the alert and the confirmation dialog.
    var renaming: ChatRow?
    var renameText = ""
    var deleting: ChatRow?

    @ObservationIgnored private var nextTurnID = 0
    @ObservationIgnored private var transcriptToken = 0
    @ObservationIgnored private var started = false
    /// The selection the transcript on screen was loaded for (nil = newest).
    @ObservationIgnored private var loadedSelection: Int?? = .none

    // MARK: Derived

    var busyHere: Bool { pending.map { $0.chatID == convID } ?? false }
    var busyElsewhere: Bool { pending != nil && !busyHere }
    var aiOff: Bool { app.engine.status?.ai == false }
    var currentChatRow: ChatRow? { chats.first { $0.id == convID } ?? conversation }
    /// Stored messages in the open chat: the rail's count, else the transcript's.
    /// (A question still being answered is on screen before the rail counts it.)
    var currentTurnCount: Int { max(chats.first { $0.id == convID }?.turns ?? 0, turns.filter { $0.failure == nil }.count) }
    var isEmptyChat: Bool {
        transcriptState == .loaded && turns.isEmpty && !busyHere && gone == nil
    }

    // MARK: Loading

    /// First appearance: chats, the transcript and the greeting's name, then settle.
    /// Later appearances refresh quietly and keep what is on screen.
    func appeared() async {
        if started {
            async let c: Void = loadChats()
            if loadedSelection != .some(app.selectedChatID) { await loadTranscript() }
            await c
            return
        }
        started = true
        async let c: Void = loadChats()
        async let t: Void = loadTranscript()
        async let s: Void = loadSelfName()
        _ = await (c, t, s)
        app.markSettled()
    }

    func loadChats() async {
        do {
            let d = try await client.get("/api/chats", as: ChatsResponse.self)
            chats = d.chats
            adoption = d.adoption
            if d.limits.titleMax > 0 { limits = d.limits }
            chatsState = .loaded
        } catch {
            if chats.isEmpty { chatsState = .failed(Self.words(error)) }
        }
    }

    /// The open chat's transcript. Only the newest request may write: switching chats
    /// while one loads never paints the old chat over the new one.
    func loadTranscript() async {
        let selection = app.selectedChatID
        transcriptToken += 1
        let token = transcriptToken
        loadedSelection = .some(selection)
        transcriptState = .loading
        gone = nil
        do {
            let path = selection.map { "/api/chat/\($0)/history" } ?? "/api/chat/history"
            let h = try await client.get(path, as: ChatHistory.self)
            guard token == transcriptToken else { return }
            turns = h.messages.map { AskTurn(id: takeID(), message: $0) }
            conversation = h.conversation
            convID = h.conversation?.id ?? h.conversationID
            trimmed = h.trimmed
            lastShape = nil
            // The chat's own shape, so the chips describe the answers on screen. Never
            // written back to the preference a NEW chat starts from (ask.js:858).
            if let s = h.conversation?.shape, !s.people.isEmpty {
                shape = Shape(
                    people: s.people, detail: s.detail.isEmpty ? shape.detail : s.detail,
                    search: s.search.isEmpty ? shape.search : s.search)
            }
            // The question still being answered in this chat: the engine saves it before it
            // starts answering (api_ask.py, NC-052), so it is usually in the history already.
            // Added only when it is not, in the first instant after sending.
            if let p = pending, p.chatID == convID,
               !(turns.last.map { $0.role == .user && $0.content == p.question } ?? false) {
                turns.append(AskTurn(id: takeID(), role: .user, content: p.question))
            }
            takeSnapshotHint()
            transcriptState = .loaded
        } catch is CancellationError {
            // Ask went off screen mid-load: forget what was being loaded, so coming back
            // loads it again instead of showing this load's spinner for good.
            if token == transcriptToken { loadedSelection = .none }
            return
        } catch {
            guard token == transcriptToken else { return }
            turns = []
            if selection != nil, (error as? EngineError)?.status == 404 {
                // Only a named chat can 404; the newest always resolves (ask.js:869).
                gone = Self.words(error)
                conversation = nil
                convID = 0
                transcriptState = .loaded
            } else {
                transcriptState = .failed(Self.words(error))
            }
        }
    }

    /// `--snapshot` hints: "ask-ticked" ticks three people on the newest answer that names
    /// two or more; "ask-addtolist" does that and opens the add-to-list chooser.
    private func takeSnapshotHint() {
        guard let hint = app.snapshotHint, hint == "ask-ticked" || hint == "ask-addtolist" else { return }
        guard let t = turns.last(where: { $0.role == .assistant && Set($0.contacts.map(\.id)).count >= 2 }) else { return }
        app.snapshotHint = nil
        pretick = Pretick(
            turnID: t.id, ids: Array(ListsAddTo.unique(t.contacts.map(\.id)).prefix(3)),
            openChooser: hint == "ask-addtolist")
    }

    /// Text another screen staged for the composer (an intent, "Ask about…"): put it in
    /// the box and the caret after it, never send it (ask.js:1166-1174).
    func takeDraft() {
        guard !app.askDraft.isEmpty else { return }
        draft = app.askDraft
        app.askDraft = ""
        composerFocus += 1
    }

    /// ⌘K "Start a new chat" (`app.requestNewChat`, ask.js:1184-1190): once the open chat's
    /// transcript is known, because New Chat must know whether this chat is empty already.
    func handleNewChatRequest() async {
        guard app.requestNewChat else { return }
        app.requestNewChat = false
        let deadline = Date().addingTimeInterval(15)
        while transcriptState == .loading && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        await newChat()
        // newChat moves the selection; a screen that is on shows it through its onChange,
        // and a model with no screen (the self-test) reads it here.
        if loadedSelection != .some(app.selectedChatID) { await loadTranscript() }
    }

    /// SELF_NAME for the greeting. The settings payload carries masked secret keys: only
    /// that one value is kept, nothing else is stored or logged.
    private func loadSelfName() async {
        guard let s = try? await client.get("/api/settings", as: SettingsResponse.self) else { return }
        let name = s.value(for: "SELF_NAME") ?? ""
        if !name.isEmpty { selfName = name }
    }

    // MARK: Selecting

    /// The rail's selection: the open chat, without ever rewriting "newest" into an id.
    func select(_ id: Int?) {
        guard let id else { return }
        if app.selectedChatID == nil && id == convID { return }
        if app.selectedChatID != id { app.selectedChatID = id }
    }

    // MARK: Asking

    func send(_ raw: String, fromComposer: Bool) {
        let q = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, pending == nil, gone == nil, transcriptState == .loaded else { return }
        let forChat = convID
        var request = AskRequest(message: q, conversationID: forChat == 0 ? nil : forChat, shape: shape)
        let runID = request.runID.replacingOccurrences(of: "-", with: "")
        request.runID = runID
        // Only the composer's own send empties the composer (NC-159).
        if fromComposer { draft = "" }
        turns.append(AskTurn(id: takeID(), role: .user, content: q))
        pending = Pending(runID: runID, chatID: forChat, question: q, startedAt: Date())
        Task { await self.run(request, question: q, forChat: forChat) }
    }

    private func run(_ request: AskRequest, question q: String, forChat: Int) async {
        do {
            let a = try await client.ask(request)
            // A first question names the chat and every answer moves it to the top.
            await loadChats()
            if convID != forChat {
                // You switched chats while it was answering: it is on disk where it was asked.
                showToast("The answer went to “\(a.conversation?.title ?? "the other chat")”")
            } else {
                if let c = a.conversation, c.id > 0 {
                    convID = c.id
                    conversation = c
                }
                lastShape = a.shape
                turns.append(AskTurn(id: takeID(), answer: a))
                // NC-164: the one turn that needs a reply must not arrive in silence.
                announce(a.question.map { "Ask has a question for you, with \($0.options.count) options" } ?? "Answer ready")
            }
        } catch {
            if convID == forChat {
                var t = AskTurn(id: takeID(), role: .assistant, content: "")
                var timedOut = false
                var plain = Self.words(error)
                var detail = ""
                switch error as? EngineError {
                case .timedOut?:
                    timedOut = true
                case .engine?:
                    // The engine's sentence may be a Python exception: say the fact
                    // plainly (ask.js:1264-1272, R8), and let the detail toggle hold
                    // the engine's own sentence when it is one for people (NC-363: a
                    // crash's text goes to the log, not to the toggle).
                    plain = "The engine could not answer this question. Sending it again can hit the same problem."
                    detail = Self.words(error)
                case .decoding?, .unexpectedAnswer?:
                    plain = "The engine sent an answer this app could not read."
                    detail = Self.words(error)
                default:
                    break
                }
                t.failure = AskFailure(
                    message: plain, detail: detail, timedOut: timedOut,
                    runID: timedOut ? request.runID : "", question: q)
                turns.append(t)
                announce("Ask could not answer")
            }
            // Put the question back unless something new was typed meanwhile (NC-082).
            if draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { draft = q }
            await loadChats()
        }
        pending = nil
        composerFocus += 1
        await app.refreshCounts()
    }

    /// Stop the answer in flight: a second request with the same run id. The first one
    /// stays open and comes back with what was found and the real bill (ask.js:813-824).
    func stop() {
        guard var p = pending, !p.stopping else { return }
        p.stopping = true
        pending = p
        let runID = p.runID
        let client = self.client
        Task { _ = try? await client.stopAsk(runID: runID) }
    }

    /// Stop an answer this screen stopped waiting for (NC-157).
    func stopTimedOut(turnID: Int) {
        guard let i = turns.firstIndex(where: { $0.id == turnID }), let f = turns[i].failure, !f.runID.isEmpty
        else { return }
        turns[i].failure?.stoppedByYou = true
        let client = self.client
        Task { _ = try? await client.stopAsk(runID: f.runID) }
    }

    /// Send a failed question again, in place of the failed turn.
    func retry(turnID: Int) {
        guard pending == nil, let i = turns.firstIndex(where: { $0.id == turnID }), let f = turns[i].failure
        else { return }
        turns.remove(at: i)
        if i > 0, turns[i - 1].role == .user, turns[i - 1].content == f.question { turns.remove(at: i - 1) }
        if draft.trimmingCharacters(in: .whitespacesAndNewlines) == f.question { draft = "" }
        send(f.question, fromComposer: false)
    }

    // MARK: Chats

    func newChat() async {
        // An empty chat you are already in IS what New chat would make (ask.js:1050).
        if convID != 0, isEmptyChat {
            showToast("This chat is empty already")
            composerFocus += 1
            return
        }
        do {
            let pref = Self.loadPreference()
            let body: [String: JSONValue] = [
                "shape": ["people": .string(pref.people), "detail": .string(pref.detail), "search": .string(pref.search)]
            ]
            let d = try await client.post("/api/chats", body: body, as: ChatCreated.self)
            await loadChats()
            if d.conversation.id > 0 {
                app.selectedChatID = d.conversation.id
            } else {
                await loadTranscript()
            }
            composerFocus += 1
            await app.refreshCounts()
        } catch {
            showToast(Self.words(error))
        }
    }

    func beginRename(_ chat: ChatRow) {
        renameText = chat.title
        renaming = chat
    }

    func commitRename(_ chat: ChatRow, to text: String) async {
        renaming = nil
        let typed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty, typed != chat.title else { return }
        do {
            let r = try await client.post(
                "/api/chat/\(chat.id)/rename", body: ["title": JSONValue.string(typed)], as: ChatRenamed.self)
            // The engine normalises names; say so when it stored something else (ask.js:1126).
            let stored = r.title.isEmpty ? typed : r.title
            if chat.id == convID { conversation?.title = stored }
            if stored != typed { showToast("Saved as “\(stored)”") }
            await loadChats()
        } catch {
            showToast(Self.words(error))
        }
    }

    func beginDelete(_ chat: ChatRow) { deleting = chat }

    func commitDelete(_ chat: ChatRow) async {
        deleting = nil
        do {
            let d = try await client.delete("/api/chat/\(chat.id)", as: ChatDeleted.self)
            let n = d.turns
            showToast(n > 0 ? "Chat deleted. \(n) \(n == 1 ? "message stays" : "messages stay") on disk." : "Chat deleted")
            await loadChats()
            if chat.id == convID || chat.id == app.selectedChatID {
                // Land on the newest chat, which is what "no id" means.
                if app.selectedChatID == nil { await loadTranscript() } else { app.selectedChatID = nil }
            }
            await app.refreshCounts()
        } catch {
            showToast(Self.words(error))
        }
    }

    func dismissAdoption() {
        guard let a = adoption else { return }
        adoptionSeen = String(a.conversationID)
        UserDefaults.standard.set(adoptionSeen, forKey: Self.adoptKey)
    }

    // MARK: Shape

    /// A chip was changed: it applies to the next question and becomes the preference for
    /// new chats (ask.js:542-546).
    func pickShape(_ key: String, _ value: String) {
        switch key {
        case "people": shape.people = value
        case "detail": shape.detail = value
        default: shape.search = value
        }
        let dict = ["people": shape.people, "detail": shape.detail, "search": shape.search]
        UserDefaults.standard.set(dict, forKey: Self.shapeKey)
    }

    private static let shapeKey = "nc_ask_shape"
    private static let adoptKey = "nc_ask_adoption_seen"

    private static func loadPreference() -> Shape {
        let d = UserDefaults.standard.dictionary(forKey: shapeKey) as? [String: String] ?? [:]
        let p = Shape.preferred
        return Shape(people: d["people"] ?? p.people, detail: d["detail"] ?? p.detail, search: d["search"] ?? p.search)
    }

    // MARK: Toast

    func showToast(_ text: String) {
        toast = text
        toastGeneration += 1
        let gen = toastGeneration
        Task {
            try? await Task.sleep(for: .seconds(2.6))
            if self.toastGeneration == gen { self.toast = nil }
        }
    }

    // MARK: Helpers

    /// Spoken by VoiceOver when an answer lands; nothing is drawn (ask.js:1395-1403).
    private func announce(_ text: String) {
        guard let element = NSApp.mainWindow ?? NSApp.keyWindow else { return }
        NSAccessibility.post(
            element: element, notification: .announcementRequested,
            userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    private func takeID() -> Int {
        nextTurnID += 1
        return nextTurnID
    }

    static func words(_ error: Error) -> String {
        EngineError.sentence(for: error)
    }
}
