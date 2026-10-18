import AppKit
import Observation
import SwiftUI

// Merge two people: fold one record into another. RootView presents it whenever
// app.mergeRequest is set: from a person's page (firstID = that person, kept, fixed), from a
// group of possible duplicates (both given), or from the menus and ⌘K (neither: pick both by
// name). The web page's MergeModal (static/app/ui.js:148-208) and its pickers (:85-140).
//
// Deliberately two explicit steps, pick then confirm, because a merge cannot be undone:
// the folded record is deleted, and which one survives decides whose id, notes and history
// the result carries.
//
//   GET  /api/contacts?q=&sort=closeness&limit=40   the name search (two letters or more)
//   GET  /api/contact/{id}                          each chosen person, for what moves over
//   POST /api/merge {keep_id, dup_id}               crm/api_duplicates.py:49-62 → {ok, kept_id}

/// One side of a merge, from a search hit or a person's page.
struct MergePerson: Equatable, Sendable, Identifiable {
    var id: Int
    var name: String
    var photo: String
    var position: String
    var company: String
    var city: String
    var messages: Int
    /// Only known once the person's page has been read.
    var notes: Int?
    var identifiers: Int?
    var dates: Int?

    init(_ s: PersonSummary) {
        id = s.id; name = s.name; photo = s.photo; position = s.position; company = s.company
        city = Display.locationString(metro: s.homeMetro, city: s.homeCity); messages = s.counts.total
    }

    init(_ d: PersonDetail) {
        id = d.id; name = d.name; photo = d.photo; position = d.position; company = d.company
        city = Display.locationString(metro: d.homeMetro, city: d.homeCity); messages = d.counts.total
        notes = d.notes.count; identifiers = d.identifiers.count; dates = d.dates.count
    }

    /// Enough detail to tell two same-named people apart before folding them.
    // from static/app/ui.js:142-146 (which summed fields the search answer does not carry;
    // the channel counts are the same total)
    var line: String {
        let parts = [position, company, city,
                     messages > 0 ? "\(Display.number(messages)) \(Display.noun(messages, "message", "messages"))" : ""]
            .filter { !$0.isEmpty }
        return parts.isEmpty ? "No details yet" : parts.joined(separator: " · ")
    }

    var firstName: String { name.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? name }
}

@MainActor
@Observable
final class MergeStore {
    var keep: MergePerson?
    var fold: MergePerson?
    /// Opened from a person's page: that person is the one kept and cannot be changed.
    let keepFixed: Bool
    var keepQuery = ""
    var foldQuery = ""
    private(set) var keepHits: [MergePerson] = []
    private(set) var foldHits: [MergePerson] = []
    private(set) var searched: Set<String> = []
    private(set) var busy = false
    private(set) var loading = false
    var error: String?

    @ObservationIgnored weak var app: AppModel?
    let request: MergeRequest

    init(request: MergeRequest) {
        self.request = request
        keepFixed = request.firstID != nil && request.secondID == nil
    }

    var ready: Bool { keep != nil && fold != nil && keep?.id != fold?.id }

    func start(app: AppModel) async {
        self.app = app
        loading = true
        if let id = request.firstID { keep = await person(id) }
        if let id = request.secondID { fold = await person(id) }
        loading = false
        app.markSettled()
    }

    private func person(_ id: Int) async -> MergePerson? {
        guard let client = app?.client else { return nil }
        do {
            return MergePerson(try await client.get("/api/contact/\(id)", as: PersonDetail.self))
        } catch is CancellationError {
            return nil
        } catch let e as EngineError where e.status == 404 {
            error = "That person is no longer in your network. Pick someone else."
            return nil
        } catch {
            self.error = LedgerSettings.sentence(error)
            return nil
        }
    }

    /// A chosen search hit: read their page too, for the counts of what moves over.
    func pick(_ p: MergePerson, keepSide: Bool) async {
        if keepSide { keep = p; keepQuery = "" } else { fold = p; foldQuery = "" }
        error = nil
        guard let full = await person(p.id) else { return }
        if keepSide, keep?.id == p.id { keep = full }
        if !keepSide, fold?.id == p.id { fold = full }
    }

