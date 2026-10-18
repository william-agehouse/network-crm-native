import Foundation

// --selftest and --selftest-writes checks for the setup sheets (Full Disk Access, Gmail, paste a list). Owned by that screen's files.
// Reads run against the real data (print counts only, never a value); writes run only
// against the empty scratch engine on 8768 (SelfTest.run refuses anything else).
//
// Each read below was checked in its handler before it was allowed here: none writes, none
// uses the network, and none looks inside a folder macOS protects.
//   GET /api/fda/status     four file-exists checks (the helper's LaunchAgent, its program,
//                           its installer) and the helper's own report file in the app's own
//                           folder (crm/api_fda.py:193-283, crm/importers.py:1092-1162)
//   GET /api/gmail/status   the GMAIL_ACCOUNTS setting, file-exists checks in the app's
//                           secrets folder and one database read (crm/api_gmail.py:120-230)
//   GET /api/intake/status  environment values only (crm/api_intake.py:453-494)
//   GET /api/intake/batch   one row of the database's state table (crm/intake.py:1723-1734)
//
// Never called by either mode, because they act on the Mac or an account, or cost money:
// POST /api/fda/install, /uninstall, /recheck; POST /api/gmail/credentials, /connect;
// POST /api/intake/lookup. POST /api/intake/propose runs only on the scratch engine and
// only when it reports no AI key, where it is the plain read under another name.

extension SelfTest {
    static func setupReads(_ t: inout Tally) async {
        let c = t.client
        await t.check("/api/fda/status") { try await c.get("/api/fda/status", as: FullDiskAccessStatus.self) } count: {
            "state=\($0.state), \($0.sources.count) sources, \($0.blocked.count) blocked, installed=\($0.installed)"
        }
        await t.check("Full Disk Access sheet loads") {
            let store = FullDiskAccessStore()
            store.client = c
            await store.load()
            guard let s = store.status else { throw EngineError.engine(status: 0, message: store.loadError ?? "no status") }
            return s
        } count: { "\($0.sources.count) source lines" }
        await t.check("Full Disk Access waits (fda.js:193-199)") {
            let w = FullDiskAccessStore.self
            let got = [
                w.wait("install", waits: ["install": 300]), w.wait("recheck", waits: nil),
                w.wait("recheck", waits: ["recheck": -1]), w.wait("install", waits: ["install": 99_999]),
            ]
            let want: [TimeInterval] = [315, 80, 80, w.sessionCeiling]
            guard got == want else { throw EngineError.engine(status: 0, message: "waits \(got), expected \(want)") }
            return got.count
        } count: { "\($0) cases" }
        await t.check("Full Disk Access live route per source (importers.py auto_copy_label)") {
            var s = SetupSamples.fdaBlocked
            let partial = FullDiskAccessWords.liveRoutes(s)
            s.installed = false
            let off = FullDiskAccessWords.liveRoutes(s)
            s.installed = true
            s.sources = s.sources.map { var x = $0; x.outcome = "current"; return x }
            let all = FullDiskAccessWords.liveRoutes(s)
            guard partial.helper == ["WhatsApp", "Contacts"], partial.ownCopy == ["iMessage", "Calendar"],
                  off.helper.isEmpty, off.ownCopy.count == 4, all.ownCopy.isEmpty,
                  FullDiskAccessWords.ownCopyTag(all).0 == "Ready if you turn the helper off"
            else { throw EngineError.engine(status: 0, message: "routes \(partial) \(off) \(all)") }
            return 3
        } count: { "\($0) cases" }

        await t.check("/api/gmail/status") { try await c.get("/api/gmail/status", as: GmailStatus.self) } count: {
            "\($0.accounts.count) addresses, \($0.needsFile.count) need a file, \($0.needsSignIn.count) need a sign-in, sign-in \($0.connect.state)"
        }
        await t.check("Gmail sheet loads") {
            let store = GmailStore()
            store.client = c
            await store.load()
            guard let s = store.status else { throw EngineError.engine(status: 0, message: store.loadError ?? "no status") }
            return s
        } count: { "\($0.accounts.count) rows" }
        await t.check("Gmail row wording (gmail.js:200-206)") {
            var row = GmailAccount()
            var connect = GmailConnect()
            row.email = "a@example.com"
            var seen: [GmailRowState] = [GmailRowState(row, connect: connect)]
            row.hasCredentials = true
            seen.append(GmailRowState(row, connect: connect))
            row.hasToken = true
            seen.append(GmailRowState(row, connect: connect))
            row.signInExpired = true
            seen.append(GmailRowState(row, connect: connect))
            connect.state = "running"
            connect.account = row.email
            seen.append(GmailRowState(row, connect: connect))
            let want: [GmailRowState] = [.needsFile, .readyToSignIn, .connected, .expired, .waiting]
            guard seen == want else { throw EngineError.engine(status: 0, message: "row states \(seen)") }
            return seen.count
        } count: { "\($0) states" }

        await t.check("/api/intake/status") { try await c.get("/api/intake/status", as: PasteIntakeStatus.self) } count: {
            "llm=\($0.llm) web=\($0.webLookup), \($0.settings.count) limits, \($0.sentences.count) sentences"
        }
        await t.check("GET /api/intake/batch") {
            let raw = try await c.get("/api/intake/batch", as: JSONValue.self)
            return try PasteJSON.typed(raw, as: PasteDraftAnswer.self, route: "/api/intake/batch")
        } count: { $0.batch.proposals.isEmpty ? "no draft parked" : "a draft of \($0.batch.proposals.count) parked" }
        await t.check("Paste sheet loads") {
            let store = PasteStore()
            store.client = c
            await store.load()
            guard store.status != nil, store.err.isEmpty else { throw EngineError.engine(status: 0, message: store.err) }
            return store.draft?.draft.proposals.count ?? 0
        } count: { "\($0) people in a parked draft" }

        t.note("POST /api/fda/install, /uninstall, /recheck", "never run: they install or run a background helper")
        t.note("POST /api/gmail/credentials, /connect", "never run: Google sign-in and saved credentials")
        t.note("POST /api/intake/lookup", "never run: it fetches a web page")
    }

