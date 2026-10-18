import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers

// The Lists screen's state and every engine call it makes. Views read it and call its
// methods; nothing here draws. Routes and bodies follow native/API_CONTRACT.md §6:
//
//   GET    /api/lists                     the left column
//   GET    /api/list/{id}                 the open list (members as an array)
//   POST   /api/lists          {name}     New List
//   POST   /api/list/{id}/add  {contact_ids:[int]}
//   POST   /api/list/{id}/member {contact_id:int, status} or {contact_id:int, remove:true}
//   POST   /api/list/{id}/rename {name}
//   POST   /api/list/{id}/meta {event_date} or {venue}   (one key at a time, like the web page)
//   DELETE /api/list/{id}
//   GET    /api/list/{id}/export          CSV text, saved where the person chooses

/// A write that failed, shown as an alert: what went wrong and what to do.
struct ListsAlert: Identifiable {
    let id = UUID()
    let title: String
    let message: String
}

@MainActor
@Observable
final class ListsStore {
    /// nil until the first answer (the column shows a small progress indicator).
    private(set) var lists: [CRMList]?
    private(set) var listsError: String?
    /// The open list. May briefly belong to the previously selected id while the next loads.
    private(set) var detail: ListDetail?
    private(set) var detailError: String?
    private(set) var loadingDetailID: Int?
    /// One plain line under the header after an export or an add ("Saved Team-dinner.csv").
    private(set) var notice: String?
    var alert: ListsAlert?
    var showingNewList = false

    @ObservationIgnored private weak var app: AppModel?
    @ObservationIgnored private var noticeTask: Task<Void, Never>?

    private var client: EngineClient? { app?.client }

    // MARK: Loading

    func start(app: AppModel) async {
        self.app = app
        await loadLists()
        ensureSelection()
        // With a list to open, the detail's own load settles the screen.
        if lists?.isEmpty != false { app.markSettled() }
    }

    func loadLists() async {
        guard let client else { return }
        do {
            lists = try await client.get("/api/lists", as: ListsResponse.self).lists
            listsError = nil
        } catch is CancellationError {
        } catch {
            listsError = Self.sentence(error)
        }
    }

    /// When nothing (or a list that no longer exists) is selected, open the first list, so
    /// the screen is never blank.
    func ensureSelection() {
        guard let app, let lists else { return }
        if let id = app.selectedListID, lists.contains(where: { $0.id == id }) { return }
        app.selectedListID = lists.first?.id
    }

    func loadDetail(_ id: Int) async {
        guard let client else { return }
        if detail?.id != id { detailError = nil }
        loadingDetailID = id
        defer {
            if loadingDetailID == id { loadingDetailID = nil }
        }
        do {
            let d = try await client.get("/api/list/\(id)", as: ListDetail.self)
            guard app?.selectedListID == id else { return }
            detail = d
            detailError = nil
            // Opened from elsewhere (Ask's "Save as list") before this column knew of it.
            if lists?.contains(where: { $0.id == id }) == false { await loadLists() }
        } catch is CancellationError {
            return
        } catch let e as EngineError where e.status == 404 {
            // Deleted somewhere else: fall back to the first list that still exists.
            detail = nil
            await loadLists()
            if app?.selectedListID == id { app?.selectedListID = nil }
            ensureSelection()
        } catch {
            if app?.selectedListID == id { detailError = Self.sentence(error) }
        }
        app?.markSettled()
    }

    /// After any write: the open list and the column's counts both change.
    private func reload(_ id: Int) async {
        async let lists: Void = loadLists()
        async let detail: Void = loadDetail(id)
        _ = await (lists, detail)
    }

    // MARK: Lists

    /// Creates a list and opens it. Returns false (and shows why) when the engine refused.
    @discardableResult
    func create(name: String) async -> Bool {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, !trimmed.isEmpty else { return false }
        do {
            let made = try await client.post(
                "/api/lists", body: ["name": .string(trimmed)] as [String: JSONValue], as: CreatedID.self)
            await loadLists()
            app?.selectedListID = made.id
            await app?.refreshCounts()
            return true
        } catch is CancellationError {
            return false
        } catch {
            fail("The list was not created.", error)
            return false
        }
    }

    func rename(_ id: Int, to name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, !trimmed.isEmpty, trimmed != detail?.name else { return }
        do {
            _ = try await client.post(
                "/api/list/\(id)/rename", body: ["name": .string(trimmed)] as [String: JSONValue], as: WriteResult.self)
        } catch {
            fail("The new name was not saved.", error)
        }
        await reload(id)
    }

