import Foundation

// Every answer the engine gives, as Codable structs. The shapes are in native/API_CONTRACT.md;
// the section numbers below point there.
//
// Decoding is deliberately forgiving: a missing key, or a null where a value was expected,
// becomes the field's default ("" / 0 / false / [] / nil) instead of failing the whole
// screen. The engine adds keys over time and old saved Ask turns lack newer ones, so a
// strict decoder would turn one new key into a blank page. `--selftest` flips
// `DecodingPolicy.strict` so a key of the WRONG TYPE still fails loudly there, with its
// coding path, instead of being silently defaulted.

// MARK: - Tolerant decoding machinery

/// A value a field falls back to when the engine leaves the key out or sends null.
protocol HasDefault { static var defaultValue: Self { get } }

extension Int: HasDefault { static var defaultValue: Int { 0 } }
extension Double: HasDefault { static var defaultValue: Double { 0 } }
extension Bool: HasDefault { static var defaultValue: Bool { false } }
extension String: HasDefault { static var defaultValue: String { "" } }
extension Array: HasDefault { static var defaultValue: [Element] { [] } }
extension Dictionary: HasDefault { static var defaultValue: [Key: Value] { [:] } }
extension Optional: HasDefault { static var defaultValue: Wrapped? { nil } }

enum DecodingPolicy {
    /// Off in the app (a wrong-typed key falls back to its default), on in `--selftest`
    /// (a wrong-typed key is a FAIL with its coding path). Set once at startup, before
    /// any request, which is why the unchecked global is safe.
    nonisolated(unsafe) static var strict = false
}

/// `@Def var name: String` decodes a missing or null key as the type's default.
/// Every model therefore has an `init()` with all fields at their defaults; build one with
/// `var p = PersonSummary(); p.name = "Ada"` (the memberwise init takes `Def` values).
@propertyWrapper
struct Def<Value: Codable & HasDefault>: Codable {
    var wrappedValue: Value
    init(wrappedValue: Value) { self.wrappedValue = wrappedValue }
    init() { wrappedValue = Value.defaultValue }
    init(from decoder: Decoder) throws {
        wrappedValue = try decoder.singleValueContainer().decode(Value.self)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wrappedValue)
    }
}
extension Def: Sendable where Value: Sendable {}
extension Def: Equatable where Value: Equatable {}
extension Def: Hashable where Value: Hashable {}

extension KeyedDecodingContainer {
    // Synthesized Decodable picks this overload for every @Def property; it is what turns
    // "key missing" into "default" rather than a keyNotFound error.
    func decode<V>(_: Def<V>.Type, forKey key: Key) throws -> Def<V> {
        do {
            return Def(wrappedValue: try decodeIfPresent(V.self, forKey: key) ?? V.defaultValue)
        } catch {
            if DecodingPolicy.strict { throw error }
            return Def()
        }
    }
}

/// Loosely typed JSON, for fields whose shape the screens never rely on (diagnostics,
/// tool caps) and for building request bodies:
/// `["name": "Ada", "contact_ids": [1, 2], "excluded": true]` is a `[String: JSONValue]`.
enum JSONValue: Codable, Sendable, Hashable {
    case null
    case bool(Bool)
    /// Kept apart from `number` because several write routes demand a JSON int and
    /// reject `5.0` (list member `contact_id`, `date_id`, merge ids).
    case int(Int)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let i = try? c.decode(Int.self) { self = .int(i); return }
        if let d = try? c.decode(Double.self) { self = .number(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([JSONValue].self) { self = .array(a); return }
        self = .object(try c.decode([String: JSONValue].self))
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .int(let i): try c.encode(i)
        case .number(let d): try c.encode(d)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// Number of items (array/object) — what the self-test prints instead of values.
    var itemCount: Int {
        switch self {
        case .array(let a): a.count
        case .object(let o): o.count
        default: 1
        }
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByFloatLiteral,
    ExpressibleByBooleanLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral, ExpressibleByNilLiteral
{
    init(stringLiteral v: String) { self = .string(v) }
    init(integerLiteral v: Int) { self = .int(v) }
    init(floatLiteral v: Double) { self = .number(v) }
    init(booleanLiteral v: Bool) { self = .bool(v) }
    init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
    init(dictionaryLiteral elements: (String, JSONValue)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, last in last }))
    }
    init(nilLiteral: ()) { self = .null }
}

extension JSONValue: HasDefault { static var defaultValue: JSONValue { .null } }

/// Lets `"ada-\(n)@example.com"` be a JSONValue too.
extension JSONValue: ExpressibleByStringInterpolation {}

// MARK: - Shared types (contract section 2)

/// The seven channels, in the web page's order (`static/app/api.js:184`).
enum Channel: String, CaseIterable, Sendable, Codable {
    case linkedin, imessage, whatsapp, email, telegram, messenger, instagram
}

/// Message counts per channel. All seven keys are always sent.
struct ChannelCounts: Codable, Sendable, Hashable, HasDefault {
    @Def var linkedin: Int
    @Def var imessage: Int
    @Def var whatsapp: Int
    @Def var email: Int
    @Def var telegram: Int
    @Def var messenger: Int
    @Def var instagram: Int

