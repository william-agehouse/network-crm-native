import AppKit
import SwiftUI

// Per-member extras on a roster: the person's newest note (with a sheet to add one), and a
// drafted message to them (lists.js:444-445 → ui.js DraftModal). Both reach the person's
// own record through the same routes their page in People uses; the list itself never
// changes a person (lists.js:571-575).

/// The newest note a member has, as the roster shows it.
struct ListsMemberNote: Sendable, Equatable {
    let body: String
    let kind: String
    let createdAt: String
    /// Every note they have (the roster shows the newest).
    let count: Int

    /// From a person's notes (newest first, contract §2). Automatic notes are not theirs.
    init?(notes: [Note]) {
        let own = notes.filter { $0.kind != "auto" }
        guard let n = own.first else { return nil }
        body = n.body
        kind = n.kind
        createdAt = n.createdAt
        count = own.count
    }

    /// One line for the cell: the first line of the note.
    var line: String {
        body.split(whereSeparator: \.isNewline).first.map(String.init)?.trimmingCharacters(in: .whitespaces) ?? body
    }

    /// people.js:2098-2110: "how you met", "voice note", else "note".
    var label: String {
        switch kind {
        case "how_we_met": "how you met"
        case "voice": "voice note"
        default: "note"
        }
    }

    var tooltip: String {
        let head = [label.prefix(1).uppercased() + label.dropFirst(), Display.fmtDay(createdAt)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        let more = count > 1 ? "\n\n\(count - 1) earlier \(count == 2 ? "note" : "notes") on their page in People." : ""
        return "\(head)\n\(body)\(more)"
    }
}

/// The roster's Note cell: newest note, or a quiet "No note yet" (never blank).
struct ListsNoteCell: View {
    let store: ListsStore
    let person: PersonSummary

    var body: some View {
        // nil before the note arrives and when there is none; only "asked and none" says so.
        let note = store.memberNotes[person.id]
        Group {
            if let note {
                Text(verbatim: note.line)
                    .foregroundStyle(Palette.muted)
                    .help(note.tooltip)
            } else if store.noteChecked(person.id) {
                Text("No note yet").foregroundStyle(Palette.faint)
            } else {
                Text(verbatim: " ")
            }
        }
        .font(.system(size: Metrics.fsUI))
        .lineLimit(1)
        .truncationMode(.tail)
        .task(id: person.id) { await store.loadNote(person.id) }
    }
}

/// "Add a note about Ada…": saved on the person, newest first on their page.
struct ListsNoteSheet: View {
    let store: ListsStore
    let person: PersonSummary
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focused: Bool

    private var first: String { person.name.split(separator: " ").first.map(String.init) ?? person.name }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                ListsAvatar(person: person, size: 30)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Note about \(first)")
                        .font(.system(size: Metrics.fsTitle, weight: .semibold))
                    Text("It is saved on \(first)’s page in People, with the date.")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
            TextField("Add a note about \(first)…", text: $text, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(4...10)
                .focused($focused)
                .disabled(busy)
                .onSubmit(save)
                .accessibilityLabel("Note text")
            if let latest = store.memberNotes[person.id] {
                Text("Latest \(latest.label): \(latest.line)")
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.muted)
                    .lineLimit(2)
            }
            if let error {
                Text(error)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Text("Option-Return adds a line")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Palette.faint)
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(busy ? "Saving…" : "Save Note", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 420)
        .onAppear { focused = true }
    }

    private func save() {
        guard !busy, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        busy = true
        error = nil
        Task {
            // A failure keeps what was typed.
            if let problem = await store.addNote(text, to: person.id) {
                error = problem
                busy = false
            } else {
                busy = false
                dismiss()
            }
        }
    }
}

/// A short message to one member, written by the engine's AI (costs money: only ever on a
/// click). From the web page's DraftModal (static/app/ui.js:425-471).
///
///   POST /api/contact/{id}/draftmessage {}  → {message, meta}   (400 without an API key)
struct ListsDraftSheet: View {
    let person: PersonSummary
    @AppEnvironment private var app
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var meta: BriefMeta?
    @State private var busy = true
    @State private var error: String?
    @State private var copied = false

    private var first: String { person.name.split(separator: " ").first.map(String.init) ?? person.name }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Message for \(first)")
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
            if busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Writing the message…").foregroundStyle(Palette.muted)
                }
                .frame(maxWidth: .infinity, minHeight: 120)
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
                    .accessibilityLabel("Draft message")
                if let meta { receipt(meta) }
            }
            HStack {
                Button {
                    Task { await generate() }
                } label: {
                    Label("Write Another", systemImage: "arrow.clockwise")
                }
                .disabled(busy)
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(copied ? "Copied" : "Copy") {
                    copied = ListsExport.copy(text)
                    if !copied { error = "Could not copy. Select the text and press ⌘C." }
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

    /// "From 12 messages · 2 channels · built Oct 14, 2026" (ui.js:333-347).
    private func receipt(_ m: BriefMeta) -> some View {
        let bits = [
            "From \(m.messages) \(m.messages == 1 ? "message" : "messages")",
            "\(m.channels) \(m.channels == 1 ? "channel" : "channels")",
            m.builtAt.isEmpty ? "" : "built \(Display.fmtDay(m.builtAt))",
        ].filter { !$0.isEmpty }
        return Text(verbatim: bits.joined(separator: " · "))
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(Palette.muted)
    }

    private func generate() async {
        busy = true
        error = nil
        copied = false
        do {
            let r: DraftResult = try await app.client.post(
                "/api/contact/\(person.id)/draftmessage", timeout: EngineClient.askTimeout)
            text = r.message
            meta = r.meta
        } catch is CancellationError {
        } catch {
            let said = ListsStore.sentence(error)
            self.error = said.contains("ANTHROPIC") ? "Drafts need an API key. Open Settings." : said
        }
        busy = false
    }
}