    /// `key` is "event_date" or "venue". An event date must already be validated
    /// (ListsDates.isValidDay) or be "" to clear it. The engine answers 400 to anything else
    /// since NC-338; the popover has already told the person what is wrong with the date,
    /// so a bad one never gets as far as this call.
    func saveMeta(_ id: Int, key: String, value: String) async {
        guard let client else { return }
        if key == "event_date", !value.isEmpty, !ListsDates.isValidDay(value) { return }
        do {
            _ = try await client.post(
                "/api/list/\(id)/meta", body: [key: .string(value)] as [String: JSONValue], as: WriteResult.self)
        } catch {
            fail(key == "venue" ? "The venue was not saved." : "The event date was not saved.", error)
        }
        await reload(id)
    }

    func deleteList(_ id: Int) async {
        guard let client else { return }
        do {
            _ = try await client.delete("/api/list/\(id)", as: WriteResult.self)
            note("List deleted")
        } catch let e as EngineError where e.status == 404 {
            // Already gone: just refresh.
        } catch {
            fail("The list was not deleted.", error)
            return
        }
        if detail?.id == id { detail = nil }
        await loadLists()
        if app?.selectedListID == id { app?.selectedListID = nil }
        ensureSelection()
        await app?.refreshCounts()
    }

    // MARK: Members

    /// Adds one person. The engine's `/add` brings back someone who was removed from this
    /// list earlier and counts them as added (NC-336). ListsAddTo still restores them with
    /// `/member` and the "To invite" status when an older engine skipped them.
    @discardableResult
    func add(_ person: PersonSummary, to id: Int) async -> Bool {
        guard let client else { return false }
        do {
            _ = try await ListsAddTo.add([person.id], toList: id, named: detail?.name ?? "", client: client)
            await reload(id)
            return true
        } catch is CancellationError {
            return false
        } catch {
            fail("\(person.name) was not added.", error)
            await reload(id)
            return false
        }
    }

    /// A member dragged from the open list onto another list in the column: it ADDS, the
    /// person stays here too, because a list only points at people (lists.js:588-611).
    func drop(personID: Int, onto list: CRMList) async {
        // The open list is not a target: everyone you could drag is already in it.
        guard let client, list.id != app?.selectedListID else { return }
        let name = detail?.members.first(where: { $0.id == personID })?.name ?? ""
        let first = name.split(separator: " ").first.map(String.init) ?? name
        let title = Display.listIcon(list.name).title
        do {
            let r = try await ListsAddTo.add([personID], toList: list.id, named: list.name, client: client)
            say(r.added > 0 ? "Added \(first) to “\(title)”" : "\(first) is already in “\(title)”")
            await loadLists()
        } catch is CancellationError {
        } catch {
            fail("\(first.isEmpty ? "The person" : first) was not added to “\(title)”.", error)
        }
    }

    func setStatus(_ status: String, for ids: [Int], in id: Int) async {
        guard let client, !ids.isEmpty else { return }
        // Show the new status at once; the reload below confirms it.
        if var d = detail, d.id == id {
            for i in d.members.indices where ids.contains(d.members[i].id) { d.members[i].listStatus = status }
            detail = d
        }
        do {
            for pid in ids {
                _ = try await client.post(
                    "/api/list/\(id)/member",
                    body: ["contact_id": .int(pid), "status": .string(status)] as [String: JSONValue], as: WriteResult.self)
            }
        } catch {
            fail("The status was not saved.", error)
        }
        await reload(id)
    }

    func remove(_ ids: [Int], from id: Int) async {
        guard let client, !ids.isEmpty else { return }
        do {
            for pid in ids {
                _ = try await client.post(
                    "/api/list/\(id)/member",
                    body: ["contact_id": .int(pid), "remove": true] as [String: JSONValue], as: WriteResult.self)
            }
        } catch {
            fail("Not everyone was removed.", error)
        }
        await reload(id)
    }

    /// People matching a search, for the Add Person popover. Closest first (the engine's
    /// default order is LinkedIn recency only, contract §3).
    func search(_ query: String) async throws -> [PersonSummary] {
        guard let client else { return [] }
        return try await client.get(
            "/api/contacts", query: ["q": query, "sort": "closeness", "limit": "40"], as: ContactsResponse.self
        ).contacts
    }

    // MARK: Member notes

    /// A member's newest note, shown in the roster. Lists have no note of their own (the
    /// engine's list_members row has none), so this is the person's own note, written
    /// through the route their page in People uses and shown there too.
    private(set) var memberNotes: [Int: ListsMemberNote] = [:]
    @ObservationIgnored private var notesAsked: Set<Int> = []
    /// Members whose notes have arrived (with or without a note).
    private(set) var notesLoaded: Set<Int> = []
    func noteChecked(_ personID: Int) -> Bool { notesLoaded.contains(personID) }

