import AppKit
import Foundation
import Observation

// Everything one person's page knows and every write it can make, in one place. The page
// makes a fresh store per person (PersonPage's `.task(id:)`), so nothing from the last
// person can leak onto the next one's page, and leaving a person cancels their load.
//
// Every write follows the same shape as the web page (static/app/people.js, detail half):
// send it, say what happened in one short line ("Saved", "Noted"), then reload the person so
// the page never shows a stale second copy of a field. Routes and bodies are in
// native/API_CONTRACT.md §4.

/// A list this person is in, with their status there ("" = To invite).
struct PersonListMembership: Identifiable, Hashable, Sendable {
    let list: CRMList
    let status: String
    var id: Int { list.id }
}

/// A sentence for the person, raised by the page itself rather than the engine.
struct PersonProblem: LocalizedError {
    let text: String
    var errorDescription: String? { text }
}

@MainActor
@Observable
final class PersonStore {
    let id: Int
    let client: EngineClient

    private(set) var person: PersonDetail?
    /// Set when the first load failed: what went wrong, in words, and whether the person is gone.
    private(set) var loadFailure: (message: String, missing: Bool)?
    private(set) var isLoading = true

    /// The one-line confirmation at the foot of the page ("Saved"), like the web page's toast.
    private(set) var flashText: String?
    @ObservationIgnored private var flashTask: Task<Void, Never>?

    /// "Not now" on the Going cold card: the card becomes one line saying so.
    var snoozed = false

    // One-shot requests from the People table ("Add a Note", "Draft a Message…"), the web
    // page's ?note=1 and ?draft=1. Set before the page has drawn; the view that carries
    // them out clears them, so drawing the page again never repeats them.
    var pendingDraft = false
    var pendingNoteFocus = false
    /// Bumped to put the caret in the note composer.
    var noteFocusToken = 0

    /// Which timeline view is showing, kept from one person to the next.
    var timelineTab: PersonTimelineTab = PersonStore.lastTimelineTab {
        didSet { Self.lastTimelineTab = timelineTab }
    }
    @ObservationIgnored static var lastTimelineTab: PersonTimelineTab = .all

    // The conversations (GET /api/contact/{id}/episodes), shared by the All and
    // Conversations views and read a page of 20 at a time (people.js:2060-2069).
    private(set) var episodes: [Episode] = []
    private(set) var episodesLoaded = false
    private(set) var episodesHasMore = false
    private(set) var episodesLoadingMore = false
    private(set) var episodesError: String?
    @ObservationIgnored private var episodesNextBefore: String?
    static let episodePageSize = 20

    // Loaded after the page is on screen, by the sections that show them.
    /// The newest message per channel, for the Channels section's dates.
    private(set) var channelLast: [Channel: String] = [:]
    private(set) var channelLastLoaded = false
    /// Set when the newest-message dates could not be read: the engine's sentence. Without
    /// it a failed read would look the same as a channel with no messages (a dash).
    private(set) var channelLastProblem: String?
    /// Every list (for "Add to list") and the ones this person is in.
    private(set) var lists: [CRMList] = []
    private(set) var memberships: [PersonListMembership] = []
    private(set) var listsLoaded = false
    private(set) var listsError: String?

    /// The engine has no "lists this person is in" route, so the page reads each list. This
    /// caps how many it reads for one person; more than this many lists and the rest are not
    /// checked (the section says so).
    static let listScanCap = 100
    private(set) var listsSkipped = 0

    /// Called after a write the People table should see. PersonPage points it at
    /// `AppModel.peopleChanged`.
    @ObservationIgnored var onWrite: (@MainActor () -> Void)?

    init(id: Int, client: EngineClient) {
        self.id = id
        self.client = client
    }

    var firstName: String {
        person.map { $0.name.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? $0.name } ?? ""
    }

    // MARK: Loading

