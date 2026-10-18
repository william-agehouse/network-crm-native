import Foundation

// --selftest and --selftest-writes checks for first-run setup and the command bar. Owned by that screen's files.
// Reads run against the real data (print counts only, never a value); writes run only
// against the empty scratch engine on 8768 (SelfTest.run refuses anything else).
//
// Left out on purpose, because pressing them acts on the Mac: POST /api/import/run (reads
// messages), /api/import/pick-file (a picker on screen), /api/import/open (Finder),
// GET /api/import/detect (probes protected folders), POST /api/settings with a key (it
// would switch Ask on for every other check on the scratch engine), and the commands that
// reveal Finder, copy to the clipboard, change the appearance or send a question.
// GET /api/import/sources is read on the scratch engine only: its answer comes from files
// in ~/Library.

extension SelfTest {
    static func commandReads(_ t: inout Tally) async {
        let c = t.client

        // ⌘K's searches and vocabulary.
        await t.check("⌘K /api/contacts q=an sort=closeness limit=6") {
            try await c.get("/api/contacts", query: ["q": "an", "sort": "closeness", "limit": "6"], as: ContactsResponse.self)
        } count: { "\($0.contacts.count) people" }
        await t.check("⌘K /api/search/conversations mode=keyword") {
            try await c.get("/api/search/conversations", query: ["q": "meeting", "mode": "keyword"],
                            as: CmdConversationsResponse.self)
        } count: { "available=\($0.available ?? true) \($0.conversations.count) conversations" }
        await t.check("⌘K /api/search/semantic q=climate") {
            try await c.get("/api/search/semantic", query: ["q": "climate"], as: SemanticSearchResponse.self, timeout: 90)
        } count: { "available=\($0.available) \($0.contacts.count) people" }
        await t.check("⌘K /api/search/conversations mode=semantic") {
            try await c.get("/api/search/conversations", query: ["q": "climate", "mode": "semantic"],
                            as: CmdConversationsResponse.self, timeout: 90)
        } count: { "available=\($0.available ?? true) \($0.conversations.count) conversations" }
        let facets = await t.check("⌘K facet commands (/api/locations + /api/facets)") {
            async let l = c.get("/api/locations", as: LocationsResponse.self)
            async let f = c.get("/api/facets", as: FacetsResponse.self)
            let (locations, fs) = try await (l, f)
            return CommandCatalog.facets(locations: locations.locations, roles: fs.roles, topics: fs.topics)
        } count: { "\($0.count) commands" }
        await t.check("⌘K recent-chat commands (/api/chats)") {
            CommandCatalog.chats(try await c.get("/api/chats", as: ChatsResponse.self).chats)
        } count: { "\($0.count) commands (cap \(CommandCatalog.recentChatCap))" }
        if let first = try? await c.get("/api/contacts", query: ["sort": "closeness", "limit": "1"], as: ContactsResponse.self)
            .contacts.first?.id
        {
            await t.check("⌘K person commands (/api/contact/{id})") {
                CommandCatalog.person(try await c.get("/api/contact/\(first)", as: PersonDetail.self))
            } count: { "\($0.count) commands, \($0.filter(\.typedOnly).count) typed only" }
        }
        if let facets {
            await t.check("⌘K a facet command needs its keyword") {
                let all = CommandCatalog.base(isDark: false) + facets
                let bare = CommandCatalog.rank(all, "", min: 1)
                try expect(bare.allSatisfy { !$0.typedOnly }, "a typed-only command showed in the bare list")
                return bare.count
            } count: { "\($0) in the bare list" }
        }

        // First run.
        await t.check("setup /api/describe-queue") {
            try await c.get("/api/describe-queue", as: OnbDescribeQueue.self)
        } count: { q in "\(q.queue.count) people, \(q.queue.map(\.lastMessages.count).reduce(0, +)) last messages" }
        await t.check("setup gate /api/settings/ledger (read-only twin of /api/onboarding)") {
            try await c.get("/api/settings/ledger", as: OnbSettingsLedger.self, timeout: 20)
        } count: { "done=\($0.install.setupAgain.done)" }

        await commandLogic(&t)
    }

