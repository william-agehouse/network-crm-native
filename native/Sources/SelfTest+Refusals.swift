import Foundation

// --selftest and --selftest-writes checks for the engine changes the native app had to
// answer: emptying a filled field (NC-333), Add on a list bringing someone back (NC-336),
// and the engine saying no in words, with a status, where it used to say "ok" or fail with
// a server error (NC-337 unknown people, NC-338 dates, NC-339 missing notes, NC-340 bad
// page sizes and unknown people on a person's conversations).
//
// Reads (`refusalReads`) need no engine: they check what this app sends and how it reads
// an answer. Writes (`refusalWrites`, scratch engine on 8768 only) send the bad request on
// purpose and check the engine refuses it with the status and a sentence. An engine from
// before these changes fails the writes checks: that is the point of them. Everything
// written to the scratch engine is made up.

extension SelfTest {
    // MARK: Reads: what this app sends and how it reads an answer

    static func refusalReads(_ t: inout Tally) async {
        await t.askCheck("refusals: emptying a filled field sends clear:true (NC-333)") {
            func body(_ value: String, old: String) -> [String: JSONValue] {
                PersonStore.fieldBody("company", value, old: old)
            }
            let filled = "Analytical Engines"
            guard body("", old: filled)["clear"] == JSONValue.bool(true) else {
                throw AskListsCheck.failed("an emptied field did not carry clear")
            }
            guard body("   ", old: filled)["clear"] == JSONValue.bool(true),
                body("   ", old: filled)["value"] == JSONValue.string("")
            else { throw AskListsCheck.failed("spaces only were not treated as empty") }
            guard !body("Difference Engines", old: filled).keys.contains("clear") else {
                throw AskListsCheck.failed("a changed value carried clear")
            }
            guard !body("", old: "").keys.contains("clear"), !body("", old: "  ").keys.contains("clear") else {
                throw AskListsCheck.failed("clear was sent for a field with nothing in it")
            }
            guard body("  Ada  ", old: "")["value"] == JSONValue.string("Ada") else {
                throw AskListsCheck.failed("the value was not trimmed")
            }
            return 5
        } count: { "\($0) cases" }

        await t.askCheck("refusals: an answer of \"unchanged\" is never read as a save (NC-333)") {
            func answer(_ json: String) throws -> WriteResult {
                try JSONDecoder().decode(WriteResult.self, from: Data(json.utf8))
            }
            guard let sentence = PersonStore.fieldAnswerProblem(try answer(#"{"ok":true,"unchanged":true}"#)), !sentence.isEmpty
            else { throw AskListsCheck.failed("unchanged was read as saved") }
            guard PersonStore.fieldAnswerProblem(try answer(#"{"ok":true}"#)) == nil,
                PersonStore.fieldAnswerProblem(try answer(#"{"ok":true,"unchanged":false}"#)) == nil
            else { throw AskListsCheck.failed("a real save was read as refused") }
            return 3
        } count: { "\($0) answers read right" }

        await t.askCheck("refusals: only a real YYYY-MM-DD day is ever sent (NC-338)") {
            let real = ["2026-12-01", "1990-12-10", "2028-02-29"]
            let notReal = [
                "tomorrow", "soon", "", "2026-02-30", "2026-13-01", "2026-1-5", "26-12-01", "2026/12/01", "2026-12-01T10:00",
            ]
            for s in real where !ListsDates.isValidDay(s, years: 1000...9999) {
                throw AskListsCheck.failed("a real day was refused: \(s)")
            }
            for s in notReal where ListsDates.isValidDay(s, years: 1000...9999) {
                throw AskListsCheck.failed("a day that is not real was accepted: \(s.isEmpty ? "(empty)" : s)")
            }
            guard !ListsDates.isValidDay("0999-01-01", years: 1000...9999), ListsDates.isValidDay("1899-01-01", years: 1000...9999)
            else { throw AskListsCheck.failed("the year range is wrong") }
            return real.count + notReal.count
        } count: { "\($0) dates judged as the engine now does" }

        await t.askCheck("refusals: a person's date that is not a day is refused before the engine is asked (NC-338)") {
            // A client aimed at a port nothing listens on: a request would come back as
            // "not answering", so getting the page's own sentence proves none was sent.
            let store = PersonStore(id: 1, client: EngineClient(port: 1))
            do {
                try await store.addDate(kind: "birthday", onDate: "tomorrow", label: "", yearKnown: true)
            } catch let problem as PersonProblem {
                guard !problem.text.isEmpty else { throw AskListsCheck.failed("the sentence was empty") }
                return problem.text.count
            }
            throw AskListsCheck.failed("a date that is not a day was sent")
        } count: { "refused with a \($0)-character sentence, nothing sent" }

        await t.askCheck("refusals: the engine's sentence reaches the screen word for word, never blank (NC-337 to NC-340)") {
            let cases: [(Int, String)] = [
                (400, "Some of these people are not in your network."),
                (404, "That note no longer exists."),
                (400, "limit must be a whole number."),
                (404, ""),
            ]
            for (status, message) in cases {
                let said = PersonStore.say(EngineError.engine(status: status, message: message))
                guard !said.isEmpty else { throw AskListsCheck.failed("a \(status) was shown as nothing") }
                guard message.isEmpty || said == message else { throw AskListsCheck.failed("a \(status) sentence was changed") }
            }
            return cases.count
        } count: { "\($0) refusals read as sentences" }

        // NC-363: the same seven helpers every screen reads an error through. A failure the
        // engine did not word for people (a crash, a stopped engine, an answer this app could
        // not read) must reach the screen as a plain sentence with no route, status, port or
        // exception text in it; the engine's own refusal must reach it untouched.
        await t.askCheck("refusals: a failure reaches every screen as a plain sentence, a refusal word for word (NC-363)") {
            let hidden: [EngineError] = [
                .unreachable(port: 8765),
                .timedOut(seconds: 30),
                .engine(status: 500, message: "KeyError: 'name' in /api/contacts"),
                .engine(status: 502, message: "Draft failed: Error code: 401 - invalid key"),
                .unexpectedAnswer(status: 502),
                .decoding(route: "/api/contacts", path: "items[0].name", problem: "key not found"),
            ]
            let developerText = [
                "/api/", "HTTP", "KeyError", "Error code", "Internal", "items[0]", "8765", "500", "502", "key not found", "port",
            ]
            let readers: [(String, (Error) -> String)] = [
                ("the person page", { PersonStore.say($0) }),
                ("the Data and Settings pages", { LedgerSettings.sentence($0) }),
                ("the setup sheets", { SetupWords.sentence($0) }),
                ("onboarding", { OnboardingStore.sentence($0) }),
                ("Lists", { ListsStore.sentence($0) }),
                ("Ask", { AskModel.words($0) }),
                ("everything else", { EngineError.sentence(for: $0) }),
            ]
            for (place, say) in readers {
                for error in hidden {
                    let said = say(error)
                    guard !said.isEmpty else { throw AskListsCheck.failed("\(place) showed nothing for \(error.technicalDetail)") }
                    for word in developerText where said.localizedCaseInsensitiveContains(word) {
                        throw AskListsCheck.failed("\(place) showed “\(said)” for \(error.technicalDetail)")
                    }
                }
                let refusal = EngineError.engine(status: 400, message: "Some of these people are not in your network.")
                guard say(refusal) == "Some of these people are not in your network." else {
                    throw AskListsCheck.failed("\(place) changed the engine's own sentence to “\(say(refusal))”")
                }
            }
            // A 502's own plain sentence about something outside the engine still gets through.
            let outside = "Could not reach calendar.example within 20s (timed out). Nothing was downloaded."
            guard EngineError.sentence(for: EngineError.engine(status: 502, message: outside)) == outside else {
                throw AskListsCheck.failed("a 502's own sentence was replaced")
            }
            // A person's number of seconds is said, not hidden: the wait is a fact.
            guard EngineError.sentence(for: EngineError.timedOut(seconds: 30)).contains("30 seconds") else {
                throw AskListsCheck.failed("the wait was not said")
            }
            return readers.count * (hidden.count + 1) + 2
        } count: { "\($0) sentences read, none with a route, a code or a crash in it" }
    }

    // MARK: Writes: the engine says no, in words (scratch engine only)

    static func refusalWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        /// A person id no engine has.
        let nobody = 999_999_999
        func fail(_ s: String) -> EngineError { .engine(status: 0, message: s) }
        /// The status and sentence of the refusal `op` provokes. nil when nothing was refused.
        func refusal(_ op: () async throws -> Void) async -> (status: Int, sentence: String)? {
            do {
                try await op()
                return nil
            } catch let e as EngineError {
                return (e.status ?? 0, e.errorDescription ?? "")
            } catch {
                return (0, "")
            }
        }
        func expect(_ got: (status: Int, sentence: String)?, _ want: Int, _ what: String) throws {
            guard let got else { throw fail("\(what): nothing was refused") }
            guard got.status == want else { throw fail("\(what): refused with \(got.status), expected \(want)") }
            guard !got.sentence.isEmpty else { throw fail("\(what): the refusal had no sentence") }
        }

        let adaName = "Selftest Refusal Ada \(stamp)"
        guard
            let ada = try? await c.post("/api/contacts", body: ["name": .string(adaName)] as [String: JSONValue], as: CreatedID.self).id
        else {
            t.fail("refusals: a made-up person", "the scratch engine would not make one")
            return
        }

        // NC-337: an unknown person on a list is a 400 naming them, from both routes.
        await t.check("refusals: an unknown person on a new list is a 400 with a sentence (NC-337)") {
            let got = await refusal {
                _ = try await c.post(
                    "/api/lists", body: ["name": .string("Selftest refusal \(stamp)"), "contact_ids": [.int(nobody)]] as [String: JSONValue],
                    as: CreatedID.self)
            }
            try expect(got, 400, "create")
            return 1
        } count: { _ in "refused, nothing created" }

        let listName = "Selftest refusal list \(stamp)"
        let made = await t.check("refusals: a list to test with") {
            try await c.post(
                "/api/lists", body: ["name": .string(listName), "contact_ids": [.int(ada)]] as [String: JSONValue], as: CreatedID.self)
        } count: { "added \($0.added ?? 0)" }
        if let made {
            let l = made.id
            await t.check("refusals: an unknown person added to a list is a 400 with a sentence (NC-337)") {
                let got = await refusal {
                    _ = try await c.post(
                        "/api/list/\(l)/add", body: ["contact_ids": [.int(nobody)]] as [String: JSONValue], as: WriteResult.self)
                }
                try expect(got, 400, "add")
                // The page's own wording over the same refusal.
                var list = CRMList()
                list.id = l
                list.name = listName
                let line = await PeopleExtrasStore().addToList([nobody], list: list, newName: nil, client: c)
                let opening = "Nobody was added to “\(listName)”. "
                guard !line.ok, line.text.hasPrefix(opening), line.text.count > opening.count else {
                    throw fail("the screen's line was “\(line.text)”")
                }
                return 1
            } count: { _ in "refused, and the screen says why" }

            // NC-338: an event date that is not a day.
            await t.check("refusals: a list's event date that is not a day is a 400 (NC-338)") {
                let got = await refusal {
                    _ = try await c.post("/api/list/\(l)/meta", body: ["event_date": "soon"] as [String: JSONValue], as: WriteResult.self)
                }
                try expect(got, 400, "event date")
                _ = try await c.post("/api/list/\(l)/meta", body: ["event_date": "2026-12-01"] as [String: JSONValue], as: WriteResult.self)
                _ = try await c.post("/api/list/\(l)/meta", body: ["event_date": ""] as [String: JSONValue], as: WriteResult.self)
                return 1
            } count: { _ in "“soon” refused; a real day and a clear still work" }

            // NC-336: the engine's own Add brings a removed member back and counts them.
            await t.check("refusals: Add brings a removed member back and counts them (NC-336)") {
                _ = try await c.post(
                    "/api/list/\(l)/member", body: ["contact_id": .int(ada), "remove": true] as [String: JSONValue], as: WriteResult.self)
                let r = try await c.post(
                    "/api/list/\(l)/add", body: ["contact_ids": [.int(ada)]] as [String: JSONValue], as: WriteResult.self)
                let members = try await c.get("/api/list/\(l)", as: ListDetail.self).members.map(\.id)
                guard r.added == 1, members.contains(ada) else { throw fail("added \(r.added ?? -1), member \(members.contains(ada))") }
                return 1
            } count: { _ in "added 1, and they are on the list" }
            _ = try? await c.delete("/api/list/\(l)", as: WriteResult.self)
        }

        // NC-338: a person's date that is not a day.
        await t.check("refusals: a date that is not a day is a 400 (NC-338)") {
            let got = await refusal {
                _ = try await c.post(
                    "/api/contact/\(ada)/dates", body: ["kind": "birthday", "on_date": "tomorrow"] as [String: JSONValue],
                    as: WriteResult.self)
            }
            try expect(got, 400, "date")
            return 1
        } count: { _ in "“tomorrow” refused" }

        // NC-339: a note that is not there is a 404, and the page says so and refreshes.
        await t.check("refusals: editing or deleting a missing note is a 404 with a sentence (NC-339)") {
            let edit = await refusal {
                _ = try await c.put("/api/note/\(nobody)", body: ["body": "x"] as [String: JSONValue], as: WriteResult.self)
            }
            try expect(edit, 404, "edit")
            let delete = await refusal { _ = try await c.delete("/api/note/\(nobody)", as: WriteResult.self) }
            try expect(delete, 404, "delete")
            let store = PersonStore(id: ada, client: c)
            await store.load()
            var saved = false
            do {
                try await store.editNote(nobody, "x")
                saved = true
            } catch let e as EngineError {
                guard e.status == 404 else { throw fail("the page got \(e.status ?? 0) for a missing note") }
            }
            guard !saved else { throw fail("the page said a missing note was saved") }
            await store.deleteNote(nobody)
            guard let said = store.flashText, !said.isEmpty, said != "Note deleted" else {
                throw fail("the page did not say the note was gone")
            }
            return 1
        } count: { _ in "both refused, and the page names the problem" }

        // NC-340: episodes and history say no to a bad size and an unknown person.
        await t.check("refusals: a bad page size is a 400 on episodes (NC-340)") {
            let got = await refusal {
                _ = try await c.get("/api/contact/\(ada)/episodes", query: ["limit": "abc"], as: JSONValue.self)
            }
            try expect(got, 400, "episodes limit")
            return 1
        } count: { _ in "“abc” refused" }
        await t.check("refusals: an unknown person is a 404 on episodes and on history (NC-340)") {
            let episodes = await refusal { _ = try await c.get("/api/contact/\(nobody)/episodes", as: JSONValue.self) }
            try expect(episodes, 404, "episodes")
            let history = await refusal { _ = try await c.get("/api/contact/\(nobody)/history", as: JSONValue.self) }
            try expect(history, 404, "history")
            return 2
        } count: { "\($0) refused" }

        // Tidy up: the made-up person, then the name deleting put on the skip list.
        _ = try? await c.post("/api/contact/\(ada)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
        _ = try? await c.post("/api/contact/\(ada)/purge", as: WriteResult.self)
        _ = try? await c.post("/api/exclude-list/remove", body: ["name": JSONValue.string(adaName)], as: WriteResult.self)
    }
}
