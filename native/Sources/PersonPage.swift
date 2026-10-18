import SwiftUI

// A person's page, shown beside the People table (about 380 to 600 points wide) and,
// when the window gives it room, in two columns. It loads GET /api/contact/{id} and shows
// what the web page's detail pane shows, in its order and its words
// (static/app/people.js:2129-2157, contract §8.7):
//
//   header (photo, name, role, company, where, tags, actions) · the four vital tiles ·
//   Going cold · Coming up · Story · Timeline (notes, conversations, messages)
//   then Details · Channels · Phone and email · Dates · Lists · Mutual connections
//
// Contract with the rest of the app: `PersonPage(contactID:)`, reads AppModel from the
// environment, talks to the engine only through `app.client`, and calls
// `app.markSettled()` once its first load finished (page or error on screen). The People
// screen also hands it where the person sits in the table (`steps`, for ‹ 3 / 12 ›), a
// one-shot `intent` ("Add a Note", "Draft a Message…" from the table) and how to close.

struct PersonPage: View {
    let contactID: Int
    var steps: PersonSteps? = nil
    var intent: Binding<PersonIntent?> = .constant(nil)
    var onStep: (Int) -> Void = { _ in }
    var onClose: (() -> Void)? = nil
    @AppEnvironment private var app
    @State private var store: PersonStore?

    var body: some View {
        ZStack {
            if let store, store.id == contactID {
                PersonPageBody(store: store, steps: steps, onStep: onStep, onClose: onClose)
            } else {
                PersonLoading()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.panel)
        // A new person gets a new store; leaving one cancels its load (the task is
        // cancelled when contactID changes, and the request with it). A merge into this
        // person bumps their reload count, which reads the page again from scratch.
        .task(id: PageKey(id: contactID, reload: app.personReloads[contactID, default: 0])) {
            let s = PersonStore(id: contactID, client: app.client)
            let model = app
            s.onWrite = { model.peopleChanged() }
            store = s
            take(intent.wrappedValue, into: s)
            await s.load()
            if !Task.isCancelled { app.markSettled() }
        }
        .onChange(of: intent.wrappedValue) { _, new in if let store { take(new, into: store) } }
    }

    private struct PageKey: Equatable {
        let id: Int
        let reload: Int
    }

    /// Hands a request from the table to this person's store, once.
    private func take(_ request: PersonIntent?, into store: PersonStore) {
        guard let request, request.id == contactID, store.id == contactID else { return }
        switch request.kind {
        case .note: store.pendingNoteFocus = true
        case .draft: store.pendingDraft = true
        }
        intent.wrappedValue = nil
    }
}

private struct PersonLoading: View {
    var body: some View {
        ProgressView()
            .controlSize(.small)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct PersonPageBody: View {
    let store: PersonStore
    let steps: PersonSteps?
    let onStep: (Int) -> Void
    let onClose: (() -> Void)?
    @AppEnvironment private var app
    @State private var width: CGFloat = 0

    /// Wider than this, Details and the rest move into a second column, as on the web page.
    static let twoColumnWidth: CGFloat = 900

    var body: some View {
        Group {
            if let p = store.person {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 10) {
                            PersonTopBar(steps: steps, onStep: onStep, onClose: onClose)
                            content(p)
                        }
                        .padding(.horizontal, Metrics.padView)
                        .padding(.top, 10)
                        .padding(.bottom, 48)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                    }
                    // "Add a Note" from the table: show the composer, scroll to it, put
                    // the caret in it (people.js:2051-2058).
                    .onChange(of: store.pendingNoteFocus, initial: true) { _, pending in
                        guard pending else { return }
                        store.pendingNoteFocus = false
                        if store.timelineTab != .all && store.timelineTab != .notes { store.timelineTab = .all }
                        Task { @MainActor in
                            try? await Task.sleep(for: .milliseconds(80))
                            withAnimation(.easeOut(duration: 0.25)) {
                                proxy.scrollTo(PersonNoteComposer.scrollID, anchor: .center)
                            }
                            store.noteFocusToken += 1
                        }
                    }
                }
                .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
                .task(id: p.id) { await store.loadLists() }
            } else if store.isLoading {
                PersonLoading()
            } else if let failure = store.loadFailure {
                PersonLoadFailed(store: store, message: failure.message, missing: failure.missing)
            } else {
                PersonLoading()
            }
        }
        .overlay(alignment: .bottom) {
            if let text = store.flashText {
                PersonFlash(text: text)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: store.flashText)
    }