    func clear(keepSide: Bool) {
        if keepSide { keep = nil } else { fold = nil }
    }

    /// Allowed even when the sheet was opened from a person's page, as the web page allows
    /// it (ui.js:153, :190-194): you may want to fold this person into the other one.
    func swap() {
        guard ready else { return }
        (keep, fold) = (fold, keep)
    }

    /// Two records of one name: words that name them would not say which is which.
    var sameName: Bool {
        guard let keep, let fold else { return false }
        return keep.firstName.localizedCaseInsensitiveCompare(fold.firstName) == .orderedSame
    }

    /// "Keep Ada instead", or, when both have the same first name, which side.
    var swapTitle: String {
        guard let fold else { return "" }
        return sameName ? "Keep the record on the right instead" : "Keep \(fold.firstName) instead"
    }

    var confirmTitle: String {
        guard let keep, let fold else { return "" }
        return keep.name == fold.name
            ? "Merge the two records of \(keep.name)?" : "Merge \(fold.name) into \(keep.name)?"
    }

    var confirmMessage: String {
        guard let keep, let fold else { return "" }
        return sameName
            ? "The record on the right is deleted after everything on it moves to the one on the left. You cannot undo this."
            : "\(fold.firstName)'s record is deleted after everything on it moves to \(keep.name). You cannot undo this."
    }

    /// Names matching the search, closest first, without the person on the other side.
    // from static/app/ui.js:85-101 (two letters, a short pause, six rows)
    func search(keepSide: Bool) async {
        let q = (keepSide ? keepQuery : foldQuery).trimmingCharacters(in: .whitespacesAndNewlines)
        guard q.count >= 2, let client = app?.client else {
            if keepSide { keepHits = [] } else { foldHits = [] }
            return
        }
        try? await Task.sleep(for: .milliseconds(200))
        if Task.isCancelled { return }
        let other = keepSide ? fold?.id : keep?.id
        do {
            let rows = try await client.get(
                "/api/contacts", query: ["q": q, "sort": "closeness", "limit": "40"], as: ContactsResponse.self
            ).contacts
            if Task.isCancelled { return }
            let hits = rows.filter { $0.id != other }.prefix(6).map(MergePerson.init)
            if keepSide { keepHits = Array(hits) } else { foldHits = Array(hits) }
            searched.insert((keepSide ? "k:" : "f:") + q)
        } catch {
            // A failed lookup just shows nothing, as on the web page.
            if keepSide { keepHits = [] } else { foldHits = [] }
        }
    }

    func hasSearched(keepSide: Bool) -> Bool {
        let q = (keepSide ? keepQuery : foldQuery).trimmingCharacters(in: .whitespacesAndNewlines)
        return searched.contains((keepSide ? "k:" : "f:") + q)
    }

    /// Returns true when the merge went through.
    func merge() async -> Bool {
        guard ready, let keep, let fold, let app, !busy else { return false }
        busy = true
        defer { busy = false }
        do {
            _ = try await app.client.post(
                "/api/merge", body: ["keep_id": .int(keep.id), "dup_id": .int(fold.id)] as [String: JSONValue],
                as: WriteResult.self)
        } catch is CancellationError {
            return false
        } catch {
            self.error = "The merge did not happen. " + LedgerSettings.sentence(error)
            return false
        }
        if app.selectedPersonID == fold.id { app.selectedPersonID = keep.id }
        // The kept person's page, if open, still shows only their own notes and numbers.
        app.reloadPerson(keep.id)
        app.peopleChanged()
        await app.refreshCounts()
        DataSettingsBus.shared.announce("Merged \(fold.name) into \(keep.name)")
        return true
    }