    static var defaultValue: ChannelCounts { ChannelCounts() }

    subscript(channel: Channel) -> Int {
        switch channel {
        case .linkedin: linkedin
        case .imessage: imessage
        case .whatsapp: whatsapp
        case .email: email
        case .telegram: telegram
        case .messenger: messenger
        case .instagram: instagram
        }
    }
    var total: Int { Channel.allCases.reduce(0) { $0 + self[$1] } }
    /// Channels with at least one message, in display order.
    var used: [Channel] { Channel.allCases.filter { self[$0] > 0 } }
}

/// A row in the People table, a list member, or a meaning-search hit (`_contact_summary`).
struct PersonSummary: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    /// "" or a RELATIVE "/api/photo/{id}?v={mtime}". Use `EngineClient.absoluteURL(_:)`.
    @Def var photo: String
    @Def var name: String
    @Def var company: String
    @Def var position: String
    @Def var homeCity: String
    @Def var homeCountry: String
    @Def var homeMetro: String
    @Def var roleType: String
    @Def var blurb: String
    @Def var topics: [String]
    @Def var category: String
    @Def var status: String
    @Def var excluded: Bool
    @Def var counts: ChannelCounts
    /// Naive UTC "YYYY-MM-DD HH:MM:SS", "" = never in touch.
    @Def var last: String
    @Def var strength: Int
    @Def var strengthLabel: String
    /// Only on `/api/search/semantic` hits.
    @Def var score: Double?
    /// Only on `/api/list/{id}` members: "", invited, replied, attending, declined.
    @Def var listStatus: String?

    enum CodingKeys: String, CodingKey {
        case id, photo, name, company, position, blurb, topics, category, status, excluded, counts, last, strength, score
        case homeCity = "home_city", homeCountry = "home_country", homeMetro = "home_metro", roleType = "role_type"
        case strengthLabel = "strength_label", listStatus = "list_status"
    }
}

// MARK: - People (section 3)

struct ContactsResponse: Codable, Sendable, Hashable {
    @Def var contacts: [PersonSummary]
    /// Matches before the `limit` cut. THIS is the People count, not status.counts.total.
    @Def var total: Int
    @Def var limit: Int
}

struct SemanticSearchResponse: Codable, Sendable, Hashable {
    @Def var available: Bool
    @Def var contacts: [PersonSummary]
    @Def var query: String
    /// Set when `available` is false: a sentence to show above the table, not an error.
    @Def var message: String?
}

struct FacetsResponse: Codable, Sendable, Hashable {
    @Def var roles: [String]
    @Def var topics: [String]
}

struct LocationsResponse: Codable, Sendable, Hashable {
    @Def var locations: [String]
}

struct UpcomingResponse: Codable, Sendable, Hashable {
    @Def var days: Int
    @Def var shown: [UpcomingItem]
    @Def var more: Int
}

