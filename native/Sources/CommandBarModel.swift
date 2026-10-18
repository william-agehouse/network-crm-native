import AppKit
import Foundation
import Observation

// ⌘K: the command bar's state, its commands and every engine call it makes. The view
// (CommandBar.swift) only draws this. Rebuilds the omnibox in static/app/main.js:138-590:
// names jump to a person, a question goes to Ask, commands run. An empty box lists the
// everyday commands, ">" narrows to commands only, and anything else is a search where a
// command only shows when you typed one of its keywords.
//
//   GET /api/contacts?q=&sort=closeness&limit=6     people whose name, company or role match
//   GET /api/search/conversations?q=&mode=keyword   archived messages (FTS), first 4
//   GET /api/search/semantic?q=                     people by meaning (the "by meaning" row)
//   GET /api/search/conversations?q=&mode=semantic  conversations by meaning
//   GET /api/locations, GET /api/facets             the per-city / role / topic commands
//   GET /api/chats                                  the recent-chat commands
//   GET /api/contact/{id}                           the open person, for their own commands

// MARK: - Engine answers this file decodes (prefixed so names never clash)

/// One hit of `/api/search/conversations` (`api_messages.py:305-415`). A message you sent
/// to a group has no person of its own: `contactID` is 0 and `conversation` names the
/// group, `openID` the person whose page shows it (or nil: nothing to open).
struct CmdConversationHit: Codable, Sendable, Hashable {
    @Def var contactID: Int
    @Def var contactName: String
    @Def var conversation: String?
    @Def var openID: Int?
    @Def var channel: String
    @Def var ts: String
    /// Keyword hits carry `<b>…</b>` around the match.
    @Def var snippet: String
    @Def var score: Double?

    enum CodingKeys: String, CodingKey {
        case conversation, channel, ts, snippet, score
        case contactID = "contact_id", contactName = "contact_name", openID = "open_id"
    }
}

struct CmdConversationsResponse: Codable, Sendable, Hashable {
    /// false = nothing imported yet, or meaning search not installed: show `message`.
    @Def var available: Bool?
    @Def var conversations: [CmdConversationHit]
    @Def var query: String
    @Def var mode: String
    @Def var message: String?
}

// MARK: - Commands

/// What running a command does, as data, so the self-test can check the mapping without
/// pressing anything. `CommandBarRunner.perform` carries it out.
enum CmdAction: Equatable, Sendable {
    /// People, with its add sheet (main.js:155-156 `people?new=1`).
    case addPerson
    /// The paste-a-list sheet (main.js:160-162 `intake`).
    case paste
    /// A view of People (main.js `people?sort=quiet` and friends).
    case people(PeopleRequest)
    case section(AppModel.Section)
    /// Ask, starting a new chat there (main.js:174-175 `ask?new=1`).
    case newChat
    case merge(MergeRequest)
    case findDuplicates
    case exportBackup
    /// Lists, starting a new list there (main.js:187-188 `lists?new=1`).
    case newList
    /// "dark", "light" or "system" (main.js:116-136).
    case appearance(String)
    /// The Data page opened on one row (main.js:205-207 `data?row=linkedin_facts`).
    case dataFocus(String)
    /// Finder, showing the data folder (main.js:210-212 `openTarget("data")`).
    case revealDataFolder
    /// Put text in Ask's composer without sending (ask.js:48-58, main.js:213-223).
    case askDraft(String)
    /// Ask this question at once in the newest chat (main.js:474, ask.js:1028-1036).
    case askSend(String)
    case openPerson(Int)
    /// The person's page, ready for a note (main.js:255-256 `people/{id}?note=1`).
    case noteAbout(Int)
    /// Copy an email or phone number (main.js:233-236). `noun` is "email" or "number".
    case copy(value: String, noun: String)
    case openChat(Int)
}