    /// What moves to the person kept, in counts, once both pages have been read.
    var movesLine: String? {
        guard let keep, let fold, let notes = fold.notes, let ids = fold.identifiers, let dates = fold.dates else { return nil }
        var parts: [String] = []
        if notes > 0 { parts.append("\(Display.number(notes)) \(Display.noun(notes, "note", "notes"))") }
        if fold.messages > 0 { parts.append("\(Display.number(fold.messages)) \(Display.noun(fold.messages, "message", "messages"))") }
        if ids > 0 { parts.append("\(Display.number(ids)) \(Display.noun(ids, "phone number or email", "phone numbers and emails"))") }
        if dates > 0 { parts.append("\(Display.number(dates)) \(Display.noun(dates, "date", "dates"))") }
        let list: String
        switch parts.count {
        case 0: list = "Their lists"
        case 1: list = parts[0] + " and their lists"
        default: list = parts.joined(separator: ", ") + " and their lists"
        }
        // crm/db.py:2071-2128: notes, identifiers, list places and dates move; message
        // counts add up; an empty field on the kept record is filled from the other one.
        if sameName {
            return "\(list) move to the record on the left. Anything it has empty, such as a role, a company or a place, is filled in from the one on the right. Then the record on the right is deleted."
        }
        return "\(list) move to \(keep.name). Anything \(keep.firstName)'s record has empty, such as a role, a company or a place, is filled in from \(fold.firstName)'s. Then \(fold.firstName)'s record is deleted."
    }
}

struct MergeSheet: View {
    let request: MergeRequest
    @AppEnvironment private var app
    @Environment(\.dismiss) private var dismiss
    @State private var store: MergeStore
    @State private var confirming = false

