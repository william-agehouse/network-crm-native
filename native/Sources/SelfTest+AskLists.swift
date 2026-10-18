import Foundation

// --selftest and --selftest-writes checks for Ask and Lists. Owned by that screen's files.
// Reads run against the real data (print counts only, never a value); writes run only
// against the empty scratch engine on 8768 (SelfTest.run refuses anything else).
//
// Port 8869 is the stand-in engine (native/tools/standin.py, launch config
// network-crm-native-standin): it answers only what Ask needs, takes ~20 s over POST
// /api/chat, and records whether POST
// /api/chat/stop arrived and with which run id. On that port --selftest runs ONE check of
// this file: Stop pressed in the middle of an answer, through the same AskModel.stop() the
// Stop button calls. (The shared read checks then decode its `{}` answers to empty
// defaults; only this file's line means anything there.)

extension SelfTest {
    static let askStandInPort = 8869

    static func askListsReads(_ t: inout Tally) async {
        let c = t.client
        if c.port == askStandInPort {
            await askStopCheck(&t)
            return
        }
        let app = AppModel(engine: EngineController(address: c.address, launch: nil))
        await app.engine.connect()

        // Lists: what Copy as Text and Copy as Markdown would put on the clipboard, for
        // every list. Line counts only.
        let lists = (try? await c.get("/api/lists", as: ListsResponse.self).lists) ?? []
        var withMembers: ListDetail?
        await t.checkMany("lists copy as text and Markdown", ids: lists.map(\.id), unit: "lines") { id in
            let d = try await c.get("/api/list/\(id)", as: ListDetail.self)
            if withMembers == nil, !d.members.isEmpty { withMembers = d }
            let text = ListsExport.text(d, scope: "all")
            let md = ListsExport.markdown(d, scope: "all")
            let groups = ListsExport.groups(d.members)
            let blocks = text.components(separatedBy: "\n\n")
            // Header block + one block per status group; Markdown: title, meta, table.
            guard text.hasSuffix("\n"), md.hasPrefix("## "), blocks.count == 1 + groups.count,
                md.components(separatedBy: "\n").filter({ $0.hasPrefix("| ") }).count == 2 + d.members.count
            else { throw AskListsCheck.failed("export shape") }
            return text.components(separatedBy: "\n").count
        }
        if let d = withMembers {
            await t.check("GET /api/list/{id}/export (CSV)") {
                try await c.photoData("/api/list/\(d.id)/export")
            } count: { data in
                // The engine's csv module ends rows with \r\n, one Character in Swift.
                let lines = data.map { String(decoding: $0, as: UTF8.self).split(whereSeparator: \.isNewline).count } ?? 0
                return "\(lines) lines for \(d.members.count) members"
            }
            // Member notes, the way the roster's Note column loads them.
            let ids = Array(d.members.prefix(20).map(\.id))
            await t.askCheck("list member notes (GET /api/contact/{id})") {
                let store = ListsStore()
                app.selectedListID = d.id
                await store.start(app: app)
                for id in ids { await store.loadNote(id) }
                guard store.notesLoaded.count == ids.count else { throw AskListsCheck.failed("notes not loaded") }
                return store.memberNotes.count
            } count: { "\(ids.count) members read, \($0) with a note" }
        } else {
            t.note("list member notes", "skipped: no list has members")
        }

        // Ask: every saved answer's people, as the rows draw them.
        let chats = (try? await c.get("/api/chats", as: ChatsResponse.self).chats) ?? []
        await t.check("ask people rows (selection bar, tags)") {
            var answers = 0, withBar = 0, tags = 0
            for id in chats.map(\.id) {
                let h = try await c.get("/api/chat/\(id)/history", as: ChatHistory.self)
                for m in h.messages where m.role == "assistant" && !m.contacts.isEmpty {
                    answers += 1
                    if m.contacts.count > 1 { withBar += 1 }
                    tags += m.contacts.reduce(0) { $0 + AskSelection.tags($1).count }
                    let all = Set(m.contacts.map(\.id))
                    guard AskSelection.ordered(m.contacts, all).count == all.count else { throw AskListsCheck.failed("order") }
                }
            }
            return (answers, withBar, tags)
        } count: { "\(chats.count) chats, \($0.0) answers with people, \($0.1) show the selection bar, \($0.2) tags" }

        // The open chat loads, and a staged draft goes into the composer without sending.
        await t.askCheck("Ask opens the newest chat; a staged draft is not sent") {
            let model = AskModel.shared(for: app)
            await model.appeared()
            guard model.transcriptState == .loaded, model.chatsState == .loaded else {
                throw AskListsCheck.failed("not loaded")
            }
            let before = model.turns.count
            app.askDraft = "Who could introduce me to "
            model.takeDraft()
            guard model.draft == "Who could introduce me to ", app.askDraft.isEmpty, model.pending == nil,
                model.turns.count == before
            else { throw AskListsCheck.failed("draft") }
            return model.turns.count
        } count: { "\($0) messages on screen, draft staged, nothing sent" }
    }

