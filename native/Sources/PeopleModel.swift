import Foundation
import Observation
import SwiftUI

// The People table's state: what to ask the engine for, what came back, and the rows the
// table draws after the client-side filters and the sort. The rules are the web page's
// (static/app/people.js, contract §3 and §8.6); every place this differs from it on purpose
// says so.

/// One table row: a PersonSummary plus everything a cell would otherwise recompute while
/// scrolling (thousands of rows, drawn only where visible, but sorted and filtered as a whole).
struct PeopleRow: Identifiable, Hashable, Sendable {
    let person: PersonSummary
    /// Position in the engine's answer: the tiebreak that keeps every sort stable.
    let order: Int
    /// Found by meaning search only (`~` beside the name). people.js:336
    var semanticOnly = false
    /// Meaning-search score when this row also scored there.
    var score: Double = 0

    let name: String
    let roleLine: String
    let location: String
    let messageTotal: Int
    let strength: Int
    let tier: Display.Tier
    let last: String
    let lastText: String
    let staleness: Display.Staleness
    let daysSince: Int?
    /// Non-empty topics, for the Topics cell (people.js:441).
    let topics: [String]

    var id: Int { person.id }

    init(_ p: PersonSummary, order: Int, now: Date) {
        person = p
        self.order = order
        name = p.name
        roleLine = Display.roleString(position: p.position, company: p.company)
        location = Display.locationString(metro: p.homeMetro, city: p.homeCity)
        messageTotal = p.counts.total
        strength = p.strength
        tier = Display.Tier(label: p.strengthLabel)
        last = p.last
        lastText = Display.lastContactText(p.last, now: now)
        staleness = Display.staleness(lastContact: p.last, now: now)
        daysSince = Display.daysSince(p.last, now: now)
        topics = p.topics.filter { !$0.isEmpty }
    }
}

/// The columns, which are also the sort keys.
enum PeopleColumn: String, Sendable {
    case name, closeness, last, location, messages

    /// The direction a first click on the header sorts in: the useful end first.
    var naturalAscending: Bool {
        switch self {
        case .name, .location: true
        case .closeness, .last, .messages: false
        }
    }
}

struct PeopleSort: Equatable, Sendable {
    var column: PeopleColumn
    var ascending: Bool

    static let closest = PeopleSort(column: .closeness, ascending: false)

    /// The engine's `sort` param. Last contact maps onto the web page's two orders
    /// (ascending = "Longest quiet first", descending = "Recently active"); every other
    /// column sorts the closeness answer on this Mac. Never "": that is LinkedIn-only
    /// recency (contract §3, trap 5).
    var serverKey: String {
        column == .last ? (ascending ? "quiet" : "recent") : "closeness"
    }

    /// The web page's SORTS key this matches, or nil for a header-only sort.
    var webKey: String? {
        switch (column, ascending) {
        case (.closeness, false): "closeness"
        case (.last, true): "quiet"
        case (.last, false): "recent"
        default: nil
        }
    }

    // from static/app/people.js:96 (SORTS)
    static let menu: [(key: String, label: String, sort: PeopleSort)] = [
        ("closeness", "Closest first", .closest),
        ("quiet", "Longest quiet first", PeopleSort(column: .last, ascending: true)),
        ("recent", "Recently active", PeopleSort(column: .last, ascending: false)),
    ]

    static func fromWebKey(_ k: String) -> PeopleSort { menu.first { $0.key == k }?.sort ?? .closest }
}

// MARK: - Filters and views

/// The server's `filter` values. Any other value would silently mean `all` (contract §3).
enum PeopleStatusFilter: String, CaseIterable, Sendable {
    case all, todo, described, excluded, deleted
}

/// Last-contact thresholds. from static/app/people.js:86-94
enum PeopleLastFilter: String, CaseIterable, Sendable {
    case m1, q3, q6, y1, never

    var label: String {
        switch self {
        case .m1: "In the last month"
        case .q3: "Quiet 3+ months"
        case .q6: "Quiet 6+ months"
        case .y1: "Quiet 1+ year"
        case .never: "Never contacted"
        }
    }