struct CmdCommand: Identifiable, Equatable, Sendable {
    let id: String
    let label: String
    /// SF Symbol standing in for the web page's icon (static/app/icons.js).
    let symbol: String
    /// The grey word on the right: where it goes.
    let hint: String
    /// Words someone would reach for, matched as prefixes (main.js:144-147). Lower case.
    let keys: [String]
    /// Kept out of the bare list, one word away (main.js:149-153).
    var typedOnly = false
    let action: CmdAction
}

/// A row of the results, in the order main.js:449-459 builds them.
enum CmdRow: Identifiable, Equatable {
    case person(PersonSummary)
    case convo(CmdConversationHit, index: Int)
    case cmd(CmdCommand)
    case meaning
    case ask(String)

    var id: String {
        switch self {
        case .person(let p): "p\(p.id)"
        case .convo(_, let i): "cv\(i)"
        case .cmd(let c): "cmd-\(c.id)"
        case .meaning: "meaning"
        case .ask: "ask"
        }
    }
}

enum CommandCatalog {
    /// Ask's four intent chips (ask.js:53-58). Copied, not shared: AskScreen keeps its own
    /// private list, and the two are the same four lines.
    static let intents: [(label: String, prefill: String)] = [
        ("Find introductions", "Who could introduce me to "),
        ("Overdue contacts", "Who should I contact this month?"),
        ("Prepare a meeting", "What do I know about "),
        ("Find expertise", "Who in my network knows about "),
    ]

