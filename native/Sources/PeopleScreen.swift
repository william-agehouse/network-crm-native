import AppKit
import SwiftUI

// People: a native table of everyone, with the person's page beside it.
//
// The table holds the whole network at once (GET /api/contacts?sort=closeness&limit=20000,
// 0.1 s for a few thousand people), so there is no "load 500 more": a Table only makes cells for the
// rows on screen. The engine answers the search, the status filter (Everyone / To describe /
// Described / Excluded / Deleted), location, role and topic; closeness, channel, last
// contact and the column sorts are applied on this Mac over everyone, as the web page does
// (static/app/people.js, contract §3 and §8.6). Around the table: Coming up, the Hidden
// names, Add Person, and "Add these N to a list".
//
// Keyboard: arrows move through people (and the page beside follows), ⌘-click and ⇧-click
// select several, Return opens, Delete asks before hiding, Escape clears a selection or
// closes the person, ⌘C copies the name, ⌘F searches, ⌘N adds someone.

struct PeopleScreen: View {
    @AppEnvironment private var app
    @State private var model = PeopleModel()
    @State private var extras = PeopleExtrasStore()
    @State private var columns = TableColumnCustomization<PeopleRow>()
    /// Location, Topics and Messages, as they were before a person opened and narrowed the table.
    @State private var savedVisibility: [String: Visibility] = [:]
    @FocusState private var searchFocused: Bool
    @State private var confirm: PeopleConfirm?
    @State private var busy = false
    @State private var totalWidth: CGFloat = 1000
    @AppStorage("people.personPaneWidth") private var paneWidth: Double = 440
    /// Rows picked with ⌘ or ⇧, when more than one. One row is simply the open person.
    @State private var marked: Set<Int> = []
    /// "Add a note" / "Draft a message" from the table, for the page about to open.
    @State private var intent: PersonIntent?
    @State private var showHidden = false
    /// The people a new list is being named for ("New List…" in an Add to List menu).
    @State private var newListIDs: [Int]?
    @State private var newListName = ""
    /// `--snapshot` only: the table's rows are being swapped on purpose (NC-344 check).
    @State private var churning = false

    static let narrowHidden = ["location", "topics", "messages"]