    func matches(_ d: Int?) -> Bool {
        switch self {
        case .m1: d.map { $0 <= 30 } ?? false
        case .q3: d.map { $0 >= 90 } ?? false
        case .q6: d.map { $0 >= 180 } ?? false
        case .y1: d.map { $0 >= 365 } ?? false
        case .never: d == nil
        }
    }
}

/// Everything a saved view sets. from static/app/people.js:131-132 (VIEW_DEFAULTS)
struct PeopleViewParams: Equatable, Sendable {
    var filter: PeopleStatusFilter = .all
    var sort: String = "closeness"
    var location = ""
    var role = ""
    var topic = ""
    var last: PeopleLastFilter?
    var tier: Display.Tier?
    var channel: Channel?
}

struct PeopleSavedView: Identifiable, Sendable {
    let key: String
    let name: String
    let note: String
    let params: PeopleViewParams
    var id: String { key }

    // from static/app/people.js:107-117 (VIEWS)
    static let fixed: [PeopleSavedView] = [
        .init(key: "everyone", name: "Everyone", note: "the whole network, closest first", params: .init()),
        .init(key: "cold", name: "Going cold", note: "quiet 6 months or more, longest first",
              params: .init(sort: "quiet", last: .q6)),
        .init(key: "never", name: "Never contacted", note: "no message on any channel", params: .init(last: .never)),
        .init(key: "recent", name: "Recently active", note: "last exchange first", params: .init(sort: "recent")),
        .init(key: "close", name: "Inner circle", note: "closeness reads as close", params: .init(tier: .close)),
        .init(key: "todo", name: "To describe", note: "no description written yet", params: .init(filter: .todo)),
        .init(key: "described", name: "Described", note: "you have written them up", params: .init(filter: .described)),
        .init(key: "excluded", name: "Excluded", note: "hidden from review and imports", params: .init(filter: .excluded)),
        .init(key: "deleted", name: "Deleted", note: "reversible — restore from here", params: .init(filter: .deleted)),
    ]

    /// Founders / Investors / Engineers, only when the network has that role.
    // from static/app/people.js:120-128 (roleViews)
    static func roleViews(_ roles: [String]) -> [PeopleSavedView] {
        ["founder", "investor", "engineer"].compactMap { want in
            guard let hit = roles.first(where: { $0.lowercased() == want }) else { return nil }
            let name = hit.prefix(1).uppercased() + hit.dropFirst() + "s"
            return .init(key: "role:" + hit, name: name, note: "role is " + hit, params: .init(role: hit))
        }
    }
}

/// What the engine is asked for. A change here is a new request; everything else
/// (closeness, channel, last contact, the column sort) is applied to the rows on this Mac.
/// What the People table's load depends on: the query, plus `AppModel.peopleRevision` so
/// an edit on a person's page refetches the rows (without the typing delay, since the
/// search text did not change).
struct PeopleLoadKey: Equatable, Sendable {
    let query: PeopleQuery
    let revision: Int
}

struct PeopleQuery: Equatable, Sendable {
    var q: String
    var filter: PeopleStatusFilter
    var location: String
    var role: String
    var topic: String
    var sort: String
    /// Bumped to re-run the same query after a write.
    var reload: Int
}

// MARK: - The model

@MainActor
@Observable
final class PeopleModel {
    // What came back
    private(set) var keywordRows: [PeopleRow] = []
    private(set) var semanticRows: [PersonSummary] = []
    private(set) var semanticScores: [Int: Double] = [:]
    /// `contacts.total`: the People count (never status.counts.total; contract trap 7).
    private(set) var total: Int?
    private(set) var hasLoaded = false
    private(set) var isLoading = false
    private(set) var error: String?
    /// The meaning-search "not installed / failed" sentence, shown above the table.
    private(set) var semanticNote = ""
    /// The query the rows on screen answer.
    private(set) var loadedQuery: PeopleQuery?
    /// The view the rows on screen were loaded for. Until a newly chosen view's rows land,
    /// the old rows are still showing, and Hide, Unexclude and Restore must be offered
    /// for them, not for the view just chosen (an Unexclude on a deleted person would
    /// take their name off the import skip list).
    var rowsFilter: PeopleStatusFilter { loadedQuery?.filter ?? filter }

