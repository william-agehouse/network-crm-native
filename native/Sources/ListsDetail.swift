import SwiftUI

// The open list: its name (edit in place), the pipeline summary, the event date and
// venue, Add Person, Export (copy as text or Markdown, or the CSV), and the members as a
// native Table: name, role and place, last contact, newest note, invite status. A member
// row's menu opens them, sets the status, adds a note, drafts a message or removes them;
// dragging a row onto another list in the column adds them there too.

struct ListsDetailPane: View {
    let store: ListsStore
    @AppEnvironment private var app

    var body: some View {
        Group {
            if let id = app.selectedListID {
                if let d = store.detail, d.id == id {
                    ListsDetailView(store: store, detail: d)
                        .id(d.id)
                } else if let error = store.detailError {
                    ListsMessage(
                        title: "Could not load this list",
                        detail: error,
                        action: ("Try Again", { Task { await store.loadDetail(id) } }))
                } else {
                    ProgressView().controlSize(.small)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else if store.lists?.isEmpty == true {
                ListsMessage(
                    title: "No lists yet",
                    detail: "Name a list with New List (⌘N) to create it. You can also save an Ask answer as a list.",
                    hint: "A list is a group you curate: an event roster, a shortlist, a segment. Your people stay in People — a list only points at them.",
                    action: ("New List", { store.showingNewList = true }))
            } else if store.lists == nil && store.listsError == nil {
                ProgressView().controlSize(.small)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ListsMessage(
                    title: "Select a list",
                    detail: "A list is a group you curate: an event roster, a shortlist, a segment. Your people stay in People — a list only points at them.")
            }
        }
        .task(id: app.selectedListID) {
            if let id = app.selectedListID { await store.loadDetail(id) }
        }
    }
}

/// A calm centred message: what is (or is not) here, and what to do.
struct ListsMessage: View {
    let title: String
    let detail: String
    var hint: String? = nil
    var action: (String, () -> Void)? = nil

    var body: some View {
        VStack(spacing: 8) {
            Text(title)
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
                .foregroundStyle(Palette.ink)
            Text(detail)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
            if let hint {
                Text(hint)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .multilineTextAlignment(.center)
                    .padding(.top, 2)
            }
            if let action {
                Button(action.0, action: action.1)
                    .padding(.top, 6)
            }
        }
        .frame(maxWidth: 420)
        .padding(Metrics.padView)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - One list

private struct ListsDetailView: View {
    let store: ListsStore
    let detail: ListDetail
    @AppEnvironment private var app

    @State private var nameDraft = ""
    @FocusState private var nameFocused: Bool
    @State private var venueDraft = ""
    @FocusState private var venueFocused: Bool
    @State private var showingDate = false
    @State private var showingAdd = false
    @State private var confirmDelete = false
    @State private var confirmRemove: [Int] = []
    @State private var selection = Set<Int>()
    @State private var showingExport = false
    @State private var noteFor: PersonSummary?
    @State private var draftFor: PersonSummary?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, Metrics.padView)
                .padding(.top, 16)
                .padding(.bottom, 12)
            Divider().overlay(Palette.line)
            if detail.members.isEmpty {
                ListsMessage(
                    title: "This list is empty",
                    detail: "Add a person with Add Person above. You can also save an Ask answer into this list.",
                    hint: "A list is a subset of your people. It never changes the records themselves.")
            } else {
                members
            }
        }
        .onAppear {
            nameDraft = detail.name
            venueDraft = detail.venue
            takeSnapshotHint()
        }
        .sheet(item: $noteFor) { p in ListsNoteSheet(store: store, person: p).environment(app) }
        .sheet(item: $draftFor) { p in ListsDraftSheet(person: p).environment(app) }
        .onChange(of: detail.name) { _, new in if !nameFocused { nameDraft = new } }
        .onChange(of: detail.venue) { _, new in if !venueFocused { venueDraft = new } }
        .onChange(of: nameFocused) { _, focused in if !focused { commitName() } }
        .onChange(of: venueFocused) { _, focused in if !focused { commitVenue() } }
        .onChange(of: detail.members.map(\.id)) { _, ids in selection.formIntersection(ids) }
        .confirmationDialog(
            ListsDeleteWording.title(detail.name), isPresented: $confirmDelete
        ) {
            Button("Delete List", role: .destructive) { Task { await store.deleteList(detail.id) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(ListsDeleteWording.message(members: detail.members.count))
        }
        .confirmationDialog(
            removeTitle,
            isPresented: Binding(get: { !confirmRemove.isEmpty }, set: { if !$0 { confirmRemove = [] } })
        ) {
            Button("Remove", role: .destructive) {
                let ids = confirmRemove
                Task { await store.remove(ids, from: detail.id) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("They stay in People. You can add them back at any time.")
        }
    }

    // MARK: Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 10) {
                // The whole name, leading emoji included: renaming the icon is renaming
                // the name (lists.js:521-524).
                TextField("List name", text: $nameDraft)
                    .textFieldStyle(.plain)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .focused($nameFocused)
                    .onSubmit { nameFocused = false }
                    .onExitCommand {
                        nameDraft = detail.name
                        nameFocused = false
                    }
                    .accessibilityLabel("List name")
                Spacer(minLength: 8)
                actions
            }
            Text(Display.listSummary(
                members: detail.members.count, invited: detail.counts.invited,
                replied: detail.counts.replied, attending: detail.counts.attending))
                .font(.system(size: Metrics.fsUI).monospacedDigit())
                .foregroundStyle(Palette.muted)
            HStack(spacing: 10) {
                eventDateButton
                venueField
                Spacer(minLength: 8)
                if let notice = store.notice {
                    Text(notice)
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                        .transition(.opacity)
                }
            }
            .padding(.top, 2)
        }
    }

    private var actions: some View {
        HStack(spacing: 8) {
            Button {
                showingAdd = true
            } label: {
                Label("Add Person", systemImage: "person.badge.plus")
            }
            .help("Add a person to this list")
            .popover(isPresented: $showingAdd, arrowEdge: .bottom) {
                ListsAddPersonPopover(store: store, listID: detail.id, memberIDs: Set(detail.members.map(\.id)))
            }
            // lists.js:545: only a list with people in it has anything to copy out.
            Button {
                showingExport = true
            } label: {
                Label("Export", systemImage: "square.and.arrow.up")
            }
            .disabled(detail.members.isEmpty)
            .help("Copy this list as text or Markdown, or save it as a CSV file")
            .popover(isPresented: $showingExport, arrowEdge: .bottom) {
                ListsExportPopover(store: store, detail: detail) { showingExport = false }
            }
            Menu {
                Button("Rename") { nameFocused = true }
                Button("Add Person…") { showingAdd = true }
                Button("Copy as Text") { copyAll() }
                    .disabled(detail.members.isEmpty)
                Button("Save CSV…") { Task { await store.exportCSV(detail) } }
                    .disabled(detail.members.isEmpty)
                Divider()
                Button("Delete List…", role: .destructive) { confirmDelete = true }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("More actions for this list")
            .accessibilityLabel("More actions")
        }
        .controlSize(.regular)
    }

    // MARK: Event date and venue

    private var eventDateButton: some View {
        let raw = detail.eventDate
        let valid = ListsDates.isValidDay(raw)
        let text = raw.isEmpty ? "Add event date" : valid ? Display.fmtDay(raw) : "“\(raw)” is not a date"
        return Button {
            showingDate = true
        } label: {
            Label(text, systemImage: "calendar")
                .foregroundStyle(raw.isEmpty ? Palette.muted : valid ? Palette.ink : Palette.danger)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .help(raw.isEmpty ? "Set the date of the event this list is for" : "Event date — click to change")
        .popover(isPresented: $showingDate, arrowEdge: .bottom) {
            ListsEventDatePopover(current: raw) { value in
                showingDate = false
                Task { await store.saveMeta(detail.id, key: "event_date", value: value) }
            } cancel: {
                showingDate = false
            }
        }
    }

    private var venueField: some View {
        HStack(spacing: 5) {
            Image(systemName: "mappin")
                .font(.system(size: 11))
                .foregroundStyle(Palette.muted)
            TextField("Venue", text: $venueDraft, prompt: Text("Venue").foregroundStyle(Palette.muted))
                .textFieldStyle(.plain)
                .font(.system(size: Metrics.fsUI))
                .focused($venueFocused)
                .onSubmit { venueFocused = false }
                .onExitCommand {
                    venueDraft = detail.venue
                    venueFocused = false
                }
                .accessibilityLabel("Venue")
        }
        .padding(.horizontal, 8)
        .frame(width: 240, height: 22)
        .background(
            RoundedRectangle(cornerRadius: 5)
                .strokeBorder(venueFocused ? Palette.accent : Palette.lineStrong, lineWidth: 1))
    }

    private func commitName() {
        let trimmed = nameDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        // An empty name is not a name: put the old one back rather than send it.
        guard !trimmed.isEmpty else {
            nameDraft = detail.name
            return
        }
        guard trimmed != detail.name else { return }
        Task { await store.rename(detail.id, to: trimmed) }
    }

    private func commitVenue() {
        let trimmed = venueDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed != detail.venue else { return }
        Task { await store.saveMeta(detail.id, key: "venue", value: trimmed) }
    }

    // MARK: Members

    private var members: some View {
        Table(of: PersonSummary.self, selection: $selection) {
            TableColumn("Name") { (m: PersonSummary) in
                HStack(spacing: 8) {
                    ListsAvatar(person: m, size: 26)
                    Text(verbatim: m.name)
                        .font(.system(size: Metrics.fsUI, weight: .medium))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .help(m.name)
                }
                .padding(.vertical, 3)
            }
            .width(min: 130, ideal: 165, max: 280)
            TableColumn("Role and place") { (m: PersonSummary) in
                let text = Self.whereText(m)
                Text(verbatim: text.isEmpty ? "No role yet" : text)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(text.isEmpty ? Palette.faint : Palette.muted)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(text)
            }
            .width(min: 100, ideal: 165)
            TableColumn("Last contact") { (m: PersonSummary) in
                Text(Display.lastContactText(m.last))
                    .font(.system(size: Metrics.fsUI).monospacedDigit())
                    .foregroundStyle(m.last.isEmpty ? Palette.muted : Display.staleness(lastContact: m.last).color)
                    .lineLimit(1)
            }
            .width(min: 80, ideal: 88, max: 140)
            TableColumn("Note") { (m: PersonSummary) in
                ListsNoteCell(store: store, person: m)
            }
            .width(min: 80, ideal: 125)
            TableColumn("Status") { (m: PersonSummary) in
                statusCell(m)
            }
            .width(min: 104, ideal: 112, max: 140)
        } rows: {
            ForEach(detail.members) { m in
                // Drop it on another list in the column to add them there (lists.js:600-611).
                TableRow(m).draggable(ListsDrag.payload(m.id))
            }
        }
        .tableStyle(.inset)
        .scrollContentBackground(.hidden)
        .alternatingRowBackgrounds(.disabled)
        .contextMenu(forSelectionType: Int.self) { ids in
            memberMenu(Array(ids))
        } primaryAction: { ids in
            if let id = ids.first { app.openPerson(id) }
        }
        .onDeleteCommand {
            if !selection.isEmpty { confirmRemove = orderedSelection(selection) }
        }
    }

    private func statusCell(_ m: PersonSummary) -> some View {
        let current = m.listStatus ?? ""
        return HStack(spacing: 6) {
            Circle().fill(ListsStatus.color(current)).frame(width: 7, height: 7)
            Menu {
                statusChoices(current: [current]) { value in
                    Task { await store.setStatus(value, for: [m.id], in: detail.id) }
                }
            } label: {
                Text(Display.listStatusLabel(current))
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .font(.system(size: Metrics.fsUI))
            .help("Invite status for \(m.name)")
        }
    }

    @ViewBuilder
    private func statusChoices(current: Set<String>, apply: @escaping (String) -> Void) -> some View {
        ForEach(ListsStatus.all, id: \.value) { s in
            Toggle(
                s.label,
                isOn: Binding(get: { current == [s.value] }, set: { _ in apply(s.value) }))
        }
    }

    @ViewBuilder
    private func memberMenu(_ ids: [Int]) -> some View {
        if !ids.isEmpty {
            let people = detail.members.filter { ids.contains($0.id) }
            let statuses = Set(people.map { $0.listStatus ?? "" })
            if people.count == 1, let p = people.first {
                Button("Open \(p.name)") { app.openPerson(p.id) }
                Divider()
                Button("Add a Note…") { noteFor = p }
                Button("Write a Message…") { draftFor = p }
                Divider()
            }
            Menu(people.count == 1 ? "Status" : "Status for \(people.count) People") {
                statusChoices(current: statuses) { value in
                    Task { await store.setStatus(value, for: people.map(\.id), in: detail.id) }
                }
            }
            Divider()
            Button(people.count == 1 ? "Remove from List…" : "Remove \(people.count) People from List…") {
                confirmRemove = people.map(\.id)
            }
        }
    }

    /// lists.js ExportPop's "Copy as text", for everyone, straight from the menu.
    private func copyAll() {
        let text = ListsExport.text(detail, scope: "all")
        store.say(ListsExport.copy(text)
            ? "Copied \(ListsExport.nPeople(detail.members.count)) as text"
            : "Could not copy. Use Export and take the text from the box.")
    }

    /// `--snapshot` opens the states a picture needs a click for.
    private func takeSnapshotHint() {
        guard let hint = app.snapshotHint else { return }
        switch hint {
        case "lists-note":
            if let p = detail.members.first {
                app.snapshotHint = nil
                noteFor = p
            }
        case "lists-select":
            app.snapshotHint = nil
            selection = Set(detail.members.prefix(2).map(\.id))
        default:
            break
        }
    }

    private var removeTitle: String {
        if confirmRemove.count == 1, let p = detail.members.first(where: { $0.id == confirmRemove[0] }) {
            return "Remove \(p.name) from this list?"
        }
        return "Remove \(confirmRemove.count) people from this list?"
    }

    private func orderedSelection(_ ids: Set<Int>) -> [Int] {
        detail.members.map(\.id).filter { ids.contains($0) }
    }

    /// "position · company · metro", the web page's member line (lists.js:420).
    static func whereText(_ m: PersonSummary) -> String {
        [Display.roleString(position: m.position, company: m.company),
         Display.locationString(metro: m.homeMetro, city: m.homeCity)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

// MARK: - Event date popover

/// A calendar and a typed field. Only a real YYYY-MM-DD day can be set (the engine refuses
/// anything else, NC-338, but this says what is wrong before asking it); Clear Date sends ""
/// to remove it.
private struct ListsEventDatePopover: View {
    let current: String
    let save: (String) -> Void
    let cancel: () -> Void
    @State private var typed = ""
    @State private var picked = Date()

    var body: some View {
        let valid = ListsDates.isValidDay(typed)
        VStack(alignment: .leading, spacing: 10) {
            Text("Event date")
                .font(.system(size: Metrics.fsUI, weight: .semibold))
            DatePicker("Event date", selection: $picked, displayedComponents: .date)
                .datePickerStyle(.graphical)
                .labelsHidden()
                .onChange(of: picked) { _, d in typed = ListsDates.string(d) }
            HStack(spacing: 8) {
                TextField("YYYY-MM-DD", text: $typed)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: Metrics.fsUI).monospacedDigit())
                    .frame(width: 110)
                    .onSubmit { if valid { save(typed) } }
                    .onChange(of: typed) { _, s in
                        if let d = ListsDates.date(s), ListsDates.string(picked) != s { picked = d }
                    }
                if !typed.isEmpty && !valid {
                    Text("Write a real day as YYYY-MM-DD, e.g. 2026-12-01.")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack {
                if !current.isEmpty {
                    Button("Clear Date") { save("") }
                }
                Spacer()
                Button("Cancel", action: cancel)
                    .keyboardShortcut(.cancelAction)
                Button("Set") { save(typed) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!valid || typed == current)
            }
        }
        .padding(14)
        .frame(width: 290)
        .onAppear {
            if ListsDates.isValidDay(current), let d = ListsDates.date(current) {
                typed = current
                picked = d
            } else {
                typed = ListsDates.string(Date())
            }
        }
    }
}

// MARK: - Add Person popover

/// Search people (debounced) and add one with a click or Return. People already in the
/// list are shown but cannot be added twice.
private struct ListsAddPersonPopover: View {
    let store: ListsStore
    let listID: Int
    let memberIDs: Set<Int>

    @State private var query = ""
    @State private var results: [PersonSummary]?
    /// The words `results` answer. Return adds only from results for what is typed now,
    /// never from the previous search still on screen while the next one runs.
    @State private var resultsQuery = ""
    @State private var searching = false
    @State private var error: String?
    @State private var highlighted: Int?
    @State private var addedNames: [String] = []
    @State private var adding = false
    @FocusState private var focused: Bool

    private static let shown = 8

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            TextField("Search people by name, company or role", text: $query)
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                .onSubmit(addHighlighted)
                .onKeyPress(.downArrow) { move(1); return .handled }
                .onKeyPress(.upArrow) { move(-1); return .handled }
                .padding(10)
            Divider()
            content
                .frame(height: 300)
            if let last = addedNames.last {
                Divider()
                Text(addedNames.count == 1 ? "Added \(last)." : "Added \(last) and \(addedNames.count - 1) more.")
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
            }
        }
        .frame(width: 360)
        .onAppear { focused = true }
        .task(id: query) { await search() }
    }

    @ViewBuilder private var content: some View {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if q.isEmpty {
            hint("Type a name, company or role.")
        } else if let error {
            hint(error)
        } else if let results {
            if results.isEmpty {
                hint("No one matches “\(q)”.")
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(results) { p in row(p) }
                    }
                    .padding(.vertical, 4)
                }
            }
        } else {
            ProgressView().controlSize(.small)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func hint(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.muted)
            .multilineTextAlignment(.center)
            .padding(16)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func row(_ p: PersonSummary) -> some View {
        let inList = memberIDs.contains(p.id)
        let role = Display.roleString(position: p.position, company: p.company)
        let place = Display.locationString(metro: p.homeMetro, city: p.homeCity)
        return Button {
            add(p)
        } label: {
            HStack(spacing: 9) {
                ListsAvatar(person: p, size: 26)
                VStack(alignment: .leading, spacing: 1) {
                    Text(verbatim: p.name)
                        .font(.system(size: Metrics.fsUI, weight: .medium))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                    Text(verbatim: role.isEmpty ? (place.isEmpty ? "No role yet" : place) : role)
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                }
                Spacer(minLength: 6)
                Text(inList ? "In list" : "Add")
                    .font(.system(size: Metrics.fsOverline, weight: .medium))
                    .foregroundStyle(inList ? Palette.faint : Palette.accentInk)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(highlighted == p.id ? Palette.accentSoft : .clear)
                    .padding(.horizontal, 4))
        }
        .buttonStyle(.plain)
        .disabled(inList || adding)
        .onHover { if $0 && !inList { highlighted = p.id } }
    }

    private var addable: [PersonSummary] { (results ?? []).filter { !memberIDs.contains($0.id) } }

    private func move(_ step: Int) {
        let ids = addable.map(\.id)
        guard !ids.isEmpty else { return }
        let i = highlighted.flatMap { ids.firstIndex(of: $0) } ?? (step > 0 ? -1 : ids.count)
        highlighted = ids[max(0, min(ids.count - 1, i + step))]
    }

    private func addHighlighted() {
        guard resultsQuery == query.trimmingCharacters(in: .whitespacesAndNewlines) else { return }
        guard let p = addable.first(where: { $0.id == highlighted }) ?? addable.first else { return }
        add(p)
    }

    private func add(_ p: PersonSummary) {
        guard !adding, !memberIDs.contains(p.id) else { return }
        adding = true
        Task {
            if await store.add(p, to: listID) {
                addedNames.append(p.name)
                query = ""
            }
            adding = false
            focused = true
        }
    }

    /// Debounced: waits 250 ms after the last keystroke; a newer keystroke cancels this.
    private func search() async {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        error = nil
        guard !q.isEmpty else {
            results = nil
            highlighted = nil
            return
        }
        try? await Task.sleep(for: .milliseconds(250))
        guard !Task.isCancelled else { return }
        searching = true
        defer { searching = false }
        do {
            let found = try await store.search(q)
            guard !Task.isCancelled else { return }
            // Not-yet-members first, closest first within each half.
            let sorted = found.filter { !memberIDs.contains($0.id) } + found.filter { memberIDs.contains($0.id) }
            results = Array(sorted.prefix(Self.shown))
            resultsQuery = q
            highlighted = results?.first(where: { !memberIDs.contains($0.id) })?.id
        } catch is CancellationError {
        } catch {
            self.error = ListsStore.sentence(error)
        }
    }
}