/// Two shapes share this: a person's date (contactID/dateID set) or a dated list
/// (kind "event", listID/venue/people/attending set).
struct UpcomingItem: Codable, Sendable, Hashable, Identifiable {
    @Def var when: String
    @Def var daysUntil: Int
    @Def var kind: String
    @Def var title: String
    @Def var reason: String
    @Def var contactID: Int?
    @Def var dateID: Int?
    @Def var listID: Int?
    @Def var venue: String?
    @Def var people: Int?
    @Def var attending: Int?

    var id: String { "\(kind)|\(contactID ?? 0)|\(dateID ?? 0)|\(listID ?? 0)|\(when)" }

    enum CodingKeys: String, CodingKey {
        case when, kind, title, reason, venue, people, attending
        case daysUntil = "days_until", contactID = "contact_id", dateID = "date_id", listID = "list_id"
    }
}

struct ExcludeListResponse: Codable, Sendable, Hashable {
    @Def var entries: [ExcludeEntry]
    @Def var excludeFileSaved: Bool?
    @Def var warning: String?
    enum CodingKeys: String, CodingKey {
        case entries, warning
        case excludeFileSaved = "exclude_file_saved"
    }
}

struct ExcludeEntry: Codable, Sendable, Hashable, Identifiable {
    @Def var name: String
    /// false = a hidden name with no matching person.
    @Def var inDB: Bool
    var id: String { name }
    enum CodingKeys: String, CodingKey {
        case name
        case inDB = "in_db"
    }
}

// MARK: - A person's page (section 4)

struct PersonDetail: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var photo: String
    @Def var name: String
    @Def var company: String
    @Def var position: String
    @Def var category: String
    @Def var howWeMet: String
    /// "YYYY-MM-DD" or "".
    @Def var connectedOn: String
    /// "YYYY-MM-DD HH:MM:SS" or "" — a different format from connectedOn.
    @Def var firstContact: String
    @Def var linkedinURL: String
    @Def var status: String
    @Def var excluded: Bool
    @Def var deleted: Bool
    @Def var source: String
    @Def var counts: ChannelCounts
    @Def var last: String
    @Def var strength: Int
    @Def var strengthLabel: String
    /// Newest first.
    @Def var notes: [Note]
    @Def var identifiers: [Identifier]
    @Def var historyPreview: [ArchivedMessage]
    @Def var homeCity: String
    @Def var homeCountry: String
    @Def var homeMetro: String
    @Def var topics: [String]
    @Def var roleType: String
    @Def var blurb: String
    @Def var factsSource: String
    @Def var mutuals: Mutuals
    @Def var dates: [DateRow]
    @Def var story: String
    @Def var storySource: String
    /// nil for most people, and always for hidden or deleted ones.
    @Def var nextAction: NextAction?
    @Def var briefMeta: BriefMeta?
    @Def var strengthParts: StrengthParts

    enum CodingKeys: String, CodingKey {
        case id, photo, name, company, position, category, status, excluded, deleted, source, counts, last, strength
        case notes, identifiers, topics, blurb, mutuals, dates, story
        case howWeMet = "how_we_met", connectedOn = "connected_on", firstContact = "first_contact"
        case linkedinURL = "linkedin_url", strengthLabel = "strength_label", historyPreview = "history_preview"
        case homeCity = "home_city", homeCountry = "home_country", homeMetro = "home_metro", roleType = "role_type"
        case factsSource = "facts_source", storySource = "story_source", nextAction = "next_action"
        case briefMeta = "brief_meta", strengthParts = "strength_parts"
    }
}

struct Note: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var body: String
    /// "note", "how_we_met", "voice".
    @Def var kind: String
    @Def var createdAt: String
    enum CodingKeys: String, CodingKey {
        case id, body, kind
        case createdAt = "created_at"
    }
}

struct Identifier: Codable, Sendable, Hashable, Identifiable {
    /// "phone" or "email".
    @Def var kind: String
    @Def var value: String
    var id: String { kind + ":" + value }
}

struct NextAction: Codable, Sendable, Hashable {
    /// Only "reconnect" exists today.
    @Def var kind: String
    @Def var daysSilent: Int
    @Def var lastSnippet: String
    @Def var lastChannel: String
    enum CodingKeys: String, CodingKey {
        case kind
        case daysSilent = "days_silent", lastSnippet = "last_snippet", lastChannel = "last_channel"
    }
}

