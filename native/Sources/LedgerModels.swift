import Foundation

// The answers the Data and Settings pages read, as Codable structs, in the forgiving style
// of Models.swift (a missing key or a null becomes the field's default; --selftest decodes
// strictly). Every name starts with Ledger, Data, Merge or Settings so nothing clashes with
// the shared Models.
//
//   GET  /api/data/ledger            LedgerAnswer          crm/api_ledger.py:1566-1585
//   GET  /api/settings/ledger        SettingsLedgerAnswer  crm/api_abilities.py:266-278
//   GET  /api/provenance?source=     DataProvenanceAnswer  crm/api_provenance.py:569-585
//   GET  /api/duplicates             DataDuplicatesAnswer  crm/api_duplicates.py:22-46
//   POST /api/merge                  WriteResult (kept_id) crm/api_duplicates.py:49-62
//   POST /api/import/run             DataImportResult      crm/api_import.py:841-941
//   POST /api/import/open            DataOpenResult        crm/api_import.py:547-563
//   POST /api/settings               SettingsSaveResult    crm/api_instance.py:531-562
//   GET  /api/owner, POST …/goals    SettingsOwner, SettingsGoalsSaved  crm/owner.py:140-167
//   GET  /api/diagnostics            SettingsDiagnostics   crm/api_instance.py:165-264
//   POST /api/semantic/install       SettingsSemanticStarted  crm/api_instance.py:490-528

// MARK: - The shared row (both ledgers)

/// A row's state: the dot's tone and its words. Tones: ok, off, behind, blocked, paused,
/// na (not in this copy), ready (waiting for you). (`static/app/ledger.js:116-119`)
struct LedgerState: Codable, Sendable, Hashable, HasDefault {
    @Def var tone: String
    @Def var says: String
    static var defaultValue: LedgerState { LedgerState() }
}

/// What switching a row on costs the person, on one scale (`crm/api_ledger.py:84-97`).
struct LedgerCost: Codable, Sendable, Hashable, HasDefault {
    @Def var kind: String
    @Def var rank: Int
    @Def var label: String
    @Def var tone: String
    static var defaultValue: LedgerCost { LedgerCost() }
}

/// A number that says which people it counts (`crm/figures.py` figure()).
struct LedgerFigure: Codable, Sendable, Hashable, HasDefault {
    @Def var n: Int
    @Def var of: Int?
    @Def var unit: String
    @Def var says: String
    @Def var population: String
    @Def var populationSays: String
    @Def var share: Double?
    @Def var side: String
    @Def var sideSays: String
    static var defaultValue: LedgerFigure { LedgerFigure() }
    enum CodingKeys: String, CodingKey {
        case n, of, unit, says, population, share, side
        case populationSays = "population_says", sideSays = "side_says"
    }
}

struct LedgerHave: Codable, Sendable, Hashable, HasDefault {
    @Def var says: String
    @Def var figures: [LedgerFigure]
    static var defaultValue: LedgerHave { LedgerHave() }
}

/// One control a row or a need carries. Which fields are filled depends on `kind`:
///   import   source, verb, pick, folder, needs_path, preview, path?, path_says?, form?
///            (`form`: extra fields the run sends with it. NC-372: LinkedIn profile
///            facts' Read shared history is that source's run with {"extras": true})
///   switch   key, label, on
///   secret   key, label, set, masked, placeholder
///   text     key, label, value, placeholder, name?
///   open     target, label            (POST /api/import/open {source: target})
///   row      row, label               (open another row on the same page)
///   settings row?, label              (Data → the Settings page, on that row)
///   page     page, row?, label        (Settings → the Data page, on that row)
///   helper   state                    (the Full Disk Access helper; its own sheet)
///   gmail                             (Gmail setup; its own sheet)
///   model    key, value               (Settings: the model the paid features use)
///   goals, semantic                   (Settings: their own panels)
struct LedgerAct: Codable, Sendable, Hashable {
    @Def var kind: String
    @Def var key: String
    @Def var label: String
    @Def var name: String
    @Def var on: Bool
    @Def var isSet: Bool
    @Def var masked: String
    @Def var placeholder: String
    @Def var value: String
    @Def var source: String
    @Def var verb: String
    @Def var pick: String
    @Def var folder: Bool
    @Def var needsPath: Bool
    @Def var preview: Bool
    @Def var path: String
    @Def var pathSays: String
    @Def var target: String
    @Def var row: String
    @Def var page: String
    @Def var state: String
    @Def var form: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case kind, key, label, name, on, masked, placeholder, value, source, verb, pick, folder, preview, path
        case target, row, page, state, form
        case isSet = "set", needsPath = "needs_path", pathSays = "path_says"
    }
}