    // Vocabulary for the filter menus
    private(set) var locations: [String] = []
    private(set) var roles: [String] = []
    private(set) var topics: [String] = []

    // What the person chose
    var filter: PeopleStatusFilter = .all
    var location = ""
    var role = ""
    var topic = ""
    var lastFilter: PeopleLastFilter? { didSet { recompute() } }
    var tierFilter: Display.Tier? { didSet { recompute() } }
    var channelFilter: Channel? { didSet { recompute() } }
    var sort: PeopleSort = .closest { didSet { recompute() } }
    var reloadToken = 0

    /// A transient line in the status bar ("Excluded — hidden from review and imports").
    private(set) var flash: String?
    @ObservationIgnored private var flashTask: Task<Void, Never>?

    /// The rows the table draws, in order.
    private(set) var visible: [PeopleRow] = [] {
        didSet {
            meaningOnlyCount = visible.reduce(0) { $0 + ($1.semanticOnly ? 1 : 0) }
            visibleRevision &+= 1
        }
    }
    /// Bumped whenever `visible` is replaced: cheaper to watch than the rows themselves.
    private(set) var visibleRevision = 0
    /// Rows found by meaning search alone (`~`), counted once per recompute.
    private(set) var meaningOnlyCount = 0
    /// What the keyword search itself matched, before any client-side filter.
    var keywordCount: Int { keywordRows.count }

    // MARK: Query

    /// A search with no letter or digit would match everyone (contract trap 17), so it
    /// counts as no search at all.
    static func effectiveSearch(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.unicodeScalars.contains { CharacterSet.alphanumerics.contains($0) } ? t : ""
    }

    func query(search: String) -> PeopleQuery {
        PeopleQuery(
            q: Self.effectiveSearch(search), filter: filter, location: location, role: role, topic: topic,
            sort: sort.serverKey, reload: reloadToken)
    }

    var allViews: [PeopleSavedView] { PeopleSavedView.fixed + PeopleSavedView.roleViews(roles) }

    var currentParams: PeopleViewParams {
        PeopleViewParams(
            filter: filter, sort: sort.webKey ?? "custom", location: location, role: role, topic: topic,
            last: lastFilter, tier: tierFilter, channel: channelFilter)
    }

    /// The saved view the current state matches, or nil ("Custom view").
    var activeView: PeopleSavedView? { allViews.first { $0.params == currentParams } }

    /// Applying a view resets every other filter (the caller clears the search).
    // from static/app/people.js:486-492
    func apply(_ view: PeopleSavedView) {
        let p = view.params
        filter = p.filter
        location = p.location
        role = p.role
        topic = p.topic
        lastFilter = p.last
        tierFilter = p.tier
        channelFilter = p.channel
        sort = PeopleSort.fromWebKey(p.sort)
    }

    var activeFilterCount: Int {
        [!location.isEmpty, !role.isEmpty, !topic.isEmpty, lastFilter != nil, tierFilter != nil, channelFilter != nil]
            .filter { $0 }.count
    }

    /// The list on screen is a chosen subset, so "Add these N to a list" means something.
    /// On the whole network it would mean nothing and invite a mistake. people.js:763
    var isNarrowed: Bool {
        !(loadedQuery?.q.isEmpty ?? true) || activeFilterCount > 0 || filter != .all
    }

    /// A view of People asked for from ⌘K or a menu (AppModel.peopleRequest), in the web
    /// page's own words. A field left nil keeps what is set, and the search stays, as the
    /// web page's preset does (people.js:686-695). Unknown words are ignored rather than
    /// guessed at: an unknown `filter` would silently mean Everyone at the engine.
    func apply(_ r: PeopleRequest) {
        if let f = r.filter, let value = PeopleStatusFilter(rawValue: f) { filter = value }
        if let s = r.sort {
            if s == "name" {
                sort = PeopleSort(column: .name, ascending: true)
            } else if PeopleSort.menu.contains(where: { $0.key == s }) {
                sort = PeopleSort.fromWebKey(s)
            }
        }
        if let l = r.location { location = l }
        if let v = r.role { role = v }
        if let t = r.topic { topic = t }
    }

