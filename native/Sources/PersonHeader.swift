import AppKit
import SwiftUI
import UniformTypeIdentifiers

// The top of a person's page: photo, name, role and company, where they live, tags, the
// actions, and the four vital tiles. Then the "Going cold" card. Wording and order follow
// static/app/people.js:1104-1553 (contract §8.7).

struct PersonHeader: View {
    let store: PersonStore
    let p: PersonDetail
    @AppEnvironment private var app
    @Environment(\.openURL) private var openURL

    @State private var pending: PendingAction?
    @State private var showDraft = false
    @State private var naming = false
    @State private var newListName = ""

    enum PendingAction: Identifiable {
        case exclude, unexclude, delete, purge
        var id: Self { self }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 14) {
                Button(action: pickPhoto) {
                    PersonAvatar(name: p.name, photo: p.photo, strength: p.strength, tier: p.strengthLabel, size: 76)
                }
                .buttonStyle(.plain)
                .help("Set a photo for \(p.name)")
                .accessibilityLabel("Set a photo for \(p.name)")

                VStack(alignment: .leading, spacing: 1) {
                    PersonEditableText(
                        label: "Name", value: p.name, font: .system(size: 20, weight: .semibold), allowEmpty: false
                    ) { try await store.saveField("name", $0, old: p.name) }
                    PersonEditableText(label: "Role", value: p.position) {
                        try await store.saveField("position", $0, old: p.position)
                    }
                    PersonEditableText(label: "Company", value: p.company, color: Palette.muted) {
                        try await store.saveField("company", $0, old: p.company)
                    }
                    if !location.isEmpty {
                        Label {
                            Text(verbatim: location)
                        } icon: {
                            Image(systemName: "mappin.and.ellipse")
                        }
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .labelStyle(.titleAndIcon)
                        .padding(.top, 3)
                    }
                    if !tags.isEmpty {
                        HStack(spacing: 6) {
                            ForEach(tags, id: \.text) { tag in
                                PersonTag(text: tag.text, color: tag.color, dot: tag.dot)
                            }
                        }
                        .padding(.top, 5)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            if !p.blurb.isEmpty {
                Text(verbatim: p.blurb)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }

            actions
            PersonVitals(p: p)
        }
        .confirmationDialog(
            dialogTitle, isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }),
            presenting: pending
        ) { action in
            confirmButtons(action)
        } message: { action in
            Text(dialogMessage(action))
        }
        .sheet(isPresented: $showDraft) {
            PersonDraftSheet(store: store, name: p.name)
        }
        // "Draft a Message…" from the table's right-click menu (people.js:1320).
        .onChange(of: store.pendingDraft, initial: true) { _, pending in
            guard pending else { return }
            store.pendingDraft = false
            if !p.deleted { showDraft = true }
        }
        .alert("New list", isPresented: $naming) {
            TextField("New list — e.g. Team dinner", text: $newListName)
            Button("Create") {
                let name = newListName
                Task { await store.createList(named: name) }
            }
            .disabled(newListName.trimmingCharacters(in: .whitespaces).isEmpty)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("\(p.name) is added to it. Start a name with an emoji and it becomes the list icon.")
        }
    }

    // MARK: Pieces

    private var location: String {
        let place = Display.locationString(metro: p.homeMetro, city: p.homeCity)
        return [place, p.homeCountry].filter { !$0.isEmpty }.joined(separator: ", ")
    }

    private var tags: [(text: String, color: Color, dot: Color?)] {
        var out: [(String, Color, Color?)] = []
        if Display.showsCategoryTag(p.category) {
            out.append((Display.categoryLabel(p.category), Palette.ink, Display.categoryColor(p.category)))
        }
        if p.excluded { out.append(("excluded", Palette.danger, nil)) }
        if p.deleted { out.append(("deleted", Palette.danger, nil)) }
        return out
    }

    /// The action row; in a narrow column "Draft message" shortens to "Draft" rather than
    /// being cut off.
    private var actions: some View {
        ViewThatFits(in: .horizontal) {
            actionRow(draftTitle: "Draft message")
            actionRow(draftTitle: "Draft")
        }
    }

    private func actionRow(draftTitle: String) -> some View {
        HStack(spacing: 6) {
            quickLinks
            Spacer(minLength: 4)
            Button {
                showDraft = true
            } label: {
                Label(draftTitle, systemImage: "pencil").labelStyle(.titleAndIcon)
            }
            .buttonStyle(PersonInkButtonStyle())
            .fixedSize()
            .help("Draft a message to \(p.name)")
            .disabled(p.deleted)

            listMenu
            moreMenu
        }
        .controlSize(.regular)
    }

    @ViewBuilder private var quickLinks: some View {
        let email = p.identifiers.first { $0.kind == "email" }
        let phone = p.identifiers.first { $0.kind == "phone" }
        HStack(spacing: 4) {
            if let url = URL(string: p.linkedinURL), !p.linkedinURL.isEmpty {
                quickLink(help: "Open the LinkedIn profile", url: url) {
                    Text("in").font(.system(size: 13, weight: .bold)).foregroundStyle(Display.channelColor(.linkedin))
                }
            }
            if let email, let url = URL(string: "mailto:\(email.value)") {
                quickLink(help: "Write to \(email.value)", url: url) {
                    Image(systemName: "envelope").foregroundStyle(Display.channelColor(.email))
                }
            }
            if let phone, let url = URL(string: "tel:\(phone.value)") {
                quickLink(help: "Call \(phone.value)", url: url) {
                    Image(systemName: "phone").foregroundStyle(Display.channelColor(.imessage))
                }
            }
        }
    }

    private func quickLink<L: View>(help: String, url: URL, @ViewBuilder label: () -> L) -> some View {
        Button {
            openURL(url)
        } label: {
            label().frame(width: 18, height: 16)
        }
        .buttonStyle(.bordered)
        .help(help)
        .accessibilityLabel(help)
    }

    private var listMenu: some View {
        let inIDs = Set(store.memberships.map(\.id))
        let addable = store.lists.filter { !inIDs.contains($0.id) }
        return Menu {
            if !store.listsLoaded {
                Text("Loading lists…")
            } else {
                if addable.isEmpty {
                    Text(store.lists.isEmpty ? "No lists yet" : "Already in every list")
                }
                ForEach(addable) { list in
                    Button(PersonListRow.title(list)) { Task { await store.addToList(list) } }
                }
                Divider()
                Button("New List…") {
                    newListName = ""
                    naming = true
                }
            }
        } label: {
            Label("List", systemImage: "plus")
        }
        .menuStyle(.button)
        .buttonStyle(.bordered)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Add \(p.name) to a list")
        .disabled(p.deleted)
    }

    private var moreMenu: some View {
        Menu {
            ForEach(PersonMenuItem.sections(for: p), id: \.title) { section in
                Section(section.title) {
                    ForEach(section.items) { item in menuButton(item) }
                }
            }
        } label: {
            Image(systemName: "ellipsis")
                .frame(width: 18, height: 16)
        }
        .menuStyle(.button)
        .buttonStyle(.bordered)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("More actions")
        .accessibilityLabel("More actions")
    }

    @ViewBuilder private func menuButton(_ item: PersonMenuItem) -> some View {
        if case .category(let cat) = item.action {
            Toggle(
                item.title,
                isOn: Binding(
                    get: { p.category == cat },
                    set: { on in if on { Task { await store.setCategory(cat) } } }))
        } else {
            Button(role: item.destructive ? .destructive : nil) {
                perform(item.action)
            } label: {
                Text(item.title)
                if let note = item.note { Text(note) }
            }
        }
    }

    private func perform(_ action: PersonMenuItem.Action) {
        switch action {
        // The merge sheet belongs to the Data part (MergeSheet), with this person as the
        // record that survives (people.js:1450-1464).
        case .merge: app.mergeRequest = MergeRequest(firstID: p.id)
        case .category(let cat): Task { await store.setCategory(cat) }
        case .setPhoto: pickPhoto()
        case .removePhoto: Task { await store.removePhoto() }
        case .restore: Task { _ = await store.setDeleted(false) }
        case .purge: pending = .purge
        case .exclude: pending = .exclude
        case .unexclude: pending = .unexclude
        case .delete: pending = .delete
        }
    }

    // MARK: Confirmations

    private var dialogTitle: String {
        switch pending {
        case .exclude: "Exclude \(p.name)?"
        case .unexclude: "Bring \(p.name) back?"
        case .delete: "Delete \(p.name)?"
        case .purge: "Erase \(p.name) permanently?"
        case nil: ""
        }
    }

    private func dialogMessage(_ action: PendingAction) -> String {
        switch action {
        case .exclude:
            "They are hidden everywhere, and every import skips this name. You can bring them back from the Excluded view."
        case .unexclude:
            "They come back into People, and imports collect them again."
        case .delete:
            "This is reversible: restore them from the Deleted filter."
        case .purge:
            "This cannot be undone. Notes, photo and archived messages go too."
        }
    }

    @ViewBuilder private func confirmButtons(_ action: PendingAction) -> some View {
        switch action {
        case .exclude:
            Button("Exclude", role: .destructive) { Task { await store.setExcluded(true) } }
        case .unexclude:
            Button("Unexclude") { Task { await store.setExcluded(false) } }
        case .delete:
            Button("Delete", role: .destructive) {
                Task {
                    if await store.setDeleted(true) { app.selectedPersonID = nil }
                }
            }
        case .purge:
            Button("Yes — erase permanently", role: .destructive) {
                Task {
                    let r = await store.purge()
                    if r.ok {
                        // The page closes: the line (and any skip-list warning) goes to the
                        // app-wide message the People screen shows.
                        DataSettingsBus.shared.announce(r.message)
                        app.selectedPersonID = nil
                    }
                }
            }
        }
        Button("Cancel", role: .cancel) {}
    }

    private func pickPhoto() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = "Choose a photo for \(p.name)"
        panel.prompt = "Set Photo"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard let data = try? Data(contentsOf: url) else {
            store.flash("Could not read that file.")
            return
        }
        Task { await store.setPhoto(data) }
    }
}

