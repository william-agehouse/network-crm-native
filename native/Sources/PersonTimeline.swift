import AppKit
import SwiftUI

// The timeline. "All" is the web page's own timeline: notes you wrote, the person's
// conversations and their dated milestones in one list, newest first (people.js:2044-2127).
// The other views narrow it: Notes (with milestones), Conversations, and the raw Messages.
// Conversations and messages load only when shown, a page at a time. Rules from
// static/app/people.js 1847-2127 (contract §4 history/episodes, §8.7 Timeline).

enum PersonTimelineTab: String, CaseIterable, Identifiable, Sendable {
    case all = "All", notes = "Notes", conversations = "Conversations", messages = "Messages"
    var id: Self { self }
}

struct PersonTimeline: View {
    let store: PersonStore
    let p: PersonDetail

    var body: some View {
        @Bindable var store = store
        PersonSection(title: "Timeline") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Show", selection: $store.timelineTab) {
                    ForEach(PersonTimelineTab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                // Not fixedSize: four segments at their ideal width are wider than a narrow
                // page and would push the whole page past the window's edge.
                .frame(maxWidth: 420, alignment: .leading)

                if store.timelineTab == .all || store.timelineTab == .notes {
                    PersonNoteComposer(store: store).id(PersonNoteComposer.scrollID)
                }
                switch store.timelineTab {
                case .all: PersonAllTimeline(store: store, p: p)
                case .notes: PersonNotes(store: store, p: p)
                case .conversations: PersonEpisodes(store: store, p: p)
                case .messages: PersonMessages(store: store, p: p)
                }
            }
        }
    }
}

// MARK: - Entries

/// One line of the timeline: a note you wrote, a conversation, or a dated milestone.
private enum PersonEntry: Identifiable {
    case note(Note)
    case episode(Episode)
    /// `uid` keeps two milestones with the same day and label apart (two calendar
    /// meetings called "Lunch" on one day would otherwise share an identity).
    case event(uid: String, icon: String, label: String, source: String, date: String)

    var id: String {
        switch self {
        case .note(let n): "n\(n.id)"
        case .episode(let e): "c\(e.id)"
        case .event(let uid, _, _, _, _): "e\(uid)"
        }
    }

    /// Date-only stamps sort at the end of their day, so a same-day milestone sits at the
    /// head of that day rather than under its first conversation (people.js:2084-2096, tsKey).
    var key: String {
        let raw: String
        switch self {
        case .note(let n): raw = n.createdAt
        case .episode(let e): raw = e.tsEnd
        case .event(_, _, _, _, let date): raw = date
        }
        let v = String(raw.prefix(19))
        return v.count <= 10 ? v + " 23:59:59" : v
    }

    /// people.js:2027-2042: the LinkedIn connection, and one-off dates (not birthdays or
    /// anniversaries, which stay under Dates), captioned with where they came from.
    static func events(_ p: PersonDetail) -> [PersonEntry] {
        var out: [PersonEntry] = []
        if !p.connectedOn.isEmpty {
            out.append(.event(uid: "linkedin", icon: "link", label: "Connected on LinkedIn", source: "LinkedIn", date: p.connectedOn))
        }
        for d in p.dates where !d.recurring && d.kind != "birthday" && d.kind != "anniversary" && !d.onDate.isEmpty {
            let label = d.label.isEmpty ? (d.kind == "meeting" ? "Met in person" : "Marked date") : d.label
            out.append(
                .event(
                    uid: "d\(d.id)", icon: Display.dateKindSymbol(d.kind), label: label, source: d.source == "calendar" ? "Calendar" : "added",
                    date: d.onDate))
        }
        return out
    }

    static func newestFirst(_ entries: [PersonEntry]) -> [PersonEntry] {
        entries.sorted { $0.key > $1.key }
    }
}

private struct PersonEntryRow: View {
    let store: PersonStore
    let p: PersonDetail
    let entry: PersonEntry

    var body: some View {
        switch entry {
        case .note(let n):
            PersonNoteRow(store: store, note: n)
        case .episode(let ep):
            PersonEpisodeRow(store: store, firstName: store.firstName, fullName: p.name, ep: ep)
        case .event(_, let icon, let label, let source, let date):
            PersonEventRow(icon: icon, label: label, source: source, date: date)
        }
    }
}