    /// The ranking and the command → app-state mapping, with no engine involved.
    private static func commandLogic(_ t: inout Tally) async {
        let base = CommandCatalog.base(isDark: false)
        await t.check("⌘K empty box lists the everyday commands in order") {
            let ids = CommandCatalog.rank(base, "", min: 1).map(\.id)
            let want = ["add", "paste", "quiet", "todo", "ask", "newchat", "data", "merge", "dups", "export", "lists",
                        "newlist", "theme", "settings"]
            try expect(ids == want, "order was \(ids)")
            return ids.count
        } count: { "\($0) commands" }
        await t.check("⌘K ranking (main.js:295-325)") {
            func top(_ q: String) -> String? { CommandCatalog.rank(base, q, min: 2).first?.id }
            try expect(top("dupe") == "dups", "dupe → \(top("dupe") ?? "-")")
            try expect(top("tidy") == "dups", "tidy → \(top("tidy") ?? "-")")
            try expect(top("recent") == "recent", "recent → \(top("recent") ?? "-")")
            try expect(top("new list") == "newlist", "new list → \(top("new list") ?? "-")")
            try expect(top("introduce") == "intent0", "introduce → \(top("introduce") ?? "-")")
            try expect(top("xyzzy") == nil, "xyzzy matched")
            // A word only in a label scores 1: shown in ">" mode, not beside a search.
            try expect(CommandCatalog.rank(base, "someone", min: 1).first?.id == "add", "someone (label) missing")
            let rows = CommandCatalog.rows(raw: "> list", people: [], convos: [], commands: base)
            try expect(rows.first?.id == "cmd-lists", "> list → \(rows.first?.id ?? "-")")
            let search = CommandCatalog.rows(raw: "ada", people: [], convos: [], commands: base)
            try expect(search.suffix(2).map(\.id) == ["meaning", "ask"], "a search must end in meaning, ask")
            try expect(CommandCatalog.rows(raw: "> xyzzy", people: [], convos: [], commands: base).isEmpty,
                       "> xyzzy should match nothing")
            return 9
        } count: { "\($0) cases" }
        await t.check("⌘K facets fold casings (main.js:276-282)") {
            let f = CommandCatalog.facets(locations: ["New York", "new york"], roles: ["founder"], topics: ["AI", "ai"])
            try expect(f.count == 3, "\(f.count) commands")
            try expect(f[0].keys.prefix(2) == ["new", "york"], "keys \(f[0].keys)")
            try expect(f[0].action == .people(PeopleRequest(location: "New York")), "location action")
            return f.count
        } count: { "\($0) commands from 5 values" }
        await t.check("⌘K intents (ask.js:53-58, main.js:217-223)") {
            let labels = base.filter { $0.id.hasPrefix("intent") }.map(\.label)
            try expect(labels == ["Ask: Who could introduce me to…", "Ask: Who should I contact this month?",
                                  "Ask: What do I know about…", "Ask: Who in my network knows about…"], "labels \(labels)")
            return labels.count
        } count: { "\($0) intents" }
        await t.check("⌘K commands set the right app state") {
            var checked = 0
            for cmd in base {
                let app = AppModel(engine: EngineController(port: 1, isDevelopmentPort: true))
                app.selectedPersonID = 7
                switch cmd.action {
                // These act on the Mac or another screen's code; their wiring is read, not pressed.
                case .appearance, .revealDataFolder, .copy, .askSend, .findDuplicates, .exportBackup: continue
                default: break
                }
                CommandBarRunner.perform(cmd.action, app: app)
                checked += 1
                switch cmd.id {
                case "add": try expect(app.showAddPerson && app.section == .people && app.selectedPersonID == nil, cmd.id)
                case "paste": try expect(app.setupFlow == .paste, cmd.id)
                case "quiet": try expect(app.peopleRequest == PeopleRequest(sort: "quiet") && app.section == .people
                                         && app.selectedPersonID == nil, cmd.id)
                case "todo": try expect(app.peopleRequest == PeopleRequest(filter: "todo"), cmd.id)
                case "recent": try expect(app.peopleRequest == PeopleRequest(sort: "recent"), cmd.id)
                case "deleted": try expect(app.peopleRequest == PeopleRequest(filter: "deleted"), cmd.id)
                case "excluded": try expect(app.peopleRequest == PeopleRequest(filter: "excluded"), cmd.id)
                case "ask": try expect(app.section == .ask, cmd.id)
                case "newchat": try expect(app.requestNewChat && app.section == .ask, cmd.id)
                case "data": try expect(app.section == .data, cmd.id)
                case "merge": try expect(app.mergeRequest == MergeRequest(), cmd.id)
                case "lists": try expect(app.section == .lists, cmd.id)
                case "newlist": try expect(app.requestNewList && app.section == .lists, cmd.id)
                case "settings": try expect(app.section == .settings, cmd.id)
                case "lifacts": try expect(app.dataFocus == "linkedin_facts" && app.section == .data, cmd.id)
                case let id where id.hasPrefix("intent"):
                    try expect(!app.askDraft.isEmpty && app.section == .ask, cmd.id)
                default: try expect(false, "no expectation for \(cmd.id)")
                }
            }
            var p = PersonDetail()
            p.id = 42
            p.name = "Selftest Ada"
            let mine = CommandCatalog.person(p)
            let app = AppModel(engine: EngineController(port: 1, isDevelopmentPort: true))
            CommandBarRunner.perform(mine[0].action, app: app)
            try expect(app.mergeRequest == MergeRequest(firstID: 42), "person merge")
            CommandBarRunner.perform(mine[1].action, app: app)
            try expect(app.selectedPersonID == 42 && app.section == .people, "person note")
            CommandBarRunner.perform(mine[2].action, app: app)
            try expect(app.askDraft == "What do I know about Selftest Ada" && app.section == .ask, "person ask")
            CommandBarRunner.perform(.openChat(9), app: app)
            try expect(app.selectedChatID == 9 && app.section == .ask, "open chat")
            return checked + 4
        } count: { "\($0) commands" }
        await t.check("⌘K a conversation hit opens the right page (main.js:466-472)") {
            var hit = CmdConversationHit()
            hit.contactID = 0
            hit.openID = 0
            try expect(CommandBarModel.action(for: .convo(hit, index: 0)) == nil, "group hit with nobody opened a page")
            hit.openID = 12
            try expect(CommandBarModel.action(for: .convo(hit, index: 0)) == .openPerson(12), "group hit")
            hit.openID = nil
            hit.contactID = 7
            try expect(CommandBarModel.action(for: .convo(hit, index: 0)) == .openPerson(7), "person hit")
            try expect(CommandBarModel.action(for: .meaning) == nil, "by-meaning row closes the bar")
            try expect(CommandBarModel.action(for: .ask("who")) == .askSend("who"), "ask row")
            return 5
        } count: { "\($0) cases" }
        await t.check("setup gate stays shut on a development port") {
            let app = AppModel(engine: EngineController(port: 1, isDevelopmentPort: true))
            await OnboardingGate.check(app)
            try expect(!app.showOnboarding, "the wizard opened on a development port")
            app.snapshotHint = "onboarding"
            await OnboardingGate.check(app)
            try expect(app.showOnboarding, "the snapshot hint did not open it")
            return 2
        } count: { "\($0) cases" }
    }