// MARK: - Vital tiles

/// Closeness · Last touch · Connected (or First contact) · Messages (people.js:1225-1303).
struct PersonVitals: View {
    let p: PersonDetail
    @State private var showWhy = false
    @State private var width: CGFloat = 0

    /// Four tiles in a row when they fit at a readable size, else two rows of two.
    private var columns: [GridItem] {
        Array(repeating: GridItem(.flexible(), spacing: 8, alignment: .top), count: width >= 470 ? 4 : 2)
    }

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 8) {
            Button {
                showWhy.toggle()
            } label: {
                tile(label: "Closeness", info: true) {
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Circle().fill(tier.color).frame(width: 8, height: 8)
                        Text(verbatim: "\(p.strength)").font(.system(size: Metrics.fsTitle, weight: .semibold))
                        Text(tier.label).font(.system(size: Metrics.fsUI)).foregroundStyle(Palette.muted)
                    }
                } sub: {
                    if Display.showsNoMessages(p.counts) { Text("No messages") }
                }
            }
            .buttonStyle(.plain)
            .help("Why this score?")
            .popover(isPresented: $showWhy, arrowEdge: .bottom) { PersonWhyScore(p: p) }

            tile(label: "Last touch") {
                if p.last.isEmpty {
                    Text("No contact yet").foregroundStyle(Palette.muted)
                } else {
                    Text(verbatim: Display.relDate(p.last))
                        .foregroundStyle(Display.staleness(lastContact: p.last).color)
                }
            } sub: {
                if !p.last.isEmpty { Text(verbatim: Display.fmtDay(p.last)) }
            }

            tile(label: p.connectedOn.isEmpty ? "First contact" : "Connected") {
                let since = p.connectedOn.isEmpty ? p.firstContact : p.connectedOn
                if since.isEmpty {
                    Text("Not known").foregroundStyle(Palette.faint)
                } else {
                    Text(verbatim: Display.fmtMonthYear(since))
                }
            } sub: {
                EmptyView()
            }

            tile(label: "Messages") {
                Text(verbatim: Display.number(p.counts.total))
            } sub: {
                let used = p.counts.used
                if !used.isEmpty {
                    Text(verbatim: used.count == 1 ? Display.channelLabel(used[0]) : PersonWords.count(used.count, "channel"))
                }
            }
            // The per-channel counts the web tile lists (people.js:1289-1300); the Channels
            // section below has them in full.
            .help(p.counts.used.map { "\(Display.channelLabel($0)) \(Display.number(p.counts[$0]))" }.joined(separator: " · "))
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }

    private var tier: Display.Tier { Display.Tier(label: p.strengthLabel) }

    private func tile<V: View, S: View>(
        label: String, info: Bool = false, @ViewBuilder value: () -> V, @ViewBuilder sub: () -> S
    ) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 3) {
                Text(label)
                if info { Image(systemName: "info.circle").imageScale(.small) }
            }
            .font(.system(size: Metrics.fsOverline, weight: .medium))
            .foregroundStyle(Palette.muted)
            value()
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            sub()
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, minHeight: 66, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.line, lineWidth: 1))
        .contentShape(Rectangle())
    }
}