    /// Where `id` sits in the table's current order, for the person page's ‹ 3 / 12 ›
    /// stepper (people.js:1321-1369). nil when they are not in the list, or alone in it.
    func steps(around id: Int) -> PersonSteps? {
        guard visible.count > 1, let i = visible.firstIndex(where: { $0.id == id }) else { return nil }
        return PersonSteps(
            index: i + 1, total: visible.count,
            previous: i > 0 ? visible[i - 1].id : nil,
            next: i + 1 < visible.count ? visible[i + 1].id : nil)
    }

    func clearFilters() {
        location = ""; role = ""; topic = ""
        lastFilter = nil; tierFilter = nil; channelFilter = nil
    }

    // MARK: Loading

    /// Loads everyone who matches (`limit=20000`, contract trap 6), then, for a search,
    /// adds what meaning search finds. Runs inside a `.task(id:)`, so a newer query
    /// cancels it; `debounce` waits first so typing does not fetch on every key.
    func load(_ query: PeopleQuery, client: EngineClient, debounce: Bool) async {
        if debounce {
            try? await Task.sleep(for: .milliseconds(250))
            if Task.isCancelled { return }
        }
        isLoading = true
        var params = ["sort": query.sort, "limit": "20000", "filter": query.filter.rawValue]
        if !query.q.isEmpty { params["q"] = query.q }
        if !query.location.isEmpty { params["location"] = query.location }
        if !query.role.isEmpty { params["role"] = query.role }
        if !query.topic.isEmpty { params["topic"] = query.topic }
        do {
            let r = try await client.get("/api/contacts", query: params, as: ContactsResponse.self)
            if Task.isCancelled { return }
            let now = Date()
            keywordRows = r.contacts.enumerated().map { PeopleRow($1, order: $0, now: now) }
            total = r.total
            semanticRows = []
            semanticScores = [:]
            semanticNote = ""
            error = nil
            loadedQuery = query
            hasLoaded = true
            isLoading = false
            recompute()
        } catch {
            if Task.isCancelled || error is CancellationError { return }
            self.error = error.localizedDescription
            hasLoaded = true
            isLoading = false
            return
        }
        if !query.q.isEmpty, query.filter != .excluded, query.filter != .deleted {
            await loadSemantic(query, client: client)
        }
    }

    /// Meaning search: top 30, no relevance floor, hidden and deleted people removed by the
    /// engine (contract §3). Merged the way people.js:330-341 does it, with one fix: the
    /// web page also merges these into the To describe / Described / Excluded / Deleted
    /// views, where they do not belong (meaning search ignores `filter`). Here they are
    /// skipped for Excluded and Deleted and held to the row's `status` for the other two.
    private func loadSemantic(_ query: PeopleQuery, client: EngineClient) async {
        do {
            let r = try await client.get(
                "/api/search/semantic", query: ["q": query.q], as: SemanticSearchResponse.self, timeout: 60)
            if Task.isCancelled || loadedQuery != query { return }
            guard r.available else {
                semanticNote = r.message ?? ""
                return
            }
            let hits = r.contacts.filter { c in
                if !query.location.isEmpty && c.homeMetro != query.location && c.homeCity != query.location { return false }
                if !query.role.isEmpty && c.roleType != query.role { return false }
                if !query.topic.isEmpty && !c.topics.contains(where: { $0.lowercased().contains(query.topic.lowercased()) }) {
                    return false
                }
                switch query.filter {
                case .todo: return c.status == "new"
                case .described: return c.status == "described"
                default: return true
                }
            }
            semanticRows = hits
            semanticScores = Dictionary(hits.map { ($0.id, $0.score ?? 0) }, uniquingKeysWith: { a, _ in a })
            recompute()
        } catch {
            // Meaning search is extra: the keyword rows are already on screen.
        }
    }