struct DateRow: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    /// birthday, anniversary, meeting, custom.
    @Def var kind: String
    @Def var label: String
    @Def var onDate: String
    @Def var yearKnown: Bool
    @Def var recurring: Bool
    /// "" (not null) for a one-off already past.
    @Def var nextOccurrence: String
    @Def var daysUntil: Int
    /// A finished sentence from the engine ("Birthday tomorrow (turns 40)").
    @Def var reason: String
    @Def var source: String
    enum CodingKeys: String, CodingKey {
        case id, kind, label, recurring, reason, source
        case onDate = "on_date", yearKnown = "year_known", nextOccurrence = "next_occurrence", daysUntil = "days_until"
    }
}

struct ArchivedMessage: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var channel: String
    /// 1 = you sent it.
    @Def var direction: Int
    @Def var ts: String
    @Def var subject: String
    @Def var body: String
}

struct Mutuals: Codable, Sendable, Hashable, HasDefault {
    @Def var mutualNames: [String]
    @Def var sharedHistory: [String]
    @Def var recentActivity: String
    static var defaultValue: Mutuals { Mutuals() }
    var isEmpty: Bool { mutualNames.isEmpty && sharedHistory.isEmpty && recentActivity.isEmpty }
    enum CodingKeys: String, CodingKey {
        case mutualNames = "mutual_names", sharedHistory = "shared_history", recentActivity = "recent_activity"
    }
}

struct BriefMeta: Codable, Sendable, Hashable {
    @Def var messages: Int
    @Def var channels: Int
    @Def var builtAt: String
    @Def var newerMessages: Int
    enum CodingKeys: String, CodingKey {
        case messages, channels
        case builtAt = "built_at", newerMessages = "newer_messages"
    }
}

struct StrengthParts: Codable, Sendable, Hashable, HasDefault {
    @Def var days: Double
    @Def var tenure: Double
    @Def var volume: Double
    @Def var depth: Double
    @Def var reciprocity: Double
    @Def var bond: Double
    @Def var warmth: Double
    @Def var activeDays: Int
    @Def var totalMessages: Int
    @Def var channelsUsed: Int
    /// nil = never in touch (different from 0 = in touch today).
    @Def var daysSince: Int?
    static var defaultValue: StrengthParts { StrengthParts() }
    enum CodingKeys: String, CodingKey {
        case days, tenure, volume, depth, reciprocity, bond, warmth
        case activeDays = "active_days", totalMessages = "total_messages", channelsUsed = "channels_used"
        case daysSince = "days_since"
    }
}

struct HistoryResponse: Codable, Sendable, Hashable {
    /// Newest first.
    @Def var messages: [HistoryMessage]
    @Def var hasMore: Bool
    @Def var threads: Bool
    enum CodingKeys: String, CodingKey {
        case messages, threads
        case hasMore = "has_more"
    }
}

struct HistoryMessage: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var channel: String
    @Def var direction: Int
    @Def var ts: String
    @Def var subject: String
    @Def var body: String
    /// Group chat name or "".
    @Def var group: String
    /// Speaker label; "" unless threads=1 and it is someone else's group line.
    @Def var who: String
}

struct EpisodesResponse: Codable, Sendable, Hashable {
    @Def var episodes: [Episode]
    @Def var hasMore: Bool
    /// Pass as `before` for the next page. Missing on empty answers.
    @Def var nextBefore: String?
    enum CodingKeys: String, CodingKey {
        case episodes
        case hasMore = "has_more", nextBefore = "next_before"
    }
}

struct Episode: Codable, Sendable, Hashable, Identifiable {
    @Def var channel: String
    @Def var tsStart: String
    @Def var tsEnd: String
    @Def var count: Int
    @Def var sent: Int
    @Def var snippet: String
    @Def var group: String
    /// Only when count <= 6, oldest first. Never use these for a group episode.
    @Def var messages: [EpisodeMessage]?
    /// Only on group episodes.
    @Def var threadSince: String?
    @Def var threadBefore: String?