// MARK: - The composer

/// "Add a note about Ada… (Enter to save)": Return saves, Shift-Return or Option-Return
/// starts a new line, as the web page's one-line field saves on Enter (people.js:2108-2114).
struct PersonNoteComposer: View {
    let store: PersonStore
    static let scrollID = "person-note-composer"
    @State private var draft = ""
    @State private var busy = false
    @State private var error: String?
    @State private var height: CGFloat = PersonNoteField.minHeight
    @State private var focused = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .bottom, spacing: 8) {
                PersonNoteField(
                    text: $draft, placeholder: "Add a note about \(store.firstName)… (Enter to save)",
                    isEnabled: !busy, focusRequest: store.noteFocusToken, height: $height, focused: $focused,
                    onSubmit: save
                )
                .frame(height: height)
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(focused ? Palette.accent : Palette.lineStrong, lineWidth: 1))

                Button(busy ? "Saving…" : "Note", action: save)
                    .disabled(busy || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .help("Save the note (Return)")
            }
            if let error {
                Text(verbatim: error).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.danger)
            }
        }
    }

    private func save() {
        let body = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty, !busy else { return }
        busy = true
        error = nil
        Task {
            do {
                try await store.addNote(body)
                draft = ""
            } catch {
                // What was typed stays in the field.
                self.error = "The note was not saved. \(PersonStore.say(error))"
            }
            busy = false
        }
    }
}

// MARK: - All: the web page's timeline

private struct PersonAllTimeline: View {
    let store: PersonStore
    let p: PersonDetail

    var body: some View {
        let entries = PersonEntry.newestFirst(
            p.notes.map(PersonEntry.note) + store.episodes.map(PersonEntry.episode) + PersonEntry.events(p))
        VStack(alignment: .leading, spacing: 0) {
            if entries.isEmpty {
                if store.episodesLoaded {
                    PersonEmptyLine(text: "No history yet. Import WhatsApp, iMessage or email in Data and this becomes a story.")
                } else {
                    ProgressView().controlSize(.small).padding(.vertical, 8)
                }
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(entries) { entry in
                        PersonEntryRow(store: store, p: p, entry: entry)
                        Divider().overlay(Palette.line)
                    }
                }
                if !store.episodesLoaded {
                    ProgressView().controlSize(.small).padding(.top, 8)
                } else if let error = store.episodesError, store.episodes.isEmpty {
                    PersonEmptyLine(text: "The conversations could not be loaded. \(error)")
                        .padding(.top, 8)
                }
                if store.episodesHasMore {
                    Button(store.episodesLoadingMore ? "Loading…" : "Earlier…") {
                        Task { await store.loadEpisodes(more: true) }
                    }
                    .disabled(store.episodesLoadingMore)
                    .padding(.top, 10)
                }
            }
        }
        .task(id: p.id) { await store.loadEpisodes(more: false) }
    }
}

// MARK: - Notes and milestones

private struct PersonNotes: View {
    let store: PersonStore
    let p: PersonDetail
    @State private var showAll = false

    /// Calendar imports can give one person dozens of meetings; the rest wait behind "Show all".
    static let firstShown = 12

    var body: some View {
        let entries = PersonEntry.newestFirst(p.notes.map(PersonEntry.note) + PersonEntry.events(p))
        VStack(alignment: .leading, spacing: 10) {
            if entries.isEmpty {
                PersonEmptyLine(text: "No notes yet. Write what you know about \(store.firstName) above, and Return saves it.")
            } else {
                let shown = showAll ? entries : Array(entries.prefix(Self.firstShown))
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(shown) { entry in
                        PersonEntryRow(store: store, p: p, entry: entry)
                        Divider().overlay(Palette.line)
                    }
                }
                if entries.count > Self.firstShown {
                    Button(showAll ? "Show fewer" : "Show all \(entries.count)") { showAll.toggle() }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                }
            }
        }
    }
}

private struct PersonNoteRow: View {
    let store: PersonStore
    let note: Note
    @State private var editing = false
    @State private var text = ""
    @State private var confirmDelete = false
    @State private var error: String?
    @State private var busy = false
    @State private var hovering = false
    @FocusState private var focused: Bool

