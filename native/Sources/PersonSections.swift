import AppKit
import SwiftUI

// The person page's blocks below the header: Coming up, Story, Details, Channels, Phone and
// email, Dates, Lists and Mutual connections. Words and rules from static/app/people.js
// (Details 1790-1845, Story 1660-1693, dates 1557-1631, mutuals 1633-1657; contract §8.7).

// MARK: - Coming up

/// The next dates on this person's calendar: birthdays, anniversaries, planned meetings.
struct PersonComingUp: View {
    let dates: [DateRow]
    static let shown = 4

    var body: some View {
        let upcoming = dates.filter { !$0.nextOccurrence.isEmpty }.sorted { $0.daysUntil < $1.daysUntil }
        if !upcoming.isEmpty {
            PersonSection(title: "Coming up") {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(upcoming.prefix(Self.shown)) { d in
                        HStack(spacing: 8) {
                            Image(systemName: Display.dateKindSymbol(d.kind))
                                .foregroundStyle(Palette.muted)
                                .frame(width: 18)
                            Text(verbatim: d.reason)
                                .foregroundStyle(Palette.ink)
                                .lineLimit(1)
                            Spacer(minLength: 8)
                            Text(verbatim: Display.whenShort(d.daysUntil))
                                .foregroundStyle(d.daysUntil <= 1 ? Display.Staleness.fresh.color : Palette.muted)
                                .monospacedDigit()
                        }
                        .font(.system(size: Metrics.fsUI))
                        .help(Display.fmtDay(d.nextOccurrence))
                    }
                    if upcoming.count > Self.shown {
                        Text("\(upcoming.count - Self.shown) more under Dates below.")
                            .font(.system(size: Metrics.fsOverline))
                            .foregroundStyle(Palette.muted)
                    }
                }
            }
        }
    }
}

// MARK: - Story

struct PersonStory: View {
    let store: PersonStore
    let p: PersonDetail
    @State private var busy = false