    // MARK: Writes (8768 only)

    static func askListsWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        func fail(_ s: String) -> EngineError { .engine(status: 0, message: s) }
        func makePerson(_ name: String) async throws -> Int {
            try await c.post("/api/contacts", body: ["name": .string("\(name) \(stamp)")] as [String: JSONValue], as: CreatedID.self).id
        }
        guard let status = try? await c.get("/api/status", as: EngineStatus.self) else {
            t.fail("ask+lists writes", "the scratch engine did not answer /api/status")
            return
        }
        let people = await t.check("ask+lists: three made-up people") {
            [try await makePerson("Selftest Ada"), try await makePerson("Selftest Bob"), try await makePerson("Selftest Cy")]
        } count: { "\($0.count) created" }
        guard let people else { return }
        let (a, b, cy) = (people[0], people[1], people[2])
        let app = AppModel(engine: EngineController(address: c.address, launch: nil))
        await app.engine.connect()
        var listIDs: [Int] = []
        var chatIDs: [Int] = []

        // 1. Adding people to a list the way Ask's "Add to List…" does.
        let made = await t.check("add to a new list (POST /api/lists with contact_ids)") {
            let o = try await ListsAddTo.create(named: "Selftest ask list \(stamp)", with: [a, b, a], client: c)
            listIDs.append(o.listID)
            guard o.created, o.added == 2, o.skipped == 0, o.message.hasPrefix("Added 2 people to “") else {
                throw fail("expected 2 added, got \(o.added) (\(o.message))")
            }
            return o
        } count: { "added \($0.added): \($0.message)" }
        if let made {
            let l = made.listID
            await t.check("add to an existing list (2 already in)") {
                let o = try await ListsAddTo.add([a, b, cy], toList: l, named: made.listName, client: c)
                guard o.added == 1, o.skipped == 2, o.message.hasSuffix(" · 2 already in the list") else {
                    throw fail("expected 1 added 2 skipped, got \(o.added)/\(o.skipped)")
                }
                return o
            } count: { $0.message }
            await t.check("add brings back a member removed earlier") {
                _ = try await c.post(
                    "/api/list/\(l)/member", body: ["contact_id": .int(b), "remove": true] as [String: JSONValue],
                    as: WriteResult.self)
                let o = try await ListsAddTo.add([b], toList: l, named: made.listName, client: c)
                let members = try await c.get("/api/list/\(l)", as: ListDetail.self).members.map(\.id)
                guard o.added == 1, members.contains(b) else { throw fail("removed member not restored") }
                return o
            } count: { "\($0.message) (before NC-336 the web page said “already holds everyone” here)" }
            await t.check("add someone already in the list") {
                let o = try await ListsAddTo.add([a], toList: l, named: made.listName, client: c)
                guard o.added == 0, o.message == "“\(made.listName)” already holds everyone you selected" else {
                    throw fail("got \(o.message)")
                }
                return o
            } count: { $0.message }
        }