    var id: String { "\(channel)|\(tsStart)|\(tsEnd)|\(group)" }
    var isGroup: Bool { threadSince != nil || threadBefore != nil }

    enum CodingKeys: String, CodingKey {
        case channel, count, sent, snippet, group, messages
        case tsStart = "ts_start", tsEnd = "ts_end", threadSince = "thread_since", threadBefore = "thread_before"
    }
}

struct EpisodeMessage: Codable, Sendable, Hashable {
    @Def var channel: String
    @Def var direction: Int
    @Def var ts: String
    @Def var subject: String
    @Def var body: String
    @Def var group: String
}

// MARK: - Write answers (sections 3, 4, 6)

/// The `{ok: true, ...}` family. Each route fills a different subset, so every extra is optional.
struct WriteResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
    /// `/field`: an empty value on a filled field was ignored (send clear:true to empty it).
    @Def var unchanged: Bool?
    @Def var excludeFileSaved: Bool?
    /// A sentence to show instead of the usual confirmation.
    @Def var warning: String?
    @Def var deleted: Bool?
    @Def var purged: Bool?
    @Def var messagesRemoved: Int?
    @Def var identifiers: [Identifier]?
    @Def var dates: [DateRow]?
    @Def var photo: String?
    @Def var keptID: Int?
    @Def var added: Int?
    enum CodingKeys: String, CodingKey {
        case ok, unchanged, warning, deleted, purged, identifiers, dates, photo, added
        case excludeFileSaved = "exclude_file_saved", messagesRemoved = "messages_removed", keptID = "kept_id"
    }
}

/// `POST /api/contacts` → {id}; `POST /api/lists` → {id, added}.
struct CreatedID: Codable, Sendable, Hashable {
    @Def var id: Int
    @Def var added: Int?
}

struct NoteCreated: Codable, Sendable, Hashable {
    @Def var note: Note
}
extension Note: HasDefault { static var defaultValue: Note { Note() } }

struct BriefResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var story: String
    @Def var source: String
    @Def var briefMeta: BriefMeta?
    enum CodingKeys: String, CodingKey {
        case ok, story, source
        case briefMeta = "brief_meta"
    }
}

struct DraftResult: Codable, Sendable, Hashable {
    @Def var message: String
    @Def var meta: BriefMeta?
}

// MARK: - Ask (section 5)

struct Shape: Codable, Sendable, Hashable, HasDefault {
    /// "10", "25", "all".
    @Def var people: String
    /// "brief", "full".
    @Def var detail: String
    /// "quick", "thorough".
    @Def var search: String
    static var defaultValue: Shape { Shape() }
    /// What a new chat uses unless the person picked otherwise (`ask.js`).
    static let preferred = Shape(people: "all", detail: "full", search: "thorough")
}

// In extensions so the synthesized init() (every field at its default) survives.
extension Shape {
    init(people: String, detail: String, search: String) {
        self.init()
        self.people = people
        self.detail = detail
        self.search = search
    }
}

struct ChatRow: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var title: String
    @Def var createdAt: String
    @Def var updatedAt: String
    @Def var shape: Shape
    /// Stored messages (a question plus its answer = 2). Missing on some answers.
    @Def var turns: Int?
    @Def var lastMessageAt: String?
    enum CodingKeys: String, CodingKey {
        case id, title, shape, turns
        case createdAt = "created_at", updatedAt = "updated_at", lastMessageAt = "last_message_at"
    }
}
extension ChatRow: HasDefault { static var defaultValue: ChatRow { ChatRow() } }