/// "Why this score": the closeness model's inputs as bars (people.js:1103-1129).
struct PersonWhyScore: View {
    let p: PersonDetail

    var body: some View {
        let s = p.strengthParts
        VStack(alignment: .leading, spacing: 8) {
            Text("Why \(p.strength) — bond × warmth")
                .font(.system(size: Metrics.fsUI, weight: .semibold))
            meter("Shared days", s.days, hint: "\(Display.number(s.activeDays)) days")
            meter("Tenure", s.tenure)
            meter("Volume", s.volume, hint: "\(Display.number(s.totalMessages)) msgs")
            meter("Curation", s.depth)
            meter("Reciprocity", s.reciprocity, hint: s.reciprocity <= 0.36 ? "one-way" : nil)
            meter("Warmth", s.warmth, hint: warmthHint(s.daysSince))
            Text(
                "Bond = shared days · tenure · volume · curation, scaled by reciprocity and \(PersonWords.count(s.channelsUsed, "channel")). Warmth is recency against your own rhythm — it cools a score but never erases history."
            )
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(width: 300)
    }

    private func warmthHint(_ days: Int?) -> String {
        guard let days else { return "no touchpoint yet" }
        if days == 0 { return "in touch today" }
        return "quiet \(days) day\(days == 1 ? "" : "s")"
    }

    private func meter(_ label: String, _ value: Double, hint: String? = nil) -> some View {
        let v = min(1, max(0, value))
        return HStack(spacing: 8) {
            Text(label).frame(width: 82, alignment: .leading)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.line)
                    Capsule().fill(Palette.accent).frame(width: g.size.width * v)
                }
            }
            .frame(height: 5)
            Text(verbatim: hint ?? "\(Int((v * 100).rounded()))%")
                .foregroundStyle(Palette.muted)
                .frame(width: 92, alignment: .trailing)
                .lineLimit(1)
        }
        .font(.system(size: Metrics.fsOverline))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Going cold