    /// The COMMANDS array (main.js:154-224), in its authored order: the ranking keeps
    /// that order for ties, so an empty box reads as written. `isDark` names the theme
    /// toggle the way main.js:189-191 does.
    static func base(isDark: Bool) -> [CmdCommand] {
        var out: [CmdCommand] = [
            CmdCommand(id: "add", label: "Add someone new", symbol: "plus", hint: "People",
                       keys: ["add", "new", "someone", "person", "contact", "create"], action: .addPerson),
            CmdCommand(id: "paste", label: "Paste a list of people", symbol: "pencil.tip", hint: "Intake",
                       keys: ["paste", "intake", "bio", "signature", "batch", "several", "many", "add", "clipboard", "parse"],
                       action: .paste),
            CmdCommand(id: "quiet", label: "Who I've gone quiet with", symbol: "clock", hint: "People",
                       keys: ["quiet", "quietest", "neglected", "overdue", "cold", "stale", "silent", "forgotten"],
                       action: .people(PeopleRequest(sort: "quiet"))),
            CmdCommand(id: "todo", label: "People still to describe", symbol: "pencil", hint: "People",
                       keys: ["describe", "todo", "undescribed", "blank", "unknown"],
                       action: .people(PeopleRequest(filter: "todo"))),
            CmdCommand(id: "ask", label: "Ask your network a question", symbol: "bubble.left.and.text.bubble.right",
                       hint: "Ask", keys: ["ask", "question", "chat", "answer", "why", "wonder"], action: .section(.ask)),
            CmdCommand(id: "newchat", label: "Start a new chat", symbol: "plus", hint: "Ask",
                       keys: ["chat", "new", "thread", "fresh", "clean", "restart"], action: .newChat),
            CmdCommand(id: "data", label: "Import & sync data", symbol: "arrow.triangle.2.circlepath", hint: "Data",
                       keys: ["import", "sync", "data", "refresh", "messages", "gmail", "linkedin", "imessage", "whatsapp"],
                       action: .section(.data)),
            CmdCommand(id: "merge", label: "Merge two people", symbol: "square.stack.3d.up", hint: "Data",
                       keys: ["merge", "combine", "fold", "same"], action: .merge(MergeRequest())),
            CmdCommand(id: "dups", label: "Find duplicates", symbol: "person.2.square.stack", hint: "Data",
                       keys: ["duplicate", "duplicates", "dupe", "dup", "tidy", "clean"], action: .findDuplicates),
            CmdCommand(id: "export", label: "Export a backup", symbol: "square.and.arrow.down", hint: "Data",
                       keys: ["export", "backup", "download", "csv", "json", "markdown", "save"], action: .exportBackup),
            CmdCommand(id: "lists", label: "Open your lists", symbol: "list.bullet.rectangle", hint: "Lists",
                       keys: ["list", "lists", "group", "segment"], action: .section(.lists)),
            CmdCommand(id: "newlist", label: "Start a new list", symbol: "plus", hint: "Lists",
                       keys: ["list", "new", "dinner", "invite", "event", "pipeline"], action: .newList),
            CmdCommand(id: "theme", label: isDark ? "Switch to light mode" : "Switch to dark mode",
                       symbol: isDark ? "sun.max" : "moon", hint: "Appearance",
                       keys: ["dark", "light", "theme", "mode", "night"], action: .appearance(isDark ? "light" : "dark")),
            CmdCommand(id: "settings", label: "Open settings", symbol: "gearshape", hint: "Settings",
                       keys: ["settings", "preferences", "config", "key", "model", "api"], action: .section(.settings)),

            // ── tail: one word away, never in the bare list (main.js:195-223) ──
            CmdCommand(id: "recent", label: "Who I've been talking to lately", symbol: "bolt", hint: "People",
                       keys: ["recent", "recently", "active", "lately", "talking", "warm"], typedOnly: true,
                       action: .people(PeopleRequest(sort: "recent"))),
            CmdCommand(id: "deleted", label: "Deleted people", symbol: "trash", hint: "People",
                       keys: ["deleted", "trash", "removed", "restore", "bin"], typedOnly: true,
                       action: .people(PeopleRequest(filter: "deleted"))),
            CmdCommand(id: "excluded", label: "People you've excluded", symbol: "nosign", hint: "People",
                       keys: ["excluded", "exclude", "hidden", "blocked", "ignored"], typedOnly: true,
                       action: .people(PeopleRequest(filter: "excluded"))),
            CmdCommand(id: "lifacts", label: "Pull new LinkedIn profile facts", symbol: "globe", hint: "Data",
                       keys: ["linkedin", "profile", "facts", "enrich", "cities", "scrape", "pull"], typedOnly: true,
                       action: .dataFocus("linkedin_facts")),
            CmdCommand(id: "auto", label: "Follow my Mac's appearance", symbol: "circle.lefthalf.filled", hint: "Appearance",
                       keys: ["auto", "system", "mac", "theme", "appearance", "follow"], typedOnly: true,
                       action: .appearance("system")),
            CmdCommand(id: "finder", label: "Show my data folder in Finder", symbol: "folder", hint: "This Mac",
                       keys: ["finder", "folder", "file", "reveal", "where", "disk", "backup"], typedOnly: true,
                       action: .revealDataFolder),
        ]
        // main.js:217-223: one command per intent, prefilling the composer like the chip.
        for (i, it) in intents.enumerated() {
            let trimmed = it.prefill.replacingOccurrences(of: "\\s+$", with: "", options: .regularExpression)
            let label = "Ask: \(trimmed)\(it.prefill.hasSuffix(" ") ? "…" : "")"
            let letters = it.prefill.lowercased().replacingOccurrences(of: "[^a-z ]", with: "", options: .regularExpression)
            let keys = ["ask"] + words(it.label.lowercased())
                + words(letters).filter { $0.count > 3 }
            out.append(CmdCommand(id: "intent\(i)", label: label, symbol: "bubble.left.and.text.bubble.right",
                                  hint: "Ask", keys: keys, typedOnly: true, action: .askDraft(it.prefill)))
        }
        return out
    }

