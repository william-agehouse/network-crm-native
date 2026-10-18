import SwiftUI

// Lists: curated groups of people (an event roster, a shortlist, a segment), each with a
// light invite pipeline per member: To invite → Invited → Replied → Going / Declined.
// The web page's version is static/app/lists.js; words and rules follow it (contract §8.9).
//
// The index of lists on the left, the open list on the right (app.selectedListID; when
// that is nil the first list opens, so the screen is never blank). A list only points at
// people: nothing here changes a person's own record.
//
// Keyboard: ⌘N new list; arrows move through the lists and the members; Delete on a list
// asks before deleting it, on members asks before removing them; Return or a double-click
// opens a member's page.

struct ListsScreen: View {
    @AppEnvironment private var app
    @State private var store = ListsStore()

    var body: some View {
        @Bindable var store = store
        HStack(spacing: 0) {
            ListsColumn(store: store)
                .frame(width: 290)
                .background(Palette.panel2)
            Divider()
            ListsDetailPane(store: store)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Palette.panel)
        }
        .task { await store.start(app: app) }
        // ⌘K "Start a new list" (lists.js:299-302): the name field, focused. Creates
        // nothing on its own. Also on appear, when the request came from another screen.
        .onChange(of: app.requestNewList, initial: true) {
            guard app.requestNewList else { return }
            app.requestNewList = false
            store.showingNewList = true
        }
        .sheet(isPresented: $store.showingNewList) {
            ListsNewListSheet(store: store)
        }
        .alert(
            store.alert?.title ?? "",
            isPresented: Binding(get: { store.alert != nil }, set: { if !$0 { store.alert = nil } }),
            presenting: store.alert
        ) { _ in
            Button("OK", role: .cancel) {}
        } message: { a in
            Text(a.message)
        }
    }
}

// MARK: - The index of lists

private struct ListsColumn: View {
    let store: ListsStore
    @AppEnvironment private var app
    @State private var confirmDelete: CRMList?
    /// The list a dragged member is over (never the open one: it already holds them).
    @State private var dropOver: Int?

    var body: some View {
        @Bindable var app = app
        VStack(spacing: 0) {
            header
            Divider().overlay(Palette.line)
            content
        }
        .confirmationDialog(
            ListsDeleteWording.title(confirmDelete?.name ?? ""),
            isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
            presenting: confirmDelete
        ) { list in
            Button("Delete List", role: .destructive) {
                Task { await store.deleteList(list.id) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { list in
            Text(ListsDeleteWording.message(members: list.members))
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            if let lists = store.lists, !lists.isEmpty {
                Text("\(lists.count) \(Display.noun(lists.count, "list", "lists"))")
                    .font(.system(size: Metrics.fsOverline, weight: .medium))
                    .foregroundStyle(Palette.muted)
            } else {
                Text("Lists")
                    .font(.system(size: Metrics.fsOverline, weight: .medium))
                    .foregroundStyle(Palette.muted)
            }
            Spacer(minLength: 4)
            Button {
                store.showingNewList = true
            } label: {
                Image(systemName: "plus")
                    .font(.system(size: 12, weight: .medium))
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.borderless)
            .foregroundStyle(Palette.ink)
            .keyboardShortcut("n", modifiers: .command)
            .help("New List (⌘N)")
            .accessibilityLabel("New List")
        }
        .padding(.leading, 14)
        .padding(.trailing, 8)
        .frame(height: 36)
    }

    @ViewBuilder private var content: some View {
        @Bindable var app = app
        if let lists = store.lists {
            if lists.isEmpty {
                VStack(spacing: 6) {
                    Text("No lists yet")
                        .font(.system(size: Metrics.fsUI, weight: .semibold))
                        .foregroundStyle(Palette.ink)
                    Text("Start a name with an emoji and it becomes the list icon.")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal, 20)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(selection: $app.selectedListID) {
                    ForEach(lists) { list in
                        ListsRow(list: list, dropTarget: dropOver == list.id)
                            .tag(list.id)
                            .dropDestination(for: String.self) { items, _ in
                                guard list.id != app.selectedListID,
                                    let pid = items.lazy.compactMap(ListsDrag.personID).first
                                else { return false }
                                Task { await store.drop(personID: pid, onto: list) }
                                return true
                            } isTargeted: { over in
                                if over && list.id != app.selectedListID {
                                    dropOver = list.id
                                } else if dropOver == list.id {
                                    dropOver = nil
                                }
                            }
                            .contextMenu {
                                Button("Delete List…") { confirmDelete = list }
                            }
                    }
                }
                .listStyle(.inset)
                .scrollContentBackground(.hidden)
                .alternatingRowBackgrounds(.disabled)
                .onDeleteCommand {
                    if let id = app.selectedListID, let list = lists.first(where: { $0.id == id }) {
                        confirmDelete = list
                    }
                }
            }
        } else if let error = store.listsError {
            VStack(spacing: 8) {
                Text("Could not load your lists")
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Text(error)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .multilineTextAlignment(.center)
                Button("Try Again") { Task { await store.loadLists(); store.ensureSelection() } }
                    .controlSize(.small)
            }
            .padding(.horizontal, 20)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ProgressView()
                .controlSize(.small)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct ListsRow: View {
    let list: CRMList
    var dropTarget = false

    var body: some View {
        HStack(spacing: 8) {
            ListsIcon(name: list.name, size: 15)
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: Display.listIcon(list.name).title)
                    .font(.system(size: Metrics.fsUI, weight: .medium))
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(Display.listSummary(
                    members: list.members, invited: list.invited, replied: list.replied, attending: list.attending))
                    .font(.system(size: Metrics.fsOverline).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
        .padding(.horizontal, 2)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(dropTarget ? Palette.accent : .clear, lineWidth: 1.5))
        .accessibilityElement(children: .combine)
    }
}

/// The web page's confirmation (lists.js:556-559), with the list named in the title
/// because a context menu can delete a list other than the open one.
enum ListsDeleteWording {
    static func title(_ listName: String) -> String {
        let title = Display.listIcon(listName).title
        return title.isEmpty ? "Delete this list?" : "Delete “\(title)”?"
    }

    static func message(members: Int) -> String {
        guard members > 0 else { return "It has no members." }
        return "The \(Display.number(members)) \(Display.noun(members, "person", "people")) stay in People."
    }
}

// MARK: - New list

struct ListsNewListSheet: View {
    let store: ListsStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var busy = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New List")
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
            TextField("New list — e.g. Team dinner", text: $name)
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                .onSubmit(create)
            Text("Start a name with an emoji and it becomes the list icon.")
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create", action: create)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.top, 4)
        }
        .padding(20)
        .frame(width: 380)
        .onAppear { focused = true }
    }

    private func create() {
        guard !busy, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        busy = true
        Task {
            let ok = await store.create(name: name)
            busy = false
            if ok { dismiss() }
        }
    }
}
