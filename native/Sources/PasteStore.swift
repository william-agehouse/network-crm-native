import Foundation
import Observation

// The paste sheet's state and every engine call it makes. The web page's version is
// static/app/intake.js against crm/api_intake.py; all eight routes are called from here:
//
//   GET    /api/intake/status   what works, what is off, every limit (no database, no request)
//   POST   /api/intake/parse    {text} the plain read: no key, no network, writes nothing
//   POST   /api/intake/propose  {text, pages:[{host,text}]} the model read — offered only when
//                               status.llm says a key is set (it sends the paste to Anthropic)
//   POST   /api/intake/lookup   {url, text} read ONE pasted link (web lookup must be on)
//   POST   /api/intake/commit   {batch:[item], confirm:true} THE ONLY WRITER
//   GET    /api/intake/batch    the parked draft, or {}
//   POST   /api/intake/batch    {batch:{text, proposals, report, allowed_lookup_urls, pages, edits, skips}}
//   DELETE /api/intake/batch    discard the draft
//
// The commit sends only intake.COMMIT_KEYS for each person (intake.py:1368-1369), never a
// ref, a verdict or a contact id: the engine re-runs the matcher and decides who each
// person is (intake.js:16-23).

/// One proposal with what is on screen for it now.
struct PasteRow: Identifiable, Sendable {
    let p: PasteProposal
    let item: PasteItem
    /// What still stops this row being written, judged against the text on screen.
    let blocked: [String]
    let skip: Bool
    var id: String { p.ref }
}

/// The item `commit_batch` reads, built from one proposal plus whatever was edited.
// from static/app/intake.js:99-123
struct PasteItem: Sendable, Hashable {
    var name = ""
    var nameConfirmed = false
    var company = ""
    var position = ""
    var howWeMet = ""
    var linkedinURL = ""
    var email = ""
    var phone = ""
    var note = ""
    var origin = "paste"

    subscript(key: String) -> String {
        switch key {
        case "name": name
        case "company": company
        case "position": position
        case "how_we_met": howWeMet
        case "linkedin_url": linkedinURL
        case "email": email
        case "phone": phone
        case "note": note
        default: ""
        }
    }

    /// The commit body's item: exactly intake.COMMIT_KEYS.
    var json: JSONValue {
        [
            "name": .string(name), "name_confirmed": .bool(nameConfirmed), "company": .string(company),
            "position": .string(position), "how_we_met": .string(howWeMet), "linkedin_url": .string(linkedinURL),
            "email": .string(email), "phone": .string(phone), "note": .string(note), "origin": .string(origin),
        ]
    }
}

/// The fields a review row can show, in the web page's order (intake.js:78-87).
enum PasteFields {
    static let all: [(key: String, label: String)] = [
        ("name", "Name"), ("company", "Company"), ("position", "Position"), ("email", "Email"),
        ("phone", "Phone"), ("linkedin_url", "LinkedIn"), ("how_we_met", "How you met"), ("note", "Note"),
    ]
    /// Match keys, read from `proposal.identifiers` rather than `proposal.fields`.
    static let identifiers: Set<String> = ["email", "phone"]

    /// Verdict → how the row reads (intake.js:71-77).
    static func verdict(_ v: String) -> (label: String, tone: SetupTone) {
        switch v {
        case "new": ("New person", .plain)
        case "matched": ("Already in your CRM", .plain)
        case "ambiguous": ("Matches more than one", .warn)
        // NC-361. The People page calls this list Hidden contacts; one list, one name.
        case "refused_excluded": ("In Hidden contacts", .bad)
        case "refused_deleted": ("You deleted this person", .bad)
        default: (v, .warn)
        }
    }
}

/// A plan on screen: typed for drawing, raw for parking it exactly as the engine sent it.
struct PastePlan: Sendable {
    var proposals: [PasteProposal]
    var report: PasteReport
    var allowedLookupUrls: [String]
    var rawProposals: JSONValue
    var rawReport: JSONValue
}

/// A parked draft found when the sheet opened: offered, never silently restored.
struct PasteDraftOffer: Sendable {
    let draft: PasteDraft
    let raw: JSONValue
}