/// The next-action card: shown only when the engine sends `next_action` (people.js:1512-1553).
struct PersonNextCard: View {
    let store: PersonStore
    let action: NextAction
    @State private var showDraft = false

    var body: some View {
        if store.snoozed {
            Text("Snoozed. This person is off the list for a while.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("Going cold")
                    .font(.system(size: Metrics.fsOverline, weight: .semibold))
                    .foregroundStyle(Display.Staleness.cool.color)
                Text("Last exchange \(PersonWords.count(action.daysSilent, "day")) ago.")
                    .font(.system(size: Metrics.fsUI, weight: .medium))
                    .foregroundStyle(Palette.ink)
                if !evidence.isEmpty {
                    Text(verbatim: evidence)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(2)
                }
                HStack(spacing: 6) {
                    Button("Draft a message") { showDraft = true }
                        .buttonStyle(.bordered)
                    Button("Not now") { Task { await store.snooze() } }
                        .buttonStyle(.borderless)
                }
                .controlSize(.small)
                .padding(.top, 2)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.goldSoft))
            .sheet(isPresented: $showDraft) {
                PersonDraftSheet(store: store, name: store.person?.name ?? "")
            }
        }
    }

    private var evidence: String {
        var bits: [String] = []
        if !action.lastSnippet.isEmpty { bits.append("“\(action.lastSnippet)”") }
        if !action.lastChannel.isEmpty { bits.append(Display.channelLabel(action.lastChannel)) }
        return bits.joined(separator: " · ")
    }
}

