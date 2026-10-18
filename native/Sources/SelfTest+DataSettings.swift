import Foundation

// --selftest and --selftest-writes checks for the Data and Settings pages. Owned by that screen's files.
// Reads run against the real data (print counts only, never a value); writes run only
// against the empty scratch engine on 8768 (SelfTest.run refuses anything else).
//
// Never called from here, because they act on the Mac or cost money: /api/import/run,
// /api/import/pick-file, /api/import/open, /api/import/detect, /api/data/ledger?scan=1,
// /api/semantic/install, /api/fda/*, /api/gmail/*, /api/onboarding (either way).

extension SelfTest {
    /// Every control kind the pages know how to draw. A kind the engine sends that is not
    /// here would be a control silently missing from the page.
    static let dataActKinds: Set<String> = ["import", "switch", "open", "secret", "text", "helper", "gmail", "settings", "row"]
    static let settingsActKinds: Set<String> = ["text", "secret", "switch", "model", "goals", "semantic", "page", "open", "row"]

    static func dataSettingsReads(_ t: inout Tally) async {
        let c = t.client

        // The rules the pages work out themselves, against the web page's outputs.
        await t.check("Data and Settings wording rules") { try wordingRules() } count: { "\($0) rules" }

        let ledger = await t.check("/api/data/ledger") { try await c.get("/api/data/ledger", as: LedgerAnswer.self) } count: {
            "\($0.rows.count) rows, \($0.needs.count) needs, \($0.inUse) in use, scanned=\($0.scanned)"
        }
        if let ledger {
            await t.check("/api/data/ledger: every control is drawn") {
                let kinds = Set(ledger.rows.flatMap { $0.detail.acts.map(\.kind) } + ledger.needs.flatMap { $0.acts.map(\.kind) })
                let unknown = kinds.subtracting(dataActKinds)
                // Kind names are the engine's own words, never a person's data.
                guard unknown.isEmpty else { throw selfFail("not drawn: \(unknown.sorted())") }
                guard !ledger.scanned else { throw selfFail("scanned without being asked") }
                return kinds.count
            } count: { "\($0) kinds of control" }
            await t.check("/api/data/ledger: rows say something") {
                // Every row has a name and what it gives; every open row has a Now block or
                // the parts list; every figure the install shows has a population.
                for r in ledger.rows where r.label.isEmpty || r.gives.isEmpty {
                    throw selfFail("a row without words: \(r.key)")
                }
                let nows = ledger.rows.filter { $0.detail.parts != nil || !LedgerWords.now($0).isEmpty }.count
                guard LedgerWords.figure(ledger.install.people.figure) != nil,
                    LedgerWords.figure(ledger.install.messages.figure) != nil
                else { throw selfFail("an install figure without a population") }
                return nows
            } count: { "\($0) of \(ledger.rows.count) rows have a Now block" }

            // NC-371: LinkedIn's client ID and secret have boxes on the messages row once a
            // token is connected, and no row sends anyone to edit the settings file.
            await t.check("/api/data/ledger: LinkedIn's own boxes, no settings file") {
                for r in ledger.rows where (r.detail.help + r.takes).contains("secrets/.env") {
                    throw selfFail("a row sends you to the settings file: \(r.key)")
                }
                guard let li = ledger.rows.first(where: { $0.key == "linkedin_messages" }) else {
                    throw selfFail("no LinkedIn messages row")
                }
                let token = li.detail.acts.contains { $0.kind == "secret" && $0.key == "LINKEDIN_TOKEN" && $0.isSet }
                let id = li.detail.acts.contains { $0.kind == "text" && $0.key == "LINKEDIN_CLIENT_ID" }
                let secret = li.detail.acts.contains { $0.kind == "secret" && $0.key == "LINKEDIN_CLIENT_SECRET" }
                guard id == token, secret == token else {
                    throw selfFail(token ? "a token without the client boxes" : "client boxes without a token")
                }
                // NC-372: Read shared history is the profile facts source's own run, with
                // its one extra field.
                for a in ledger.rows.flatMap(\.detail.acts) where !a.form.isEmpty {
                    guard a.kind == "import", a.source == "linkedin_facts", a.form["extras"] == .bool(true) else {
                        throw selfFail("an unexpected run with extra fields")
                    }
                }
                return token ? 2 : 0
            } count: { "\($0) client boxes" }

            // What a source added: every distinct source a row offers it for.
            let sources = Array(Set(ledger.rows.map(\.detail.provenance).filter { !$0.isEmpty }
                + ledger.rows.flatMap { ($0.detail.parts ?? []).map(\.provenance) }.filter { !$0.isEmpty })).sorted()
            var kinds = 0
            await t.check("/api/provenance?source= (\(sources.count) sources)") {
                for s in sources {
                    let d = try await c.get("/api/provenance", query: ["source": s], as: DataProvenanceAnswer.self)
                    kinds += d.panels.first?.kinds.count ?? 0
                }
                return sources.count
            } count: { "\($0) panels, \(kinds) kinds" }
        }

        let settings = await t.check("/api/settings/ledger") {
            try await c.get("/api/settings/ledger", as: SettingsLedgerAnswer.self)
        } count: { "\($0.rows.count) rows, \($0.needs.count) needs, version set=\(!$0.install.version.isEmpty)" }
        if let settings {
            await t.check("/api/settings/ledger: every control is drawn") {
                let kinds = Set(settings.rows.flatMap { $0.detail.acts.map(\.kind) } + settings.needs.flatMap { $0.acts.map(\.kind) })
                let unknown = kinds.subtracting(settingsActKinds)
                guard unknown.isEmpty else { throw selfFail("not drawn: \(unknown.sorted())") }
                // The model picker must show the stored model as chosen.
                for a in settings.rows.flatMap(\.detail.acts) where a.kind == "model" {
                    guard SettingsModels.choices(current: a.value).contains(where: { $0.value == a.value }) else {
                        throw selfFail("the stored model is not in the picker")
                    }
                }
                return kinds.count
            } count: { "\($0) kinds of control" }
            // Ask's "Open Settings" lands on this row (AppModel.openSettings(row: "ai")).
            await t.check("/api/settings/ledger: the row Ask's Open Settings opens") {
                guard let ai = settings.rows.first(where: { $0.key == "ai" }) else { throw selfFail("no ai row") }
                guard ai.detail.acts.contains(where: { $0.kind == "secret" && $0.key == "ANTHROPIC_API_KEY" }) else {
                    throw selfFail("the ai row has no key box")
                }
                return ai.detail.acts.count
            } count: { "ai row with the key box, \($0) controls" }
            // NC-364: the spend cap and the two prices sit beside the key and the model,
            // and like the model they come with a key.
            await t.check("/api/settings/ledger: the AI row's money boxes") {
                guard let ai = settings.rows.first(where: { $0.key == "ai" }) else { throw selfFail("no ai row") }
                let hasKey = ai.detail.acts.contains { $0.kind == "secret" && $0.key == "ANTHROPIC_API_KEY" && $0.isSet }
                let money = ["ENRICH_LLM_MAX", "NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK"]
                let boxes = ai.detail.acts.filter { $0.kind == "text" && money.contains($0.key) }
                guard boxes.count == (hasKey ? money.count : 0) else {
                    throw selfFail(hasKey ? "a money box is missing" : "money boxes without a key")
                }
                for b in boxes where b.label.isEmpty || b.name.isEmpty || b.placeholder.isEmpty {
                    throw selfFail("a money box without its words or its default")
                }
                return boxes.count
            } count: { "\($0) money boxes" }
        }

        // NC-314: Remove everything's plan. Read only: POST /api/remove/everything is never sent
        // from here. Either it may run (and names what goes and the word to type) or it says why not.
        await t.check("/api/remove/plan") {
            let p = try await c.get("/api/remove/plan", as: RemovePlan.self)
            if p.allowed {
                guard !p.removes.isEmpty, !p.word.isEmpty, p.byHand.count == 2 else {
                    throw selfFail("allowed, but without what goes, the word or the two steps")
                }
            } else if p.whyNot.isEmpty {
                throw selfFail("refused without saying why")
            }
            return p
        } count: { $0.allowed ? "allowed, \($0.removes.count) folders, word \($0.word)" : "refused: \($0.whyNot)" }

        await t.check("/api/duplicates") { try await c.get("/api/duplicates", as: DataDuplicatesAnswer.self) } count: { d in
            let keepable = d.groups.filter { g in LedgerWords.bestToKeep(g).map { id in g.contains { $0.id == id } } ?? false }.count
            return "\(d.groups.count) groups, \(d.groups.reduce(0) { $0 + $1.count }) records, \(keepable) with a record to keep"
        }
        await t.check("/api/diagnostics") { try await c.get("/api/diagnostics", as: SettingsDiagnostics.self) } count: {
            "meaning search installed=\($0.semanticSearch.available), install \($0.semanticSearch.install.state), \($0.syncSummary.collecting) sources imported"
        }
        await t.check("/api/owner") { try await c.get("/api/owner", as: SettingsOwner.self) } count: {
            "max_chars=\($0.maxChars)"
        }
        for f in DataExportFormat.allCases {
            await t.check("/api/export format=\(f.rawValue)") {
                let r = try await c.rawGet("/api/export", query: ["format": f.rawValue])
                guard let name = r.fileName, name.hasPrefix("network-crm-export-"), name.hasSuffix("." + f.rawValue) else {
                    throw selfFail("no download name")
                }
                return r.data.count
            } count: { "\($0) bytes" }
        }
        // An unknown format is a plain sentence, not a crash.
        await t.check("/api/export format=pdf (refused)") {
            do {
                _ = try await c.rawGet("/api/export", query: ["format": "pdf"])
                throw selfFail("accepted an unknown format")
            } catch let e as EngineError where e.status == 400 {
                return 1
            }
        } count: { _ in "refused with a sentence" }
    }

