import AppKit
import Foundation

// --selftest: GETs every read route the app uses from the engine on NC_NATIVE_PORT and
// decodes each answer into the Models, strictly (a wrong-typed key fails). One line per
// route: `PASS <route> <count>` or `FAIL <route> <coding path>`. It prints counts and
// coding paths only, NEVER a value: this runs against real data.
//
// --selftest-writes: exercises every write route against the EMPTY scratch engine. It
// refuses unless NC_NATIVE_PORT is 8768, and refuses to send a question to Ask unless that
// engine reports no AI key (so it can never spend money).
//
// Routes deliberately left out of --selftest on real data:
// - POST /api/db-recovery-notice/dismiss and POST /api/onboarding change what the engine
//   remembers (contract §7). The first runs only against the stand-in, the second only
//   against the empty scratch engine.
// - GET /api/onboarding no longer writes anything (NC-342; it used to store "setup done"
//   for an install with many people, on the first read). It stays out anyway, to be
//   cautious: the app asks GET /api/settings/ledger for the same answer
//   (OnboardingGate), and that read works with an engine from before NC-342 too.

@MainActor
enum SelfTest {
    static let scratchPort = 8768

    /// True when the engine on a private connection (NC_NATIVE_SOCKET) was checked to keep
    /// its data in a temporary folder, the promise port 8768 makes by convention.
    static var scratchSocket = false

    /// Whether `client` is the scratch engine, the only one writes and made-up people go to.
    static func isScratch(_ client: EngineClient) -> Bool {
        client.port == scratchPort || (client.port == 0 && scratchSocket)
    }

    static func run(writes: Bool) async -> Int32 {
        let env = ProcessInfo.processInfo.environment
        let client: EngineClient
        var port = 0
        if let path = env["NC_NATIVE_SOCKET"], !path.isEmpty {
            // NC-330: the same checks over the app's private connection.
            client = EngineClient(address: .socket(path))
            if writes {
                // Scratch data lives in a temporary folder; real data never does (it is in
                // ~/Library/Application Support/NetworkCRM, or a checkout's data/).
                let scratchRoots = ["/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/"]
                guard let s = try? await client.get("/api/status", as: EngineStatus.self),
                      scratchRoots.contains(where: { s.dbPath.hasPrefix($0) })
                else {
                    print("REFUSED: --selftest-writes over NC_NATIVE_SOCKET only runs against an engine whose data is in a temporary folder.")
                    return 2
                }
                scratchSocket = true
            }
        } else {
            guard let raw = env["NC_NATIVE_PORT"], let p = Int(raw) else {
                print("REFUSED: set NC_NATIVE_PORT (or NC_NATIVE_SOCKET) to the engine to test.")
                return 2
            }
            if writes && p != scratchPort {
                print("REFUSED: --selftest-writes only runs against the empty scratch engine on \(scratchPort), not \(p).")
                return 2
            }
            port = p
            client = EngineClient(port: p)
        }
        DecodingPolicy.strict = true
        var t = Tally(client: client, showMessages: writes)
        if writes {
            await writeRoutes(&t)
            // Each screen's own routes, from its own file (SelfTest+<Screen>.swift).
            await dataSettingsWrites(&t)
            await setupWrites(&t)
            await commandWrites(&t)
            await peopleWrites(&t)
            await engineWrites(&t)
            await askListsWrites(&t)
            await refusalWrites(&t)
        } else if port == askStandInPort {
            // The stand-in answers only what its own checks need; the shared read checks
            // would decode its {} answers and fail on what it never claimed to serve.
            await engineReads(&t)
            await askListsReads(&t)
        } else {
            await readRoutes(&t)
            await dataSettingsReads(&t)
            await setupReads(&t)
            await commandReads(&t)
            await peopleReads(&t)
            await engineReads(&t)
            await askListsReads(&t)
            await refusalReads(&t)
        }
        print("\(t.passed) passed, \(t.failed) failed")
        return t.failed == 0 ? 0 : 1
    }

    // MARK: Reads

