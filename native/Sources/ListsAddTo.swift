import SwiftUI

// Adding a set of people to a list, existing or new: the engine calls and their honest
// count, and the chooser that Ask's selection bar opens (static/app/ui.js:373-421).
//
//   POST /api/list/{id}/add   {contact_ids:[int]}            → {ok, added}
//   POST /api/lists           {name, contact_ids:[int]}      → {id, added}
//   GET  /api/list/{id}                                     (who is in it after the add)
//   POST /api/list/{id}/member {contact_id:int, status:""}  (brings a removed member back)
//
// Since NC-336 the engine's /add brings back someone who was removed from the list earlier
// and counts them as added. Before that it skipped them and answered added:0, which the web
// page then read as "already holds everyone you selected" while they silently stayed out.
// So whoever /add did not add and who is still not a member afterwards is restored with
// /member, and the count says what really happened. With the current engine that step finds
// nobody to restore (the only cost is one read of the list when someone was already in); it
// stays so an engine from before NC-336 still works.
//
// A person the engine does not know is a 400 that names them (NC-337; it used to be a server
// error). It reaches the caller as an EngineError whose sentence the screen shows as it is.

/// What one add did, and the one line the screen says about it.
struct ListsAddOutcome: Sendable, Equatable {
    let listID: Int
    let listName: String
    /// Newly in the list (inserted, or brought back after an earlier removal).
    let added: Int
    /// Sent but already in the list.
    let skipped: Int
    let created: Bool

    /// ui.js:391-395.
    var message: String {
        PeopleExtrasStore.addedSentence(added: added, sent: added + skipped, list: listName)
    }
}

@MainActor
enum ListsAddTo {
    /// Order kept, duplicates dropped: the list should read like the answer it came from.
    nonisolated static func unique(_ ids: [Int]) -> [Int] {
        var seen = Set<Int>()
        return ids.filter { $0 > 0 && seen.insert($0).inserted }
    }

    static func add(_ raw: [Int], toList id: Int, named name: String, client: EngineClient) async throws -> ListsAddOutcome {
        let ids = unique(raw)
        guard !ids.isEmpty else { return ListsAddOutcome(listID: id, listName: name, added: 0, skipped: 0, created: false) }
        let r = try await client.post(
            "/api/list/\(id)/add", body: ["contact_ids": .array(ids.map { .int($0) })] as [String: JSONValue],
            as: WriteResult.self)
        var added = r.added ?? 0
        if added < ids.count {
            // Someone /add skipped: already in (the usual case), or, on an engine from before
            // NC-336, removed earlier and so still out.
            let members = Set(try await client.get("/api/list/\(id)", as: ListDetail.self).members.map(\.id))
            let missing = ids.filter { !members.contains($0) }
            if !missing.isEmpty {
                for pid in missing {
                    _ = try await client.post(
                        "/api/list/\(id)/member", body: ["contact_id": .int(pid), "status": ""] as [String: JSONValue],
                        as: WriteResult.self)
                }
                let after = Set(try await client.get("/api/list/\(id)", as: ListDetail.self).members.map(\.id))
                added += missing.filter { after.contains($0) }.count
            }
        }
        added = min(added, ids.count)
        return ListsAddOutcome(listID: id, listName: name, added: added, skipped: ids.count - added, created: false)
    }

    static func create(named raw: String, with ids: [Int], client: EngineClient) async throws -> ListsAddOutcome {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let unique = unique(ids)
        let r = try await client.post(
            "/api/lists",
            body: ["name": .string(name), "contact_ids": .array(unique.map { .int($0) })] as [String: JSONValue],
            as: CreatedID.self)
        let added = min(r.added ?? unique.count, unique.count)
        return ListsAddOutcome(listID: r.id, listName: name, added: added, skipped: unique.count - added, created: true)
    }
}

/// "Add N people to a list": every list with its size, each one click, and a field to name
/// a new one. Errors stay in the chooser, one sentence, and keep what was typed.
struct ListsAddToListView: View {
    let ids: [Int]
    /// Called once the people are in a list; the caller says the outcome and closes this.
    let onDone: (ListsAddOutcome) -> Void
    @AppEnvironment private var app