        // 2. The selection rules under an answer: ids, shift adds a range, answer order out.
        await t.check("ask selection: tick, shift-click range, answer order") {
            let cards: [AskCard] = [a, b, cy].map { var x = AskCard(); x.id = $0; return x }
            var sel = AskSelection.toggle([], cards: cards, index: 0, anchor: nil, shift: false)
            guard sel == [a] else { throw fail("tick") }
            sel = AskSelection.toggle(sel, cards: cards, index: 2, anchor: 0, shift: true)
            guard sel == [a, b, cy] else { throw fail("shift range") }
            sel = AskSelection.toggle(sel, cards: cards, index: 1, anchor: 2, shift: false)
            guard sel == [a, cy] else { throw fail("untick") }
            sel = AskSelection.toggle(sel, cards: cards, index: 0, anchor: 1, shift: true)
            guard sel == [a, b, cy] else { throw fail("a shift range only adds") }
            guard AskSelection.ordered(cards, [cy, a]) == [a, cy] else { throw fail("order") }
            return sel.count
        } count: { "\($0) selected after the ranges" }

        // 3. An answer's people into a list, end to end (keyword engine only: no cost).
        if status.ai {
            t.fail("ask answer → list", "REFUSED: this engine has an AI key, so a question could cost money")
        } else {
            await t.check("ask answer's ticked people → new list") {
                let chat = try await c.post(
                    "/api/chats", body: ["title": .string("Selftest ask \(stamp)")] as [String: JSONValue],
                    as: ChatCreated.self)
                chatIDs.append(chat.conversation.id)
                // Two made-up people of its own live in London, so the keyword engine has
                // someone to name on a fresh scratch engine too, not only on one an earlier
                // run left London people in. Not Ada, Bob or Cy: later steps count them.
                for name in ["Selftest Londoner One", "Selftest Londoner Two"] {
                    let pid = try await makePerson(name)
                    _ = try await c.post(
                        "/api/contact/\(pid)/field",
                        body: ["field": .string("home_metro"), "value": .string("London")] as [String: JSONValue],
                        as: WriteResult.self)
                }
                let answer = try await c.ask(AskRequest(message: "Who is in London?", conversationID: chat.conversation.id))
                let cards = answer.contacts
                guard cards.count >= 2 else { throw fail("the keyword engine named \(cards.count) people") }
                let picked = Set(cards.suffix(2).map(\.id))
                let ids = AskSelection.ordered(cards, picked)
                let o = try await ListsAddTo.create(named: "Selftest from Ask \(stamp)", with: ids, client: c)
                listIDs.append(o.listID)
                let members = Set(try await c.get("/api/list/\(o.listID)", as: ListDetail.self).members.map(\.id))
                guard o.added == ids.count, members == Set(ids) else { throw fail("list holds \(members.count) of \(ids.count)") }
                return (cards.count, o)
            } count: { "\($0.0) people named, \($0.1.message)" }
        }