    var body: some View {
        @Bindable var app = app
        HStack(spacing: 0) {
            tableColumn
                .frame(minWidth: 320, maxWidth: .infinity)
            if let id = app.selectedPersonID {
                PeopleSplitHandle(width: $paneWidth, range: Self.paneRange(total: totalWidth))
                PersonPage(
                    contactID: id, steps: model.steps(around: id), intent: $intent,
                    onStep: { app.selectedPersonID = $0 }, onClose: { app.selectedPersonID = nil })
                    .id(id)
                    .frame(width: Self.paneFrameWidth(paneWidth, total: totalWidth))
                    .frame(maxHeight: .infinity)
                    .background(Palette.panel)
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { totalWidth = $0 }
        .searchable(text: $app.peopleSearch, placement: .toolbar, prompt: "Search or describe someone…")
        .searchFocused($searchFocused)
        .toolbar { toolbar }
        .task(id: PeopleLoadKey(query: model.query(search: app.peopleSearch), revision: app.peopleRevision)) {
            let q = model.query(search: app.peopleSearch)
            // Typing waits 250 ms; a menu choice or the first load does not.
            let typing = model.hasLoaded && model.loadedQuery.map { $0.q != q.q } ?? false
            await model.load(q, client: app.client, debounce: typing)
            if model.hasLoaded && !churning { app.markSettled() }
        }
        .task { await model.loadVocabulary(client: app.client) }
        .task { await churnForSnapshot() }
        // The hidden names (for the badge), the lists (for Add to List) and Coming up: read
        // on arrival, and again after any change to people, which can add a birthday or
        // hide a name.
        .task(id: app.peopleRevision) {
            let client = app.client
            let extras = self.extras
            async let h: Void = extras.loadHidden(client: client)
            async let u: Void = extras.loadUpcoming(client: client)
            async let l: Void = extras.loadLists(client: client)
            async let w: Void = extras.loadLinkedInWait(client: client)
            _ = await (h, u, l, w)
        }
        .onAppear {
            if app.searchFocusPending {
                app.searchFocusPending = false
                searchFocused = true
            }
            openHintedState()
        }
        .onChange(of: app.searchFocusRequest) {
            app.searchFocusPending = false
            searchFocused = true
        }
        // ⌘K "Add a note about X" (main.js:255, people/{id}?note=1).
        .onChange(of: app.noteComposerRequest, initial: true) { _, id in
            guard let id else { return }
            intent = PersonIntent(id: id, kind: .note)
            app.noteComposerRequest = nil
        }
        .onChange(of: app.selectedPersonID != nil, initial: true) { _, open in narrowColumns(open) }
        .onChange(of: app.peopleRequest, initial: true) { _, request in
            guard let request else { return }
            model.apply(request)
            // A view of People has no person in it (the web page's route drops the id).
            app.selectedPersonID = nil
            app.peopleRequest = nil
        }
        .onChange(of: model.visibleRevision) { pruneMarked() }
        .sheet(isPresented: $app.showAddPerson) {
            PeopleAddPersonSheet(
                onOpen: { id in
                    app.peopleSearch = ""
                    app.selectedPersonID = id
                },
                onAdded: { line in
                    model.showFlash(line)
                    app.peopleChanged()
                    Task { await app.refreshCounts() }
                }
            )
            .environment(app)
        }
        .alert(confirm?.title ?? "", isPresented: confirmShown, presenting: confirm) { c in
            Button(c.action) { Task { await perform(c) } }
                .keyboardShortcut(.defaultAction)
            Button("Cancel", role: .cancel) {}
        } message: { c in
            Text(c.message)
        }
        .alert("New list", isPresented: newListShown) {
            TextField("Name a new list", text: $newListName)
            Button("Create") {
                let ids = newListIDs ?? []
                let name = newListName
                Task { await addToList(ids, list: nil, newName: name) }
            }
            .disabled(newListName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
        } message: {
            let n = newListIDs?.count ?? 0
            Text(verbatim: "\(Display.number(n)) \(Display.noun(n, "person is", "people are")) added to it. Start a name with an emoji and it becomes the list icon.")
        }
    }

    /// The person page is 340 points at least; the table keeps `tableReserve` for its
    /// Name, Closeness and Last contact columns, or they are cut off at the page's edge.
    /// Their minimums (150 + 90 + 84), the gaps between them (3 × 17) and the table's own
    /// edge padding (15) come to 390 (NC-360).
    static let paneMin: Double = 340
    static let tableReserve: Double = 400

    static func paneRange(total: CGFloat) -> ClosedRange<Double> {
        paneMin...max(paneMin, Double(total) - tableReserve)
    }

    static func paneFrameWidth(_ wanted: Double, total: CGFloat) -> CGFloat {
        let r = paneRange(total: total)
        return CGFloat(min(max(wanted, r.lowerBound), r.upperBound))
    }

    /// `--snapshot` states: "people-hidden" opens the Hidden popover, "people-add" the sheet,
    /// "person-note" asks the open person's page for its note composer.
    /// ("people-select", which marks the first rows, waits for them: see pruneMarked.)
    private func openHintedState() {
        switch app.snapshotHint {
        case "people-hidden":
            showHidden = true
            app.snapshotHint = nil
        case "people-add":
            app.showAddPerson = true
            app.snapshotHint = nil
        case "person-note":
            if let id = app.selectedPersonID { intent = PersonIntent(id: id, kind: .note) }
            app.snapshotHint = nil
        default:
            break
        }
    }

    // MARK: The table and what sits around it

    private var tableColumn: some View {
        VStack(spacing: 0) {
            if model.activeFilterCount > 0 { PeopleAppliedFilters(model: model) }
            // Hidden while a person is open, as the web page's rail hides it (people.js:868-870).
            if app.selectedPersonID == nil, let upcoming = extras.upcoming, !upcoming.shown.isEmpty {
                PeopleComingUpStrip(upcoming: upcoming)
            }
            if app.selectedPersonID == nil, let wait = extras.linkedInWait, wait.waiting {
                PeopleLinkedInWaitLine(says: wait.says, extras: extras)
            }
            if !model.semanticNote.isEmpty { PeopleNoteLine(text: model.semanticNote) }
            if let note = meaningOnlyNote { PeopleNoteLine(text: note) }
            ZStack {
                // The table is made only once it has rows: no rows means no table, and the
                // loading line or the empty sentence stands in its place. (NC-344's cause
                // was the table measuring every inserted row; PeopleTableTuner fixes that.)
                if model.hasLoaded && model.error == nil && !model.visible.isEmpty {
                    PeopleTable(
                        model: model, columns: $columns, selection: tableSelection, marked: marked,
                        lists: extras, onConfirm: { confirm = $0 }, onIntent: open(_:_:),
                        onAddToList: { ids, list in Task { await addToList(ids, list: list, newName: nil) } },
                        onNewList: askNewList(_:), onClearSelection: { marked = [] })
                        // Opening or closing a person re-lays the columns out for the new width
                        // from their ideal sizes (an existing table keeps its old widths and
                        // pushes Last contact off the right edge).
                        .id(app.selectedPersonID != nil)
                }
                overlay
            }
            PeopleStatusBar(model: model) {
                PeopleBulkBar(
                    model: model, extras: extras, selected: markedInOrder,
                    onAdd: { ids, list in Task { await addToList(ids, list: list, newName: nil) } },
                    onNewList: askNewList(_:), onClear: { marked = [] })
            }
        }
    }

    /// Meaning search always offers its closest 30, however far off (contract trap 15), so
    /// when no name, role or company matched, say that these are only suggestions.
    private var meaningOnlyNote: String? {
        guard let q = model.loadedQuery?.q, !q.isEmpty, model.keywordCount == 0, model.meaningOnlyCount > 0
        else { return nil }
        return "No name, role or company matches “\(q)”. The people below are the closest by meaning (~), which always suggests someone."
    }

    @ViewBuilder private var overlay: some View {
        if !model.hasLoaded {
            VStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text("Loading your people…")
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = model.error {
            PeopleMessage(
                symbol: "exclamationmark.triangle",
                title: "People could not be loaded.",
                detail: error + " Check that Network CRM's engine is running, then try again.",
                action: ("Try Again", { model.reloadToken += 1 }))
        } else if model.visible.isEmpty {
            PeopleMessage(symbol: emptySymbol, title: emptySentence, detail: nil, action: emptyAction)
        }
    }

    private var emptySymbol: String {
        model.loadedQuery?.q.isEmpty == false ? "magnifyingglass" : "person.2"
    }

    /// One plain sentence: what would appear here, and how.
    private var emptySentence: String {
        let searched = !(model.loadedQuery?.q.isEmpty ?? true)
        let narrowed = model.activeFilterCount > 0
        if searched {
            return "No one matches “\(model.loadedQuery?.q ?? "")”. Try other words, clear a filter, or add someone."
        }
        if narrowed { return "No one matches these filters. Clear a filter to see more people." }
        switch model.filter {
        case .todo: return "Everyone has a description. People you have not written up yet appear here."
        case .described: return "No one is described yet. People appear here once you add a note to them."
        case .excluded: return "No one is excluded. People you hide appear here, and imports skip them."
        case .deleted: return "No one is deleted. People you delete appear here, and can be restored."
        case .all:
            return "Your network is empty. Import contacts in Data, or add someone with ⌘N."
        }
    }

    private var emptyAction: (String, () -> Void)? {
        if model.activeFilterCount > 0 { return ("Clear Filters", { model.clearFilters() }) }
        if !(model.loadedQuery?.q.isEmpty ?? true) { return ("Clear Search", { app.peopleSearch = "" }) }
        if model.filter == .all { return ("Add Person…", { app.showAddPerson = true }) }
        return nil
    }

    // MARK: Toolbar

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            PeopleViewPicker(model: model, clearSearch: { app.peopleSearch = "" })
            PeopleSortMenu(model: model)
            PeopleFilterMenu(model: model)
            PeopleHiddenButton(extras: extras, isPresented: $showHidden) { afterHiddenChange() }
            Button {
                app.showAddPerson = true
            } label: {
                Label("Add Person", systemImage: "person.badge.plus")
            }
            .keyboardShortcut("n", modifiers: .command)
            .help("Add someone new (⌘N)")
        }
    }

    // MARK: Selection

    /// The table's selection. One row is the open person (AppModel.selectedPersonID, as
    /// before); two or more are `marked`, and the page beside stays on whoever was open.
    /// Only a real change is written back: the table hands its selection back while it is
    /// still updating, and an unchanged write would wake every view that reads it.
    private var tableSelection: Binding<Set<Int>> {
        Binding(
            get: { marked.count > 1 ? marked : (app.selectedPersonID.map { [$0] } ?? []) },
            set: { new in
                if new.count > 1 {
                    if marked != new { marked = new }
                } else {
                    if !marked.isEmpty { marked = [] }
                    let id = new.first
                    if app.selectedPersonID != id { app.selectedPersonID = id }
                }
            })
    }

    /// The marked rows in the table's order (the order lists receive them in).
    private var markedInOrder: [Int] {
        guard marked.count > 1 else { return [] }
        return model.visible.map(\.id).filter { marked.contains($0) }
    }

    /// Rows that left the table (a new search, a filter) leave the selection too.
    private func pruneMarked() {
        if app.snapshotHint == "people-select", model.visible.count >= 3 {
            marked = Set(model.visible.prefix(3).map(\.id))
            app.snapshotHint = nil
            return
        }
        guard !marked.isEmpty else { return }
        let still = marked.intersection(model.visible.map(\.id))
        let next = still.count > 1 ? still : []
        if next != marked { marked = next }
    }

    /// "Add a Note" / "Draft a Message…" from the table: open the person, and tell their
    /// page what to do once it has loaded.
    private func open(_ id: Int, _ kind: PersonIntent.Kind?) {
        marked = []
        app.selectedPersonID = id
        if let kind { intent = PersonIntent(id: id, kind: kind) }
    }

    // MARK: Narrow table while a person is open

    /// With a person open the table narrows; Location, Topics and Messages step aside, as
    /// the web page's rail keeps only the columns that fit (view-network.css:411-415).
    /// Whatever the person had chosen for those comes back when the page closes.
    private func narrowColumns(_ open: Bool) {
        let keys = Self.narrowHidden
        if open {
            if savedVisibility.isEmpty {
                for k in keys { savedVisibility[k] = columns[visibility: k] }
            }
            for k in keys { columns[visibility: k] = .hidden }
        } else {
            guard !savedVisibility.isEmpty else { return }
            for k in keys { columns[visibility: k] = savedVisibility[k] ?? .automatic }
            savedVisibility = [:]
        }
    }

    // MARK: Writes

    private var confirmShown: Binding<Bool> {
        Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } })
    }

    private var newListShown: Binding<Bool> {
        Binding(get: { newListIDs != nil }, set: { if !$0 { newListIDs = nil } })
    }

    private func askNewList(_ ids: [Int]) {
        newListName = ""
        newListIDs = ids
    }

    private func addToList(_ ids: [Int], list: CRMList?, newName: String?) async {
        let said = await extras.addToList(ids, list: list, newName: newName, client: app.client)
        if !said.text.isEmpty { model.showFlash(said.text) }
        if said.ok { marked = [] }
        await app.refreshCounts()
    }

    /// A name was hidden (the engine hides matching people at once) or taken off the list.
    private func afterHiddenChange() {
        app.peopleChanged()
        Task { await app.refreshCounts() }
    }

    /// `--snapshot` scene "people-churn" (NC-344): swap the rows the ways a person does —
    /// a search that narrows them, clearing it, a view with few or none, back to everyone —
    /// so the run shows whether AppKit still prints its "reentrant operation" warning.
    private func churnForSnapshot() async {
        guard app.snapshotHint == "people-churn" else { return }
        app.snapshotHint = nil
        churning = true
        defer {
            churning = false
            app.markSettled()
        }
        func settle() async {
            for _ in 0..<40 {
                try? await Task.sleep(for: .milliseconds(150))
                if !model.isLoading { break }
            }
            try? await Task.sleep(for: .milliseconds(400))
        }
        await settle()
        NSLog("CHURN reload same rows=%d", model.visible.count)
        model.reloadToken += 1
        await settle()
        NSLog("CHURN search an rows=%d", model.visible.count)
        app.peopleSearch = "an"
        await settle()
        NSLog("CHURN search cleared rows=%d", model.visible.count)
        app.peopleSearch = ""
        await settle()
        NSLog("CHURN deleted rows=%d", model.visible.count)
        model.filter = .deleted
        await settle()
        NSLog("CHURN all rows=%d", model.visible.count)
        model.filter = .all
        await settle()
        NSLog("CHURN tier close rows=%d", model.visible.count)
        model.tierFilter = .close
        await settle()
        NSLog("CHURN tier nil rows=%d", model.visible.count)
        model.tierFilter = nil
        await settle()
        NSLog("CHURN sort name rows=%d", model.visible.count)
        model.sort = PeopleSort(column: .name, ascending: true)
        await settle()
        NSLog("CHURN sort messages rows=%d", model.visible.count)
        model.sort = PeopleSort(column: .messages, ascending: false)
        await settle()
        NSLog("CHURN sort closest rows=%d", model.visible.count)
        model.sort = .closest
        await settle()
        NSLog("CHURN sort quiet (engine) rows=%d", model.visible.count)
        model.sort = PeopleSort(column: .last, ascending: true)
        await settle()
        NSLog("CHURN sort closest (engine) rows=%d", model.visible.count)
        model.sort = .closest
        await settle()
        NSLog("CHURN open person rows=%d", model.visible.count)
        app.selectedPersonID = model.visible.first?.id
        await settle()
        NSLog("CHURN close person rows=%d", model.visible.count)
        app.selectedPersonID = nil
        await settle()
    }

    private func perform(_ c: PeopleConfirm) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        let next = model.neighbour(of: c.id)
        do {
            let r: WriteResult
            switch c.kind {
            // Exactly the person page's own calls: people.js:1404-1418.
            case .hide:
                r = try await app.client.post("/api/contact/\(c.id)/exclude", body: ["excluded": true], as: WriteResult.self)
            case .unhide:
                r = try await app.client.post("/api/contact/\(c.id)/exclude", body: ["excluded": false], as: WriteResult.self)
            case .restore:
                r = try await app.client.post("/api/contact/\(c.id)/delete", body: ["deleted": false], as: WriteResult.self)
            }
            model.showFlash(r.warning ?? c.done)
            // They leave this view: move on to the next person.
            if app.selectedPersonID == c.id { app.selectedPersonID = next }
            app.peopleChanged()
            await app.refreshCounts()
        } catch {
            model.showFlash("\(c.failed) \(error.localizedDescription)")
        }
    }
}