    /// Commands that only exist with a person open (main.js:243-259). Merging from their
    /// page keeps THEM as the record that survives. "Ask what I know about" is the native
    /// app's addition: Ask's "Prepare a meeting" intent with the name already in.
    static func person(_ c: PersonDetail) -> [CmdCommand] {
        var out: [CmdCommand] = [
            CmdCommand(id: "pmerge", label: "Merge \(c.name) with…", symbol: "square.stack.3d.up", hint: "This person",
                       keys: ["merge", "duplicate", "dupe", "combine", "fold"], action: .merge(MergeRequest(firstID: c.id))),
            CmdCommand(id: "pnote", label: "Add a note about \(c.name)", symbol: "pencil", hint: "This person",
                       keys: ["note", "remember", "write", "jot", "log"], action: .noteAbout(c.id)),
            CmdCommand(id: "pask", label: "Ask what I know about \(c.name)", symbol: "bubble.left.and.text.bubble.right",
                       hint: "This person", keys: ["ask", "know", "about", "prepare", "meeting", "brief"],
                       action: .askDraft("What do I know about \(c.name)")),
        ]
        // main.js:228-231, 244-251: email → "email", phone → "number".
        let nouns = ["email": ("envelope", "email"), "phone": ("phone", "number")]
        for (i, ident) in c.identifiers.enumerated() {
            guard let (symbol, noun) = nouns[ident.kind], !ident.value.isEmpty else { continue }
            out.append(CmdCommand(id: "pcopy\(i)", label: "Copy \(c.name)'s \(noun)", symbol: symbol, hint: ident.value,
                                  keys: ["copy", noun, ident.kind, "clipboard"], typedOnly: true,
                                  action: .copy(value: ident.value, noun: noun)))
        }
        return out
    }

    /// One command per city, role and topic the network contains (main.js:266-288), so
    /// "Paris" finds who you know there: the people search matches only name, company
    /// and position. All one word away. The same topic in two casings is kept once, in
    /// its most common spelling (the first).
    static func facets(locations: [String], roles: [String], topics: [String]) -> [CmdCommand] {
        func firstOfEach(_ values: [String]) -> [String] {
            var seen = Set<String>()
            return values.filter { seen.insert($0.lowercased()).inserted }
        }
        func make(_ value: String, _ label: String, _ symbol: String, _ kind: String, _ extra: [String],
                  _ request: PeopleRequest) -> CmdCommand {
            let parts = value.lowercased().split(whereSeparator: { " \t\n,/-".contains($0) }).map(String.init)
            return CmdCommand(id: "\(kind):\(value)", label: label, symbol: symbol, hint: "People",
                              keys: parts + extra, typedOnly: true, action: .people(request))
        }
        return firstOfEach(locations).map {
            make($0, "People in \($0)", "mappin.and.ellipse", "loc", ["in", "city", "where"], PeopleRequest(location: $0))
        } + firstOfEach(roles).map {
            make($0, "\($0) in my network", "briefcase", "role", ["role", "job"], PeopleRequest(role: $0))
        } + firstOfEach(topics).map {
            make($0, "People who know \($0)", "tag", "topic", ["topic", "about", "knows"], PeopleRequest(topic: $0))
        }
    }

    /// The native app's addition: the recent chats, one word away, so "⌘K, dinner" reopens
    /// the chat that started with a dinner question. Most recent first, at most `cap`.
    static let recentChatCap = 8
    static func chats(_ rows: [ChatRow], now: Date = Date()) -> [CmdCommand] {
        rows.prefix(recentChatCap).map { chat in
            let title = chat.title.isEmpty ? "New chat" : chat.title
            let when = Display.relDate(chat.lastMessageAt ?? chat.updatedAt, now: now)
            let titleWords = words(title.lowercased().replacingOccurrences(of: "[^\\p{L}\\p{N} ]", with: " ", options: .regularExpression))
            return CmdCommand(id: "chat\(chat.id)", label: "Open chat: \(title)", symbol: "bubble.left.and.bubble.right",
                              hint: when.isEmpty ? "Ask" : when, keys: ["chat", "chats", "recent"] + titleWords,
                              typedOnly: true, action: .openChat(chat.id))
        }
    }

    private static func words(_ s: String) -> [String] {
        s.split(whereSeparator: \.isWhitespace).map(String.init)
    }

    // MARK: Ranking (main.js:295-325)

