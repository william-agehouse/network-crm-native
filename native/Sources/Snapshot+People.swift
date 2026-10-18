import SwiftUI

// Extra --snapshot scenes for the People screen and a person's page. Owned by that screen's files; Snapshot.run adds
// these to its own list. `firstID` is the closest person, for scenes that need someone.
//
// Popovers and sheets are windows of their own, which a picture of the main window cannot
// show, so the Hidden panel and the Add Person sheet are drawn on their own here. Every
// scene only reads from the engine: the duplicate check is a GET.

@MainActor
private enum PeopleSnapshotState {
    /// The closest person's name, for the "already in your network" picture.
    static var firstName = ""
    /// The closest person, for the menu picture (never printed).
    static var person: PersonDetail?
}

extension Snapshot {
    static func peopleScenes(firstID: Int?) -> [Scene] {
        [
            // The Coming up strip with made-up dates (it draws nothing when the real
            // network has nothing in the next 14 days).
            Scene(
                name: "people-comingup",
                view: { _ in AnyView(PeopleComingUpStrip(upcoming: madeUpUpcoming()).frame(width: 900).background(Palette.panel)) },
                size: NSSize(width: 900, height: 40)
            ) { app in app.markSettled() },
            // The whole People window with its Coming up strip, on the scratch engine, whose
            // made-up people have a birthday tomorrow, a dated list and an anniversary.
            Scene(name: "people-comingup-window", port: SelfTest.scratchPort) { app in app.section = .people },
            // The person page's ⋯ menu, drawn from the same item list the menu uses.
            Scene(
                name: "person-menu",
                view: { _ in
                    AnyView(
                        Group {
                            if let p = PeopleSnapshotState.person { PersonMenuPicture(p: p) } else { Text("No person") }
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                        .background(Palette.panel))
                },
                size: NSSize(width: 320, height: 470)
            ) { app in
                if let firstID { PeopleSnapshotState.person = try? await app.client.get("/api/contact/\(firstID)", as: PersonDetail.self) }
                app.markSettled()
            },
            Scene(
                name: "people-hidden",
                view: { _ in AnyView(PeopleHiddenPanel(extras: PeopleExtrasStore(), onChange: {}).background(Palette.panel)) },
                size: NSSize(width: 340, height: 420)
            ) { _ in },
            Scene(
                name: "people-add",
                view: { _ in AnyView(PeopleAddPersonSheet(onOpen: { _ in }, onAdded: { _ in }).background(Palette.panel)) },
                size: NSSize(width: 420, height: 200)
            ) { _ in },
            Scene(
                name: "people-add-duplicate",
                view: { _ in
                    AnyView(
                        PeopleAddPersonSheet(onOpen: { _ in }, onAdded: { _ in }, initialName: PeopleSnapshotState.firstName)
                            .background(Palette.panel))
                },
                size: NSSize(width: 420, height: 280)
            ) { app in
                if let firstID, let p = try? await app.client.get("/api/contact/\(firstID)", as: PersonDetail.self) {
                    PeopleSnapshotState.firstName = p.name
                }
            },
            // Three rows selected: the selection bar replaces the count's neighbour.
            Scene(name: "people-select") { app in
                app.section = .people
                app.snapshotHint = "people-select"
            },
            // A request from ⌘K: People still to describe, most recent first.
            Scene(name: "people-request") { app in
                app.section = .people
                app.peopleRequest = PeopleRequest(sort: "recent", filter: "todo")
            },
            // "Add a Note" from the table: the page scrolls to the composer.
            Scene(name: "person-note") { app in
                app.section = .people
                app.selectedPersonID = firstID
                app.snapshotHint = "person-note"
            },
            // NC-344: rows swapped by search, views and filters; the run must print no
            // "reentrant operation" line.
            Scene(name: "people-churn") { app in
                app.section = .people
                app.snapshotHint = "people-churn"
            },
            // A narrow window: the person page must fit its column, nothing cut at the edge.
            Scene(
                name: "person-narrow",
                view: { _ in AnyView(RootView()) },
                size: NSSize(width: 960, height: 700)
            ) { app in
                app.section = .people
                app.selectedPersonID = firstID
            },
        ]
    }

    /// Made-up dates for the strip picture: no real person's data.
    private static func madeUpUpcoming() -> UpcomingResponse {
        func item(_ kind: String, _ title: String, _ days: Int, _ reason: String, contact: Int? = nil, list: Int? = nil)
            -> UpcomingItem
        {
            var i = UpcomingItem()
            i.kind = kind
            i.title = title
            i.daysUntil = days
            i.reason = reason
            i.contactID = contact
            i.listID = list
            return i
        }
        var r = UpcomingResponse()
        r.days = 14
        r.shown = [
            item("birthday", "Selftest Ada Lovelace", 1, "Birthday tomorrow (turns 36)", contact: 1),
            item("anniversary", "Selftest Grace Hopper", 5, "Anniversary on Tuesday", contact: 2),
            item("event", "🍽 Selftest dinner", 9, "On Friday · Café Ada", list: 3),
        ]
        r.more = 2
        return r
    }
}