@MainActor
@Observable
final class PasteStore {
    enum Phase: Sendable { case paste, review, done }
    enum Busy: String, Sendable { case none, parse, propose, lookup, park, discard, commit }

    private(set) var status: PasteIntakeStatus?
    var text = ""
    private(set) var plan: PastePlan?
    private(set) var edits: [String: [String: JSONValue]] = [:]
    private(set) var skips: [String: Bool] = [:]
    /// Pages read by lookup. Never sent anywhere on their own (intake.js:320-325).
    private(set) var pages: [PastePage] = []
    private(set) var receipt: PasteReceipt?
    private(set) var draft: PasteDraftOffer?
    /// The line beside the bar after parking ("The draft was saved. … 3.2 KB.").
    private(set) var saved = ""
    private(set) var busy: Busy = .none
    /// One sentence when something failed; what was typed stays.
    private(set) var err = ""
    private(set) var notice: SetupNotice?
    var confirming = false
    private(set) var loaded = false

    /// What the pinned line under the sheet's title shows: a failure first, then the last
    /// action's answer.
    var notices: [SetupNotice] {
        var out: [SetupNotice] = []
        if !err.isEmpty { out.append(SetupNotice(text: err, tone: .bad)) }
        if let notice { out.append(notice) }
        return out
    }

    /// Set by `start(app:)`; the self-test sets them directly.
    @ObservationIgnored var client: EngineClient?
    /// Called after a commit that wrote something: People refetches, sidebar counts update.
    @ObservationIgnored var onWrote: (@MainActor () async -> Void)?
    @ObservationIgnored private var isPreview = false

    // The two waits api()'s 20 s default is wrong for (intake.js:59-66).
    static let modelTimeout: TimeInterval = 120
    static let commitTimeout: TimeInterval = 60
    static let lookupSlack: TimeInterval = 8

    init() {}

    /// For `--snapshot` only: a sheet in a given state, drawn without the engine.
    init(previewStatus: PasteIntakeStatus?, text: String = "", plan: PastePlan? = nil,
         receipt: PasteReceipt? = nil, draft: PasteDraftOffer? = nil) {
        self.status = previewStatus
        self.text = text
        self.plan = plan
        self.receipt = receipt
        self.draft = draft
        self.loaded = true
        self.isPreview = true
    }

    /// `settle: false` leaves the snapshot waiting, for a picture that needs one more step.
    func start(app: AppModel, settle: Bool = true) async {
        if client == nil { client = app.client }
        if onWrote == nil {
            onWrote = { [weak app] in
                app?.peopleChanged()
                await app?.refreshCounts()
            }
        }
        reopened()
        if !isPreview { await load() }
        if settle { app.markSettled() }
    }

    /// The sheet opened again on a remembered store: a batch saved last time is finished
    /// business, so it starts empty; anything else carries on where it was left. Old
    /// answers go, because they answered something that is no longer on screen.
    private func reopened() {
        guard loaded, !isPreview else { return }
        if phase == .done { startOver() }
        notice = nil
        err = ""
        confirming = false
    }

    /// Status and the parked draft, side by side (intake.js:378-401).
    func load() async {
        guard let client else { return }
        async let s: PasteIntakeStatus = client.get("/api/intake/status", as: PasteIntakeStatus.self)
        async let b: JSONValue = client.get("/api/intake/batch", as: JSONValue.self)
        do {
            status = try await s
        } catch is CancellationError {
        } catch {
            err = SetupWords.sentence(error)
        }
        // A missing draft is not worth a message; an unreadable one says so.
        if let raw = try? await b, let batch = raw.pasteKey("batch"),
           let d = try? PasteJSON.typed(batch, as: PasteDraft.self, route: "/api/intake/batch") {
            if !d.error.isEmpty {
                err = d.error
            } else if !d.proposals.isEmpty {
                draft = PasteDraftOffer(draft: d, raw: batch)
            } else {
                // Discarded or spent since this sheet last looked (the web page, a commit).
                draft = nil
            }
        }
        loaded = true
    }

    // MARK: Derived

    var phase: Phase {
        if receipt?.wroteAnything == true { return .done }
        return plan == nil ? .paste : .review
    }