// MARK: - Draft a message

/// Asks the engine to write a message to this person (AI, costs money: only on a click).
/// From the web page's DraftModal (static/app/ui.js:425-471).
struct PersonDraftSheet: View {
    let store: PersonStore
    let name: String
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var meta: BriefMeta?
    @State private var busy = true
    @State private var error: String?
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Message for \(name.split(separator: " ").first.map(String.init) ?? name)")
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
            if busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Writing the message…").foregroundStyle(Palette.muted)
                }
                .frame(maxWidth: .infinity, minHeight: 120, alignment: .center)
            } else if let error {
                Text(verbatim: error)
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, minHeight: 60, alignment: .topLeading)
            } else {
                TextEditor(text: $text)
                    .font(.system(size: Metrics.fsUI))
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel2))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Palette.line))
                    .frame(minHeight: 140)
                if let meta { PersonReceipt(meta: meta) }
            }
            HStack {
                Button {
                    Task { await generate() }
                } label: {
                    Label("Write another", systemImage: "arrow.clockwise")
                }
                .disabled(busy)
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(copied ? "Copied" : "Copy") {
                    PersonClipboard.copy(text)
                    copied = true
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .font(.system(size: Metrics.fsUI))
        .padding(20)
        .frame(width: 460)
        .task { await generate() }
    }

    private func generate() async {
        busy = true
        error = nil
        copied = false
        do {
            let r = try await store.draftMessage()
            text = r.message
            meta = r.meta
        } catch is CancellationError {
        } catch {
            self.error = PersonStore.say(error)
        }
        busy = false
    }
}

/// "From 12 messages · 2 channels · built Oct 14, 2026" (+ "3 new messages since").
/// From static/app/ui.js:333-347.
struct PersonReceipt: View {
    let meta: BriefMeta
    var body: some View {
        let bits = [
            "From \(PersonWords.count(meta.messages, "message"))",
            PersonWords.count(meta.channels, "channel"),
            meta.builtAt.isEmpty ? "" : "built \(Display.fmtDay(meta.builtAt))",
        ].filter { !$0.isEmpty }
        HStack(spacing: 8) {
            Text(verbatim: bits.joined(separator: " · "))
                .foregroundStyle(Palette.muted)
            if meta.newerMessages > 0 {
                Text(verbatim: "\(PersonWords.count(meta.newerMessages, "new message", "new messages")) since")
                    .foregroundStyle(Display.Staleness.warm.color)
            }
        }
        .font(.system(size: Metrics.fsOverline))
    }
}
