import AppKit
import Foundation
import SwiftUI

// --selftest checks for joining the engine and for what happens when it goes away
// (Engine.swift, RootView's banners), plus two Ask transcript checks that need an engine in
// a particular state.
//
// On any engine: the fingerprint and build stamp the app hands its engine, and the private
// connection's own request writing, answer reading and file path (NC-330). On a real-data port: leaving
// Ask while a chat loads does not leave it loading for good (reads only).
//
// On the stand-in (8869, native/tools/standin.py, launch config network-crm-native-standin):
// one missed status check is not trouble, two are, and the banner clears by itself when
// the engine answers again; the database-moved-aside sentence arrives and is dismissed; and
// a question still being answered shows once, not twice, when its chat is loaded again.

extension SelfTest {
    static func engineReads(_ t: inout Tally) async {
        await environmentFallbackCheck(&t)
        await engineIdentityChecks(&t)
        if t.client.port == askStandInPort {
            await engineTroubleCheck(&t)
            await engineRecoveryCheck(&t)
            await askPendingOnceCheck(&t)
            await askBusyDraftCheck(&t)
        } else {
            await askCancelledLoadCheck(&t)
        }
    }

    // MARK: A view drawn without the app model (NC-359)

    /// Draws a view that reads the app model, with no model handed to it, as SwiftUI did
    /// when the sidebar collapsed on Lists. `@Environment(AppModel.self)` stopped the app
    /// there; `@AppEnvironment` must hand over the window's own model instead.
    private static func environmentFallbackCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("a view drawn without the app model still gets it (NC-359)") {
            let app = AppModel(engine: EngineController(address: c.address, launch: nil))
            let seen = EnvironmentProbe.Seen()
            let hosting = NSHostingView(rootView: EnvironmentProbe(seen: seen))
            hosting.frame = NSRect(x: 0, y: 0, width: 40, height: 40)
            hosting.layoutSubtreeIfNeeded()
            _ = hosting.fittingSize
            try? await Task.sleep(for: .milliseconds(200))
            hosting.layoutSubtreeIfNeeded()
            guard let got = seen.model else { throw AskListsCheck.failed("the probe was never drawn") }
            guard got === app else { throw AskListsCheck.failed("it got another model") }
            return 1
        } count: { _ in "drew, and got the window's model" }
    }

    // MARK: The app's own engine and its private connection (NC-330)

    private static func engineIdentityChecks(_ t: inout Tally) async {
        await t.askCheck("engine fingerprint = the one older engines report") {
            // printf '%s\n%s\n' "/a b/app" "/x é/data" | shasum -a 256 | cut -c1-16
            let got = EngineController.fingerprint(appHome: "/a b/app", dataFolder: "/x é/data")
            guard got == "a8de66d0409cf1be" else { throw AskListsCheck.failed("got \(got)") }
            return got.count
        } count: { "\($0) hex characters, matches shasum" }

        await t.askCheck("engine build stamp kept as appify.sh writes it") {
            let dir = FileManager.default.temporaryDirectory.appending(path: "nc-native-buildid-\(getpid())")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: dir) }
            try Data("2026-10-16T09:12:03Z abc1234\n".utf8).write(to: dir.appending(path: "build_id.txt"))
            // tr -cd 'A-Za-z0-9-'
            let got = EngineController.buildID(resources: dir)
            guard got == "2026-10-16T091203Zabc1234" else { throw AskListsCheck.failed("got \(got)") }
            return got.count
        } count: { "\($0) characters, same as tr -cd" }

        await t.askCheck("a request on the private connection is the one the engine expects") {
            var req = URLRequest(url: URL(string: "http://localhost/api/contacts?q=a%2Bb&sort=closeness")!)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = Data(#"{"x":1}"#.utf8)
            let wire = String(decoding: try PrivateConnection.request(req), as: UTF8.self)
            let want = "POST /api/contacts?q=a%2Bb&sort=closeness HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            guard wire.hasPrefix(want) else { throw AskListsCheck.failed("began \(wire.prefix(80))") }
            guard wire.contains("Content-Length: 7\r\n"), wire.hasSuffix("\r\n\r\n{\"x\":1}") else {
                throw AskListsCheck.failed("body or its length is wrong")
            }
            var bad = URLRequest(url: URL(string: "http://localhost/api/status")!)
            bad.setValue("x\r\nX-Injected: 1", forHTTPHeaderField: "X-Test")
            // Refused here, or dropped by URLRequest itself: either way no second header.
            if let sent = try? PrivateConnection.request(bad),
               String(decoding: sent, as: UTF8.self).contains("\r\nX-Injected") {
                throw AskListsCheck.failed("a header with a line break was sent")
            }
            return 3
        } count: { "\($0) cases: path and query kept, body sized, no header injection" }

        await t.askCheck("an answer on the private connection is read whole") {
            let sized = try PrivateConnection.parse(Data("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}trailing".utf8))
            guard sized.status == 200, sized.body == Data("{}".utf8) else { throw AskListsCheck.failed("sized body") }
            let chunked = try PrivateConnection.parse(Data("HTTP/1.1 404 Not Found\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n".utf8))
            guard chunked.status == 404, chunked.body == Data("abcde".utf8) else { throw AskListsCheck.failed("chunked body") }
            let open = try PrivateConnection.parse(Data("HTTP/1.0 200 OK\r\n\r\nto the end".utf8))
            guard open.body == Data("to the end".utf8) else { throw AskListsCheck.failed("body to the end") }
            guard (try? PrivateConnection.parse(Data("HTTP/1.0 200 OK\r\nContent-Length: 9\r\n\r\nshort".utf8))) == nil else {
                throw AskListsCheck.failed("a cut-off answer was accepted")
            }
            return 4
        } count: { "\($0) cases: sized, chunked, to the end, cut off" }

        await t.askCheck("an older version's engine is recognised by its command line only") {
            let mine = EngineLaunch.commandLine(of: getpid())
            guard mine.contains("--selftest") else { throw AskListsCheck.failed("read “\(mine.prefix(60))”") }
            guard EngineLaunch.commandLine(of: 999_999).isEmpty else { throw AskListsCheck.failed("a missing process had one") }
            return 2
        } count: { "\($0) cases: this process, a missing one" }

        await t.askCheck("the private connection's file fits, or moves to the account's own temporary folder") {
            let home = FileManager.default.homeDirectoryForCurrentUser
            func launch(_ support: URL) -> EngineLaunch {
                EngineLaunch(support: support, logs: home, resources: home, codeFolder: nil, python: nil,
                             setsUpOptionalParts: false)
            }
            let short = launch(home.appending(path: "Library/Application Support/NetworkCRM")).socketPath
            guard short.hasSuffix("NetworkCRM/run/engine.sock") else {
                throw AskListsCheck.failed("got \(short)")
            }
            let long = launch(home.appending(path: String(repeating: "x", count: 120))).socketPath
            guard long.utf8.count <= PrivateConnection.longestPath, long.hasPrefix(NSTemporaryDirectory()) else {
                throw AskListsCheck.failed("a long path stayed: \(long.utf8.count) bytes")
            }
            return short.utf8.count
        } count: { "the app's is \($0) bytes of 103" }

        await t.askCheck("Data and Settings show the engine's own 502/504 sentences, not a 500's") {
            let stale = "Could not reach calendar.example within 20s (timed out). Nothing was downloaded."
            let shown = LedgerSettings.sentence(EngineError.engine(status: 502, message: stale))
            guard shown == stale else { throw AskListsCheck.failed("502 shown as “\(shown)”") }
            let crash = LedgerSettings.sentence(EngineError.engine(status: 500, message: "KeyError: 'x'"))
            guard !crash.contains("KeyError") else { throw AskListsCheck.failed("a 500's exception text was shown") }
            return 2
        } count: { "\($0) cases: 502 kept word for word, 500 replaced" }
    }

    // MARK: The engine going away and coming back (stand-in)

    private static func standInApp(_ c: EngineClient) async throws -> AppModel {
        _ = try? await c.get("/stub/reset", as: WriteResult.self)
        let app = AppModel(engine: EngineController(address: c.address, launch: nil))
        await app.engine.connect()
        guard app.engine.isReady else { throw AskListsCheck.failed("the stand-in on 8869 is not running") }
        return app
    }

    private static func engineTroubleCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("engine misses checks, screens stay, banner clears by itself (stand-in)") {
            let app = try await standInApp(c)
            _ = try await c.get("/stub/pause", query: ["seconds": "4"], as: WriteResult.self)
            await app.engine.refreshStatus()
            guard app.engine.trouble == nil, app.engine.isReady else {
                throw AskListsCheck.failed("one missed check already counted as trouble")
            }
            await app.engine.refreshStatus()
            guard app.engine.trouble != nil else { throw AskListsCheck.failed("two missed checks set no banner") }
            guard app.engine.isReady else { throw AskListsCheck.failed("the screens were taken down") }
            let started = Date()
            while app.engine.trouble != nil && Date().timeIntervalSince(started) < 12 {
                try? await Task.sleep(for: .milliseconds(500))
                await app.engine.refreshStatus()
            }
            guard app.engine.trouble == nil, app.engine.isReady else {
                throw AskListsCheck.failed("the banner stayed after the engine answered again")
            }
            return Date().timeIntervalSince(started)
        } count: { "banner after 2 misses, gone \(String(format: "%.1f", $0)) s after the pause began to end" }
    }

    private static func engineRecoveryCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("database-moved-aside sentence shown, then dismissed (stand-in)") {
            let app = try await standInApp(c)
            let sentence = "Stand-in: crm.db could not be read and was moved aside."
            _ = try await c.get("/stub/recovery", query: ["text": sentence], as: WriteResult.self)
            await app.refreshCounts()
            guard app.engine.status?.dbRecovery == sentence else { throw AskListsCheck.failed("the sentence did not arrive") }
            // A GET of the dismiss address must change nothing (NC-342): the stand-in, like
            // the engine, only forgets the notice on a POST.
            _ = try await app.client.get("/api/db-recovery-notice/dismiss", as: JSONValue.self)
            await app.refreshCounts()
            guard app.engine.status?.dbRecovery == sentence else { throw AskListsCheck.failed("a GET cleared the notice") }
            // What "I've read this" runs: the same function, so this proves the route the
            // button uses is the POST.
            if let problem = await app.dismissRecoveryNotice() { throw AskListsCheck.failed(problem) }
            guard app.engine.status?.dbRecovery.isEmpty == true else { throw AskListsCheck.failed("still shown after dismissing") }
            return sentence.count
        } count: { "\($0)-character sentence arrived in status, kept through a GET, gone after the POST" }
    }

    // MARK: Ask transcripts

    /// Go back to a chat whose question is still being answered: the question is in the
    /// history already (the engine saves it first), so it must show once.
    private static func askPendingOnceCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("a question being answered shows once when its chat reloads (stand-in)") {
            let app = try await standInApp(c)
            let model = AskModel.shared(for: app)
            await model.appeared()
            let q = "Stand-in: count me once"
            model.send(q, fromComposer: true)
            guard model.pending != nil else { throw AskListsCheck.failed("the question was not sent") }
            try? await Task.sleep(for: .seconds(1))
            await model.loadTranscript()
            let shown = model.turns.filter { $0.role == .user && $0.content == q }.count
            model.stop()
            let wait = Date().addingTimeInterval(30)
            while model.pending != nil && Date() < wait { try? await Task.sleep(for: .milliseconds(100)) }
            guard shown == 1 else { throw AskListsCheck.failed("shown \(shown) times") }
            return shown
        } count: { "shown \($0) time" }
    }

    /// ⌘K Ask while another answer is still running: the question cannot be sent, so it
    /// must wait in the composer instead of disappearing.
    private static func askBusyDraftCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("⌘K Ask while an answer runs keeps the question (stand-in)") {
            let app = try await standInApp(c)
            let model = AskModel.shared(for: app)
            await model.appeared()
            model.send("Stand-in: the first, slow question", fromComposer: true)
            guard model.pending != nil else { throw AskListsCheck.failed("the first question was not sent") }
            let second = "Stand-in: typed in ⌘K meanwhile"
            await CommandBarRunner.askNow(second, app: app).value
            let kept = app.askDraft == second
            let sentTwice = model.turns.filter { $0.role == .user && $0.content == second }.count
            model.stop()
            let wait = Date().addingTimeInterval(30)
            while model.pending != nil && Date() < wait { try? await Task.sleep(for: .milliseconds(100)) }
            guard sentTwice == 0 else { throw AskListsCheck.failed("the second question was sent beside the first") }
            guard kept else { throw AskListsCheck.failed("the second question vanished") }
            return second.count
        } count: { "the \($0)-character question waits in the composer" }
    }

    /// Leave Ask while a chat loads (the load is cancelled), then come back: the chat loads,
    /// instead of keeping the cancelled load's spinner. Reads only.
    private static func askCancelledLoadCheck(_ t: inout Tally) async {
        let c = t.client
        await t.askCheck("leaving Ask mid-load, then coming back, loads the chat") {
            let chats = try await c.get("/api/chats", as: ChatsResponse.self).chats
            guard let other = chats.dropFirst().first else { return -1 }
            let app = AppModel(engine: EngineController(address: c.address, launch: nil))
            await app.engine.connect()
            let model = AskModel.shared(for: app)
            await model.appeared()
            app.selectedChatID = other.id
            // What going to another screen does to AskScreen's load: cancels it.
            let load = Task { await model.loadTranscript() }
            load.cancel()
            await load.value
            await model.appeared()
            guard model.transcriptState == .loaded else { throw AskListsCheck.failed("still loading after coming back") }
            return model.turns.count
        } count: { $0 < 0 ? "skipped: fewer than two chats" : "loaded on return, \($0) messages" }
    }
}