    /// GET /api/contact/{id}: read-only. Asked once per member per visit, as rows appear.
    func loadNote(_ personID: Int, force: Bool = false) async {
        guard let client, force || !notesAsked.contains(personID) else { return }
        notesAsked.insert(personID)
        do {
            let p = try await client.get("/api/contact/\(personID)", as: PersonDetail.self)
            memberNotes[personID] = ListsMemberNote(notes: p.notes)
            notesLoaded.insert(personID)
        } catch is CancellationError {
            notesAsked.remove(personID)
        } catch {
            // The cell stays quiet; the person's page shows why if it matters.
        }
    }

    /// POST /api/contact/{id}/note {body}. Returns nil when saved, else one sentence. The
    /// engine also marks the person as described, so People is told to refresh.
    func addNote(_ text: String, to personID: Int) async -> String? {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let client, !body.isEmpty else { return "Write the note first." }
        do {
            let made = try await client.post(
                "/api/contact/\(personID)/note", body: ["body": .string(body)] as [String: JSONValue], as: NoteCreated.self)
            lastNoteID = made.note.id
            await loadNote(personID, force: true)
            app?.peopleChanged()
            say("Noted")
            return nil
        } catch is CancellationError {
            return "The note was not saved."
        } catch {
            return "The note was not saved. " + Self.sentence(error)
        }
    }

    /// The id of the last note this screen wrote (the self-test tidies it up).
    @ObservationIgnored private(set) var lastNoteID: Int?

    /// A note line under the header ("Noted", "Copied 4 people as text").
    func say(_ text: String) { note(text) }

    // MARK: Export

    /// Fetches the engine's CSV (every member, with phone numbers and emails) and writes it
    /// where the person chooses.
    func exportCSV(_ list: ListDetail) async {
        guard let client else { return }
        let data: Data
        do {
            // photoData is EngineClient's only raw-bytes GET; it answers nil for a 404.
            guard let d = try await client.photoData("/api/list/\(list.id)/export") else {
                alert = ListsAlert(title: "This list no longer exists.", message: "Another window may have deleted it.")
                await loadLists()
                ensureSelection()
                return
            }
            data = d
        } catch is CancellationError {
            return
        } catch {
            fail("The list could not be exported.", error)
            return
        }

        let panel = NSSavePanel()
        panel.title = "Export List"
        panel.message = "One row per person: role, company, place, invite status, phone numbers and emails."
        panel.prompt = "Export"
        panel.nameFieldStringValue = Self.fileName(list) + ".csv"
        panel.allowedContentTypes = [.commaSeparatedText]
        panel.canCreateDirectories = true
        let response: NSApplication.ModalResponse
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            response = await withCheckedContinuation { cont in
                panel.beginSheetModal(for: window) { cont.resume(returning: $0) }
            }
        } else {
            response = panel.runModal()
        }
        guard response == .OK, let url = panel.url else { return }
        do {
            try data.write(to: url, options: .atomic)
            note("Saved \(url.lastPathComponent)")
        } catch {
            alert = ListsAlert(
                title: "The file was not saved.",
                message: "\(error.localizedDescription) Choose another folder and try again.")
        }
    }

    /// The engine's own download name rule (`crm/app.py:634`).
    static func fileName(_ list: ListDetail) -> String {
        let safe = list.name.replacingOccurrences(of: "[^A-Za-z0-9._-]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        return safe.isEmpty ? "list-\(list.id)" : safe
    }

    // MARK: Messages

    private func fail(_ title: String, _ error: Error) {
        if error is CancellationError { return }
        // A refusal (the engine's 400 or 404: a person or date it does not accept, since
        // NC-337 and NC-338) would be refused again, so only other failures say to try again.
        let refused = ((error as? EngineError)?.status).map { (400..<500).contains($0) } ?? false
        alert = ListsAlert(title: title, message: Self.sentence(error) + (refused ? "" : " Try again in a moment."))
    }

    private func note(_ text: String) {
        notice = text
        noticeTask?.cancel()
        noticeTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            self?.notice = nil
        }
    }

    /// NC-363. For an engine failure that was not the engine's own sentence, only what
    /// happened: `fail` above adds " Try again in a moment." itself, and the other
    /// callers put their own opening before this.
    static func sentence(_ error: Error) -> String {
        if let e = error as? EngineError { return e.problem }
        return EngineError.sentence(for: error)
    }
}