/// A person an Ask answer names. Only the first nine fields are on every card; AI cards
/// come straight from tool results and carry different extras. No card has a photo.
struct AskCard: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var name: String
    @Def var company: String
    @Def var position: String
    @Def var homeCity: String
    @Def var homeMetro: String
    @Def var roleType: String
    @Def var strength: Int
    @Def var strengthLabel: String
    @Def var category: String?
    @Def var topics: [String]?
    @Def var blurb: String?
    @Def var reason: String?
    @Def var score: Double?
    @Def var last: String?
    @Def var matched: JSONValue?
    @Def var matchingMessages: Int?
    @Def var channels: [String]?
    @Def var first: String?
    @Def var firstEver: String?
    @Def var newInWindow: Bool?
    @Def var inGroupThreads: Int?
    @Def var oneToOne: Int?
    @Def var nameMatch: Int?
    @Def var messagesTotal: Int?
    @Def var messagesOneToOne: Int?
    @Def var knownSince: String?
    @Def var yearsKnown: Double?
    @Def var lastMessage: String?

    enum CodingKeys: String, CodingKey {
        case id, name, company, position, strength, category, topics, blurb, reason, score, last, matched, channels, first
        case homeCity = "home_city", homeMetro = "home_metro", roleType = "role_type", strengthLabel = "strength_label"
        case matchingMessages = "matching_messages", firstEver = "first_ever", newInWindow = "new_in_window"
        case inGroupThreads = "in_group_threads", oneToOne = "one_to_one", nameMatch = "name_match"
        case messagesTotal = "messages_total", messagesOneToOne = "messages_one_to_one"
        case knownSince = "known_since", yearsKnown = "years_known", lastMessage = "last_message"
    }
}

struct Spend: Codable, Sendable, Hashable {
    @Def var rounds: Int
    @Def var inputTokens: Int
    @Def var outputTokens: Int
    @Def var cacheReadTokens: Int
    @Def var cacheWriteTokens: Int
    @Def var usd: Double
    @Def var priceInPerMtok: Double
    @Def var priceOutPerMtok: Double
    @Def var model: String
    enum CodingKeys: String, CodingKey {
        case rounds, usd, model
        case inputTokens = "input_tokens", outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens", cacheWriteTokens = "cache_write_tokens"
        case priceInPerMtok = "price_in_per_mtok", priceOutPerMtok = "price_out_per_mtok"
    }
}

struct TrailEntry: Codable, Sendable, Hashable {
    @Def var tool: String
    @Def var detail: String
    @Def var matched: Int
    @Def var total: Int
    /// Only for message_stats and ask_the_user.
    @Def var label: String?
}

/// The answer asked something back: show `options` as buttons.
struct AskQuestion: Codable, Sendable, Hashable {
    @Def var question: String
    @Def var options: [String]
    @Def var multi: Bool
}

struct Searched: Codable, Sendable, Hashable {
    @Def var contacts: Int
    @Def var conversations: Int
    @Def var messages: Int
}

struct ShapeStatus: Codable, Sendable, Hashable {
    @Def var requested: Shape
    @Def var effective: ShapeEffective
    @Def var options: ShapeOptions
    /// Keyed "people", "detail", "search".
    @Def var controls: [String: ShapeControl]
    @Def var peopleCap: Int
    @Def var caps: JSONValue?
    enum CodingKeys: String, CodingKey {
        case requested, effective, options, controls, caps
        case peopleCap = "people_cap"
    }
}

struct ShapeEffective: Codable, Sendable, Hashable, HasDefault {
    @Def var people: String
    /// nil = the control did nothing on this answer.
    @Def var detail: String?
    @Def var search: String?
    static var defaultValue: ShapeEffective { ShapeEffective() }
}

struct ShapeOptions: Codable, Sendable, Hashable, HasDefault {
    @Def var people: [String]
    @Def var detail: [String]
    @Def var search: [String]
    static var defaultValue: ShapeOptions { ShapeOptions() }
}

struct ShapeControl: Codable, Sendable, Hashable {
    @Def var available: Bool
    @Def var layer: String
    /// Tooltip for a disabled chip.
    @Def var reason: String
}

struct AskContext: Codable, Sendable, Hashable {
    @Def var limit: Int
    @Def var turns: Int
    @Def var total: Int
    @Def var trimmed: Int
    @Def var fedToLLM: Bool
    @Def var scope: String
    enum CodingKeys: String, CodingKey {
        case limit, turns, total, trimmed, scope
        case fedToLLM = "fed_to_llm"
    }
}