    /// 3 = a whole keyword · 2 = a keyword's prefix · 1 = somewhere in the label · 0 = no
    /// match. Every word of a multi-word query has to land. An empty needle scores 1.
    static func score(_ cmd: CmdCommand, _ needle: String) -> Int {
        if needle.isEmpty { return 1 }
        let label = cmd.label.lowercased()
        var worst = 3
        for w in words(needle) {
            var best = 0
            for k in cmd.keys {
                if k == w { best = 3; break }
                if k.hasPrefix(w) { best = max(best, 2) }
            }
            if best == 0 && label.contains(w) { best = 1 }
            if best == 0 { return 0 }
            worst = min(worst, best)
        }
        return worst
    }

    /// Best match first, then the authored order. `min` 1 shows everything that matches at
    /// all, 2 demands a real keyword; a typed-only command always needs a real keyword,
    /// which is what keeps it out of a bare box.
    static func rank(_ cmds: [CmdCommand], _ needle: String, min floor: Int) -> [CmdCommand] {
        struct Scored {
            let cmd: CmdCommand
            let index: Int
            let score: Int
        }
        var scored: [Scored] = []
        for (i, c) in cmds.enumerated() {
            let s = score(c, needle)
            let bar = c.typedOnly ? max(floor, 2) : floor
            if s >= bar { scored.append(Scored(cmd: c, index: i, score: s)) }
        }
        scored.sort { a, b in a.score != b.score ? a.score > b.score : a.index < b.index }
        return scored.map(\.cmd)
    }

    /// What the box shows for `raw` (main.js:442-459): an empty box or ">" lists commands;
    /// anything else is people, then conversations, then commands you named, then the two
    /// fallbacks.
    static func rows(raw: String, people: [PersonSummary], convos: [CmdConversationHit],
                     commands: [CmdCommand]) -> [CmdRow] {
        let q = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if q.isEmpty || q.hasPrefix(">") {
            let needle = q.hasPrefix(">") ? String(q.dropFirst()).trimmingCharacters(in: .whitespaces).lowercased() : ""
            return rank(commands, needle, min: 1).map { .cmd($0) }
        }
        var out: [CmdRow] = people.map { .person($0) }
        out += convos.enumerated().map { .convo($0.element, index: $0.offset) }
        out += rank(commands, q.lowercased(), min: 2).map { .cmd($0) }
        out.append(.meaning)
        out.append(.ask(q))
        return out
    }

    static func isCommandMode(_ raw: String) -> Bool {
        let q = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return q.isEmpty || q.hasPrefix(">")
    }
}

// MARK: - The store

@MainActor
@Observable
final class CommandBarModel {
    var query = "" {
        didSet {
            guard query != oldValue else { return }
            selection = 0
            queryChanged()
        }
    }
    /// The highlighted row.
    var selection = 0
    /// True when the last move came from the arrow keys: only then does the list scroll to
    /// follow it (main.js:347-355), so a hovering mouse never fights the scroll.
    var keyboardMoved = false

    private(set) var people: [PersonSummary] = []
    private(set) var convos: [CmdConversationHit] = []
    /// "keyword", or "semantic" after the by-meaning row ran.
    private(set) var convoMode = "keyword"
    private(set) var meaningBusy = false
    /// "Nothing imported yet" / "needs a one-time install": shown above the results.
    private(set) var hint = ""

    private(set) var facetCommands: [CmdCommand] = []
    private(set) var chatCommands: [CmdCommand] = []
    /// The open person, for their own commands and the context chip.
    private(set) var contextPerson: PersonDetail?
    private(set) var contextText = "People"

    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var searchTask: Task<Void, Never>?
    @ObservationIgnored private var searchToken = 0
    /// The web page waits 180 ms after the last keystroke before searching (main.js:385-398).
    nonisolated static let debounce: Duration = .milliseconds(180)
    /// main.js:389 keeps 6 people and :394 4 conversations; meaning search 6 of each (:410-414).
    nonisolated static let peopleShown = 6
    nonisolated static let convosShown = 4
    nonisolated static let meaningShown = 6

    private var client: EngineClient? { app?.client }

    // MARK: Rows

    var isCommandMode: Bool { CommandCatalog.isCommandMode(query) }

    var commands: [CmdCommand] {
        let isDark = Self.isDarkNow()
        let person = contextPerson.map { CommandCatalog.person($0) } ?? []
        return person + CommandCatalog.base(isDark: isDark) + facetCommands + chatCommands
    }