    private var kindLabel: String {
        switch note.kind {
        case "how_we_met": "how you met"
        case "voice": "voice note"
        default: "note"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Label(kindLabel, systemImage: note.kind == "voice" ? "waveform" : "pencil")
                    .labelStyle(.titleAndIcon)
                    .foregroundStyle(Palette.muted)
                Text(verbatim: Display.fmtDay(note.createdAt))
                    .foregroundStyle(Palette.muted)
                Spacer()
                if editing {
                    Button("Cancel") {
                        editing = false
                        error = nil
                    }
                    .buttonStyle(.borderless)
                } else {
                    Group {
                        Button("Edit", action: startEdit).buttonStyle(.borderless)
                        Button("Delete") { confirmDelete = true }.buttonStyle(.borderless)
                    }
                    .opacity(hovering ? 1 : 0)
                }
            }
            .font(.system(size: Metrics.fsOverline))
            .controlSize(.small)

            if editing {
                TextEditor(text: $text)
                    .font(.system(size: Metrics.fsUI))
                    .scrollContentBackground(.hidden)
                    .focused($focused)
                    .frame(height: 70)
                    .padding(5)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Palette.accent))
                    .accessibilityLabel("Note text")
                HStack {
                    if let error {
                        Text(verbatim: error).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.danger)
                    }
                    Spacer()
                    Button(busy ? "Saving…" : "Save", action: save)
                        .controlSize(.small)
                        .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .keyboardShortcut(focused ? KeyboardShortcut(.return, modifiers: .command) : nil)
                }
            } else {
                Text(verbatim: note.body)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .padding(.vertical, 8)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Edit Note", action: startEdit)
            Button("Copy Note") { PersonClipboard.copy(note.body) }
            Divider()
            Button("Delete Note…") { confirmDelete = true }
        }
        .confirmationDialog("Delete this note?", isPresented: $confirmDelete) {
            Button("Delete", role: .destructive) { Task { await store.deleteNote(note.id) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The note leaves the timeline.")
        }
    }

    private func startEdit() {
        text = note.body
        error = nil
        editing = true
        focused = true
    }

    private func save() {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty, !busy else { return }
        guard body != note.body else {
            editing = false
            return
        }
        busy = true
        Task {
            do {
                try await store.editNote(note.id, body)
                editing = false
            } catch {
                self.error = PersonStore.say(error)
            }
            busy = false
        }
    }
}

private struct PersonEventRow: View {
    let icon: String
    let label: String
    let source: String
    let date: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon).foregroundStyle(Palette.faint).frame(width: 16)
            Text(verbatim: label).foregroundStyle(Palette.muted).lineLimit(1)
            Text(verbatim: source)
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.faint)
            Spacer(minLength: 8)
            Text(verbatim: Display.fmtDay(date))
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
        }
        .font(.system(size: Metrics.fsUI))
        .padding(.vertical, 7)
    }
}

// MARK: - Conversations (episodes)

private struct PersonEpisodes: View {
    let store: PersonStore
    let p: PersonDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if !store.episodesLoaded {
                ProgressView().controlSize(.small).padding(.vertical, 8)
            } else if let error = store.episodesError, store.episodes.isEmpty {
                PersonEmptyLine(text: "The conversations could not be loaded. \(error)")
            } else if store.episodes.isEmpty {
                PersonEmptyLine(text: "No history yet. Import WhatsApp, iMessage or email in Data and this becomes a story.")
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(store.episodes) { ep in
                        PersonEpisodeRow(store: store, firstName: store.firstName, fullName: p.name, ep: ep)
                        Divider().overlay(Palette.line)
                    }
                }
                if store.episodesHasMore {
                    Button(store.episodesLoadingMore ? "Loading…" : "Earlier…") {
                        Task { await store.loadEpisodes(more: true) }
                    }
                    .disabled(store.episodesLoadingMore)
                    .padding(.top, 10)
                }
            }
        }
        .task(id: p.id) { await store.loadEpisodes(more: false) }
    }
}

private struct PersonEpisodeRow: View {
    let store: PersonStore
    let firstName: String
    let fullName: String
    let ep: Episode

    @State private var open = false
    @State private var messages: [HistoryMessage]?
    @State private var more = false
    /// Why the last read of this conversation failed, in the engine's own words (since NC-340
    /// the engine says so when the message archive cannot be read, instead of answering
    /// "no messages").
    @State private var failure: String?