// MARK: - The table

private struct PeopleTable: View {
    let model: PeopleModel
    @Binding var columns: TableColumnCustomization<PeopleRow>
    let selection: Binding<Set<Int>>
    let marked: Set<Int>
    let lists: PeopleExtrasStore
    let onConfirm: (PeopleConfirm) -> Void
    /// Open a person, optionally to add a note or draft a message.
    let onIntent: (Int, PersonIntent.Kind?) -> Void
    let onAddToList: ([Int], CRMList) -> Void
    let onNewList: ([Int]) -> Void
    let onClearSelection: () -> Void
    @AppEnvironment private var app

    var body: some View {
        ScrollViewReader { proxy in
            Table(model.visible, selection: selection, sortOrder: sortOrder, columnCustomization: $columns) {
                TableColumn("Name", value: \PeopleRow.name) { row in
                    PeopleNameCell(row: row, client: app.client)
                }
                .width(min: 150, ideal: 280)
                .customizationID("name")
                .disabledCustomizationBehavior(.visibility)

                TableColumn("Closeness", value: \PeopleRow.strength) { row in
                    PeopleClosenessCell(row: row)
                }
                .width(min: 90, ideal: 118, max: 160)
                .customizationID("closeness")

                TableColumn("Last contact", value: \PeopleRow.last) { row in
                    PeopleLastCell(row: row)
                }
                .width(min: 84, ideal: 104, max: 150)
                .customizationID("last")

                TableColumn("Location", value: \PeopleRow.location) { row in
                    PeoplePlainCell(text: row.location, empty: "No location")
                }
                .width(min: 80, ideal: 140)
                .customizationID("location")

                TableColumn("Topics") { row in
                    PeopleTopicsCell(topics: row.topics)
                }
                .width(min: 90, ideal: 150)
                .customizationID("topics")

                TableColumn("Messages", value: \PeopleRow.messageTotal) { row in
                    PeopleMessagesCell(count: row.messageTotal)
                }
                .width(min: 64, ideal: 78, max: 110)
                .alignment(.trailing)
                .customizationID("messages")
            }
            .tableStyle(.inset(alternatesRowBackgrounds: false))
            // NC-344: one fixed row height instead of AppKit measuring every row.
            .background(PeopleTableTuner(rowHeight: Metrics.rowHeight))
            .contextMenu(forSelectionType: Int.self) { ids in
                menu(for: ids)
            } primaryAction: { ids in
                if let id = ids.first { onIntent(id, nil) }
            }
            .onDeleteCommand {
                guard marked.count <= 1, let id = app.selectedPersonID,
                    let row = model.visible.first(where: { $0.id == id }),
                    let c = PeopleConfirm.primary(for: row, filter: model.rowsFilter)
                else { return }
                onConfirm(c)
            }
            .onExitCommand {
                if marked.count > 1 { onClearSelection() } else { app.selectedPersonID = nil }
            }
            .onCopyCommand {
                let ids = marked.count > 1 ? Array(marked) : app.selectedPersonID.map { [$0] } ?? []
                let names = model.visible.filter { ids.contains($0.id) }.map(\.name)
                guard !names.isEmpty else { return [] }
                return [NSItemProvider(object: names.joined(separator: "\n") as NSString)]
            }
            // Someone opened from elsewhere (an Ask card, a list member): bring their row
            // into view. Also once the rows first arrive, for a person chosen before then.
            .onChange(of: app.selectedPersonID) { _, id in reveal(id, proxy) }
            .onChange(of: model.loadedQuery) { reveal(app.selectedPersonID, proxy) }
            // Someone just added (or opened before their row existed) shows up on the next
            // refetch: bring them into view then, once.
            .onChange(of: model.visibleRevision) {
                if let id = waitingFor, model.visible.contains(where: { $0.id == id }) {
                    waitingFor = nil
                    proxy.scrollTo(id, anchor: Self.revealAnchor)
                }
            }
            .onAppear { reveal(app.selectedPersonID, proxy) }
        }
    }

