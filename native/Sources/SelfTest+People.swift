import AppKit
import Foundation
import SwiftUI

// --selftest and --selftest-writes checks for the People screen and a person's page. Owned by that screen's files.
// Reads run against the real data (print counts only, never a value); writes run only
// against the empty scratch engine on 8768 (SelfTest.run refuses anything else).

/// A failed expectation, said the way Tally prints engine errors (the sentence shows only
/// under --selftest-writes, where every value is made up).
private func PeopleCheckFailed(what: String) -> EngineError { .engine(status: 0, message: what) }

extension SelfTest {
    static func peopleReads(_ t: inout Tally) async {
        let c = t.client

        // The routes exactly as the People screen sends them.
        await t.check("people: GET /api/exclude-list") {
            try await c.get("/api/exclude-list", as: ExcludeListResponse.self)
        } count: { r in "\(r.entries.count) names, \(r.entries.filter { !$0.inDB }.count) not in the contacts" }
        await t.check("people: GET /api/upcoming (the strip's own call)") {
            try await c.get("/api/upcoming", as: UpcomingResponse.self)
        } count: { r in
            let linked = r.shown.filter { $0.contactID != nil || $0.listID != nil }.count
            return "\(r.shown.count) shown (\(linked) open something), \(r.more) more, \(r.days)-day window"
        }

        // The store the screen uses, end to end.
        await t.check("people: PeopleExtrasStore loads") {
            let extras = PeopleExtrasStore()
            await extras.loadHidden(client: c)
            await extras.loadUpcoming(client: c)
            await extras.loadLists(client: c)
            if let e = extras.hiddenError { throw PeopleCheckFailed(what: e) }
            guard extras.hidden != nil, extras.listsLoaded else { throw PeopleCheckFailed(what: "nothing loaded") }
            return extras
        } count: { e in "\(e.hidden?.count ?? 0) hidden, \(e.upcoming?.shown.count ?? 0) coming up, \(e.lists.count) lists" }

        // Add Person's duplicate check, for a name nobody has.
        await t.check("people: add-person duplicate check (no match)") {
            let hit = await PeopleAddPersonSheet.existing(named: "Selftest Nobody Zqx \(Int(Date().timeIntervalSince1970))", client: c)
            if hit != nil { throw PeopleCheckFailed(what: "a made-up name matched someone") }
            return 0
        } count: { _ in "no match, as expected" }

        // A person's conversations, as the All and Conversations views read them.
        if let first = try? await c.get("/api/contacts", query: ["sort": "closeness", "limit": "1"], as: ContactsResponse.self)
            .contacts.first?.id
        {
            await t.check("people: PersonStore.loadEpisodes (first page, then the next)") {
                let store = PersonStore(id: first, client: c)
                await store.loadEpisodes(more: false)
                if let e = store.episodesError { throw PeopleCheckFailed(what: e) }
                let firstPage = store.episodes.count
                if store.episodesHasMore { await store.loadEpisodes(more: true) }
                if let e = store.episodesError { throw PeopleCheckFailed(what: e) }
                return (firstPage, store.episodes.count)
            } count: { "\($0.0) on the first page, \($0.1) after Earlier…" }
        }

        // Rules that need no engine.
        await t.check("people: ⌘K requests map onto the table") {
            let m = PeopleModel()
            m.apply(PeopleRequest(sort: "quiet", filter: "todo", location: "Selftest City"))
            guard m.sort == PeopleSort(column: .last, ascending: true), m.filter == .todo, m.location == "Selftest City"
            else { throw PeopleCheckFailed(what: "quiet/todo/location not applied") }
            m.apply(PeopleRequest(filter: "nonsense"))
            guard m.filter == .todo, m.location == "Selftest City" else { throw PeopleCheckFailed(what: "an unknown filter changed the view") }
            m.apply(PeopleRequest(sort: "name"))
            guard m.sort == PeopleSort(column: .name, ascending: true) else { throw PeopleCheckFailed(what: "name sort") }
            m.apply(PeopleRequest(sort: "recent", filter: "all", location: ""))
            guard m.sort.webKey == "recent", m.filter == .all, m.location.isEmpty else { throw PeopleCheckFailed(what: "reset") }
            return 4
        } count: { "\($0) requests applied as the web page does" }
        await t.check("people: a person's ⋯ menu offers what their state allows") {
            func titles(_ p: PersonDetail) -> [String] { PersonMenuItem.sections(for: p).flatMap { $0.items.map(\.title) } }
            var p = PersonDetail()
            let normal = titles(p)
            p.excluded = true
            let hidden = titles(p)
            p.deleted = true
            let deleted = titles(p)
            guard normal.contains("Merge with…"), normal.contains("Exclude…"), normal.contains("Delete…"),
                !normal.contains("Remove Photo"), hidden.contains("Unexclude…"), !hidden.contains("Exclude…"),
                deleted.contains("Restore"), deleted.contains("Delete Permanently…"), !deleted.contains("Delete…"),
                deleted.contains("Merge with…")
            else { throw PeopleCheckFailed(what: "menu items") }
            return normal.count
        } count: { "\($0) items for an ordinary person; hidden and deleted variants right" }
        await t.check("people: add-to-list wording") {
            let cases: [(Int, Int, String)] = [
                (0, 3, "“L” already holds everyone you selected"),
                (1, 1, "Added 1 person to “L”"),
                (2, 5, "Added 2 people to “L” · 3 already in the list"),
            ]
            for (added, sent, want) in cases where PeopleExtrasStore.addedSentence(added: added, sent: sent, list: "L") != want {
                throw PeopleCheckFailed(what: "wording for \(added) of \(sent)")
            }
            return cases.count
        } count: { "\($0) sentences as ui.js:391-395" }
        await peopleTunerFindsNewTableCheck(&t)
        await peopleOpenKeepsNamesCheck(&t)
    }