/// `POST /api/chat`. HTTP 200 does not mean the AI answered: `engine == "rules"` with a
/// trailing "(AI chat was unavailable: …)" in `reply` is a degraded answer (contract §5, §8.8).
struct ChatAnswer: Codable, Sendable, Hashable {
    @Def var reply: String
    @Def var contacts: [AskCard]
    @Def var trail: [TrailEntry]
    @Def var truncated: Bool
    @Def var stopped: Bool?
    /// null on the keyword engine.
    @Def var spend: Spend?
    @Def var question: AskQuestion?
    @Def var searched: Searched?
    /// "llm" or "rules".
    @Def var engine: String
    @Def var shape: ShapeStatus?
    @Def var context: AskContext?
    @Def var conversation: ChatRow?
    /// "requested", "newest", "created".
    @Def var conversationResolved: String
    @Def var cardsMatched: Int
    @Def var cardsNamed: Int
    @Def var cardsShown: Int
    @Def var cardsFilter: String
    @Def var cardsFilterReasons: [String]
    /// Keyword engine only.
    @Def var totalMatched: Int?

    enum CodingKeys: String, CodingKey {
        case reply, contacts, trail, truncated, stopped, spend, question, searched, engine, shape, context, conversation
        case conversationResolved = "conversation_resolved", cardsMatched = "cards_matched", cardsNamed = "cards_named"
        case cardsShown = "cards_shown", cardsFilter = "cards_filter", cardsFilterReasons = "cards_filter_reasons"
        case totalMatched = "total_matched"
    }
}

struct ChatsResponse: Codable, Sendable, Hashable {
    /// Most recently active first.
    @Def var chats: [ChatRow]
    @Def var total: Int
    /// 0 when there are no chats.
    @Def var newestID: Int
    @Def var adoption: ChatAdoption?
    @Def var limits: ChatLimits
    enum CodingKeys: String, CodingKey {
        case chats, total, adoption, limits
        case newestID = "newest_id"
    }
}

struct ChatAdoption: Codable, Sendable, Hashable {
    @Def var adopted: Int
    @Def var conversationID: Int
    @Def var title: String
    @Def var createdAt: String
    @Def var updatedAt: String
    enum CodingKeys: String, CodingKey {
        case adopted, title
        case conversationID = "conversation_id", createdAt = "created_at", updatedAt = "updated_at"
    }
}

struct ChatLimits: Codable, Sendable, Hashable, HasDefault {
    @Def var context: Int
    @Def var transcript: Int
    @Def var titleMax: Int
    static var defaultValue: ChatLimits {
        var l = ChatLimits()
        (l.context, l.transcript, l.titleMax) = (19, 500, 48)
        return l
    }
    enum CodingKeys: String, CodingKey {
        case context, transcript
        case titleMax = "title_max"
    }
}

struct ChatCreated: Codable, Sendable, Hashable {
    @Def var conversation: ChatRow
    @Def var created: Bool
}

struct ChatHistory: Codable, Sendable, Hashable {
    /// Oldest first.
    @Def var messages: [ChatMessage]
    /// nil only on the legacy route when no chat exists.
    @Def var conversation: ChatRow?
    @Def var conversationID: Int
    @Def var total: Int
    @Def var shown: Int
    @Def var trimmed: Int
    @Def var limit: Int
    enum CodingKeys: String, CodingKey {
        case messages, conversation, total, shown, trimmed, limit
        case conversationID = "conversation_id"
    }
}

/// A stored turn. Receipts (`searched`, `trail`, `spend`) are absent on old turns.
struct ChatMessage: Codable, Sendable, Hashable {
    /// "user" or "assistant".
    @Def var role: String
    @Def var content: String
    @Def var createdAt: String
    @Def var contacts: [AskCard]
    @Def var truncated: Bool
    /// Cards removed because that person is hidden now.
    @Def var contactsWithheld: Int?
    @Def var searched: Searched?
    @Def var trail: [TrailEntry]?
    @Def var spend: Spend?
    @Def var stopped: Bool?
    @Def var question: AskQuestion?
    enum CodingKeys: String, CodingKey {
        case role, content, contacts, truncated, searched, trail, spend, stopped, question
        case createdAt = "created_at", contactsWithheld = "contacts_withheld"
    }
}