    static func dataSettingsWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        let app = AppModel(engine: EngineController(address: c.address, launch: nil))
        await app.engine.connect()

        // The pages' own stores, end to end, on the scratch engine.
        await t.check("Data page store loads (no scan on a development port)") {
            let store = DataStore()
            await store.start(app: app)
            guard let a = store.answer else { throw selfFail(store.error ?? "no answer") }
            guard !a.scanned, !store.scanAllowed else { throw selfFail("it scanned") }
            // The engine's own ceiling on one import, plus the margin (api.js:58-88).
            guard let sec = a.importTimeoutSec, store.importTimeout == TimeInterval(min(3600, sec) + 15) else {
                throw selfFail("the import wait does not follow the engine's ceiling")
            }
            return a.rows.count
        } count: { "\($0) rows, import wait = ceiling + 15 s" }
        await t.check("Data page keeps one store per window") {
            let other = AppModel(engine: EngineController(address: c.address, launch: nil))
            guard DataStore.store(for: app) === DataStore.store(for: app),
                DataStore.store(for: app) !== DataStore.store(for: other)
            else { throw selfFail("the store is not kept") }
            return true
        } count: { _ in "same store on every visit" }
        await t.check("AppModel.openSettings(row:)") {
            app.section = .data
            app.openSettings(row: "ai")
            guard app.section == .settings, DataSettingsBus.shared.settingsRow == "ai" else {
                throw selfFail("did not go to the ai row")
            }
            DataSettingsBus.shared.settingsRow = nil
            app.section = .people
            return true
        } count: { _ in "Settings, on the ai row" }
        // The import's own transport (longPost), on a harmless write: the goals stored now,
        // sent back unchanged; and a refusal comes back as the engine's sentence.
        await t.check("longPost (the import call's transport)") {
            let now = try await c.get("/api/owner", as: SettingsOwner.self).goals
            let d = try await c.longPost("/api/owner/goals", body: ["goals": .string(now)], as: SettingsGoalsSaved.self, timeout: 60)
            guard d.goals == now, !d.truncated else { throw selfFail("the answer was not read") }
            do {
                _ = try await c.longPost("/api/merge", body: ["keep_id": .int(1), "dup_id": .int(1)], as: WriteResult.self, timeout: 60)
                throw selfFail("a refused write passed")
            } catch let e as EngineError where e.status == 400 {}
            return d.goals.count
        } count: { _ in "answer decoded, refusal is a sentence" }
        await t.check("Settings page store loads") {
            let store = SettingsStore()
            await store.start(app: app)
            guard let a = store.answer, store.owner != nil, store.diagnostics != nil else {
                throw selfFail(store.error ?? "something did not load")
            }
            return a.rows.count
        } count: { "\($0) rows, owner and diagnostics read" }