    @ViewBuilder private func content(_ p: PersonDetail) -> some View {
        if width >= Self.twoColumnWidth {
            HStack(alignment: .top, spacing: 32) {
                main(p).frame(maxWidth: 720, alignment: .topLeading)
                side(p).frame(width: 320, alignment: .topLeading)
            }
            .frame(maxWidth: 1100, alignment: .topLeading)
        } else {
            VStack(alignment: .leading, spacing: 26) {
                main(p)
                side(p)
            }
            .frame(maxWidth: 720, alignment: .topLeading)
        }
    }

    private func main(_ p: PersonDetail) -> some View {
        VStack(alignment: .leading, spacing: 26) {
            PersonHeader(store: store, p: p)
            if let action = p.nextAction {
                PersonNextCard(store: store, action: action)
            }
            PersonComingUp(dates: p.dates)
            PersonStory(store: store, p: p)
            PersonTimeline(store: store, p: p)
        }
    }

    private func side(_ p: PersonDetail) -> some View {
        VStack(alignment: .leading, spacing: 26) {
            PersonDetails(store: store, p: p)
            PersonChannels(store: store, p: p)
            PersonIdentifiers(store: store, p: p)
            PersonDates(store: store, p: p)
            PersonLists(store: store)
            PersonMutuals(mutuals: p.mutuals)
        }
    }
}

private struct PersonLoadFailed: View {
    let store: PersonStore
    let message: String
    let missing: Bool
    @AppEnvironment private var app
    @State private var retrying = false

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: missing ? "person.crop.circle.badge.questionmark" : "exclamationmark.triangle")
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(Palette.muted)
            Text(missing ? "This person is not in your network." : "Could not load this person.")
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
                .foregroundStyle(Palette.ink)
            Text(verbatim: missing ? "They may have been erased permanently. Pick someone else in the list." : message)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            HStack {
                if missing {
                    Button("Back to People") { app.selectedPersonID = nil }
                } else {
                    Button(retrying ? "Trying…" : "Try Again") {
                        retrying = true
                        Task {
                            await store.load()
                            retrying = false
                        }
                    }
                    .disabled(retrying)
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding(.top, 4)
        }
        .padding(Metrics.padView)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The page's top line: ‹ 3 / 12 › through the table's current order (people.js:1321-1369,
/// ui.js:350-358), and a way back to the full table.
private struct PersonTopBar: View {
    let steps: PersonSteps?
    let onStep: (Int) -> Void
    let onClose: (() -> Void)?

    var body: some View {
        HStack(spacing: 6) {
            Spacer(minLength: 0)
            if let steps {
                HStack(spacing: 0) {
                    Button {
                        if let id = steps.previous { onStep(id) }
                    } label: {
                        Image(systemName: "chevron.left").frame(width: 22, height: 18)
                    }
                    .disabled(steps.previous == nil)
                    .help("Previous")
                    .accessibilityLabel("Previous")
                    Text(verbatim: "\(Display.number(steps.index)) / \(Display.number(steps.total))")
                        .font(.system(size: Metrics.fsOverline).monospacedDigit())
                        .foregroundStyle(Palette.muted)
                        .padding(.horizontal, 4)
                        .fixedSize()
                    Button {
                        if let id = steps.next { onStep(id) }
                    } label: {
                        Image(systemName: "chevron.right").frame(width: 22, height: 18)
                    }
                    .disabled(steps.next == nil)
                    .help("Next")
                    .accessibilityLabel("Next")
                }
                .buttonStyle(.borderless)
            }
            if let onClose {
                Button(action: onClose) {
                    Image(systemName: "xmark").frame(width: 22, height: 18)
                }
                .buttonStyle(.borderless)
                .help("Close and show the full table (Esc in the table)")
                .accessibilityLabel("Close")
            }
        }
        .font(.system(size: Metrics.fsUI))
        .foregroundStyle(Palette.muted)
        .frame(height: 20)
    }
}