    var rows: [CmdRow] {
        CommandCatalog.rows(raw: query, people: people, convos: convos, commands: commands)
    }

    /// The appearance the window shows now: the stored choice, or the Mac's own.
    static func isDarkNow() -> Bool {
        switch UserDefaults.standard.string(forKey: "appearance") ?? "system" {
        case "dark": return true
        case "light": return false
        default: return NSApp?.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        }
    }

    // MARK: Loading

    /// Once per opening: the facet vocabulary, the chats and the open person. Each failure
    /// just leaves its commands out (main.js:361-368).
    func attach(_ app: AppModel) { self.app = app }

    func start() async {
        guard let app, let client else { return }
        // The context chip (main.js:370-383): the open person's name, else the section.
        let section = app.section
        let personID = section == .people ? app.selectedPersonID : nil
        contextText = personID == nil ? section.title : "People"
        async let locations = try? client.get("/api/locations", as: LocationsResponse.self)
        async let facets = try? client.get("/api/facets", as: FacetsResponse.self)
        async let chats = try? client.get("/api/chats", as: ChatsResponse.self)
        async let person: PersonDetail? = {
            guard let personID else { return nil }
            return try? await client.get("/api/contact/\(personID)", as: PersonDetail.self)
        }()
        let (l, f, c, p) = await (locations, facets, chats, person)
        facetCommands = CommandCatalog.facets(
            locations: l?.locations ?? [], roles: f?.roles ?? [], topics: f?.topics ?? [])
        chatCommands = CommandCatalog.chats(c?.chats ?? [])
        if personID != nil {
            contextPerson = p
            contextText = p.map { $0.name.isEmpty ? "Person" : $0.name } ?? "Person"
        }
        if query.trimmingCharacters(in: .whitespaces).isEmpty || isCommandMode { app.markSettled() }
    }

    // MARK: Searching

    private func queryChanged() {
        searchTask?.cancel()
        searchToken += 1
        let token = searchToken
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        // ">" is ours: no search for people called ">merge" (main.js:516-519).
        if q.isEmpty || q.hasPrefix(">") {
            people = []
            convos = []
            hint = ""
            convoMode = "keyword"
            return
        }
        searchTask = Task { [weak self] in
            try? await Task.sleep(for: Self.debounce)
            guard !Task.isCancelled else { return }
            await self?.search(q, token: token)
        }
    }

    private func search(_ q: String, token: Int) async {
        guard let client else { return }
        // Closest first. main.js:388 sends no sort, which makes the engine order by the
        // LinkedIn column alone (contract §3, trap 5); closeness is what People shows.
        async let ppl: [PersonSummary] = {
            let d = try? await client.get(
                "/api/contacts", query: ["q": q, "sort": "closeness", "limit": String(Self.peopleShown)],
                as: ContactsResponse.self)
            return Array((d?.contacts ?? []).prefix(Self.peopleShown))
        }()
        async let cnv: (list: [CmdConversationHit], message: String) = {
            guard let d = try? await client.get(
                "/api/search/conversations", query: ["q": q, "mode": "keyword"], as: CmdConversationsResponse.self)
            else { return ([], "") }
            if d.available == false { return ([], d.message ?? "") }
            return (Array(d.conversations.prefix(Self.convosShown)), "")
        }()
        let (p, c) = await (ppl, cnv)
        guard token == searchToken else { return }
        people = p
        convos = c.list
        hint = c.message
        convoMode = "keyword"
        app?.markSettled()
    }

