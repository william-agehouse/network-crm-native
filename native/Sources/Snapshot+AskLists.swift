import SwiftUI

// Extra --snapshot scenes for Ask and Lists. Owned by that screen's files; Snapshot.run adds
// these to its own list. `firstID` is the closest person, for scenes that need someone.
//
// The scenes below that need written data draw from the EMPTY scratch engine on 8768,
// whatever NC_NATIVE_PORT says, so they always show made-up people: a chat called
// "Snapshot: who to invite" (an answer naming five people), an empty "Snapshot: empty
// chat", and a list called "🍽️ Snapshot: dinner" whose members have notes and statuses.
// Found by name; a missing one leaves the screen on its own default.
//
// "ask-stopping" only exists when NC_NATIVE_PORT is 8869, the stand-in engine
// (native/tools/standin.py) that answers slowly: it presses Stop and draws "Stopping".

@MainActor
enum AskListsSnapshot {
    static let scratch = 8768
    static var detail: ListDetail?
    static var store: ListsStore?

    static func chatID(titled title: String, port: Int = scratch) async -> Int? {
        let c = EngineClient(port: port)
        return try? await c.get("/api/chats", as: ChatsResponse.self).chats.first { $0.title == title }?.id
    }

    static func listID(named name: String, port: Int = scratch) async -> Int? {
        let c = EngineClient(port: port)
        return try? await c.get("/api/lists", as: ListsResponse.self).lists.first { $0.name == name }?.id
    }

    static let dinner = "🍽️ Snapshot: dinner"
}

extension Snapshot {
    static func askListsScenes(firstID: Int?) -> [Scene] {
        let s = AskListsSnapshot.self
        var scenes: [Scene] = [
            // The empty chat: greeting, the four intent chips, and (no key there) the notice.
            Scene(name: "ask-empty", port: s.scratch) { app in
                app.section = .ask
                app.selectedChatID = await s.chatID(titled: "Snapshot: empty chat")
            },
            // An intent chip's text staged in the composer, not sent.
            Scene(name: "ask-draft", port: s.scratch) { app in
                app.section = .ask
                app.selectedChatID = await s.chatID(titled: "Snapshot: empty chat")
                app.askDraft = "Who could introduce me to "
            },
            // An answer with three of its people ticked and the selection bar.
            Scene(name: "ask-ticked", port: s.scratch) { app in
                app.section = .ask
                app.selectedChatID = await s.chatID(titled: "Snapshot: who to invite")
                app.snapshotHint = "ask-ticked"
            },
            // The add-to-list chooser those ticks open (a popover is its own window).
            Scene(
                name: "ask-addtolist", port: s.scratch,
                view: { _ in
                    AnyView(
                        ListsAddToListView(ids: [27, 28, 29]) { _ in }
                            .background(Palette.panel))
                }, size: NSSize(width: 320, height: 330)
            ) { app in app.markSettled() },
            // A roster whose members have notes, statuses and an event.
            Scene(name: "lists-notes", port: s.scratch) { app in
                app.section = .lists
                app.selectedListID = await s.listID(named: s.dinner)
            },
            // Two members selected, for the row highlight in both appearances.
            Scene(name: "lists-select", port: s.scratch) { app in
                app.section = .lists
                app.selectedListID = await s.listID(named: s.dinner)
                app.snapshotHint = "lists-select"
            },
            // The Export popover: the preview of what lands on the clipboard.
            Scene(
                name: "lists-export", port: s.scratch,
                view: { _ in
                    guard let d = s.detail, let store = s.store else { return AnyView(Text("No list")) }
                    return AnyView(ListsExportPopover(store: store, detail: d) {}.background(Palette.panel))
                }, size: NSSize(width: 440, height: 470)
            ) { app in
                await loadDinner(app)
            },
            // Adding a note to a member.
            Scene(
                name: "lists-note-sheet", port: s.scratch,
                view: { _ in
                    guard let d = s.detail, let store = s.store, let p = d.members.first(where: { $0.id == 29 }) ?? d.members.first
                    else { return AnyView(Text("No list")) }
                    return AnyView(ListsNoteSheet(store: store, person: p).background(Palette.panel))
                }, size: NSSize(width: 420, height: 300)
            ) { app in
                await loadDinner(app)
            },
            // The new-list sheet that ⌘K's "Start a new list" (app.requestNewList) opens.
            Scene(
                name: "lists-new", port: s.scratch,
                view: { _ in AnyView(ListsNewListSheet(store: s.store ?? ListsStore()).background(Palette.panel)) },
                size: NSSize(width: 380, height: 170)
            ) { app in
                await loadDinner(app)
            },
        ]
        if ProcessInfo.processInfo.environment["NC_NATIVE_PORT"] == "8869" {
            scenes.append(
                Scene(name: "ask-stopping") { app in
                    app.section = .ask
                    let model = AskModel.shared(for: app)
                    await app.engine.connect()
                    await model.appeared()
                    model.send("Who should I invite to the dinner?", fromComposer: true)
                    try? await Task.sleep(for: .seconds(2))
                    model.stop()
                })
        }
        return scenes
    }

    /// The dinner list, loaded into a store, for the sheet and popover scenes.
    private static func loadDinner(_ app: AppModel) async {
        let s = AskListsSnapshot.self
        let store = ListsStore()
        if let id = await s.listID(named: s.dinner) {
            app.selectedListID = id
            await store.start(app: app)
            await store.loadDetail(id)
            for m in store.detail?.members ?? [] { await store.loadNote(m.id) }
            s.detail = store.detail
        }
        s.store = store
        app.markSettled()
    }
}