    /// A person opened while their row is not in the table yet.
    @State private var waitingFor: Int?

    private func reveal(_ id: Int?, _ proxy: ScrollViewProxy) {
        guard let id else { return }
        guard model.visible.contains(where: { $0.id == id }) else {
            waitingFor = id
            return
        }
        waitingFor = nil
        proxy.scrollTo(id, anchor: Self.revealAnchor)
    }

    /// Up and down to the middle, sideways to the left edge (NC-360). `.center` is the middle
    /// both ways, which on a table wider than its space slid it sideways and hid the names.
    static let revealAnchor = UnitPoint(x: 0, y: 0.5)

    /// The header sort, mapped onto the model's. A first click on a new column sorts the
    /// useful end first (closest, most recent, most messages, A to Z); clicking it again
    /// reverses it.
    private var sortOrder: Binding<[KeyPathComparator<PeopleRow>]> {
        Binding(
            get: { [Self.comparator(model.sort)] },
            set: { new in
                guard let first = new.first, let column = Self.column(first.keyPath) else { return }
                let ascending = first.order == .forward
                let sort = column == model.sort.column
                    ? PeopleSort(column: column, ascending: ascending)
                    : PeopleSort(column: column, ascending: column.naturalAscending)
                if sort != model.sort { model.sort = sort }
            })
    }