    /// The engine's ceiling on one history request.
    static let historyCap = 200

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                open.toggle()
                // Reopening after a failed load tries again, as the row says it will.
                if open && (messages == nil || failure != nil) {
                    failure = nil
                    messages = nil
                    Task { await fetch() }
                }
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        channelLabel
                        Text(verbatim: Display.fmtDay(ep.tsEnd)).foregroundStyle(Palette.muted)
                        Spacer(minLength: 6)
                        Text(verbatim: countText).foregroundStyle(Palette.muted)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Palette.faint)
                            .rotationEffect(.degrees(open ? 90 : 0))
                    }
                    .font(.system(size: Metrics.fsOverline))
                    Text(verbatim: PersonBubble.visible(ep.snippet) ?? "(media)")
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(open ? nil : 2)
                        .multilineTextAlignment(.leading)
                }
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(open ? .isSelected : [])
            .accessibilityHint(open ? "Hides the messages" : "Shows the messages")

            if open { expanded.padding(.bottom, 8) }
        }
    }

    private var channelLabel: some View {
        let c = Channel(rawValue: ep.channel)
        return HStack(spacing: 4) {
            if let c {
                Image(systemName: Display.channelSymbol(c)).foregroundStyle(Display.channelColor(c))
            }
            Text(verbatim: Display.channelLabel(ep.channel)).foregroundStyle(Palette.ink)
        }
    }

    private var countText: String {
        PersonWords.count(ep.count, "message") + (ep.isGroup ? " from \(firstName)" : "")
    }

    @ViewBuilder private var expanded: some View {
        if let messages {
            VStack(alignment: .leading, spacing: 6) {
                if ep.isGroup {
                    let others = messages.filter { !$0.who.isEmpty && $0.who != fullName }.count
                    if others > 0 {
                        Text(
                            "The whole conversation — \(firstName)'s \(PersonWords.count(ep.count, "message")) and \(PersonWords.count(others, "line")) from everyone else in \(ep.group)."
                        )
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                    }
                }
                if let failure {
                    PersonEmptyLine(
                        text: "Could not load these messages. \(failure)\nClose and open the conversation to try again.")
                }
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(messages) { m in
                        PersonBubble(message: m, who: m.direction == 1 ? "You" : (m.who.isEmpty ? firstName : m.who))
                    }
                }
                if more {
                    Text("The last \(messages.count) messages of a longer conversation.")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
        } else {
            ProgressView().controlSize(.small)
        }
    }

    /// people.js:1880-1915: a normal episode reads its own window (inclusive end, then
    /// trimmed to its start); a group episode reads the whole conversation around it, every
    /// voice included, and never trusts the inline messages (they are one voice only).
    private func fetch() async {
        if !ep.isGroup, let inline = ep.messages, !inline.isEmpty {
            messages = inline.enumerated().map { i, m in
                var h = HistoryMessage()
                h.id = -(i + 1)
                h.channel = m.channel
                h.direction = m.direction
                h.ts = m.ts
                h.subject = m.subject
                h.body = m.body
                h.group = m.group
                return h
            }
            return
        }
        let wide = ep.isGroup && ep.threadSince != nil && ep.threadBefore != nil
        var q = [
            "channel": ep.channel,
            "before": (wide ? ep.threadBefore ?? ep.tsEnd : ep.tsEnd) + "z",
            "limit": String(wide ? Self.historyCap : min(ep.count, Self.historyCap)),
        ]
        if wide {
            q["threads"] = "1"
            q["since"] = ep.threadSince
        }
        do {
            let r = try await store.client.get("/api/contact/\(store.id)/history", query: q, as: HistoryResponse.self)
            let list = wide ? r.messages : r.messages.filter { $0.ts >= ep.tsStart }
            messages = list.reversed()
            more = r.hasMore
        } catch is CancellationError {
        } catch {
            failure = PersonStore.say(error)
            messages = []
        }
    }
}

// MARK: - Messages (the raw history)

private struct PersonMessages: View {
    let store: PersonStore
    let p: PersonDetail
    @State private var messages: [HistoryMessage] = []
    @State private var loaded = false
    @State private var hasMore = false
    @State private var loadingMore = false
    @State private var error: String?