        // A setting round trip: the box the dates row carries, then put back.
        let before = try? await c.get("/api/settings", as: SettingsResponse.self)
        let days = before?.value(for: "NC_DATES_LOOKAHEAD_DAYS") ?? ""
        let intake = before?.value(for: "NC_INTAKE_WEB") ?? ""
        await t.check("POST /api/settings NC_DATES_LOOKAHEAD_DAYS=21") {
            guard LedgerFormat.textProblem("NC_DATES_LOOKAHEAD_DAYS", "21").isEmpty,
                !LedgerFormat.textProblem("NC_DATES_LOOKAHEAD_DAYS", "400").isEmpty
            else { throw selfFail("the days check is wrong") }
            _ = try await LedgerSettings.save(c, key: "NC_DATES_LOOKAHEAD_DAYS", value: "21")
            let l = try await c.get("/api/settings/ledger", as: SettingsLedgerAnswer.self)
            guard let row = l.rows.first(where: { $0.key == "dates" }), row.have.says.contains("21") else {
                throw selfFail("the dates row did not change")
            }
            return row.detail.acts.first { $0.key == "NC_DATES_LOOKAHEAD_DAYS" }?.value ?? ""
        } count: { "row now says \($0)" }
        await t.check("POST /api/settings NC_INTAKE_WEB=1 (a switch)") {
            _ = try await LedgerSettings.save(c, key: "NC_INTAKE_WEB", value: "1")
            let l = try await c.get("/api/settings/ledger", as: SettingsLedgerAnswer.self)
            guard let row = l.rows.first(where: { $0.key == "intake_web" }), row.inUse,
                row.detail.acts.first?.on == true
            else { throw selfFail("the switch did not change") }
            return true
        } count: { _ in "switch reads on" }
        await t.check("POST /api/settings (not a managed key, refused)") {
            do {
                _ = try await LedgerSettings.save(c, key: "SELFTEST_NOT_A_KEY", value: "x")
                throw selfFail("accepted")
            } catch let e as EngineError where e.status == 400 {
                return LedgerSettings.sentence(e).isEmpty ? 0 : 1
            }
        } count: { _ in "refused with a sentence" }
        await t.check("POST /api/settings (put back)") {
            _ = try await LedgerSettings.save(c, key: "NC_DATES_LOOKAHEAD_DAYS", value: days)
            _ = try await LedgerSettings.save(c, key: "NC_INTAKE_WEB", value: intake)
            return 2
        } count: { "\($0) settings restored" }