/// NC-231: one of the three passes folded into the "Worked out on this Mac" row.
struct LedgerPart: Codable, Sendable, Hashable, Identifiable {
    @Def var key: String
    @Def var label: String
    @Def var inUse: Bool
    @Def var state: LedgerState
    @Def var lastRunSeconds: Double?
    @Def var provenance: String
    @Def var have: String
    var id: String { key }
    enum CodingKeys: String, CodingKey {
        case key, label, state, provenance, have
        case inUse = "in_use", lastRunSeconds = "last_run_seconds"
    }
}

struct LedgerDetail: Codable, Sendable, Hashable, HasDefault {
    @Def var runs: String
    @Def var lastRun: String
    @Def var lastRunSeconds: Double?
    @Def var lastRunCaveat: String
    @Def var help: String
    @Def var manual: String
    @Def var acts: [LedgerAct]
    @Def var provenance: String
    @Def var parts: [LedgerPart]?
    static var defaultValue: LedgerDetail { LedgerDetail() }
    enum CodingKeys: String, CodingKey {
        case runs, help, manual, acts, provenance, parts
        case lastRun = "last_run", lastRunSeconds = "last_run_seconds", lastRunCaveat = "last_run_caveat"
    }
}

/// One line of either ledger (`crm/api_ledger.py:469-494`, `crm/api_abilities.py:64-70`).
struct LedgerRow: Codable, Sendable, Hashable, Identifiable {
    @Def var key: String
    @Def var label: String
    @Def var sources: [String]
    @Def var gives: String
    @Def var have: LedgerHave
    @Def var takes: String
    @Def var cost: LedgerCost
    @Def var state: LedgerState
    @Def var inUse: Bool
    @Def var blocked: String
    /// A row in use: when it last ran, said where the others say what they take (NC-233).
    @Def var when: String
    @Def var detail: LedgerDetail
    var id: String { key }
    enum CodingKeys: String, CodingKey {
        case key, label, sources, gives, have, takes, cost, state, blocked, when, detail
        case inUse = "in_use"
    }
}

/// One thing the app cannot do without you (`crm/api_ledger.py:1445-1492`).
struct LedgerNeed: Codable, Sendable, Hashable {
    @Def var row: String
    @Def var source: String
    @Def var tone: String
    @Def var says: String
    @Def var cost: String
    @Def var acts: [LedgerAct]
}

// MARK: - GET /api/data/ledger

struct LedgerNewest: Codable, Sendable, Hashable {
    @Def var row: String
    @Def var label: String
    @Def var at: String
    @Def var seconds: Double?
}

struct LedgerNext: Codable, Sendable, Hashable {
    @Def var row: String
    @Def var label: String
    @Def var takes: String
}

struct LedgerPeopleParts: Codable, Sendable, Hashable, HasDefault {
    @Def var active: Int
    @Def var hidden: Int
    @Def var deleted: Int
    static var defaultValue: LedgerPeopleParts { LedgerPeopleParts() }
}

struct LedgerPeople: Codable, Sendable, Hashable, HasDefault {
    @Def var total: Int
    @Def var figure: LedgerFigure?
    @Def var parts: LedgerPeopleParts
    @Def var says: String
    @Def var unexplained: Int
    @Def var explained: Int
    static var defaultValue: LedgerPeople { LedgerPeople() }
}

struct LedgerMessages: Codable, Sendable, Hashable, HasDefault {
    @Def var figure: LedgerFigure?
    @Def var byChannel: [String: Int]
    static var defaultValue: LedgerMessages { LedgerMessages() }
    enum CodingKeys: String, CodingKey {
        case figure
        case byChannel = "by_channel"
    }
}