    private static func comparator(_ s: PeopleSort) -> KeyPathComparator<PeopleRow> {
        let order: SortOrder = s.ascending ? .forward : .reverse
        switch s.column {
        case .name: return KeyPathComparator(\PeopleRow.name, order: order)
        case .closeness: return KeyPathComparator(\PeopleRow.strength, order: order)
        case .last: return KeyPathComparator(\PeopleRow.last, order: order)
        case .location: return KeyPathComparator(\PeopleRow.location, order: order)
        case .messages: return KeyPathComparator(\PeopleRow.messageTotal, order: order)
        }
    }

    private static func column(_ kp: PartialKeyPath<PeopleRow>) -> PeopleColumn? {
        switch kp {
        case \PeopleRow.name: .name
        case \PeopleRow.strength: .closeness
        case \PeopleRow.last: .last
        case \PeopleRow.location: .location
        case \PeopleRow.messageTotal: .messages
        default: nil
        }
    }

    /// The right-click menu (people.js:213-268): first the facts you would otherwise open
    /// the person to see, then what to do. Right-clicking inside a selection acts on all of
    /// it; on another row, on that row alone (the table hands over exactly those ids).
    @ViewBuilder private func menu(for ids: Set<Int>) -> some View {
        let rows = model.visible.filter { ids.contains($0.id) }
        if rows.count == 1, let row = rows.first {
            Text(verbatim: Self.peek(row))
            Divider()
            Button("Open") { onIntent(row.id, nil) }
            Button("Add a Note") { onIntent(row.id, .note) }
            Button("Draft a Message…") { onIntent(row.id, .draft) }
            Divider()
            // people.js:257-259; the merge sheet is the Data part's (MergeSheet).
            Button("Merge with…") { app.mergeRequest = MergeRequest(firstID: row.id) }
            PeopleAddToListMenu(
                ids: [row.id], extras: lists, onAdd: { onAddToList([row.id], $0) }, onNewList: { onNewList([row.id]) }
            ) {
                Text("Add to List")
            }
            Divider()
            Button("Copy Name") { PersonClipboard.copy(row.name) }
            if let c = PeopleConfirm.primary(for: row, filter: model.rowsFilter) {
                Divider()
                Button(c.menuTitle) { onConfirm(c) }
            }
        } else if rows.count > 1 {
            let ordered = model.visible.filter { ids.contains($0.id) }.map(\.id)
            Text(verbatim: "\(Display.number(rows.count)) people selected")
            Divider()
            PeopleAddToListMenu(
                ids: ordered, extras: lists, onAdd: { onAddToList(ordered, $0) }, onNewList: { onNewList(ordered) }
            ) {
                Text(verbatim: "Add \(Display.number(rows.count)) to a List")
            }
            // One merge at a time (people.js:257-258).
            Button("Merge with… (one at a time)") {}
                .disabled(true)
            Divider()
            Button("Copy Names") { PersonClipboard.copy(rows.map(\.name).joined(separator: "\n")) }
            Button("Clear Selection", action: onClearSelection)
        }
    }

    /// "3d ago · Close 72 · 1,204 msgs" — score and count only when there are messages.
    // from static/app/people.js:218-239 (PersonPeek)
    static func peek(_ row: PeopleRow) -> String {
        var bits = [row.last.isEmpty ? "No contact yet" : Display.relDate(row.last)]
        if row.messageTotal > 0 {
            bits.append("\(row.tier.label) \(row.strength)")
            bits.append("\(Display.number(row.messageTotal)) msgs")
        }
        return bits.joined(separator: " · ")
    }
}