    private static func readRoutes(_ t: inout Tally) async {
        let c = t.client
        await t.check("/api/status") { try await c.get("/api/status", as: EngineStatus.self) } count: { s in
            "running=\(s.running) ai=\(s.ai) \(s.counts.total) rows"
        }
        let everyone = await t.check("/api/contacts sort=closeness limit=20000") {
            try await c.get("/api/contacts", query: ["sort": "closeness", "limit": "20000"], as: ContactsResponse.self)
        } count: { "\($0.contacts.count) of \($0.total) people" }

        let variants: [(String, [String: String])] = [
            ("q=an", ["q": "an"]),
            ("sort=quiet", ["sort": "quiet"]),
            ("sort=recent", ["sort": "recent"]),
            ("filter=todo", ["filter": "todo"]),
            ("filter=described", ["filter": "described"]),
            ("filter=excluded", ["filter": "excluded"]),
            ("filter=deleted", ["filter": "deleted"]),
        ]
        var hiddenIDs: [Int] = []
        for (label, extra) in variants {
            var q = ["sort": "closeness", "limit": "20000"]
            q.merge(extra) { _, new in new }
            let r = await t.check("/api/contacts \(label)") {
                try await c.get("/api/contacts", query: q, as: ContactsResponse.self)
            } count: { "\($0.contacts.count) people" }
            if extra["filter"] == "excluded" || extra["filter"] == "deleted" {
                hiddenIDs += r?.contacts.map(\.id) ?? []
            }
        }
        await t.check("/api/search/semantic q=engineer") {
            try await c.get("/api/search/semantic", query: ["q": "engineer"], as: SemanticSearchResponse.self, timeout: 90)
        } count: { "available=\($0.available) \($0.contacts.count) people" }
        await t.check("/api/facets") { try await c.get("/api/facets", as: FacetsResponse.self) } count: {
            "\($0.roles.count) roles, \($0.topics.count) topics"
        }
        await t.check("/api/locations") { try await c.get("/api/locations", as: LocationsResponse.self) } count: {
            "\($0.locations.count) locations"
        }
        for days in ["14", "365"] {
            await t.check("/api/upcoming days=\(days)") {
                try await c.get("/api/upcoming", query: ["days": days], as: UpcomingResponse.self)
            } count: { "\($0.shown.count) shown, \($0.more) more" }
        }
        await t.check("/api/exclude-list") { try await c.get("/api/exclude-list", as: ExcludeListResponse.self) } count: {
            "\($0.entries.count) entries"
        }

        // A person's page: the closest 60 (they have the richest pages) plus every hidden
        // or deleted one (whose pages differ: deleted:true, next_action null).
        let people = everyone?.contacts ?? []
        let detailIDs = Array(people.prefix(60).map(\.id)) + hiddenIDs
        await t.checkMany("/api/contact/{id}", ids: detailIDs, unit: "people") { id in
            _ = try await c.get("/api/contact/\(id)", as: PersonDetail.self)
            return 1
        }
        let timelineIDs = Array(people.prefix(40).map(\.id))
        await t.checkMany("/api/contact/{id}/episodes limit=20", ids: timelineIDs, unit: "episodes") { id in
            try await c.get("/api/contact/\(id)/episodes", query: ["limit": "20"], as: EpisodesResponse.self).episodes.count
        }
        await t.checkMany("/api/contact/{id}/history limit=50", ids: Array(timelineIDs.prefix(20)), unit: "messages") { id in
            try await c.get("/api/contact/\(id)/history", query: ["limit": "50"], as: HistoryResponse.self).messages.count
        }
        await t.checkMany("/api/contact/{id}/history threads=1", ids: Array(timelineIDs.prefix(20)), unit: "messages") { id in
            try await c.get("/api/contact/\(id)/history", query: ["limit": "200", "threads": "1"], as: HistoryResponse.self)
                .messages.count
        }
        if let withPhoto = people.first(where: { !$0.photo.isEmpty }) {
            await t.check("/api/photo/{id}") { try await c.photoData(withPhoto.photo) } count: {
                $0.map { "\($0.count) bytes" } ?? "no photo"
            }
        } else {
            t.note("/api/photo/{id}", "skipped: nobody has a photo")
        }

        let chats = await t.check("/api/chats") { try await c.get("/api/chats", as: ChatsResponse.self) } count: {
            "\($0.chats.count) chats"
        }
        await t.checkMany("/api/chat/{id}/history", ids: chats?.chats.map(\.id) ?? [], unit: "messages") { id in
            try await c.get("/api/chat/\(id)/history", as: ChatHistory.self).messages.count
        }
        await t.check("/api/chat/history (newest)") { try await c.get("/api/chat/history", as: ChatHistory.self) } count: {
            "\($0.messages.count) messages"
        }

        let lists = await t.check("/api/lists") { try await c.get("/api/lists", as: ListsResponse.self) } count: {
            "\($0.lists.count) lists"
        }
        await t.checkMany("/api/list/{id}", ids: lists?.lists.map(\.id) ?? [], unit: "members") { id in
            try await c.get("/api/list/\(id)", as: ListDetail.self).members.count
        }
        // Decoded only, never printed: the rows include masked secret keys.
        await t.check("/api/settings") { try await c.get("/api/settings", as: SettingsResponse.self) } count: {
            "\($0.settings.count) rows"
        }
    }