struct LedgerDiskPart: Codable, Sendable, Hashable {
    @Def var name: String
    @Def var bytes: Int
}

struct LedgerDisk: Codable, Sendable, Hashable, HasDefault {
    @Def var bytes: Int
    @Def var named: [LedgerDiskPart]
    static var defaultValue: LedgerDisk { LedgerDisk() }
}

struct LedgerInstall: Codable, Sendable, Hashable, HasDefault {
    @Def var people: LedgerPeople
    @Def var messages: LedgerMessages
    @Def var disk: LedgerDisk
    @Def var empty: Bool
    /// Groups of possible duplicates; nil when they could not be counted just now.
    @Def var duplicates: Int?
    static var defaultValue: LedgerInstall { LedgerInstall() }
}

struct LedgerAnswer: Codable, Sendable, Hashable {
    @Def var rows: [LedgerRow]
    @Def var needs: [LedgerNeed]
    @Def var next: LedgerNext?
    @Def var inUse: Int
    @Def var newest: LedgerNewest?
    @Def var install: LedgerInstall
    /// True only on a `?scan=1` answer: the page looked for files on this Mac.
    @Def var scanned: Bool
    @Def var scanFailed: Bool
    /// The engine's own ceiling on one import, in seconds (NC-176).
    @Def var importTimeoutSec: Int?
    @Def var populationSays: String
    enum CodingKeys: String, CodingKey {
        case rows, needs, next, newest, install, scanned
        case inUse = "in_use", scanFailed = "scan_failed", importTimeoutSec = "import_timeout_sec"
        case populationSays = "population_says"
    }
}

// MARK: - GET /api/settings/ledger

struct SettingsInstallFacts: Codable, Sendable, Hashable, HasDefault {
    @Def var version: String
    @Def var updatesSays: String
    @Def var dataFolder: LedgerAct?
    static var defaultValue: SettingsInstallFacts { SettingsInstallFacts() }
    enum CodingKeys: String, CodingKey {
        case version
        case updatesSays = "updates_says", dataFolder = "data_folder"
    }
}

struct SettingsLedgerAnswer: Codable, Sendable, Hashable {
    @Def var rows: [LedgerRow]
    @Def var needs: [LedgerNeed]
    @Def var install: SettingsInstallFacts
}

// MARK: - GET /api/provenance?source=

struct DataProvenanceKind: Codable, Sendable, Hashable, Identifiable {
    @Def var kind: String
    @Def var label: String
    /// nil = it cannot be counted at all; 0 = nothing yet.
    @Def var count: Int?
    /// "exact" counts are drawn firm; the rest are soft, with the reason underneath.
    @Def var certainty: String
    @Def var ofTotal: Int
    @Def var detail: String
    @Def var note: String
    var id: String { kind }
    enum CodingKeys: String, CodingKey {
        case kind, label, count, certainty, detail, note
        case ofTotal = "of_total"
    }
}

struct DataProvenancePanel: Codable, Sendable, Hashable {
    @Def var source: String
    @Def var label: String
    @Def var recordsNothing: Bool
    @Def var note: String
    @Def var kinds: [DataProvenanceKind]
    @Def var notRecordedNote: String
    enum CodingKeys: String, CodingKey {
        case source, label, note, kinds
        case recordsNothing = "records_nothing", notRecordedNote = "not_recorded_note"
    }
}

struct DataProvenanceAnswer: Codable, Sendable, Hashable {
    @Def var panels: [DataProvenancePanel]
}

// MARK: - GET /api/duplicates

/// One person inside a group of possible duplicates (`crm/api_duplicates.py:22-41`).
struct DataDupMember: Codable, Sendable, Hashable, Identifiable {
    @Def var id: Int
    @Def var name: String
    @Def var company: String
    @Def var source: String
    @Def var roleType: String
    @Def var homeMetro: String
    @Def var topics: [String]
    @Def var blurb: String
    @Def var counts: ChannelCounts
    @Def var last: String
    enum CodingKeys: String, CodingKey {
        case id, name, company, source, topics, blurb, counts, last
        case roleType = "role_type", homeMetro = "home_metro"
    }
}