        // 4. A member note round trip, through the roster's own store.
        if let l = made?.listID {
            let store = ListsStore()
            app.selectedListID = l
            await store.start(app: app)
            await store.loadDetail(l)
            let text = "Selftest note \(stamp)\nSecond line."
            await t.check("member note: add (POST /api/contact/{id}/note)") {
                let before = app.peopleRevision
                if let problem = await store.addNote(text, to: a) { throw fail(problem) }
                guard store.memberNotes[a]?.body == text, store.memberNotes[a]?.line == "Selftest note \(stamp)",
                    app.peopleRevision == before + 1
                else { throw fail("note not shown on the roster") }
                return store.lastNoteID ?? 0
            } count: { _ in "saved, shown on the roster, People told to refresh" }
            await t.check("member note: read back (GET /api/contact/{id})") {
                let p = try await c.get("/api/contact/\(a)", as: PersonDetail.self)
                guard p.notes.first?.body == text, p.status == "described" || p.status.isEmpty else {
                    throw fail("the person's newest note differs")
                }
                return p.notes.count
            } count: { "\($0) note on the person's page" }
            if let n = store.lastNoteID {
                await t.check("member note: delete, roster empties") {
                    _ = try await c.delete("/api/note/\(n)", as: WriteResult.self)
                    await store.loadNote(a, force: true)
                    guard store.memberNotes[a] == nil, store.noteChecked(a) else { throw fail("still shown") }
                    return 0
                } count: { _ in "No note yet" }
            }
            await t.check("member note: empty text refused before sending") {
                guard await store.addNote("   ", to: a) != nil else { throw fail("blank note accepted") }
                return 0
            } count: { _ in "refused" }

            // 5. Copy as text / Markdown: the output's shape on made-up members.
            await t.check("copy as text: shape on made-up members") {
                for (field, value) in [("position", "Engineer"), ("company", "Analytical Engines"), ("home_metro", "London")] {
                    _ = try await c.post(
                        "/api/contact/\(a)/field", body: ["field": .string(field), "value": .string(value)] as [String: JSONValue],
                        as: WriteResult.self)
                }
                for (pid, s) in [(a, "attending"), (b, "invited"), (cy, "")] {
                    _ = try await c.post(
                        "/api/list/\(l)/member", body: ["contact_id": .int(pid), "status": .string(s)] as [String: JSONValue],
                        as: WriteResult.self)
                }
                _ = try await c.post(
                    "/api/list/\(l)/meta", body: ["event_date": "2026-12-01", "venue": "Selftest Hall"] as [String: JSONValue],
                    as: WriteResult.self)
                let d = try await c.get("/api/list/\(l)", as: ListDetail.self)
                let text = ListsExport.text(d, scope: "all")
                let blocks = text.components(separatedBy: "\n\n")
                let head = blocks[0].components(separatedBy: "\n")
                guard blocks.count == 4, head.count == 3, head[0] == d.name, head[1] == "\(Display.fmtDay("2026-12-01")) · Selftest Hall",
                    head[2] == "3 people · grouped by invite status",
                    blocks[1].hasPrefix("To invite (1)\n"), blocks[2].hasPrefix("Invited (1)\n"),
                    blocks[3] == "Attending (1)\nSelftest Ada \(stamp) — Engineer, Analytical Engines — London\n"
                else { throw fail("text was:\n\(text)") }
                let one = ListsExport.text(d, scope: "invited")
                let oneBlocks = one.components(separatedBy: "\n\n")
                guard oneBlocks.count == 2, oneBlocks[0].hasSuffix("1 of 3 people · invited only"),
                    oneBlocks[1] == "Selftest Bob \(stamp)\n"
                else { throw fail("scoped text was:\n\(one)") }
                let md = ListsExport.markdown(d, scope: "all")
                guard md.contains("| Name | Role | Company | City | Status |"),
                    md.contains("| Selftest Ada \(stamp) | Engineer | Analytical Engines | London | Attending |"),
                    !ListsExport.markdown(d, scope: "invited").contains("Status")
                else { throw fail("markdown was:\n\(md)") }
                return blocks.count
            } count: { "\($0) blocks: header, To invite, Invited, Attending; Markdown table with a Status column" }

            // 6. Dragging a member onto another list adds them there.
            if let other = listIDs.last, other != l {
                await t.check("drag a member onto another list") {
                    let target = try await c.get("/api/lists", as: ListsResponse.self).lists.first { $0.id == other }
                    guard let target else { throw fail("no target list") }
                    await store.drop(personID: a, onto: target)
                    let members = try await c.get("/api/list/\(other)", as: ListDetail.self).members.map(\.id)
                    guard members.contains(a), store.notice?.hasPrefix("Added Selftest") == true else {
                        throw fail("not added (\(store.notice ?? "no notice"))")
                    }
                    return members.count
                } count: { "\($0) members on the target list" }
            }
        }

        // 7. ⌘K "Start a new chat" (app.requestNewChat), through the Ask model.
        if !status.ai, let withMessages = chatIDs.first {
            await t.check("requestNewChat starts one chat, then is cleared") {
                let model = AskModel.shared(for: app)
                app.selectedChatID = withMessages
                await model.appeared()
                await model.loadTranscript()
                guard !model.turns.isEmpty else { throw fail("the chat to start from is empty") }
                app.requestNewChat = true
                await model.handleNewChatRequest()
                let made = model.convID
                guard !app.requestNewChat, made > 0, made != withMessages, model.turns.isEmpty,
                    app.selectedChatID == made
                else { throw fail("no new chat (conv \(made))") }
                chatIDs.append(made)
                // Again, from the empty chat it made: nothing new, and it says why.
                let count = model.chats.count
                app.requestNewChat = true
                await model.handleNewChatRequest()
                guard !app.requestNewChat, model.convID == made, model.chats.count == count,
                    model.toast == "This chat is empty already"
                else { throw fail("a second empty chat was made") }
                return made
            } count: { _ in "one new chat; a second request said “This chat is empty already”" }
        }