    func loadVocabulary(client: EngineClient) async {
        async let f = try? client.get("/api/facets", as: FacetsResponse.self)
        async let l = try? client.get("/api/locations", as: LocationsResponse.self)
        if let facets = await f {
            roles = facets.roles
            topics = facets.topics
        }
        if let locs = await l { locations = locs.locations }
    }

    func showFlash(_ text: String) {
        flash = text
        flashTask?.cancel()
        flashTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            self?.flash = nil
        }
    }

    // MARK: Filtering and sorting

    private func recompute() {
        // Merge meaning-search hits: rows that also scored get the score; the rest are
        // appended after every keyword hit. people.js:330-341
        var rows = keywordRows
        let searching = !(loadedQuery?.q.isEmpty ?? true)
        if searching {
            let have = Set(rows.map(\.id))
            for i in rows.indices { rows[i].score = semanticScores[rows[i].id] ?? 0 }
            let now = Date()
            var next = rows.count
            for s in semanticRows where !have.contains(s.id) {
                var r = PeopleRow(s, order: next, now: now)
                r.semanticOnly = true
                r.score = s.score ?? 0
                rows.append(r)
                next += 1
            }
        }

        // Closeness, channel and last contact: computed per row, so applied here, over
        // everyone (which is why the whole network is loaded). people.js:381-384
        let filtered = rows.filter { r in
            if let t = tierFilter, r.person.strengthLabel != t.rawValue { return false }
            if let c = channelFilter, r.person.counts[c] <= 0 { return false }
            if let l = lastFilter, !l.matches(r.daysSince) { return false }
            return true
        }
        visible = Self.sorted(filtered, by: sort, searching: searching)
    }

    /// Stable sorts: ties keep the engine's order.
    static func sorted(_ rows: [PeopleRow], by sort: PeopleSort, searching: Bool) -> [PeopleRow] {
        func stable(_ before: (PeopleRow, PeopleRow) -> Bool?) -> [PeopleRow] {
            rows.sorted { a, b in before(a, b) ?? (a.order < b.order) }
        }
        let asc = sort.ascending
        switch sort.column {
        case .closeness:
            if !asc {
                // Searching: keyword matches in closeness order, then meaning-only matches by
                // score. Differs from people.js:338-341 on purpose: the web page lifts every
                // keyword row that ALSO scored in meaning search to the top by that score, but
                // meaning search returns its top 30 with no relevance floor (contract trap
                // 15), so faint strangers jumped above close friends for a plain name search.
                guard searching else { return rows.sorted { $0.order < $1.order } }
                return stable { a, b in
                    if a.semanticOnly != b.semanticOnly { return !a.semanticOnly }
                    if a.semanticOnly, a.score != b.score { return a.score > b.score }
                    return nil
                }
            }
            return stable { a, b in a.strength == b.strength ? nil : a.strength < b.strength }
        case .last:
            // People with any contact first, either way round; never-contacted last.
            // people.js:390-397
            return stable { a, b in
                if a.last.isEmpty != b.last.isEmpty { return !a.last.isEmpty }
                if a.last == b.last { return nil }
                return asc ? a.last < b.last : a.last > b.last
            }
        case .name:
            return stable { a, b in
                let r = a.name.localizedStandardCompare(b.name)
                return r == .orderedSame ? nil : (asc ? r == .orderedAscending : r == .orderedDescending)
            }
        case .location:
            return stable { a, b in
                if a.location.isEmpty != b.location.isEmpty { return !a.location.isEmpty }
                let r = a.location.localizedStandardCompare(b.location)
                return r == .orderedSame ? nil : (asc ? r == .orderedAscending : r == .orderedDescending)
            }
        case .messages:
            return stable { a, b in
                a.messageTotal == b.messageTotal ? nil : (asc ? a.messageTotal < b.messageTotal : a.messageTotal > b.messageTotal)
            }
        }
    }

    /// The row after (or else before) `id` in the table, for moving on after hiding.
    func neighbour(of id: Int) -> Int? {
        guard let i = visible.firstIndex(where: { $0.id == id }) else { return nil }
        if i + 1 < visible.count { return visible[i + 1].id }
        return i > 0 ? visible[i - 1].id : nil
    }
}
