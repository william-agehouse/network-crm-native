import Foundation
import Observation

// What sits around the People table besides the rows: the hidden-names list (the "Hidden"
// button), the Coming up strip, and the lists that "Add to a list" offers. One store, owned
// by PeopleScreen, so the strip and the badge do not flash empty each time a person opens.
// Every route here is in native/API_CONTRACT.md §3 and §6.

/// Where `id` sits in the table's order, for the person page's stepper.
struct PersonSteps: Equatable, Sendable {
    let index: Int
    let total: Int
    let previous: Int?
    let next: Int?
}

/// Something the table asks the person's page to do once it is open: the web page's
/// one-shot `?note=1` and `?draft=1` (people.js:249-251, 1320, 2051-2058).
struct PersonIntent: Equatable, Sendable {
    enum Kind: Sendable { case note, draft }
    let id: Int
    let kind: Kind
    /// Two identical requests in a row still count as two.
    var nonce = UUID()
}

@MainActor
@Observable
final class PeopleExtrasStore {
    // MARK: Hidden names (GET /api/exclude-list, POST …/add, POST …/remove)

    /// nil until the first answer: the panel shows a progress line, the badge shows nothing.
    private(set) var hidden: [ExcludeEntry]?
    private(set) var hiddenBusy = false
    /// A failed load, as one sentence for the panel.
    private(set) var hiddenError: String?

    func loadHidden(client: EngineClient) async {
        do {
            hidden = try await client.get("/api/exclude-list", as: ExcludeListResponse.self).entries
            hiddenError = nil
        } catch is CancellationError {
        } catch {
            // The web page shows an empty list here (people.js:631); a sentence is kinder.
            if hidden == nil { hidden = [] }
            hiddenError = "The hidden names could not be read. \(PersonStore.say(error))"
        }
    }

    /// people.js:295-297: the same name, ignoring case, is already on the list.
    func isHidden(_ typed: String) -> Bool {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return !t.isEmpty && (hidden ?? []).contains { $0.name.lowercased() == t }
    }

    /// Adds a name and, in the engine, hides every person with that name at once. Returns
    /// the line to show: the engine's warning when the file on disk could not be written
    /// (NC-150), else "Hidden". Throws so the field keeps what was typed.
    func addHidden(_ name: String, client: EngineClient) async throws -> String {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty else { return "" }
        hiddenBusy = true
        defer { hiddenBusy = false }
        // people.js:304-307
        let r = try await client.post(
            "/api/exclude-list/add", body: ["name": JSONValue.string(n)], as: ExcludeListResponse.self)
        hidden = r.entries
        return r.warning ?? "Hidden"
    }

    /// Takes a name off the list. The engine only edits the file: anyone already hidden
    /// stays hidden until unexcluded (contract §3, trap 11). people.js:312-320
    func removeHidden(_ name: String, client: EngineClient) async -> (text: String, ok: Bool) {
        hiddenBusy = true
        defer { hiddenBusy = false }
        do {
            let r = try await client.post(
                "/api/exclude-list/remove", body: ["name": JSONValue.string(name)], as: WriteResult.self)
            hidden = (hidden ?? []).filter { $0.name != name }
            return (r.warning ?? "Removed", r.warning == nil)
        } catch {
            return ("\(name) is still hidden. \(PersonStore.say(error))", false)
        }
    }

    // MARK: Coming up (GET /api/upcoming)

    private(set) var upcoming: UpcomingResponse?

    /// No error branch, as on the web page (people.js:563-565): a strip that cannot load has
    /// nothing useful to say and must not push the table down with an apology.
    func loadUpcoming(client: EngineClient) async {
        do {
            upcoming = try await client.get("/api/upcoming", as: UpcomingResponse.self)
        } catch is CancellationError {
        } catch {
            upcoming = nil
        }
    }

    // MARK: Waiting for the LinkedIn file (GET /api/data/linkedin-wait, NC-319)

    private(set) var linkedInWait: LinkedInWait?

    /// Same rule as Coming up: a line that cannot load stays absent (people.js LinkedInWait).
    func loadLinkedInWait(client: EngineClient) async {
        do {
            linkedInWait = try await client.get("/api/data/linkedin-wait", as: LinkedInWait.self)
        } catch is CancellationError {
        } catch {
            linkedInWait = nil
        }
    }

    /// "Stop reminding me": POST /api/data/linkedin-asked {asked: false}.
    func stopLinkedInWait(client: EngineClient) async -> String? {
        do {
            linkedInWait = try await client.post("/api/data/linkedin-asked",
                                                 body: ["asked": false] as [String: JSONValue],
                                                 as: LinkedInWait.self)
            return nil
        } catch is CancellationError {
            return nil
        } catch {
            return PersonStore.say(error)
        }
    }

    // MARK: Lists, for "Add to a list" (ui.js:373-421)

    private(set) var lists: [CRMList] = []
    private(set) var listsLoaded = false

    func loadLists(client: EngineClient) async {
        if let r = try? await client.get("/api/lists", as: ListsResponse.self) {
            lists = r.lists
            listsLoaded = true
        }
    }

    /// Adds people to an existing list (`list` set) or a new one named `newName`, and says
    /// what the engine actually ADDED, not what was sent (ui.js:378-399).
    func addToList(_ ids: [Int], list: CRMList?, newName: String?, client: EngineClient) async -> (text: String, ok: Bool) {
        guard !ids.isEmpty else { return ("", false) }
        let name = list?.name ?? newName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !name.isEmpty else { return ("", false) }
        do {
            // ListsAddTo counts what really changed, and still brings back anyone removed
            // from the list earlier whom an engine from before NC-336 left out. A person the
            // engine refuses (an id it does not know, NC-337) comes back as its sentence in
            // the line below.
            let outcome = if let list {
                try await ListsAddTo.add(ids, toList: list.id, named: name, client: client)
            } else {
                try await ListsAddTo.create(named: name, with: ids, client: client)
            }
            await loadLists(client: client)
            return (outcome.message, true)
        } catch {
            return ("Nobody was added to “\(name)”. \(PersonStore.say(error))", false)
        }
    }

    // from static/app/ui.js:391-395
    nonisolated static func addedSentence(added: Int, sent: Int, list: String) -> String {
        if added == 0 { return "“\(list)” already holds everyone you selected" }
        let skipped = sent - added
        return "Added \(Display.number(added)) \(Display.noun(added, "person", "people")) to “\(list)”"
            + (skipped > 0 ? " · \(Display.number(skipped)) already in the list" : "")
    }
}
