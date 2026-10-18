import Foundation

// The answers of crm/api_intake.py, as the paste sheet reads them. The shapes come from
// crm/intake.py: a proposal is `propose()` (intake.py:1093-1116), the report is `report()`
// (intake.py:1158-1322), the receipt is `commit_batch()` (intake.py:1442-1659).
//
// A parked draft goes back to the engine exactly as the web page parks it
// (intake.js:479-492), so a draft saved here opens in the web page and the other way
// round: the proposals and report are kept as the engine sent them, untouched.

struct PasteMatch: Codable, Sendable, Hashable, HasDefault {
    @Def var verdict: String
    @Def var contactID: Int?
    /// One sentence: who this matched, or why it was refused.
    @Def var reason: String
    static var defaultValue: PasteMatch { PasteMatch() }
    enum CodingKeys: String, CodingKey {
        case verdict, reason
        case contactID = "contact_id"
    }
}

struct PasteName: Codable, Sendable, Hashable, HasDefault {
    @Def var value: String
    /// A name the person typed counts as confirmed; one guessed from a LinkedIn address,
    /// an email or a model does not, and nobody is created from it (intake.py:1076-1090).
    @Def var confirmed: Bool
    /// "pasted", "linkedin_slug", "email", "llm"…
    @Def var source: String
    /// The matched person's own name, which intake never changes.
    @Def var existing: String
    static var defaultValue: PasteName { PasteName() }
}

struct PasteField: Codable, Sendable, Hashable {
    @Def var value: String
    @Def var writes: Bool
    @Def var current: String
    @Def var why: String
}

struct PasteIdentifier: Codable, Sendable, Hashable {
    /// "email" or "phone".
    @Def var kind: String
    @Def var value: String
    @Def var source: String
    @Def var writes: Bool
    @Def var why: String
}

struct PasteDeclined: Codable, Sendable, Hashable {
    @Def var field: String
    @Def var value: String
    @Def var why: String
}

struct PasteProvenance: Codable, Sendable, Hashable, HasDefault {
    /// "paste", "llm" or "web".
    @Def var origin: String
    @Def var line: Int
    @Def var sourceURL: String
    static var defaultValue: PasteProvenance { PasteProvenance() }
    enum CodingKeys: String, CodingKey {
        case origin, line
        case sourceURL = "source_url"
    }
}

/// One person as proposed. `ref` ("p3") is a label inside this batch, never a database id.
struct PasteProposal: Codable, Sendable, Hashable, Identifiable {
    @Def var ref: String
    /// new | matched | ambiguous | refused_excluded | refused_deleted
    @Def var verdict: String
    @Def var contactID: Int?
    @Def var duplicateOf: String
    @Def var match: PasteMatch
    @Def var name: PasteName
    @Def var fields: [String: PasteField]
    @Def var identifiers: [PasteIdentifier]
    @Def var note: String
    @Def var factsDeclined: [PasteDeclined]
    @Def var writes: [String]
    @Def var blockedBy: [String]
    @Def var provenance: PasteProvenance
    @Def var warnings: [String]
    var id: String { ref }
    enum CodingKeys: String, CodingKey {
        case ref, verdict, match, name, fields, identifiers, note, writes, provenance, warnings
        case contactID = "contact_id", duplicateOf = "duplicate_of"
        case factsDeclined = "facts_declined", blockedBy = "blocked_by"
    }
}

struct PasteAccounting: Codable, Sendable, Hashable, HasDefault {
    @Def var linesUnexplained: Int
    @Def var recordsUnexplained: Int
    static var defaultValue: PasteAccounting { PasteAccounting() }
    enum CodingKeys: String, CodingKey {
        case linesUnexplained = "lines_unexplained", recordsUnexplained = "records_unexplained"
    }
}

struct PasteSpend: Codable, Sendable, Hashable {
    @Def var usd: Double
    @Def var inputTokens: Int
    @Def var outputTokens: Int
    @Def var model: String
    enum CodingKeys: String, CodingKey {
        case usd, model
        case inputTokens = "input_tokens", outputTokens = "output_tokens"
    }
}

/// `report.model`: empty for the plain read; what the model call cost when one ran.
struct PasteModelMeta: Codable, Sendable, Hashable, HasDefault {
    @Def var spend: PasteSpend?
    static var defaultValue: PasteModelMeta { PasteModelMeta() }
}