    var body: some View {
        PersonSection(title: "Story", caption: p.storySource == "ai" ? "AI written" : "From your data") {
            Button {
                busy = true
                Task {
                    await store.rewriteStory()
                    busy = false
                }
            } label: {
                if busy {
                    Text("writing…")
                } else {
                    Label((p.briefMeta?.newerMessages ?? 0) > 0 ? "rebuild" : "rewrite", systemImage: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .disabled(busy || p.deleted)
            .help("Rewrite with AI from the latest data")
        } content: {
            VStack(alignment: .leading, spacing: 6) {
                if p.story.isEmpty {
                    PersonEmptyLine(text: "No story yet. Import your conversations in Data. Or write how you met in Details.")
                } else {
                    Text(Self.markdown(p.story))
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.ink)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if let meta = p.briefMeta { PersonReceipt(meta: meta) }
            }
        }
    }

    /// The story is markdown-lite (bold, italic, code, links); line breaks are kept.
    static func markdown(_ s: String) -> AttributedString {
        (try? AttributedString(
            markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace, failurePolicy: .returnPartiallyParsedIfPossible)))
            ?? AttributedString(s)
    }
}

// MARK: - Details

/// Key/value rows you click to edit. An empty value shows the field's own name as ghost
/// text, because seeing what is missing is the point (people.js:1790-1845).
struct PersonDetails: View {
    let store: PersonStore
    let p: PersonDetail

    var body: some View {
        PersonSection(title: "Details") {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 7) {
                row("How you met") {
                    PersonEditableText(
                        label: "How you met", value: p.howWeMet, ghost: "Where you met \(store.firstName)", multiline: true
                    ) { try await store.saveHowWeMet($0) }
                }
                row("Topics") {
                    if p.topics.isEmpty {
                        Text("Topics").foregroundStyle(Palette.faint)
                    } else {
                        PersonFlow(spacing: 4) {
                            ForEach(p.topics, id: \.self) { PersonTag(text: $0, color: Palette.ink) }
                        }
                    }
                }
                field("City", "home_city", p.homeCity)
                field("Metro", "home_metro", p.homeMetro)
                field("Country", "home_country", p.homeCountry)
                field("Role type", "role_type", p.roleType)
                row("Blurb") {
                    PersonEditableText(label: "Blurb", value: p.blurb, multiline: true) {
                        try await store.saveField("blurb", $0, old: p.blurb)
                    }
                }
            }
            .font(.system(size: Metrics.fsUI))
        }
    }

    private func field(_ label: String, _ key: String, _ value: String) -> some View {
        row(label) {
            PersonEditableText(label: label, value: value) { try await store.saveField(key, $0, old: value) }
        }
    }

    private func row<V: View>(_ label: String, @ViewBuilder value: () -> V) -> some View {
        GridRow {
            Text(label)
                .foregroundStyle(Palette.muted)
                .gridColumnAlignment(.trailing)
                .frame(minWidth: 70, alignment: .trailing)
            value()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// MARK: - Channels

/// Per channel: its name and symbol, how many messages, and when the last one was.
struct PersonChannels: View {
    let store: PersonStore
    let p: PersonDetail

    var body: some View {
        let used = p.counts.used
        PersonSection(title: "Channels") {
            if used.isEmpty {
                PersonEmptyLine(text: "No messages yet. Import WhatsApp, iMessage or email in Data and they show here.")
            } else {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 6) {
                    ForEach(used, id: \.self) { channel in
                        GridRow {
                            Image(systemName: Display.channelSymbol(channel))
                                .foregroundStyle(Display.channelColor(channel))
                                .frame(width: 18)
                            Text(verbatim: Display.channelLabel(channel))
                                .foregroundStyle(Palette.ink)
                                .lineLimit(1)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(verbatim: PersonWords.count(p.counts[channel], "message"))
                                .foregroundStyle(Palette.muted)
                                .monospacedDigit()
                                .fixedSize()
                                .gridColumnAlignment(.trailing)
                            lastText(channel)
                                .fixedSize()
                                .gridColumnAlignment(.trailing)
                                .frame(minWidth: 64, alignment: .trailing)
                        }
                    }
                }
                .font(.system(size: Metrics.fsUI))
                .task(id: p.id) { await store.loadChannelLast() }
                if let problem = store.channelLastProblem {
                    PersonEmptyLine(text: "The dates of the newest messages could not be read. \(problem)")
                }
            }
        }
    }

    @ViewBuilder private func lastText(_ channel: Channel) -> some View {
        if let ts = store.channelLast[channel] {
            Text(verbatim: Display.relDate(ts))
                .foregroundStyle(Display.staleness(lastContact: ts).color)
                .help("Last message \(Display.fmtDay(ts))")
        } else if store.channelLastLoaded {
            Text("—").foregroundStyle(Palette.faint)
        } else {
            ProgressView().controlSize(.mini)
        }
    }
}

// MARK: - Phone, email and profiles

struct PersonIdentifiers: View {
    let store: PersonStore
    let p: PersonDetail
    @Environment(\.openURL) private var openURL
    @State private var adding = false
    @State private var kind = "phone"
    @State private var value = ""
    @State private var error: String?
    @State private var busy = false
    @FocusState private var fieldFocused: Bool

    var body: some View {
        PersonSection(title: "Phone and email") {
            Button {
                adding.toggle()
                error = nil
                if adding { fieldFocused = true }
            } label: {
                Label("number / email", systemImage: "plus")
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .disabled(adding)
        } content: {
            VStack(alignment: .leading, spacing: 4) {
                if p.identifiers.isEmpty && p.linkedinURL.isEmpty && !adding {
                    PersonEmptyLine(text: "No phone number or email yet. Add one with + above.")
                }
                ForEach(p.identifiers) { ident in
                    identifierRow(ident)
                }
                if !p.linkedinURL.isEmpty, let url = URL(string: p.linkedinURL) {
                    linkedInRow(url)
                }
                if adding { composer }
            }
        }
    }

    private func identifierRow(_ ident: Identifier) -> some View {
        let isPhone = ident.kind == "phone"
        let noun = isPhone ? "number" : "email"
        let url = URL(string: (isPhone ? "tel:" : "mailto:") + ident.value)
        return PersonHoverRow {
            Button {
                PersonClipboard.copy(ident.value)
                store.flash("Copied \(noun)")
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: isPhone ? "phone" : "envelope")
                        .foregroundStyle(Palette.muted)
                        .frame(width: 18)
                    Text(verbatim: ident.value)
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Copy \(ident.value)")
        } trailing: {
            Button {
                Task { await store.removeIdentifier(ident) }
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(Palette.faint)
            }
            .buttonStyle(.plain)
            .help("Remove \(ident.value)")
            .accessibilityLabel("Remove \(ident.value)")
        }
        .contextMenu {
            Button("Copy") {
                PersonClipboard.copy(ident.value)
                store.flash("Copied \(noun)")
            }
            if let url {
                Button(isPhone ? "Call \(ident.value)" : "Write to \(ident.value)") { openURL(url) }
            }
            Divider()
            Button("Remove") { Task { await store.removeIdentifier(ident) } }
        }
    }

    private func linkedInRow(_ url: URL) -> some View {
        let shown = p.linkedinURL.replacingOccurrences(of: #"^https?://(www\.)?"#, with: "", options: .regularExpression)
        return PersonHoverRow {
            Button {
                openURL(url)
            } label: {
                HStack(spacing: 8) {
                    Text("in")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Display.channelColor(.linkedin))
                        .frame(width: 18)
                    Text(verbatim: shown)
                        .foregroundStyle(Palette.accentInk)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open the LinkedIn profile")
        } trailing: {
            EmptyView()
        }
        .contextMenu {
            Button("Open in Browser") { openURL(url) }
            Button("Copy Link") {
                PersonClipboard.copy(p.linkedinURL)
                store.flash("Copied link")
            }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Picker("Kind", selection: $kind) {
                    Text("phone").tag("phone")
                    Text("email").tag("email")
                }
                .labelsHidden()
                .fixedSize()
                TextField(kind == "phone" ? "Phone number" : "Email address", text: $value, prompt: Text(kind == "phone" ? "+46…" : "name@…"))
                    .textFieldStyle(.roundedBorder)
                    .focused($fieldFocused)
                    .onSubmit { add() }
                    .onExitCommand { close() }
                Button("Add", action: add)
                    .disabled(busy || value.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("Cancel", action: close)
                    .buttonStyle(.borderless)
            }
            .controlSize(.small)
            if let error {
                Text(verbatim: error).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.danger)
            }
        }
        .padding(.top, 4)
    }

    private func close() {
        adding = false
        value = ""
        error = nil
    }

    private func add() {
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, !busy else { return }
        busy = true
        Task {
            do {
                try await store.addIdentifier(kind: kind, value: v)
                close()
            } catch {
                self.error = PersonStore.say(error)
            }
            busy = false
        }
    }
}

// MARK: - Dates

struct PersonDates: View {
    let store: PersonStore
    let p: PersonDetail
    @State private var adding = false
    @State private var kind = "birthday"
    @State private var day = Date()
    @State private var label = ""
    @State private var yearUnknown = false
    @State private var showAll = false
    @State private var error: String?
    @State private var busy = false

    static let firstShown = 8

    var body: some View {
        PersonSection(title: "Dates") {
            Button {
                adding.toggle()
                error = nil
            } label: {
                Label("birthday / meeting", systemImage: "plus")
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .disabled(adding)
        } content: {
            VStack(alignment: .leading, spacing: 4) {
                if p.dates.isEmpty && !adding {
                    PersonEmptyLine(text: "No dates yet. Add a birthday or a meeting with + above.")
                }
                let shown = showAll ? p.dates : Array(p.dates.prefix(Self.firstShown))
                ForEach(shown) { d in dateRow(d) }
                if p.dates.count > Self.firstShown {
                    Button(showAll ? "Show fewer" : "Show all \(p.dates.count)") { showAll.toggle() }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                }
                if adding { composer }
            }
        }
    }

    private func dateRow(_ d: DateRow) -> some View {
        PersonHoverRow {
            HStack(spacing: 8) {
                Image(systemName: Display.dateKindSymbol(d.kind))
                    .foregroundStyle(Palette.muted)
                    .frame(width: 18)
                Text(verbatim: d.reason.isEmpty ? d.onDate : d.reason)
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
            }
            .help(d.onDate + (d.recurring ? " · every year" : ""))
        } trailing: {
            Button {
                Task { await store.removeDate(d) }
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(Palette.faint)
            }
            .buttonStyle(.plain)
            .help("Remove \(d.reason)")
            .accessibilityLabel("Remove \(d.reason)")
        }
        .contextMenu {
            Button("Copy Date") { PersonClipboard.copy(d.onDate) }
            Divider()
            Button("Remove") { Task { await store.removeDate(d) } }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Picker("Date type", selection: $kind) {
                    Text("birthday").tag("birthday")
                    Text("anniversary").tag("anniversary")
                    Text("meeting").tag("meeting")
                    Text("date").tag("custom")
                }
                .labelsHidden()
                .fixedSize()
                DatePicker("Date", selection: $day, displayedComponents: .date)
                    .labelsHidden()
                    .datePickerStyle(.field)
                    .fixedSize()
                if kind == "birthday" || kind == "anniversary" {
                    Toggle("year unknown", isOn: $yearUnknown)
                        .toggleStyle(.checkbox)
                        .fixedSize()
                }
            }
            HStack(spacing: 6) {
                if kind != "birthday" {
                    TextField(kind == "meeting" ? "Where or what" : "Label", text: $label, prompt: Text(kind == "meeting" ? "where / what" : "label"))
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { add() }
                } else {
                    Spacer()
                }
                Button("Add", action: add).disabled(busy)
                Button("Cancel") {
                    adding = false
                    error = nil
                }
                .buttonStyle(.borderless)
            }
            if let error {
                Text(verbatim: error).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.danger)
            }
        }
        .controlSize(.small)
        .padding(.top, 4)
    }

    /// The picked day as YYYY-MM-DD in the person's own calendar. The engine refuses any
    /// other format with a 400 (NC-338), and PersonStore.addDate checks it again, so the
    /// page only ever sends a real date.
    static func isoDay(_ date: Date) -> String {
        let c = Calendar(identifier: .gregorian).dateComponents(in: .current, from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    private func add() {
        guard !busy else { return }
        busy = true
        let dated = kind == "birthday" || kind == "anniversary"
        Task {
            do {
                try await store.addDate(
                    kind: kind, onDate: Self.isoDay(day), label: kind == "birthday" ? "" : label.trimmingCharacters(in: .whitespaces),
                    yearKnown: !(dated && yearUnknown))
                adding = false
                label = ""
                yearUnknown = false
                error = nil
            } catch {
                self.error = PersonStore.say(error)
            }
            busy = false
        }
    }
}

// MARK: - Lists

struct PersonLists: View {
    let store: PersonStore
    @AppEnvironment private var app

    var body: some View {
        PersonSection(title: "Lists") {
            VStack(alignment: .leading, spacing: 4) {
                if !store.listsLoaded {
                    ProgressView().controlSize(.small)
                } else if let error = store.listsError {
                    PersonEmptyLine(text: "Could not read the lists. \(error)")
                } else if store.memberships.isEmpty {
                    PersonEmptyLine(text: "Not in any list. Use List at the top to add them to one.")
                } else {
                    ForEach(store.memberships) { m in
                        PersonListRow(membership: m) { app.openList(m.list.id) }
                            .contextMenu {
                                Button("Open List") { app.openList(m.list.id) }
                                Divider()
                                Button("Remove from List") { Task { await store.removeFromList(m.list) } }
                            }
                    }
                }
                if store.listsSkipped > 0 {
                    Text("Only the newest \(PersonStore.listScanCap) lists were checked.")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
        }
    }
}

struct PersonListRow: View {
    let membership: PersonListMembership
    let open: () -> Void

    static func title(_ list: CRMList) -> String {
        let (icon, title) = Display.listIcon(list.name)
        return icon.map { "\($0) \(title)" } ?? title
    }

    var body: some View {
        let (icon, title) = Display.listIcon(membership.list.name)
        PersonHoverRow {
            Button(action: open) {
                HStack(spacing: 8) {
                    if let icon {
                        Text(verbatim: icon).frame(width: 18)
                    } else {
                        Image(systemName: "diamond").foregroundStyle(Palette.muted).frame(width: 18)
                    }
                    Text(verbatim: title).foregroundStyle(Palette.ink).lineLimit(1)
                    Spacer(minLength: 8)
                    Text(verbatim: Display.listStatusLabel(membership.status))
                        .foregroundStyle(Palette.muted)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Palette.faint)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open “\(title)” in Lists")
        } trailing: {
            EmptyView()
        }
    }
}

// MARK: - Mutual connections

/// From the LinkedIn profile, parsed by a model, so it says it is unverified. Hidden when
/// there is nothing (people.js:1633-1657).
struct PersonMutuals: View {
    let mutuals: Mutuals

    var body: some View {
        if !mutuals.isEmpty {
            PersonSection(title: "Mutual connections", caption: "LinkedIn · unverified") {
                VStack(alignment: .leading, spacing: 6) {
                    if !mutuals.mutualNames.isEmpty {
                        Text(verbatim: mutuals.mutualNames.joined(separator: ", "))
                            .foregroundStyle(Palette.ink)
                    }
                    ForEach(Array(mutuals.sharedHistory.enumerated()), id: \.offset) { _, line in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("•").foregroundStyle(Palette.muted)
                            Text(verbatim: line).foregroundStyle(Palette.ink)
                        }
                    }
                    if !mutuals.recentActivity.isEmpty {
                        Text(verbatim: "Lately: \(mutuals.recentActivity)").foregroundStyle(Palette.muted)
                    }
                }
                .font(.system(size: Metrics.fsUI))
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            }
        }
    }
}

// MARK: - Shared row pieces

/// A row whose trailing control (remove) shows on hover.
struct PersonHoverRow<Leading: View, Trailing: View>: View {
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 6) {
            leading
            Spacer(minLength: 4)
            trailing.opacity(hovering ? 1 : 0)
        }
        .font(.system(size: Metrics.fsUI))
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .background(RoundedRectangle(cornerRadius: 5).fill(hovering ? Palette.hover : Color.clear))
        .padding(.horizontal, -6)
        .onHover { hovering = $0 }
    }
}

/// Wraps small tags onto as many lines as they need.
struct PersonFlow: Layout {
    var spacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, line: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                y += line + spacing
                x = 0
                line = 0
            }
            x += size.width + spacing
            line = max(line, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, width), height: y + line)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, line: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                y += line + spacing
                x = bounds.minX
                line = 0
            }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            line = max(line, size.height)
        }
    }
}