struct DataDuplicatesAnswer: Codable, Sendable, Hashable {
    @Def var groups: [[DataDupMember]]
}

// MARK: - POST /api/import/run, /api/import/open

struct DataReceipt: Codable, Sendable, Hashable {
    @Def var verb: String
    @Def var count: Int
}

struct DataImportResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
    @Def var output: String
    @Def var receipts: [DataReceipt]
    @Def var newlyAdded: Int
    @Def var enriched: [DataReceipt]
    @Def var dryRun: Bool
    enum CodingKeys: String, CodingKey {
        case ok, output, receipts, enriched
        case newlyAdded = "newly_added", dryRun = "dry_run"
    }
}

struct DataOpenResult: Codable, Sendable, Hashable {
    @Def var ok: Bool
}

// MARK: - Settings writes and reads

/// `POST /api/settings` answers the whole settings list too; only the warning is read.
struct SettingsSaveResult: Codable, Sendable, Hashable {
    @Def var secured: Bool
    @Def var warning: String?
}

struct SettingsOwner: Codable, Sendable, Hashable {
    @Def var name: String
    @Def var email: String
    @Def var goals: String
    @Def var maxChars: Int
    enum CodingKeys: String, CodingKey {
        case name, email, goals
        case maxChars = "max_chars"
    }
}

struct SettingsGoalsSaved: Codable, Sendable, Hashable {
    @Def var goals: String
    @Def var maxChars: Int
    @Def var truncated: Bool
    enum CodingKeys: String, CodingKey {
        case goals, truncated
        case maxChars = "max_chars"
    }
}

struct SettingsSemanticInstall: Codable, Sendable, Hashable, HasDefault {
    /// idle, running, succeeded, failed
    @Def var state: String
    @Def var message: String
    @Def var tail: [String]
    @Def var restartRequired: Bool
    static var defaultValue: SettingsSemanticInstall { SettingsSemanticInstall() }
    enum CodingKeys: String, CodingKey {
        case state, message, tail
        case restartRequired = "restart_required"
    }
}

struct SettingsSemanticStatus: Codable, Sendable, Hashable, HasDefault {
    @Def var available: Bool
    @Def var reason: String
    @Def var sizeGB: Int
    @Def var install: SettingsSemanticInstall
    static var defaultValue: SettingsSemanticStatus { SettingsSemanticStatus() }
    enum CodingKeys: String, CodingKey {
        case available, reason, install
        case sizeGB = "size_gb"
    }
}

struct SettingsSyncSummary: Codable, Sendable, Hashable, HasDefault {
    @Def var oldest: String?
    @Def var oldestLabel: String?
    @Def var newest: String?
    @Def var collecting: Int
    @Def var never: [String]
    static var defaultValue: SettingsSyncSummary { SettingsSyncSummary() }
    enum CodingKeys: String, CodingKey {
        case oldest, newest, collecting, never
        case oldestLabel = "oldest_label"
    }
}

/// The read-only figures about this Mac (`crm/api_instance.py:165-264`). Paths and sizes
/// only describe the app's own data folder.
struct SettingsDiagnostics: Codable, Sendable, Hashable {
    @Def var crmDbBytes: Int
    @Def var archiveDbBytes: Int
    @Def var diskBytes: Int
    @Def var archivedMessages: Int
    @Def var people: LedgerPeople
    @Def var notes: Int
    @Def var photos: Int
    @Def var syncSummary: SettingsSyncSummary
    @Def var semanticSearch: SettingsSemanticStatus
    enum CodingKeys: String, CodingKey {
        case people, notes, photos
        case crmDbBytes = "crm_db_bytes", archiveDbBytes = "archive_db_bytes", diskBytes = "disk_bytes"
        case archivedMessages = "archived_messages", syncSummary = "sync_summary"
        case semanticSearch = "semantic_search"
    }
}

struct SettingsSemanticStarted: Codable, Sendable, Hashable {
    @Def var started: Bool
    @Def var message: String
    @Def var status: SettingsSemanticStatus?
}