    /// The "Search conversations by meaning" row (main.js:400-423): people AND
    /// conversations by meaning. Keeps the keyword people when meaning finds none. The
    /// box stays open with the new results.
    func searchByMeaning() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, !q.isEmpty, !meaningBusy else { return }
        meaningBusy = true
        defer { meaningBusy = false }
        let token = searchToken
        // The first meaning search after the engine starts loads a model (about 8 s).
        async let ppl: (list: [PersonSummary], message: String) = {
            guard let d = try? await client.get(
                "/api/search/semantic", query: ["q": q], as: SemanticSearchResponse.self, timeout: 90)
            else { return ([], "") }
            if !d.available { return ([], d.message ?? "") }
            return (Array(d.contacts.prefix(Self.meaningShown)), "")
        }()
        async let cnv: (list: [CmdConversationHit], message: String) = {
            guard let d = try? await client.get(
                "/api/search/conversations", query: ["q": q, "mode": "semantic"], as: CmdConversationsResponse.self,
                timeout: 90)
            else { return ([], "") }
            if d.available == false { return ([], d.message ?? "") }
            return (Array(d.conversations.prefix(Self.meaningShown)), "")
        }()
        let (p, c) = await (ppl, cnv)
        guard token == searchToken else { return }
        if !p.list.isEmpty { people = p.list }
        convos = c.list
        hint = p.message.isEmpty ? c.message : p.message
        convoMode = "semantic"
    }

    // MARK: Moving

    func move(_ delta: Int) {
        let count = rows.count
        guard count > 0 else { return }
        keyboardMoved = true
        lastKeyMove = Date()
        selection = min(max(min(selection, count - 1) + delta, 0), count - 1)
    }

    /// The mouse entered a row. Ignored just after an arrow key: scrolling to follow the
    /// keyboard slides a new row under a mouse that never moved.
    func hover(_ index: Int) {
        if keyboardMoved, Date().timeIntervalSince(lastKeyMove) < 0.4 { return }
        keyboardMoved = false
        selection = index
    }

    @ObservationIgnored private var lastKeyMove = Date.distantPast

    /// What Return runs: the highlighted row, else the first (main.js:489).
    var current: CmdRow? {
        let r = rows
        return r.indices.contains(selection) ? r[selection] : r.first
    }

    /// Where a row goes, or nil for a row that only changes the results (by meaning) or
    /// leads nowhere (a group message nobody's page shows, main.js:466-472).
    static func action(for row: CmdRow) -> CmdAction? {
        switch row {
        case .person(let p): return .openPerson(p.id)
        case .convo(let c, _):
            // `open_id || contact_id` (main.js:470): a 0 in either is "nobody", as in JS.
            let open = c.openID.flatMap { $0 > 0 ? $0 : nil }
            let to = open ?? (c.contactID > 0 ? c.contactID : nil)
            return to.map { .openPerson($0) }
        case .cmd(let c): return c.action
        case .meaning: return nil
        case .ask(let q): return .askSend(q)
        }
    }

    // MARK: Words

    /// The "Conversations" header (main.js:559).
    var convoHeader: String { convoMode == "semantic" ? "Conversations · by meaning" : "Conversations" }

    /// A conversation row's title (main.js:554): "You, in {group}" or their name.
    static func convoTitle(_ c: CmdConversationHit) -> String {
        if let g = c.conversation, !g.isEmpty { return "You, in \(g)" }
        return c.contactName
    }

    /// Keyword snippets carry <b> marks around the match; the web page strips every tag.
    static func plainSnippet(_ s: String) -> String {
        s.replacingOccurrences(of: "<[^>]*>", with: "", options: .regularExpression)
    }
}

// MARK: - Carrying a command out

@MainActor
enum CommandBarRunner {
    /// Closes the bar, then carries `action` out once the sheet has gone: several actions
    /// open a sheet of their own (merge, paste, add someone, a new list), and a window
    /// cannot show a second sheet while the first is still on it.
    static func run(_ action: CmdAction, app: AppModel) {
        app.showCommandBar = false
        Task { @MainActor in
            await waitForSheetsToClose()
            perform(action, app: app)
        }
    }

