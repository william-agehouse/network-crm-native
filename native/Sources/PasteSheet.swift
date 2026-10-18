import SwiftUI

// Paste people in, review what was found, then save (NC-016). The web page's version is
// static/app/intake.js; its three screens are this sheet's three phases:
//
//   1 · Paste    a text box, "Read this list" (and "Read it with the model" when a key is set)
//   2 · Review   what the paste held, then one card per person: matched or new, fields to
//                edit, a name to confirm, Skip; then Save, which asks before writing
//   3 · Saved    the receipt of what actually happened
//
// Nothing reaches the database until the list has been on screen and the commit carries
// confirm:true (intake.js:10-15). ⌘Return reads the paste; Esc closes the sheet.

/// The paste sheet with the work kept between openings: a list pasted, or half reviewed,
/// is still there when the sheet is opened again, so Esc (or Close) never throws it away.
/// The web page is a page you navigate away from; a sheet is closed by one key.
struct PasteSheetRemembered: View {
    @AppEnvironment private var app
    var body: some View { PasteSheet(store: PasteMemory.store(for: app)) }
}

/// One PasteStore per AppModel, for as long as that model lives (weak keys: a snapshot's
/// throwaway models take their stores with them).
@MainActor
enum PasteMemory {
    private static let stores = NSMapTable<AppModel, PasteStore>.weakToStrongObjects()

    static func store(for app: AppModel) -> PasteStore {
        if let s = stores.object(forKey: app) { return s }
        let s = PasteStore()
        stores.setObject(s, forKey: app)
        return s
    }
}

struct PasteSheet: View {
    @AppEnvironment private var app
    @State private var store: PasteStore

    init(store: PasteStore = PasteStore()) {
        _store = State(initialValue: store)
    }

    var body: some View {
        @Bindable var store = store
        SetupSheetChrome(
            title: "Paste people in",
            subtitle: "One person per line — a name, an email signature, a LinkedIn address. Nothing is saved until you have read the list and confirmed it.",
            size: CGSize(width: 780, height: 720),
            notices: store.notices
        ) {
            PastePhases(phase: store.phase)
        } content: {
            switch store.phase {
            case .paste: PastePastePhase(store: store)
            case .review: PasteReviewPhase(store: store)
            case .done:
                if let receipt = store.receipt { PasteReceiptView(receipt: receipt) }
            }
        } footer: {
            PasteFooter(store: store)
        }
        .task {
            // --snapshot's review picture: the made-up sample, read on the scratch engine.
            let sample = app.snapshotHint == "paste-review"
            if sample { app.snapshotHint = nil }
            await store.start(app: app, settle: !sample)
            if sample {
                await store.readSampleForSnapshot()
                app.markSettled()
            }
        }
        .confirmationDialog("Save this batch?", isPresented: $store.confirming) {
            Button(store.busy == .commit ? "Saving…" : "Confirm") { Task { await store.commit() } }
                .keyboardShortcut(.defaultAction)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(store.confirmSentence)
        }
    }
}

/// "1 · Paste  2 · Review  3 · Saved" (intake.js:553-557).
private struct PastePhases: View {
    let phase: PasteStore.Phase

    var body: some View {
        HStack(spacing: 4) {
            item("1 · Paste", on: phase == .paste)
            item("2 · Review", on: phase == .review)
            item("3 · Saved", on: phase == .done)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Where you are")
    }

    private func item(_ text: String, on: Bool) -> some View {
        Text(text)
            .font(.system(size: Metrics.fsOverline, weight: on ? .semibold : .regular))
            .foregroundStyle(on ? Palette.ink : Palette.muted)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusPill).fill(on ? Palette.hover : .clear))
            .fixedSize()
    }
}

// MARK: - 1 · Paste

private struct PastePastePhase: View {
    let store: PasteStore
    @FocusState private var focused: Bool