        // NC-364: the spend cap moves what both pages say a paid run covers, and a value
        // outside a money box's bounds is refused with a sentence, never stored.
        let money = ["ENRICH_LLM_MAX", "NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK"]
        let keptMoney = money.map { k in before?.settings.first { $0.key == k } }
        await t.check("POST /api/settings ENRICH_LLM_MAX=12 (the spend cap)") {
            _ = try await LedgerSettings.save(c, key: "ENRICH_LLM_MAX", value: "12")
            let s = try await c.get("/api/settings/ledger", as: SettingsLedgerAnswer.self)
            let d = try await c.get("/api/data/ledger", as: LedgerAnswer.self)
            guard s.rows.first(where: { $0.key == "ai" })?.takes.contains("at most 12 people") == true,
                d.rows.first(where: { $0.key == "facts_llm" })?.takes.contains("at most 12 people") == true
            else { throw selfFail("a page did not follow the cap") }
            return 2
        } count: { "\($0) pages say 12" }
        await t.check("POST /api/settings (money out of bounds, refused)") {
            var refused = 0
            for (k, v) in [("ENRICH_LLM_MAX", "0"), ("ENRICH_LLM_MAX", "5000"), ("ENRICH_LLM_MAX", "twelve"),
                           ("NC_PRICE_IN_PER_MTOK", "abc"), ("NC_PRICE_OUT_PER_MTOK", "5000")] {
                do {
                    _ = try await LedgerSettings.save(c, key: k, value: v)
                    throw selfFail("accepted an out-of-bounds \(k)")
                } catch let e as EngineError where e.status == 400 {
                    guard !LedgerSettings.sentence(e).isEmpty else { throw selfFail("no sentence") }
                    refused += 1
                }
            }
            return refused
        } count: { "\($0) refused with a sentence" }
        await t.check("POST /api/settings (money put back)") {
            for (k, row) in zip(money, keptMoney) {
                _ = try await LedgerSettings.save(c, key: k, value: row?.isSet == true ? row?.value ?? "" : "")
            }
            return money.count
        } count: { "\($0) settings restored" }