    /// PeopleTableTuner on plain AppKit tables, no engine (NC-360). Opening or closing a
    /// person swaps the table, and for a moment the old one, still laid out for the old
    /// width, covers the helper too: it must take the new one. And when its table leaves the
    /// window it must move to the one that replaced it. Both happen only now and then in the
    /// real screen (a race), so they are checked here, where they happen every time.
    private static func peopleTunerFindsNewTableCheck(_ t: inout Tally) async {
        await t.askCheck("people: the row-height helper moves to the table that replaced the old one (NC-360)") {
            let size = NSSize(width: 800, height: 600)
            let (window, _) = Snapshot.offscreenWindow(Color.clear, size: size)
            let content = NSView(frame: NSRect(origin: .zero, size: size))
            window.contentView = content
            window.orderFrontRegardless()
            defer {
                window.orderOut(nil)
                window.contentView = nil
                window.close()
            }
            @MainActor func table(width: CGFloat) -> (NSScrollView, NSTableView) {
                let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: width, height: size.height))
                let table = NSTableView(frame: NSRect(x: 0, y: 0, width: width, height: size.height))
                for i in 0..<3 { table.addTableColumn(NSTableColumn(identifier: .init("c\(i)"))) }
                table.usesAutomaticRowHeights = true
                scroll.documentView = table
                return (scroll, table)
            }
            // The old table still has the whole width; the new one the narrowed width; the
            // helper sits where the new one's rows show (right of a 210-point sidebar strip).
            let (oldScroll, old) = table(width: 800)
            let (newScroll, new) = table(width: 590)
            content.addSubview(oldScroll)
            content.addSubview(newScroll)
            let tuner = PeopleTableTunerView(frame: NSRect(x: 210, y: 0, width: 380, height: size.height))
            content.addSubview(tuner)
            tuner.apply()
            try? await Task.sleep(for: .milliseconds(200))
            guard !new.usesAutomaticRowHeights else { throw AskListsCheck.failed("it took the old table, not the new one") }
            guard old.usesAutomaticRowHeights else { throw AskListsCheck.failed("it changed the old table") }
            // The new table leaves the window and a third takes its place.
            oldScroll.removeFromSuperview()
            newScroll.removeFromSuperview()
            let (thirdScroll, third) = table(width: 590)
            content.addSubview(thirdScroll, positioned: .below, relativeTo: tuner)
            tuner.apply()
            try? await Task.sleep(for: .milliseconds(200))
            guard !third.usesAutomaticRowHeights else { throw AskListsCheck.failed("it kept the table that left the window") }
            return 3
        } count: { _ in "took the new table over the old one, then moved to its replacement" }
    }

    // MARK: Opening a person keeps the names in view (NC-360)

    /// Clicks a row the way a person does, with the table already on screen, in off-screen
    /// windows as wide as a laptop's and a desktop's, then reads how the table is scrolled;
    /// then closes the page and reads it again. Each time the names must start at the left
    /// edge and the open columns must fit what shows, without scrolling sideways. Reads only:
    /// opening a page is all GETs. Its failure sentences are only numbers, so they print on
    /// real data too (askCheck).
    private static func peopleOpenKeepsNamesCheck(_ t: inout Tally) async {
        let c = t.client
        guard let ids = try? await c.get(
            "/api/contacts", query: ["sort": "closeness", "limit": "12"], as: ContactsResponse.self
        ).contacts.map(\.id), ids.count >= 10 else { return }
        await t.askCheck("people: opening and closing a person keeps the names in view (NC-360)") {
            var said: [String] = []
            for width in [1000, 1440] {
                let app = AppModel(engine: EngineController(address: c.address, launch: nil))
                app.section = .people
                let (window, _) = Snapshot.offscreenWindow(
                    RootView().environment(app), size: NSSize(width: CGFloat(width), height: 700))
                window.orderFrontRegardless()
                defer {
                    app.stopPolling()
                    window.orderOut(nil)
                    window.contentView = nil
                    window.close()
                }
                let deadline = Date().addingTimeInterval(Snapshot.settleCap)
                while !app.isSettled && Date() < deadline { try? await Task.sleep(for: .milliseconds(100)) }
                try? await Task.sleep(for: .milliseconds(500))
                // A click partway down the list.
                app.selectedPersonID = ids[9]
                try? await Task.sleep(for: .milliseconds(1500))
                let opened = try peopleTableFits(in: window, "\(width) wide, page open")
                app.selectedPersonID = nil
                try? await Task.sleep(for: .milliseconds(1500))
                let closed = try peopleTableFits(in: window, "\(width) wide, page closed")
                said.append("\(width): \(opened.open) columns in \(opened.shows), then \(closed.open) in \(closed.shows)")
            }
            // Too narrow for the open columns to fit at all: the table may scroll sideways
            // then, but stepping to the next person must not slide it (the reveal scrolls up
            // and down only, PeopleTable.revealAnchor).
            let app = AppModel(engine: EngineController(address: c.address, launch: nil))
            app.section = .people
            let (window, _) = Snapshot.offscreenWindow(RootView().environment(app), size: NSSize(width: 820, height: 700))
            window.orderFrontRegardless()
            defer {
                app.stopPolling()
                window.orderOut(nil)
                window.contentView = nil
                window.close()
            }
            let deadline = Date().addingTimeInterval(Snapshot.settleCap)
            while !app.isSettled && Date() < deadline { try? await Task.sleep(for: .milliseconds(100)) }
            try? await Task.sleep(for: .milliseconds(500))
            app.selectedPersonID = ids[9]
            try? await Task.sleep(for: .milliseconds(1500))
            app.selectedPersonID = ids[10]
            try? await Task.sleep(for: .milliseconds(1000))
            let narrow = try peopleTableFits(in: window, "820 wide, next person", mayOverflow: true)
            said.append("820: next person, \(narrow.open) columns, still at the left edge")
            return said.joined(separator: "; ")
        } count: { "names at the left edge, columns fit (\($0))" }
    }

    /// How the People table sits in `window`: columns open and points showing, or why not.
    private static func peopleTableFits(in window: NSWindow, _ when: String, mayOverflow: Bool = false) throws -> (open: Int, shows: Int) {
        let root = window.contentView?.superview ?? window.contentView
        root?.layoutSubtreeIfNeeded()
        guard let root, let table = peopleTable(in: root), let clip = table.enclosingScrollView?.contentView else {
            throw AskListsCheck.failed("\(when): the People table was never drawn")
        }
        // At rest the clip view sits at minus its left inset: that strip runs under the
        // see-through sidebar, and only the rest of the table shows.
        let shows = clip.bounds.width - clip.contentInsets.left - clip.contentInsets.right
        let sideways = clip.bounds.origin.x + clip.contentInsets.left
        let tooWide = table.frame.width - shows
        let open = table.tableColumns.filter { !$0.isHidden }.count
        if abs(sideways) > 0.5 { throw AskListsCheck.failed("\(when): scrolled \(Int(sideways)) points sideways, names cut off") }
        if tooWide > 0.5 && !mayOverflow {
            throw AskListsCheck.failed("\(when): \(open) open columns are \(Int(tooWide)) points wider than the \(Int(shows)) that show")
        }
        // NC-344's fixed row height must reach every table, including the one made for the page.
        if table.usesAutomaticRowHeights { throw AskListsCheck.failed("\(when): the table measures every row again (NC-344)") }
        return (open, Int(shows))
    }

    /// The People table: the one with several columns (the sidebar's list has one).
    private static func peopleTable(in view: NSView) -> NSTableView? {
        if let t = view as? NSTableView, t.tableColumns.count >= 3 { return t }
        for sub in view.subviews {
            if let t = peopleTable(in: sub) { return t }
        }
        return nil
    }

    static func peopleWrites(_ t: inout Tally) async {
        let c = t.client
        let stamp = String(Int(Date().timeIntervalSince1970))
        var madeUp: [(id: Int, name: String)] = []

        // Add Person, exactly as the sheet sends it: duplicate check, POST /api/contacts,
        // then the company through /field.
        let adaName = "Selftest Ada \(stamp)"
        let ada = await t.check("people: add person (check, create, company)") {
            if await PeopleAddPersonSheet.existing(named: adaName, client: c) != nil {
                throw PeopleCheckFailed(what: "the new name already existed")
            }
            let id = try await PeopleAddPersonSheet.create(name: adaName, company: "Analytical Engines", client: c)
            let p = try await c.get("/api/contact/\(id)", as: PersonDetail.self)
            guard p.name == adaName, p.company == "Analytical Engines" else { throw PeopleCheckFailed(what: "name or company not saved") }
            return id
        } count: { _ in "created with its company" }
        if let ada { madeUp.append((ada, adaName)) }
        if let ada {
            await t.check("people: add person duplicate check (same name, other case)") {
                guard let hit = await PeopleAddPersonSheet.existing(named: "  " + adaName.uppercased() + " ", client: c) else {
                    throw PeopleCheckFailed(what: "the duplicate was not found")
                }
                guard hit.id == ada else { throw PeopleCheckFailed(what: "found someone else") }
                return hit.id
            } count: { _ in "found the existing person" }
        }

        // The hidden-names round trip.
        let graceName = "Selftest Grace \(stamp)"
        let extras = PeopleExtrasStore()
        let grace = await t.check("people: create a person to hide") {
            try await PeopleAddPersonSheet.create(name: graceName, company: "", client: c)
        } count: { _ in "created" }
        if let grace {
            madeUp.append((grace, graceName))
            await t.check("people: POST /api/exclude-list/add (hides them at once)") {
                let said = try await extras.addHidden(graceName, client: c)
                guard let entry = extras.hidden?.first(where: { $0.name == graceName }), entry.inDB else {
                    throw PeopleCheckFailed(what: "the name is not on the list as a known person")
                }
                let p = try await c.get("/api/contact/\(grace)", as: PersonDetail.self)
                guard p.excluded else { throw PeopleCheckFailed(what: "the person was not hidden") }
                guard extras.isHidden(graceName.lowercased()) else { throw PeopleCheckFailed(what: "case-insensitive check") }
                return said
            } count: { "said “\($0)”" }
            await t.check("people: POST /api/exclude-list/remove (brings them back, NC-334)") {
                let said = await extras.removeHidden(graceName, client: c)
                guard said.ok else { throw PeopleCheckFailed(what: said.text) }
                guard extras.hidden?.contains(where: { $0.name == graceName }) == false else {
                    throw PeopleCheckFailed(what: "still listed")
                }
                // NC-334: removing the name un-hides the people it hid, mirroring add.
                let p = try await c.get("/api/contact/\(grace)", as: PersonDetail.self)
                guard !p.excluded else { throw PeopleCheckFailed(what: "the person is still hidden") }
                return said.text
            } count: { "said “\($0)”, person shown again" }
        }
        let nobody = "Selftest Nobody \(stamp)"
        await t.check("people: exclude-list add and remove for a name with no person") {
            _ = try await extras.addHidden(nobody, client: c)
            guard let e = extras.hidden?.first(where: { $0.name == nobody }), !e.inDB else {
                throw PeopleCheckFailed(what: "not listed as “not in your contacts”")
            }
            let said = await extras.removeHidden(nobody, client: c)
            guard said.ok else { throw PeopleCheckFailed(what: said.text) }
            return extras.hidden?.count ?? 0
        } count: { "round trip done, \($0) names left" }

        // Add to a list, new then existing (the engine says what it really added).
        let ids = madeUp.map(\.id)
        if ids.count == 2 {
            let listName = "Selftest list \(stamp)"
            await t.check("people: add these people to a new list, then again") {
                let first = await extras.addToList(ids, list: nil, newName: listName, client: c)
                guard first.ok, first.text == "Added 2 people to “\(listName)”" else { throw PeopleCheckFailed(what: first.text) }
                guard let list = extras.lists.first(where: { $0.name == listName }) else {
                    throw PeopleCheckFailed(what: "the new list is not listed")
                }
                let again = await extras.addToList(ids, list: list, newName: nil, client: c)
                guard again.ok, again.text == "“\(listName)” already holds everyone you selected" else {
                    throw PeopleCheckFailed(what: again.text)
                }
                // Removed from the list, then added again from People: they must end up on
                // it and counted as added, whether the engine's own /add did it (NC-336) or
                // ListsAddTo had to (an engine from before that).
                let back = ids[0]
                _ = try await c.post(
                    "/api/list/\(list.id)/member", body: ["contact_id": .int(back), "remove": true] as [String: JSONValue],
                    as: WriteResult.self)
                let restored = await extras.addToList([back], list: list, newName: nil, client: c)
                guard restored.ok, restored.text == "Added 1 person to “\(listName)”" else {
                    throw PeopleCheckFailed(what: restored.text)
                }
                let members = try await c.get("/api/list/\(list.id)", as: ListDetail.self).members.map(\.id)
                guard members.contains(back) else { throw PeopleCheckFailed(what: "the removed member is still out") }
                _ = try await c.delete("/api/list/\(list.id)", as: WriteResult.self)
                return list.id
            } count: { _ in "added 2, then 0 already in, then a removed member brought back; list deleted" }
        }

        // A save on a person's page still refreshes the table (AppModel.peopleChanged).
        if let ada {
            await t.check("people: a person page save bumps peopleRevision") {
                let model = AppModel(engine: EngineController(address: c.address, launch: nil))
                let store = PersonStore(id: ada, client: c)
                store.onWrite = { model.peopleChanged() }
                try await store.saveField("position", "Selftest analyst", old: "")
                try await store.addNote("Selftest note.")
                guard model.peopleRevision == 2 else { throw PeopleCheckFailed(what: "revision \(model.peopleRevision)") }
                return model.peopleRevision
            } count: { "\($0) refreshes for 2 saves" }
        }

        // Tidy up: delete, erase for good, and take the name off the skip list that
        // deleting put it on (purging leaves it there by design).
        for person in madeUp {
            await t.check("people: tidy up a made-up person") {
                _ = try await c.post("/api/contact/\(person.id)/delete", body: ["deleted": true] as [String: JSONValue], as: WriteResult.self)
                _ = try await c.post("/api/contact/\(person.id)/purge", as: WriteResult.self)
                _ = try await c.post(
                    "/api/exclude-list/remove", body: ["name": JSONValue.string(person.name)], as: WriteResult.self)
                return 1
            } count: { _ in "deleted, erased, unlisted" }
        }
    }
}