    // MARK: Writes (scratch engine only)

    static func setupWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        let intake = await t.check("/api/intake/status (writes)") {
            try await c.get("/api/intake/status", as: PasteIntakeStatus.self)
        } count: { "llm=\($0.llm)" }
        let engine = try? await c.get("/api/status", as: EngineStatus.self)
        guard let intake, let engine else { return }
        if intake.llm || engine.ai {
            t.fail("POST /api/intake/parse", "REFUSED: this engine has an AI key, so a read could cost money")
            return
        }

        await pasteWrites(&t, stamp: stamp)
        await gmailWrites(&t, stamp: stamp)
    }

    private static func pasteWrites(_ t: inout Tally, stamp: String) async {
        let c = t.client
        // The parser reads a word with digits as not part of a name, so the run's stamp
        // goes into the names as letters ("1792…" → "Bhjc…"): unique per run, and no
        // earlier run's tidied-up person can match it.
        let tag = String(stamp.compactMap { $0.wholeNumberValue.map { Character(UnicodeScalar(UInt8(97 + $0))) } }).capitalized
        let phone = "+46 7\(stamp.suffix(8))"
        let text = """
            Selftest Ada \(tag) — Analytical Engines
            linkedin.com/in/selftest-grace-\(tag.lowercased())
            Selftest Alan \(tag), alan-\(stamp)@example.com, \(phone)

            ???
            Selftest Alan \(tag), alan-\(stamp)@example.com
            """
        let model = AppModel(engine: EngineController(address: c.address, launch: nil))
        let store = PasteStore()
        store.client = c
        store.onWrote = { model.peopleChanged() }
        store.text = text

        // The plain read, through the store, with the web page's own blocker rules.
        let parsed = await t.check("POST /api/intake/parse") {
            await store.read(.parse)
            guard let plan = store.plan, store.err.isEmpty else { throw EngineError.engine(status: 0, message: store.err) }
            return plan
        } count: { "\($0.proposals.count) proposed, \($0.report.skippedDetail.count) not proposed" }
        guard let parsed, parsed.proposals.count == 4 else {
            t.fail("paste review", "expected 4 proposals")
            return
        }
        let grace = parsed.proposals[1].ref, dup = parsed.proposals[3].ref
        await t.check("paste review blockers (intake.js:125-152)") {
            let before = store.counts
            guard before.create == 2, store.rows[1].blocked.count == 1, store.rows[3].blocked.count == 1 else {
                throw EngineError.engine(status: 0, message: "create \(before.create), blocked \(store.rows.map(\.blocked.count))")
            }
            store.setEdit(grace, "name_confirmed", .bool(true))
            store.setEdit(parsed.proposals[0].ref, "company", .string("Selftest Works"))
            store.setSkip(dup, true)
            let after = store.counts
            guard after.create == 3, after.skipped == 1, store.rows[0].item.company == "Selftest Works" else {
                throw EngineError.engine(status: 0, message: "after edits: create \(after.create), skipped \(after.skipped)")
            }
            // Typing a name takes the confirmation off again.
            store.setEdit(grace, "name", .string("Selftest Grace \(tag)"))
            guard store.rows[1].item.nameConfirmed == false else { throw EngineError.engine(status: 0, message: "typing kept the tick") }
            store.setEdit(grace, "name_confirmed", .bool(true))
            return store.counts
        } count: { "\($0.create) to add, \($0.skipped) skipped" }

        // Park, read it back the way the web page would, reopen it, discard it.
        await t.check("POST /api/intake/batch") {
            await store.park()
            guard !store.saved.isEmpty, store.err.isEmpty else { throw EngineError.engine(status: 0, message: store.err) }
            return store.saved
        } count: { _ in "draft parked" }
        await t.check("GET /api/intake/batch (round trip)") {
            let raw = try await c.get("/api/intake/batch", as: JSONValue.self)
            let d = try PasteJSON.typed(raw, as: PasteDraftAnswer.self, route: "/api/intake/batch").batch
            guard d.proposals.count == 4, d.skips[dup] == true, d.edits[grace]?["name_confirmed"]?.pasteTruthy == true,
                  d.text == text, raw.pasteKey("batch")?.pasteKey("report")?.pasteKey("accounting") != nil
            else { throw EngineError.engine(status: 0, message: "the parked draft lost something") }
            return d
        } count: { "\($0.proposals.count) people, \($0.edits.count) edited, \($0.skips.count) skipped" }
        await t.check("paste draft reopens") {
            let again = PasteStore()
            again.client = c
            await again.load()
            guard again.draft?.draft.proposals.count == 4 else { throw EngineError.engine(status: 0, message: "no draft offered") }
            again.resume()
            guard again.counts.create == 3, again.counts.skipped == 1, again.text == text else {
                throw EngineError.engine(status: 0, message: "reopened draft differs")
            }
            return again.counts
        } count: { "\($0.create) to add after reopening" }
        await t.check("DELETE /api/intake/batch") {
            let other = PasteStore()
            other.client = c
            await other.load()
            await other.discard()
            let raw = try await c.get("/api/intake/batch", as: JSONValue.self)
            let d = try PasteJSON.typed(raw, as: PasteDraftAnswer.self, route: "/api/intake/batch").batch
            guard other.err.isEmpty, d.proposals.isEmpty else { throw EngineError.engine(status: 0, message: "draft still there") }
            return 0
        } count: { _ in "draft discarded" }

        // A commit without confirm:true must write nothing.
        await t.check("POST /api/intake/commit (no confirm)") {
            let r = try await c.post(
                "/api/intake/commit", body: ["batch": .array(store.rows.map(\.item.json)), "confirm": false] as [String: JSONValue],
                as: PasteReceipt.self)
            guard !r.wroteAnything else { throw EngineError.engine(status: 0, message: "wrote without confirm") }
            return r
        } count: { _ in "nothing written" }

        let receipt = await t.check("POST /api/intake/commit") {
            await store.commit()
            guard let r = store.receipt, store.err.isEmpty else { throw EngineError.engine(status: 0, message: store.err) }
            guard r.created == 3, r.wroteAnything, store.phase == .done, model.peopleRevision == 1 else {
                throw EngineError.engine(status: 0, message: "created \(r.created), people refreshes \(model.peopleRevision)")
            }
            return r
        } count: { "\($0.created) added, \($0.enriched) updated, \($0.outcomes.count) outcomes, People refreshed" }

        // The sheet keeps its work between openings; one that ended in a save reopens empty.
        await t.check("paste sheet reopens after a save") {
            guard PasteMemory.store(for: model) === PasteMemory.store(for: model) else {
                throw EngineError.engine(status: 0, message: "a second opening got a new store")
            }
            await store.load()
            await store.start(app: model)
            guard store.phase == .paste, store.text.isEmpty, store.plan == nil, store.receipt == nil else {
                throw EngineError.engine(status: 0, message: "reopened in \(store.phase) with \(store.textLength) characters")
            }
            return store.textLength
        } count: { _ in "empty paste box" }

        // The plain read under the model's name, with no key: it must say it degraded.
        await t.check("POST /api/intake/propose (no key)") {
            let p = PasteStore()
            p.client = c
            p.text = text
            await p.read(.propose)
            guard let plan = p.plan, !plan.report.degraded.isEmpty else { throw EngineError.engine(status: 0, message: p.err) }
            return plan
        } count: { "\($0.proposals.count) proposed, degraded path stated" }

        // Tidy the scratch engine: soft-delete the people the commit made.
        let made = (receipt?.outcomes ?? []).compactMap(\.contactID)
        for id in made {
            await t.check("POST /api/contact/{id}/delete (paste tidy)") {
                try await c.post("/api/contact/\(id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
            } count: { "deleted=\($0.deleted ?? false)" }
        }
    }

    /// Adding and removing an address goes through the GMAIL_ACCOUNTS setting; the value
    /// the scratch engine had before is put back exactly.
    private static func gmailWrites(_ t: inout Tally, stamp: String) async {
        let c = t.client
        guard let before = (try? await c.get("/api/settings", as: SettingsResponse.self))?.value(for: "GMAIL_ACCOUNTS") else {
            t.note("POST /api/settings GMAIL_ACCOUNTS", "skipped: this engine does not manage GMAIL_ACCOUNTS")
            return
        }
        let address = "selftest-ada-\(stamp)@example.com"
        let store = GmailStore()
        store.client = c
        await t.check("POST /api/settings GMAIL_ACCOUNTS (add)") {
            store.draft = address
            await store.addAccount()
            guard let row = store.status?.accounts.first(where: { $0.email == address }),
                  store.status?.needsFile.contains(address) == true, store.draft.isEmpty
            else { throw EngineError.engine(status: 0, message: store.notice?.text ?? "address not listed") }
            return GmailRowState(row, connect: store.status?.connect ?? GmailConnect())
        } count: { "listed, \($0.says.lowercased())" }
        await t.check("POST /api/settings GMAIL_ACCOUNTS (remove)") {
            await store.removeAccount(address)
            let after = try await c.get("/api/settings", as: SettingsResponse.self).value(for: "GMAIL_ACCOUNTS") ?? ""
            guard store.status?.accounts.contains(where: { $0.email == address }) == false,
                  GmailStore.entries(after) == GmailStore.entries(before)
            else { throw EngineError.engine(status: 0, message: "the setting was not put back") }
            return store.status?.accounts.count ?? 0
        } count: { "removed, \($0) addresses left as before" }
    }
}