        // What you are working on: save, trim at the ceiling, put back.
        let owner = try? await c.get("/api/owner", as: SettingsOwner.self)
        await t.check("POST /api/owner/goals") {
            let store = SettingsStore()
            await store.start(app: app)
            store.goalsDraft = "Selftest goals \(stamp): meet more people who build things."
            await store.saveGoals()
            let read = try await c.get("/api/owner", as: SettingsOwner.self)
            guard read.goals == store.goalsDraft, !store.goalsDirty else {
                throw selfFail("the goals were not stored")
            }
            return read.goals.count
        } count: { "\($0) characters stored" }
        await t.check("POST /api/owner/goals (over the ceiling)") {
            let max = owner?.maxChars ?? 4000
            let d = try await c.post("/api/owner/goals", body: ["goals": .string(String(repeating: "a", count: max + 50))]
                as [String: JSONValue], as: SettingsGoalsSaved.self)
            guard d.truncated, d.goals.count == max else { throw selfFail("not trimmed") }
            return d.goals.count
        } count: { "trimmed to \($0)" }
        await t.check("POST /api/owner/goals (put back)") {
            try await c.post("/api/owner/goals", body: ["goals": .string(owner?.goals ?? "")] as [String: JSONValue],
                             as: SettingsGoalsSaved.self).goals.count
        } count: { "\($0) characters, as before" }

        // Merging two made-up near-duplicates through the sheet's own store.
        let name = "Selftest Ada Merge \(stamp)"
        let first = try? await c.post("/api/contacts", body: ["name": .string(name)] as [String: JSONValue], as: CreatedID.self).id
        let second = try? await c.post("/api/contacts", body: ["name": .string(name)] as [String: JSONValue], as: CreatedID.self).id
        guard let a = first, let b = second else {
            t.fail("POST /api/merge", "could not make the two made-up people")
            return
        }
        _ = try? await c.post("/api/contact/\(a)/field", body: ["field": "company", "value": "Analytical Engines"]
            as [String: JSONValue], as: WriteResult.self)
        _ = try? await c.post("/api/contact/\(b)/note", body: ["body": "Selftest note to carry over."]
            as [String: JSONValue], as: NoteCreated.self)
        await t.check("GET /api/duplicates (the made-up pair)") {
            let groups = try await c.get("/api/duplicates", as: DataDuplicatesAnswer.self).groups
            guard let g = groups.first(where: { g in g.contains { $0.id == a } && g.contains { $0.id == b } }) else {
                throw selfFail("the pair is not a group")
            }
            // The filled record is the default to keep.
            guard LedgerWords.bestToKeep(g) == a else { throw selfFail("kept the bare record") }
            return g.count
        } count: { "group of \($0), the filled one kept by default" }
        await t.check("POST /api/merge (merge sheet)") {
            let store = MergeStore(request: MergeRequest(firstID: a, secondID: b))
            await store.start(app: app)
            guard store.ready, store.fold?.notes == 1, store.movesLine != nil else {
                throw selfFail("the sheet did not read both people")
            }
            app.selectedPersonID = b
            guard await store.merge() else { throw selfFail(store.error ?? "merge failed") }
            guard app.selectedPersonID == a, app.peopleRevision > 0 else {
                throw selfFail("the window was not moved to the kept person")
            }
            return true
        } count: { _ in "merged, page moved to the kept person" }
        await t.check("GET /api/contact/{id} (after the merge)") {
            do {
                _ = try await c.get("/api/contact/\(b)", as: PersonDetail.self)
                throw selfFail("the folded record still exists")
            } catch let e as EngineError where e.status == 404 {}
            let kept = try await c.get("/api/contact/\(a)", as: PersonDetail.self)
            guard kept.notes.count == 1, kept.company == "Analytical Engines" else {
                throw selfFail("the note or the company did not survive")
            }
            return kept.notes.count
        } count: { "folded record gone, \($0) note carried over" }
        await t.check("POST /api/merge (same person, refused)") {
            do {
                _ = try await c.post("/api/merge", body: ["keep_id": .int(a), "dup_id": .int(a)] as [String: JSONValue], as: WriteResult.self)
                throw selfFail("accepted")
            } catch let e as EngineError where e.status == 400 {
                return 1
            }
        } count: { _ in "refused with a sentence" }