        // Tidy the scratch engine.
        for id in chatIDs {
            _ = try? await c.delete("/api/chat/\(id)", as: ChatDeleted.self)
        }
        for id in listIDs {
            _ = try? await c.delete("/api/list/\(id)", as: WriteResult.self)
        }
        for id in people {
            _ = try? await c.post("/api/contact/\(id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
        }
        t.note("ask+lists writes", "tidied: \(chatIDs.count) chats, \(listIDs.count) lists, \(people.count) people deleted")
    }

    // MARK: Stop, proven against the stand-in (8869)

    private static func askStopCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("Stop mid-answer (stand-in engine)") {
            let app = AppModel(engine: EngineController(address: c.address, launch: nil))
            // The stand-in is started by the same shell command: give it a moment.
            let deadline = Date().addingTimeInterval(8)
            while !app.engine.isReady && Date() < deadline {
                await app.engine.connect()
                if !app.engine.isReady { try? await Task.sleep(for: .milliseconds(200)) }
            }
            guard app.engine.isReady else { throw AskListsCheck.failed("the stand-in on 8869 is not running") }
            let model = AskModel.shared(for: app)
            await model.appeared()
            let started = Date()
            model.send("Stand-in: a slow question", fromComposer: true)
            guard let runID = model.pending?.runID else { throw AskListsCheck.failed("no pending run") }
            try? await Task.sleep(for: .seconds(1.5))
            // Exactly what the Stop button does.
            model.stop()
            let showedStopping = model.pending?.stopping == true
            let wait = Date().addingTimeInterval(40)
            while model.pending != nil && Date() < wait { try? await Task.sleep(for: .milliseconds(100)) }
            let seconds = Date().timeIntervalSince(started)
            let log = try await c.get("/stub/log", as: AskListsStubLog.self)
            guard model.pending == nil else { throw AskListsCheck.failed("still waiting after 40 s") }
            guard log.chatRuns.contains(runID) else { throw AskListsCheck.failed("POST /api/chat did not carry the run id") }
            guard log.stops.contains(runID) else { throw AskListsCheck.failed("POST /api/chat/stop never arrived for this run") }
            guard showedStopping else { throw AskListsCheck.failed("the button did not turn into Stopping") }
            guard let last = model.turns.last, last.role == .assistant, last.stopped, last.failure == nil else {
                throw AskListsCheck.failed("the answer did not come back as stopped")
            }
            // The stand-in takes 20 s unless stopped.
            guard seconds < 15 else { throw AskListsCheck.failed("the answer took \(Int(seconds)) s: the stop did nothing") }
            return (seconds, log.stops.count)
        } count: { r in
            "stop arrived for the same run id; answer came back stopped after \(String(format: "%.1f", r.0)) s instead of 20 s; \(r.1) stop request(s)"
        }
    }
}

extension Tally {
    /// `check`, plus one line saying why when this file's own check failed. Those reasons
    /// are this file's words, never an engine value, so they are safe on real data.
    @discardableResult
    mutating func askCheck<T>(_ route: String, _ op: () async throws -> T, count: (T) -> String) async -> T? {
        var why: String?
        let r = await check(route, {
            do { return try await op() } catch let e as AskListsCheck {
                why = e.description
                throw e
            }
        }, count: count)
        if let why { print("     because: \(why)") }
        return r
    }
}

/// What the stand-in engine saw (standin.py, GET /stub/log).
struct AskListsStubLog: Decodable {
    var chatRuns: [String]
    var stops: [String]
    enum CodingKeys: String, CodingKey {
        case chatRuns = "chat_runs", stops
    }
}

/// A failed check whose detail must not carry engine values (reads run on real data).
enum AskListsCheck: Error, CustomStringConvertible {
    case failed(String)
    var description: String {
        switch self { case .failed(let s): s }
    }
}