/// A line or record that was not proposed, and why.
struct PasteSkipped: Codable, Sendable, Hashable {
    @Def var line: Int
    @Def var bucket: String
    @Def var name: String
    @Def var text: String
    @Def var reason: String
}

struct PasteReport: Codable, Sendable, Hashable, HasDefault {
    @Def var wouldCreate: Int
    @Def var wouldEnrich: Int
    @Def var blocked: Int
    @Def var recordsParsed: Int
    @Def var linesSeen: Int
    /// "" or the sentence saying the plain read ran instead of the model, and why.
    @Def var degraded: String
    @Def var accounting: PasteAccounting
    /// The module's own sentences, shown verbatim (intake.js:177-180).
    @Def var sentences: [String]
    @Def var factsDeclinedByField: [String: Int]
    @Def var linkedinUrlNote: String
    @Def var model: PasteModelMeta
    @Def var skippedDetail: [PasteSkipped]
    static var defaultValue: PasteReport { PasteReport() }
    enum CodingKeys: String, CodingKey {
        case blocked, degraded, accounting, sentences, model
        case wouldCreate = "would_create", wouldEnrich = "would_enrich", recordsParsed = "records_parsed"
        case linesSeen = "lines_seen", factsDeclinedByField = "facts_declined_by_field"
        case linkedinUrlNote = "linkedin_url_note", skippedDetail = "skipped_detail"
    }
}

/// `POST /api/intake/parse` and `/propose`: the identical shape (api_intake.py:33-36).
struct PastePlanAnswer: Codable, Sendable, Hashable {
    @Def var proposals: [PasteProposal]
    @Def var report: PasteReport
    @Def var allowedLookupUrls: [String]
    enum CodingKeys: String, CodingKey {
        case proposals, report
        case allowedLookupUrls = "allowed_lookup_urls"
    }
}

/// A page `/api/intake/lookup` read, plus the pasted address it came from. The address is
/// kept here only: the engine never echoes a URL, because a link can be a password.
struct PastePage: Codable, Sendable, Hashable {
    @Def var url: String
    @Def var host: String
    @Def var bytes: Int
    @Def var contentType: String
    @Def var text: String
    @Def var charsExtracted: Int
    @Def var charsDropped: Int
    @Def var charsCap: Int
    @Def var trusted: Bool
    @Def var note: String
    enum CodingKeys: String, CodingKey {
        case url, host, bytes, text, trusted, note
        case contentType = "content_type", charsExtracted = "chars_extracted"
        case charsDropped = "chars_dropped", charsCap = "chars_cap"
    }
}
extension PastePage: HasDefault { static var defaultValue: PastePage { PastePage() } }

struct PasteLookupAnswer: Codable, Sendable, Hashable {
    @Def var page: PastePage
    @Def var sentences: [String]
}

/// `GET /api/intake/batch` → `{batch: {}}`, `{batch: {error}}` or the parked draft.
struct PasteDraft: Codable, Sendable, Hashable, HasDefault {
    @Def var text: String
    @Def var proposals: [PasteProposal]
    @Def var report: PasteReport
    @Def var allowedLookupUrls: [String]
    @Def var pages: [PastePage]
    /// ref → {field: text, name_confirmed: bool}, as the web page keeps them.
    @Def var edits: [String: [String: JSONValue]]
    @Def var skips: [String: Bool]
    @Def var error: String
    static var defaultValue: PasteDraft { PasteDraft() }
    enum CodingKeys: String, CodingKey {
        case text, proposals, report, pages, edits, skips, error
        case allowedLookupUrls = "allowed_lookup_urls"
    }
}

struct PasteDraftAnswer: Codable, Sendable, Hashable {
    @Def var batch: PasteDraft
}

struct PasteParked: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var bytes: Int
    @Def var sentences: [String]
}

struct PasteOutcome: Codable, Sendable, Hashable, Identifiable {
    @Def var index: Int
    @Def var name: String
    @Def var verdict: String
    @Def var contactID: Int?
    @Def var wrote: [String]
    @Def var kept: [String]
    /// "" or the sentence saying why nothing was written for this person.
    @Def var refused: String
    /// The engine's line for this person in words ("Saved company and email.").
    /// `wrote`/`kept` hold code names such as how_we_met; "" from an older engine.
    @Def var said: String
    var id: Int { index }
    enum CodingKeys: String, CodingKey {
        case index, name, verdict, wrote, kept, refused, said
        case contactID = "contact_id"
    }
}