        // The duplicates list's own merge: a group of three, folded into one.
        let trio = "Selftest Bea Trio \(stamp)"
        var ids: [Int] = []
        for _ in 0..<3 {
            if let id = try? await c.post("/api/contacts", body: ["name": .string(trio)] as [String: JSONValue], as: CreatedID.self).id {
                ids.append(id)
            }
        }
        await t.check("POST /api/merge ×2 (a group of three, Data page)") {
            let store = DataStore()
            await store.start(app: app)
            let groups = try await c.get("/api/duplicates", as: DataDuplicatesAnswer.self).groups
            guard let g = groups.first(where: { g in Set(g.map(\.id)) == Set(ids) }) else {
                throw selfFail("the three are not one group")
            }
            await store.merge(g, keep: ids[0])
            let after = try await c.get("/api/duplicates", as: DataDuplicatesAnswer.self).groups
            guard !after.contains(where: { g in g.contains { ids.contains($0.id) } }) else {
                throw selfFail("a group is left")
            }
            return g.count - 1
        } count: { "\($0) records folded" }

        // Tidy the scratch engine: soft-delete what is left of the made-up people.
        for id in [a] + ids.prefix(1) {
            await t.check("POST /api/contact/{id}/delete (tidy)") {
                try await c.post("/api/contact/\(id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
            } count: { "deleted=\($0.deleted ?? false)" }
        }
        app.stopPolling()
    }

    /// A failure of one of these checks, in the check's own words (never an engine value),
    /// as a decoding-style error so Tally prints the reason on the real data too.
    private static func selfFail(_ why: String) -> EngineError {
        .decoding(route: "", path: "check", problem: why)
    }