    init(request: MergeRequest) {
        self.request = request
        _store = State(initialValue: MergeStore(request: request))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Merge two people")
                .font(.system(size: Metrics.fsTitle + 2, weight: .semibold))
                .foregroundStyle(Palette.ink)
            Text("Notes, messages, dates, lists and identifiers move to the person you keep. The app then deletes the other record. You cannot undo this.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .top, spacing: 10) {
                MergeSideView(title: "Keep", keepSide: true, store: store)
                VStack {
                    Spacer().frame(height: 36)
                    Button { store.swap() } label: {
                        Image(systemName: "arrow.left.arrow.right")
                    }
                    .buttonStyle(.borderless)
                    .disabled(!store.ready)
                    .help(store.ready ? store.swapTitle : "Choose both people first")
                    .accessibilityLabel("Swap which person is kept")
                }
                MergeSideView(title: "Delete after the merge", keepSide: false, store: store)
            }

            if store.ready {
                Button(store.swapTitle) { store.swap() }
                    .controlSize(.small)
            }

            if let moves = store.movesLine, store.ready {
                VStack(alignment: .leading, spacing: 4) {
                    LedgerOverline(text: "What moves over")
                    Text(verbatim: moves)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
            }

            Spacer(minLength: 0)

            HStack(spacing: 10) {
                if let error = store.error {
                    Text(verbatim: error)
                        .font(.system(size: Metrics.fsOverline + 1))
                        .foregroundStyle(Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(store.busy ? "Merging…" : store.ready ? "Merge into \(store.keep?.name ?? "")…" : "Select both people") {
                    confirming = true
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!store.ready || store.busy)
            }
        }
        .padding(22)
        .frame(width: 680, height: 540)
        .background(Palette.panel)
        .task { await store.start(app: app) }
        .confirmationDialog(store.confirmTitle, isPresented: $confirming) {
            Button("Merge", role: .destructive) {
                Task { if await store.merge() { dismiss() } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(verbatim: store.confirmMessage)
        }
    }
}

/// One searchable side of a merge: type a name, pick a person.
// from static/app/ui.js:85-140
private struct MergeSideView: View {
    let title: String
    let keepSide: Bool
    let store: MergeStore
    @AppEnvironment private var app
    @FocusState private var focused: Bool

    private var chosen: MergePerson? { keepSide ? store.keep : store.fold }
    private var hits: [MergePerson] { keepSide ? store.keepHits : store.foldHits }
    private var query: String { keepSide ? store.keepQuery : store.foldQuery }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            LedgerOverline(text: title)
            if let chosen {
                card(chosen)
            } else if store.loading {
                ProgressView().controlSize(.small).frame(height: 60)
            } else {
                picker
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private func card(_ p: MergePerson) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                MergeAvatar(name: p.name, photo: p.photo, size: 36)
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: p.name)
                        .font(.system(size: Metrics.fsUI, weight: .semibold))
                        .foregroundStyle(Palette.ink)
                    Text(verbatim: p.line)
                        .font(.system(size: Metrics.fsOverline + 1))
                        .foregroundStyle(Palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            if let notes = p.notes, let ids = p.identifiers, let dates = p.dates {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 3) {
                    fact("Notes", notes)
                    fact("Messages", p.messages)
                    fact("Phone numbers and emails", ids)
                    fact("Dates", dates)
                }
                .font(.system(size: Metrics.fsOverline + 1))
            }
            HStack {
                if !(keepSide && store.keepFixed) {
                    Button("Change") { store.clear(keepSide: keepSide) }.controlSize(.small)
                }
                Button("Open Their Page") { app.openPerson(p.id); app.mergeRequest = nil }
                    .controlSize(.small)
                    .buttonStyle(.borderless)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
            .fill(keepSide ? Palette.greenSoft.opacity(0.55) : Palette.redSoft.opacity(0.45)))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.line, lineWidth: 1))
    }

    private func fact(_ label: String, _ n: Int) -> some View {
        GridRow {
            Text(verbatim: label).foregroundStyle(Palette.muted)
            Text(verbatim: Display.number(n)).monospacedDigit().foregroundStyle(Palette.ink)
        }
    }

    private var picker: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField("Type a name", text: Binding(
                get: { query },
                set: { if keepSide { store.keepQuery = $0 } else { store.foldQuery = $0 } }))
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                .onSubmit {
                    if let first = hits.first { Task { await store.pick(first, keepSide: keepSide) } }
                }
                .task(id: query) { await store.search(keepSide: keepSide) }
            ForEach(hits) { p in
                Button { Task { await store.pick(p, keepSide: keepSide) } } label: {
                    HStack(spacing: 8) {
                        MergeAvatar(name: p.name, photo: p.photo, size: 24)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(verbatim: p.name).font(.system(size: Metrics.fsUI)).foregroundStyle(Palette.ink)
                            Text(verbatim: p.line)
                                .font(.system(size: Metrics.fsOverline))
                                .foregroundStyle(Palette.muted)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 4)
                    .padding(.horizontal, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(MergeRowStyle())
            }
            if query.trimmingCharacters(in: .whitespaces).count >= 2 && hits.isEmpty && store.hasSearched(keepSide: keepSide) {
                Text("No one else has that name.")
                    .font(.system(size: Metrics.fsOverline + 1))
                    .foregroundStyle(Palette.muted)
                    .padding(6)
            } else if query.trimmingCharacters(in: .whitespaces).count < 2 {
                Text(keepSide ? "Type two letters or more of the name of the person to keep." : "Type two letters or more of the name of the record to fold in.")
                    .font(.system(size: Metrics.fsOverline + 1))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 2)
            }
        }
        .onAppear { if keepSide || store.keep != nil { focused = true } }
    }
}

private struct MergeRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(RoundedRectangle(cornerRadius: 6).fill(configuration.isPressed ? Palette.accentSoft : Color.clear))
    }
}

/// A round avatar: the photo when there is one, else the initials on the hashed gradient
/// (static/css/ui.css:430-441), as the People table draws them.
struct MergeAvatar: View {
    let name: String
    let photo: String
    var size: CGFloat = 26
    @AppEnvironment private var app
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            if let image {
                Image(nsImage: image).resizable().interpolation(.high).aspectRatio(contentMode: .fill)
            } else {
                let c = Display.avatarColors(name)
                LinearGradient(colors: [c.top, c.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
                Text(verbatim: Display.initials(name))
                    .font(.system(size: size * 0.38, weight: .semibold))
                    .foregroundStyle(c.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .accessibilityHidden(true)
        .task(id: photo) {
            image = nil
            guard !photo.isEmpty, let data = try? await app.client.photoData(photo) else { return }
            if !Task.isCancelled { image = NSImage(data: data) }
        }
    }
}