struct PasteReceiptAccounting: Codable, Sendable, Hashable, HasDefault {
    @Def var submitted: Int
    @Def var accounted: Int
    @Def var unaccounted: Int
    static var defaultValue: PasteReceiptAccounting { PasteReceiptAccounting() }
}

/// `POST /api/intake/commit`: what ACTUALLY happened, not what was asked for.
struct PasteReceipt: Codable, Sendable, Hashable {
    @Def var created: Int
    @Def var enriched: Int
    @Def var unchanged: Int
    @Def var submitted: Int
    @Def var outcomes: [PasteOutcome]
    @Def var wroteAnything: Bool
    @Def var accounting: PasteReceiptAccounting
    @Def var sentences: [String]
    enum CodingKeys: String, CodingKey {
        case created, enriched, unchanged, submitted, outcomes, accounting, sentences
        case wroteAnything = "wrote_anything"
    }
}

/// `GET /api/intake/status`: what works, what is off, and every limit (api_intake.py:453-494).
struct PasteIntakeStatus: Codable, Sendable, Hashable {
    /// An Anthropic key is set, so "Read it with the model" is offered.
    @Def var llm: Bool
    @Def var webLookup: Bool
    @Def var model: String
    /// NC_INTAKE_* limits; numbers, except NC_INTAKE_WEB which is "0" or "1".
    @Def var settings: [String: JSONValue]
    @Def var writes: [String]
    @Def var factsDeclined: [String]
    @Def var matchKeys: [String]
    @Def var linkedinUrlNote: String
    @Def var sentences: [String]
    enum CodingKeys: String, CodingKey {
        case llm, model, settings, writes, sentences
        case webLookup = "web_lookup", factsDeclined = "facts_declined"
        case matchKeys = "match_keys", linkedinUrlNote = "linkedin_url_note"
    }

    func limit(_ key: String) -> Int? {
        switch settings[key] {
        case .int(let i): i
        case .number(let d): Int(d)
        case .string(let s): Int(s)
        default: nil
        }
    }

    /// The limits in the engine's own order (api_intake.py:466-474), then any new ones.
    var settingKeys: [String] {
        let known = ["NC_INTAKE_PASTE_MAX_CHARS", "NC_INTAKE_MAX_PEOPLE", "NC_INTAKE_WEB", "NC_INTAKE_WEB_TIMEOUT_SEC",
                     "NC_INTAKE_WEB_MAX_KB", "NC_INTAKE_WEB_MAX_LOOKUPS", "NC_INTAKE_MAX_TOKENS"]
        return known.filter { settings[$0] != nil } + settings.keys.filter { !known.contains($0) }.sorted()
    }
}

// Prefixed, because other screens may give JSONValue helpers of their own.
extension JSONValue {
    /// The text of a string value; "" for anything else.
    var pasteString: String {
        if case .string(let s) = self { return s }
        return ""
    }

    /// JavaScript's `!!value`.
    var pasteTruthy: Bool {
        switch self {
        case .null: false
        case .bool(let b): b
        case .int(let i): i != 0
        case .number(let d): d != 0 && !d.isNaN
        case .string(let s): !s.isEmpty
        case .array, .object: true
        }
    }

    func pasteKey(_ key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }
}

enum PasteJSON {
    /// Decodes a typed view of an answer that is also kept raw. A shape problem becomes
    /// the same EngineError a direct decode would give (coding path only, no values).
    static func typed<T: Decodable>(_ raw: JSONValue, as: T.Type, route: String) throws -> T {
        let data = try JSONEncoder().encode(raw)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch let e as DecodingError {
            let (path, problem) = EngineClient.describe(e)
            throw EngineError.decoding(route: route, path: path, problem: problem)
        }
    }

    static func value<T: Encodable>(_ v: T) -> JSONValue {
        guard let data = try? JSONEncoder().encode(v),
              let raw = try? JSONDecoder().decode(JSONValue.self, from: data) else { return .null }
        return raw
    }
}