    @State private var lists: [CRMList]?
    @State private var loadError: String?
    @State private var error: String?
    @State private var name = ""
    /// The list being written to (-1 = the new one), so its row shows progress.
    @State private var busy: Int?
    @FocusState private var nameFocused: Bool

    private var count: Int { ListsAddTo.unique(ids).count }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Add \(count) \(count == 1 ? "person" : "people") to a list")
                .font(.system(size: Metrics.fsUI, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
            Divider()
            content
                .frame(height: min(260, CGFloat(max(1, lists?.count ?? 3)) * 40 + 8))
            Divider()
            HStack(spacing: 8) {
                TextField("Name a new list", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .focused($nameFocused)
                    .onSubmit { create() }
                Button("Create") { create() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy != nil || count == 0)
            }
            .controlSize(.small)
            .padding(10)
            if let error {
                Text(error)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
            }
        }
        .frame(width: 320)
        .task { await load() }
    }

    @ViewBuilder private var content: some View {
        if let lists {
            if lists.isEmpty {
                Text("No lists yet. Name one below.")
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(lists) { row($0) }
                    }
                    .padding(.vertical, 4)
                }
            }
        } else if let loadError {
            VStack(spacing: 6) {
                Text(loadError)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .multilineTextAlignment(.center)
                Button("Try Again") { Task { await load() } }.controlSize(.small)
            }
            .padding(12)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func row(_ list: CRMList) -> some View {
        ListsAddToRow(list: list, busy: busy == list.id, disabled: busy != nil || count == 0) {
            commit(listID: list.id, name: list.name)
        }
    }

    private func load() async {
        loadError = nil
        do {
            lists = try await app.client.get("/api/lists", as: ListsResponse.self).lists
        } catch is CancellationError {
        } catch {
            loadError = "Your lists did not load. \(ListsStore.sentence(error))"
        }
    }

    private func create() {
        let typed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !typed.isEmpty else { return }
        commit(listID: nil, name: typed)
    }

    private func commit(listID: Int?, name listName: String) {
        guard busy == nil, count > 0 else { return }
        busy = listID ?? -1
        error = nil
        let client = app.client
        Task {
            do {
                let outcome =
                    if let listID {
                        try await ListsAddTo.add(ids, toList: listID, named: listName, client: client)
                    } else {
                        try await ListsAddTo.create(named: listName, with: ids, client: client)
                    }
                busy = nil
                name = ""
                await app.refreshCounts()
                onDone(outcome)
            } catch is CancellationError {
                busy = nil
            } catch {
                busy = nil
                self.error = (listID == nil ? "The list was not created. " : "No one was added. ")
                    + ListsStore.sentence(error)
            }
        }
    }
}

private struct ListsAddToRow: View {
    let list: CRMList
    let busy: Bool
    let disabled: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                ListsIcon(name: list.name, size: 14)
                VStack(alignment: .leading, spacing: 1) {
                    Text(verbatim: Display.listIcon(list.name).title)
                        .font(.system(size: Metrics.fsUI, weight: .medium))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                    Text("\(Display.number(list.members)) \(Display.noun(list.members, "person", "people"))")
                        .font(.system(size: Metrics.fsOverline).monospacedDigit())
                        .foregroundStyle(Palette.muted)
                }
                Spacer(minLength: 6)
                if busy {
                    ProgressView().controlSize(.mini)
                } else {
                    Text("Add")
                        .font(.system(size: Metrics.fsOverline, weight: .medium))
                        .foregroundStyle(hovering ? Palette.accentInk : Palette.muted)
                }
            }
            .padding(.horizontal, 10)
            .frame(height: 38)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(hovering && !disabled ? Palette.hover : .clear)
                    .padding(.horizontal, 4))
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .onHover { hovering = $0 }
        .help("Add to “\(Display.listIcon(list.name).title)”")
    }
}