    var pasteMax: Int { status?.limit("NC_INTAKE_PASTE_MAX_CHARS") ?? 0 }
    /// Characters as the engine counts them: Python's len() counts code points, so an
    /// emoji or an accented letter typed as two parts counts the way the limit does.
    var textLength: Int { text.unicodeScalars.count }
    var overLimit: Int { pasteMax > 0 ? max(0, textLength - pasteMax) : 0 }

    var rows: [PasteRow] {
        (plan?.proposals ?? []).map { p in
            let item = Self.item(for: p, edit: edits[p.ref])
            return PasteRow(p: p, item: item, blocked: Self.blockers(p, item), skip: skips[p.ref] == true)
        }
    }

    struct Counts: Sendable {
        var live = 0, create = 0, enrich = 0, refuse = 0, skipped = 0
        var write: Int { create + enrich }
    }

    // from static/app/intake.js:516-525
    var counts: Counts {
        let all = rows
        let live = all.filter { !$0.skip }
        var c = Counts()
        c.live = live.count
        c.create = live.filter { $0.p.verdict == "new" && $0.blocked.isEmpty }.count
        c.enrich = live.filter { $0.p.verdict == "matched" && $0.blocked.isEmpty }.count
        c.refuse = live.filter { !$0.blocked.isEmpty }.count
        c.skipped = all.count - live.count
        return c
    }

    /// The confirm sentence (intake.js:653-657).
    var confirmSentence: String {
        let c = counts
        var s = "Add \(SetupWords.people(c.create)) and fill empty fields on \(SetupWords.people(c.enrich))."
        if c.refuse > 0 { s += " \(c.refuse) will be refused, with the reasons above." }
        if c.skipped > 0 { s += " \(c.skipped) skipped \(c.skipped == 1 ? "row is" : "rows are") not sent." }
        return s
    }

    // from static/app/intake.js:99-123
    static func item(for p: PasteProposal, edit: [String: JSONValue]?) -> PasteItem {
        let e = edit ?? [:]
        func field(_ key: String) -> String {
            if let v = e[key] { return v.pasteString }
            if PasteFields.identifiers.contains(key) { return p.identifiers.first { $0.kind == key }?.value ?? "" }
            if key == "note" { return p.note }
            if key == "name" { return p.name.value }
            return p.fields[key]?.value ?? ""
        }
        var item = PasteItem()
        item.name = field("name")
        item.nameConfirmed = e["name_confirmed"].map(\.pasteTruthy) ?? p.name.confirmed
        item.company = field("company")
        item.position = field("position")
        item.howWeMet = field("how_we_met")
        item.linkedinURL = field("linkedin_url")
        item.email = field("email")
        item.phone = field("phone")
        item.note = field("note")
        item.origin = p.provenance.origin.isEmpty ? "paste" : p.provenance.origin
        return item
    }