// MARK: - Writes (the empty scratch engine only)

extension SelfTest {
    static func engineWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))

        // Rename and Delete as the dialogs now call them: the dialog has already cleared
        // `renaming` / `deleting` when the button's Task runs.
        let chatTitle = "Selftest dialog chat \(stamp)"
        let made = await t.check("ask: make a chat to rename and delete") {
            try await c.post("/api/chats", body: ["title": .string(chatTitle)] as [String: JSONValue], as: ChatCreated.self).conversation.id
        } count: { _ in "made" }
        if let made {
            let app = AppModel(engine: EngineController(address: c.address, launch: nil))
            await app.engine.connect()
            let model = AskModel.shared(for: app)
            await model.loadChats()
            await t.askCheck("ask: Rename's Save works after the dialog closed") {
                guard let chat = model.chats.first(where: { $0.id == made }) else { throw AskListsCheck.failed("chat not listed") }
                model.beginRename(chat)
                model.renaming = nil
                let renamed = "Selftest renamed \(stamp)"
                await model.commitRename(chat, to: renamed)
                guard model.chats.first(where: { $0.id == made })?.title == renamed else { throw AskListsCheck.failed("not renamed") }
                return renamed.count
            } count: { "renamed (\($0) characters)" }
            await t.askCheck("ask: Delete Chat works after the dialog closed") {
                guard let chat = model.chats.first(where: { $0.id == made }) else { throw AskListsCheck.failed("chat not listed") }
                model.beginDelete(chat)
                model.deleting = nil
                await model.commitDelete(chat)
                let left = try await c.get("/api/chats", as: ChatsResponse.self).chats
                guard !left.contains(where: { $0.id == made }) else { throw AskListsCheck.failed("still listed") }
                return left.count
            } count: { "deleted; \($0) chats left" }
        }

        // A person's page: adding them to a list they are already in keeps their invite
        // status; one they were removed from brings them back; Delete says so app-wide.
        let name = "Selftest Lin \(stamp)"
        let madePerson = await t.check("person: a made-up person") {
            try await PeopleAddPersonSheet.create(name: name, company: "", client: c)
        } count: { _ in "created" }
        guard let person = madePerson else { return }
        let listName = "Selftest status list \(stamp)"
        let listID = await t.check("person: a list they are Going on") {
            let made = try await c.post(
                "/api/lists", body: ["name": .string(listName), "contact_ids": .array([.int(person)])] as [String: JSONValue],
                as: CreatedID.self)
            _ = try await c.post(
                "/api/list/\(made.id)/member", body: ["contact_id": .int(person), "status": "attending"] as [String: JSONValue],
                as: WriteResult.self)
            return made.id
        } count: { _ in "made, status attending" }
        if let listID {
            let store = PersonStore(id: person, client: c)
            func status() async throws -> String? {
                try await c.get("/api/list/\(listID)", as: ListDetail.self).members.first { $0.id == person }.map { $0.listStatus ?? "" }
            }
            func list() async throws -> CRMList {
                guard let l = try await c.get("/api/lists", as: ListsResponse.self).lists.first(where: { $0.id == listID }) else {
                    throw AskListsCheck.failed("list not listed")
                }
                return l
            }
            await t.askCheck("person: Add to list on a list they are in keeps their status") {
                await store.addToList(try await list())
                guard try await status() == "attending" else { throw AskListsCheck.failed("status reset") }
                guard store.flashText?.hasPrefix("Already in") == true else { throw AskListsCheck.failed("said it added them") }
                return 1
            } count: { _ in "still attending, said “Already in”" }
            await t.askCheck("person: Add to list brings back a removed member") {
                _ = try await c.post(
                    "/api/list/\(listID)/member", body: ["contact_id": .int(person), "remove": true] as [String: JSONValue],
                    as: WriteResult.self)
                await store.addToList(try await list())
                guard try await status() != nil else { throw AskListsCheck.failed("still out of the list") }
                guard store.flashText?.hasPrefix("Added to") == true else { throw AskListsCheck.failed("did not say Added") }
                return 1
            } count: { _ in "back in, said “Added to”" }
            _ = try? await c.delete("/api/list/\(listID)", as: WriteResult.self)
        }
        await t.askCheck("person: Delete says so on the app-wide line") {
            let store = PersonStore(id: person, client: c)
            let before = DataSettingsBus.shared.announceTick
            guard await store.setDeleted(true) else { throw AskListsCheck.failed("not deleted") }
            guard DataSettingsBus.shared.announceTick == before + 1,
                  let line = DataSettingsBus.shared.announcement, !line.isEmpty
            else { throw AskListsCheck.failed("nothing announced") }
            return line.count
        } count: { "announced a \($0)-character line" }
        // Tidy: erase for good and take the name off the skip list deleting put it on.
        _ = try? await c.post("/api/contact/\(person)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
        _ = try? await c.post("/api/contact/\(person)/purge", as: WriteResult.self)
        _ = try? await c.post("/api/exclude-list/remove", body: ["name": JSONValue.string(name)], as: WriteResult.self)
        t.note("engine writes", "tidied: 1 person erased, 1 list deleted")
    }
}

/// A view that only reads the app model, for `environmentFallbackCheck`.
private struct EnvironmentProbe: View {
    @MainActor final class Seen { var model: AppModel? }
    let seen: Seen
    @AppEnvironment private var app

    var body: some View {
        let _ = { seen.model = app }()
        Color.clear
    }
}