    /// The pages' own rules, checked against what the web page prints for the same input.
    private static func wordingRules() throws -> Int {
        var n = 0
        func expect(_ got: String, _ want: String, _ what: String) throws {
            n += 1
            guard got == want else { throw selfFail("\(what): got “\(got)”") }
        }
        try expect(LedgerWords.ago(30), "just now", "ago 30 s")
        try expect(LedgerWords.ago(720), "12 minutes ago", "ago 12 min")
        try expect(LedgerWords.ago(3600), "an hour ago", "ago 1 h")
        try expect(LedgerWords.ago(5 * 3600), "5 hours ago", "ago 5 h")
        try expect(LedgerWords.ago(30 * 3600), "30 hours ago", "ago 30 h")
        try expect(LedgerWords.ago(40 * 3600), "2 days ago", "ago 40 h")
        try expect(LedgerWords.ago(5 * 86400), "5 days ago", "ago 5 d")
        try expect(LedgerWords.ago(nil), "", "ago nil")
        try expect(LedgerWords.size(2_500_000_000), "2.5 GB", "size GB")
        try expect(LedgerWords.size(340_400_000), "340 MB", "size MB")
        try expect(LedgerWords.size(200), "1 KB", "size tiny")
        try expect(LedgerWords.stop("Off"), "Off.", "stop")
        try expect(LedgerWords.stop("Done!"), "Done!", "stop kept")
        try expect(LedgerWords.duplicates(0), "None found", "dups 0")
        try expect(LedgerWords.duplicates(1), "1 group of people who may be the same person", "dups 1")
        try expect(LedgerWords.duplicates(nil), "Could not be checked just now", "dups nil")
        try expect(LedgerFormat.secretProblem(""), "Paste it first.", "secret empty")
        try expect(LedgerFormat.secretProblem("a b"), "That has a space or a line break in it. Copy the value again, on its own.", "secret space")
        try expect(LedgerFormat.secretProblem("sk-test-not-a-key", key: "ANTHROPIC_API_KEY"),
                   "Keys from Anthropic start with sk-ant-. Copy the whole key again.", "secret prefix")
        try expect(LedgerFormat.secretProblem("…abcd", masked: "…abcd"), "That is the hidden copy shown here. Paste the real value.", "secret masked")
        try expect(LedgerFormat.textProblem("SELF_EMAIL", "nope"), "Type whole email addresses, like you@example.com. Put a comma between two.", "email")
        try expect(LedgerFormat.textProblem("SELF_EMAIL", "me@home.example, me@work.example"), "", "two emails (NC-328)")
        try expect(LedgerFormat.textProblem("SELF_EMAIL", "me@home.example, nope"),
                   "Type whole email addresses, like you@example.com. Put a comma between two.", "one bad of two")
        try expect(LedgerFormat.textProblem("SELF_EMAIL", ""), "", "email clear")
        try expect(LedgerFormat.textProblem("NC_ICS_GOOGLE_URL", "webcal://example.com/x.ics"), "", "calendar link")
        try expect(LedgerFormat.secretProblem("https://a.example/1.ics, https://b.example/2.ics", key: "NC_ICS_GOOGLE_URL"),
                   "", "two calendar links (NC-303)")
        try expect(LedgerFormat.pasted("NC_ICS_GOOGLE_URL", " https://a.example/1.ics,\nhttps://b.example/2.ics "),
                   "https://a.example/1.ics,https://b.example/2.ics", "saved with one comma")
        try expect(LedgerFormat.secretProblem("https://a.example/1.ics, nope", key: "NC_ICS_GOOGLE_URL"),
                   "Start each address with https:// or webcal://. Copy it again from the calendar's settings.", "one bad link")
        try expect(LedgerFormat.secretProblem("sk-ant-a b", key: "ANTHROPIC_API_KEY"),
                   "That has a space or a line break in it. Copy the value again, on its own.", "space still refused elsewhere")
        try expect(LedgerFormat.textProblem("KEY", "-----BEGIN OPENSSH"),
                   "That is the key itself. Paste where the key file is instead, like ~/.ssh/id_ed25519.", "key file")
        var row = LedgerRow()
        row.state.tone = "paused"
        row.state.says = "Paused: automatic importing is off"
        row.inUse = true
        row.detail.runs = "Every couple of minutes"
        try expect(LedgerWords.runs(row), "", "runs while paused")
        row.state.tone = "off"
        row.state.says = ""
        row.inUse = false
        try expect(LedgerWords.runs(row), "Once it is on: every couple of minutes", "runs while off")
        try expect(LedgerWords.now(row), "Off. Once it is on: every couple of minutes.", "now while off")
        try expect(DataExport.defaultName(.csv, now: Date(timeIntervalSince1970: 1_760_000_000)),
                   "network-crm-export-2025-10-09.csv", "export name")
        try expect(EngineClient.fileName(#"attachment; filename="network-crm-export-2026-10-16.csv""#) ?? "",
                   "network-crm-export-2026-10-16.csv", "download name")
        var quiet = LedgerAnswer()
        let store = DataStore()
        try expect(String(Int(store.importTimeout)), "315", "import wait, no answer")
        quiet.importTimeoutSec = 1800
        try expect(String(Int(DataStore.importWait(quiet))), "1815", "import wait, 30 min ceiling")
        quiet.importTimeoutSec = 99999
        try expect(String(Int(DataStore.importWait(quiet))), "3615", "import wait, clamped")
        let blocks = LedgerWords.helpBlocks("Do this:\n\n1. Open it\n2. Copy it\n\n- one\n- two")
        guard blocks == [.paragraph("Do this:"), .steps(["Open it", "Copy it"]), .bullets(["one", "two"])] else {
            throw selfFail("help blocks")
        }
        return n + 1
    }
}