    /// The two name refusals are recomputed from the text on screen; everything else the
    /// engine said is kept as written (intake.js:125-152).
    static func blockers(_ p: PasteProposal, _ item: PasteItem) -> [String] {
        var out: [String] = []
        if p.verdict == "new" {
            if item.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                out.append("A new person needs a name. Type one, or nobody is created.")
            } else if !item.nameConfirmed {
                // Where the name came from, in words (intake.py SOURCE_WORDS): the raw
                // value made this read "a guess from the linkedin slug".
                let words = ["linkedin_slug": "LinkedIn address", "email_local": "email address",
                             "llm": "model", "pasted_line": "pasted line", "pasted": "paste"]
                let src = words[p.name.source]
                    ?? (p.name.source.isEmpty ? "paste" : p.name.source).replacingOccurrences(of: "_", with: " ")
                out.append("The name “\(item.name)” is a guess from the \(src). Confirm it below. Nobody is created from a guessed name.")
            }
        }
        for b in p.blockedBy where !(b.contains("is a guess from the") || b.contains("needs a name")) {
            out.append(b)
        }
        return out
    }

    /// Whether typing in a field could change anything: the engine fills empty columns
    /// only and never renames a match, so a box over a filled column would be a control
    /// that cannot work (intake.js:220-236).
    static func writable(_ p: PasteProposal, _ key: String) -> Bool {
        if p.verdict == "new" { return true }
        if p.verdict != "matched" { return false }
        if key == "name" { return false }
        if key == "note" { return true }
        if PasteFields.identifiers.contains(key) {
            guard let hit = p.identifiers.first(where: { $0.kind == key }) else { return true }
            return hit.writes
        }
        guard let f = p.fields[key] else { return true }
        return f.writes
    }

    /// The small sentence under a field (intake.js:209-219).
    static func fieldNote(_ p: PasteProposal, _ key: String) -> String {
        if PasteFields.identifiers.contains(key) {
            return p.identifiers.first { $0.kind == key }?.why ?? ""
        }
        if key == "name", !p.name.existing.isEmpty {
            return "Kept as “\(p.name.existing)”. Intake never renames somebody it matched."
        }
        if let f = p.fields[key], !f.writes { return f.why }
        return ""
    }

    // MARK: Editing

    /// Typing a name takes the confirmation off: the tick referred to the string that was
    /// on screen when it was given (intake.js:419-429).
    func setEdit(_ ref: String, _ key: String, _ value: JSONValue) {
        var row = edits[ref] ?? [:]
        row[key] = value
        if key == "name" { row["name_confirmed"] = .bool(false) }
        edits[ref] = row
    }

    func setSkip(_ ref: String, _ on: Bool) { skips[ref] = on }

    func hasTypedName(_ ref: String) -> Bool { edits[ref]?["name"] != nil }

    // from static/app/intake.js:431-434
    func reset() {
        plan = nil
        receipt = nil
        edits = [:]
        skips = [:]
        pages = []
        saved = ""
        confirming = false
        err = ""
        notice = nil
    }

    /// "Paste another list" after a save: an empty box. (The web page's reset keeps the
    /// old text, so reading it again proposes the people just saved as matches.)
    func startOver() {
        reset()
        text = ""
    }

    // MARK: Reading the paste

    /// `parse` is the plain read and needs no key; `propose` is the model read. Both
    /// answer the identical shape (intake.js:436-457).
    func read(_ which: Busy) async {
        guard let client, which == .parse || which == .propose else { return }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, busy == .none else { return }
        busy = which
        err = ""
        notice = nil
        receipt = nil
        confirming = false
        defer { busy = .none }
        do {
            let route = which == .propose ? "/api/intake/propose" : "/api/intake/parse"
            let raw: JSONValue
            if which == .propose {
                // Only pages this engine fetched, and only their host and text.
                let pageBodies: [JSONValue] = pages.map { ["host": .string($0.host), "text": .string($0.text)] }
                raw = try await client.post(
                    route, body: ["text": .string(text), "pages": .array(pageBodies)] as [String: JSONValue],
                    as: JSONValue.self, timeout: Self.modelTimeout)
            } else {
                raw = try await client.post(route, body: ["text": .string(text)] as [String: JSONValue], as: JSONValue.self)
            }
            let typed = try PasteJSON.typed(raw, as: PastePlanAnswer.self, route: route)
            plan = PastePlan(
                proposals: typed.proposals, report: typed.report, allowedLookupUrls: typed.allowedLookupUrls,
                rawProposals: raw.pasteKey("proposals") ?? .array([]), rawReport: raw.pasteKey("report") ?? .object([:]))
            edits = [:]
            skips = [:]
            saved = ""
        } catch is CancellationError {
        } catch {
            err = SetupWords.sentence(error)
        }
    }

    /// Reads ONE address out of the paste. The paste goes with it: the engine works out
    /// for itself which addresses were in it (NC-148, intake.js:459-477).
    func lookup(_ url: String) async {
        guard let client, busy == .none else { return }
        busy = .lookup
        err = ""
        notice = nil
        defer { busy = .none }
        let wait = TimeInterval(status?.limit("NC_INTAKE_WEB_TIMEOUT_SEC") ?? 15) + Self.lookupSlack
        do {
            let d = try await client.post(
                "/api/intake/lookup", body: ["url": .string(url), "text": .string(text)] as [String: JSONValue],
                as: PasteLookupAnswer.self, timeout: wait)
            var page = d.page
            page.url = url
            pages = pages.filter { $0.url != url } + [page]
            notice = SetupNotice(text: "Read \(page.host)", tone: .ok)
        } catch is CancellationError {
        } catch {
            err = SetupWords.sentence(error)
        }
    }

    // MARK: The draft

    // from static/app/intake.js:479-492
    func park() async {
        guard let client, let plan, busy == .none else { return }
        busy = .park
        err = ""
        notice = nil
        defer { busy = .none }
        let batch: JSONValue = [
            "text": .string(text),
            "proposals": plan.rawProposals,
            "report": plan.rawReport,
            "allowed_lookup_urls": .array(plan.allowedLookupUrls.map { .string($0) }),
            "pages": PasteJSON.value(pages),
            "edits": .object(edits.mapValues { .object($0) }),
            "skips": .object(skips.mapValues { .bool($0) }),
        ]
        do {
            let d = try await client.post("/api/intake/batch", body: ["batch": batch] as [String: JSONValue], as: PasteParked.self)
            saved = "\(d.sentences.first ?? "The draft was saved.") \(Self.kb(d.bytes))."
        } catch is CancellationError {
        } catch {
            err = SetupWords.sentence(error)
        }
    }

    // from static/app/intake.js:494-502
    func discard() async {
        guard let client, busy == .none else { return }
        busy = .discard
        err = ""
        notice = nil
        defer { busy = .none }
        do {
            let d = try await client.delete("/api/intake/batch", as: PasteParked.self)
            draft = nil
            saved = ""
            notice = SetupNotice(text: d.sentences.first ?? "The draft was discarded.", tone: .ok)
        } catch is CancellationError {
        } catch {
            err = SetupWords.sentence(error)
        }
    }

    // from static/app/intake.js:504-510
    func resume() {
        guard let offer = draft else { return }
        let d = offer.draft
        text = d.text
        plan = PastePlan(
            proposals: d.proposals, report: d.report, allowedLookupUrls: d.allowedLookupUrls,
            rawProposals: offer.raw.pasteKey("proposals") ?? .array([]), rawReport: offer.raw.pasteKey("report") ?? .object([:]))
        edits = d.edits
        skips = d.skips
        pages = d.pages
        draft = nil
        err = ""
    }

    // MARK: Saving

    /// `confirm` is the JSON boolean true, never a string: the engine's gate is bool() on
    /// whatever arrives, so "false" would confirm the batch (intake.js:531-532).
    func commit() async {
        let live = rows.filter { !$0.skip }
        guard let client, busy == .none, !live.isEmpty else { return }
        busy = .commit
        err = ""
        notice = nil
        defer { busy = .none }
        do {
            let d = try await client.post(
                "/api/intake/commit",
                body: ["batch": .array(live.map(\.item.json)), "confirm": true] as [String: JSONValue],
                as: PasteReceipt.self, timeout: Self.commitTimeout)
            receipt = d
            confirming = false
            if d.wroteAnything {
                saved = ""
                // The engine discards the parked draft on any commit that wrote
                // (api_intake.py:652-655), so the offer is gone too.
                draft = nil
                await onWrote?()
            } else {
                // The receipt sits at the top of the list, which may be scrolled out of
                // sight from the Save button at the foot.
                notice = SetupNotice(text: "Nothing was written. The receipt at the top of the list says why.", tone: .warn)
            }
        } catch is CancellationError {
            confirming = false
        } catch {
            err = SetupWords.sentence(error)
            confirming = false
        }
    }

    // from static/app/intake.js:93
    static func kb(_ bytes: Int) -> String { String(format: "%.1f KB", Double(bytes) / 1024) }

    // MARK: Snapshot

    /// Made-up people for the review picture. Every name is invented.
    static let sampleText = """
        Selftest Ada Lovelace — Analytical Engines, London
        linkedin.com/in/selftest-grace-hopper
        Selftest Alan Turing, alan@example.com, +46 70 174 06 05
        Grace Hopper, grace@example.com

        ???
        Selftest Alan Turing, alan@example.com
        """

    /// For the review picture on the scratch engine only: fills the sample and reads it
    /// with the plain read, which the engine runs with no key and no network.
    func readSampleForSnapshot() async {
        guard let client, SelfTest.isScratch(client), status?.llm == false else { return }
        text = Self.sampleText
        await read(.parse)
    }
}