    static let pageSize = 50

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !loaded {
                ProgressView().controlSize(.small).padding(.vertical, 8)
            } else if let error, messages.isEmpty {
                PersonEmptyLine(text: "Could not load the messages. \(error)")
            } else if messages.isEmpty {
                PersonEmptyLine(text: "No messages yet. Import WhatsApp, iMessage or email in Data and they show here.")
            } else {
                Text("Newest first. \(PersonWords.count(p.counts.total, "message")) in all.")
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(messages) { m in
                        PersonBubble(
                            message: m, who: m.direction == 1 ? "You" : (m.who.isEmpty ? store.firstName : m.who),
                            showChannel: true)
                    }
                }
                if hasMore {
                    Button(loadingMore ? "Loading…" : "Earlier…") { Task { await load(more: true) } }
                        .disabled(loadingMore)
                        .padding(.top, 4)
                }
            }
        }
        .task(id: p.id) { await load(more: false) }
    }

    private func load(more: Bool) async {
        var q = ["limit": String(Self.pageSize)]
        // `before` is strict (ts < before). With "z" after the oldest time shown, the next
        // page also holds the other messages from that same second (a burst of photos),
        // as people.js does; the ones already shown are dropped by id below.
        if more, let oldest = messages.last?.ts { q["before"] = oldest + "z" }
        if more { loadingMore = true }
        defer { loadingMore = false }
        do {
            var r = try await store.client.get("/api/contact/\(p.id)/history", query: q, as: HistoryResponse.self)
            let seen = Set(messages.map(\.id))
            if more, r.hasMore, !r.messages.isEmpty, r.messages.allSatisfy({ seen.contains($0.id) }),
               let oldest = messages.last?.ts {
                // A whole page from that one second, all on screen already: step past it.
                q["before"] = oldest
                r = try await store.client.get("/api/contact/\(p.id)/history", query: q, as: HistoryResponse.self)
            }
            let fresh = r.messages.filter { !seen.contains($0.id) }
            messages = more ? messages + fresh : r.messages
            hasMore = r.hasMore && !(more ? fresh : r.messages).isEmpty
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = PersonStore.say(error)
            if more { store.flash(PersonStore.say(error)) }
        }
        loaded = true
    }
}

// MARK: - One message

/// A message: yours on the right, theirs on the left; who and when as text, because
/// alignment alone does not say who spoke. Long bodies fold at 280 characters.
struct PersonBubble: View {
    let message: HistoryMessage
    let who: String
    var showChannel = false
    @State private var expanded = false

    static let foldAt = 280

    /// The text worth showing, or nil when a message is only an attachment (iMessage sends
    /// those as an object-replacement character, which draws as nothing).
    static func visible(_ text: String) -> String? {
        let t = text.replacingOccurrences(of: "\u{FFFC}", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : text.replacingOccurrences(of: "\u{FFFC}", with: "")
    }

    var body: some View {
        let mine = message.direction == 1
        let body = PersonBubble.visible(message.body) ?? "(media)"
        let long = body.count > Self.foldAt
        HStack {
            if mine { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                if !message.subject.isEmpty {
                    Text(verbatim: message.subject).font(.system(size: Metrics.fsUI, weight: .semibold))
                }
                Text(verbatim: long && !expanded ? String(body.prefix(Self.foldAt)) + "…" : body)
                    .font(.system(size: Metrics.fsUI))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if long {
                    Button(expanded ? "Show less" : "Show more") { expanded.toggle() }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                }
                HStack(spacing: 4) {
                    Text(verbatim: who).fontWeight(.medium)
                    if showChannel, let c = Channel(rawValue: message.channel) {
                        Image(systemName: Display.channelSymbol(c)).foregroundStyle(Display.channelColor(c))
                    }
                    if !message.group.isEmpty { Text(verbatim: "in \(message.group)") }
                    Text(verbatim: Display.bubbleTime(message.ts))
                }
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
            }
            .foregroundStyle(Palette.ink)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 10).fill(mine ? Palette.accentSoft : Palette.panel2))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(mine ? Color.clear : Palette.line, lineWidth: 1))
            if !mine { Spacer(minLength: 40) }
        }
        .contextMenu {
            Button("Copy Message") { PersonClipboard.copy(message.body) }
        }
    }
}