// MARK: - Cells

/// Selected rows draw on the accent colour: tinted text there would be unreadable, so
/// cells fall back to the system's own primary and secondary inks.
private struct PeopleInk {
    let prominence: BackgroundProminence
    var selected: Bool { prominence == .increased }
    func color(_ c: Color) -> AnyShapeStyle { selected ? AnyShapeStyle(.primary) : AnyShapeStyle(c) }
    func secondary(_ c: Color) -> AnyShapeStyle { selected ? AnyShapeStyle(.secondary) : AnyShapeStyle(c) }
}

private struct PeopleNameCell: View {
    let row: PeopleRow
    let client: EngineClient
    @Environment(\.backgroundProminence) private var prominence

    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        HStack(spacing: 9) {
            PeopleAvatar(name: row.name, photo: row.person.photo, size: 28, client: client)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(row.name)
                        .font(.system(size: Metrics.fsUI, weight: .medium))
                        .foregroundStyle(ink.color(Palette.ink))
                    if row.semanticOnly {
                        Text(verbatim: "~")
                            .font(.system(size: Metrics.fsUI, weight: .bold))
                            .foregroundStyle(ink.color(Palette.gold))
                            .help("Matched by meaning, not by keyword")
                            .accessibilityLabel("Matched by meaning, not by keyword")
                    }
                }
                Text(row.roleLine.isEmpty ? "No role yet" : row.roleLine)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(ink.secondary(row.roleLine.isEmpty ? Palette.faint : Palette.muted))
            }
            .lineLimit(1)
            .truncationMode(.tail)
        }
        .padding(.vertical, 3)
    }
}

private struct PeopleClosenessCell: View {
    let row: PeopleRow
    @Environment(\.backgroundProminence) private var prominence

    // from static/app/people.js:533-540 — "No messages" instead of a score when every channel is 0
    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        if Display.showsNoMessages(row.person.counts) {
            Text("No messages")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(ink.secondary(Palette.faint))
        } else {
            HStack(spacing: 6) {
                Circle().fill(row.tier.color).frame(width: 8, height: 8)
                Text(row.tier.label)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(ink.color(Palette.ink))
                Text("\(row.strength)")
                    .font(.system(size: Metrics.fsOverline).monospacedDigit())
                    .foregroundStyle(ink.secondary(Palette.muted))
            }
            .help(Display.ringTooltip(strength: row.strength, label: row.person.strengthLabel))
        }
    }
}

private struct PeopleLastCell: View {
    let row: PeopleRow
    @Environment(\.backgroundProminence) private var prominence

    // The staleness colour is on the text (tokens.css §staleness); "No contact yet" is muted.
    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        Text(row.lastText)
            .font(.system(size: Metrics.fsUI, weight: row.last.isEmpty ? .regular : .medium).monospacedDigit())
            .foregroundStyle(ink.color(row.last.isEmpty ? Palette.muted : row.staleness.color))
            .help(row.last.isEmpty ? "" : "Last contact \(Display.fmtDay(row.last))")
    }
}

private struct PeoplePlainCell: View {
    let text: String
    let empty: String
    @Environment(\.backgroundProminence) private var prominence

    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        Text(text.isEmpty ? empty : text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(text.isEmpty ? ink.secondary(Palette.faint) : ink.color(Palette.ink))
            .lineLimit(1)
    }
}

/// The first topic, whole, plus a count whose tooltip lists the rest: one readable tag
/// beats two cut-off ones in a narrow column. people.js:514-529
private struct PeopleTopicsCell: View {
    let topics: [String]
    @Environment(\.backgroundProminence) private var prominence

    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        if let first = topics.first {
            HStack(spacing: 4) {
                Text(verbatim: first)
                    .lineLimit(1)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Capsule().strokeBorder(ink.selected ? AnyShapeStyle(.secondary) : AnyShapeStyle(Palette.lineStrong)))
                    .foregroundStyle(ink.color(Palette.ink))
                if topics.count > 1 {
                    Text(verbatim: "+\(topics.count - 1)")
                        .foregroundStyle(ink.secondary(Palette.muted))
                        .fixedSize()
                        .help(topics.dropFirst().joined(separator: ", "))
                }
            }
            .font(.system(size: Metrics.fsOverline))
            .help(topics.joined(separator: ", "))
        } else {
            Text("No topics")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(ink.secondary(Palette.faint))
        }
    }
}

private struct PeopleMessagesCell: View {
    let count: Int
    @Environment(\.backgroundProminence) private var prominence