struct ChatRenamed: Codable, Sendable, Hashable {
    @Def var conversation: ChatRow
    /// What was stored (normalised, maybe shortened, "" becomes "New chat").
    @Def var title: String
}

struct ChatDeleted: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var id: Int
    @Def var title: String
    @Def var turns: Int
    @Def var purged: Bool
}

struct ChatStopResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
    /// "stopping" or "unknown" (normal: the run already finished).
    @Def var status: String
    @Def var live: Int
}

// MARK: - Lists (section 6)

/// A row of `/api/lists`. Named CRMList so it never collides with SwiftUI's `List`.
struct CRMList: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var name: String
    @Def var createdAt: String
    @Def var eventDate: String
    @Def var venue: String
    /// A NUMBER here; an array in ListDetail.
    @Def var members: Int
    @Def var invited: Int
    /// Cumulative: replied + attending.
    @Def var replied: Int
    @Def var attending: Int
    enum CodingKeys: String, CodingKey {
        case id, name, venue, members, invited, replied, attending
        case createdAt = "created_at", eventDate = "event_date"
    }
}

struct ListsResponse: Codable, Sendable, Hashable {
    /// Newest first.
    @Def var lists: [CRMList]
}

struct ListDetail: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var name: String
    @Def var createdAt: String
    @Def var eventDate: String
    @Def var venue: String
    /// Each carries `listStatus`.
    @Def var members: [PersonSummary]
    @Def var counts: ListCounts
    enum CodingKeys: String, CodingKey {
        case id, name, venue, members, counts
        case createdAt = "created_at", eventDate = "event_date"
    }
}

struct ListCounts: Codable, Sendable, Hashable, HasDefault {
    @Def var members: Int
    @Def var invited: Int
    @Def var replied: Int
    @Def var attending: Int
    static var defaultValue: ListCounts { ListCounts() }
}

// MARK: - Status and settings (section 7)

struct EngineStatus: Codable, Sendable, Hashable {
    @Def var running: Bool
    @Def var mode: String
    @Def var dbPath: String
    @Def var host: String
    @Def var port: Int
    @Def var instance: String
    @Def var build: String
    /// Whether Ask can use the AI engine (an API key is set).
    @Def var ai: Bool
    /// Non-empty = a "your database was moved aside" sentence to show.
    @Def var dbRecovery: String
    @Def var counts: StatusCounts

    /// `counts.total` includes hidden and deleted rows; `excluded` is exactly those, so the
    /// difference is the People total (`/api/contacts` total) without loading anyone.
    var peopleCount: Int { max(0, counts.total - counts.excluded) }

    enum CodingKeys: String, CodingKey {
        case running, mode, host, port, instance, build, ai, counts
        case dbPath = "db_path", dbRecovery = "db_recovery"
    }
}

struct StatusCounts: Codable, Sendable, Hashable, HasDefault {
    @Def var total: Int
    @Def var new: Int
    @Def var described: Int
    @Def var skipped: Int
    @Def var excluded: Int
    static var defaultValue: StatusCounts { StatusCounts() }
}

struct SettingsResponse: Codable, Sendable, Hashable {
    /// Includes secret keys (masked). Never log or store this payload.
    @Def var settings: [SettingRow]
    @Def var diagnostics: JSONValue
    func value(for key: String) -> String? { settings.first { $0.key == key }?.value }
}

struct SettingRow: Codable, Sendable, Hashable, Identifiable {
    @Def var key: String
    @Def var value: String
    @Def var secret: Bool
    @Def var isSet: Bool
    var id: String { key }
    enum CodingKeys: String, CodingKey {
        case key, value, secret
        case isSet = "set"
    }
}

/// GET /api/data/linkedin-wait and POST /api/data/linkedin-asked (api_ledger.py, NC-319):
/// whether this install asked LinkedIn for its file and is still waiting for it.
struct LinkedInWait: Codable, Sendable, Hashable {
    @Def var waiting: Bool
    @Def var says: String
}

struct OnboardingState: Codable, Sendable, Hashable {
    @Def var done: Bool
}