    /// The first load. Cancelling the task that runs it (moving to another person) cancels
    /// the request too.
    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            person = try await client.get("/api/contact/\(id)", as: PersonDetail.self)
            loadFailure = nil
        } catch is CancellationError {
            return
        } catch {
            let missing = (error as? EngineError)?.status == 404
            loadFailure = (Self.say(error), missing)
        }
    }

    /// After a write: fetch the person again without blanking the page.
    func reload() async {
        onWrite?()
        do {
            person = try await client.get("/api/contact/\(id)", as: PersonDetail.self)
        } catch is CancellationError {
        } catch {
            flash("Could not refresh this page. \(Self.say(error))")
        }
    }

    /// The first page (once), or the next one (`more`).
    func loadEpisodes(more: Bool) async {
        if !more && episodesLoaded { return }
        if more {
            guard episodesHasMore, !episodesLoadingMore else { return }
            episodesLoadingMore = true
        }
        defer { episodesLoadingMore = false }
        var q = ["limit": String(Self.episodePageSize)]
        if more, let before = episodesNextBefore { q["before"] = before }
        do {
            let r = try await client.get("/api/contact/\(id)/episodes", query: q, as: EpisodesResponse.self)
            episodes = more ? episodes + r.episodes : r.episodes
            // people.js:2066: more only while the last page was not empty.
            episodesHasMore = r.hasMore && !r.episodes.isEmpty
            episodesNextBefore = r.nextBefore
            episodesError = nil
            episodesLoaded = true
        } catch is CancellationError {
        } catch {
            episodesError = Self.say(error)
            episodesLoaded = true
            if more { flash("Earlier conversations could not be loaded. \(Self.say(error))") }
        }
    }

    func loadChannelLast() async {
        guard let p = person else { return }
        let client = self.client
        let id = self.id
        var found: [Channel: String] = [:]
        var failure: Error?
        // At most seven tiny reads: the newest message on each channel this person used.
        await withTaskGroup(of: (Channel, String?, Error?).self) { group in
            for channel in p.counts.used {
                group.addTask {
                    do {
                        let r = try await client.get(
                            "/api/contact/\(id)/history", query: ["channel": channel.rawValue, "limit": "1"],
                            as: HistoryResponse.self)
                        return (channel, r.messages.first?.ts, nil)
                    } catch {
                        return (channel, nil, error)
                    }
                }
            }
            for await (channel, ts, error) in group {
                if let ts { found[channel] = ts }
                if let error, !(error is CancellationError) { failure = error }
            }
        }
        if Task.isCancelled { return }
        channelLast = found
        channelLastProblem = failure.map { Self.say($0) }
        channelLastLoaded = true
    }

    func loadLists() async {
        do {
            let all = try await client.get("/api/lists", as: ListsResponse.self).lists
            let scanned = Array(all.prefix(Self.listScanCap))
            let client = self.client
            let id = self.id
            var status: [Int: String] = [:]
            try await withThrowingTaskGroup(of: (Int, String?).self) { group in
                for list in scanned {
                    group.addTask {
                        let d = try await client.get("/api/list/\(list.id)", as: ListDetail.self)
                        return (list.id, d.members.first { $0.id == id }.map { $0.listStatus ?? "" })
                    }
                }
                for try await (listID, s) in group { if let s { status[listID] = s } }
            }
            lists = all
            listsSkipped = all.count - scanned.count
            memberships = scanned.compactMap { l in status[l.id].map { PersonListMembership(list: l, status: $0) } }
            listsError = nil
            listsLoaded = true
        } catch is CancellationError {
        } catch {
            listsError = Self.say(error)
            listsLoaded = true
        }
    }

    // MARK: Saying what happened

    func flash(_ text: String) {
        flashTask?.cancel()
        flashText = text
        // The web page's toast stays 2.6 s (contract §8.10).
        flashTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.6))
            guard !Task.isCancelled else { return }
            self?.flashText = nil
        }
    }

    /// An error as one sentence a person can act on.
    static func say(_ error: Error) -> String {
        EngineError.sentence(for: error)
    }

    // MARK: Writes

    private func write(_ path: String, _ body: [String: JSONValue]) async throws -> WriteResult {
        try await client.post(path, body: body, as: WriteResult.self)
    }

    /// The editable fields (name, position, company, home_city, home_metro, home_country,
    /// role_type, blurb). Throws so the field keeps the typed draft on failure, and also when
    /// the engine says it kept the old text: that is not a save, so it never reads "Saved".
    func saveField(_ field: String, _ value: String, old: String) async throws {
        let r = try await write("/api/contact/\(id)/field", Self.fieldBody(field, value, old: old))
        if let problem = Self.fieldAnswerProblem(r) { throw PersonProblem(text: problem) }
        await reload()
    }

    /// What one field save sends. Emptying a field that has text sends `clear: true`: the
    /// engine refuses to blank a filled field otherwise (a guard against wiping it by a
    /// stray click away from it) and answers "ok, unchanged". The web page used not to send
    /// it, so emptying a field there did nothing and still said "Saved" (NC-333, contract §4
    /// and trap 10). The text is trimmed here the way the engine trims it, so a field
    /// holding only spaces counts as empty on both sides.
    nonisolated static func fieldBody(_ field: String, _ value: String, old: String) -> [String: JSONValue] {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        var body: [String: JSONValue] = ["field": .string(field), "value": .string(text)]
        if text.isEmpty && !old.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { body["clear"] = true }
        return body
    }

    /// A sentence when the engine's answer to a field save was not a save (`unchanged`:
    /// the old text is still there), else nil.
    nonisolated static func fieldAnswerProblem(_ answer: WriteResult) -> String? {
        answer.unchanged == true ? "Nothing was saved: the engine kept the old text." : nil
    }

    /// How you met has its own route, which clears with "".
    func saveHowWeMet(_ text: String) async throws {
        _ = try await write("/api/contact/\(id)/howwemet", ["text": .string(text)])
        await reload()
    }

    func setCategory(_ category: String) async {
        do {
            _ = try await write("/api/contact/\(id)/category", ["category": .string(category)])
            flash("Saved")
            await reload()
        } catch { flash(Self.say(error)) }
    }

    /// Hide or bring back. The engine's `warning` (the skip-list file could not be saved)
    /// replaces the confirmation, never sits under it (people.js:404-411).
    func setExcluded(_ excluded: Bool) async {
        do {
            let r = try await write("/api/contact/\(id)/exclude", ["excluded": .bool(excluded)])
            flash(r.warning ?? (excluded ? "Excluded — hidden from review and imports" : "Back in your network"))
            await reload()
        } catch { flash(Self.say(error)) }
    }

    /// Reversible delete, or restore. Returns true when the person was deleted (the page
    /// then goes back to the table, as the web page does).
    func setDeleted(_ deleted: Bool) async -> Bool {
        do {
            let r = try await write("/api/contact/\(id)/delete", ["deleted": .bool(deleted)])
            if deleted {
                // The page closes after a delete: say it (and any skip-list warning) on the
                // app-wide message line instead of on the page going away (people.js:1415).
                DataSettingsBus.shared.announce(r.warning ?? "Deleted — restore from the Deleted filter")
                onWrite?()
            } else {
                flash(r.warning ?? "Restored")
                await reload()
            }
            return deleted
        } catch {
            flash(Self.say(error))
            return false
        }
    }

    /// Irreversible. Only offered for an already-deleted person, after a confirmation.
    func purge() async -> (ok: Bool, message: String) {
        do {
            let r = try await write("/api/contact/\(id)/purge", [:])
            onWrite?()
            let n = r.messagesRemoved ?? 0
            return (true, r.warning ?? "Permanently deleted\(n > 0 ? " · \(Display.number(n)) messages removed" : "")")
        } catch {
            flash(Self.say(error))
            return (false, "")
        }
    }

    func setPhoto(_ data: Data) async {
        // The engine refuses bodies over 10,000,000 bytes, and base64 grows a picture by a third.
        guard data.count <= 7_400_000 else {
            flash("That picture is larger than 7 MB. Pick a smaller one.")
            return
        }
        do {
            _ = try await write("/api/contact/\(id)/photo", ["data": .string(data.base64EncodedString())])
            PersonPhotoCache.forget(id: id)
            flash("Photo saved")
            await reload()
        } catch { flash(Self.say(error)) }
    }

    func removePhoto() async {
        do {
            let _: WriteResult = try await client.delete("/api/contact/\(id)/photo")
            PersonPhotoCache.forget(id: id)
            flash("Photo removed")
            await reload()
        } catch { flash(Self.say(error)) }
    }

    func snooze() async {
        do {
            _ = try await write("/api/contact/\(id)/reconnectskip", [:])
            snoozed = true
            flash("Snoozed — off the list for a while")
        } catch { flash(Self.say(error)) }
    }

    func rewriteStory() async {
        do {
            let _: BriefResult = try await client.post("/api/contact/\(id)/brief", timeout: EngineClient.askTimeout)
            flash("Story refreshed")
            await reload()
        } catch {
            let text = Self.say(error)
            flash(text.contains("ANTHROPIC") ? "AI stories need an ANTHROPIC_API_KEY — add one in Settings" : text)
        }
    }

    func draftMessage() async throws -> DraftResult {
        do {
            return try await client.post("/api/contact/\(id)/draftmessage", timeout: EngineClient.askTimeout)
        } catch {
            let text = Self.say(error)
            throw PersonProblem(text: text.contains("ANTHROPIC") ? "Drafts need an API key. Open Settings." : text)
        }
    }

    // Notes

    func addNote(_ body: String) async throws {
        let _: NoteCreated = try await client.post("/api/contact/\(id)/note", body: ["body": JSONValue.string(body)])
        flash("Noted")
        await reload()
    }

    /// A note that no longer exists (deleted in another window) is a 404 with a sentence
    /// (NC-339; the engine used to answer "ok"): the page is refreshed so the note goes, and
    /// the sentence reaches the caller, which keeps the typed text.
    func editNote(_ noteID: Int, _ body: String) async throws {
        do {
            let _: WriteResult = try await client.put("/api/note/\(noteID)", body: ["body": JSONValue.string(body)])
        } catch let e as EngineError where e.status == 404 {
            await reload()
            throw e
        }
        flash("Saved")
        await reload()
    }

    func deleteNote(_ noteID: Int) async {
        do {
            let _: WriteResult = try await client.delete("/api/note/\(noteID)")
            flash("Note deleted")
            await reload()
        } catch {
            flash(Self.say(error))
            if (error as? EngineError)?.status == 404 { await reload() }
        }
    }

    // Phones and emails

    func addIdentifier(kind: String, value: String) async throws {
        _ = try await write("/api/contact/\(id)/identifier", ["kind": .string(kind), "value": .string(value)])
        flash("Saved")
        await reload()
    }

    func removeIdentifier(_ identifier: Identifier) async {
        do {
            _ = try await write(
                "/api/contact/\(id)/identifier",
                ["kind": .string(identifier.kind), "value": .string(identifier.value), "remove": true])
            flash("Removed")
            await reload()
        } catch { flash(Self.say(error)) }
    }

    // Dates

    /// `onDate` must be a real day written YYYY-MM-DD. The engine refuses anything else with a
    /// 400 since NC-338 (it used to store "tomorrow" as a birthday), but the page says so
    /// itself first, in words, rather than pass the refusal on.
    func addDate(kind: String, onDate: String, label: String, yearKnown: Bool) async throws {
        guard ListsDates.isValidDay(onDate, years: 1000...9999) else {
            throw PersonProblem(text: "That is not a real date. Pick a day from the calendar, or write it as year-month-day, for example 1990-12-10.")
        }
        _ = try await write(
            "/api/contact/\(id)/dates",
            ["kind": .string(kind), "on_date": .string(onDate), "label": .string(label), "year_known": .bool(yearKnown)])
        flash("Saved")
        await reload()
    }

    func removeDate(_ date: DateRow) async {
        do {
            _ = try await write("/api/contact/\(id)/dates", ["remove": true, "date_id": .int(date.id)])
            flash("Removed")
            await reload()
        } catch { flash(Self.say(error)) }
    }

    // Lists

    func addToList(_ list: CRMList) async {
        let title = Display.listIcon(list.name).title
        do {
            // The engine's /add brings back someone removed from this list earlier and counts
            // them as added (NC-336). ListsAddTo still reads the list when the count falls
            // short, so an engine from before that change is covered too, and someone
            // already in the list keeps their invite status rather than being reset to
            // "To invite".
            let outcome = try await ListsAddTo.add([id], toList: list.id, named: list.name, client: client)
            flash(outcome.added > 0 ? "Added to “\(title)”" : "Already in “\(title)”")
            await loadLists()
        } catch { flash(Self.say(error)) }
    }

    func createList(named name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            let _: CreatedID = try await client.post(
                "/api/lists", body: ["name": JSONValue.string(trimmed), "contact_ids": [.int(id)]])
            flash("Added to “\(Display.listIcon(trimmed).title)”")
            await loadLists()
        } catch { flash(Self.say(error)) }
    }

    func removeFromList(_ list: CRMList) async {
        do {
            _ = try await write("/api/list/\(list.id)/member", ["contact_id": .int(id), "remove": true])
            flash("Removed from “\(Display.listIcon(list.name).title)”")
            await loadLists()
        } catch { flash(Self.say(error)) }
    }
}

// MARK: - Photos

/// Pictures already fetched, keyed by the engine's photo path (which carries the file's
/// modification time, so a new picture is a new key).
@MainActor
enum PersonPhotoCache {
    private static var images: [String: NSImage] = [:]

    static func image(for photo: String, client: EngineClient) async -> NSImage? {
        guard !photo.isEmpty else { return nil }
        if let cached = images[photo] { return cached }
        guard let data = try? await client.photoData(photo), let image = NSImage(data: data) else { return nil }
        if images.count > 300 { images.removeAll() }
        images[photo] = image
        return image
    }

    /// After this page changes someone's picture: two uploads inside one second get the
    /// same path, so drop every cached copy for that person.
    static func forget(id: Int) {
        let prefix = "/api/photo/\(id)"
        images = images.filter { key, _ in !(key == prefix || key.hasPrefix(prefix + "?")) }
    }
}