    var body: some View {
        let ink = PeopleInk(prominence: prominence)
        Text(count == 0 ? "None" : Display.number(count))
            .font(.system(size: Metrics.fsUI).monospacedDigit())
            .foregroundStyle(count == 0 ? ink.secondary(Palette.faint) : ink.color(Palette.ink))
            .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

// MARK: - Hide, unexclude, restore

struct PeopleConfirm: Identifiable {
    enum Kind { case hide, unhide, restore }
    let kind: Kind
    let id: Int
    let name: String

    /// The one reversible action that fits the view the row is in.
    static func primary(for row: PeopleRow, filter: PeopleStatusFilter) -> PeopleConfirm? {
        switch filter {
        case .all, .todo, .described: PeopleConfirm(kind: .hide, id: row.id, name: row.name)
        case .excluded: row.semanticOnly ? nil : PeopleConfirm(kind: .unhide, id: row.id, name: row.name)
        case .deleted: PeopleConfirm(kind: .restore, id: row.id, name: row.name)
        }
    }

    var menuTitle: String {
        switch kind {
        case .hide: "Hide…"
        case .unhide: "Unexclude…"
        case .restore: "Restore…"
        }
    }

    var title: String {
        switch kind {
        case .hide: "Hide \(name)?"
        case .unhide: "Bring \(name) back?"
        case .restore: "Restore \(name)?"
        }
    }

    var message: String {
        switch kind {
        case .hide:
            "They leave People, search and Ask, and every import skips them. The Excluded view brings them back."
        case .unhide:
            "They come back into People, search and Ask, and imports collect them again."
        case .restore:
            "They come back into People with their notes and messages, and imports collect them again."
        }
    }

    var action: String {
        switch kind {
        case .hide: "Hide"
        case .unhide: "Unexclude"
        case .restore: "Restore"
        }
    }

    // The web page's own confirmations, people.js:1407 and :1416.
    var done: String {
        switch kind {
        case .hide: "Excluded — hidden from review and imports"
        case .unhide: "Back in your network"
        case .restore: "Restored"
        }
    }

    var failed: String {
        switch kind {
        case .hide: "\(name) was not hidden."
        case .unhide: "\(name) was not brought back."
        case .restore: "\(name) was not restored."
        }
    }
}

// MARK: - Toolbar controls

/// The saved views (people.js:107-128), with the current one checked; "Custom view" when
/// the state matches none.
private struct PeopleViewPicker: View {
    let model: PeopleModel
    let clearSearch: () -> Void

    var body: some View {
        let active = model.activeView
        Menu {
            ForEach(model.allViews) { v in
                Toggle(isOn: Binding(get: { active?.key == v.key }, set: { _ in
                    model.apply(v)
                    clearSearch()
                })) {
                    Text(v.name)
                    Text(v.note)
                }
                if v.key == "everyone" || v.key == "close" || v.key == "described" || v.key == "deleted" {
                    Divider()
                }
            }
        } label: {
            Label(active?.name ?? "Custom view", systemImage: "rectangle.stack")
                .labelStyle(.titleAndIcon)
        }
        .menuIndicator(.visible)
        .fixedSize()
        .help("Saved views")
    }
}

private struct PeopleSortMenu: View {
    let model: PeopleModel

    var body: some View {
        Menu {
            ForEach(PeopleSort.menu, id: \.key) { item in
                Toggle(item.label, isOn: Binding(
                    get: { model.sort == item.sort },
                    set: { _ in model.sort = item.sort }))
            }
            Divider()
            Toggle("Name", isOn: Binding(
                get: { model.sort.column == .name },
                set: { _ in model.sort = PeopleSort(column: .name, ascending: true) }))
            Toggle("Most messages", isOn: Binding(
                get: { model.sort == PeopleSort(column: .messages, ascending: false) },
                set: { _ in model.sort = PeopleSort(column: .messages, ascending: false) }))
        } label: {
            Label("Sort", systemImage: "arrow.up.arrow.down")
        }
        .help("Sort the table")
    }
}

/// Location, Role, Topic (from the engine's own lists), Last contact, Closeness, Channel.
private struct PeopleFilterMenu: View {
    let model: PeopleModel

    var body: some View {
        let n = model.activeFilterCount
        Menu {
            choice("Location", any: "Anywhere", options: model.locations, value: model.location) { model.location = $0 }
            choice("Role", any: "Any role", options: model.roles, value: model.role) { model.role = $0 }
            choice("Topic", any: "Any topic", options: model.topics, value: model.topic) { model.topic = $0 }
            Menu("Last contact") {
                pick("Any time", model.lastFilter == nil) { model.lastFilter = nil }
                Divider()
                ForEach(PeopleLastFilter.allCases, id: \.self) { f in
                    pick(f.label, model.lastFilter == f) { model.lastFilter = f }
                }
            }
            Menu("Closeness") {
                pick("Any closeness", model.tierFilter == nil) { model.tierFilter = nil }
                Divider()
                ForEach(Display.Tier.allCases, id: \.self) { t in
                    pick(t.label, model.tierFilter == t) { model.tierFilter = t }
                }
            }
            Menu("Channel") {
                pick("Any channel", model.channelFilter == nil) { model.channelFilter = nil }
                Divider()
                ForEach(Channel.allCases, id: \.self) { c in
                    pick(Display.channelLabel(c), model.channelFilter == c) { model.channelFilter = c }
                }
            }
            if n > 0 {
                Divider()
                Button("Clear All Filters") { model.clearFilters() }
            }
        } label: {
            Label(n > 0 ? "Filter (\(n))" : "Filter",
                  systemImage: n > 0 ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
        }
        .help(n > 0 ? "\(n) filter\(n == 1 ? "" : "s") on" : "Filter the table")
    }

    @ViewBuilder
    private func choice(_ title: String, any: String, options: [String], value: String, set: @escaping (String) -> Void)
        -> some View
    {
        Menu(title) {
            pick(any, value.isEmpty) { set("") }
            if !options.isEmpty { Divider() }
            ForEach(options, id: \.self) { o in
                pick(o, o == value) { set(o) }
            }
        }
    }

    private func pick(_ label: String, _ on: Bool, _ action: @escaping () -> Void) -> some View {
        Toggle(label, isOn: Binding(get: { on }, set: { _ in action() }))
    }
}

// MARK: - Strips around the table

/// The applied filters, spelled out and removable (people.js:80-96).
private struct PeopleAppliedFilters: View {
    let model: PeopleModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                if !model.location.isEmpty { chip("Location", model.location) { model.location = "" } }
                if !model.role.isEmpty { chip("Role", model.role) { model.role = "" } }
                if !model.topic.isEmpty { chip("Topic", model.topic) { model.topic = "" } }
                if let l = model.lastFilter { chip("Last contact", l.label) { model.lastFilter = nil } }
                if let t = model.tierFilter { chip("Closeness", t.label) { model.tierFilter = nil } }
                if let c = model.channelFilter { chip("Channel", Display.channelLabel(c)) { model.channelFilter = nil } }
                if model.activeFilterCount > 1 {
                    Button("Clear All") { model.clearFilters() }
                        .buttonStyle(.link)
                        .font(.system(size: Metrics.fsOverline))
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
        }
        .background(Palette.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
    }

    private func chip(_ key: String, _ value: String, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 4) {
            Text(key).foregroundStyle(Palette.muted)
            Text(value).fontWeight(.semibold).foregroundStyle(Palette.ink)
            Button(action: remove) {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Palette.muted)
                    .frame(width: 14, height: 14)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Remove the \(key.lowercased()) filter")
            .accessibilityLabel("Remove the \(key.lowercased()) filter")
        }
        .font(.system(size: Metrics.fsOverline))
        .padding(.leading, 8)
        .padding(.trailing, 4)
        .frame(height: 22)
        .background(Capsule().fill(Palette.panel2))
        .overlay(Capsule().strokeBorder(Palette.line))
    }
}

/// The meaning-search note ("Semantic search needs a one-time install: …").
private struct PeopleNoteLine: View {
    let text: String
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "info.circle").foregroundStyle(Palette.muted)
            Text(text).foregroundStyle(Palette.muted).lineLimit(2)
            Spacer(minLength: 0)
        }
        .font(.system(size: Metrics.fsOverline))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Palette.panel2)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
    }
}

