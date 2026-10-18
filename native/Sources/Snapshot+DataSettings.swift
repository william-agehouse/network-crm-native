import SwiftUI

// Extra --snapshot scenes for the Data and Settings pages. Owned by that screen's files; Snapshot.run adds
// these to its own list. `firstID` is the closest person, for scenes that need someone.
//
// Nothing here writes to the engine it draws from, except the two "-scratch" scenes, which
// always use the empty scratch engine on 8768 and make two made-up people there once.

extension Snapshot {
    static func dataSettingsScenes(firstID: Int?) -> [Scene] {
        [
            // The Data page opened on each part a menu command or ⌘K can ask for.
            Scene(name: "data-duplicates") { app in
                app.section = .data
                app.dataFocus = "duplicates"
            },
            Scene(name: "data-export") { app in
                app.section = .data
                app.dataFocus = "export"
            },
            // A source row opened: its Now, What you can do, How, and What it added.
            Scene(name: "data-row") { app in
                app.section = .data
                app.dataFocus = "contacts"
            },
            Scene(name: "data-row-helper") { app in
                app.section = .data
                app.dataFocus = "helper"
            },
            Scene(name: "data-row-parts") { app in
                app.section = .data
                app.dataFocus = "worked_out"
            },
            Scene(name: "data-scratch", port: SelfTest.scratchPort) { app in
                _ = await scratchPair(app.client)
                app.section = .data
                app.dataFocus = "duplicates"
            },
            // Settings rows opened, and the end of the page.
            Scene(name: "settings-ai") { app in
                app.section = .settings
                app.snapshotHint = "settings:ai"
            },
            Scene(name: "settings-identity") { app in
                app.section = .settings
                app.snapshotHint = "settings:identity"
            },
            Scene(name: "settings-goals", port: SelfTest.scratchPort) { app in
                app.section = .settings
                app.snapshotHint = "settings:goals"
            },
            Scene(name: "settings-semantic") { app in
                app.section = .settings
                app.snapshotHint = "settings:semantic"
            },
            Scene(name: "settings-install") { app in
                app.section = .settings
                app.snapshotHint = "settings:install"
            },
            // The merge sheet: choosing both people (no writes), and two made-up people chosen.
            Scene(name: "merge-pick", view: { _ in AnyView(MergeSheet(request: MergeRequest())) },
                  size: NSSize(width: 680, height: 540)) { _ in },
            Scene(name: "merge-fixed", view: { _ in AnyView(MergeSheet(request: MergeRequest(firstID: firstID))) },
                  size: NSSize(width: 680, height: 540)) { _ in },
            Scene(name: "merge-scratch", port: SelfTest.scratchPort,
                  view: { app in AnyView(MergeSheet(request: app.mergeRequest ?? MergeRequest())) },
                  size: NSSize(width: 680, height: 540)) { app in
                if let (a, b) = await scratchPair(app.client) {
                    app.mergeRequest = MergeRequest(firstID: a, secondID: b)
                }
            },
        ]
    }

    /// Two made-up near-duplicates on the scratch engine, made once and reused: one with a
    /// company and a role, the other with a note and an email address.
    static func scratchPair(_ client: EngineClient) async -> (Int, Int)? {
        guard SelfTest.isScratch(client) else { return nil }
        let name = "Selftest Ada Mergewell"
        if let found = try? await client.get(
            "/api/contacts", query: ["q": name, "sort": "closeness", "limit": "40"], as: ContactsResponse.self
        ).contacts.filter({ $0.name == name }), found.count >= 2 {
            return (found[0].id, found[1].id)
        }
        let body: [String: JSONValue] = ["name": .string(name)]
        guard let a = try? await client.post("/api/contacts", body: body, as: CreatedID.self).id,
            let b = try? await client.post("/api/contacts", body: body, as: CreatedID.self).id
        else { return nil }
        _ = try? await client.post(
            "/api/contact/\(a)/field", body: ["field": "company", "value": "Analytical Engines"] as [String: JSONValue],
            as: WriteResult.self)
        _ = try? await client.post(
            "/api/contact/\(a)/field", body: ["field": "position", "value": "Engineer"] as [String: JSONValue],
            as: WriteResult.self)
        _ = try? await client.post(
            "/api/contact/\(b)/note", body: ["body": "Met at a made-up conference."] as [String: JSONValue],
            as: NoteCreated.self)
        _ = try? await client.post(
            "/api/contact/\(b)/identifier", body: ["kind": "email", "value": "ada.mergewell@example.com"] as [String: JSONValue],
            as: WriteResult.self)
        return (a, b)
    }
}