    /// Up to a second for the bar's sheet to finish leaving the window.
    private static func waitForSheetsToClose() async {
        for _ in 0..<20 {
            if !NSApp.windows.contains(where: { $0.attachedSheet != nil }) { return }
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    /// The app state each command sets. Values follow the web page's routes: a command
    /// that lands on the People table closes the open person, as `people?…` does there.
    static func perform(_ action: CmdAction, app: AppModel) {
        switch action {
        case .addPerson:
            app.selectedPersonID = nil
            app.section = .people
            app.showAddPerson = true
        case .paste:
            app.setupFlow = .paste
        case .people(let request):
            app.selectedPersonID = nil
            app.peopleRequest = request
            app.section = .people
        case .section(let s):
            // The web page's routes: "lists" is no list open, "ask" is the newest chat.
            if s == .lists { app.selectedListID = nil }
            if s == .ask { app.selectedChatID = nil }
            app.section = s
        case .newChat:
            app.requestNewChat = true
            app.section = .ask
        case .merge(let request):
            app.mergeRequest = request
        case .findDuplicates:
            CommandActions.findDuplicates(app)
        case .exportBackup:
            CommandActions.exportBackup(app)
        case .newList:
            app.requestNewList = true
            app.section = .lists
        case .appearance(let value):
            UserDefaults.standard.set(value, forKey: "appearance")
            if value == "system" {
                // main.js followSystemTheme: say which way the Mac is set right now.
                let dark = UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark"
                DataSettingsBus.shared.announce(dark ? "Follows your Mac — dark now" : "Follows your Mac — light now")
            }
        case .dataFocus(let row):
            app.dataFocus = row
            app.section = .data
        case .revealDataFolder:
            revealDataFolder(app)
        case .askDraft(let text):
            app.askDraft = text
            app.section = .ask
        case .askSend(let question):
            askNow(question, app: app)
        case .openPerson(let id):
            app.openPerson(id)
        case .noteAbout(let id):
            app.openPerson(id)
            app.noteComposerRequest = id
        case .copy(let value, let noun):
            NSPasteboard.general.clearContents()
            let copied = NSPasteboard.general.setString(value, forType: .string)
            DataSettingsBus.shared.announce(copied ? "Copied \(noun)" : "Couldn’t copy")
        case .openChat(let id):
            app.openChat(id)
        }
    }

    /// Finder with the database selected, in the folder the engine reports
    /// (`/api/status` db_path), as `OPEN_TARGETS["data"]` reveals crm.db and archive.db.
    static func revealDataFolder(_ app: AppModel) {
        guard let path = app.engine.status?.dbPath, !path.isEmpty else { return }
        let db = URL(fileURLWithPath: path)
        let archive = db.deletingLastPathComponent().appending(path: "archive.db")
        let files = [db] + (FileManager.default.fileExists(atPath: archive.path) ? [archive] : [])
        NSWorkspace.shared.activateFileViewerSelecting(files)
    }

    /// The web page sends a question typed into the box at once, in the newest chat
    /// (main.js:474 `ask?q=`, ask.js:1028-1036). Ask's own model does the sending, after
    /// its transcript for the newest chat has loaded; if it cannot take a question now
    /// (another is still being answered), the question waits in the composer instead.
    ///
    /// Ask's screen starts loading its own transcript as it appears, and a newer load
    /// throws away an older one's answer; so this loads, then waits for whichever load is
    /// the live one to finish before sending (ask.js:1028 sends once `msgs` is loaded).
    @discardableResult
    static func askNow(_ question: String, app: AppModel) -> Task<Void, Never> {
        app.openChat(nil)
        let model = AskModel.shared(for: app)
        return Task { @MainActor in
            await model.loadTranscript()
            var waited = 0
            while model.transcriptState == .loading && waited < askWaitSteps {
                try? await Task.sleep(for: .milliseconds(100))
                waited += 1
            }
            let ready = model.transcriptState == .loaded && model.gone == nil && model.pending == nil
                && app.selectedChatID == nil
            if ready { model.send(question, fromComposer: false) }
            // Not sent (another answer is still running, or the chat moved): it waits in
            // the composer rather than vanishing.
            if !ready || model.pending == nil { app.askDraft = question }
        }
    }

    /// How long ⌘K's Ask row waits for the newest chat to load: 150 × 0.1 s.
    static let askWaitSteps = 150
}