/// NC-319: setup can start LinkedIn's data request, and the file arrives by email a day or
/// two later. Until it is imported, one quiet line above the table says so and where it
/// goes (people.js LinkedInWait). The engine ends the wait once a LinkedIn connection is in.
private struct PeopleLinkedInWaitLine: View {
    let says: String
    let extras: PeopleExtrasStore
    @AppEnvironment private var app
    @State private var note: String?

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "clock").foregroundStyle(Palette.muted)
            Text(note ?? says).foregroundStyle(Palette.muted).lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button("Open the LinkedIn row") {
                app.dataFocus = "linkedin_connections"
                app.section = .data
            }
            .controlSize(.small)
            Button("Stop reminding me") {
                Task { note = await extras.stopLinkedInWait(client: app.client) }
            }
            .controlSize(.small)
        }
        .font(.system(size: Metrics.fsOverline))
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Palette.panel2)
        .overlay(alignment: .bottom) { Rectangle().fill(Palette.line).frame(height: 1) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Waiting for your LinkedIn file")
    }
}

/// The count ("1,250 people", "12 of 1,250 people"), a quiet spinner while a newer answer
/// is on its way, and a passing confirmation after a change.
private struct PeopleStatusBar<Bulk: View>: View {
    let model: PeopleModel
    /// "Add these N to a list", or the selection bar (PeopleBulkBar).
    @ViewBuilder var bulk: Bulk

    var body: some View {
        HStack(spacing: 8) {
            if let total = model.total, model.error == nil {
                Text(Display.peopleCountLine(shown: model.visible.count, total: total)
                    + (model.meaningOnlyCount > 0 ? " · \(Display.number(model.meaningOnlyCount)) by meaning (~)" : ""))
                    .monospacedDigit()
                    .foregroundStyle(Palette.muted)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
            if model.isLoading && model.hasLoaded {
                ProgressView().controlSize(.mini)
            }
            bulk
            Spacer(minLength: 8)
            if let flash = model.flash {
                Text(flash)
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .transition(.opacity)
                    .help(flash)
            }
        }
        .font(.system(size: Metrics.fsOverline))
        .padding(.horizontal, 12)
        .frame(height: 30)
        .background(Palette.panel2)
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
        .animation(.easeOut(duration: 0.15), value: model.flash)
    }
}

private struct PeopleMessage: View {
    let symbol: String
    let title: String
    let detail: String?
    let action: (String, () -> Void)?

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 26, weight: .light))
                .foregroundStyle(Palette.faint)
            Text(title)
                .font(.system(size: Metrics.fsUI, weight: .medium))
                .foregroundStyle(Palette.ink)
                .multilineTextAlignment(.center)
            if let detail {
                Text(detail)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.muted)
                    .multilineTextAlignment(.center)
            }
            if let action {
                Button(action.0, action: action.1)
                    .controlSize(.small)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: 420)
        .padding(Metrics.padView)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.panel)
    }
}

/// The divider between the table and the person: drag to resize; the width is remembered.
private struct PeopleSplitHandle: View {
    @Binding var width: Double
    let range: ClosedRange<Double>
    @State private var start: Double?

    var body: some View {
        Rectangle()
            .fill(Palette.line)
            .frame(width: 1)
            .frame(maxHeight: .infinity)
            .overlay {
                Color.clear
                    .frame(width: 9)
                    .contentShape(Rectangle())
                    .onHover { inside in
                        if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() }
                    }
                    .gesture(
                        DragGesture(minimumDistance: 1, coordinateSpace: .global)
                            .onChanged { g in
                                let s = start ?? width
                                start = s
                                width = min(max(s - g.translation.width, range.lowerBound), range.upperBound)
                            }
                            .onEnded { _ in start = nil })
            }
            .accessibilityHidden(true)
    }
}