    static func commandWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))

        await t.check("setup /api/import/sources (scratch engine only)") {
            let d = try await c.get("/api/import/sources", as: OnbSourcesResponse.self)
            let contacts = d.sources.first { $0.key == "contacts" }
            let imessage = d.sources.first { $0.key == "imessage" }
            try expect(contacts?.pickKind == "vcf" && imessage?.pickKind == "chatdb", "contacts/imessage specs")
            return d
        } count: { "\($0.sources.count) sources, ceiling \($0.importTimeoutSec ?? 0) s" }

        // Two made-up people: one described through the wizard's Save, one skipped.
        let ada = await t.check("setup POST /api/contacts") {
            try await c.post("/api/contacts", body: ["name": .string("Selftest Ada \(stamp)")] as [String: JSONValue],
                             as: CreatedID.self)
        } count: { _ in "created" }
        let bob = await t.check("setup POST /api/contacts (second)") {
            try await c.post("/api/contacts", body: ["name": .string("Selftest Bob \(stamp)")] as [String: JSONValue],
                             as: CreatedID.self)
        } count: { _ in "created" }
        if let ada, let bob {
            let engine = EngineController(address: c.address, launch: nil)
            await engine.connect()
            let app = AppModel(engine: engine)
            let store = OnboardingStore()
            store.attach(app)
            var a = OnbQueuePerson()
            a.id = ada.id
            a.name = "Selftest Ada"
            var b = OnbQueuePerson()
            b.id = bob.id
            b.name = "Selftest Bob"
            store.useQueue([a, b])
            await t.check("setup Save → POST /api/contact/{id}/note") {
                store.noteText = "Met at a selftest of first-run setup."
                await store.saveNote()
                try expect(store.toast == nil, "the save showed an error")
                try expect(store.rememberedCount == 1 && store.queueIndex == 1 && store.noteText.isEmpty, "did not advance")
                let d = try await c.get("/api/contact/\(ada.id)", as: PersonDetail.self)
                try expect(d.notes.count == 1, "\(d.notes.count) notes")
                return d.notes.count
            } count: { "\($0) note saved" }
            await t.check("setup Skip → POST /api/contact/{id}/skip") {
                await store.skip()
                try expect(store.toast == nil, "the skip showed an error")
                try expect(store.queueIndex == 2 && store.currentPerson == nil, "did not advance")
                let d = try await c.get("/api/contact/\(bob.id)", as: PersonDetail.self)
                try expect(d.status == "skipped", "status is \(d.status)")
                return d.status
            } count: { "status=\($0)" }
            await t.check("setup /api/describe-queue (after)") {
                try await c.get("/api/describe-queue", as: OnbDescribeQueue.self)
            } count: { "\($0.queue.count) people" }

            // Finishing marks setup done; the gate's read-only signal then agrees. The
            // engine's earlier answer is put back afterwards.
            let before = await t.check("setup gate before") {
                try await c.get("/api/settings/ledger", as: OnbSettingsLedger.self)
            } count: { "done=\($0.install.setupAgain.done)" }
            if let before {
                await t.check("setup Finish → POST /api/onboarding {done:true}") {
                    app.showOnboarding = true
                    await store.finish()
                    try expect(store.toast == nil, "finishing showed an error")
                    try expect(!app.showOnboarding, "the sheet stayed open")
                    let needs = await OnboardingGate.needsSetup(c)
                    try expect(!needs, "the gate still wants setup")
                    return needs
                } count: { "gate needs setup=\($0)" }
                await t.check("setup restore POST /api/onboarding") {
                    try await c.post("/api/onboarding", body: ["done": .bool(before.install.setupAgain.done)] as [String: JSONValue],
                                     as: OnboardingState.self)
                } count: { "done=\($0.done)" }
            }
            app.stopPolling()
        }

        await askRowWrites(&t)

        // Tidy the scratch engine: soft-delete the two made-up people.
        for id in [ada?.id, bob?.id].compactMap({ $0 }) {
            await t.check("setup POST /api/contact/{id}/delete") {
                try await c.post("/api/contact/\(id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
            } count: { "deleted=\($0.deleted ?? false)" }
        }
    }

    /// ⌘K's last row, `Ask: "…"`, sends the question at once in the newest chat
    /// (main.js:474, ask.js:1028-1036). Only where it cannot cost anything: the scratch
    /// engine with no AI key. A fresh empty chat is made first, so "the newest chat" is
    /// this test's own and no other chat is touched; it is deleted afterwards.
    private static func askRowWrites(_ t: inout Tally) async {
        let c = t.client
        guard let status = try? await c.get("/api/status", as: EngineStatus.self), !status.ai else {
            t.note("⌘K Ask row sends at once", "skipped: this engine has an AI key, so a question could cost money")
            return
        }
        let chat = await t.check("⌘K Ask row: POST /api/chats (its own chat)") {
            try await c.post("/api/chats", body: ["shape": ["people": "10", "detail": "brief", "search": "quick"]] as [String: JSONValue],
                             as: ChatCreated.self)
        } count: { "created=\($0.created)" }
        guard let chat else { return }
        let id = chat.conversation.id
        let engine = EngineController(address: c.address, launch: nil)
        await engine.connect()
        let app = AppModel(engine: engine)
        app.selectedChatID = 5
        await t.check("⌘K Ask row sends at once (POST /api/chat in the newest chat)") {
            let send = CommandBarRunner.askNow("Who works in engineering?", app: app)
            // What Ask's screen does as it appears: its own load, which makes the first
            // one's answer stale. The question must still go once that load lands.
            let model = AskModel.shared(for: app)
            Task { await model.loadTranscript() }
            await send.value
            try expect(app.section == .ask && app.selectedChatID == nil, "did not open the newest chat")
            try expect(app.askDraft.isEmpty, "the question was left in the composer instead of sent")
            try expect(model.convID == id, "sent to another chat than the newest")
            var waited = 0
            while model.pending != nil && waited < 300 {
                try? await Task.sleep(for: .milliseconds(100))
                waited += 1
            }
            try expect(model.pending == nil, "no answer within 30 s")
            let h = try await c.get("/api/chat/\(id)/history", as: ChatHistory.self)
            try expect(h.messages.count == 2 && h.messages.first?.role == "user", "\(h.messages.count) stored messages")
            return h.messages.count
        } count: { "\($0) stored messages" }
        app.stopPolling()
        await t.check("⌘K Ask row: DELETE /api/chat/{id}") {
            try await c.delete("/api/chat/\(id)", as: ChatDeleted.self)
        } count: { "\($0.turns) turns kept on disk" }
    }

    /// A failed expectation, as an error the tally prints (never a value from the engine).
    /// The sentence is printed here because a read run hides engine messages; it names
    /// command ids and counts only.
    fileprivate static func expect(_ ok: Bool, _ what: @autoclosure () -> String) throws {
        guard !ok else { return }
        let sentence = what()
        print("  expected: \(sentence)")
        throw EngineError.engine(status: 0, message: "expected: \(sentence)")
    }
}