    static let placeholder = "Ada Lovelace — Analytical Engines, London\nlinkedin.com/in/some-profile\nGrace Hopper, COBOL, grace@example.com, +46 70 174 06 05"

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 14) {
            if store.status == nil && store.err.isEmpty && !store.loaded {
                ProgressView().controlSize(.small)
            }
            if let offer = store.draft { PasteDraftBanner(store: store, offer: offer) }

            ZStack(alignment: .topLeading) {
                TextEditor(text: $store.text)
                    .font(.system(size: Metrics.fsUI))
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .focused($focused)
                    .accessibilityLabel("People to add, one per line")
                if store.text.isEmpty {
                    Text(Self.placeholder)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.faint)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 6)
                        .allowsHitTesting(false)
                }
            }
            .frame(height: 230)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                .strokeBorder(focused ? Palette.accent : Palette.lineStrong, lineWidth: 1))

            HStack(alignment: .center, spacing: 8) {
                Text(countLine)
                    .font(.system(size: Metrics.fsOverline + 1).monospacedDigit())
                    .foregroundStyle(store.overLimit > 0 ? Palette.gold : Palette.muted)
                Spacer(minLength: 8)
                let empty = store.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                if store.status?.llm == true {
                    Button(store.busy == .propose ? "Asking the model…" : "Read it with the model") {
                        Task { await store.read(.propose) }
                    }
                    .disabled(empty || store.busy != .none)
                    .help("Sends the pasted text to Anthropic, and the model proposes records")
                }
                Button(store.busy == .parse ? "Reading…" : "Read this list") {
                    Task { await store.read(.parse) }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(empty || store.busy != .none)
                .help("Read the list on this Mac (⌘Return)")
            }

            if let status = store.status { PasteCapabilities(status: status) }
        }
        .onAppear { focused = true }
    }

    // from static/app/intake.js:579-582
    private var countLine: String {
        var s = "\(Display.number(store.textLength))"
        if store.pasteMax > 0 { s += " / \(Display.number(store.pasteMax))" }
        s += " characters"
        if store.overLimit > 0 { s += " · \(Display.number(store.overLimit)) past the limit will not be read" }
        return s
    }
}

private struct PasteDraftBanner: View {
    let store: PasteStore
    let offer: PasteDraftOffer

    var body: some View {
        SetupCard(tone: .warn) {
            HStack(spacing: 8) {
                Text("A draft of \(SetupWords.people(offer.draft.proposals.count)) is parked from last time. Nobody in it has been added.")
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Button("Open the draft") { store.resume() }
                Button(store.busy == .discard ? "Discarding…" : "Discard it") { Task { await store.discard() } }
                    .disabled(store.busy != .none)
            }
        }
    }
}

/// "What this can do on this Mac" (intake.js:597-614).
private struct PasteCapabilities: View {
    let status: PasteIntakeStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SetupSectionLabel(text: "What this can do on this Mac")
            SetupBullets(lines: status.sentences)
            Text(summary)
                .font(.system(size: Metrics.fsOverline + 1))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// NC-365: plain words only. The setting names, database columns and match keys this
    /// used to print are gone; the engine's own sentences above say what it does.
    private var summary: String {
        var s = "Up to \(SetupWords.people(status.limit("NC_INTAKE_MAX_PEOPLE") ?? 0)) per paste. Paste a longer list in parts."
        if status.llm { s += " The model is \(status.model)." }
        return s
    }
}

// MARK: - The foot of the sheet

private struct PasteFooter: View {
    let store: PasteStore
    @AppEnvironment private var app

    var body: some View {
        switch store.phase {
        case .paste:
            EmptyView()
        case .review:
            let c = store.counts
            let busy = store.busy != .none
            Button("Start again") { store.reset() }.disabled(busy)
            Button(store.busy == .park ? "Saving…" : "Save the draft") { Task { await store.park() } }.disabled(busy)
            Button(store.busy == .discard ? "Discarding…" : "Discard the draft") { Task { await store.discard() } }.disabled(busy)
            Text(barLine(c))
                .font(.system(size: Metrics.fsOverline + 1))
                .foregroundStyle(Palette.muted)
                .lineLimit(2)
                .frame(maxWidth: 220, alignment: .leading)
            Spacer(minLength: 4)
            // Disabled when nothing would be written, rather than offering a confirm that
            // resolves to "0 people added" (intake.js:642-650).
            Button(c.write > 0 ? "Save \(SetupWords.people(c.write))…" : "Nothing to save") {
                store.confirming = true
            }
            .buttonStyle(.borderedProminent)
            .disabled(c.write == 0 || busy)
        case .done:
            Button("Paste another list") { store.startOver() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
            Button("Open People") {
                app.section = .people
                app.setupFlow = nil
            }
        }
    }

    // from static/app/intake.js:638-641
    private func barLine(_ c: PasteStore.Counts) -> String {
        if !store.saved.isEmpty { return store.saved }
        if c.write == 0 && !store.rows.isEmpty {
            return "Every row above is refused or still needs a name, so there is nothing to save yet."
        }
        return ""
    }
}