    // MARK: Writes (scratch engine only)

    private static func writeRoutes(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        let status = await t.check("/api/status") { try await c.get("/api/status", as: EngineStatus.self) } count: {
            "ai=\($0.ai)"
        }
        guard let status else { return }

        let ada = await t.check("POST /api/contacts") {
            try await c.post("/api/contacts", body: ["name": .string("Selftest Ada \(stamp)")] as [String: JSONValue], as: CreatedID.self)
        } count: { _ in "created" }
        let bob = await t.check("POST /api/contacts (second)") {
            try await c.post("/api/contacts", body: ["name": .string("Selftest Bob \(stamp)")] as [String: JSONValue], as: CreatedID.self)
        } count: { _ in "created" }
        guard let ada, let bob else { return }
        let a = ada.id, b = bob.id

        let note = await t.check("POST /api/contact/{id}/note") {
            try await c.post("/api/contact/\(a)/note", body: ["body": "Met at a selftest."] as [String: JSONValue], as: NoteCreated.self)
        } count: { "note \($0.note.kind)" }
        if let n = note?.note.id {
            await t.check("PUT /api/note/{id}") {
                try await c.put("/api/note/\(n)", body: ["body": "Met at a selftest, edited."] as [String: JSONValue], as: WriteResult.self)
            } count: { "ok=\($0.ok)" }
        }
        await t.check("POST /api/contact/{id}/field") {
            try await c.post("/api/contact/\(a)/field", body: ["field": "company", "value": "Analytical Engines"] as [String: JSONValue], as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("POST /api/contact/{id}/field (empty, no clear)") {
            try await c.post("/api/contact/\(a)/field", body: ["field": "company", "value": ""] as [String: JSONValue], as: WriteResult.self)
        } count: { "unchanged=\($0.unchanged ?? false)" }
        await t.check("POST /api/contact/{id}/field (clear:true)") {
            try await c.post("/api/contact/\(a)/field", body: ["field": "company", "value": "", "clear": true] as [String: JSONValue], as: WriteResult.self)
        } count: { "unchanged=\($0.unchanged ?? false)" }
        await t.check("POST /api/contact/{id}/howwemet") {
            try await c.post("/api/contact/\(a)/howwemet", body: ["text": "A selftest."] as [String: JSONValue], as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("POST /api/contact/{id}/category") {
            try await c.post("/api/contact/\(a)/category", body: ["category": "professional"] as [String: JSONValue], as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("POST /api/contact/{id}/identifier") {
            try await c.post("/api/contact/\(a)/identifier", body: ["kind": "email", "value": "ada-\(stamp)@example.com"] as [String: JSONValue], as: WriteResult.self)
        } count: { "\($0.identifiers?.count ?? 0) identifiers" }
        let dates = await t.check("POST /api/contact/{id}/dates") {
            try await c.post("/api/contact/\(a)/dates", body: ["kind": "birthday", "on_date": "1990-12-10"] as [String: JSONValue], as: WriteResult.self)
        } count: { "\($0.dates?.count ?? 0) dates" }
        if let d = dates?.dates?.first?.id {
            await t.check("POST /api/contact/{id}/dates (remove)") {
                try await c.post("/api/contact/\(a)/dates", body: ["remove": true, "date_id": .int(d)] as [String: JSONValue], as: WriteResult.self)
            } count: { "\($0.dates?.count ?? 0) dates" }
        }
        await t.check("POST /api/contact/{id}/exclude true") {
            try await c.post("/api/contact/\(a)/exclude", body: ["excluded": true] as [String: JSONValue], as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("POST /api/contact/{id}/exclude false") {
            try await c.post("/api/contact/\(a)/exclude", body: ["excluded": false] as [String: JSONValue], as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        let photo = await t.check("POST /api/contact/{id}/photo") {
            try await c.post("/api/contact/\(a)/photo", body: ["data": .string(testPNG().base64EncodedString())] as [String: JSONValue], as: WriteResult.self)
        } count: { $0.photo?.isEmpty == false ? "photo set" : "no photo path" }
        if let path = photo?.photo {
            await t.check("GET /api/photo/{id}") { try await c.photoData(path) } count: { "\($0?.count ?? 0) bytes" }
        }
        await t.check("DELETE /api/contact/{id}/photo") {
            try await c.delete("/api/contact/\(a)/photo", as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("POST /api/contact/{id}/reconnectskip") {
            try await c.post("/api/contact/\(a)/reconnectskip", as: WriteResult.self)
        } count: { "ok=\($0.ok)" }
        await t.check("GET /api/contact/{id} (after edits)") {
            try await c.get("/api/contact/\(a)", as: PersonDetail.self)
        } count: { "\($0.notes.count) notes, \($0.identifiers.count) identifiers" }
        // A save on a person's page must tell the People table to refetch.
        await t.check("person page edits refresh People") {
            let model = AppModel(engine: EngineController(address: c.address, launch: nil))
            let store = PersonStore(id: a, client: c)
            store.onWrite = { model.peopleChanged() }
            try await store.saveField("company", "Selftest Works", old: "")
            await store.setExcluded(true)
            await store.setExcluded(false)
            guard model.peopleRevision == 3 else {
                throw EngineError.engine(status: 0, message: "revision \(model.peopleRevision), expected 3")
            }
            return model.peopleRevision
        } count: { "\($0) refreshes for 3 saves" }
        // Emptying a filled field must really empty it, through the page's own store (NC-333:
        // the engine keeps the old text unless the request says clear:true).
        await t.check("person page: emptying a filled field clears it") {
            let store = PersonStore(id: a, client: c)
            try await store.saveField("company", "", old: "Selftest Works")
            let after = try await c.get("/api/contact/\(a)", as: PersonDetail.self)
            guard after.company.isEmpty else { throw EngineError.engine(status: 0, message: "the company is still filled") }
            return 1
        } count: { _ in "the field is empty after the save" }
        if let n = note?.note.id {
            await t.check("DELETE /api/note/{id}") { try await c.delete("/api/note/\(n)", as: WriteResult.self) } count: {
                "ok=\($0.ok)"
            }
        }

        // Lists
        let list = await t.check("POST /api/lists") {
            try await c.post("/api/lists", body: ["name": .string("Selftest list \(stamp)"), "contact_ids": [.int(a)]] as [String: JSONValue], as: CreatedID.self)
        } count: { "added \($0.added ?? 0)" }
        if let list {
            let l = list.id
            await t.check("POST /api/list/{id}/add") {
                try await c.post("/api/list/\(l)/add", body: ["contact_ids": [.int(b)]] as [String: JSONValue], as: WriteResult.self)
            } count: { "added \($0.added ?? 0)" }
            await t.check("POST /api/list/{id}/member") {
                try await c.post("/api/list/\(l)/member", body: ["contact_id": .int(b), "status": "invited"] as [String: JSONValue], as: WriteResult.self)
            } count: { "ok=\($0.ok)" }
            await t.check("POST /api/list/{id}/meta") {
                try await c.post("/api/list/\(l)/meta", body: ["event_date": "2026-12-01", "venue": "Selftest Hall"] as [String: JSONValue], as: WriteResult.self)
            } count: { "ok=\($0.ok)" }
            await t.check("POST /api/list/{id}/rename") {
                try await c.post("/api/list/\(l)/rename", body: ["name": .string("Selftest list renamed \(stamp)")] as [String: JSONValue], as: WriteResult.self)
            } count: { "ok=\($0.ok)" }
            await t.check("GET /api/list/{id}") { try await c.get("/api/list/\(l)", as: ListDetail.self) } count: {
                "\($0.members.count) members, \($0.counts.invited) invited"
            }
            await t.check("DELETE /api/list/{id}") { try await c.delete("/api/list/\(l)", as: WriteResult.self) } count: {
                "ok=\($0.ok)"
            }
        }

        // Ask — only where it cannot cost anything.
        if status.ai {
            t.fail("POST /api/chat", "REFUSED: this engine has an AI key, so a question could cost money")
            return
        }
        let chat = await t.check("POST /api/chats") {
            try await c.post("/api/chats", body: ["shape": ["people": "10", "detail": "brief", "search": "quick"]] as [String: JSONValue], as: ChatCreated.self)
        } count: { "created=\($0.created)" }
        if let chat {
            let id = chat.conversation.id
            await t.check("POST /api/chat") {
                try await c.ask(AskRequest(message: "Who works in engineering?", conversationID: id))
            } count: { "engine=\($0.engine) \($0.contacts.count) cards" }
            // Cancelling the waiting task must still hand back the engine's answer.
            await t.check("POST /api/chat (cancelled → stop)") {
                let task = Task { try await c.ask(AskRequest(message: "Who lives in Lisbon?", conversationID: id)) }
                task.cancel()
                return try await task.value
            } count: { "answer returned, stopped=\($0.stopped ?? false)" }
            await t.check("POST /api/chat/stop") { try await c.stopAsk(runID: UUID().uuidString) } count: { "status=\($0.status)" }
            await t.check("GET /api/chat/{id}/history") { try await c.get("/api/chat/\(id)/history", as: ChatHistory.self) } count: {
                "\($0.messages.count) messages"
            }
            await t.check("POST /api/chat/{id}/rename") {
                try await c.post("/api/chat/\(id)/rename", body: ["title": "Selftest chat"] as [String: JSONValue], as: ChatRenamed.self)
            } count: { _ in "renamed" }
            await t.check("DELETE /api/chat/{id}") { try await c.delete("/api/chat/\(id)", as: ChatDeleted.self) } count: {
                "\($0.turns) turns kept on disk"
            }
        }

        // Tidy the scratch engine: delete the two made-up people for good. Deleting adds a
        // name to the skip list, so take it off again, or every run leaves two more hidden
        // names behind.
        for (id, name) in [(a, "Selftest Ada \(stamp)"), (b, "Selftest Bob \(stamp)")] {
            await t.check("POST /api/contact/{id}/delete") {
                try await c.post("/api/contact/\(id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
            } count: { "deleted=\($0.deleted ?? false)" }
            _ = try? await c.post("/api/contact/\(id)/purge", as: WriteResult.self)
            _ = try? await c.post("/api/exclude-list/remove", body: ["name": JSONValue.string(name)], as: WriteResult.self)
        }
    }

    /// A small but real PNG (the engine rejects images under 120 bytes).
    private static func testPNG() -> Data {
        let side = 64
        let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8, samplesPerPixel: 4,
            hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        // Raw bytes, not setColor: an sRGB colour into a device-RGB bitmap logs a warning per pixel.
        if let px = rep.bitmapData {
            for y in 0..<side {
                for x in 0..<side {
                    let o = y * rep.bytesPerRow + x * 4
                    px[o] = UInt8(x * 4); px[o + 1] = UInt8(y * 4); px[o + 2] = 128; px[o + 3] = 255
                }
            }
        }
        return rep.representation(using: .png, properties: [:]) ?? Data()
    }
}

/// Counts and prints results. Never prints a value from the engine.
@MainActor
struct Tally {
    let client: EngineClient
    /// Writes run on made-up data, so the engine's own error sentence may be shown there.
    let showMessages: Bool
    var passed = 0
    var failed = 0

    @discardableResult
    mutating func check<T>(
        _ route: String, _ op: () async throws -> T, count: (T) -> String
    ) async -> T? {
        do {
            let value = try await op()
            passed += 1
            print("PASS \(route) \(count(value))")
            return value
        } catch {
            fail(route, describe(error))
            return nil
        }
    }

    /// One line for a route called once per id: the total of what each call counted.
    mutating func checkMany(
        _ route: String, ids: [Int], unit: String, _ op: (Int) async throws -> Int
    ) async {
        guard !ids.isEmpty else {
            note(route, "skipped: nothing to call it for")
            return
        }
        var total = 0
        for id in ids {
            do {
                total += try await op(id)
            } catch {
                fail(route, "\(describe(error)) (1 of \(ids.count) calls)")
                return
            }
        }
        passed += 1
        print("PASS \(route) \(ids.count) calls, \(total) \(unit)")
    }

    mutating func fail(_ route: String, _ why: String) {
        failed += 1
        print("FAIL \(route) \(why)")
    }

    func note(_ route: String, _ why: String) {
        print("SKIP \(route) \(why)")
    }

    private func describe(_ error: Error) -> String {
        switch error {
        case EngineError.decoding(_, let path, let problem):
            return "\(path.isEmpty ? "(top level)" : path): \(problem)"
        case EngineError.engine(let status, let message):
            return showMessages ? "HTTP \(status): \(message)" : "HTTP \(status)"
        case let e as EngineError:
            return e.errorDescription ?? "engine error"
        default:
            return String(describing: type(of: error))
        }
    }
}
